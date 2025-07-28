from influxdb import InfluxDBClient
from PIL import Image
import json
import time
import socket
import os
import threading
import subprocess
import psutil
from flask import Flask, request, jsonify, send_from_directory, send_file  # type: ignore
from flask_cors import CORS  # type: ignore

# Configuration - HPC system
HOST_IP = '0.0.0.0'
SYSINFO_PORT = 12345  # Port for system metrics (kept for compatibility)
NETTEST_PORT = 29102
IMAGE_PORT = 55555
FLASK_PORT = 5000

# PC connection (for network tests)
PC_IP = "10.42.0.1"

# RDB connection
RDB_IP = "169.254.207.123"
RDB_PORT = 54322

# InfluxDB connection (on PC)
INFLUXDB_HOST = "localhost"  # PC IP
INFLUXDB_PORT = 8086
INFLUXDB_DB = "system_metrics"
INFLUXDB_USER = "root"
INFLUXDB_PASSWORD = "root"

# Network interfaces
LW_ETH_OB_INTERFACE_ID = "enp2s0"
UP_ETH_OB_INTERFACE_ID = "enp3s0"
LW_ETH_ADT_INTERFACE_ID = "enp1s0f1"
UP_ETH_ADT_INTERFACE_ID = "enp1s0f0"
WIRELESS_INTERFACE_ID = "wlan0"

# File paths
CURR_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_DIR = os.path.join(CURR_DIR, "pictures")
DEMO_PATH = "/home/bach-ngd/demo-resrc/"
RESIZED_IMAGE_PATH = "/home/bach-ngd/demo-resrc/optimized_image.webp"

# Check if the "pictures" folder exists, create it if not
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)
    print(f"Folder 'pictures' created at: {SAVE_DIR}")
else:
    print(f"Folder 'pictures' already exists at: {SAVE_DIR}")

SAVE_PATH_TIF = os.path.join(SAVE_DIR, "tif_image.webp")
SAVE_PATH_IPERF_LW_ETH_OB = os.path.join(CURR_DIR, "iperf3_end_result_LwEthOnb.json")
SAVE_PATH_IPERF_UP_ETH_OB = os.path.join(CURR_DIR, "iperf3_end_result_UpEthOnb.json")
SAVE_PATH_IPERF_LW_ETH_ADT = os.path.join(CURR_DIR, "iperf3_end_result_LwEthAdt.json")
SAVE_PATH_IPERF_UP_ETH_ADT = os.path.join(CURR_DIR, "iperf3_end_result_UpEthAdt.json")

# Global variables
cphd_files = {}
cphd_file_list = []
cphd_file_properties = []
tif_file_properties = []
progress_update = 0.0
bwValue = 0
flag_get_image = False

# Initialize Flask
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# ===== IMAGE PROCESSING FUNCTIONS =====
def optimize_tif(image_path, output_path, format="webp", max_size=(800, 800), quality=85):
    """
    Optimize and convert a TIFF image for efficient transmission and web display.
    """
    try:
        img = Image.open(image_path)
        if img.mode in ("P", "CMYK", "RGBA"):
            img = img.convert("RGB")
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        ext = format.lower()
        if ext not in ["jpeg", "jpg", "png", "webp", "avif"]:
            raise ValueError("Unsupported format. Use jpeg, png, webp, or avif.")
        img.save(output_path, format=ext.upper(), quality=quality, optimize=True)

        optimized_size = os.path.getsize(output_path)
        print(f"Optimized image saved at {output_path}, size: {optimized_size} bytes")

    except Exception as e:
        print(f"Error optimizing image: {e}")

def send_image_to_pc(image_path):
    """Send optimized image to PC"""
    optimize_tif(image_path, RESIZED_IMAGE_PATH, format="webp", max_size=(800, 800), quality=80)
    
    if not os.path.exists(RESIZED_IMAGE_PATH):
        print(f"Error: Image file {RESIZED_IMAGE_PATH} not found!")
        return

    file_size = os.path.getsize(RESIZED_IMAGE_PATH)
    print(f"Sending image {RESIZED_IMAGE_PATH} to PC, size: {file_size} bytes...")
    
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((PC_IP, IMAGE_PORT))
            sock.sendall(file_size.to_bytes(8, byteorder="big"))
            with open(RESIZED_IMAGE_PATH, "rb") as f:
                while chunk := f.read(4096):
                    sock.sendall(chunk)
            print("Image sent successfully to PC!")
    except Exception as e:
        print(f"Error sending image to PC: {e}")

def handle_image_sending(image_path):
    """Handle image processing with progress updates"""
    timestamps = {
        0: 0.0, 1: 6.67, 2: 13.33, 3: 20.0, 4: 26.67, 5: 33.33,
        6: 40.0, 10: 66.67, 11: 73.33, 12: 80.0, 15: 100.0
    }
    for second in range(16):
        time.sleep(1)
        if second in timestamps:
            global progress_update
            progress_update = timestamps[second]
    send_image_to_pc(image_path)

# ===== CPHD FILE MANAGEMENT =====
def scan_cphd_files():
    """Scan for CPHD files and update global variables"""
    global cphd_files, cphd_file_list
    cphd_files = {}
    
    if os.path.exists(DEMO_PATH):
        print("Directory exists!")
        for root, dirs, files in os.walk(DEMO_PATH):
            for file in files:
                if file.endswith(".cphd"):
                    full_path = os.path.join(root, file)
                    cphd_files[file] = full_path
        
        cphd_file_list = list(cphd_files.keys())
        print("Found .cphd files:", cphd_file_list)
    else:
        print("Directory does NOT exist!")

def get_metadata_from_json(directory):
    """Extract metadata from JSON files"""
    try:
        for file in os.listdir(directory):
            if file.endswith(".json"):
                json_path = os.path.join(directory, file)
                with open(json_path, "r") as f:
                    data = json.load(f)
                    derived_products = data.get("derivedProducts", {}).get("GEC", [{}])[0]
                    return {
                        "numRows": derived_products.get("numRows"),
                        "numColumns": derived_products.get("numColumns"),
                        "groundResolution": derived_products.get("groundResolution", {}).get("azimuthMeters")
                    }
    except Exception as e:
        print(f"Error reading metadata: {e}")
    return None

def process_cphd_file(file_path):
    """Process CPHD file and send results"""
    global tif_file_properties
    directory = os.path.dirname(file_path)
    tif_files = [f for f in os.listdir(directory) if f.endswith(".tif")]
    
    if tif_files:
        tif_path = os.path.join(directory, tif_files[0])
        
        # Send the processed image
        handle_image_sending(tif_path)
        
        # Calculate and store properties
        tif_size = os.path.getsize(tif_path)
        cphd_size = os.path.getsize(file_path)
        reduction_scale = round(cphd_size / tif_size, 2)
        size_compared = round((tif_size / cphd_size) * 100, 2) if cphd_size else 0
        reduction_factor = round(100 - size_compared, 2)
        tif_size_str = f"{tif_size / 1_000_000:.2f} MB" if tif_size >= 1_000_000 else f"{tif_size} bytes"

        tif_file_properties = {
            "tif_filename": tif_files[0],
            "size": tif_size_str,
            "reduction_factor": reduction_factor,
            "size_compared": size_compared,
            "reduction_scale": reduction_scale
        }
        
        print(f"TIF file properties: {tif_file_properties}")

# ===== NETWORK TESTING FUNCTIONS =====
def run_iperf3_onboard_test(file_path, netTestDuration):
    """Run iperf3 test with PC (onboard interfaces)"""
    def run_test(reverse=False):
        command = ["iperf3", "-c", PC_IP, "-u", "-b", "100G", "-t", netTestDuration, "-i", "1", "-J"]
        if reverse:
            command.append("-R")
        
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate()
        
        if process.returncode == 0:
            return json.loads(stdout).get("end", {})
        else:
            print(f"Error running iperf3 ({'upload' if reverse else 'download'}): {stderr}")
            return None

    # Run download test
    down_result = run_test(reverse=False)
    time.sleep(2)
    # Run upload test
    up_result = run_test(reverse=True)

    if down_result or up_result:
        try:
            with open(file_path, "r") as file:
                existing_data = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            existing_data = {}

        if down_result:
            existing_data["down"] = down_result
        if up_result:
            existing_data["up"] = up_result

        with open(file_path, "w") as json_file:
            json.dump(existing_data, json_file, indent=4)
        
        print(f"Onboard test results saved to {file_path}")

def handle_netrun_test_adt(netTestDuration, netTestInterface):
    """Handle network test with RDB (ADT interfaces)"""
    global bwValue
    
    if netTestInterface == "LwEthAdt" or netTestInterface == "fm1-mac9":
        file_path = SAVE_PATH_IPERF_LW_ETH_ADT
        client_ip = RDB_IP
    elif netTestInterface == "UpEthAdt":
        file_path = SAVE_PATH_IPERF_UP_ETH_ADT
        client_ip = RDB_IP
    else:
        print(f"Unknown ADT interface: {netTestInterface}")
        return

    def run_test(reverse=False):
        if bwValue == 10000:
            command = ["iperf3", "-c", client_ip, "-b", "20G", "-t", netTestDuration, "-P", "4", "-i", "1", "-J"]
        else:
            command = ["iperf3", "-c", client_ip, "-u", "-b", "20G", "-t", netTestDuration, "-P", "4", "-i", "1", "-J"]
        if reverse:
            command.append("-R")
        
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate()
        
        if process.returncode == 0:
            return json.loads(stdout).get("end", {})
        else:
            print(f"Error running iperf3 ({'upload' if reverse else 'download'}): {stderr}")
            return None

    down_result = run_test(reverse=True)
    time.sleep(2)
    up_result = run_test(reverse=False)

    if down_result or up_result:
        try:
            with open(file_path, "r") as file:
                existing_data = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            existing_data = {}

        if down_result:
            existing_data["down"] = down_result
        if up_result:
            existing_data["up"] = up_result

        with open(file_path, "w") as json_file:
            json.dump(existing_data, json_file, indent=4)

        print(f"ADT test results saved to {file_path}")

def handle_bandwidth_setting(bw_value, target):
    """Handle bandwidth setting for network interfaces"""
    global bwValue
    bwValue = int(bw_value)
    
    # Determine the correct interface ID
    if target == "LwEthOnb":
        interface_id = LW_ETH_OB_INTERFACE_ID
    elif target == "UpEthOnb":
        interface_id = UP_ETH_OB_INTERFACE_ID
    elif target == "LwEthAdt" or target == "fm1-mac9":
        interface_id = LW_ETH_ADT_INTERFACE_ID
    elif target == "UpEthAdt":
        interface_id = UP_ETH_ADT_INTERFACE_ID
    else:
        print(f"Unknown interface identifier: {target}")
        return

    # Construct the ethtool command
    command = ["ethtool", "-s", interface_id, "speed", bw_value, "autoneg", "on"]
    print(f"Executing command: {' '.join(command)}")

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate()

    if process.returncode == 0:
        print("Bandwidth setting command executed successfully.")
        if stdout:
            print("Output:", stdout)
    else:
        print(f"Error executing bandwidth command. Return code: {process.returncode}")
        if stderr:
            print("Error message:", stderr)

# ===== SYSTEM MONITORING FUNCTIONS =====
def read_rapl_energy():
    """Read RAPL energy data"""
    try:
        with open("/sys/class/powercap/intel-rapl:0/energy_uj", "r") as f:
            return int(f.read().strip())
    except FileNotFoundError:
        return None

def get_cpu_power():
    """Calculate CPU power consumption"""
    energy_start = read_rapl_energy()
    if energy_start is None:
        return None
    
    time.sleep(0.1)
    energy_end = read_rapl_energy()
    if energy_end is None:
        return None
    
    power_watts = (energy_end - energy_start) / 1_000_000 / 0.1
    return power_watts

def get_system_info():
    """Collect comprehensive system information"""
    cpu_usage = psutil.cpu_percent(interval=0.1)
    per_core_usage = psutil.cpu_percent(interval=0.1, percpu=True)
    core_usage = {f"core_{i}_usage": usage for i, usage in enumerate(per_core_usage)}
    
    core_frequencies = {}
    if hasattr(psutil, "cpu_freq"):
        freq_info = psutil.cpu_freq(percpu=True)
        if freq_info:
            core_frequencies = {f"core_{i}_frequency": freq.current for i, freq in enumerate(freq_info)}
    
    cpu_temperature = None
    if hasattr(psutil, "sensors_temperatures"):
        temp_info = psutil.sensors_temperatures()
        if 'coretemp' in temp_info:
            cpu_temperature = temp_info['coretemp'][0].current
    
    memory_usage = psutil.virtual_memory().percent
    total_memory = psutil.virtual_memory().total
    swap_usage = psutil.swap_memory().percent
    total_swap = psutil.swap_memory().total
    
    # Disk usage
    root_disk_usage = psutil.disk_usage('/').percent
    root_total_disk = psutil.disk_usage('/').total
    total_disk_usage = root_disk_usage
    total_disk_size = root_total_disk
    
    num_threads = psutil.cpu_count(logical=True)
    num_cores = psutil.cpu_count(logical=False)
    uptime_seconds = time.time() - psutil.boot_time()
    
    # Network statistics
    interfaces = [LW_ETH_OB_INTERFACE_ID, LW_ETH_ADT_INTERFACE_ID, UP_ETH_OB_INTERFACE_ID, UP_ETH_ADT_INTERFACE_ID, WIRELESS_INTERFACE_ID]
    net_before = psutil.net_io_counters(pernic=True)
    time.sleep(0.1)
    net_after = psutil.net_io_counters(pernic=True)

    network_stats = {}
    for iface in interfaces:
        if iface in net_before and iface in net_after:
            net_b = net_before[iface]
            net_a = net_after[iface]
            network_stats[iface] = {
                "bytes_sent": net_a.bytes_sent,
                "bytes_recv": net_a.bytes_recv,
                "upload_speed": (net_a.bytes_sent - net_b.bytes_sent) / 0.1,
                "download_speed": (net_a.bytes_recv - net_b.bytes_recv) / 0.1,
                "packets_sent": net_a.packets_sent,
                "packets_recv": net_a.packets_recv,
            }
    
    cpu_power = get_cpu_power()
    
    system_info = {
        "cpu_usage": cpu_usage,
        "memory_usage": memory_usage,
        "total_memory": total_memory,
        "swap_usage": swap_usage,
        "total_swap": total_swap,
        "num_threads": num_threads,
        "num_cores": num_cores,
        "uptime_seconds": uptime_seconds,
        "per_core_usage": core_usage,
        "per_core_freq": core_frequencies,
        "cpu_temperature": cpu_temperature,
        "network": network_stats,
        "cpu_power": cpu_power,
        "total_disk_usage": total_disk_usage,
        "total_disk_size": total_disk_size,
        "progress_update": progress_update
    }
    
    return system_info

def stream_system_metrics_to_influxdb():
    """Stream system metrics directly to InfluxDB on PC"""
    client = InfluxDBClient(INFLUXDB_HOST, INFLUXDB_PORT, INFLUXDB_USER, INFLUXDB_PASSWORD, INFLUXDB_DB)
    
    print("Starting system metrics streaming to InfluxDB...")
    
    while True:
        try:
            system_info = get_system_info()
            
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
                    field_key = f"{iface_name}_{stat_name}"
                    try:
                        network_data[field_key] = float(value)
                    except (ValueError, TypeError):
                        continue

            # Prepare data for InfluxDB
            json_body = [
                {
                    "measurement": "system_metrics",
                    "tags": {
                        "host": "HPC",
                        "source": "localhost"
                    },
                    "fields": {
                        "cpu_usage": float(system_info["cpu_usage"]),
                        "memory_usage": float(system_info["memory_usage"]),
                        "swap_usage": float(system_info["swap_usage"]),
                        "cpu_temperature": float(system_info.get("cpu_temperature", 0.0)),
                        "uptime_seconds": float(system_info["uptime_seconds"]),
                        "total_memory": float(system_info["total_memory"]),
                        "total_swap": float(system_info["total_swap"]),
                        "num_threads": int(system_info["num_threads"]),
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
            time.sleep(0.3)  # Update every 300ms
            
        except Exception as e:
            print(f"Error streaming to InfluxDB: {e}")
            time.sleep(1)

# ===== FLASK API ROUTES =====
@app.route('/send_message', methods=['POST'])
def send_message():
    """Handle messages from PC and forward to RDB when necessary"""
    global progress_update, flag_get_image, tif_file_properties, cphd_file_properties, cphd_file_list
    
    data = request.get_json()
    message = data.get("message", "")
    print(f"Received message: {message}")

    if message == "3":
        # Reset/clear operation
        progress_update = 0.0
        flag_get_image = False
        delete_all_files()
        return jsonify({"status": "success", "message": "Reset completed"})
    
    elif message == "4":
        # Scan for CPHD files
        scan_cphd_files()
        return jsonify({"status": "success", "files": cphd_file_list})
    
    elif message.startswith("SIZE:"):
        # Get file size and metadata
        progress_update = 0.0
        filename = message.split(":", 1)[1]
        file_path = cphd_files.get(filename)
        
        if file_path and os.path.exists(file_path):
            file_size = os.path.getsize(file_path)
            metadata = get_metadata_from_json(os.path.dirname(file_path))
            file_size_str = f"{file_size / 1_000_000:.2f} MB" if file_size >= 1_000_000 else f"{file_size} bytes"
            
            cphd_file_properties = {
                "filename": filename, 
                "size": file_size_str, 
                "metadata": metadata
            }
            
            return jsonify({"status": "success", "properties": cphd_file_properties})
        else:
            return jsonify({"status": "error", "message": "File not found"}), 404
    
    elif message.startswith("RUN:"):
        # Process CPHD file
        flag_get_image = True
        tif_file_properties = []
        filename = message.split(":", 1)[1]
        file_path = cphd_files.get(filename)
        
        if file_path and os.path.exists(file_path):
            threading.Thread(target=process_cphd_file, args=(file_path,), daemon=True).start()
            return jsonify({"status": "success", "message": "Processing started"})
        else:
            return jsonify({"status": "error", "message": "File not found"}), 404
    
    elif message.startswith("NETRUN:"):
        # Handle network tests
        parts = message.split(":", 2)
        if len(parts) != 3:
            return jsonify({"status": "error", "message": "Invalid NETRUN format"}), 400
        
        _, net_test_duration, net_test_interface = parts
        
        if "EthOnb" in net_test_interface:
            # Onboard test with PC
            if "LwEthOnb" in net_test_interface:
                file_path = SAVE_PATH_IPERF_LW_ETH_OB
            else:
                file_path = SAVE_PATH_IPERF_UP_ETH_OB
            
            threading.Thread(target=run_iperf3_onboard_test, args=(file_path, net_test_duration), daemon=True).start()
            return jsonify({"status": "success", "message": "Onboard test started"})
        
        elif "EthAdt" in net_test_interface or "fm1-mac9" in net_test_interface:
            # ADT test with RDB - also forward to RDB
            threading.Thread(target=handle_netrun_test_adt, args=(net_test_duration, net_test_interface), daemon=True).start()
            # Forward to RDB
            return forward_message_to_rdb(message)
        
        else:
            return jsonify({"status": "error", "message": "Unknown interface"}), 400
    
    elif message.startswith("BW:"):
        # Handle bandwidth setting
        parts = message.split(":")
        if len(parts) != 3:
            return jsonify({"status": "error", "message": "Invalid BW format"}), 400
        
        _, bw_value, target = parts
        handle_bandwidth_setting(bw_value, target)
        
        # Also forward to RDB if it's an ADT interface
        if "Adt" in target or "fm1-mac9" in target:
            return forward_message_to_rdb(message)
        
        return jsonify({"status": "success", "message": "Bandwidth set"})
    
    else:
        # Unknown message - forward to RDB
        return forward_message_to_rdb(message)

def forward_message_to_rdb(message):
    """Forward message to RDB system"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
            client_socket.connect((RDB_IP, RDB_PORT))
            client_socket.sendall(message.encode())
        return jsonify({"status": "success", "message": f"Forwarded to RDB: {message}"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

def delete_all_files():
    """Delete all files in the save directory"""
    global cphd_file_list, cphd_file_properties, tif_file_properties
    cphd_file_list = []
    cphd_file_properties = []
    tif_file_properties = []

    try:
        if os.path.exists(SAVE_DIR):
            file_list = os.listdir(SAVE_DIR)
            for file_name in file_list:
                file_path = os.path.join(SAVE_DIR, file_name)
                os.remove(file_path)
                print(f"Deleted: {file_path}")
    except Exception as e:
        print(f"Error deleting files: {e}")

@app.route('/get_cphd_files', methods=['GET'])
def get_cphd_files():
    """Returns the latest list of CPHD files"""
    return jsonify({"files": cphd_file_list})

@app.route('/get_cphd_file_properties', methods=['GET'])
def get_cphd_file_properties():
    """Returns the latest CPHD file properties"""
    return jsonify({"files": cphd_file_properties})

@app.route('/get_tif_file_properties', methods=['GET'])
def get_tif_file_properties():
    """Returns the latest TIF file properties"""
    return jsonify({"files": tif_file_properties})

@app.route('/images/<filename>')
def serve_image(filename):
    """Serve images from the save directory"""
    return send_from_directory(SAVE_DIR, filename)

# Network test result endpoints
@app.route('/iperf3/lw_eth_onb_results', methods=['GET'])
def iperf_lw_eth_onb_results():
    try:
        return send_file(SAVE_PATH_IPERF_LW_ETH_OB, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404

@app.route('/iperf3/up_eth_onb_results', methods=['GET'])
def iperf_up_eth_onb_results():
    try:
        return send_file(SAVE_PATH_IPERF_UP_ETH_OB, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404

@app.route('/iperf3/lw_eth_adt_results', methods=['GET'])
def iperf_lw_eth_adt_results():
    try:
        return send_file(SAVE_PATH_IPERF_LW_ETH_ADT, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404

@app.route('/iperf3/up_eth_adt_results', methods=['GET'])
def iperf_up_eth_adt_results():
    try:
        return send_file(SAVE_PATH_IPERF_UP_ETH_ADT, mimetype='application/json', as_attachment=False)
    except FileNotFoundError:
        return "File not found", 404

# ===== SERVER FUNCTIONS =====
def run_iperf3_server():
    """Run iperf3 server for network tests"""
    try:
        print("Starting iperf3 server...")
        subprocess.run(["iperf3", "-s"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running iperf3 server: {e}")
    except FileNotFoundError:
        print("iperf3 command not found. Please ensure iperf3 is installed.")

def run_flask_server():
    """Run Flask server for API endpoints"""
    print(f"Running Flask server on {HOST_IP}:{FLASK_PORT}...")
    app.run(host=HOST_IP, port=FLASK_PORT, debug=True, use_reloader=False)

# ===== MAIN EXECUTION =====
def main():
    """Main function to start all services"""
    print("Starting HPC Host System...")
    print(f"PC IP: {PC_IP}")
    print(f"RDB IP: {RDB_IP}")
    print(f"InfluxDB: {INFLUXDB_HOST}:{INFLUXDB_PORT}")
    
    # Initialize CPHD file scanning
    scan_cphd_files()
    
    # Start all services in separate threads
    threading.Thread(target=run_iperf3_server, daemon=True).start()
    threading.Thread(target=stream_system_metrics_to_influxdb, daemon=True).start()
    threading.Thread(target=run_flask_server, daemon=True).start()
    
    print("All services started successfully!")
    print("- iperf3 server running")
    print("- System metrics streaming to InfluxDB")
    print("- Flask API server running")
    print("- CPHD file management ready")
    
    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutdown requested... exiting")

if __name__ == "__main__":
    main()