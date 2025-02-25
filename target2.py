import socket
import psutil
import time
import json

host_ip = '10.42.0.1'
host_port = 12345
timeout = 3  # Timeout for socket connection in seconds

def get_system_info():
    # Get overall CPU usage
    cpu_usage = psutil.cpu_percent(interval=1)

    # Get per-core CPU usage
    per_core_usage = psutil.cpu_percent(interval=1, percpu=True)
    core_usage = {f"core_{i}_usage": usage for i, usage in enumerate(per_core_usage)}

    # Get CPU frequencies
    core_frequencies = {}
    if hasattr(psutil, "cpu_freq"):
        freq_info = psutil.cpu_freq(percpu=True)
        if freq_info:  # Ensure it's not None
            core_frequencies = {f"core_{i}_frequency": freq.current for i, freq in enumerate(freq_info)}

    # Get CPU temperature (if available)
    cpu_temperature = None
    if hasattr(psutil, "sensors_temperatures"):
        temp_info = psutil.sensors_temperatures()
        if 'coretemp' in temp_info:
            cpu_temperature = temp_info['coretemp'][0].current  # Taking the first core temperature

    # Get memory usage
    memory_usage = psutil.virtual_memory().percent
    total_memory = psutil.virtual_memory().total

    # Get swap usage
    swap_usage = psutil.swap_memory().percent
    total_swap = psutil.swap_memory().total

    # Get disk usage
    disk_usage = psutil.disk_usage('/').percent
    total_disk = psutil.disk_usage('/').total

    # Get number of threads and cores
    num_threads = psutil.cpu_count(logical=True)  # Logical processors (threads)
    num_cores = psutil.cpu_count(logical=False)  # Physical cores

    # Get system uptime
    uptime_seconds = time.time() - psutil.boot_time()

    # Get network stats (bytes sent and received)
    net_io = psutil.net_io_counters()
    bytes_sent = net_io.bytes_sent
    bytes_recv = net_io.bytes_recv

    # Calculate download and upload speed
    time.sleep(1)  # Wait for a second before capturing again
    net_io_after = psutil.net_io_counters()
    download_speed = (net_io_after.bytes_recv - bytes_recv)  # Bytes received in the last second
    upload_speed = (net_io_after.bytes_sent - bytes_sent)  # Bytes sent in the last second

    # Create system info dictionary
    system_info = {
        "cpu_usage": cpu_usage,
        "memory_usage": memory_usage,
        "disk_usage": disk_usage,
        "total_memory": total_memory,
        "total_swap": total_swap,
        "swap_usage": swap_usage,  # Added swap usage
        "total_disk": total_disk,
        "num_threads": num_threads,
        "num_cores": num_cores,
        "uptime_seconds": uptime_seconds,
        "per_core_usage": core_usage,
        "per_core_freq": core_frequencies,
        "cpu_temperature": cpu_temperature,
        "download_speed": download_speed,  # In bytes per second
        "upload_speed": upload_speed,  # In bytes per second
    }
    
    return system_info

while True:
    try:
        # Create a socket object
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.settimeout(timeout)

        # Try connecting to the host system
        print("Attempting to connect to host system...")
        client_socket.connect((host_ip, host_port))
        print("Connected to host system!")

        # Continuously send system information every 0.3 seconds
        while True:
            system_info = get_system_info()
            client_socket.sendall((json.dumps(system_info) + "\n").encode())
            print(f"Sent system info: {system_info}")
            time.sleep(0.3)

    except (socket.error, socket.timeout, ConnectionRefusedError) as e:
        print(f"Connection failed: {e}. Retrying in 1 second...")
        time.sleep(1)  # Retry every 1 second

    finally:
        # Ensure the socket is closed after failure or disconnect
        if 'client_socket' in locals() and client_socket.fileno() != -1:
            client_socket.close()

