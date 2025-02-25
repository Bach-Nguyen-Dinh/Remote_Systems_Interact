from PIL import Image
import socket
import threading
import psutil
import time
import json
import os

IMAGE_PATH_1 = "/home/root/Desktop/Bach/backprojection_result_small.png"  
IMAGE_PATH_2 = "/home/root/Desktop/Bach/backprojection_histogram.png"
RESIZED_IMAGE_PATH = "/home/root/Desktop/Bach/resized_image.png"  # Temporary resized image path
HOST_IP = "10.42.0.1"  
HOST_PORT = 55555  

def resize_image(image_path, output_path, max_size=(800, 800)):
    try:
        img = Image.open(image_path)
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        img.save(output_path, "PNG", optimize=True, quality=85)  # Optimize size
        return output_path
    except Exception as e:
        print(f"Error resizing image: {e}")
        return image_path  # If resizing fails, return original image

def send_image(image_path):
    resized_path = resize_image(image_path, RESIZED_IMAGE_PATH)  # Resize before sending
    
    if not os.path.exists(resized_path):
        print(f"Error: Image file {resized_path} not found!")
        return

    print(f"Sending image {resized_path} to host...")
    
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((HOST_IP, HOST_PORT))
            with open(resized_path, "rb") as f:
                while chunk := f.read(4096):
                    sock.sendall(chunk)
            print("Image sent successfully!")
    except Exception as e:
        print(f"Error sending image: {e}")

def listen_for_messages():
    server_ip = "0.0.0.0"
    server_port = 54321
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((server_ip, server_port))
    server_socket.listen(1)
    
    print(f"Listening for messages on {server_ip}:{server_port}...")

    while True:
        conn, addr = server_socket.accept()
        with conn:
            print(f"Connection received from {addr}")
            message = conn.recv(1024).decode().strip()
            if message:
                print(f"Message from host: {message}")
                if message == "1":
                    send_image(IMAGE_PATH_1)
                elif message == "2":
                    send_image(IMAGE_PATH_2)

message_thread = threading.Thread(target=listen_for_messages, daemon=True)
message_thread.start()

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
    
    net_io = psutil.net_io_counters()
    bytes_sent = net_io.bytes_sent
    bytes_recv = net_io.bytes_recv
    
    time.sleep(0.1)
    net_io_after = psutil.net_io_counters()
    download_speed = (net_io_after.bytes_recv - bytes_recv) / 0.1
    upload_speed = (net_io_after.bytes_sent - bytes_sent) / 0.1
    
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
        "download_speed": download_speed,
        "upload_speed": upload_speed,
        "cpu_power": cpu_power,
        "root_disk_usage": root_disk_usage,
        "home_disk_usage": home_disk_usage,
        "total_disk_usage": total_disk_usage,
        "total_disk_size": total_disk_size
    }
    
    return system_info

host_ip = '10.42.0.1'
host_port = 12345
timeout = 3

while True:
    try:
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.settimeout(timeout)
        
        print("Attempting to connect to host system...")
        client_socket.connect((host_ip, host_port))
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
