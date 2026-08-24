"""
combined_hpc.py — single-box HPC monitoring program.

Merges the former host (host_no_chunking.py) and target (target_hpc_ubuntu.py)
programs into one process that runs entirely on the target machine, alongside
InfluxDB and Grafana. Because collector and backend now share one process:

  * system metrics are written straight to InfluxDB in-process (no metrics socket)
  * the FastAPI control API calls the SAR / CPHD / network handlers directly
    (no :54321 command socket, no image / sar-log / sar-ctrl / net-test sockets)
  * SAR output tiffs and log plots are optimized straight into the served
    directories (pictures/, sar_logs/) instead of being streamed to a host

Network (iperf3) tests: only the target<->RDB / adapter paths are kept. The old
onboard host<->target tests were pure loopback on a single box and are disabled.

SERVER
------
This is an ASGI app served by uvicorn. `python3 combined_hpc.py` still starts it
the same way it always did (same bind address, same port), and it can also be run
under an external supervisor with:

    uvicorn combined_hpc:app --host 0.0.0.0 --port 5000

Unlike the old Werkzeug development server, uvicorn keeps connections alive, so
the nginx reverse proxy in front of us reuses one TCP connection across requests
instead of paying a fresh handshake per call.

Request handlers are deliberately plain `def` (not `async def`) except where a
handler genuinely needs the event loop (`/upload_cphd`, which streams its request
body). Starlette runs `def` handlers in a worker thread, so the blocking psutil /
subprocess / file work inside them behaves exactly as it did under Flask's
threaded server and can never stall the event loop.
"""

from PIL import Image
Image.MAX_IMAGE_PIXELS = None  # SAR TIFF files exceed PIL's default decompression bomb limit
from influxdb import InfluxDBClient
import socket
import subprocess
import threading
import psutil
import time
import json
import os
import re
import glob
import shutil
import signal
import unicodedata
import requests
from contextlib import asynccontextmanager
from typing import Optional

import anyio
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
HOST_IP = "0.0.0.0"          # uvicorn bind address
SERVER_PORT = 5000

# Worker threads available to the `def` request handlers. Starlette's default is
# 40; the SAR/CPHD handlers can block for a while on subprocess and disk work, so
# give the dashboard's frequent pollers plenty of headroom behind them.
REQUEST_THREAD_LIMIT = 100

INFLUXDB_HOST = "localhost"
INFLUXDB_PORT = 8086
INFLUXDB_DB = "system_metrics"
INFLUXDB_USER = "root"
INFLUXDB_PASSWORD = "root"

METRICS_INTERVAL = 0.3       # seconds between metric samples (was the target send loop cadence)

# SAR / target-side paths (unchanged from target_hpc_ubuntu.py)
RESIZED_IMAGE_PATH = "/home/sarthak/demo-resrc/optimized_image.webp"
DEMO_PATH = "/home/public/sar/sar-server/data/cphd"
# User-uploaded CPHD files land in this subfolder of DEMO_PATH. It is scanned like
# any other CPHD (scan_cphd_files walks DEMO_PATH), and cphd_aic.py resolves the
# relative name "user_data/<file>.cphd" against its own CPHD_DIR, so no change to
# cphd_aic.py is needed. Keeping uploads under one subfolder means user data can be
# wiped independently of the bundled demo files. NOTE: DEMO_PATH is root-owned, so
# this folder must be pre-created and made writable by the process user, e.g.:
#   sudo mkdir -p /home/public/sar/sar-server/data/cphd/user_data
#   sudo chown $(whoami) /home/public/sar/sar-server/data/cphd/user_data
USER_DATA_DIRNAME = "user_data"
USER_DATA_PATH = os.path.join(DEMO_PATH, USER_DATA_DIRNAME)
# Refuse an upload that would leave the filesystem with less than this much free
# space, so a large CPHD can't fill the disk out from under SAR processing.
UPLOAD_FREE_SPACE_MARGIN = 2 * 1024 * 1024 * 1024  # 2 GB headroom
# How much of an upload to accumulate before touching the disk. uvicorn hands the
# request body over in far smaller pieces than the 8 MB the old Werkzeug-side
# `request.stream.read(...)` returned, and the free-space check below runs once per
# written block — batching back up to 8 MB keeps both the write pattern and the
# check cadence exactly as they were.
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
OUT_TIF_PATH = "/home/sarthak/workspace/SAR_codebase/output_immediate"
SAR_DIR = "/home/sarthak/workspace/SAR_codebase"
SAR_PROG = os.path.join(SAR_DIR, "cphd_aic.py")
SAR_LOGS = os.path.join(SAR_DIR, "logs")            # where the profiler writes log folders
FAN_STATUS = "/home/sarthak/Remote_Systems_Interact/check_fan_status.sh"
PROFILER_OPTION = ["--metrics_interval_ms", "500", "--csv_write_interval_s", "5",
                   "--histograms", "--cores-per-row", "16", "--bin_width", "10", "--split_saturated",
                   "--"]

# Network test peers (two-machine paths that are still meaningful on one box)
RDB_IP = "10.42.1.7"
LW_ETH_ADT_CLIENT_IP = RDB_IP
UP_ETH_ADT_CLIENT_IP = "10.42.0.1"

# Interface IDs (for BW/ethtool + per-interface metric collection)
LW_ETH_OB_INTERFACE_ID = "enp6s0"
UP_ETH_OB_INTERFACE_ID = "enp5s0"
LW_ETH_ADT_INTERFACE_ID = "enp4s0f0"
UP_ETH_ADT_INTERFACE_ID = "enp4s0f1"
WIRELESS_INTERFACE_ID = "wlp3s0"
FM_INTERFACE_ID = "fm1-mac3"

# VLM box (AICraft Gemma3/SigLIP pipeline, demo1/backend/main.py) -- reachable
# over the same Ethernet link as the RDB/adapter test peers above. Change
# VLM_HOST here if that box's address on the link ever changes; nothing else
# in this file needs to change.
VLM_HOST = "192.168.0.11"
VLM_PORT = 8001
VLM_BASE_URL = f"http://{VLM_HOST}:{VLM_PORT}"
VLM_UPLOAD_TIMEOUT = 30      # seconds -- generous for a large SAR tiff over Ethernet
VLM_RESET_TIMEOUT = 5

# Topaz edge device -- the box behind nginx's /topaz/ prefix. Only the health
# probe below talks to it from here; the dashboard itself reaches it through
# nginx, never through this process.
TOPAZ_HOST = "192.168.0.32"
TOPAZ_PORT = 5001
TOPAZ_BASE_URL = f"http://{TOPAZ_HOST}:{TOPAZ_PORT}"

# ---- remote device health --------------------------------------------------
# Two of the dashboard's panels stream from OTHER boxes and reach the browser
# only through nginx (/vlm/ -> the VLM box, /topaz/ -> the Topaz box). When one
# of those boxes is off, its <iframe> would show nginx's raw 502 page, so the
# shell instead asks us whether each box is answering and paints its own
# "disconnected" placeholder.
#
# The probe lives here rather than in the browser for three reasons: one probe
# serves every open dashboard instead of one per tab; the timeout is ours to set
# rather than nginx's 60s default; and this process sits on the same box nginx
# proxies from, so what we can reach is exactly what nginx can reach.
#
# Each device's probe URL is picked to be cheap and to prove the *app* is alive,
# not merely that the port is open:
#   * Topaz -> /system_metrics, ~1.5 KB of JSON the app has to assemble.
#   * VLM   -> /health. If that box turns out not to serve it, no change is
#     needed: a 404 still means an HTTP server answered, and classify_probe()
#     below reads that as up. Only a refused/timed-out connection, a 5xx or an
#     auth rejection count as down.
DEVICE_HEALTH_INTERVAL = 5.0     # seconds between rounds of probes
DEVICE_HEALTH_TIMEOUT = 3.0      # per probe (connect + read)
# Consecutive failures before a device is called down. Two rounds (~10s) is the
# whole safety margin against a false positive: the shell blanks a device's
# frame when it goes down, so a single dropped packet must not be enough to tear
# down a panel somebody is using.
DEVICE_HEALTH_FAIL_STREAK = 2
DEVICES = {
    "vlm":   {"label": "Vision language model", "url": f"{VLM_BASE_URL}/health"},
    "topaz": {"label": "Edge device",           "url": f"{TOPAZ_BASE_URL}/system_metrics"},
}

# Host-side served directories / files (unchanged from host_no_chunking.py)
CURR_DIR = os.path.dirname(os.path.abspath(__file__))
# Which SAR frontend to serve. To go back to the older no-upload index, comment the
# "_upload" line and uncomment the "_combined" line (then restart).
SAR_FRONTEND = os.path.join(CURR_DIR, "index", "hpc", "sar_process_combined.html")   # old index (no upload)
# SAR_FRONTEND = os.path.join(CURR_DIR, "index", "hpc", "sar_process_upload.html")        # new index (upload + delete)
SAR_FRONTEND_RSAT = os.path.join(CURR_DIR, "index", "hpc", "sar_process_combined.html")
CCTV_FRONTEND = os.path.join(CURR_DIR, "index", "hpc", "cctv_process.html")
MONITOR_FRONTEND = os.path.join(CURR_DIR, "index", "hpc", "system_monitor.html")         # live system-utilisation page
# Post-run profiler plots (CPU / power / thermal / memory) of the latest SAR run,
# read out of SAR_LOGS_DIR. Its own standalone page so the combined shell can host
# it as one more collapsible row, exactly like the SAR and monitor pages.
PERFORMANCE_FRONTEND = os.path.join(CURR_DIR, "index", "hpc", "performance_analysis.html")
# Combined shell served at '/': two collapsible sections, each an <iframe> onto
# one of the pages above (SAR_FRONTEND via /sar_app, MONITOR_FRONTEND via
# /system_monitor). Replaces the old Grafana frontend that iframed them separately.
COMBINED_FRONTEND = os.path.join(CURR_DIR, "index", "hpc", "combined_dashboard.html")
COMBINED_FRONTEND_RSAT = os.path.join(CURR_DIR, "index", "hpc", "combined_dashboard_rsat.html")
# COMBINED_FRONTEND_RSAT = os.path.join(CURR_DIR, "index", "hpc", "combined_dashboard_rsat_no_mission_panel.html")
COMBINED_FRONTEND_TESTING = os.path.join(CURR_DIR, "index", "hpc", "combined_dashboard_testing.html")
BRANDING_DIR = os.path.join(CURR_DIR, "index", "branding")  # logo / mission-banner panels iframed by the RSAT header
SAVE_DIR = os.path.join(CURR_DIR, "pictures")
SAR_LOGS_DIR = os.path.join(CURR_DIR, "sar_logs")           # served SAR log plots (webp)
SAVE_PATH_TIF = os.path.join(SAVE_DIR, "tif_image.webp")
SAR_COLORED_IMAGE_PATH = os.path.join(CURR_DIR, "sar_colored_images")
SAVE_PATH_IPERF_LW_ETH_ADT = os.path.join(CURR_DIR, "iperf3_end_result_LwEthAdt.json")
SAVE_PATH_IPERF_UP_ETH_ADT = os.path.join(CURR_DIR, "iperf3_end_result_UpEthAdt.json")

for _d in (SAVE_DIR, SAR_LOGS_DIR):
    if not os.path.exists(_d):
        os.makedirs(_d)
        print(f"Created directory: {_d}")

# ----------------------------------------------------------------------------
# Global state
# ----------------------------------------------------------------------------
progress_update = 0.0
cphd_files = {}                # filename -> full path (populated by scan_cphd_files)
cphd_file_list = []            # list of filenames (served to the frontend)
cphd_file_properties = {}      # SIZE:/metadata result (served to the frontend)
tif_file_properties = {}       # processed-image properties (served to the frontend)
current_run_cphd = None        # CPHD filename currently being / last processed
sar_run_pid = None             # PID of the running SAR program (None when idle)
sar_log_version = 0            # bumped whenever a new SAR log folder is collected
sar_log_cleared = False        # True between a Reset and the next completed run
bwValue = 0
sar_proc_time = 0
ai_card_power_cache = None
ai_card_temp_cache = None
sar_process = None             # Popen handle of the running SAR program
sar_stop_requested = False
sar_error = None               # set to a user-facing message when a run fails (e.g. card unresponsive)

# Latest system snapshot, refreshed in-place by metrics_loop so the monitoring
# frontend can pull every live value in a single /system_metrics request instead
# of querying InfluxDB / Grafana. Guarded by a lock because metrics_loop (writer)
# and request handler threads (readers) touch it concurrently.
latest_metrics = {}
latest_metrics_lock = threading.Lock()

# Per-device health verdict served by /device_health, refreshed by
# device_health_loop. Seeded as "unknown" rather than "down" so a dashboard
# opened in the first few seconds after a restart shows "Connecting..." instead
# of accusing a perfectly healthy box of being offline.
device_health = {
    name: {"status": "unknown", "detail": "not probed yet", "label": cfg["label"],
           "since": None, "checked": None}
    for name, cfg in DEVICES.items()
}
device_health_lock = threading.Lock()


# ----------------------------------------------------------------------------
# Application setup
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start the background workers once the server is up, stop nothing on the way
    down — they are daemon threads with no cleanup of their own, exactly as when
    __main__ started them. Doing it here means an external `uvicorn combined_hpc:app`
    gets the collector and iperf3 server too, not just `python3 combined_hpc.py`."""
    anyio.to_thread.current_default_thread_limiter().total_tokens = REQUEST_THREAD_LIMIT
    scan_cphd_files()  # populate the CPHD list at boot
    threading.Thread(target=run_iperf3_server, daemon=True).start()
    threading.Thread(target=poll_ai_card_diagnostics, daemon=True).start()
    threading.Thread(target=metrics_loop, daemon=True).start()
    threading.Thread(target=device_health_loop, daemon=True).start()
    yield


app = FastAPI(title="Combined HPC dashboard", lifespan=lifespan)

# Matches the old flask_cors CORS(app) default: every origin, method and header
# allowed, credentials not allowed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class CommandMessage(BaseModel):
    """Body of POST /send_message."""
    message: str = "Default message"


class DeleteUploadRequest(BaseModel):
    """Body of POST /delete_upload."""
    filename: str = ""


# ----------------------------------------------------------------------------
# Static-file helpers (stand-ins for Flask's send_file / send_from_directory)
# ----------------------------------------------------------------------------
_FILENAME_STRIP_RE = re.compile(r"[^A-Za-z0-9_.-]")


def secure_filename(filename):
    """Reduce a client-supplied name to a safe bare filename.

    Local stand-in for werkzeug.utils.secure_filename, which left with Flask:
    fold to ASCII, flatten path separators and whitespace to underscores, drop
    anything outside [A-Za-z0-9_.-], and trim leading/trailing dots so the result
    can never be a path, a traversal, or a dotfile."""
    filename = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    for sep in (os.sep, os.path.altsep):
        if sep:
            filename = filename.replace(sep, " ")
    filename = "_".join(filename.split())
    return _FILENAME_STRIP_RE.sub("", filename).strip("._")


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
# Image / SAR helpers (from target)
# ----------------------------------------------------------------------------
def optimize_tif(image_path, output_path, format="webp", max_size=(800, 800), quality=85):
    """Optimize and convert an image (typically a large SAR TIFF) for web display."""
    try:
        img = Image.open(image_path)
        if img.mode in ("P", "CMYK", "RGBA"):
            img = img.convert("RGB")
        resample = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
        img.thumbnail(max_size, resample)
        ext = format.lower()
        if ext not in ["jpeg", "jpg", "png", "webp", "avif"]:
            raise ValueError("Unsupported format. Use jpeg, png, webp, or avif.")
        img.save(output_path, format=ext.upper(), quality=quality, optimize=True)
        print(f"Optimized image saved at {output_path}, size: {os.path.getsize(output_path)} bytes")
    except Exception as e:
        print(f"Error optimizing image: {e}")

def find_new_output_tif(known_tifs):
    """Return the newest tiff in OUT_TIF_PATH that appeared or changed since the run started."""
    candidates = []
    for path in glob.glob(os.path.join(OUT_TIF_PATH, "*.tiff")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if path not in known_tifs or mtime > known_tifs[path]:
            candidates.append((mtime, path))
    if not candidates:
        return None
    return max(candidates)[1]

def publish_tif_image(tif_path):
    """Merged send_image()+host image receiver: optimize the SAR tiff straight into the
    served pictures/ dir (previously streamed over a socket to the host)."""
    optimize_tif(tif_path, SAVE_PATH_TIF, format="webp", max_size=(800, 800), quality=80)
    if os.path.exists(SAVE_PATH_TIF):
        print(f"Published SAR image to {SAVE_PATH_TIF}")

def push_tif_to_vlm(tif_path):
    """Push the finished, full-resolution SAR tiff to the VLM box's Chat-with-SAR
    tab (POST /sar/upload -- see demo1/backend/main.py). Best-effort: the VLM box
    being unreachable/down must never break local SAR processing, so failures are
    logged and swallowed rather than raised."""
    try:
        with open(tif_path, "rb") as f:
            resp = requests.post(
                f"{VLM_BASE_URL}/sar/upload",
                files={"image": (os.path.basename(tif_path), f, "image/tiff")},
                timeout=VLM_UPLOAD_TIMEOUT,
            )
        if resp.ok:
            print(f"Pushed SAR tiff to VLM box: {os.path.basename(tif_path)}")
        else:
            print(f"VLM box rejected SAR tiff push: {resp.status_code} {resp.text}")
    except requests.exceptions.RequestException as e:
        print(f"Could not reach VLM box to push SAR tiff: {e}")

def notify_vlm_reset():
    """Tell the VLM box that Reset was pressed here, so it drops the current SAR
    image and ends any SAR chat session (POST /sar/reset). Best-effort, same as
    push_tif_to_vlm -- must not block or fail the local reset."""
    try:
        resp = requests.post(f"{VLM_BASE_URL}/sar/reset", timeout=VLM_RESET_TIMEOUT)
        if resp.ok:
            print("Notified VLM box of reset")
        else:
            print(f"VLM box rejected reset notification: {resp.status_code} {resp.text}")
    except requests.exceptions.RequestException as e:
        print(f"Could not reach VLM box to notify reset: {e}")

def collect_sar_logs():
    """Merged send_sar_logs()+host sar-log receiver: optimize the latest SAR log folder's
    PNG plots to webp into the served SAR_LOGS_DIR and bump the version counter."""
    global sar_log_version, sar_log_cleared
    if not os.path.exists(SAR_LOGS):
        print("SAR_LOGS directory not found, skipping log collection")
        return
    log_dirs = sorted(d for d in os.listdir(SAR_LOGS) if os.path.isdir(os.path.join(SAR_LOGS, d)))
    if not log_dirs:
        print("No SAR log folders found")
        return
    latest = log_dirs[-1]
    latest_path = os.path.join(SAR_LOGS, latest)
    dest_dir = os.path.join(SAR_LOGS_DIR, latest)
    os.makedirs(dest_dir, exist_ok=True)
    produced = False
    for fname in sorted(os.listdir(latest_path)):
        src_path = os.path.join(latest_path, fname)
        if not os.path.isfile(src_path):
            continue
        if fname.lower().endswith('.png'):
            webp_name = os.path.splitext(fname)[0] + '.webp'
            optimize_tif(src_path, os.path.join(dest_dir, webp_name),
                         format='webp', max_size=(800, 800), quality=85)
            produced = True
        elif fname.lower().endswith('.csv'):
            try:
                with open(src_path, 'rb') as fsrc, open(os.path.join(dest_dir, fname), 'wb') as fdst:
                    fdst.write(fsrc.read())
                produced = True
            except Exception as e:
                print(f"Error copying SAR log csv {fname}: {e}")
    # Histogram plots (--histograms) land in their own subfolder of the run dir,
    # under fixed filenames rather than a timestamp suffix, so they get their own
    # (smaller) type map and endpoint below instead of reusing SAR_LOG_IMAGE_TYPES'
    # prefix matching. They are much higher-resolution source PNGs (the stacked
    # chart especially, one axis per row of cores) than the six standard plots, so
    # they get a larger max_size to stay legible.
    hist_src_dir = os.path.join(latest_path, "histogram")
    if os.path.isdir(hist_src_dir):
        hist_dest_dir = os.path.join(dest_dir, "histogram")
        os.makedirs(hist_dest_dir, exist_ok=True)
        for fname in sorted(os.listdir(hist_src_dir)):
            src_path = os.path.join(hist_src_dir, fname)
            if not os.path.isfile(src_path) or not fname.lower().endswith('.png'):
                continue
            webp_name = os.path.splitext(fname)[0] + '.webp'
            optimize_tif(src_path, os.path.join(hist_dest_dir, webp_name),
                         format='webp', max_size=(1600, 1600), quality=85)
            produced = True
    if produced:
        sar_log_cleared = False   # this run's plots supersede whatever Reset hid
        sar_log_version += 1
        print(f"Collected SAR log folder '{latest}' into {dest_dir} (version {sar_log_version})")

def stop_sar_process(message):
    """Send SIGINT (Ctrl+C) to the running SAR process group, if any."""
    global sar_stop_requested
    process = sar_process
    if process is None or process.poll() is not None:
        print("No SAR process running, nothing to stop")
        return
    parts = message.split(":", 1)
    if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) != process.pid:
        print(f"STOPSAR pid {parts[1]} does not match running SAR pid {process.pid}, ignoring")
        return
    sar_stop_requested = True
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGINT)
        print(f"Sent SIGINT (Ctrl+C) to SAR process group of PID {process.pid}")
    except ProcessLookupError:
        print("SAR process already exited")

def report_sar_failure(return_code):
    """Record that a SAR run ended without producing new output.

    This is the "card not responsive" case: cphd_aic.py detects the SAR/AI card
    server is down (it prints "Server is not running. Cannot test other functions.")
    and exits without generating a tiff, while the profiler wrapper still runs its
    metrics/plotting workflow to completion. The message is surfaced to the frontend
    via /get_tif_file_properties so it can stop its elapsed timer and prompt the
    operator to restart the server manually."""
    global sar_error
    sar_error = "SAR card not responsive — please restart the server manually."
    print(f"SAR run failed (no new output, exit code {return_code}): {sar_error}")

def handle_image_sending(filename, on_image_sent=None):
    """Run the SAR program, detect the output tiff, and publish it locally."""
    global sar_proc_time, sar_process, sar_stop_requested, sar_run_pid

    start_time = time.perf_counter()
    sar_stop_requested = False

    known_tifs = {}
    for path in glob.glob(os.path.join(OUT_TIF_PATH, "*.tiff")):
        try:
            known_tifs[path] = os.path.getmtime(path)
        except OSError:
            pass

    process = subprocess.Popen(["profiler", SAR_PROG, *PROFILER_OPTION, "--file", filename],
                               start_new_session=True)
    sar_process = process
    sar_run_pid = process.pid                 # was reported to host over SAR_CTRL socket
    print(f"SAR program started with PID {process.pid}")

    sent_tif_path = None
    pending_tif = None
    pending_size = -1

    while process.poll() is None:
        print("processing...")
        if sent_tif_path is None and not sar_stop_requested:
            new_tif = find_new_output_tif(known_tifs)
            if new_tif:
                try:
                    size = os.path.getsize(new_tif)
                except OSError:
                    size = -1
                if new_tif == pending_tif and size == pending_size and size > 0:
                    sar_proc_time = round(time.perf_counter() - start_time, 1)
                    print(f"SAR processing time: {sar_proc_time:.1f}s")
                    publish_tif_image(new_tif)
                    if on_image_sent:
                        on_image_sent(new_tif)
                    sent_tif_path = new_tif
                else:
                    pending_tif, pending_size = new_tif, size
        time.sleep(1)

    return_code = process.returncode
    sar_process = None
    sar_run_pid = None                        # was the SAR_CTRL "finished" status

    if sar_stop_requested:
        print("SAR program stopped by reset, skipping output publish")
        return None

    print("done")

    if sent_tif_path is None:
        # The poll loop didn't confirm a fresh tiff before the SAR program exited.
        # Look once more, but ONLY for a tiff that appeared or changed during THIS
        # run (find_new_output_tif honours known_tifs) — never fall back to a stale
        # tiff from a previous run. If the SAR program terminated without producing
        # new output (card server down, see cphd_aic.py's "Server is not running"),
        # that's a failed run, not a success with an old image.
        sar_proc_time = round(time.perf_counter() - start_time, 1)
        print(f"SAR processing time: {sar_proc_time:.1f}s")
        sent_tif_path = find_new_output_tif(known_tifs)
        if sent_tif_path is None:
            report_sar_failure(return_code)
            return None
        publish_tif_image(sent_tif_path)
        if on_image_sent:
            on_image_sent(sent_tif_path)

    collect_sar_logs()
    return sent_tif_path

def compute_tif_properties(tif_path, filePath):
    """Merged send_tif_properties()+host text receiver: compute the processed-image
    properties and store them for the /get_tif_file_properties route."""
    global tif_file_properties
    tif_size = os.path.getsize(tif_path)
    cphd_size = os.path.getsize(filePath)
    reduction_scale = round(cphd_size / tif_size, 2) if tif_size else 0
    size_compared = round((tif_size / cphd_size) * 100, 2) if cphd_size else 0
    reduction_factor = round(100 - size_compared, 2)
    tif_size_str = f"{tif_size / 1_000_000:.2f} MB" if tif_size >= 1_000_000 else f"{tif_size} bytes"
    tif_resolution = get_resolution_from_tif(tif_path)
    print(f"TIF resolution: {tif_resolution}")

    tif_file_properties = {
        "tif_filename": os.path.basename(tif_path),
        "size": tif_size_str,
        "reduction_factor": reduction_factor,
        "size_compared": size_compared,
        "reduction_scale": reduction_scale,
        "sar_proc_time": sar_proc_time,
        "tif_resolution": tif_resolution,
    }
    print(f"TIF properties: {tif_file_properties}")

def process_cphd_file(filePath, filename):
    def on_image_sent(p):
        compute_tif_properties(p, filePath)
        push_tif_to_vlm(p)

    tif_path = handle_image_sending(filename, on_image_sent=on_image_sent)
    if tif_path is None:
        print("No SAR output to report")

# ----------------------------------------------------------------------------
# CPHD discovery / metadata (from target)
# ----------------------------------------------------------------------------
def scan_cphd_files():
    """Merged send_cphd_files_list()+host list receiver: scan DEMO_PATH and store the
    filename->path map and the served name list."""
    global cphd_files, cphd_file_list
    cphd_files = {}
    if not os.path.exists(DEMO_PATH):
        print(f"CPHD directory does NOT exist: {DEMO_PATH}")
    for root, dirs, files in os.walk(DEMO_PATH):
        for file in files:
            if file.endswith(".cphd"):
                full = os.path.join(root, file)
                # Key by path relative to DEMO_PATH: a bare basename for the bundled
                # top-level demo files (unchanged), and "user_data/<file>.cphd" for
                # uploads. This relative name is exactly what cphd_aic.py wants for
                # its --file argument, and lets the frontend tell uploads apart.
                rel = os.path.relpath(full, DEMO_PATH)
                cphd_files[rel] = full
    cphd_file_list = list(cphd_files.keys())
    print(f"Found .cphd files: {cphd_file_list}")

def get_resolution_from_tif(tif_path):
    try:
        from osgeo import gdal
        ds = gdal.Open(tif_path)
        if ds:
            gt = ds.GetGeoTransform()
            res = abs(gt[1])
            ds = None
            return res
    except ImportError:
        pass
    except Exception as e:
        print(f"Error extracting resolution via GDAL: {e}")
    try:
        img = Image.open(tif_path)
        tag_v2 = getattr(img, 'tag_v2', {})
        pixel_scale = tag_v2.get(33550)  # GeoTIFF ModelPixelScaleTag
        if pixel_scale and len(pixel_scale) >= 1:
            return pixel_scale[0]
    except Exception as e:
        print(f"Error extracting resolution via PIL: {e}")
    return None

def get_metadata_from_cphd(filePath):
    try:
        from sarpy.io.phase_history.cphd import CPHDReader
        reader = CPHDReader(filePath)
        meta = reader.cphd_meta
        result = {
            "numVectors": meta.Data.Channels[0].NumVectors,
            "fxC": meta.Channel.Parameters[0].FxC,
            "numLines": meta.SceneCoordinates.ImageGrid.IAXExtent.NumLines,
            "lineSpacing": meta.SceneCoordinates.ImageGrid.IAXExtent.LineSpacing,
            "numSamples": meta.SceneCoordinates.ImageGrid.IAYExtent.NumSamples,
            "sampleSpacing": meta.SceneCoordinates.ImageGrid.IAYExtent.SampleSpacing,
        }
        reader.close()
        return result
    except Exception as e:
        print(f"Error reading CPHD metadata: {e}")
    return None

def compute_cphd_properties(filename):
    """Merged SIZE: handler: store size + metadata for /get_cphd_file_properties."""
    global cphd_file_properties, progress_update
    progress_update = 0.0
    filePath = cphd_files.get(filename)
    if filePath and os.path.exists(filePath):
        file_size = os.path.getsize(filePath)
        metadata = get_metadata_from_cphd(filePath)
        file_size_str = f"{file_size / 1_000_000:.2f} MB" if file_size >= 1_000_000 else f"{file_size} bytes"
        cphd_file_properties = {"filename": filename, "size": file_size_str, "metadata": metadata}
        print(f"CPHD properties: {cphd_file_properties}")

# ----------------------------------------------------------------------------
# Network tests (target<->RDB / adapter paths only; onboard loopback dropped)
# ----------------------------------------------------------------------------
def handle_netrun_test(netTestDuration, netTestInterface):
    global bwValue
    bwValue = int(bwValue)

    if netTestInterface == "LwEthAdt" or netTestInterface == FM_INTERFACE_ID:
        filePath = SAVE_PATH_IPERF_LW_ETH_ADT
        netTest_clientIP = LW_ETH_ADT_CLIENT_IP
    elif netTestInterface == "UpEthAdt":
        filePath = SAVE_PATH_IPERF_UP_ETH_ADT
        netTest_clientIP = UP_ETH_ADT_CLIENT_IP
    else:
        print(f"Onboard/unsupported net test '{netTestInterface}' disabled in single-box mode")
        return

    def run_test(reverse=False):
        if bwValue == 10000:
            print("run tcp net test")
            command = ["iperf3", "-c", netTest_clientIP, "-b", "20G", "-t", netTestDuration, "-P", "4", "-i", "1", "-J"]
        else:
            print("run udp net test")
            command = ["iperf3", "-c", netTest_clientIP, "-u", "-b", "20G", "-t", netTestDuration, "-P", "4", "-i", "1", "-J"]
        if reverse:
            command.append("-R")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate()
        if process.returncode == 0:
            return json.loads(stdout).get("end", {})
        print(f"Error running iperf3 ({'upload' if reverse else 'download'}): {stderr}")
        return None

    down_result = run_test(reverse=True)
    time.sleep(2)
    up_result = run_test(reverse=False)

    if down_result or up_result:
        try:
            with open(filePath, "r") as f:
                existing_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            existing_data = {}
        if down_result:
            existing_data["down"] = down_result
        if up_result:
            existing_data["up"] = up_result
        with open(filePath, "w") as f:
            json.dump(existing_data, f, indent=4)
        print(f"Net test results saved to {filePath}")
    else:
        print("No valid results to save.")

def set_bandwidth(bw, target):
    """BW: handler — set interface link speed with ethtool."""
    global bwValue
    bwValue = bw
    if target == "LwEthOnb":
        interface_id = LW_ETH_OB_INTERFACE_ID
    elif target == "UpEthOnb":
        interface_id = UP_ETH_OB_INTERFACE_ID
    elif target == "LwEthAdt" or target == FM_INTERFACE_ID:
        interface_id = LW_ETH_ADT_INTERFACE_ID
    elif target == "UpEthAdt":
        interface_id = UP_ETH_ADT_INTERFACE_ID
    else:
        print(f"Unknown interface identifier: {target}")
        return
    command = ["ethtool", "-s", interface_id, "speed", bw, "autoneg", "on"]
    print(f"Executing command: {' '.join(command)}")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = process.communicate()
    if process.returncode == 0:
        print("Command executed successfully.", stdout or "")
    else:
        print(f"Error executing ethtool. Return code: {process.returncode}: {stderr}")

def run_iperf3_server():
    try:
        subprocess.run(["iperf3", "-s"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running iperf3 server: {e}")
    except FileNotFoundError:
        print("iperf3 command not found. Please ensure iperf3 is installed.")

def delete_all_files():
    """'3' handler: clear pictures/ and reset all cached SAR/CPHD state + progress.

    The frontend's Reset button (resetMenu() in sar_process_upload.html) always
    sends this after STOPSAR, so it's the single place that reliably fires once
    per Reset click -- the natural hook to also tell the VLM box to drop its
    SAR image and end any SAR chat session."""
    global cphd_file_list, cphd_file_properties, tif_file_properties, current_run_cphd, progress_update, sar_error
    global sar_log_cleared
    # The profiler plots belong to the same result set as the output images this
    # clears, so they go with them: the performance-analysis panel polls
    # /sar_log/status and blanks itself as soon as this makes it report no run.
    # Note the SAR page also calls resetMenu() at boot, so a dashboard load lands
    # here too -- the panel is deliberately empty until a run completes.
    sar_log_cleared = True
    cphd_file_list = []
    cphd_file_properties = {}
    tif_file_properties = {}
    current_run_cphd = None
    progress_update = 0.0
    sar_error = None
    threading.Thread(target=notify_vlm_reset, daemon=True).start()
    try:
        for file_name in os.listdir(SAVE_DIR):
            os.remove(os.path.join(SAVE_DIR, file_name))
            print(f"Deleted: {file_name}")
    except Exception as e:
        print(f"Error clearing pictures: {e}")

# ----------------------------------------------------------------------------
# Command dispatch (replaces host /send_message forwarding + target listener)
# ----------------------------------------------------------------------------
def handle_command(message):
    """Execute a control command in-process. Returns the JSON response to send."""
    global current_run_cphd, tif_file_properties, sar_error

    if message == "3":
        delete_all_files()
        return JSONResponse({"status": "success", "message": "cleared"})

    if message == "4":
        scan_cphd_files()
        return JSONResponse({"status": "success", "message": "cphd list refreshed"})

    if message == "STOPSAR" or message.startswith("STOPSAR:"):
        stop_sar_process(message)
        return JSONResponse({"status": "success", "message": "stop requested"})

    if message.startswith("SIZE:"):
        compute_cphd_properties(message.split(":", 1)[1])
        return JSONResponse({"status": "success", "message": "size computed"})

    if message.startswith("RUN:"):
        filename = message[4:]
        current_run_cphd = filename
        tif_file_properties = {}
        sar_error = None                      # clear any prior failure before a new run
        filePath = cphd_files.get(filename)
        if filePath and os.path.exists(filePath):
            print("file exists, start processing")
            # An operator can pick a different CPHD and hit Run directly without
            # pressing Reset first -- drop the VLM box's now-stale SAR image /
            # chat session the same way Reset does, so it never lingers past the
            # point a new run has actually started.
            threading.Thread(target=notify_vlm_reset, daemon=True).start()
            threading.Thread(target=process_cphd_file, args=(filePath, filename), daemon=True).start()
            return JSONResponse({"status": "success", "message": f"processing {filename}"})
        return JSONResponse({"status": "error", "error": f"CPHD not found: {filename}"}, status_code=404)

    if message.startswith("NETRUN:"):
        _, netTestDuration, netTestInterface = message.split(":", 2)
        if netTestInterface in ("LwEthOnb", "UpEthOnb"):
            return JSONResponse({"status": "skipped",
                                 "message": "onboard net tests disabled in single-box mode"})
        threading.Thread(target=handle_netrun_test,
                         args=(netTestDuration, netTestInterface), daemon=True).start()
        return JSONResponse({"status": "success", "message": "net test started"})

    if message.startswith("BW:"):
        parts = message.split(":")
        if len(parts) != 3:
            return JSONResponse({"status": "error", "error": "invalid BW format"}, status_code=400)
        set_bandwidth(parts[1], parts[2])
        return JSONResponse({"status": "success", "message": "bandwidth set"})

    print(f"Unknown command: {message}")
    return JSONResponse({"status": "success", "message": message})

# ----------------------------------------------------------------------------
# Metrics collection (from target) + InfluxDB write (from host), in-process
# ----------------------------------------------------------------------------
def read_rapl_energy():
    try:
        with open("/sys/class/powercap/intel-rapl:0/energy_uj", "r") as f:
            return int(f.read().strip())
    except FileNotFoundError:
        return None

def get_cpu_power():
    energy_start = read_rapl_energy()
    if energy_start is None:
        return None
    time.sleep(0.1)
    energy_end = read_rapl_energy()
    if energy_end is None:
        return None
    return (energy_end - energy_start) / 1_000_000 / 0.1  # µJ -> W

def poll_ai_card_diagnostics():
    global ai_card_power_cache, ai_card_temp_cache
    while True:
        try:
            result = subprocess.run(["diagnostic", "-power"], capture_output=True, text=True, timeout=20)
            for line in result.stdout.splitlines():
                if line.startswith("power (W):"):
                    ai_card_power_cache = float(line.split(":")[1].strip())
                    break
        except Exception:
            pass
        try:
            result = subprocess.run(["diagnostic", "-temperature"], capture_output=True, text=True, timeout=20)
            temp = {}
            for line in result.stdout.splitlines():
                if line.startswith("VDD_12V0 rail 1 external thermal sensor temperature (C):"):
                    temp["ext_temp"] = float(line.split(":")[1].strip())
                elif line.startswith("VDD_1V8 rail 2 UCD9090 internal sensor temperature (C):"):
                    temp["int_temp_1"] = float(line.split(":")[1].strip())
                elif line.startswith("internal temperature - rail 4 ISL8273MD-U47 (C):"):
                    temp["int_temp_2"] = float(line.split(":")[1].strip())
                elif line.startswith("internal temperature - rail 4 ISL8273MD-U48 (C):"):
                    temp["int_temp_3"] = float(line.split(":")[1].strip())
            if temp:
                ai_card_temp_cache = temp
        except Exception:
            pass

def get_fan_power():
    try:
        result = subprocess.run(["sudo", "bash", FAN_STATUS], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if line.startswith("PWM Setting :"):
                pwm_value = float(line.split(":")[1].strip().split("/")[0])
                return pwm_value / 63 * 12 * 0.75 * 1.2 * 5  # 5x fans
    except Exception:
        pass
    return None

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

    # root_disk_usage = psutil.disk_usage('/').percent
    # root_total_disk = psutil.disk_usage('/').total
    home = psutil.disk_usage('/home')
    total_disk_usage = home.percent      # df-style: used / (used + avail)
    total_disk_size = home.total
    # total_disk_used = home.used


    num_threads = psutil.cpu_count(logical=True)
    num_cores = psutil.cpu_count(logical=False)
    uptime_seconds = time.time() - psutil.boot_time()

    interfaces = [LW_ETH_OB_INTERFACE_ID, LW_ETH_ADT_INTERFACE_ID,
                  UP_ETH_OB_INTERFACE_ID, UP_ETH_ADT_INTERFACE_ID, WIRELESS_INTERFACE_ID]
    net_before = psutil.net_io_counters(pernic=True)
    time.sleep(0.1)
    net_after = psutil.net_io_counters(pernic=True)
    network_stats = {}
    for iface in interfaces:
        if iface in net_before and iface in net_after:
            net_b, net_a = net_before[iface], net_after[iface]
            network_stats[iface] = {
                "bytes_sent": net_a.bytes_sent,
                "bytes_recv": net_a.bytes_recv,
                "upload_speed": (net_a.bytes_sent - net_b.bytes_sent) / 0.1,
                "download_speed": (net_a.bytes_recv - net_b.bytes_recv) / 0.1,
                "packets_sent": net_a.packets_sent,
                "packets_recv": net_a.packets_recv,
            }

    return {
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
        "network": network_stats,
        "cpu_power": get_cpu_power(),
        "total_disk_usage": total_disk_usage,
        "total_disk_size": total_disk_size,
        "progress_update": progress_update,
        "ai_card_power": ai_card_power_cache,
        "ai_card_temp": ai_card_temp_cache,
        "fan_power": get_fan_power(),
    }

def _fnum(v, default=0.0):
    """Coerce to float, tolerating None/invalid (fields may be absent on some hardware)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def build_influx_point(system_info):
    """Transform a system_info dict into an InfluxDB point (from host's writer)."""
    per_core_usage_data = {
        f"per_core_usage{i}": _fnum(system_info["per_core_usage"].get(f"core_{i}_usage", 0))
        for i in range(32)
    }
    per_core_freq_data = {
        f"per_core_freq{i}": _fnum(system_info["per_core_freq"].get(f"core_{i}_frequency", 0))
        for i in range(32)
    }
    network_data = {}
    for iface_name, iface_stats in system_info.get("network", {}).items():
        for stat_name, value in iface_stats.items():
            v = _fnum(value, None)
            if v is not None:
                network_data[f"{iface_name}_{stat_name}"] = v

    raw_ai_card_temp = system_info.get("ai_card_temp") or {}
    ai_card_temp = {
        "ai_card_ext_temp": _fnum(raw_ai_card_temp.get("ext_temp", 0)),
        "ai_card_int_temp_1": _fnum(raw_ai_card_temp.get("int_temp_1", 0)),
        "ai_card_int_temp_2": _fnum(raw_ai_card_temp.get("int_temp_2", 0)),
        "ai_card_int_temp_3": _fnum(raw_ai_card_temp.get("int_temp_3", 0)),
    }

    cpu_power = _fnum(system_info.get("cpu_power"))
    ai_card_power = _fnum(system_info.get("ai_card_power"))
    fan_power = _fnum(system_info.get("fan_power"))
    total_power = (cpu_power + ai_card_power + fan_power) * 1.08  # peripherals adjustment

    return {
        "measurement": "system_metrics",
        "tags": {"host": "HPC", "source": "127.0.0.1"},
        "fields": {
            "cpu_usage": _fnum(system_info["cpu_usage"]),
            "memory_usage": _fnum(system_info["memory_usage"]),
            "swap_usage": _fnum(system_info["swap_usage"]),
            "cpu_temperature": _fnum(system_info.get("cpu_temperature")),
            "uptime_seconds": _fnum(system_info["uptime_seconds"]),
            "total_memory": _fnum(system_info["total_memory"]),
            "total_swap": _fnum(system_info["total_swap"]),
            "num_threads": int(system_info["num_threads"]),
            "cpu_power": cpu_power,
            "total_disk_usage": _fnum(system_info.get("total_disk_usage")),
            "total_disk_size": _fnum(system_info.get("total_disk_size")),
            "progress_update": _fnum(system_info.get("progress_update")),
            **per_core_usage_data,
            **per_core_freq_data,
            **network_data,
            **ai_card_temp,
            "ai_card_power": ai_card_power,
            "fan_power": fan_power,
            "total_power": total_power,
        },
        "time": int(time.time() * 1e9),
    }

# ----------------------------------------------------------------------------
# Remote device health
# ----------------------------------------------------------------------------
def classify_probe(url):
    """Probe one device over HTTP and say whether the dashboard can show it.

    Returns (ok, detail); detail is the short phrase the shell prints under
    "disconnected", so it is written for whoever is looking at the screen.

    Down is deliberately narrow -- the states you cannot see past: the box does
    not answer at all, it answers with a server error (nginx would turn that into
    the 502 page the placeholder replaces), or it refuses us. Anything else,
    including a 404, means an HTTP server is alive on the other end and the
    panel is worth loading, so the exact health path never has to be right."""
    try:
        resp = requests.get(url, timeout=DEVICE_HEALTH_TIMEOUT, allow_redirects=False)
    except requests.exceptions.Timeout:
        return False, f"no response within {DEVICE_HEALTH_TIMEOUT:g}s"
    except requests.exceptions.ConnectionError:
        # Covers both failure modes that look different on the wire but identical
        # on screen: the app is not listening (connection refused) and the box is
        # off the network entirely (the connect attempt goes nowhere).
        return False, "Unreachable - connection refused or host down"
    except requests.exceptions.RequestException as e:
        return False, f"probe failed: {type(e).__name__}"

    code = resp.status_code
    if code in (401, 403):
        return False, f"permission denied (HTTP {code})"
    if code >= 500:
        return False, f"device returned HTTP {code}"
    return True, f"HTTP {code}"


def device_health_loop():
    """Probe every device in DEVICES on a fixed interval and publish the verdict.

    Failures have to repeat DEVICE_HEALTH_FAIL_STREAK times before a device flips
    to down; a success flips it back immediately, because a box that just
    answered is not a box worth waiting on. `since` records when the current
    status was entered so the shell can say how long something has been out."""
    streaks = {name: 0 for name in DEVICES}
    while True:
        for name, cfg in DEVICES.items():
            ok, detail = classify_probe(cfg["url"])
            if ok:
                streaks[name] = 0
                status = "up"
            else:
                streaks[name] += 1
                # Hold the previous status until the streak is long enough. A
                # device that has never reported stays "unknown", which the shell
                # paints as "Connecting..." rather than as an outage.
                if streaks[name] < DEVICE_HEALTH_FAIL_STREAK:
                    with device_health_lock:
                        device_health[name]["checked"] = time.time()
                    continue
                status = "down"

            now = time.time()
            with device_health_lock:
                entry = device_health[name]
                if entry["status"] != status:
                    entry["since"] = now
                    print(f"Device '{name}' is {status}: {detail}")
                entry["status"] = status
                entry["detail"] = detail
                entry["checked"] = now
        time.sleep(DEVICE_HEALTH_INTERVAL)


def metrics_loop():
    """Collect metrics and write them straight to InfluxDB (no socket round-trip).

    The same sample is cached in `latest_metrics` (with the derived total_power)
    so /system_metrics can serve the whole monitoring dashboard from memory — the
    frontend never triggers its own psutil sampling, keeping each refresh cheap."""
    global latest_metrics
    client = InfluxDBClient(INFLUXDB_HOST, INFLUXDB_PORT, INFLUXDB_USER, INFLUXDB_PASSWORD, INFLUXDB_DB)
    print("Metrics loop started, writing to InfluxDB")
    while True:
        try:
            info = get_system_info()
            point = build_influx_point(info)
            client.write_points([point])
            snapshot = dict(info)
            snapshot["total_power"] = point["fields"]["total_power"]
            snapshot["timestamp"] = time.time()
            with latest_metrics_lock:
                latest_metrics = snapshot
        except Exception as e:
            print(f"Metrics loop error: {e}")
        time.sleep(METRICS_INTERVAL)

# ----------------------------------------------------------------------------
# HTTP API (from host)
# ----------------------------------------------------------------------------
@app.get('/')
def combined_frontend():
    """Serve the combined dashboard shell (replaces the old Grafana frontend).

    It is a thin page that iframes the two standalone frontends below, so a
    single visit to http://<target>:5000/ shows the SAR application on top and
    the live system-utilisation monitor beneath it. The frames load same-origin
    from '/sar_app' and '/system_monitor'."""
    return send_file(COMBINED_FRONTEND)

@app.get('/rsat')
def combined_frontend_rsat():
    return send_file(COMBINED_FRONTEND_RSAT)

@app.get('/test')
def combined_frontend_testing():
    return send_file(COMBINED_FRONTEND_TESTING)

@app.get('/sar_app')
def sar_frontend():
    """Serve the standalone SAR-process page (upload + delete). Hosted in the
    combined dashboard via a same-origin <iframe>, and still reachable directly."""
    return send_file(SAR_FRONTEND)

@app.get('/sar_app_rsat')
def sar_frontend_rsat():
    return send_file(SAR_FRONTEND_RSAT)

@app.get('/cctv_app')
def cctv_frontend():
    return send_file(CCTV_FRONTEND)

@app.get('/branding/{filename:path}')
def branding_panel(filename: str):
    """Serve a standalone branding panel (RSAT logo, mission banner, Insight One
    logo, 'Powered by AICRAFT') from index/branding/. The RSAT dashboard header
    iframes these same-origin so the logo assets stay single-source files."""
    return send_from_directory(BRANDING_DIR, filename)

@app.get('/monitor')
@app.get('/system_monitor')
def monitor_frontend():
    """Serve the live system-utilisation page (embeddable in Grafana via iframe,
    same as the SAR page). It refreshes itself from /system_metrics."""
    return send_file(MONITOR_FRONTEND)

@app.get('/performance_analysis')
def performance_analysis_frontend():
    """Serve the post-run performance-analysis page (the profiler plots of the
    latest SAR run). Hosted as its own collapsible row in the combined dashboard
    the same way the SAR and monitor pages are, and still reachable directly. It
    drives itself from /sar_log/status and /sar_log/image/<type>."""
    return send_file(PERFORMANCE_FRONTEND)

@app.get('/system_metrics')
def system_metrics():
    """Return the whole live snapshot the monitoring dashboard needs in one call.

    Served straight from the in-memory cache filled by metrics_loop, so a 1 Hz
    (or faster) refresh from any number of viewers costs nothing beyond a dict
    copy — there is no per-request psutil sampling or InfluxDB query."""
    with latest_metrics_lock:
        return JSONResponse(latest_metrics)

@app.get('/device_health')
def device_health_status():
    """Whether each box behind an nginx prefix is answering, for the shell page.

    Polled by the combined dashboard every few seconds. It is served from the
    cache device_health_loop maintains, so it never blocks on a dead box no
    matter how many dashboards are open."""
    with device_health_lock:
        devices = {name: dict(entry) for name, entry in device_health.items()}
    return JSONResponse({"devices": devices})


@app.get('/storage_info')
def storage_info():
    """Report free/total disk space on the filesystem that holds the CPHD store,
    so the frontend can refuse an upload that won't fit before sending it."""
    try:
        usage = shutil.disk_usage(DEMO_PATH)
        return JSONResponse({
            "free": usage.free,
            "total": usage.total,
            "used": usage.used,
            "margin": UPLOAD_FREE_SPACE_MARGIN,
            "usable": max(0, usage.free - UPLOAD_FREE_SPACE_MARGIN),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post('/upload_cphd')
async def upload_cphd(request: Request, filename: str = Query("")):
    """Stream an uploaded .cphd file into DEMO_PATH/user_data.

    The body is the raw file bytes (not multipart), so nginx and this app can
    stream it straight to disk in chunks instead of buffering multi-GB files in
    memory or a temp file. The target filename comes from the ?filename= query
    parameter.

    This is the one `async def` handler in the file: reading the body as it
    arrives needs the event loop. Every blocking step (write, disk_usage, the
    rescan afterwards) is pushed to a worker thread so a multi-GB upload never
    stalls the metrics endpoint the dashboard is polling.
    """
    name = secure_filename(filename)
    if not name:
        return JSONResponse({"status": "error", "error": "missing filename"}, status_code=400)
    if not name.lower().endswith('.cphd'):
        return JSONResponse({"status": "error", "error": "only .cphd files are allowed"}, status_code=400)

    # Pre-flight space check against the declared upload size.
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    try:
        free = shutil.disk_usage(DEMO_PATH).free
    except Exception as e:
        return JSONResponse({"status": "error", "error": f"cannot stat storage: {e}"}, status_code=500)
    if declared and declared + UPLOAD_FREE_SPACE_MARGIN > free:
        return JSONResponse({
            "status": "error", "error": "not enough space",
            "free": free, "needed": declared, "margin": UPLOAD_FREE_SPACE_MARGIN,
        }, status_code=507)  # Insufficient Storage

    try:
        os.makedirs(USER_DATA_PATH, exist_ok=True)
    except Exception as e:
        return JSONResponse({"status": "error",
                             "error": f"upload folder not writable: {e}"}, status_code=500)

    dest = os.path.join(USER_DATA_PATH, name)

    def write_block(handle, block):
        # Guard against the disk filling mid-stream (e.g. no Content-Length).
        if shutil.disk_usage(DEMO_PATH).free < UPLOAD_FREE_SPACE_MARGIN:
            raise IOError("ran out of space during upload")
        handle.write(block)

    written = 0
    try:
        out = await run_in_threadpool(open, dest, 'wb')
        try:
            buffered = bytearray()
            async for chunk in request.stream():
                if not chunk:
                    continue
                buffered.extend(chunk)
                if len(buffered) >= UPLOAD_CHUNK_SIZE:
                    block = bytes(buffered)
                    buffered.clear()
                    written += len(block)
                    await run_in_threadpool(write_block, out, block)
            if buffered:
                block = bytes(buffered)
                written += len(block)
                await run_in_threadpool(write_block, out, block)
        finally:
            await run_in_threadpool(out.close)
    except Exception as e:
        if os.path.exists(dest):
            os.remove(dest)  # don't leave a truncated file in the picker
        return JSONResponse({"status": "error", "error": str(e)}, status_code=507)

    await run_in_threadpool(scan_cphd_files)  # make the new file appear in the picker
    rel = os.path.relpath(dest, DEMO_PATH)
    print(f"Uploaded CPHD '{rel}' ({written} bytes)")
    return JSONResponse({"status": "success", "filename": rel,
                         "display": name, "size": written})

@app.post('/delete_upload')
def delete_upload(payload: Optional[DeleteUploadRequest] = None):
    """Delete a single user-uploaded CPHD. Only files under user_data/ may be
    removed — the bundled demo files and anything outside the folder are refused."""
    rel = payload.filename if payload else ''
    prefix = USER_DATA_DIRNAME + '/'
    if not rel.startswith(prefix):
        return JSONResponse({"status": "error", "error": "only uploaded files can be deleted"}, status_code=403)

    # Resolve and confirm the path stays inside USER_DATA_PATH (no traversal).
    full = os.path.normpath(os.path.join(DEMO_PATH, rel))
    base = os.path.abspath(USER_DATA_PATH)
    if os.path.commonpath([base, os.path.abspath(full)]) != base:
        return JSONResponse({"status": "error", "error": "invalid path"}, status_code=403)

    if os.path.isfile(full):
        try:
            os.remove(full)
            print(f"Deleted uploaded CPHD '{rel}'")
        except Exception as e:
            return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    scan_cphd_files()
    return JSONResponse({"status": "success", "filename": rel})

@app.post('/send_message')
def send_message(payload: Optional[CommandMessage] = None):
    message = payload.message if payload else "Default message"
    print(f"Command: {message}")
    return handle_command(message)

@app.get('/get_cphd_files')
def get_cphd_files():
    return JSONResponse({"files": cphd_file_list})

@app.get('/get_cphd_file_properties')
def get_cphd_file_properties():
    return JSONResponse({"files": cphd_file_properties})

@app.get('/get_tif_file_properties')
def get_tif_file_properties():
    return JSONResponse({"files": tif_file_properties, "cphd_filename": current_run_cphd, "error": sar_error})

@app.get('/images/{filename}')
def serve_image(filename: str):
    return send_from_directory(SAVE_DIR, filename)

SAR_LOG_IMAGE_TYPES = {
    "overall_cpu_usage":    "overall_cpu_usage_",
    "cpu_power":            "cpu_power_",
    "cpu_cores_usage":      "cpu_cores_usage_",
    "cpu_cores_frequency":  "cpu_cores_frequency_",
    "cpu_temperature":      "cpu_temperature_",
    "memory_usage":         "memory_usage_",
}

# Histogram plots (histogram/ subfolder of the run dir, written by --histograms).
# Unlike SAR_LOG_IMAGE_TYPES these filenames carry no run timestamp, so the map
# holds the exact basename rather than a prefix. Only a subset of what
# --histograms can produce is exposed here for now.
HISTOGRAM_IMAGE_TYPES = {
    "core_usage_p_and_e_mean": "core_usage_histogram_p_and_e_mean",
    "core_usage_stacked_mean": "core_usage_histogram_stacked_mean",
    "pooled_core_usage":       "pooled_core_usage_histogram",
}

def current_sar_log_folder():
    """Name of the SAR log folder the dashboard should be showing, or None.

    That is the newest collected folder -- collect_sar_logs() names each one
    after the profiler run's start time (YYYYmmdd_HHMMSS), so a plain sort puts
    the newest last -- except between a Reset and the next completed run, where
    it is None. The folders themselves are never deleted; sar_log_cleared only
    hides them from the API so the analysis panel blanks along with the SAR
    output images that Reset wipes."""
    if sar_log_cleared:
        return None
    try:
        folders = sorted(d for d in os.listdir(SAR_LOGS_DIR)
                         if os.path.isdir(os.path.join(SAR_LOGS_DIR, d)))
    except FileNotFoundError:
        return None
    return folders[-1] if folders else None

@app.get('/sar_log/image/{image_type}')
def serve_sar_log_image(image_type: str):
    if image_type not in SAR_LOG_IMAGE_TYPES:
        return PlainTextResponse("Unknown image type", status_code=400)
    folder = current_sar_log_folder()
    if folder is None:
        return PlainTextResponse("No log data", status_code=404)
    folder_path = os.path.join(SAR_LOGS_DIR, folder)
    prefix = SAR_LOG_IMAGE_TYPES[image_type]
    try:
        matches = [f for f in os.listdir(folder_path) if f.startswith(prefix) and f.endswith('.webp')]
    except FileNotFoundError:
        return PlainTextResponse("Log folder not found", status_code=404)
    if not matches:
        return PlainTextResponse("Image not found", status_code=404)
    return send_from_directory(folder_path, matches[0])

@app.get('/sar_log/histogram_image/{image_type}')
def serve_sar_log_histogram_image(image_type: str):
    if image_type not in HISTOGRAM_IMAGE_TYPES:
        return PlainTextResponse("Unknown image type", status_code=400)
    folder = current_sar_log_folder()
    if folder is None:
        return PlainTextResponse("No log data", status_code=404)
    hist_dir = os.path.join(SAR_LOGS_DIR, folder, "histogram")
    webp_name = HISTOGRAM_IMAGE_TYPES[image_type] + ".webp"
    return send_from_directory(hist_dir, webp_name)

@app.get('/sar_log/status')
def sar_log_status():
    """Describe the profiler plots currently on offer, in one call.

    `version` counts the collections THIS process has made; the SAR page uses it
    to spot the logs of a run it just started, and it stays first in the payload
    because that page has always read it. `folder` and `images` are what let the
    standalone performance-analysis panel drive itself: the folder timestamp
    identifies the run and survives a restart (unlike version, which resets to
    0), so the panel re-fetches exactly when it changes, and `images` says which
    of SAR_LOG_IMAGE_TYPES that folder actually holds so the panel never asks for
    a plot that is not there.

    After a Reset both go empty (folder null, images []) until the next run is
    collected, which is what makes the panel clear itself -- see
    current_sar_log_folder(). `version` deliberately does NOT move on a Reset:
    sar_process_image_panel.html polls it against a pre-run baseline to spot its
    own run's logs, and a Reset-driven bump would fire that early."""
    folder = current_sar_log_folder()
    images = []
    histogram_images = []
    if folder is not None:
        try:
            names = os.listdir(os.path.join(SAR_LOGS_DIR, folder))
        except OSError:
            names = []
        images = [t for t, prefix in SAR_LOG_IMAGE_TYPES.items()
                  if any(n.startswith(prefix) and n.endswith('.webp') for n in names)]
        try:
            hist_names = os.listdir(os.path.join(SAR_LOGS_DIR, folder, "histogram"))
        except OSError:
            hist_names = []
        histogram_images = [t for t, base in HISTOGRAM_IMAGE_TYPES.items()
                            if f"{base}.webp" in hist_names]
    return JSONResponse({"version": sar_log_version, "folder": folder, "images": images,
                         "histogram_images": histogram_images})

@app.get('/sar_colored_image')
def serve_sar_colored_image(filename: str = Query("")):
    if not filename:
        return PlainTextResponse("No filename provided", status_code=400)
    # Colored images are stored flat by basename; strip any "user_data/" prefix
    # that an uploaded file's relative name carries.
    base_name = os.path.basename(filename)
    if base_name.lower().endswith('.cphd'):
        base_name = base_name[:-5]
    colored_image_name = f"{base_name}_colered_img.webp"
    if os.path.exists(os.path.join(SAR_COLORED_IMAGE_PATH, colored_image_name)):
        return send_from_directory(SAR_COLORED_IMAGE_PATH, colored_image_name)
    return PlainTextResponse("No image found", status_code=404)

@app.get('/iperf3/lw_eth_adt_results')
def iperf_lw_eth_adt_results():
    return send_file(SAVE_PATH_IPERF_LW_ETH_ADT, media_type='application/json')

@app.get('/iperf3/up_eth_adt_results')
def iperf_up_eth_adt_results():
    return send_file(SAVE_PATH_IPERF_UP_ETH_ADT, media_type='application/json')


# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Running uvicorn server on {HOST_IP}:{SERVER_PORT}...")
    # The collector / iperf3 / AI-card threads start from `lifespan` above, so
    # they come up whether the app is launched from here or by an external
    # `uvicorn combined_hpc:app`.
    uvicorn.run(app, host=HOST_IP, port=SERVER_PORT, log_level="info")
