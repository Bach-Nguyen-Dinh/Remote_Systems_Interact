import psutil
import time
# import json

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
    
    # # Get disk usage for both '/' and '/home/root'
    # root_disk_usage = psutil.disk_usage('/').percent
    # root_total_disk = psutil.disk_usage('/').total
    # home_disk_usage = 0
    # home_total_disk = 0
    
    # total_disk_usage = (root_disk_usage * root_total_disk + home_disk_usage * home_total_disk) / (root_total_disk + home_total_disk)
    # total_disk_size = root_total_disk + home_total_disk
    
    # num_threads = psutil.cpu_count(logical=True)
    # num_cores = psutil.cpu_count(logical=False)
    
    # uptime_seconds = time.time() - psutil.boot_time()
    
    cpu_power = get_cpu_power()
    
    system_info = {
        "cpu_usage": cpu_usage,
        "memory_usage": memory_usage,
        "total_memory": total_memory,
        "swap_usage": swap_usage,
        "total_swap": total_swap,
        # "num_threads": num_threads,
        # "num_cores": num_cores,
        # "uptime_seconds": uptime_seconds,
        "per_core_usage": core_usage,
        "per_core_freq": core_frequencies,
        "cpu_temperature": cpu_temperature,
        "cpu_power": cpu_power,
        # "root_disk_usage": root_disk_usage,
        # "home_disk_usage": home_disk_usage,
        # "total_disk_usage": total_disk_usage,
        # "total_disk_size": total_disk_size,
    }
    
    return system_info

system_info = get_system_info()
print(system_info)