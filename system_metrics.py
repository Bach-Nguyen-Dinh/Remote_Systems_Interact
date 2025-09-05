import psutil
import time
import threading
import csv
import os
import atexit
from datetime import datetime

# ---------- Get System Metrics ----------
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
    power_watts = delta_energy_j / delta_time_s
    return power_watts

def get_system_info():
    cpu_usage = psutil.cpu_percent(interval=None)
    per_core_usage = psutil.cpu_percent(interval=None, percpu=True)
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

    cpu_power = get_cpu_power()

    system_info = {
        "cpu_usage": cpu_usage,
        "memory_usage": memory_usage,
        "total_memory": total_memory,
        "swap_usage": swap_usage,
        "total_swap": total_swap,
        "per_core_usage": core_usage,
        "per_core_freq": core_frequencies,
        "cpu_temperature": cpu_temperature,
        "cpu_power": cpu_power,
    }
    return system_info

# ---------- System Metrics Logger ----------
class SystemMetricsLogger:
    def __init__(self):
        self._running = False
        self._thread = None
        self._buffer = []
        self._lock = threading.Lock()
        self._csv_file = None
        self._csv_write_interval = None
        self._last_csv_write = None

        # Ensure data is saved on abrupt exit
        atexit.register(self._flush_buffer_to_csv)

    def start(self, metrics_interval_ms=100, output_dir=".", csv_write_interval_s=None):
        if self._running:
            print("Logger is already running!")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._csv_file = os.path.join(output_dir, f"system_metrics_{timestamp}.csv")
        self._metrics_interval = metrics_interval_ms / 1000  # convert ms → s
        self._csv_write_interval = csv_write_interval_s
        self._last_csv_write = time.time()

        self._running = True
        self._thread = threading.Thread(target=self._collect_metrics, daemon=True)
        self._thread.start()
        print(f"Logging started. CSV: {self._csv_file}")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join()
        self._flush_buffer_to_csv()
        print(f"Logging stopped. CSV saved to: {self._csv_file}")

    def _collect_metrics(self):
        while self._running:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            metrics = get_system_info()

            # Flatten per-core dictionaries, timestamp first
            flat_metrics = {"Timestamp": timestamp}
            for k, v in metrics.items():
                if isinstance(v, dict):
                    flat_metrics.update(v)
                else:
                    flat_metrics[k] = v

            # Append to buffer thread-safely
            with self._lock:
                self._buffer.append(flat_metrics)

            # Flush to CSV if needed
            if self._csv_write_interval:
                now = time.time()
                if now - self._last_csv_write >= self._csv_write_interval:
                    self._flush_buffer_to_csv()
                    self._last_csv_write = now

            time.sleep(self._metrics_interval)

    def _flush_buffer_to_csv(self):
        with self._lock:
            if not self._buffer:
                return

            # Write header if file does not exist
            file_exists = os.path.exists(self._csv_file)
            with open(self._csv_file, mode="a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self._buffer[0].keys())
                if not file_exists:
                    writer.writeheader()
                writer.writerows(self._buffer)
            self._buffer.clear()


# ---------- Example usage ----------
if __name__ == "__main__":
    logger = SystemMetricsLogger()
    # Start logging metrics every 500 ms, write to CSV every 5 seconds
    logger.start(metrics_interval_ms=500, output_dir=".", csv_write_interval_s=5)

    # Simulate your workflow running for 20 seconds
    time.sleep(20)

    logger.stop()
