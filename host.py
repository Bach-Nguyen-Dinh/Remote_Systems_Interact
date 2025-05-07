from influxdb import InfluxDBClient
import json
import time
import socket
import os
import threading
import subprocess
from http.server import SimpleHTTPRequestHandler, HTTPServer
from flask import Flask, request, jsonify, send_from_directory, send_file  # type: ignore
from flask_cors import CORS  # type: ignore

# Configuration
HOST_IP = '0.0.0.0'
SYSINFO_PORT = 12345  # Port for system metrics
NETTEST_PORT = 29102
IMAGE_PORT = 55555
FLASK_PORT = 5000

TARGET_IP = "10.42.0.90"  # Target system IP
TARGET_PORT = 54321       # Target system port

INFLUXDB_HOST = "localhost"
INFLUXDB_PORT = 8086
INFLUXDB_DB = "system_metrics"
INFLUXDB_USER = "root"
INFLUXDB_PASSWORD = "root"

CURR_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR = os.path.join(CURR_DIR, "pictures")
# Check if the "pictures" folder exists, create it if not
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)
    print(f"Folder 'pictures' created at: {SAVE_DIR}")
else:
    print(f"Folder 'pictures' already exists at: {SAVE_DIR}")
SAVE_PATH_TIF = os.path.join(SAVE_DIR, "tif_image.webp")
SAVE_PATH_OUT = os.path.join(SAVE_DIR, "out_image.png")
SAVE_PATH_IPERF_LW_ETH_OB = os.path.join(CURR_DIR, "iperf3_end_result_LwEthOnb.json")
SAVE_PATH_IPERF_UP_ETH_OB = os.path.join(CURR_DIR, "iperf3_end_result_UpEthOnb.json")
SAVE_PATH_IPERF_LW_ETH_ADT = os.path.join(CURR_DIR, "iperf3_end_result_LwEthAdt.json")
SAVE_PATH_IPERF_UP_ETH_ADT = os.path.join(CURR_DIR, "iperf3_end_result_UpEthAdt.json")

# Global variable
message = ""
cphd_file_list = []  # Global list to store CPHD file names
cphd_file_properties = []  # Global list to store properties of a CPHD file
tif_file_properties = []
final_results = {
    "sender_transfer": "",
    "sender_bitrate": "",
    "sender_jitter": "",
    "sender_loss": "",
    "receiver_transfer": "",
    "receiver_bitrate": "",
    "receiver_jitter": "",
    "receiver_loss": ""
}
netTestDuration = 0
flag_get_image = False

# Initialize Flask
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

def handle_image_process_server():
    global message, cphd_file_list, cphd_file_properties, tif_file_properties, flag_get_image
    imageSaved = False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((HOST_IP, IMAGE_PORT))
        server_socket.listen(1)
        print(f"Listening for incoming image on {HOST_IP}:{IMAGE_PORT}...")
        while True:
            conn, addr = server_socket.accept()
            with conn:
                print(f"Receiving data from {addr}")
                # expect receiving an image only if there is no current saved image
                # if new image is already saved, skip to expect other data
                if flag_get_image == True and imageSaved == False:
                    flag_get_image == False
                    save_path = SAVE_PATH_TIF
                    # Receive file size first
                    file_size = int.from_bytes(conn.recv(8), byteorder="big")
                    print(f"Expecting to receive {file_size} bytes...")
                    # Receive the actual data
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
                    save_path = None
                    imageSaved = False
                    data = conn.recv(4096).decode()
                    try:
                        received_data = json.loads(data)
                        # Check if it's a list of CPHD files
                        if "cphd_files" in received_data:
                            cphd_file_list = received_data["cphd_files"]
                            print(f"Updated CPHD file list: {cphd_file_list}")
                        # Check if it's the CPHD file's metrics
                        elif "filename" in received_data and "size" in received_data:
                            file_name = received_data["filename"]
                            file_size_str = received_data["size"]
                            print(f"File '{file_name}' has a size of '{file_size_str}'.")
                            # Update the dictionary to store the formatted size
                            cphd_file_properties = received_data
                        # Check if it's the TIF file's metrics
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
                print(f"Current save path: {save_path}")

def handle_net_test_server():
    global message, cphd_file_list, cphd_file_properties, tif_file_properties
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((HOST_IP, NETTEST_PORT))
        server_socket.listen(1)
        print(f"Listening for incoming net tesk results on {HOST_IP}:{NETTEST_PORT}...")
        while True:
            conn, addr = server_socket.accept()
            with conn:
                print(f"Receiving data from {addr}")
                if message.startswith("NETRUN:"):
                    if "LwEthAdt" in message:
                        save_path = SAVE_PATH_IPERF_LW_ETH_ADT
                    elif "UpEthAdt" in message:
                        save_path = SAVE_PATH_IPERF_UP_ETH_ADT
                    else:
                        save_path = None
                    try:
                        # Read the incoming JSON data until the client closes the connection
                        received_data = b""
                        while True:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break  # Connection closed by client
                            received_data += chunk
                        # Decode and save
                        if received_data:
                            json_text = received_data.decode()
                            with open(save_path, "w") as f:
                                f.write(json_text)
                            print(f"JSON file saved to: {save_path} ({len(received_data)} bytes)")
                        else:
                            print("No data received.")
                    except Exception as e:
                        print(f"Error receiving JSON file: {e}")
                else:
                    save_path = None
                print(f"Current save path: {save_path}")

# # Function to serve images over HTTP
# def run_http_server():
#     os.chdir(SAVE_DIR)
#     httpd = HTTPServer(("0.0.0.0", IMAGE_PORT), SimpleHTTPRequestHandler)
#     print(f"Serving images on port {IMAGE_PORT}...")
#     httpd.serve_forever()

# # Override to suppress logging for specific status codes (200 and 404)
# class CustomHTTPRequestHandler(SimpleHTTPRequestHandler):
#     def log_message(self, format, *args):
#         # Extract the status code from the format
#         status_code = args[-2]  # The second-to-last argument is the status code
        
#         # Suppress logs for 404 status code (and 200 if needed)
#         if status_code == "200" or status_code == "404":
#             return  # Do not log the message
        
#         # Call the original log_message method for other status codes
#         super().log_message(format, *args)

# def run_http_server():
#     # Set the working directory to serve files from
#     os.chdir(SAVE_DIR)
#     # Start the HTTP server with the custom request handler
#     httpd = HTTPServer((HOST_IP, IMAGE_PORT), CustomHTTPRequestHandler)
#     print(f"Serving images on port {IMAGE_PORT}...")
#     httpd.serve_forever()

# Function to receive system metrics and store them in InfluxDB
def handle_system_metrics_server():
    client = InfluxDBClient(INFLUXDB_HOST, INFLUXDB_PORT, INFLUXDB_USER, INFLUXDB_PASSWORD, INFLUXDB_DB)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((HOST_IP, SYSINFO_PORT))
    server_socket.listen(1)
    print(f"Listening for incoming system metrics on {HOST_IP}:{SYSINFO_PORT}...")
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
                    # Network data
                    network_data = {}
                    network_info = system_info.get("network", {})

                    for iface_name, iface_stats in network_info.items():
                        for stat_name, value in iface_stats.items():
                            # Create a field like enp2s0_upload_speed, enp1s0f1_bytes_recv, etc.
                            field_key = f"{iface_name}_{stat_name}"
                            try:
                                network_data[field_key] = float(value)
                            except (ValueError, TypeError):
                                # Skip if value is not convertible to float
                                continue

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
                                # "download_speed": float(system_info.get("download_speed", 0.0)),
                                # "upload_speed": float(system_info.get("upload_speed", 0.0)),
                                "cpu_power": float(system_info.get("cpu_power", 0.0)),
                                "total_disk_usage": float(system_info.get("total_disk_usage", 0.0)),
                                "total_disk_size": float(system_info.get("total_disk_size", 0.0)),
                                "progress_update": float(system_info.get("progress_update", 0.0)),
                                **per_core_usage_data,
                                **per_core_freq_data,
                                **network_data
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
    global message, netTestDuration, flag_get_image
    data = request.get_json()
    message = data.get("message", "Default message from host")
    print(message)

    if message == "3":
        return delete_all_files()
    elif message.startswith("RUN:"):
        global tif_file_properties
        # clear the database for new data
        flag_get_image = True
        tif_file_properties = []
    elif message.startswith("NETRUN:"):
        netTestDuration = message.split(":", 1)[1]
        if "LwEthOnb" in message:
            # Start iperf3 in a separate thread
            start_iperf_thread(SAVE_PATH_IPERF_LW_ETH_OB)
            return jsonify({"status": "iperf3 test started"}), 200
        elif "UpEthOnb" in message:
            # Start iperf3 in a separate thread
            start_iperf_thread(SAVE_PATH_IPERF_UP_ETH_OB)
            return jsonify({"status": "iperf3 test started"}), 200
        elif "EthAdt" in message:
            return forward_message_to_target(message)
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

# Function to run iperf3 and capture the results
def run_iperf3(file_path):
    global netTestDuration

    def run_test(reverse=False):
        # Define the command with or without reverse mode
        command = ["iperf3", "-c", TARGET_IP, "-u", "-b", "100G", "-t", netTestDuration, "-i", "1", "-J"]
        if reverse:
            command.append("-R")  # Add reverse flag for upload test

        # Run the command
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate()

        if process.returncode == 0:
            # Convert JSON output to a Python dictionary
            iperf3_result = json.loads(stdout)
            end_data = iperf3_result.get("end", {})

            return end_data
        else:
            print(f"Error running iperf3 ({'upload' if reverse else 'download'}): {stderr}")
            return None

    # Run download test
    down_result = run_test(reverse=False)
    time.sleep(2)
    # Run upload test
    up_result = run_test(reverse=True)

    if down_result or up_result:
        # Load existing results if the file exists
        try:
            with open(file_path, "r") as file:
                existing_data = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            existing_data = {}

        # Add results with appropriate tags
        if down_result:
            existing_data["down"] = down_result
        if up_result:
            existing_data["up"] = up_result

        # Save the updated results back to the file
        with open(file_path, "w") as json_file:
            json.dump(existing_data, json_file, indent=4)

        print(f"Results saved to {file_path}")
    else:
        print("No valid results to save.")


# Function to start the iperf3 test in a background thread
def start_iperf_thread(file_path):
    iperf_thread = threading.Thread(target=run_iperf3, args=(file_path,), daemon=True)
    iperf_thread.start()

# Flask route to fetch and stream the iperf3_end_result.json file
@app.route('/iperf3/lw_eth_onb_results', methods=['GET'])
def iperf_lw_eth_onb_results():
    file_path = SAVE_PATH_IPERF_LW_ETH_OB
    try:
        return send_file(file_path, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404
    
@app.route('/iperf3/up_eth_onb_results', methods=['GET'])
def iperf_up_eth_onb_results():
    file_path = SAVE_PATH_IPERF_UP_ETH_OB
    try:
        return send_file(file_path, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404
    
@app.route('/iperf3/lw_eth_adt_results', methods=['GET'])
def iperf_lw_eth_adt_results():
    file_path = SAVE_PATH_IPERF_LW_ETH_ADT
    try:
        return send_file(file_path, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404
    
@app.route('/iperf3/up_eth_adt_results', methods=['GET'])
def iperf_up_eth_adt_results():
    file_path = SAVE_PATH_IPERF_UP_ETH_OB
    try:
        return send_file(file_path, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404

# Function to run Flask server
def run_flask_server():
    print(f"Running FLASK server on {HOST_IP}:{FLASK_PORT}...")
    app.run(host=HOST_IP, port=FLASK_PORT, debug=True, use_reloader=False)

# Start all services in separate threads
threading.Thread(target=handle_image_process_server, daemon=True).start()
threading.Thread(target=handle_net_test_server, daemon=True).start()
threading.Thread(target=handle_system_metrics_server, daemon=True).start()
threading.Thread(target=run_flask_server, daemon=True).start()

# Keep main thread alive
while True:
    time.sleep(1)
