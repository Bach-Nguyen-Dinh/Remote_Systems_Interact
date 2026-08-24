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

The app then serves the dashboard directly at '/':

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

SERVER
------
This is an ASGI app served by uvicorn. `python3 combined_topaz2.py` starts it the
same way it always did (same bind address, same port), and it can also be run under
an external supervisor with:

    uvicorn combined_topaz2:app --host 0.0.0.0 --port 5001

This replaces the Werkzeug development server, which hard-coded "Connection: close"
on every response (werkzeug/serving.py, "Always close the connection") and so forced
the reverse proxy in front of us to open a fresh TCP connection per request — an
extra round trip on every call over a long-haul link, unrecoverable from the nginx
side. uvicorn keeps connections alive, so that cost is gone.

Handlers are plain `def` (Starlette runs those in a worker thread, so their blocking
directory and file work behaves as it did under Flask's threaded server) except the
SSE endpoints, which are `async def`. That distinction matters here: an SSE stream
sits idle for many seconds at a time between updates, so a thread-backed generator
would hold a worker thread for the life of every open stream and a handful of open
dashboards would exhaust the pool. The progress fan-out below therefore hands updates
to asyncio queues on the event loop instead of blocking `queue.Queue.get()`.
"""

import os
import json
import time
import queue
import socket
import struct
import asyncio
import hashlib
import mimetypes
import threading
import subprocess
from contextlib import asynccontextmanager
from typing import Optional

import anyio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel

try:
    from influxdb import InfluxDBClient  # type: ignore
except Exception:                        # influxdb client is optional
    InfluxDBClient = None

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
HOST_IP = "0.0.0.0"          # uvicorn + metrics-socket bind address
SERVER_PORT = 5001
SYSINFO_PORT = 12346         # target -> host system-metrics stream (JSON per line)
DATA_PORT = 55556            # target -> host workload updates (one JSON object per connection)

TARGET_IP = "10.42.0.7"      # embedded Topaz target (for /send_message forwarding)
TARGET_PORT = 54322

# Worker threads available to the `def` request handlers. Starlette's default is
# 40; the image-bundle handlers walk large directories and stream hundreds of
# megabytes, so give the dashboard's frequent pollers headroom behind them.
REQUEST_THREAD_LIMIT = 100

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

AI_SMOKE_INPUT_DIR = os.path.join(CURR_DIR, "SmokeNet-Data/validation/opt_web_img")
AI_SMOKE_OUTPUT_DIR = os.path.join(CURR_DIR, "SmokeNet-Data/classification/opt_web_img")

AI_SHIP_INPUT_DIR = os.path.join(CURR_DIR, "modified_ai_ship/ship/short_example/opt_web_img")
AI_SHIP_OUTPUT_DIR = os.path.join(CURR_DIR, "modified_ai_ship/ship/output_segment/opt_web_img")

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
# Guarded by a lock because the receiver (writer) and request handler threads
# (readers) touch it concurrently.
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

# Live SSE listeners per workload: one asyncio.Queue per open '<workload>/events'
# request. The '<workload>/progress' cache above only ever holds the *latest*
# update, so a polling page silently loses any update overwritten between two polls
# (and re-renders the surviving one on every poll in between). Pushing each update
# into these queues instead delivers every update exactly once, in order.
#
# The queues are asyncio queues, not queue.Queue: the socket reader thread hands
# updates over with loop.call_soon_threadsafe (see _publish_progress) and each
# stream awaits its own queue on the event loop, so an open-but-idle stream costs
# no thread at all.
progress_subscribers = {key: set() for key in latest_progress}
progress_subscribers_lock = threading.Lock()

# The event loop uvicorn is running, captured at startup so the metrics/progress
# socket threads can hand work to it. None until lifespan runs.
main_loop: Optional[asyncio.AbstractEventLoop] = None

# Per-listener backlog. A run emits one update per frame, so this is many seconds
# of slack; a client that falls this far behind is not reading at all and gets
# dropped rather than growing the queue without bound.
PROGRESS_QUEUE_MAX = 1000

# How long a listener waits for an update before emitting an SSE comment. Keeps
# proxies from closing an idle stream and surfaces vanished clients (the write
# fails, the generator closes, the queue is unregistered).
PROGRESS_HEARTBEAT_SECONDS = 15

# Accept queue for the target's per-frame progress connections (see
# receive_workload_updates). The backlog is sized for the ~43 connections a
# second a small-object run makes, with the queue giving several seconds of
# slack on top.
WORKLOAD_CONN_BACKLOG = 128
WORKLOAD_CONN_QUEUE = 512
# The target writes one small JSON object and closes immediately, so a
# connection still unfinished after this is not going to finish. The old 5 s
# meant one such peer could hold the whole progress pipeline for 5 s.
WORKLOAD_READ_TIMEOUT = 1.0

# Emit-cadence log for the progress pipeline (see _note_progress_timing).
# Enable with TOPAZ_PROGRESS_TIMING=1 when investigating a stuttering run.
PROGRESS_TIMING = bool(os.environ.get("TOPAZ_PROGRESS_TIMING"))
_progress_timing = {}

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


# ----------------------------------------------------------------------------
# Application setup
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Capture the running loop (the socket threads publish onto it) and start the
    receivers. Doing it here rather than in __main__ means an external
    `uvicorn combined_topaz2:app` gets the metrics and progress receivers too."""
    global main_loop
    main_loop = asyncio.get_running_loop()
    anyio.to_thread.current_default_thread_limiter().total_tokens = REQUEST_THREAD_LIMIT
    threading.Thread(target=receive_metrics, daemon=True).start()
    threading.Thread(target=receive_workload_updates, daemon=True).start()
    yield


app = FastAPI(title="Combined Topaz2 dashboard", lifespan=lifespan)

# Matches the old flask_cors CORS(app) default: every origin, method and header
# allowed, credentials not allowed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Python's mimetypes table doesn't always know .webp, and without this every
# frame goes out as application/octet-stream.
mimetypes.add_type("image/webp", ".webp")

# How many frames one /image_bundle request may carry. The pages ask for chunks
# well under this; the cap is only here so a hand-made request can't make the
# server build a single multi-hundred-megabyte response.
BUNDLE_MAX_FILES = 500


class CommandMessage(BaseModel):
    """Body of POST /send_message."""
    message: str = ""


# ----------------------------------------------------------------------------
# Static-file helpers (stand-ins for Flask's send_file / send_from_directory)
# ----------------------------------------------------------------------------
def send_file(path, media_type=None):
    """Serve one known file, 404 if it has gone missing.

    Flask's send_file raised NotFound for a missing path; FileResponse would only
    fail when the body is already being written, so the check is explicit here."""
    if not os.path.isfile(path):
        return PlainTextResponse("File not found", status_code=404)
    return FileResponse(path, media_type=media_type)


def send_from_directory(directory, filename, media_type=None):
    """Serve `filename` from inside `directory`, refusing anything that resolves
    outside it (Flask's send_from_directory did the containment check for us)."""
    base = os.path.abspath(directory)
    full = os.path.abspath(os.path.join(base, filename))
    if os.path.commonpath([base, full]) != base:
        return PlainTextResponse("Not found", status_code=404)
    return send_file(full, media_type=media_type)


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


def _note_progress_timing(key, update):
    """Record how evenly THIS HOST pushed `key`'s updates out, summarised when the
    run ends.

    The page's [diag] block says when updates reached the browser; this says when
    the host let go of them. Comparing the two for the same run puts a stutter on
    one side of the link or the other instead of leaving it to guesswork — a run
    that is even here and ragged there is a delivery problem, and one that is
    ragged here is the target's. Off unless TOPAZ_PROGRESS_TIMING is set, since
    it costs a timestamp on every frame."""
    if not PROGRESS_TIMING:
        return
    msg_type = str(update.get("type", ""))
    now = time.perf_counter()
    state = _progress_timing.get(key)
    if msg_type.endswith("_start") or state is None:
        _progress_timing[key] = {"t0": now, "last": now, "gaps": []}
        return
    state["gaps"].append((now - state["last"]) * 1000.0)
    state["last"] = now
    if msg_type.endswith(("_complete", "_stopped", "_error")):
        _progress_timing.pop(key, None)
        gaps = sorted(state["gaps"])
        if not gaps:
            return
        at = lambda p: gaps[min(len(gaps) - 1, int(p * len(gaps)))]
        print(f"[timing] {key}: {len(gaps) + 1} updates in {now - state['t0']:.1f}s, "
              f"gap ms p50={at(0.5):.1f} p90={at(0.9):.1f} max={gaps[-1]:.1f}, "
              f">200ms={sum(1 for g in gaps if g > 200)}")


def _subscribe_progress(key):
    """Register a queue to receive every future update for `key`.

    Called from the event loop (the SSE handler), so the queue is bound to the
    loop that will await it."""
    q = asyncio.Queue(maxsize=PROGRESS_QUEUE_MAX)
    with progress_subscribers_lock:
        progress_subscribers[key].add(q)
    return q


def _unsubscribe_progress(key, q):
    with progress_subscribers_lock:
        progress_subscribers[key].discard(q)


def _deliver_progress(key, q, update):
    """Put one update on one listener's queue. Runs ON the event loop, handed over
    by _publish_progress, because asyncio.Queue is not thread-safe."""
    try:
        q.put_nowait(update)
    except asyncio.QueueFull:
        print(f"Dropping stalled {key} progress listener")
        _unsubscribe_progress(key, q)


def _publish_progress(key, update):
    """Hand `update` to every open listener on `key`.

    Never blocks the socket thread: the actual queue writes are scheduled onto the
    event loop, and a listener whose backlog is full is one that has stopped
    reading, so it is dropped from the fan-out and its stream ends on the next
    heartbeat."""
    loop = main_loop
    if loop is None:      # an update arrived before the server finished starting
        return
    with progress_subscribers_lock:
        listeners = list(progress_subscribers[key])
    for q in listeners:
        try:
            loop.call_soon_threadsafe(_deliver_progress, key, q, update)
        except RuntimeError:
            pass          # loop closed (shutting down)


def _read_workload_update(conn, addr):
    """Drain one progress connection and file the update it carries.

    Connection-per-message: the target dials in, writes one JSON object, and
    hangs up — so read until EOF and parse the whole payload rather than
    splitting on newlines."""
    try:
        with conn:
            conn.settimeout(WORKLOAD_READ_TIMEOUT)
            chunks = []
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks).decode(errors="replace").strip()
            if not raw:
                return
            update = json.loads(raw)
            key = _progress_key(update)
            if key is not None:
                latest_progress[key] = update
                _track_workload_pid(key, update)
                _note_progress_timing(key, update)
                _publish_progress(key, update)
    except json.JSONDecodeError:
        pass    # image bytes / non-JSON traffic on the same port — not ours
    except Exception as e:
        print(f"Workload-progress connection error ({addr}): {e}")


def receive_workload_updates():
    """Accept the target's workload-progress connections and cache each update.

    Accepting and reading in the same loop is what this deliberately avoids. A
    small-object run reports one update per frame, each on its own short-lived
    connection — about 43 a second — so a single peer that connects and then
    dawdles blocks the accept loop for the whole read timeout. Measured against
    the old inline loop: one idle peer held 300 frames back and then released
    them in a burst once its 5 s timeout expired, which is exactly the "frames
    arrive in clumps, so most are never seen" shape. The kernel's backlog
    absorbs the connections meanwhile, so they are late rather than refused, but
    late in a 5 s clump is what the eye reads as a stall.

    Accepting into a queue keeps the accept loop free whatever a peer does, and
    WORKLOAD_READ_TIMEOUT caps how long one bad connection can hold the reader.
    Draining from ONE reader keeps updates in the order the target sent them —
    frames going backwards would read as stutter just as badly as frames
    arriving late."""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST_IP, DATA_PORT))
    server_socket.listen(WORKLOAD_CONN_BACKLOG)
    print(f"Workload-progress server listening on {HOST_IP}:{DATA_PORT}")

    pending = queue.Queue(maxsize=WORKLOAD_CONN_QUEUE)

    def reader():
        while True:
            conn, addr = pending.get()
            _read_workload_update(conn, addr)

    threading.Thread(target=reader, daemon=True).start()

    while True:
        conn, addr = server_socket.accept()
        try:
            pending.put_nowait((conn, addr))
        except queue.Full:
            # Only reachable if the reader is genuinely wedged. Closing the new
            # connection keeps the accept loop honest rather than letting the
            # queue grow without bound.
            print("Workload-progress backlog full; dropping connection")
            conn.close()


def forward_message_to_target(message):
    """Send a control message to the embedded Topaz target over its command socket."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect((TARGET_IP, TARGET_PORT))
            s.sendall(message.encode())
        return JSONResponse({"status": "success", "message": message})
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=500)


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.get('/')
def combined_frontend():
    """Thin shell: two collapsible sections, each an <iframe> onto a standalone
    page below (Orientation + System monitoring), same-origin so both auto-size."""
    return send_file(COMBINED_FRONTEND)


@app.get('/orientation')
def orientation_frontend():
    """IMU orientation page (gyroscope, accelerometer, integrated angles)."""
    return send_file(ORIENTATION_FRONTEND)


@app.get('/monitor')
@app.get('/system_monitor')
def monitor_frontend():
    """Live system-utilisation page (CPU / AI cores / memory / disk / network)."""
    return send_file(MONITOR_FRONTEND)


@app.get('/small_obj_app')
def small_obj_frontend():
    """Small object detection page (Vision model tab)."""
    return send_file(SMALL_OBJ_FRONTEND)


@app.get('/auto_nav_app')
def auto_nav_frontend():
    """Autonomous navigation page (Vision model tab)."""
    return send_file(AUTO_NAV_FRONTEND)


@app.get('/ai_ship_app')
def ai_ship_frontend():
    """AI ship detection page (Vision model tab)."""
    return send_file(AI_SHIP_FRONTEND)


@app.get('/ai_smoke_app')
def ai_smoke_frontend():
    """AI smoke detection page (Vision model tab)."""
    return send_file(AI_SMOKE_FRONTEND)


@app.get('/system_metrics')
def system_metrics():
    """The whole live snapshot the two dashboard pages need, in one call. Served
    straight from the in-memory cache the metrics receiver fills."""
    with latest_metrics_lock:
        return JSONResponse(latest_metrics)


@app.post('/send_message')
def send_message(payload: Optional[CommandMessage] = None):
    """Forward a control command to the embedded target.

    Three families of command need host-side bookkeeping before (or instead of)
    being forwarded:

      * "run_<workload>[:<n ai cores>[:<n cpu cores>]]" — drop the previous
        run's progress so the page doesn't briefly show stale numbers, and for
        the AI-engine workloads snapshot current usage as the baseline their
        "has it actually started?" check measures against. The core counts are
        the target's business; everything after the workload name is forwarded
        untouched (the AI-ship and AI-smoke pages send both counts, the others
        just the AI count).
      * "stop_<workload>" — append the PID the target reported for the run in
        flight, so it kills that run and not a newer one.
      * "clear_<workload>" — purely host-side; nothing to tell the target."""
    message = payload.message if payload else ""
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
            return JSONResponse({"status": "cleared", "workload": workload})

    return forward_message_to_target(message)


@app.post('/imu/recalibrate')
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
    return JSONResponse({
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
        return JSONResponse({"valid": False, "error": "Invalid image type"}, status_code=400)

    # send_from_directory does this too, but do it here as well so a crafted
    # filename can never escape the directory even in this cheap probe.
    filepath = os.path.normpath(os.path.join(directory, filename))
    if not filepath.startswith(os.path.abspath(directory) + os.sep):
        return JSONResponse({"valid": False, "error": "Invalid filename"}, status_code=400)

    if os.path.isfile(filepath) and os.access(filepath, os.R_OK):
        return JSONResponse({
            "valid": True,
            "filename": filename,
            "size": os.path.getsize(filepath),
            "path": filepath,
        })
    return JSONResponse({"valid": False, "error": "File not accessible"}, status_code=404)


def bundle_in(directories, image_type, request):
    """Return many frames in a single response.

    Fetching frames one <img> at a time costs one round trip each, which is
    invisible next to the LAN but dominates everything for a browser on another
    continent (~1 s per request was measured from the US west coast). These
    image sets run to ~1500 frames, so that alone is ~25 minutes of waiting for
    a few megabytes. One request per few-hundred frames removes essentially all
    of it.

    Wire format, so the client can cut the blob back up without a second pass:

        [4-byte big-endian header length][header JSON, utf-8][frame bytes, concatenated]
        header = {"files": [{"name": "a.webp", "size": 1234}, ...]}

    Sizes come from stat() and the body is padded/truncated to match, so the
    framing can never desync even if a file changes underneath us. A frame that
    has gone missing since the listing is reported with size 0 and contributes
    no bytes — the client skips it instead of spending a round trip finding out.

    Query parameters `start` and `count` select a slice of the same (sorted)
    listing /image_list returns, so the page can stream several chunks in
    parallel and show real progress.
    """
    directory = directories.get(image_type)
    if directory is None:
        return JSONResponse({"error": "Invalid image type"}, status_code=400)

    try:
        start = max(0, int(request.query_params.get("start", 0)))
        count = int(request.query_params.get("count", BUNDLE_MAX_FILES))
    except ValueError:
        return JSONResponse({"error": "start and count must be integers"}, status_code=400)
    count = max(0, min(count, BUNDLE_MAX_FILES))

    names = [img["filename"] for img in list_webp(directory)][start:start + count]

    entries = []          # (absolute path or None, declared size)
    header_files = []
    fingerprint = hashlib.sha1()
    for name in names:
        path = os.path.join(directory, name)
        try:
            stat = os.stat(path)
            size, mtime = stat.st_size, stat.st_mtime
        except OSError:
            path, size, mtime = None, 0, 0
        entries.append((path, size))
        header_files.append({"name": name, "size": size})
        fingerprint.update(f"{name}:{size}:{mtime};".encode("utf-8"))

    # Revalidate rather than trust a TTL: these directories get swapped for a new
    # dataset from time to time, and a stale frame is far more confusing than one
    # extra round trip per chunk (a few dozen, against the ~1500 this saves).
    etag = '"' + fingerprint.hexdigest() + '"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

    header = json.dumps({"files": header_files}).encode("utf-8")
    prefix = struct.pack(">I", len(header)) + header
    total = len(prefix) + sum(size for _, size in entries)

    def generate():
        yield prefix
        for path, size in entries:
            if size == 0:
                continue
            sent = 0
            try:
                with open(path, "rb") as fh:
                    while sent < size:
                        chunk = fh.read(min(65536, size - sent))
                        if not chunk:
                            break
                        sent += len(chunk)
                        yield chunk
            except OSError as e:
                print(f"Error reading {path} for bundle: {e}")
            if sent < size:
                # File shrank or could not be read — keep the declared length so
                # every later offset in the header stays correct.
                yield b"\0" * (size - sent)

    # The generator is synchronous, so Starlette pulls it on a worker thread: each
    # 64 KB read borrows a thread only for the read itself, never for the life of
    # the response.
    return StreamingResponse(
        generate(),
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(total),
            "ETag": etag,
            "Cache-Control": "no-cache",
        },
    )


async def progress_event_stream(workload):
    """Push `workload`'s progress updates to the page as Server-Sent Events.

    The push counterpart to '<workload>/progress'. That endpoint serves a
    last-value cache, so a page polling it samples the update stream: updates
    arriving closer together than the poll interval are overwritten unseen, and
    a poll landing between two updates re-delivers one already handled. Here the
    receiver thread hands us every update exactly once, in order.

    The connection opens with the cached update (if any) so a page that loads or
    reconnects mid-run shows current state instead of waiting for the next frame.
    Subscribing *before* reading that cache means an update landing right now is
    delivered twice rather than lost — displaying one frame twice is harmless,
    missing one is not.

    Async on purpose: a stream spends nearly all its life waiting, so awaiting the
    queue costs nothing while it idles. A blocking `queue.Queue.get()` in a sync
    generator would instead hold one of the server's worker threads for as long as
    the page stays open."""
    q = _subscribe_progress(workload)
    snapshot = latest_progress[workload]

    async def generate():
        try:
            if snapshot:
                # Tagged so the page can tell replayed state from a live update.
                # It matters because a page subscribes BEFORE sending its run
                # command (otherwise the frames produced while that command is in
                # flight are lost), and at that moment this cache still describes
                # the PREVIOUS run — an untagged "…_complete" would end the run
                # the page is only just starting.
                yield f"data: {json.dumps(dict(snapshot, replay=True))}\n\n"
            while True:
                try:
                    update = await asyncio.wait_for(q.get(), PROGRESS_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(update)}\n\n"
        finally:
            # Runs on client disconnect too: the server closes the generator,
            # which raises GeneratorExit at the yield above.
            _unsubscribe_progress(workload, q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx buffers proxied responses by default, which would hold events
            # back until the buffer fills. Asking for it here keeps the /topaz/
            # prefix block in set_up_proxy_server.md unchanged.
            "X-Accel-Buffering": "no",
        },
    )


def ai_core_usage_for(workload):
    """Live AI-engine usage plus the baseline captured when this workload's Run
    was pressed — the page starts displaying frames once the gap exceeds ~5%."""
    return JSONResponse({
        "total_ai_usage": latest_ai_core_usage,
        "baseline": ai_baseline_usage.get(workload, 0.0),
    })


# ---- small object detection ----
@app.get('/small_obj_detect/image_list')
def small_obj_image_list():
    return pair_image_list(SMALL_OBJ_INPUT_DIR, SMALL_OBJ_OUTPUT_DIR)


@app.get('/small_obj_detect/validate_image/{image_type}/{filename}')
def small_obj_validate_image(image_type: str, filename: str):
    return validate_in({"input": SMALL_OBJ_INPUT_DIR, "output": SMALL_OBJ_OUTPUT_DIR}, image_type, filename)


@app.get('/small_obj_detect/image_bundle/{image_type}')
def small_obj_image_bundle(image_type: str, request: Request):
    return bundle_in({"input": SMALL_OBJ_INPUT_DIR, "output": SMALL_OBJ_OUTPUT_DIR}, image_type, request)


@app.get('/small_obj_detect/images/input/{filename}')
def small_obj_input_image(filename: str):
    return send_from_directory(SMALL_OBJ_INPUT_DIR, filename)


@app.get('/small_obj_detect/images/output/{filename}')
def small_obj_output_image(filename: str):
    return send_from_directory(SMALL_OBJ_OUTPUT_DIR, filename)


@app.get('/small_obj_detect/progress')
def small_obj_progress():
    return JSONResponse(latest_progress["small_obj_detect"])


@app.get('/small_obj_detect/events')
async def small_obj_events():
    return await progress_event_stream("small_obj_detect")


# ---- autonomous navigation ----
@app.get('/auto_nav/image_list')
def auto_nav_image_list():
    """Four independent streams (GPS / features / depth / LIDAR) rather than the
    input+output pair the other three pages use."""
    listing = {kind: list_webp(d) for kind, d in AUTO_NAV_DIRS.items()}
    payload = {}
    for kind, images in listing.items():
        payload[f"{kind}_images"] = [img["filename"] for img in images]
        payload[f"{kind}_images_info"] = images
        payload[f"total_{kind}"] = len(images)
    return JSONResponse(payload)


@app.get('/auto_nav/validate_image/{image_type}/{filename}')
def auto_nav_validate_image(image_type: str, filename: str):
    return validate_in(AUTO_NAV_VALIDATE_DIRS, image_type, filename)


@app.get('/auto_nav/image_bundle/{image_type}')
def auto_nav_image_bundle(image_type: str, request: Request):
    return bundle_in(AUTO_NAV_VALIDATE_DIRS, image_type, request)


@app.get('/auto_nav/images/{kind}/{filename}')
def auto_nav_image(kind: str, filename: str):
    directory = AUTO_NAV_DIRS.get(kind)
    if directory is None:
        return JSONResponse({"error": "Invalid image type"}, status_code=404)
    return send_from_directory(directory, filename)


@app.get('/auto_nav/progress')
def auto_nav_progress():
    return JSONResponse(latest_progress["auto_nav"])


# ---- AI ship ----
@app.get('/ai_ship/image_list')
def ai_ship_image_list():
    return pair_image_list(AI_SHIP_INPUT_DIR, AI_SHIP_OUTPUT_DIR)


@app.get('/ai_ship/validate_image/{image_type}/{filename}')
def ai_ship_validate_image(image_type: str, filename: str):
    return validate_in({"input": AI_SHIP_INPUT_DIR, "output": AI_SHIP_OUTPUT_DIR}, image_type, filename)


@app.get('/ai_ship/image_bundle/{image_type}')
def ai_ship_image_bundle(image_type: str, request: Request):
    return bundle_in({"input": AI_SHIP_INPUT_DIR, "output": AI_SHIP_OUTPUT_DIR}, image_type, request)


@app.get('/ai_ship/images/input/{filename}')
def ai_ship_input_image(filename: str):
    return send_from_directory(AI_SHIP_INPUT_DIR, filename)


@app.get('/ai_ship/images/output/{filename}')
def ai_ship_output_image(filename: str):
    return send_from_directory(AI_SHIP_OUTPUT_DIR, filename)


@app.get('/ai_ship/progress')
def ai_ship_progress():
    return JSONResponse(latest_progress["ai_ship"])


@app.get('/ai_ship/events')
async def ai_ship_events():
    return await progress_event_stream("ai_ship")


@app.get('/ai_ship/ai_core_usage')
def ai_ship_core_usage():
    return ai_core_usage_for("ai_ship")


# ---- AI smoke ----
@app.get('/ai_smoke/image_list')
def ai_smoke_image_list():
    return pair_image_list(AI_SMOKE_INPUT_DIR, AI_SMOKE_OUTPUT_DIR)


@app.get('/ai_smoke/validate_image/{image_type}/{filename}')
def ai_smoke_validate_image(image_type: str, filename: str):
    return validate_in({"input": AI_SMOKE_INPUT_DIR, "output": AI_SMOKE_OUTPUT_DIR}, image_type, filename)


@app.get('/ai_smoke/image_bundle/{image_type}')
def ai_smoke_image_bundle(image_type: str, request: Request):
    return bundle_in({"input": AI_SMOKE_INPUT_DIR, "output": AI_SMOKE_OUTPUT_DIR}, image_type, request)


@app.get('/ai_smoke/images/input/{filename}')
def ai_smoke_input_image(filename: str):
    return send_from_directory(AI_SMOKE_INPUT_DIR, filename)


@app.get('/ai_smoke/images/output/{filename}')
def ai_smoke_output_image(filename: str):
    return send_from_directory(AI_SMOKE_OUTPUT_DIR, filename)


@app.get('/ai_smoke/progress')
def ai_smoke_progress():
    return JSONResponse(latest_progress["ai_smoke"])


@app.get('/ai_smoke/events')
async def ai_smoke_events():
    return await progress_event_stream("ai_smoke")


@app.get('/ai_smoke/ai_core_usage')
def ai_smoke_core_usage():
    return ai_core_usage_for("ai_smoke")


# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Running uvicorn server on {HOST_IP}:{SERVER_PORT} ...")
    # The metrics / workload-progress receivers start from `lifespan` above, so
    # they come up whether the app is launched from here or by an external
    # `uvicorn combined_topaz2:app`.
    uvicorn.run(app, host=HOST_IP, port=SERVER_PORT, log_level="info")
