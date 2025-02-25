from influxdb import InfluxDBClient
import json
import time
import socket
import os
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from flask import Flask, request, jsonify  # type: ignore
from flask_cors import CORS  # type: ignore

# Configuration
host_ip = '0.0.0.0'
host_port = 12345  # Port for system metrics
influxdb_host = "localhost"
influxdb_port = 8086
influxdb_db = "system_metrics"
influxdb_user = "root"
influxdb_password = "root"

SAVE_DIR = "/home/bach/python_script/pictures/"
SAVE_PATH = os.path.join(SAVE_DIR, "latest_image.png")
IMAGE_PORT = 55555
HTTP_PORT = 8080
FLASK_PORT = 5000

TARGET_IP = "10.42.0.91"  # Target system IP
TARGET_PORT = 54321       # Target system port

# Initialize Flask
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Function to receive images over socket
def receive_image():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("0.0.0.0", IMAGE_PORT))
    server_socket.listen(1)

    while True:
        conn, addr = server_socket.accept()
        print(f"Receiving image from {addr}")

        with open(SAVE_PATH, "wb") as f:
            while chunk := conn.recv(4096):
                f.write(chunk)

        print(f"Image saved at {SAVE_PATH}")
        conn.close()

# Function to serve images over HTTP
def run_http_server():
    os.chdir(SAVE_DIR)
    httpd = HTTPServer(("0.0.0.0", HTTP_PORT), SimpleHTTPRequestHandler)
    print(f"Serving images on port {HTTP_PORT}...")
    httpd.serve_forever()

# Function to receive system metrics and store them in InfluxDB
def receive_metrics():
    client = InfluxDBClient(influxdb_host, influxdb_port, influxdb_user, influxdb_password, influxdb_db)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((host_ip, host_port))
    server_socket.listen(1)

    print("System metrics server listening for connections...")
    
    while True:
        client_socket, client_address = server_socket.accept()
        print(f"Connection established with {client_address}")

        buffer = ""
        while True:
            data = client_socket.recv(1024 * 10).decode()
            if not data:
                break

            buffer += data
            while "\n" in buffer:
                message, buffer = buffer.split("\n", 1)
                try:
                    system_info = json.loads(message)

                    # Per-core CPU data
                    per_core_usage_data = {
                        f"per_core_usage{i}": float(system_info["per_core_usage"].get(f"core_{i}_usage", 0))
                        for i in range(32)
                    }
                    per_core_freq_data = {
                        f"per_core_freq{i}": float(system_info["per_core_freq"].get(f"core_{i}_frequency", 0))
                        for i in range(32)
                    }

                    # Prepare data for InfluxDB
                    json_body = [
                        {
                            "measurement": "system_metrics",
                            "tags": {"host": client_address[0]},
                            "fields": {
                                "cpu_usage": float(system_info["cpu_usage"]),
                                "memory_usage": float(system_info["memory_usage"]),
                                "swap_usage": float(system_info["swap_usage"]),
                                "cpu_temperature": float(system_info.get("cpu_temperature", 0.0)),
                                "uptime_seconds": float(system_info["uptime_seconds"]),
                                "total_memory": float(system_info["total_memory"]),
                                "total_swap": float(system_info["total_swap"]),
                                "num_threads": int(system_info["num_threads"]),
                                "download_speed": float(system_info.get("download_speed", 0.0)),
                                "upload_speed": float(system_info.get("upload_speed", 0.0)),
                                "cpu_power": float(system_info.get("cpu_power", 0.0)),
                                "total_disk_usage": float(system_info.get("total_disk_usage", 0.0)),
                                "total_disk_size": float(system_info.get("total_disk_size", 0.0)),
                                **per_core_usage_data,
                                **per_core_freq_data
                            },
                            "time": int(time.time() * 1e9)  # Nanoseconds
                        }
                    ]

                    # Write data to InfluxDB
                    client.write_points(json_body)
                except json.JSONDecodeError as e:
                    print(f"JSON Decode Error: {e}. Skipping message.")

        client_socket.close()

# Flask route to send messages to target system
@app.route('/send_message', methods=['POST'])
def send_message():
    data = request.get_json()
    message = data.get("message", "Default message from host")

    if message == "3":
        print("Received message '3', deleting all files in pictures directory...")
        for file_name in os.listdir(SAVE_DIR):
            file_path = os.path.join(SAVE_DIR, file_name)
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    print(f"Deleted {file_path}")
            except Exception as e:
                print(f"Error deleting {file_path}: {e}")
        return jsonify({"status": "success", "message": "All images deleted"})
    
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
            client_socket.connect((TARGET_IP, TARGET_PORT))
            client_socket.sendall(message.encode())
        return jsonify({"status": "success", "message": message})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# Function to run Flask server
def run_flask_server():
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=True, use_reloader=False)

# Start all services in separate threads
threading.Thread(target=receive_image, daemon=True).start()
threading.Thread(target=run_http_server, daemon=True).start()
threading.Thread(target=receive_metrics, daemon=True).start()
threading.Thread(target=run_flask_server, daemon=True).start()

# Keep main thread alive
while True:
    time.sleep(1)
