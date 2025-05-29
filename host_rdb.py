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
SYSINFO_PORT = 12346  # Port for system metrics
NETTEST_PORT = 29103
IMAGE_PORT = 55556
FLASK_PORT = 5001

RDB_IP = "169.254.207.123"
RDB_PORT = 54322
HPC_IP = "10.42.0.90"
HPC_PORT = 54321

INFLUXDB_HOST = "localhost"
INFLUXDB_PORT = 8086
INFLUXDB_DB = "system_metrics"
INFLUXDB_USER = "root"
INFLUXDB_PASSWORD = "root"

DOCKER_INTERFACE_ID = "docker0"
FM_INTERFACE_ID = "fm1-mac9"
LOCAL_INTERFACE_ID = "lo"
VIRTUAL_INTERFACE_ID = "virbr0"

COMP_ETH_PORT_INTERFACE_ID = "enp3s0"

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
SAVE_PATH_IPERF_FM = os.path.join(CURR_DIR, "iperf3_end_result_fm.json")
SAVE_PATH_IPERF_LOCAL = os.path.join(CURR_DIR, "iperf3_end_result_local.json")
SAVE_PATH_IPERF_DOCKER = os.path.join(CURR_DIR, "iperf3_end_result_docker.json")
SAVE_PATH_IPERF_VIRTUAL = os.path.join(CURR_DIR, "iperf3_end_result_virtual.json")

# Global variable
message = ""
cphd_file_list = []  # Global list to store CPHD file names
cphd_file_properties = []  # Global list to store properties of a CPHD file
tif_file_properties = []
final_results = {
    "sender_transfer_RDB": "",
    "sender_bitrate_RDB": "",
    "sender_jitter_RDB": "",
    "sender_loss_RDB": "",
    "receiver_transfer_RDB": "",
    "receiver_bitrate_RDB": "",
    "receiver_jitter_RDB": "",
    "receiver_loss_RDB": ""
}
netTestDuration = 0
flag_get_image = False

# Initialize Flask
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

def handle_image_process_server():
    global cphd_file_list, cphd_file_properties, tif_file_properties, flag_get_image
    imageSaved = False
    save_path = None
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
                    print(f"Current save path: {save_path}")
                    # Receive file size first
                    file_size = int.from_bytes(conn.recv(8), byteorder="big")
                    print(f"Expecting to receive {file_size} bytes...")
                    # Receive the actual data
                    received_data = b""
                    while len(received_data) < file_size:
                        # expect an image so can take the binary directly
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
                    print(f"Current save path: {save_path}")
                    # expect text so binary data have to be decoded
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
    save_path = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((HOST_IP, NETTEST_PORT))
        server_socket.listen(1)
        print(f"Listening for incoming net tesk results on {HOST_IP}:{NETTEST_PORT}...")
        while True:
            conn, addr = server_socket.accept()
            with conn:
                print(f"Receiving data from {addr}")
                try:
                    json_data = b""
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        json_data += chunk
                    if json_data:
                        try:
                            received_dict = json.loads(json_data.decode())
                            if "NETDONE" in received_dict:
                                net_iface = received_dict["NETDONE"]
                                if net_iface == FM_INTERFACE_ID:
                                    save_path = SAVE_PATH_IPERF_FM
                                elif net_iface == LOCAL_INTERFACE_ID:
                                    save_path = SAVE_PATH_IPERF_LOCAL
                                elif net_iface == DOCKER_INTERFACE_ID:
                                    save_path = SAVE_PATH_IPERF_DOCKER
                                elif net_iface == VIRTUAL_INTERFACE_ID:
                                    save_path = SAVE_PATH_IPERF_VIRTUAL
                                else:
                                    save_path = None
                                    print(f"Unknown interface: {net_iface}")
                                print(f"Received NETDONE for {net_iface}")
                            elif "data" in received_dict:
                                # This is the actual iperf result data
                                if save_path:
                                    with open(save_path, "w") as f:
                                        json.dump(received_dict["data"], f, indent=4)
                                    print(f"Saved net test JSON to: {save_path}")
                                else:
                                    print("Warning: data received but no save_path set (NETDONE must arrive first)")
                            else:
                                print("Unknown message format:", received_dict)
                        except json.JSONDecodeError as e:
                            print(f"Error decoding received JSON: {e}")
                    else:
                        print("No data received.")
                except Exception as e:
                    print(f"Error receiving net test JSON data: {e}")
                print(f"Current save path: {save_path}")

# Function to receive system metrics and store them in InfluxDB
def handle_system_metrics_server():
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

                    # Prepare data for InfluxDB
                    json_body = [
                        {
                            "measurement": "system_metrics",
                            "tags": {
                                "host": "RDB",
                                "source": client_address[0]
                            },
                            "fields": {
                                "cpu_usage_RDB": total_cpu_usage,
                                "memory_usage_RDB": float(system_info["memory_usage"]),
                                "swap_usage_RDB": float(system_info["swap_usage"]),
                                "sys_temp_RDB": system_info.get("sys_temp", 0.0),
                                "uptime_seconds_RDB": float(system_info["uptime_seconds"]),
                                "total_memory_RDB": float(system_info["total_memory"]),
                                "total_swap_RDB": float(system_info["total_swap"]),
                                "num_threads_RDB": int(system_info["num_threads"]),
                                # "cpu_power": float(system_info.get("cpu_power", 0.0)),
                                "total_disk_usage_RDB": float(system_info.get("total_disk_usage", 0.0)),
                                "total_disk_size_RDB": float(system_info.get("total_disk_size", 0.0)),
                                "progress_update_RDB": float(system_info.get("progress_update", 0.0)),
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
        flag_get_image = False
        delete_all_files()
    elif message.startswith("RUN:"):
        global tif_file_properties
        flag_get_image = True
        # clear the database for new data
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
    
from flask import jsonify
import socket

def forward_message_to_target(message):
    # define our send‐targets
    targets = [
        ("RDB", RDB_IP, RDB_PORT)
    ]
    if message.startswith("BW:"):
        targets.append(("HPC", HPC_IP, HPC_PORT))
    elif message.startswith("NETRUN:"):
        targets = [
            ("HPC", HPC_IP, HPC_PORT)
        ]

    results = {}
    # send to each target and capture success/error
    for name, ip, port in targets:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
                client_socket.connect((ip, port))
                client_socket.sendall(message.encode())
            results[name] = "success"
        except Exception as e:
            results[name] = f"error: {e}"

    # if all succeeded, 200; otherwise 500
    if all(status == "success" for status in results.values()):
        return jsonify({
            "status": "success",
            "message": message,
            "results": results
        })
    else:
        return jsonify({
            "status": "error",
            "message": message,
            "results": results
        }), 500

    
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

# Function to run Flask server
def run_flask_server():
    print(f"Running FLASK server on {HOST_IP}:{FLASK_PORT}...")
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=True, use_reloader=False)

# Start all services in separate threads
threading.Thread(target=handle_image_process_server, daemon=True).start()
threading.Thread(target=handle_net_test_server, daemon=True).start()
threading.Thread(target=handle_system_metrics_server, daemon=True).start()
threading.Thread(target=run_flask_server, daemon=True).start()

# Keep main thread alive
while True:
    time.sleep(1)
