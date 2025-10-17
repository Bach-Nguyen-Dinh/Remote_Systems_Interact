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
DATA_PORT = 55555
IMAGE_PORT = 8080
FLASK_PORT = 5001

TARGET_IP = "10.42.1.7"  # Target system IP
TARGET_PORT = 54321       # Target system port

INFLUXDB_HOST = "localhost"
INFLUXDB_PORT = 8086
INFLUXDB_DB = "system_metrics"
INFLUXDB_USER = "root"
INFLUXDB_PASSWORD = "root"

DOCKER_INTERFACE_ID = "docker0"
FM_INTERFACE_ID = "fm1-mac3"
LOCAL_INTERFACE_ID = "lo"
VIRTUAL_INTERFACE_ID = "virbr0"

COMP_ETH_PORT_INTERFACE_ID = "enx98fc84e12360"

CURR_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR = os.path.join(CURR_DIR, "pictures")
# Check if the "pictures" folder exists, create it if not
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)
    print(f"Folder 'pictures' created at: {SAVE_DIR}")
else:
    print(f"Folder 'pictures' already exists at: {SAVE_DIR}")
# SAVE_PATH = os.path.join(SAVE_DIR, "latest_image.png")
SAVE_PATH_TIF = os.path.join(SAVE_DIR, "tif_image.webp")
SAVE_PATH_OUT = os.path.join(SAVE_DIR, "out_image.png")

SAVE_PATH_IPERF_FM = os.path.join(CURR_DIR, "iperf3_end_result_fm.json")
SAVE_PATH_IPERF_LOCAL = os.path.join(CURR_DIR, "iperf3_end_result_local.json")
SAVE_PATH_IPERF_DOCKER = os.path.join(CURR_DIR, "iperf3_end_result_docker.json")
SAVE_PATH_IPERF_VIRTUAL = os.path.join(CURR_DIR, "iperf3_end_result_virtual.json")

SMALL_OBJ_INPUT_DIR = "/home/matthew/Remote_Systems_Interact/small_obj_detect/data1/image"
SMALL_OBJ_OUTPUT_DIR = "/home/matthew/Remote_Systems_Interact/small_obj_detect/data1/predictions"

# Gyro parameters
CALIBRATION_TIME = 10.0  # seconds to collect stationary gyro data
alpha = 0.2              # low-pass filter coefficient
MIN_DIFF = 0.05          # minimum change threshold (deg/s)

# Global variable
connected_clients = []
latest_small_obj_progress = {}

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

# Persistent variables to keep track of angle integration
_last_time = None
_angles = {"angle_x": 0.0, "angle_y": 0.0, "angle_z": 0.0}

if "_gyro_filtered" not in locals():
    _gyro_filtered = {"x": 0.0, "y": 0.0, "z": 0.0}
if "_gyro_filtered_prev" not in locals():
    _gyro_filtered_prev = {"x": 0.0, "y": 0.0, "z": 0.0}
if "_gyro_bias" not in locals():
    _gyro_bias = {"x": 0.0, "y": 0.0, "z": 0.0}
if "_gyro_prev" not in locals():
    _gyro_prev = {"x": 0.0, "y": 0.0, "z": 0.0}
if "_calibration_start" not in locals():
    _calibration_start = None
if "_calibration_samples" not in locals():
    _calibration_samples = []
if "_calibrated" not in locals():
    _calibrated = False

# Initialize Flask
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Define functions
def broadcast_to_clients(data):
    """Broadcast data to all connected clients"""
    # This could be implemented with WebSockets or Server-Sent Events
    # For simplicity, we'll store the latest update and serve it via HTTP
    global latest_small_obj_progress
    latest_small_obj_progress = data

def start_server():
    global message, cphd_file_list, cphd_file_properties, tif_file_properties
    imageSaved = False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((HOST_IP, DATA_PORT))
        server_socket.listen(1)
        print(f"Listening for incoming image on {HOST_IP}:{DATA_PORT}...")

        while True:
            conn, addr = server_socket.accept()
            with conn:
                print(f"Receiving data from {addr}")

                # Check if message is related to an image
                if message.startswith("RUN:") and imageSaved == False:
                    save_path = SAVE_PATH_TIF
                elif message.startswith("NETRUN:"):
                    if DOCKER_INTERFACE_ID in message:
                        save_path = SAVE_PATH_IPERF_DOCKER
                    elif VIRTUAL_INTERFACE_ID in message:
                        save_path = SAVE_PATH_IPERF_VIRTUAL
                    else:
                        save_path = None
                else:
                    save_path = None
                    imageSaved = False

                print(f"Current save path: {save_path}")

                if save_path == SAVE_PATH_TIF:
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
                elif save_path is not None and save_path != SAVE_PATH_TIF:
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

                        if "type" in received_data and received_data["type"].startswith("small_obj_detect"):
                            print(f"Small object detection update: {received_data}")
                            # Broadcast to all connected clients (like index grafana)
                            broadcast_to_clients(received_data)

                    except json.JSONDecodeError as e:
                        print(f"Error decoding received data: {e}")


# # Function to serve images over HTTP
# def run_http_server():
#     os.chdir(SAVE_DIR)
#     httpd = HTTPServer(("0.0.0.0", IMAGE_PORT), SimpleHTTPRequestHandler)
#     print(f"Serving images on port {IMAGE_PORT}...")
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
    httpd = HTTPServer((HOST_IP, IMAGE_PORT), CustomHTTPRequestHandler)
    print(f"Serving images on port {IMAGE_PORT}...")
    httpd.serve_forever()

# Function to receive system metrics and store them in InfluxDB
def receive_metrics():
    global _last_time, _angles
    global _gyro_filtered, _gyro_bias, _calibration_start, _calibration_samples, _calibrated
    
    client = InfluxDBClient(INFLUXDB_HOST, INFLUXDB_PORT, INFLUXDB_USER, INFLUXDB_PASSWORD, INFLUXDB_DB)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((HOST_IP, SYSINFO_PORT))
    server_socket.listen(1)

    print("System metrics server listening for connections...")
    
    while True:
        client_socket, client_address = server_socket.accept()
        print(f"Connection established with {client_address}")

        # ===== Reset calibration on new connection =====
        print("Resetting gyro calibration for new device connection...")
        _calibration_start = None
        _calibration_samples = []
        _calibrated = False
        _angles = {"angle_x": 0.0, "angle_y": 0.0, "angle_z": 0.0}
        _gyro_filtered = {"x": 0.0, "y": 0.0, "z": 0.0}
        _gyro_bias = {"x": 0.0, "y": 0.0, "z": 0.0}
        _gyro_prev = {"x": 0.0, "y": 0.0, "z": 0.0}
        _last_time = None

        buffer = ""
        while True:
            data = client_socket.recv(1024 * 10).decode()
            # print(data)
            if not data:
                break

            buffer += data
            while "\n" in buffer:
                message, buffer = buffer.split("\n", 1)
                try:
                    system_info = json.loads(message)
                    total_cpu_usage = 0
                    # CPU metrics
                    per_core_usage_data = {}
                    for i in range(4):
                        core_key = f"core_{i}_usage"
                        usage = float(system_info["per_core_usage"].get(core_key, 0))
                        per_core_usage_data[f"per_core_usage_RDB{i}"] = usage
                        total_cpu_usage += usage*0.25
                    per_core_freq_data = {
                        f"per_core_freq_RDB{i}": float(system_info["per_core_freq"].get(f"core_{i}_frequency", 0))
                        for i in range(4)
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
                    per_ai_core_temp = {
                        f"ai_core_{i}_temp": float(system_info["ai_temps"].get(f"ai_core_{i}_temp", 0))
                        for i in range(4)
                    }
                    per_ai_core_freq = {
                        f"ai_core_{i}_freq": float(system_info["ai_freqs"].get(f"ai_core_{i}_freq", 0))
                        for i in range(4)
                    }
                    total_ai_core_usage = 0
                    per_ai_core_usage = {}
                    for i in range(4):
                        ai_core_key = f"ai_core_{i}_usage"
                        ai_usage = float(system_info["ai_run_metrics"].get(ai_core_key, 0))
                        per_ai_core_usage[ai_core_key] = ai_usage
                        total_ai_core_usage += ai_usage*0.25

                    # print(total_ai_core_usage)
                    # Prepare data for InfluxDB
                    per_ai_core_pwr ={
                        f"ai_core_{i}_pwr": float(system_info["per_ai_core_pwrs"].get(f"ai_core_{i}_pwr", 0))
                        for i in range(4)
                    }
                    imu_data = {
                        "accel_x": float(system_info["imu_data"].get("accel_x", 0.0) or 0.0),
                        "accel_y": float(system_info["imu_data"].get("accel_y", 0.0) or 0.0),
                        "accel_z": float(system_info["imu_data"].get("accel_z", 0.0) or 0.0),
                        "gyro_x": float(system_info["imu_data"].get("gyro_x", 0.0) or 0.0),
                        "gyro_y": float(system_info["imu_data"].get("gyro_y", 0.0) or 0.0),
                        "gyro_z": float(system_info["imu_data"].get("gyro_z", 0.0) or 0.0),
                        "angle_x": _angles["angle_x"],
                        "angle_y": _angles["angle_y"],
                        "angle_z": _angles["angle_z"],
                        "gyro_x_filtered": _gyro_filtered["x"],
                        "gyro_y_filtered": _gyro_filtered["y"],
                        "gyro_z_filtered": _gyro_filtered["z"]
                    }
                    now = time.time()
                    if _last_time is not None:
                        dt = now - _last_time

                        # --- Step 1: Calibration phase ---
                        if not _calibrated:
                            if _calibration_start is None:
                                _calibration_start = now

                            _calibration_samples.append(
                                (imu_data["gyro_x"], imu_data["gyro_y"], imu_data["gyro_z"])
                            )

                            # If calibration time not over → keep angles at zero
                            elapsed = now - _calibration_start
                            if elapsed < CALIBRATION_TIME:
                                imu_data["angle_x"] = 0.0
                                imu_data["angle_y"] = 0.0
                                imu_data["angle_z"] = 0.0
                            else:
                                # Compute average gyro bias
                                n = len(_calibration_samples)
                                avg_x = sum(s[0] for s in _calibration_samples) / n
                                avg_y = sum(s[1] for s in _calibration_samples) / n
                                avg_z = sum(s[2] for s in _calibration_samples) / n
                                _gyro_bias = {"x": avg_x, "y": avg_y, "z": avg_z}
                                
                                # Initialize filtered values to zero after calibration
                                _gyro_filtered = {"x": 0.0, "y": 0.0, "z": 0.0}
                                _gyro_filtered_prev = {"x": 0.0, "y": 0.0, "z": 0.0}

                                _calibrated = True

                        # --- Step 2: Apply gyro offset and filter (only after calibration) ---
                        if _calibrated:
                            gyro_x_corr = imu_data["gyro_x"] - _gyro_bias["x"]
                            gyro_y_corr = imu_data["gyro_y"] - _gyro_bias["y"]
                            gyro_z_corr = imu_data["gyro_z"] - _gyro_bias["z"]

                            # --- Threshold: Small value AND small change = likely noise ---
                            # If the value is small AND it's not changing much, zero it out
                            # This handles stationary drift while preserving slow constant rotation
                            if abs(gyro_x_corr) < MIN_DIFF and abs(gyro_x_corr - _gyro_prev["x"]) < MIN_DIFF * 0.5:
                                gyro_x_corr = 0.0
                            if abs(gyro_y_corr) < MIN_DIFF and abs(gyro_y_corr - _gyro_prev["y"]) < MIN_DIFF * 0.5:
                                gyro_y_corr = 0.0
                            if abs(gyro_z_corr) < MIN_DIFF and abs(gyro_z_corr - _gyro_prev["z"]) < MIN_DIFF * 0.5:
                                gyro_z_corr = 0.0

                            # Update previous gyro readings
                            _gyro_prev["x"] = gyro_x_corr
                            _gyro_prev["y"] = gyro_y_corr
                            _gyro_prev["z"] = gyro_z_corr

                            # --- Low-pass filter for gyro ---
                            # α (alpha) controls the cutoff frequency — smaller = smoother, larger = more responsive
                            _gyro_filtered_prev = _gyro_filtered.copy()

                            _gyro_filtered["x"] = alpha * gyro_x_corr + (1 - alpha) * _gyro_filtered["x"]
                            _gyro_filtered["y"] = alpha * gyro_y_corr + (1 - alpha) * _gyro_filtered["y"]
                            _gyro_filtered["z"] = alpha * gyro_z_corr + (1 - alpha) * _gyro_filtered["z"]

                            # --- Integrate the *average* of filtered values between samples ---
                            # use the trapezoidal rule
                            imu_data["angle_x"] += 0.5 * (_gyro_filtered["x"] + _gyro_filtered_prev["x"]) * dt
                            imu_data["angle_y"] += 0.5 * (_gyro_filtered["y"] + _gyro_filtered_prev["y"]) * dt
                            imu_data["angle_z"] += 0.5 * (_gyro_filtered["z"] + _gyro_filtered_prev["z"]) * dt

                            # Wrap angles to [-180, 180)
                            imu_data["angle_x"] = (imu_data["angle_x"] + 180.0) % 360.0 - 180.0
                            imu_data["angle_y"] = (imu_data["angle_y"] + 180.0) % 360.0 - 180.0
                            imu_data["angle_z"] = (imu_data["angle_z"] + 180.0) % 360.0 - 180.0

                            # Update stored angles
                            _angles["angle_x"] = imu_data["angle_x"]
                            _angles["angle_y"] = imu_data["angle_y"]
                            _angles["angle_z"] = imu_data["angle_z"]

                    _last_time = now

                    json_body = [
                        {
                            "measurement": "system_metrics",
                            "tags": {"host": client_address[0]},
                            "fields": {
                                "cpu_usage_RDB": total_cpu_usage,
                                "memory_usage_RDB": float(system_info["memory_usage"]),
                                "swap_usage_RDB": float(system_info["swap_usage"]),
                                "sys_temp_RDB": system_info.get("sys_temp", 0.0),
                                "uptime_seconds_RDB": float(system_info["uptime_seconds"]),
                                "total_memory_RDB": float(system_info["total_memory"]),
                                "total_swap_RDB": float(system_info["total_swap"]),
                                "num_threads_RDB": int(system_info["num_threads"]),
                                "cpu_power_RDB": float(system_info.get("cpu_power", 0.0)),
                                "total_disk_usage_RDB": float(system_info.get("total_disk_usage", 0.0)),
                                "total_disk_size_RDB": float(system_info.get("total_disk_size", 0.0)),
                                "progress_update_RDB": float(system_info.get("progress_update", 0.0)),
                                **per_core_usage_data,
                                **per_core_freq_data,
                                **network_data,
                                **per_ai_core_temp,
                                **per_ai_core_freq,
                                "ai_total_pwr": float(system_info["ai_total_pwr"]),
                                **per_ai_core_usage,
                                "total_ai_usage": total_ai_core_usage,
                                **per_ai_core_pwr,
                                **imu_data
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
    global message, netTestDuration
    global latest_small_obj_progress

    data = request.get_json()
    message = data.get("message", "Default message from host")
    print(message)

    if message == "3":
        return delete_all_files()
    elif message.startswith("RUN:"):
        global tif_file_properties
        # clear the database for new data
        tif_file_properties = []
    elif message.startswith("NETRUN:"):
        netTestDuration = message.split(":", 1)[1]
        if FM_INTERFACE_ID in message:
            # Start iperf3 in a separate thread
            start_iperf_thread(SAVE_PATH_IPERF_FM)
            return jsonify({"status": "iperf3 test started"}), 200
        elif LOCAL_INTERFACE_ID in message:
            # Start iperf3 in a separate thread
            start_iperf_thread(SAVE_PATH_IPERF_LOCAL)
            return jsonify({"status": "iperf3 test started"}), 200
        elif DOCKER_INTERFACE_ID in message:
            return forward_message_to_target(message)
        elif VIRTUAL_INTERFACE_ID in message:
            return forward_message_to_target(message)
    elif message.startswith("BW:"):
        if FM_INTERFACE_ID in message:
            parts = message.split(":")
            if len(parts) != 3:
                print("Invalid BW message format")
                return
            _, bwValue, target = parts  # bwValue = "1000", target = "LwEthOnb"
            target = COMP_ETH_PORT_INTERFACE_ID
            # Construct the ethtool command
            command = ["sudo", "ethtool", "-s", target, "speed", bwValue, "autoneg", "off"]
            print(f"Executing command: {' '.join(command)}")
            # Run the command and capture stdout and stderr
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            # Capture output and error streams
            stdout, stderr = process.communicate()

            # Check the return code
            if process.returncode == 0:
                print("Command executed successfully.")
                if stdout:
                    print("Output:", stdout)
                else:
                    print("No output from the command.")
                return jsonify({"status": "success", "message": "Bandwidth updated successfully"}), 200
            else:
                print(f"Error executing command. Return code: {process.returncode}")
                if stderr:
                    print("Error message:", stderr)
                return jsonify({"status": "error", "message": stderr.strip()}), 500
            
    if message == "run_small_obj_detect":
        # Clear previous progress
        latest_small_obj_progress = {}
        return forward_message_to_target(message)
    elif message == "clear_small_obj_detect":
        latest_small_obj_progress = {}
        return jsonify({"status": "cleared"})

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
        command = ["iperf3", "-c", TARGET_IP, "-u", "-b", "100G", "-t", netTestDuration, "-P", "4", "-i", "1", "-J"]
        if reverse:
            command.append("-R")  # Add reverse flag for upload test

        print(f"Executing command: {' '.join(command)}")
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
    file_path = SAVE_PATH_IPERF_FM
    try:
        return send_file(file_path, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404
    
@app.route('/iperf3/up_eth_onb_results', methods=['GET'])
def iperf_up_eth_onb_results():
    file_path = SAVE_PATH_IPERF_LOCAL
    try:
        return send_file(file_path, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404
    
@app.route('/iperf3/lw_eth_adt_results', methods=['GET'])
def iperf_lw_eth_adt_results():
    file_path = SAVE_PATH_IPERF_DOCKER
    try:
        return send_file(file_path, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404
    
@app.route('/iperf3/up_eth_adt_results', methods=['GET'])
def iperf_up_eth_adt_results():
    file_path = SAVE_PATH_IPERF_LOCAL
    try:
        return send_file(file_path, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404
    
@app.route('/small_obj_detect/images/input/<filename>')
def serve_input_image(filename):
    """Serve input images for small object detection"""
    return send_from_directory(SMALL_OBJ_INPUT_DIR, filename)

@app.route('/small_obj_detect/images/output/<filename>')
def serve_output_image(filename):
    """Serve output images for small object detection"""
    return send_from_directory(SMALL_OBJ_OUTPUT_DIR, filename)

@app.route('/small_obj_detect/progress', methods=['GET'])
def get_small_obj_progress():
    """Get the latest small object detection progress"""
    return jsonify(latest_small_obj_progress)

@app.route('/small_obj_detect/image_list', methods=['GET'])
def get_image_list():
    """Get list of all input and output images with validation"""
    input_images = []
    output_images = []
    
    # Helper function to validate and get image info
    def get_valid_images(directory, image_type):
        images = []
        if os.path.exists(directory):
            for filename in os.listdir(directory):
                if filename.endswith('.webp'):
                    filepath = os.path.join(directory, filename)
                    try:
                        # Verify file exists and is readable
                        if os.path.isfile(filepath) and os.access(filepath, os.R_OK):
                            file_size = os.path.getsize(filepath)
                            # Only include files that have content
                            if file_size > 0:
                                images.append({
                                    'filename': filename,
                                    'size': file_size,
                                    'type': image_type
                                })
                            else:
                                print(f"Warning: Empty file {filepath}")
                        else:
                            print(f"Warning: Cannot read file {filepath}")
                    except Exception as e:
                        print(f"Error checking file {filepath}: {e}")
            
            # Sort by numerical order
            images.sort(key=lambda x: int(x['filename'].replace('.webp', '')))
        
        return images
    
    input_images = get_valid_images(SMALL_OBJ_INPUT_DIR, 'input')
    output_images = get_valid_images(SMALL_OBJ_OUTPUT_DIR, 'output')
    
    return jsonify({
        "input_images": [img['filename'] for img in input_images],
        "output_images": [img['filename'] for img in output_images],
        "input_images_info": input_images,
        "output_images_info": output_images,
        "total_input": len(input_images),
        "total_output": len(output_images)
    })

@app.route('/small_obj_detect/validate_image/<image_type>/<filename>')
def validate_image(image_type, filename):
    """Validate that a specific image exists and is accessible"""
    try:
        if image_type == 'input':
            directory = SMALL_OBJ_INPUT_DIR
        elif image_type == 'output':
            directory = SMALL_OBJ_OUTPUT_DIR
        else:
            return jsonify({"valid": False, "error": "Invalid image type"}), 400
        
        filepath = os.path.join(directory, filename)
        
        if os.path.isfile(filepath) and os.access(filepath, os.R_OK):
            file_size = os.path.getsize(filepath)
            return jsonify({
                "valid": True,
                "filename": filename,
                "size": file_size,
                "path": filepath
            })
        else:
            return jsonify({"valid": False, "error": "File not accessible"}), 404
            
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 500

# Function to run Flask server
def run_flask_server():
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=True, use_reloader=False)

# Start all services in separate threads
threading.Thread(target=start_server, daemon=True).start()
threading.Thread(target=run_http_server, daemon=True).start()
threading.Thread(target=receive_metrics, daemon=True).start()
threading.Thread(target=run_flask_server, daemon=True).start()

# Keep main thread alive
while True:
    time.sleep(1)
