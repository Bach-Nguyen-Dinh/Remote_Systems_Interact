import psutil
import time

def read_rapl_energy():
    try:
        with open("/sys/class/powercap/intel-rapl:0/energy_uj", "r") as f:
            return int(f.read().strip())  # Energy in microjoules
    except FileNotFoundError:
        return None

def get_cpu_power():
    energy_start = read_rapl_energy()
    t_start = time.time()
    time.sleep(0.1)
    energy_end = read_rapl_energy()
    t_end = time.time()

    if energy_start is None or energy_end is None:
        return None

    delta_energy_j = (energy_end - energy_start) / 1_000_000  # convert to joules
    delta_time_s = t_end - t_start
    power_watts = delta_energy_j / delta_time_s # Watts = Joules / Seconds
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
    
    # uptime_seconds = time.time() - psutil.boot_time()
    
    cpu_power = get_cpu_power()
    
    system_info = {
        "cpu_usage": cpu_usage,
        "memory_usage": memory_usage,
        "total_memory": total_memory,
        "swap_usage": swap_usage,
        "total_swap": total_swap,
        # "uptime_seconds": uptime_seconds,
        "per_core_usage": core_usage,
        "per_core_freq": core_frequencies,
        "cpu_temperature": cpu_temperature,
        "cpu_power": cpu_power,
    }
    
    return system_info

system_info = get_system_info()
print(system_info)