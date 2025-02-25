from influxdb import InfluxDBClient
import json
import socket
import time

host_ip = '0.0.0.0'
host_port = 12345
influxdb_host = "localhost"  # InfluxDB host
influxdb_port = 8086  # InfluxDB port
influxdb_db = "system_metrics"  # Database name
influxdb_user = "root"  # InfluxDB username (default for 1.x)
influxdb_password = "root"  # InfluxDB password (default for 1.x)

# Connect to InfluxDB
client = InfluxDBClient(influxdb_host, influxdb_port, influxdb_user, influxdb_password, influxdb_db)

# Create the socket object
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.bind((host_ip, host_port))
server_socket.listen(1)

print("Server is listening for incoming connections...")

# Accept the incoming connection
client_socket, client_address = server_socket.accept()
print(f"Connection established with {client_address}")

buffer = ""
while True:
    data = client_socket.recv(1024 * 10).decode()
    if not data:
        break  # Connection closed

    buffer += data
    while "\n" in buffer:
        message, buffer = buffer.split("\n", 1)
        try:
            system_info = json.loads(message)
            
            # Ensure all per-core usage values are floats
            per_core_usage_data = {
                f"per_core_usage{i}": float(system_info["per_core_usage"].get(f"core_{i}_usage", 0))
                for i in range(32)
            }

            # Ensure all per-core frequencies are floats
            per_core_freq_data = {
                f"per_core_freq{i}": float(system_info["per_core_freq"].get(f"core_{i}_frequency", 0))
                for i in range(32)
            }

            # Convert values to float to prevent InfluxDB type conflicts
            cpu_temperature = float(system_info.get("cpu_temperature", 0.0))
            cpu_power = float(system_info.get("cpu_power", 0.0))
            download_speed = float(system_info.get("download_speed", 0.0))
            upload_speed = float(system_info.get("upload_speed", 0.0))

            # Prepare the data for InfluxDB
            json_body = [
                {
                    "measurement": "system_metrics",
                    "tags": {
                        "host": client_address[0]
                    },
                    "fields": {
                        "cpu_usage": float(system_info["cpu_usage"]),
                        "memory_usage": float(system_info["memory_usage"]),
                        "disk_usage": float(system_info["disk_usage"]),
                        "swap_usage": float(system_info["swap_usage"]),
                        "cpu_temperature": cpu_temperature,
                        "uptime_seconds": float(system_info["uptime_seconds"]),
                        "total_memory": float(system_info["total_memory"]),
                        "total_swap": float(system_info["total_swap"]),
                        "total_disk": float(system_info["total_disk"]),
                        "num_threads": int(system_info["num_threads"]),
                        "download_speed": download_speed,  # Ensured float
                        "upload_speed": upload_speed,  # Ensured float
                        "cpu_power": cpu_power,  # Ensured float
                        **per_core_usage_data,
                        **per_core_freq_data
                    },
                    "time": int(time.time() * 1e9)  # Convert to nanoseconds
                }
            ]

            # Write the data to InfluxDB
            client.write_points(json_body)
            print(f"Stored data in InfluxDB: {system_info}")
        except json.JSONDecodeError as e:
            print(f"JSON Decode Error: {e}. Skipping message.")

# Close the client socket
client_socket.close()
server_socket.close()







from influxdb import InfluxDBClient
import json
import socket
import time

host_ip = '0.0.0.0'
host_port = 12345
influxdb_host = "localhost"  # InfluxDB host
influxdb_port = 8086  # InfluxDB port
influxdb_db = "system_metrics"  # Database name from step 1
influxdb_user = "root"  # InfluxDB username (default for 1.x)
influxdb_password = "root"  # InfluxDB password (default for 1.x)

# Connect to InfluxDB
client = InfluxDBClient(influxdb_host, influxdb_port, influxdb_user, influxdb_password, influxdb_db)

# Create the socket object
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.bind((host_ip, host_port))
server_socket.listen(1)

print("Server is listening for incoming connections...")

# Accept the incoming connection
client_socket, client_address = server_socket.accept()
print(f"Connection established with {client_address}")

buffer = ""
while True:
    data = client_socket.recv(1024*10).decode()
    if not data:
        break  # Connection closed

    buffer += data
    while "\n" in buffer:
        message, buffer = buffer.split("\n", 1)
        try:
            system_info = json.loads(message)
            
            # Generate per-core usage dynamically up to 31 cores (ensure float type)
            per_core_usage_data = {
                f"per_core_usage{i}": float(system_info["per_core_usage"].get(f"core_{i}_usage", 0))
                for i in range(32)
            }

            # Generate per-core frequency dynamically up to 31 cores (ensure float type)
            per_core_freq_data = {
                f"per_core_freq{i}": float(system_info["per_core_freq"].get(f"core_{i}_frequency", 0))
                for i in range(32)
            }

            # Prepare the data for InfluxDB
            json_body = [
                {
                    "measurement": "system_metrics",
                    "tags": {
                        "host": client_address[0]
                    },
                    "fields": {
                        "cpu_usage": system_info["cpu_usage"],
                        "memory_usage": system_info["memory_usage"],
                        "disk_usage": system_info["disk_usage"],
                        "swap_usage": system_info["swap_usage"],
                        "cpu_temperature": system_info["cpu_temperature"],
                        "uptime_seconds": system_info["uptime_seconds"],
                        "total_memory": system_info["total_memory"],
                        "total_swap": system_info["total_swap"],
                        "total_disk": system_info["total_disk"],
                        "num_threads": system_info["num_threads"],
                        "download_speed": system_info["download_speed"],
                        "upload_speed": system_info["upload_speed"],
                        **per_core_usage_data,
                        **per_core_freq_data
                    },
                    "time": int(time.time() * 1e9)  # Convert to nanoseconds
                }
            ]

            # Write the data to InfluxDB
            client.write_points(json_body)
            print(f"Stored data in InfluxDB: {system_info}")
        except json.JSONDecodeError as e:
            print(f"JSON Decode Error: {e}. Skipping message.")

# Close the client socket
client_socket.close()
server_socket.close()
