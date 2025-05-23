from PIL import Image
import socket
import subprocess
import threading
import psutil
import time
import json
import os

# IMAGE_PATH_2 = "/home/root/Desktop/Bach/backprojection_result_small.png"  
# IMAGE_PATH_1 = "/home/root/Desktop/Bach/backprojection_histogram.png"
RESIZED_IMAGE_PATH = "/home/root/Desktop/Bach/optimized_image.webp"  # Temporary resized image path
DEMO_PATH = "/home/root/Desktop/Bach/"

HOST_IP = "10.42.0.1"
SYSINFO_PORT = 12345
NETTEST_PORT = 29102
IMAGE_PORT = 55555

LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 54321
SOCK_TOUT = 3

RDB_IP = "169.254.207.123"

LW_ETH_OB_INTERFACE_ID = "enp2s0"
UP_ETH_OB_INTERFACE_ID = "enp3s0"
LW_ETH_ADT_INTERFACE_ID = "enp1s0f1"
UP_ETH_ADT_INTERFACE_ID = "enp1s0f0"

SAVE_PATH_IPERF_LW_ETH_ADT = "/home/root/iperf3_end_result_LwEthAdt.json"
SAVE_PATH_IPERF_UP_ETH_ADT = "/home/root/iperf3_end_result_UpEthAdt.json"

# Global variable
progress_update = 0.0
cphd_files = {}

def optimize_tif(image_path, output_path, format="webp", max_size=(800, 800), quality=85):
    """
    Optimize and convert a TIFF image for efficient transmission and web display.

    :param image_path: Path to the input TIFF file.
    :param output_path: Path to save the optimized image.
    :param format: Target format (jpg, png, webp, avif).
    :param max_size: Tuple (width, height) to resize the image.
    :param quality: Quality setting for lossy formats (JPEG/WebP/AVIF).
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

        # Print out the size of the optimized image
        optimized_size = os.path.getsize(output_path)
        print(f"Optimized image saved at {output_path}, size: {optimized_size} bytes")

    except Exception as e:
        print(f"Error optimizing image: {e}")

def send_image(image_path):
    optimize_tif(image_path, RESIZED_IMAGE_PATH, format="webp", max_size=(800, 800), quality=80)
    
    if not os.path.exists(RESIZED_IMAGE_PATH):
        print(f"Error: Image file {RESIZED_IMAGE_PATH} not found!")
        return

    file_size = os.path.getsize(RESIZED_IMAGE_PATH)
    print(f"Sending image {RESIZED_IMAGE_PATH} to host, size: {file_size} bytes...")
    
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((HOST_IP, IMAGE_PORT))

            # Send file size first
            sock.sendall(file_size.to_bytes(8, byteorder="big"))

            # Read and send the file
            with open(RESIZED_IMAGE_PATH, "rb") as f:
                while chunk := f.read(4096):
                    sock.sendall(chunk)

            print("Image sent successfully!")
    except Exception as e:
        print(f"Error sending image: {e}")

def handle_image_sending(image_path):
    timestamps = {
        0: 0.0,
        1: 6.67,
        2: 13.33,
        3: 20.0,
        4: 26.67,
        5: 33.33,
        6: 40.0,
        10: 66.67,
        11: 73.33,
        12: 80.0,
        15: 100.0
    }
    for second in range(16):
        time.sleep(1)
        if second in timestamps:
            global progress_update
            progress_update = timestamps[second]
    send_image(image_path)

def send_cphd_files_list():
    global cphd_files
    cphd_files = {}  # Change to dictionary

    if os.path.exists(DEMO_PATH):
        print("Directory exists!")
    else:
        print("Directory does NOT exist!")

    # Scan for .cphd files
    for root, dirs, files in os.walk(DEMO_PATH):
        for file in files:
            if file.endswith(".cphd"):
                full_path = os.path.join(root, file)
                cphd_files[file] = full_path  # Store filename as key, full path as value

    print("Found .cphd files:", list(cphd_files.keys()))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((HOST_IP, IMAGE_PORT))
            sock.sendall(json.dumps({"cphd_files": list(cphd_files.keys())}).encode())  # Send only filenames
    except Exception as e:
        print(f"Error sending CPHD files: {e}")
    
def get_cphd_file_size(filename):
    global cphd_files
    file_path = cphd_files.get(filename)

    if file_path and os.path.exists(file_path):
        return os.path.getsize(file_path)
    return None

def get_metadata_from_json(directory):
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
    directory = os.path.dirname(file_path)
    tif_files = [f for f in os.listdir(directory) if f.endswith(".tif")]
    # png_files = [f for f in os.listdir(directory) if f.endswith(".png")]
    
    if tif_files:
        tif_path = os.path.join(directory, tif_files[0])  # Take the first .tif file found

        # send the processed image
        handle_image_sending(tif_path)
        
        # send the properies of the processed image
        tif_size = os.path.getsize(tif_path)
        cphd_size = os.path.getsize(file_path)
        reduction_scale = round(cphd_size / tif_size, 2)
        size_compared = round((tif_size / cphd_size) * 100, 2) if cphd_size else 0
        reduction_factor = round(100 - size_compared, 2)

        tif_size_str = f"{tif_size / 1_000_000:.2f} MB" if tif_size >= 1_000_000 else f"{tif_size} bytes"


        response = {
            "tif_filename": tif_files[0],
            "size": tif_size_str,
            "reduction_factor": reduction_factor,
            "size_compared": size_compared,
            "reduction_scale": reduction_scale
        }        
        print(f"Response: {response}")
        
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as size_sock:
                size_sock.connect((HOST_IP, IMAGE_PORT))
                size_sock.sendall(json.dumps(response).encode())
        except Exception as e:
            print(f"Error sending tif file properties: {e}")

        # if png_files:
        #     png_path = os.path.join(directory, png_files[0])

        #     # send the furhter processed image
        #     handle_image_sending(png_path)

def handle_netrun_test(netTestDuration):
    file_path = SAVE_PATH_IPERF_LW_ETH_ADT

    def run_test(reverse=False):
        command = ["iperf3", "-c", RDB_IP, "-b", "20G", "-t", netTestDuration, "-P", "4", "-i", "1", "-J"]
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

        print(f"Results saved to {file_path}")

        try:
            with open(file_path, "r") as json_file:
                json_data = json.load(json_file)
            response = {"data": json_data}
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as data_sock:
                data_sock.connect((HOST_IP, NETTEST_PORT))
                data_sock.sendall(json.dumps(response).encode())
            print(f"JSON data sent to {HOST_IP}:{NETTEST_PORT}")
        except Exception as e:
            print(f"Error sending JSON data over socket: {e}")
    else:
        print("No valid results to save.")

def listen_for_messages():
    global progress_update

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((LISTEN_IP, LISTEN_PORT))
    server_socket.listen(1)
    
    print(f"Listening for messages on {LISTEN_IP}:{LISTEN_PORT}...")

    while True:
        conn, addr = server_socket.accept()
        with conn:
            print(f"Connection received from {addr}")
            message = conn.recv(1024).decode().strip()
            if message:
                print(f"Message from host: {message}")
                # if message == "2":
                #     threading.Thread(target=handle_image_sending, args=(IMAGE_PATH_1,), daemon=True).start()
                # elif message == "1":
                #     send_image(IMAGE_PATH_2)
                if message == "3":
                    progress_update = 0.0
                elif message == "4":
                    send_cphd_files_list()  # Send CPHD files back to host
                elif message.startswith("SIZE:"):
                    progress_update = 0.0
                    filename = message.split(":", 1)[1]
                    file_path = cphd_files.get(filename)
                    
                    if file_path and os.path.exists(file_path):
                        file_size = os.path.getsize(file_path)
                        metadata = get_metadata_from_json(os.path.dirname(file_path))
                        
                        file_size_str = f"{file_size / 1_000_000:.2f} MB" if file_size >= 1_000_000 else f"{file_size} bytes"
                        
                        response = {"filename": filename, "size": file_size_str, "metadata": metadata}
                        print(f"Response: {response}")
                        
                        try:
                            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as size_sock:
                                size_sock.connect((HOST_IP, IMAGE_PORT))
                                size_sock.sendall(json.dumps(response).encode())
                        except Exception as e:
                            print(f"Error sending file size and metadata: {e}")
                elif message.startswith("RUN:"):
                    filename = message.split(":", 1)[1]
                    file_path = cphd_files.get(filename)
                    
                    if file_path and os.path.exists(file_path):
                        threading.Thread(target=process_cphd_file, args=(file_path,), daemon=True).start()
                elif message.startswith("NETRUN:"):
                    # handle run iperf test in thread to avoid blocking other tasks
                    netTestDuration = message.split(":", 1)[1]
                    threading.Thread(target=handle_netrun_test, args=(netTestDuration,), daemon=True).start()

                elif message.startswith("BW:"):
                    parts = message.split(":")
                    if len(parts) != 3:
                        print("Invalid BW message format")
                        return

                    _, bwValue, target = parts  # bwValue = "1000", target = "LwEthOnb"

                    # Determine the correct interface ID
                    if target == "LwEthOnb":
                        interface_id = LW_ETH_OB_INTERFACE_ID
                    elif target == "UpEthOnb":
                        interface_id = UP_ETH_OB_INTERFACE_ID
                    elif target == "LwEthAdt":
                        interface_id = LW_ETH_ADT_INTERFACE_ID
                    elif target == "UpEthAdt":
                        interface_id = UP_ETH_ADT_INTERFACE_ID
                    else:
                        print(f"Unknown interface identifier: {target}")
                        return

                    # Construct the ethtool command
                    command = ["ethtool", "-s", interface_id, "speed", bwValue, "autoneg", "on"]
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
                    else:
                        print(f"Error executing command. Return code: {process.returncode}")
                        if stderr:
                            print("Error message:", stderr)


# Function to run the iperf3 server
def run_iperf3_server():
    try:
        # Start iperf3 server as a subprocess
        subprocess.run(["iperf3", "-s"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running iperf3 server: {e}")
    except FileNotFoundError:
        print("iperf3 command not found. Please ensure iperf3 is installed.")

def read_rapl_energy():
    try:
        with open("/sys/class/powercap/intel-rapl:0/energy_uj", "r") as f:
            return int(f.read().strip())  # Energy in microjoules
    except FileNotFoundError:
        return None

def get_cpu_power():
    energy_start = read_rapl_energy()
    if energy_start is None:
        return None  # Intel RAPL not available
    
    time.sleep(0.1)  # Wait for a second to measure power
    energy_end = read_rapl_energy()
    if energy_end is None:
        return None
    
    power_watts = (energy_end - energy_start) / 1_000_000 / 0.1  # Convert µJ to W
    return power_watts

def get_system_info():
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
    
    # Get disk usage for both '/' and '/home/root'
    root_disk_usage = psutil.disk_usage('/').percent
    root_total_disk = psutil.disk_usage('/').total
    home_disk_usage = psutil.disk_usage('/home/root/Desktop/Bach').percent
    home_total_disk = psutil.disk_usage('/home/root/Desktop/Bach').total
    
    total_disk_usage = (root_disk_usage * root_total_disk + home_disk_usage * home_total_disk) / (root_total_disk + home_total_disk)
    total_disk_size = root_total_disk + home_total_disk
    
    num_threads = psutil.cpu_count(logical=True)
    num_cores = psutil.cpu_count(logical=False)
    
    uptime_seconds = time.time() - psutil.boot_time()
    
    # net_io = psutil.net_io_counters()
    # bytes_sent = net_io.bytes_sent
    # bytes_recv = net_io.bytes_recv
    
    # # time.sleep(0.1)
    # net_io_after = psutil.net_io_counters()
    # download_speed = (net_io_after.bytes_recv - bytes_recv) / 0.1
    # upload_speed = (net_io_after.bytes_sent - bytes_sent) / 0.1

    # Network info for specific interfaces
    interfaces = [LW_ETH_OB_INTERFACE_ID, LW_ETH_ADT_INTERFACE_ID, UP_ETH_OB_INTERFACE_ID, UP_ETH_ADT_INTERFACE_ID]
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
                # "errors_in": net_a.errin,
                # "errors_out": net_a.errout,
                # "dropin": net_a.dropin,
                # "dropout": net_a.dropout,
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
        # "download_speed": download_speed,
        # "upload_speed": upload_speed,
        "network": network_stats,
        "cpu_power": cpu_power,
        "root_disk_usage": root_disk_usage,
        "home_disk_usage": home_disk_usage,
        "total_disk_usage": total_disk_usage,
        "total_disk_size": total_disk_size,
        "progress_update": progress_update
    }
    
    return system_info

# All the thread
threading.Thread(target=listen_for_messages, daemon=True).start()
threading.Thread(target=run_iperf3_server, daemon=True).start()

while True:
    try:
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.settimeout(SOCK_TOUT)
        
        print("Attempting to connect to host system...")
        client_socket.connect((HOST_IP, SYSINFO_PORT))
        print("Connected to host system!")
        
        while True:
            system_info = get_system_info()
            client_socket.sendall((json.dumps(system_info) + "\n").encode())
            # print(f"Sent system info: {system_info}")
            time.sleep(0.3)
    
    except (socket.error, socket.timeout, ConnectionRefusedError) as e:
        print(f"Connection failed: {e}. Retrying in 1 second...")
        time.sleep(1)
    
    finally:
        if 'client_socket' in locals() and client_socket.fileno() != -1:
            client_socket.close()
