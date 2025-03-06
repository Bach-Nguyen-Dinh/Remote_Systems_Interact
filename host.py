from influxdb import InfluxDBClient
import json
import time
import socket
import os
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from flask import Flask, request, jsonify, send_from_directory  # type: ignore
from flask_cors import CORS  # type: ignore

# Configuration
HOST_IP = '0.0.0.0'
SYSINFO_PORT = 12345  # Port for system metrics
IMAGE_PORT = 55555
HTTP_PORT = 8080
FLASK_PORT = 5000

TARGET_IP = "10.42.0.91"  # Target system IP
TARGET_PORT = 54321       # Target system port

INFLUXDB_HOST = "localhost"
INFLUXDB_PORT = 8086
INFLUXDB_DB = "system_metrics"
INFLUXDB_USER = "root"
INFLUXDB_PASSWORD = "root"

SAVE_DIR = "/home/bach/python_script/pictures/"
# SAVE_PATH = os.path.join(SAVE_DIR, "latest_image.png")
SAVE_PATH_TIF = os.path.join(SAVE_DIR, "tif_image.webp")
SAVE_PATH_OUT = os.path.join(SAVE_DIR, "out_image.png")

# Global variable
message = ""
cphd_file_list = []  # Global list to store CPHD file names
cphd_file_properties = []  # Global list to store properties of a CPHD file
tif_file_properties = []

# Initialize Flask
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

def receive_image():
    global message, cphd_file_list, cphd_file_properties, tif_file_properties
    imageSaved = False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((HOST_IP, IMAGE_PORT))
        server_socket.listen(1)
        print(f"Listening for incoming image on {HOST_IP}:{IMAGE_PORT}...")

        while True:
            conn, addr = server_socket.accept()
            with conn:
                print(f"Receiving data from {addr}")

                # Check if message is related to an image
                if message.startswith("RUN:") and imageSaved == False:
                    save_path = SAVE_PATH_TIF
                else:
                    save_path = None
                    imageSaved = False

                print(f"Current save path: {save_path}")

                if save_path:
                    # Receive file size first
                    file_size = int.from_bytes(conn.recv(8), byteorder="big")
                    print(f"Expecting to receive {file_size} bytes...")

                    received_data = b""
                    while len(received_data) < file_size:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        received_data += chunk

                    if len(received_data) == file_size:
                        with open(save_path, "wb") as f:
                            f.write(received_data)
                        print(f"Image received and saved as {save_path} ({len(received_data)} bytes)")
                    else:
                        print(f"Error: Received {len(received_data)} bytes, expected {file_size} bytes")
                    imageSaved = True
                    
                else:
                    data = conn.recv(4096).decode()
                    try:
                        received_data = json.loads(data)

                        # Check if it's a file size response
                        if "filename" in received_data and "size" in received_data:
                            file_name = received_data["filename"]
                            file_size_str = received_data["size"]
                            print(f"File '{file_name}' has a size of '{file_size_str}'.")
                            # Update the dictionary to store the formatted size
                            cphd_file_properties = received_data

                        # Check if it's a list of CPHD files
                        elif "cphd_files" in received_data:
                            cphd_file_list = received_data["cphd_files"]
                            print(f"Updated CPHD file list: {cphd_file_list}")

                        elif "tif_filename" in received_data:
                            file_name = received_data["tif_filename"]
                            file_size_str = received_data["size"]
                            print(f"File '{file_name}' has a size of '{file_size_str}'.")
                            # Update the dictionary to store the formatted size
                            tif_file_properties = received_data

                        else:
                            print(f"Received unknown data: {received_data}")

                    except json.JSONDecodeError as e:
                        print(f"Error decoding received data: {e}")


# # Function to serve images over HTTP
# def run_http_server():
#     os.chdir(SAVE_DIR)
#     httpd = HTTPServer(("0.0.0.0", HTTP_PORT), SimpleHTTPRequestHandler)
#     print(f"Serving images on port {HTTP_PORT}...")
#     httpd.serve_forever()

# Override to suppress logging for specific status codes (200 and 404)
class CustomHTTPRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Extract the status code from the format
        status_code = args[-2]  # The second-to-last argument is the status code
        
        # Suppress logs for 404 status code (and 200 if needed)
        if status_code == "200" or status_code == "404":
            return  # Do not log the message
        
        # Call the original log_message method for other status codes
        super().log_message(format, *args)

def run_http_server():
    # Set the working directory to serve files from
    os.chdir(SAVE_DIR)
    # Start the HTTP server with the custom request handler
    httpd = HTTPServer((HOST_IP, HTTP_PORT), CustomHTTPRequestHandler)
    print(f"Serving images on port {HTTP_PORT}...")
    httpd.serve_forever()

# Function to receive system metrics and store them in InfluxDB
def receive_metrics():
    client = InfluxDBClient(INFLUXDB_HOST, INFLUXDB_PORT, INFLUXDB_USER, INFLUXDB_PASSWORD, INFLUXDB_DB)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((HOST_IP, SYSINFO_PORT))
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
                                "progress_update": float(system_info.get("progress_update", 0.0)),
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
    global message
    data = request.get_json()
    message = data.get("message", "Default message from host")

    if message == "3":
        return delete_all_files()
    elif message.startswith("RUN:"):
        global tif_file_properties
        tif_file_properties = []
        
    return forward_message_to_target(message)   
    
def delete_all_files():
    """Deletes all files in the SAVE_DIR directory."""
    global cphd_file_list, cphd_file_properties, tif_file_properties
    cphd_file_list = []
    cphd_file_properties = []
    tif_file_properties = []

    try:
        file_list = os.listdir(SAVE_DIR)
        if not file_list:
            return jsonify({"status": "success", "message": "No files to delete"})

        for file_name in file_list:
            file_path = os.path.join(SAVE_DIR, file_name)
            os.remove(file_path)
            print(f"Deleted: {file_path}")

        return forward_message_to_target("3")  # Notify target system

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    
def forward_message_to_target(message):
    """Sends a message to the target system via a socket."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
            client_socket.connect((TARGET_IP, TARGET_PORT))
            client_socket.sendall(message.encode())
        return jsonify({"status": "success", "message": message})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    
@app.route('/get_cphd_files', methods=['GET'])
def get_cphd_files():
    """Returns the latest list of CPHD files"""
    return jsonify({"files": cphd_file_list})

@app.route('/get_cphd_file_properties', methods=['GET'])
def get_cphd_file_properties():
    """Returns the latest list of CPHD files"""
    return jsonify({"files": cphd_file_properties})

@app.route('/get_tif_file_properties', methods=['GET'])
def get_tif_file_properties():
    """Returns the latest list of CPHD files"""
    return jsonify({"files": tif_file_properties})

# Serve static files from the SAVE_DIR
@app.route('/images/<filename>')
def serve_image(filename):
    """Serve images from the SAVE_DIR directory."""
    return send_from_directory(SAVE_DIR, filename)

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
