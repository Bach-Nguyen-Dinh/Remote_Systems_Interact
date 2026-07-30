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

plus the four "Vision model" workload pages the shell hosts as tabs, each with its
own small API family (image lists, image bytes, run progress):

  * '/small_obj_app'  -> index_grafana_topaz_small_object.html + '/small_obj_detect/*'
  * '/auto_nav_app'   -> index_land_nav.html                   + '/auto_nav/*'
  * '/ai_ship_app'    -> index_ai_ship.html                    + '/ai_ship/*'
  * '/ai_smoke_app'   -> index_ai_smoke.html                   + '/ai_smoke/*'

Those pages' live progress ("3 of 40 images processed", elapsed time, …) does not
come over the metrics stream: the target opens a short connection to DATA_PORT and
posts one JSON object per update, which run_data_server() files into the matching
latest_*_progress cache (see broadcast_to_clients).

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

from flask import Flask, request, jsonify, send_file, send_from_directory  # type: ignore
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
DATA_PORT = 55556            # target -> host workload updates (one JSON object per connection)

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
INDEX_DIR = os.path.join(CURR_DIR, "index", "topaz2")
COMBINED_FRONTEND = os.path.join(INDEX_DIR, "combined_dashboard.html")
ORIENTATION_FRONTEND = os.path.join(INDEX_DIR, "orientation.html")
MONITOR_FRONTEND = os.path.join(INDEX_DIR, "system_monitor.html")

# Vision-model tab pages (hosted as <iframe>s by the shell's "Vision model" row)
SMALL_OBJ_FRONTEND = os.path.join(INDEX_DIR, "index_grafana_topaz_small_object.html")
AUTO_NAV_FRONTEND = os.path.join(INDEX_DIR, "index_land_nav.html")
AI_SHIP_FRONTEND = os.path.join(INDEX_DIR, "index_ai_ship.html")
AI_SMOKE_FRONTEND = os.path.join(INDEX_DIR, "index_ai_smoke.html")

# Image sets those pages display. Input/"before" and output/"after" frames are
# produced on this host (or copied here) — the target only reports progress.
SMALL_OBJ_INPUT_DIR = os.path.join(CURR_DIR, "small_obj_detect", "data1", "image")
SMALL_OBJ_OUTPUT_DIR = os.path.join(CURR_DIR, "small_obj_detect", "data1", "predictions")

AI_SMOKE_INPUT_DIR = "/home/matthew/Downloads/SmokeNet-Data/validation/opt_web_img"
AI_SMOKE_OUTPUT_DIR = "/home/matthew/Downloads/SmokeNet-Data/classification/opt_web_img"

AI_SHIP_INPUT_DIR = "/home/matthew/modified_ai_ship/ship/short_example/opt_web_img"
AI_SHIP_OUTPUT_DIR = "/home/matthew/modified_ai_ship/ship/output_segment/opt_web_img"

AUTO_NAV_DIRS = {
    "gps": os.path.join(CURR_DIR, "autonomous_nav", "gps"),
    "features": os.path.join(CURR_DIR, "autonomous_nav", "features"),
    "depth": os.path.join(CURR_DIR, "autonomous_nav", "depth"),
    "lidar": os.path.join(CURR_DIR, "autonomous_nav", "lidar"),
}
# The auto-nav page validates with the '<kind>_img' spelling; keep the two apart
# so /auto_nav/images/<kind>/ URLs stay short.
AUTO_NAV_VALIDATE_DIRS = {k + "_img": v for k, v in AUTO_NAV_DIRS.items()}

# ----------------------------------------------------------------------------
# Global live state
# ----------------------------------------------------------------------------
# Latest snapshot the target sent, refreshed in-place by the metrics receiver so
# the two frontend pages can pull every live value in one /system_metrics call.
# Guarded by a lock because the receiver (writer) and Flask threads (readers)
# touch it concurrently.
latest_metrics = {}
latest_metrics_lock = threading.Lock()

# Last workload update the target pushed to DATA_PORT, one cache per workload
# (the pages poll their own '<workload>/progress'). Plain dict assignment only —
# never mutated in place — so readers always see a complete object.
latest_progress = {
    "small_obj_detect": {},
    "auto_nav": {},
    "ai_ship": {},
    "ai_smoke": {},
}

# PID of the program each workload currently has running *on the target*, learned
# from the "<workload>_start" progress update and dropped when the run ends. A
# page's Stop button posts "stop_<workload>"; we append the PID before forwarding
# so the target only ever kills the run the user was actually looking at (same
# scheme as the HPC SAR panel's STOPSAR).
workload_pids = {key: None for key in latest_progress}

# AI-engine usage, used by the ai_ship/ai_smoke pages to detect "the workload has
# actually started on the card" (usage risen ~5% above the baseline captured when
# Run was pressed) and only then begin cycling through the image pairs.
latest_ai_core_usage = 0.0
ai_baseline_usage = {"ai_ship": 0.0, "ai_smoke": 0.0}

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
    global latest_metrics, latest_ai_core_usage
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
                    # Mirrored out of the snapshot so the vision pages' cheap
                    # 200 ms '/ai_core_usage' poll never has to take the lock.
                    latest_ai_core_usage = snapshot["total_ai_usage"]

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


# ----------------------------------------------------------------------------
# Workload progress receiver (target -> host, one JSON object per connection)
# ----------------------------------------------------------------------------
def _progress_key(update):
    """Which cache does this update belong to? The target tags every workload
    message with a 'type' prefixed by the workload name ("ai_smoke_start",
    "small_obj_detect_progress", …). Returns None for anything else — the same
    port also carries CPHD file lists and file-size replies this dashboard has no
    use for, and those must not land in a progress cache."""
    msg_type = str(update.get("type", ""))
    for key in ("ai_smoke", "ai_ship", "auto_nav", "small_obj_detect"):
        if msg_type.startswith(key):
            return key
    return None


def _track_workload_pid(key, update):
    """Keep workload_pids in step with the run this update describes.

    The target puts its process's PID in the "_start" message and repeats it on
    the message that ends the run ("_complete" / "_stopped" / "_error"), at which
    point there is nothing left to stop."""
    msg_type = str(update.get("type", ""))
    if msg_type.endswith("_start"):
        workload_pids[key] = update.get("pid")
    elif msg_type.endswith(("_complete", "_stopped", "_error")):
        workload_pids[key] = None


def receive_workload_updates():
    """Accept the target's workload-progress connections and cache each update.

    Unlike the metrics stream this is connection-per-message: the target dials in,
    writes one JSON object, and hangs up — so read until EOF and parse the whole
    payload rather than splitting on newlines."""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST_IP, DATA_PORT))
    server_socket.listen(5)
    print(f"Workload-progress server listening on {HOST_IP}:{DATA_PORT}")

    while True:
        conn, addr = server_socket.accept()
        try:
            with conn:
                conn.settimeout(5)
                chunks = []
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                raw = b"".join(chunks).decode(errors="replace").strip()
                if not raw:
                    continue
                update = json.loads(raw)
                key = _progress_key(update)
                if key is not None:
                    latest_progress[key] = update
                    _track_workload_pid(key, update)
        except json.JSONDecodeError:
            pass    # image bytes / non-JSON traffic on the same port — not ours
        except Exception as e:
            print(f"Workload-progress connection error ({addr}): {e}")


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


@app.route('/small_obj_app')
def small_obj_frontend():
    """Small object detection page (Vision model tab)."""
    return send_file(SMALL_OBJ_FRONTEND)


@app.route('/auto_nav_app')
def auto_nav_frontend():
    """Autonomous navigation page (Vision model tab)."""
    return send_file(AUTO_NAV_FRONTEND)


@app.route('/ai_ship_app')
def ai_ship_frontend():
    """AI ship detection page (Vision model tab)."""
    return send_file(AI_SHIP_FRONTEND)


@app.route('/ai_smoke_app')
def ai_smoke_frontend():
    """AI smoke detection page (Vision model tab)."""
    return send_file(AI_SMOKE_FRONTEND)


@app.route('/system_metrics', methods=['GET'])
def system_metrics():
    """The whole live snapshot the two dashboard pages need, in one call. Served
    straight from the in-memory cache the metrics receiver fills."""
    with latest_metrics_lock:
        return jsonify(latest_metrics)


@app.route('/send_message', methods=['POST'])
def send_message():
    """Forward a control command to the embedded target.

    Three families of command need host-side bookkeeping before (or instead of)
    being forwarded:

      * "run_<workload>[:<n cores>]" — drop the previous run's progress so the
        page doesn't briefly show stale numbers, and for the AI-engine workloads
        snapshot current usage as the baseline their "has it actually started?"
        check measures against.
      * "stop_<workload>" — append the PID the target reported for the run in
        flight, so it kills that run and not a newer one.
      * "clear_<workload>" — purely host-side; nothing to tell the target."""
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    print(f"Command: {message}")

    for workload in latest_progress:
        if message.startswith(f"run_{workload}"):
            latest_progress[workload] = {}
            if workload in ai_baseline_usage:
                ai_baseline_usage[workload] = latest_ai_core_usage
            break
        if message == f"stop_{workload}":
            pid = workload_pids.get(workload)
            if pid is None:
                # Nothing recorded as running (start update lost, or the run
                # already ended). Forward it anyway — the target stops whatever
                # it still has running for this workload, and no-ops otherwise.
                print(f"No PID recorded for {workload}; forwarding unqualified stop")
            else:
                print(f"Requesting target to stop {workload} process {pid}")
                message = f"{message}:{pid}"
            break
        if message == f"clear_{workload}":
            latest_progress[workload] = {}
            return jsonify({"status": "cleared", "workload": workload})

    return forward_message_to_target(message)


@app.route('/imu/recalibrate', methods=['POST'])
def recalibrate_imu():
    """Trigger IMU recalibration on the target device."""
    return forward_message_to_target("recalibrate_imu")


# ----------------------------------------------------------------------------
# Vision-model APIs (small object detection / autonomous nav / AI ship / AI smoke)
# ----------------------------------------------------------------------------
# All four pages talk the same little dialect — list the .webp frames in a
# directory, validate one before loading it, serve its bytes, poll progress — so
# the shape is written once here and each workload just names its directories.
def list_webp(directory):
    """Readable, non-empty .webp files in `directory`, in display order.

    Sorted numerically when the names are plain numbers ("7.webp" before
    "10.webp", which is how the small-object frames are numbered) and
    alphabetically otherwise."""
    images = []
    if not os.path.isdir(directory):
        print(f"Image directory missing: {directory}")
        return images

    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".webp"):
            continue
        filepath = os.path.join(directory, filename)
        try:
            if not (os.path.isfile(filepath) and os.access(filepath, os.R_OK)):
                print(f"Warning: cannot read file {filepath}")
                continue
            size = os.path.getsize(filepath)
            if size <= 0:
                print(f"Warning: empty file {filepath}")
                continue
            images.append({"filename": filename, "size": size})
        except OSError as e:
            print(f"Error checking file {filepath}: {e}")

    stems = [img["filename"][:-len(".webp")] for img in images]
    if all(s.isdigit() for s in stems):
        images.sort(key=lambda img: int(img["filename"][:-len(".webp")]))
    return images


def pair_image_list(input_dir, output_dir):
    """The {input,output} listing the smoke/ship/small-object pages fetch once up
    front, so they can pre-load every frame into the browser cache before a run."""
    input_images = list_webp(input_dir)
    output_images = list_webp(output_dir)
    return jsonify({
        "input_images": [img["filename"] for img in input_images],
        "output_images": [img["filename"] for img in output_images],
        "input_images_info": [dict(img, type="input") for img in input_images],
        "output_images_info": [dict(img, type="output") for img in output_images],
        "total_input": len(input_images),
        "total_output": len(output_images),
    })


def validate_in(directories, image_type, filename):
    """Confirm one frame exists and is readable before the page points an <img> at
    it. `directories` maps the page's image-type name to a directory."""
    directory = directories.get(image_type)
    if directory is None:
        return jsonify({"valid": False, "error": "Invalid image type"}), 400

    # send_from_directory does this too, but do it here as well so a crafted
    # filename can never escape the directory even in this cheap probe.
    filepath = os.path.normpath(os.path.join(directory, filename))
    if not filepath.startswith(os.path.abspath(directory) + os.sep):
        return jsonify({"valid": False, "error": "Invalid filename"}), 400

    if os.path.isfile(filepath) and os.access(filepath, os.R_OK):
        return jsonify({
            "valid": True,
            "filename": filename,
            "size": os.path.getsize(filepath),
            "path": filepath,
        })
    return jsonify({"valid": False, "error": "File not accessible"}), 404


def ai_core_usage_for(workload):
    """Live AI-engine usage plus the baseline captured when this workload's Run
    was pressed — the page starts displaying frames once the gap exceeds ~5%."""
    return jsonify({
        "total_ai_usage": latest_ai_core_usage,
        "baseline": ai_baseline_usage.get(workload, 0.0),
    })


# ---- small object detection ----
@app.route('/small_obj_detect/image_list', methods=['GET'])
def small_obj_image_list():
    return pair_image_list(SMALL_OBJ_INPUT_DIR, SMALL_OBJ_OUTPUT_DIR)


@app.route('/small_obj_detect/validate_image/<image_type>/<filename>')
def small_obj_validate_image(image_type, filename):
    return validate_in({"input": SMALL_OBJ_INPUT_DIR, "output": SMALL_OBJ_OUTPUT_DIR}, image_type, filename)


@app.route('/small_obj_detect/images/input/<filename>')
def small_obj_input_image(filename):
    return send_from_directory(SMALL_OBJ_INPUT_DIR, filename)


@app.route('/small_obj_detect/images/output/<filename>')
def small_obj_output_image(filename):
    return send_from_directory(SMALL_OBJ_OUTPUT_DIR, filename)


@app.route('/small_obj_detect/progress', methods=['GET'])
def small_obj_progress():
    return jsonify(latest_progress["small_obj_detect"])


# ---- autonomous navigation ----
@app.route('/auto_nav/image_list', methods=['GET'])
def auto_nav_image_list():
    """Four independent streams (GPS / features / depth / LIDAR) rather than the
    input+output pair the other three pages use."""
    listing = {kind: list_webp(d) for kind, d in AUTO_NAV_DIRS.items()}
    payload = {}
    for kind, images in listing.items():
        payload[f"{kind}_images"] = [img["filename"] for img in images]
        payload[f"{kind}_images_info"] = images
        payload[f"total_{kind}"] = len(images)
    return jsonify(payload)


@app.route('/auto_nav/validate_image/<image_type>/<filename>')
def auto_nav_validate_image(image_type, filename):
    return validate_in(AUTO_NAV_VALIDATE_DIRS, image_type, filename)


@app.route('/auto_nav/images/<kind>/<filename>')
def auto_nav_image(kind, filename):
    directory = AUTO_NAV_DIRS.get(kind)
    if directory is None:
        return jsonify({"error": "Invalid image type"}), 404
    return send_from_directory(directory, filename)


@app.route('/auto_nav/progress', methods=['GET'])
def auto_nav_progress():
    return jsonify(latest_progress["auto_nav"])


# ---- AI ship ----
@app.route('/ai_ship/image_list', methods=['GET'])
def ai_ship_image_list():
    return pair_image_list(AI_SHIP_INPUT_DIR, AI_SHIP_OUTPUT_DIR)


@app.route('/ai_ship/validate_image/<image_type>/<filename>')
def ai_ship_validate_image(image_type, filename):
    return validate_in({"input": AI_SHIP_INPUT_DIR, "output": AI_SHIP_OUTPUT_DIR}, image_type, filename)


@app.route('/ai_ship/images/input/<filename>')
def ai_ship_input_image(filename):
    return send_from_directory(AI_SHIP_INPUT_DIR, filename)


@app.route('/ai_ship/images/output/<filename>')
def ai_ship_output_image(filename):
    return send_from_directory(AI_SHIP_OUTPUT_DIR, filename)


@app.route('/ai_ship/progress', methods=['GET'])
def ai_ship_progress():
    return jsonify(latest_progress["ai_ship"])


@app.route('/ai_ship/ai_core_usage', methods=['GET'])
def ai_ship_core_usage():
    return ai_core_usage_for("ai_ship")


# ---- AI smoke ----
@app.route('/ai_smoke/image_list', methods=['GET'])
def ai_smoke_image_list():
    return pair_image_list(AI_SMOKE_INPUT_DIR, AI_SMOKE_OUTPUT_DIR)


@app.route('/ai_smoke/validate_image/<image_type>/<filename>')
def ai_smoke_validate_image(image_type, filename):
    return validate_in({"input": AI_SMOKE_INPUT_DIR, "output": AI_SMOKE_OUTPUT_DIR}, image_type, filename)


@app.route('/ai_smoke/images/input/<filename>')
def ai_smoke_input_image(filename):
    return send_from_directory(AI_SMOKE_INPUT_DIR, filename)


@app.route('/ai_smoke/images/output/<filename>')
def ai_smoke_output_image(filename):
    return send_from_directory(AI_SMOKE_OUTPUT_DIR, filename)


@app.route('/ai_smoke/progress', methods=['GET'])
def ai_smoke_progress():
    return jsonify(latest_progress["ai_smoke"])


@app.route('/ai_smoke/ai_core_usage', methods=['GET'])
def ai_smoke_core_usage():
    return ai_core_usage_for("ai_smoke")


def run_flask_server():
    print(f"Running Flask server on {HOST_IP}:{FLASK_PORT} ...")
    app.run(host=HOST_IP, port=FLASK_PORT, debug=False, use_reloader=False)


# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    threading.Thread(target=receive_metrics, daemon=True).start()
    threading.Thread(target=receive_workload_updates, daemon=True).start()
    threading.Thread(target=run_flask_server, daemon=True).start()
    while True:
        time.sleep(1)
