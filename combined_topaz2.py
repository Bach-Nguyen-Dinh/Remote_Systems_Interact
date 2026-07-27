"""
combined_topaz2.py — Topaz host backend that serves its own frontend (no Grafana).

Same idea as combined_hpc.py, but for the Topaz/RDB ARM box. The difference is that
the AI-core, IMU and power metrics live on the embedded *target* (target_topaz2.py),
not on this machine — so this program keeps the target<->host metrics socket
(SYSINFO_PORT) instead of sampling psutil locally. Every metrics line the target
sends is:

  * cached in-memory as `latest_metrics` (the whole live snapshot), and
  * best-effort written to InfluxDB for historical logging (optional; the frontend
    never reads InfluxDB — if it is down, the dashboard still works).

The Flask app then serves the dashboard directly at '/':

  * '/'                     -> index/topaz2/combined_dashboard.html  (thin shell)
  * '/orientation'         -> index/topaz2/orientation.html         (IMU: gyro/accel/angles)
  * '/system_monitor'      -> index/topaz2/system_monitor.html      (CPU/AI/mem/net panels)
  * '/system_metrics'      -> JSON snapshot the two pages poll for live values

DESIGNED TO SIT BEHIND AN nginx PREFIX (e.g. https://<domain>/topaz/ ->
http://<this-box>:5001/, the same "streaming" pattern as the VLM box's /vlm/). For
that to work the served pages use *relative* URLs only (iframe src="orientation",
fetch("system_metrics"), …) so they resolve against whatever prefix the request
arrived on — a leading slash would escape the prefix. See set_up_proxy_server.md.
"""

import os
import json
import time
import socket
import threading
import subprocess

from flask import Flask, request, jsonify, send_file  # type: ignore
from flask_cors import CORS  # type: ignore

try:
    from influxdb import InfluxDBClient  # type: ignore
except Exception:                        # influxdb client is optional
    InfluxDBClient = None

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
HOST_IP = "0.0.0.0"          # Flask + metrics-socket bind address
FLASK_PORT = 5001
SYSINFO_PORT = 12346         # target -> host system-metrics stream (JSON per line)

TARGET_IP = "10.42.0.7"      # embedded Topaz target (for /send_message forwarding)
TARGET_PORT = 54322

INFLUXDB_HOST = "localhost"
INFLUXDB_PORT = 8086
INFLUXDB_DB = "system_metrics"
INFLUXDB_USER = "root"
INFLUXDB_PASSWORD = "root"

# Network interface the FM/main link uses on the target (shown in the Networking panel)
FM_INTERFACE_ID = "fm1-mac3"

CURR_DIR = os.path.dirname(os.path.abspath(__file__))
COMBINED_FRONTEND = os.path.join(CURR_DIR, "index", "topaz2", "combined_dashboard.html")
ORIENTATION_FRONTEND = os.path.join(CURR_DIR, "index", "topaz2", "orientation.html")
MONITOR_FRONTEND = os.path.join(CURR_DIR, "index", "topaz2", "system_monitor.html")

# ----------------------------------------------------------------------------
# Global live state
# ----------------------------------------------------------------------------
# Latest snapshot the target sent, refreshed in-place by the metrics receiver so
# the two frontend pages can pull every live value in one /system_metrics call.
# Guarded by a lock because the receiver (writer) and Flask threads (readers)
# touch it concurrently.
latest_metrics = {}
latest_metrics_lock = threading.Lock()

app = Flask(__name__)
CORS(app)

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _fnum(v, default=0.0):
    """Coerce to float, tolerating None/invalid (fields vary by hardware)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_snapshot(system_info):
    """Turn a raw target payload into the snapshot the frontend polls.

    The nested structures (per_core_usage, ai_temps, ai_run_metrics, imu_data, …)
    are passed through untouched; we only add the two derived headline numbers the
    gauges want and a server timestamp so charts can share one timeline."""
    snap = dict(system_info)

    # Overall CPU usage = mean of the per-core usages the target reported.
    pcu = system_info.get("per_core_usage", {}) or {}
    core_vals = [_fnum(v, None) for v in pcu.values()]
    core_vals = [v for v in core_vals if v is not None]
    snap["cpu_usage"] = sum(core_vals) / len(core_vals) if core_vals else 0.0

    # Overall AI-engine usage = mean over the four cores (0-indexed usage keys).
    arm = system_info.get("ai_run_metrics", {}) or {}
    ai_vals = [_fnum(arm.get(f"ai_core_{i}_usage"), None) for i in range(4)]
    ai_vals = [v for v in ai_vals if v is not None]
    snap["total_ai_usage"] = sum(ai_vals) / 4.0 if ai_vals else 0.0

    snap["timestamp"] = time.time()
    return snap


def build_influx_point(system_info, source):
    """Best-effort historical point (mirrors host_topaz2_standalone's schema)."""
    per_core_usage_data = {
        f"per_core_usage_RDB{i}": _fnum(system_info.get("per_core_usage", {}).get(f"core_{i}_usage", 0))
        for i in range(4)
    }
    total_cpu_usage = sum(per_core_usage_data.values()) * 0.25
    per_core_freq_data = {
        f"per_core_freq_RDB{i}": _fnum(system_info.get("per_core_freq", {}).get(f"core_{i}_frequency", 0))
        for i in range(4)
    }
    network_data = {}
    for iface_name, iface_stats in (system_info.get("network", {}) or {}).items():
        for stat_name, value in iface_stats.items():
            v = _fnum(value, None)
            if v is not None:
                network_data[f"{iface_name}_{stat_name}"] = v

    ai_temps = system_info.get("ai_temps", {}) or {}
    ai_freqs = system_info.get("ai_freqs", {}) or {}
    arm = system_info.get("ai_run_metrics", {}) or {}
    ai_pwrs = system_info.get("per_ai_core_pwrs", {}) or {}
    per_ai_core_temp = {f"ai_core_{i}_temp": _fnum(ai_temps.get(f"ai_core_{i}_temp", 0)) for i in range(1, 5)}
    per_ai_core_freq = {f"ai_core_{i}_freq": _fnum(ai_freqs.get(f"ai_core_{i}_freq", 0)) for i in range(1, 5)}
    per_ai_core_usage = {f"ai_core_{i}_usage": _fnum(arm.get(f"ai_core_{i}_usage", 0)) for i in range(4)}
    per_ai_core_pwr = {f"ai_core_{i}_pwr": _fnum(ai_pwrs.get(f"ai_core_{i}_pwr", 0)) for i in range(4)}
    total_ai_core_usage = sum(per_ai_core_usage.values()) * 0.25

    imu = system_info.get("imu_data", {}) or {}
    imu_data = {
        "accel_x": _fnum(imu.get("accel_x")), "accel_y": _fnum(imu.get("accel_y")), "accel_z": _fnum(imu.get("accel_z")),
        "gyro_x": _fnum(imu.get("gyro_x")), "gyro_y": _fnum(imu.get("gyro_y")), "gyro_z": _fnum(imu.get("gyro_z")),
        "angle_x": _fnum(imu.get("roll")), "angle_y": _fnum(imu.get("pitch")), "angle_z": _fnum(imu.get("yaw")),
        "imu_calibration_progress": _fnum(imu.get("calibration_progress")),
    }

    return {
        "measurement": "system_metrics",
        "tags": {"host": source},
        "fields": {
            "cpu_usage_RDB": total_cpu_usage,
            "memory_usage_RDB": _fnum(system_info.get("memory_usage")),
            "swap_usage_RDB": _fnum(system_info.get("swap_usage")),
            "sys_temp_RDB": _fnum(system_info.get("sys_temp")),
            "uptime_seconds_RDB": _fnum(system_info.get("uptime_seconds")),
            "total_memory_RDB": _fnum(system_info.get("total_memory")),
            "total_swap_RDB": _fnum(system_info.get("total_swap")),
            "num_threads_RDB": int(_fnum(system_info.get("num_threads"))),
            "cpu_power_RDB": _fnum(system_info.get("cpu_power")),
            "total_disk_usage_RDB": _fnum(system_info.get("total_disk_usage")),
            "total_disk_size_RDB": _fnum(system_info.get("total_disk_size")),
            "progress_update_RDB": _fnum(system_info.get("progress_update")),
            **per_core_usage_data,
            **per_core_freq_data,
            **network_data,
            **per_ai_core_temp,
            **per_ai_core_freq,
            "ai_total_pwr": _fnum(system_info.get("ai_total_pwr")),
            **per_ai_core_usage,
            "total_ai_usage": total_ai_core_usage,
            **per_ai_core_pwr,
            **imu_data,
        },
        "time": int(time.time() * 1e9),
    }


# ----------------------------------------------------------------------------
# Metrics receiver (target -> host socket) + cache + best-effort InfluxDB write
# ----------------------------------------------------------------------------
def _connect_influx():
    if InfluxDBClient is None:
        print("influxdb client not installed; historical logging disabled")
        return None
    try:
        client = InfluxDBClient(INFLUXDB_HOST, INFLUXDB_PORT, INFLUXDB_USER, INFLUXDB_PASSWORD, INFLUXDB_DB)
        client.ping()
        print("Connected to InfluxDB for historical logging")
        return client
    except Exception as e:
        print(f"InfluxDB unavailable ({e}); serving live metrics from memory only")
        return None


def receive_metrics():
    """Accept the target's metrics stream, cache each snapshot, log to InfluxDB."""
    global latest_metrics
    client = _connect_influx()

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST_IP, SYSINFO_PORT))
    server_socket.listen(1)
    print(f"System-metrics server listening on {HOST_IP}:{SYSINFO_PORT}")

    while True:
        client_socket, client_address = server_socket.accept()
        source = client_address[0]
        print(f"Metrics connection established with {client_address}")
        buffer = ""
        try:
            while True:
                data = client_socket.recv(1024 * 10).decode()
                if not data:
                    break
                buffer += data
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if not line.strip():
                        continue
                    try:
                        system_info = json.loads(line)
                    except json.JSONDecodeError as e:
                        print(f"JSON decode error: {e}. Skipping line.")
                        continue

                    snapshot = build_snapshot(system_info)
                    with latest_metrics_lock:
                        latest_metrics = snapshot

                    if client is not None:
                        try:
                            client.write_points([build_influx_point(system_info, source)])
                        except Exception as e:
                            print(f"InfluxDB write failed (continuing): {e}")
                            client = None   # stop retrying every 300 ms; live cache still works
        except Exception as e:
            print(f"Metrics connection error: {e}")
        finally:
            client_socket.close()
            print(f"Metrics connection with {client_address} closed")


def forward_message_to_target(message):
    """Send a control message to the embedded Topaz target over its command socket."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect((TARGET_IP, TARGET_PORT))
            s.sendall(message.encode())
        return jsonify({"status": "success", "message": message})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ----------------------------------------------------------------------------
# Flask routes
# ----------------------------------------------------------------------------
@app.route('/')
def combined_frontend():
    """Thin shell: two collapsible sections, each an <iframe> onto a standalone
    page below (Orientation + System monitoring), same-origin so both auto-size."""
    return send_file(COMBINED_FRONTEND)


@app.route('/orientation')
def orientation_frontend():
    """IMU orientation page (gyroscope, accelerometer, integrated angles)."""
    return send_file(ORIENTATION_FRONTEND)


@app.route('/monitor')
@app.route('/system_monitor')
def monitor_frontend():
    """Live system-utilisation page (CPU / AI cores / memory / disk / network)."""
    return send_file(MONITOR_FRONTEND)


@app.route('/system_metrics', methods=['GET'])
def system_metrics():
    """The whole live snapshot the two dashboard pages need, in one call. Served
    straight from the in-memory cache the metrics receiver fills."""
    with latest_metrics_lock:
        return jsonify(latest_metrics)


@app.route('/send_message', methods=['POST'])
def send_message():
    """Forward a control command to the embedded target (kept minimal for now)."""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    print(f"Command: {message}")
    return forward_message_to_target(message)


@app.route('/imu/recalibrate', methods=['POST'])
def recalibrate_imu():
    """Trigger IMU recalibration on the target device."""
    return forward_message_to_target("recalibrate_imu")


def run_flask_server():
    print(f"Running Flask server on {HOST_IP}:{FLASK_PORT} ...")
    app.run(host=HOST_IP, port=FLASK_PORT, debug=False, use_reloader=False)


# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    threading.Thread(target=receive_metrics, daemon=True).start()
    threading.Thread(target=run_flask_server, daemon=True).start()
    while True:
        time.sleep(1)
