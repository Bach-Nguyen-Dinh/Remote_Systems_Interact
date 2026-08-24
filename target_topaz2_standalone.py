from PIL import Image
import socket
import subprocess
import threading
import psutil
import time
import json
import os
import re
import glob
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import signal

from imu_manager import IMUManager

RSS_EXECUTABLE_PATH = "/home/user/Small-Object-Detection/Utils/RSS"
INPUT_IMAGE_DIR = "/home/user/Small-Object-Detection/Data/Data1/Image"
OUTPUT_IMAGE_DIR = "/home/user/Small-Object-Detection/Data/Data1/Predictions"

AI_SMOKE_PATH = "/home/user/ai_smoke/ai_server.py"
AI_SHIP_PATH = "/home/user/modified_ai_ship/ship/ai_ship.py"

# IMAGE_PATH_2 = "/home/root/Desktop/Bach/backprojection_result_small.png"  
# IMAGE_PATH_1 = "/home/root/Desktop/Bach/backprojection_histogram.png"
RESIZED_IMAGE_PATH = "/home/user/demo/optimized_image.webp"  # Temporary resized image path
DEMO_PATH = "/home/user/demo/"

HOST_IP = "10.42.0.1"
SYSINFO_PORT = 12346
IMAGE_PORT = 55556
TIME_BEFORE_RETRY = 1.0

LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 54322
SOCK_TOUT = 3

INTERNAL_STREAM_IP = "127.0.0.1"
RECV_BUFFER = 65536    # 64KB, should be enough for typical JSON payloads
SOCKET_TIMEOUT = 1.0   # seconds - allows clean shutdown checks

DOCKER_INTERFACE_ID = "docker0"
FM_INTERFACE_ID = "fm1-mac3"
LOCAL_INTERFACE_ID = "lo"
VIRTUAL_INTERFACE_ID = "virbr0"

# SAVE_PATH_IPERF_LW_ETH_ADT = "/home/root/iperf3_end_result_LwEthAdt.json"
# SAVE_PATH_IPERF_UP_ETH_ADT = "/home/root/iperf3_end_result_UpEthAdt.json"

CURR_DIR = os.path.dirname(os.path.abspath(__file__))
SAVE_PATH_IPERF_LW_ETH_ADT = os.path.join(CURR_DIR, "iperf3_end_result_LwEthAdt.json")
SAVE_PATH_IPERF_UP_ETH_ADT = os.path.join(CURR_DIR, "iperf3_end_result_UpEthAdt.json")

AI_METRIC_PATH = "/home/user/ai_tool/status"
AI_PORT = 8888
NUM_AI_CORE = 4
# Upper bound for the AI-ship page's "number of running CPU core" picker. The
# page offers 1..4; clamp to what this box actually has so a stale page can't
# ask for more.
NUM_CPU_CORE = min(4, os.cpu_count() or 1)

# IMU Fusion daemon configuration
IMU_EXECUTABLE_PATH = "/home/user/Remote_Systems_Interact/topaz_imu/main"
IMU_UDP_PORT = 8889
IMU_SAMPLE_RATE = 10   # Hz
IMU_CALIBRATION_SAMPLES = 100  # ~10 seconds at 10Hz

# IMU Manager instance (initialized after stop_event is created)
imu_manager = None

# Global variable
progress_update = 0.0
global_pwr_var = 1000
cphd_files = {}
BW = 1000

small_obj_detect_running = False
small_obj_process = None          # Popen of the RSS binary while a run is in flight
small_obj_stop_requested = False  # set by stop_small_object_detection()
current_output_count = 0

ai_core_run_ai_smoke = 4
cpu_core_run_ai_smoke = NUM_CPU_CORE
ai_smoke_running = False
ai_smoke_process = None           # Popen of ai_server.py while a run is in flight
ai_smoke_stop_requested = False   # set by stop_ai_smoke()
ai_core_run_ai_ship = 4
cpu_core_run_ai_ship = NUM_CPU_CORE
ai_ship_running = False
ai_ship_process = None            # Popen of ai_ship.py while a run is in flight
ai_ship_stop_requested = False    # set by stop_ai_ship()

ai_run_metrics_raw = None
ai_run_metrics_lock = threading.Lock()  # Add thread safety

stop_event = threading.Event()

def signal_handler(sig, frame):
    stop_event.set()
    if imu_manager is not None:
        imu_manager.stop()

def listener_ai_run():
    global ai_run_metrics_raw  # Declare global variable
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((INTERNAL_STREAM_IP, AI_PORT))
        print(f"AI metrics listener bound to {INTERNAL_STREAM_IP}:{AI_PORT}")
    except Exception as e:
        print(f"Failed to bind to {INTERNAL_STREAM_IP}:{AI_PORT}: {e}")
        return
    
    sock.settimeout(SOCKET_TIMEOUT)

    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(RECV_BUFFER)
            # Update the global variable with thread safety
            with ai_run_metrics_lock:
                ai_run_metrics_raw = data  # Store the raw bytes
            # print(f"Received AI metrics data from {addr}: {len(data)} bytes")
        except socket.timeout:
            continue  # This is expected and allows clean shutdown
        except Exception as e:
            print(f"Socket error in AI listener: {e}")
            break

    sock.close()
    print("AI run listener stopped")

def send_progress_update(data):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((HOST_IP, IMAGE_PORT))
            sock.sendall(json.dumps(data).encode())
        # print(f"Progress update sent: {data}")
    except Exception as e:
        print(f"Error sending progress update: {e}")

def terminate_process_group(process, label):
    """Escalate SIGINT -> SIGTERM -> SIGKILL over `process`'s group.

    The AI workloads are plain compute scripts with no signal handling of their
    own, so Ctrl+C cannot be assumed to be honoured — same escalation
    stop_small_object_detection() uses. Signalling the *group* also catches the
    worker processes the script spawns, which is what actually keeps the AI
    engines busy.
    """
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        print(f"{label} process already exited")
        return

    for sig, grace in ((signal.SIGINT, 3), (signal.SIGTERM, 3), (signal.SIGKILL, 0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            print(f"{label} process already exited")
            return
        print(f"Sent {sig.name} to {label} process group {pgid}")
        if grace == 0:
            return
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue

def stop_targets_running_process(message, process):
    """Does this "stop_<workload>[:<pid>]" command refer to `process`?

    The host appends the PID this target reported in the run's "_start" update,
    so a Stop click that arrives after the run it was meant for already ended
    can never kill a newer run. An unqualified stop (no pid) always matches.
    """
    parts = message.split(":", 1)
    if len(parts) == 2 and parts[1].strip().isdigit():
        return int(parts[1]) == process.pid
    return True

def run_ai_smoke():
    global ai_core_run_ai_smoke, cpu_core_run_ai_smoke, ai_smoke_running
    global ai_smoke_process, ai_smoke_stop_requested

    if ai_smoke_running:
        print("AI smoke already running")
        return
    ai_smoke_running = True
    ai_smoke_stop_requested = False

    try:
        print(f"Running ai server with {ai_core_run_ai_smoke} AI cores "
              f"and {cpu_core_run_ai_smoke} CPU cores")

        # Run the AI smoke process in its own process group, so a Stop from the
        # dashboard can signal the whole group (the server plus its workers).
        process = subprocess.Popen([
            "python3",
            AI_SMOKE_PATH,
            "--cpu",
            str(cpu_core_run_ai_smoke),
            "--ai",
            str(ai_core_run_ai_smoke)
        ], start_new_session=True)
        ai_smoke_process = process

        # Send start notification. The PID rides along so the host can hand it
        # back with a later "stop_ai_smoke:<pid>".
        start_data = {
            "type": "ai_smoke_start",
            "status": "started",
            "ai_cores": ai_core_run_ai_smoke,
            "cpu_cores": cpu_core_run_ai_smoke,
            "pid": process.pid
        }
        send_progress_update(start_data)

        # Wait for process to complete
        return_code = process.wait()

        # Send completion notification based on return code — but a Stop makes
        # the non-zero exit expected, so report that as "stopped", not an error.
        if ai_smoke_stop_requested:
            completion_data = {
                "type": "ai_smoke_stopped",
                "status": "stopped",
                "ai_cores": ai_core_run_ai_smoke,
                "cpu_cores": cpu_core_run_ai_smoke,
                "pid": process.pid
            }
            print("AI smoke process stopped on request")
        elif return_code == 0:
            completion_data = {
                "type": "ai_smoke_complete",
                "status": "completed",
                "ai_cores": ai_core_run_ai_smoke,
                "cpu_cores": cpu_core_run_ai_smoke,
                "pid": process.pid
            }
            print("AI smoke process completed successfully")
        else:
            completion_data = {
                "type": "ai_smoke_error",
                "status": "error",
                "error": f"Process exited with code {return_code}",
                "ai_cores": ai_core_run_ai_smoke,
                "cpu_cores": cpu_core_run_ai_smoke,
                "pid": process.pid
            }
            print(f"AI smoke process failed with return code {return_code}")

        send_progress_update(completion_data)

    except Exception as e:
        # Send error notification
        error_data = {
            "type": "ai_smoke_error",
            "error": str(e)
        }
        send_progress_update(error_data)
        print(f"Error running ai_smoke: {e}")
    finally:
        ai_smoke_process = None
        ai_smoke_running = False

def stop_ai_smoke(message):
    """Kill the running AI-smoke process group, if any.

    Runs on its own thread: the escalation in terminate_process_group() waits,
    and listen_for_messages() must stay responsive to other commands meanwhile.
    """
    global ai_smoke_stop_requested

    process = ai_smoke_process
    if process is None or process.poll() is not None:
        print("No AI smoke running, nothing to stop")
        return

    if not stop_targets_running_process(message, process):
        print(f"stop_ai_smoke pid does not match running pid {process.pid}, ignoring")
        return

    # Tell run_ai_smoke() this exit was requested, so it reports "stopped"
    # instead of the error the kill's non-zero exit code would otherwise mean.
    ai_smoke_stop_requested = True
    terminate_process_group(process, "AI smoke")

def run_ai_ship():
    global ai_core_run_ai_ship, cpu_core_run_ai_ship, ai_ship_running
    global ai_ship_process, ai_ship_stop_requested

    if ai_ship_running:
        print("AI ship already running")
        return
    ai_ship_running = True
    ai_ship_stop_requested = False

    try:
        print(f"Running ai application with {ai_core_run_ai_ship} AI cores "
              f"and {cpu_core_run_ai_ship} CPU cores")

        # Own process group so Stop can signal the whole group — see run_ai_smoke().
        process = subprocess.Popen([
            "python3",
            AI_SHIP_PATH,
            "--cpu",
            str(cpu_core_run_ai_ship),
            "--ai",
            str(ai_core_run_ai_ship)
        ], start_new_session=True)
        ai_ship_process = process

        # Send start notification, carrying the PID for a later
        # "stop_ai_ship:<pid>".
        start_data = {
            "type": "ai_ship_start",
            "status": "started",
            "ai_cores": ai_core_run_ai_ship,
            "cpu_cores": cpu_core_run_ai_ship,
            "pid": process.pid
        }
        send_progress_update(start_data)

        return_code = process.wait()

        # Send completion notification based on return code — a requested stop
        # is reported as "stopped" rather than an error.
        if ai_ship_stop_requested:
            completion_data = {
                "type": "ai_ship_stopped",
                "status": "stopped",
                "ai_cores": ai_core_run_ai_ship,
                "cpu_cores": cpu_core_run_ai_ship,
                "pid": process.pid
            }
            print("AI ship application stopped on request")
        elif return_code == 0:
            completion_data = {
                "type": "ai_ship_complete",
                "status": "completed",
                "ai_cores": ai_core_run_ai_ship,
                "cpu_cores": cpu_core_run_ai_ship,
                "pid": process.pid
            }
            print("AI ship application completed successfully")
        else:
            completion_data = {
                "type": "ai_ship_error",
                "status": "error",
                "error": f"Process exited with code {return_code}",
                "ai_cores": ai_core_run_ai_ship,
                "cpu_cores": cpu_core_run_ai_ship,
                "pid": process.pid
            }
            print(f"AI ship application failed with return code {return_code}")

        send_progress_update(completion_data)

    except Exception as e:
        # Send error notification
        error_data = {
            "type": "ai_ship_error",
            "error": str(e)
        }
        send_progress_update(error_data)
        print(f"Error running ai_ship: {e}")
    finally:
        ai_ship_process = None
        ai_ship_running = False

def stop_ai_ship(message):
    """Kill the running AI-ship process group, if any. See stop_ai_smoke()."""
    global ai_ship_stop_requested

    process = ai_ship_process
    if process is None or process.poll() is not None:
        print("No AI ship running, nothing to stop")
        return

    if not stop_targets_running_process(message, process):
        print(f"stop_ai_ship pid does not match running pid {process.pid}, ignoring")
        return

    ai_ship_stop_requested = True
    terminate_process_group(process, "AI ship")

def stop_small_object_detection(message):
    """Kill the running small-object-detection process group, if any.

    `message` is "stop_small_obj_detect" or "stop_small_obj_detect:<pid>" — the
    optional pid is the one this target reported in the "_start" update, so a
    Stop click that arrives late (after the run it was meant for already ended)
    can never kill a newer run. Mirrors stop_sar_process() on the HPC target.

    RSS is a plain compute binary with no signal handling of its own, so escalate
    SIGINT -> SIGTERM -> SIGKILL rather than assuming Ctrl+C is honoured. Runs on
    its own thread: the escalation waits, and listen_for_messages() must stay
    responsive to other commands meanwhile.
    """
    global small_obj_stop_requested

    process = small_obj_process
    if process is None or process.poll() is not None:
        print("No small object detection running, nothing to stop")
        return

    parts = message.split(":", 1)
    if len(parts) == 2 and parts[1].strip().isdigit() and int(parts[1]) != process.pid:
        print(f"stop_small_obj_detect pid {parts[1]} does not match running pid {process.pid}, ignoring")
        return

    # Tell run_small_object_detection() this exit was requested, so it reports
    # "stopped" instead of "completed" when process.wait() returns.
    small_obj_stop_requested = True

    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        print("Small object detection process already exited")
        return

    for sig, grace in ((signal.SIGINT, 3), (signal.SIGTERM, 3), (signal.SIGKILL, 0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            print("Small object detection process already exited")
            return
        print(f"Sent {sig.name} to small object detection process group {pgid}")
        if grace == 0:
            return
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue

def run_small_object_detection():
    global small_obj_detect_running, current_output_count
    global small_obj_process, small_obj_stop_requested

    if small_obj_detect_running:
        print("Small object detection already running")
        return

    small_obj_detect_running = True
    small_obj_stop_requested = False
    current_output_count = 0

    # Clear output directory
    if os.path.exists(OUTPUT_IMAGE_DIR):
        for file in glob.glob(os.path.join(OUTPUT_IMAGE_DIR, "*.png")):
            os.remove(file)
    else:
        os.makedirs(OUTPUT_IMAGE_DIR)
    
    # Set up file system watcher
    event_handler = OutputImageHandler()
    observer = Observer()
    observer.schedule(event_handler, OUTPUT_IMAGE_DIR, recursive=False)
    observer.start()
    
    try:
        # Run the RSS executable in its own process group, so a Stop from the
        # dashboard can signal the whole group (RSS plus anything it spawns).
        print(f"Starting RSS executable: {RSS_EXECUTABLE_PATH}")
        process = subprocess.Popen([RSS_EXECUTABLE_PATH],
                                 cwd="/home/user/Small-Object-Detection",
                                 start_new_session=True)
        small_obj_process = process

        # Send start notification. The PID rides along so the host can hand it
        # back with a later "stop_small_obj_detect:<pid>".
        start_data = {
            "type": "small_obj_detect_start",
            "status": "started",
            "pid": process.pid
        }
        send_progress_update(start_data)

        # Wait for process to complete
        process.wait()

        # Send completion notification — or a distinct "stopped" if the exit was
        # the result of a Stop request rather than the run finishing.
        if small_obj_stop_requested:
            completion_data = {
                "type": "small_obj_detect_stopped",
                "status": "stopped",
                "pid": process.pid
            }
        else:
            completion_data = {
                "type": "small_obj_detect_complete",
                "status": "completed",
                "pid": process.pid
            }
        send_progress_update(completion_data)

    except Exception as e:
        error_data = {
            "type": "small_obj_detect_error", 
            "error": str(e)
        }
        send_progress_update(error_data)
        print(f"Error running small object detection: {e}")
    finally:
        observer.stop()
        observer.join()
        small_obj_process = None
        small_obj_detect_running = False

class OutputImageHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith('.png'):
            filename = os.path.basename(event.src_path)
            if filename.replace('.png', '').isdigit():
                output_index = int(filename.replace('.png', ''))
                input_index = output_index + 1  # Input is always 1 index ahead
                # track the progress of the png file but tell the host to use the webp version for updating progress
                progress_data = {
                    "type": "small_obj_detect_progress",
                    "output_image": f"{output_index}.webp", # tell the host to take the webp version
                    "input_image": f"{input_index}.webp", # tell the host to take the webp version
                    "output_index": output_index,
                    "input_index": input_index
                }
                
                send_progress_update(progress_data)

def optimize_tif(image_path, output_path, format="webp", max_size=(800, 800), quality=85):
    """
    Optimize and convert a TIFF image for efficient transmission and web display.

    :param image_path: Path to the input TIFF file.
    :param output_path: Path to save the optimized image.
    :param format: Target format (jpg, png, webp, avif).
    :param max_size: Tuple (width, height) to resize the image.
    :param quality: Quality setting for lossy formats (JPEG/WebP/AVIF).
    """
    try:
        img = Image.open(image_path)
        if img.mode in ("P", "CMYK", "RGBA"):
            img = img.convert("RGB")
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        ext = format.lower()
        if ext not in ["jpeg", "jpg", "png", "webp", "avif"]:
            raise ValueError("Unsupported format. Use jpeg, png, webp, or avif.")
        img.save(output_path, format=ext.upper(), quality=quality, optimize=True)

        # Print out the size of the optimized image
        optimized_size = os.path.getsize(output_path)
        print(f"Optimized image saved at {output_path}, size: {optimized_size} bytes")

    except Exception as e:
        print(f"Error optimizing image: {e}")

def send_image(image_path):
    optimize_tif(image_path, RESIZED_IMAGE_PATH, format="webp", max_size=(800, 800), quality=80)
    
    if not os.path.exists(RESIZED_IMAGE_PATH):
        print(f"Error: Image file {RESIZED_IMAGE_PATH} not found!")
        return

    file_size = os.path.getsize(RESIZED_IMAGE_PATH)
    print(f"Sending image {RESIZED_IMAGE_PATH} to host, size: {file_size} bytes...")
    
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((HOST_IP, IMAGE_PORT))

            # Send file size first
            sock.sendall(file_size.to_bytes(8, byteorder="big"))

            # Read and send the file
            with open(RESIZED_IMAGE_PATH, "rb") as f:
                while chunk := f.read(4096):
                    sock.sendall(chunk)

            print("Image sent successfully!")
    except Exception as e:
        print(f"Error sending image: {e}")

def handle_image_sending(image_path):
    timestamps = {
        0: 0.0,
        1: 6.67,
        2: 13.33,
        3: 20.0,
        4: 26.67,
        5: 33.33,
        6: 40.0,
        10: 66.67,
        11: 73.33,
        12: 80.0,
        15: 100.0
    }
    for second in range(16):
        time.sleep(1)
        if second in timestamps:
            global progress_update
            progress_update = timestamps[second]
    send_image(image_path)

def send_cphd_files_list():
    global cphd_files
    cphd_files = {}  # Change to dictionary

    if os.path.exists(DEMO_PATH):
        print("Directory exists!")
    else:
        print("Directory does NOT exist!")

    # Scan for .cphd files
    for root, dirs, files in os.walk(DEMO_PATH):
        for file in files:
            if file.endswith(".cphd"):
                full_path = os.path.join(root, file)
                cphd_files[file] = full_path  # Store filename as key, full path as value

    print("Found .cphd files:", list(cphd_files.keys()))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((HOST_IP, IMAGE_PORT))
            sock.sendall(json.dumps({"cphd_files": list(cphd_files.keys())}).encode())  # Send only filenames
    except Exception as e:
        print(f"Error sending CPHD files: {e}")
    
def get_cphd_file_size(filename):
    global cphd_files
    file_path = cphd_files.get(filename)

    if file_path and os.path.exists(file_path):
        return os.path.getsize(file_path)
    return None

def get_metadata_from_json(directory):
    try:
        for file in os.listdir(directory):
            if file.endswith(".json"):
                json_path = os.path.join(directory, file)
                with open(json_path, "r") as f:
                    data = json.load(f)
                    derived_products = data.get("derivedProducts", {}).get("GEC", [{}])[0]
                    return {
                        "numRows": derived_products.get("numRows"),
                        "numColumns": derived_products.get("numColumns"),
                        "groundResolution": derived_products.get("groundResolution", {}).get("azimuthMeters")
                    }
    except Exception as e:
        print(f"Error reading metadata: {e}")
    return None

def process_cphd_file(file_path):
    directory = os.path.dirname(file_path)
    tif_files = [f for f in os.listdir(directory) if f.endswith(".tif")]
    # png_files = [f for f in os.listdir(directory) if f.endswith(".png")]
    
    if tif_files:
        tif_path = os.path.join(directory, tif_files[0])  # Take the first .tif file found

        # send the processed image
        handle_image_sending(tif_path)
        
        # send the properies of the processed image
        tif_size = os.path.getsize(tif_path)
        cphd_size = os.path.getsize(file_path)
        reduction_scale = round(cphd_size / tif_size, 2)
        size_compared = round((tif_size / cphd_size) * 100, 2) if cphd_size else 0
        reduction_factor = round(100 - size_compared, 2)

        tif_size_str = f"{tif_size / 1_000_000:.2f} MB" if tif_size >= 1_000_000 else f"{tif_size} bytes"

        response = {
            "tif_filename": tif_files[0],
            "size": tif_size_str,
            "reduction_factor": reduction_factor,
            "size_compared": size_compared,
            "reduction_scale": reduction_scale
        }        
        print(f"Response: {response}")
        
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as size_sock:
                size_sock.connect((HOST_IP, IMAGE_PORT))
                size_sock.sendall(json.dumps(response).encode())
        except Exception as e:
            print(f"Error sending tif file properties: {e}")

        # if png_files:
        #     png_path = os.path.join(directory, png_files[0])

        #     # send the furhter processed image
        #     handle_image_sending(png_path)

def listen_for_messages():
    global progress_update, BW
    global ai_core_run_ai_smoke, cpu_core_run_ai_smoke
    global ai_core_run_ai_ship, cpu_core_run_ai_ship

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Allow reuse
    server_socket.settimeout(1.0)  # Add timeout for accept()
    server_socket.bind((LISTEN_IP, LISTEN_PORT))
    server_socket.listen(1)

    print(f"Listening for messages on {LISTEN_IP}:{LISTEN_PORT}...")

    while not stop_event.is_set():
        try:
            conn, addr = server_socket.accept()
            conn.settimeout(2.0)  # Add timeout for recv()
            with conn:
                print(f"Connection received from {addr}")
                message = conn.recv(1024).decode().strip()
                if message:
                    print(f"Message from host: {message}")
                    if message == "3":
                        progress_update = 0.0
                    elif message == "4":
                        send_cphd_files_list()

                    elif message.startswith("SIZE"):
                        progress_update = 0.0
                        filename = message.split(":", 1)[1]
                        file_path = cphd_files.get(filename)

                        if file_path and os.path.exists(file_path):
                            file_size = os.path.getsize(file_path)
                            metadata = get_metadata_from_json(os.path.dirname(file_path))

                            file_size_str = f"{file_size / 1_000_000:.2f} MB" if file_size >= 1_000_000 else f"{file_size} bytes"

                            response = {"filename": filename, "size": file_size_str, "metadata": metadata}
                            print(f"Response: {response}")

                            try:
                                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as size_sock:
                                    size_sock.connect((HOST_IP, IMAGE_PORT))
                                    size_sock.sendall(json.dumps(response).encode())
                            except Exception as e:
                                print(f"Error sending file size and metadata: {e}")

                    elif message.startswith("RUN"):
                        filename = message.split(":", 1)[1] if ":" in message else None
                        if filename:
                            file_path = cphd_files.get(filename)

                            if file_path and os.path.exists(file_path):
                                threading.Thread(target=process_cphd_file, args=(file_path,), daemon=True).start()

                    elif message == "run_small_obj_detect":
                        threading.Thread(target=run_small_object_detection, daemon=True).start()

                    elif message.startswith("stop_small_obj_detect"):
                        # Off-thread: the signal escalation can take several
                        # seconds and this loop handles one message at a time.
                        threading.Thread(target=stop_small_object_detection,
                                         args=(message,), daemon=True).start()

                    elif message.startswith("run_ai_smoke"):
                        # "run_ai_smoke:<ai cores>[:<cpu cores>]" — the CPU field
                        # is optional so an older page that only sends the AI
                        # count still runs, keeping the previous CPU selection.
                        parts = message.split(":")
                        if len(parts) > 1:
                            ai_core_run_ai_smoke = int(parts[1])
                            if ai_core_run_ai_smoke > NUM_AI_CORE:
                                ai_core_run_ai_smoke = NUM_AI_CORE
                            elif ai_core_run_ai_smoke <= 0:
                                ai_core_run_ai_smoke = 1
                        if len(parts) > 2:
                            cpu_core_run_ai_smoke = int(parts[2])
                            if cpu_core_run_ai_smoke > NUM_CPU_CORE:
                                cpu_core_run_ai_smoke = NUM_CPU_CORE
                            elif cpu_core_run_ai_smoke <= 0:
                                cpu_core_run_ai_smoke = 1
                        threading.Thread(target=run_ai_smoke, daemon=True).start()

                    elif message.startswith("stop_ai_smoke"):
                        # Off-thread: the signal escalation can take several
                        # seconds and this loop handles one message at a time.
                        threading.Thread(target=stop_ai_smoke,
                                         args=(message,), daemon=True).start()

                    elif message.startswith("run_ai_ship"):
                        # "run_ai_ship:<ai cores>[:<cpu cores>]" — the CPU field
                        # is optional so an older page that only sends the AI
                        # count still runs, keeping the previous CPU selection.
                        parts = message.split(":")
                        if len(parts) > 1:
                            ai_core_run_ai_ship = int(parts[1])
                            if ai_core_run_ai_ship > NUM_AI_CORE:
                                ai_core_run_ai_ship = NUM_AI_CORE
                            elif ai_core_run_ai_ship <= 0:
                                ai_core_run_ai_ship = 1
                        if len(parts) > 2:
                            cpu_core_run_ai_ship = int(parts[2])
                            if cpu_core_run_ai_ship > NUM_CPU_CORE:
                                cpu_core_run_ai_ship = NUM_CPU_CORE
                            elif cpu_core_run_ai_ship <= 0:
                                cpu_core_run_ai_ship = 1
                        threading.Thread(target=run_ai_ship, daemon=True).start()

                    elif message.startswith("stop_ai_ship"):
                        threading.Thread(target=stop_ai_ship,
                                         args=(message,), daemon=True).start()

                    elif message == "recalibrate_imu":
                        print("IMU recalibration requested by host")
                        if imu_manager is not None:
                            threading.Thread(target=imu_manager.restart, daemon=True).start()

        except socket.timeout:
            # This is expected and allows checking stop_event
            continue
        except Exception as e:
            if not stop_event.is_set():
                print(f"Error in message listener: {e}")
            break

    server_socket.close()
    print("Message listener stopped")

    # try:
    #     server_socket.bind((LISTEN_IP, LISTEN_PORT))
    #     server_socket.listen(1)

    #     print(f"Listening for messages on {LISTEN_IP}:{LISTEN_PORT}...")

    #     while not stop_event.is_set():
    #         try:
    #             conn, addr = server_socket.accept()  # This can timeout
    #             conn.settimeout(2.0)  # Add timeout for recv()
    #             with conn:
    #                 print(f"Connection received from {addr}")
    #                 message = conn.recv(1024).decode().strip()
    #                 if message:
    #                     print(f"Message from host: {message}")
    #                     # Your existing message handling code...
    #                     if message == "3":
    #                         progress_update = 0.0
    #                     elif message == "4":
    #                         send_cphd_files_list()  # Send CPHD files back to host
    #                     elif message.startswith("SIZE:"):
    #                         progress_update = 0.0
    #                         filename = message.split(":", 1)[1]
    #                         file_path = cphd_files.get(filename)
                            
    #                         if file_path and os.path.exists(file_path):
    #                             file_size = os.path.getsize(file_path)
    #                             metadata = get_metadata_from_json(os.path.dirname(file_path))
                                
    #                             file_size_str = f"{file_size / 1_000_000:.2f} MB" if file_size >= 1_000_000 else f"{file_size} bytes"
                                
    #                             response = {"filename": filename, "size": file_size_str, "metadata": metadata}
    #                             print(f"Response: {response}")
                                
    #                             try:
    #                                 with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as size_sock:
    #                                     size_sock.connect((HOST_IP, IMAGE_PORT))
    #                                     size_sock.sendall(json.dumps(response).encode())
    #                             except Exception as e:
    #                                 print(f"Error sending file size and metadata: {e}")
    #                     elif message.startswith("RUN:"):
    #                         filename = message.split(":", 1)[1]
    #                         file_path = cphd_files.get(filename)
                            
    #                         if file_path and os.path.exists(file_path):
    #                             threading.Thread(target=process_cphd_file, args=(file_path,), daemon=True).start()

    #                     elif message == "run_small_obj_detect":
    #                         threading.Thread(target=run_small_object_detection, daemon=True).start()
                            
    #         except socket.timeout:
    #             # This is expected and allows checking stop_event
    #             continue
    #         except Exception as e:
    #             if not stop_event.is_set():
    #                 print(f"Error in message listener: {e}")
    #             break
                
    # finally:
    #     server_socket.close()
    #     print("Message listener stopped")

# Function to run the iperf3 server
def run_iperf3_server():
    try:
        # Start iperf3 as subprocess instead of blocking call
        process = subprocess.Popen(["iperf3", "-s"], 
                                 stdout=subprocess.DEVNULL, 
                                 stderr=subprocess.DEVNULL)
        
        # Wait for stop event while process runs
        while not stop_event.is_set() and process.poll() is None:
            time.sleep(0.5)
            
        # Terminate when stopping
        if process.poll() is None:
            print("Terminating iperf3 server...")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                
    except FileNotFoundError:
        print("iperf3 command not found. Please ensure iperf3 is installed.")
    except Exception as e:
        print(f"Error running iperf3 server: {e}")
    finally:
        print("iperf3 server stopped")

def get_power_from_sensor(sensor_name):
    try:
        # Run the sensors command
        output = subprocess.check_output(["sensors"], text=True)

        # Split into blocks (each sensor section is separated by blank lines)
        blocks = output.strip().split("\n\n")

        for block in blocks:
            if block.startswith(sensor_name):
                # Look for power1 line with watts (W)
                watt_match = re.search(r"power1:\s+([\d\.]+)\s*W", block)
                if watt_match:
                    return float(watt_match.group(1))
                
                # Look for power1 line with milliwatts (mW) 
                milliwatt_match = re.search(r"power1:\s+([\d\.]+)\s*mW", block)
                if milliwatt_match:
                    # Convert milliwatts to watts
                    return float(milliwatt_match.group(1)) / 1000.0
                
                # If power1 line exists but no unit match, return None
                if "power1:" in block:
                    print(f"Warning: Found power1 in {sensor_name} but couldn't parse unit")
                    return None
                    
        return None  # Sensor not found
    except subprocess.CalledProcessError as e:
        print("Error running sensors:", e)
        return None

def get_ai_metrics():
    global ai_run_metrics_raw
    ai_temp = {}
    ai_freq = {}
    ai_run_metrics = {
        "ai_core_1_usage": 0.0,
        "ai_core_2_usage": 0.0,
        "ai_core_3_usage": 0.0,
        "ai_core_4_usage": 0.0,
        "active_core": 4,
    }

    # Get AI temperature and frequency metrics (your existing code)
    try:
        process = subprocess.Popen([AI_METRIC_PATH],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   text=True)
        time.sleep(0.6)
        process.terminate()
        stdout, stderr = process.communicate(timeout=1)

        if stdout:
            lines = stdout.strip().split('\n')
            
            for line in lines:
                temp_match = re.search(r"AI core (\d+) temp: ([\d\.]+)", line)
                if temp_match:
                    core_id = int(temp_match.group(1))
                    temp = float(temp_match.group(2))
                    ai_temp[f"ai_core_{core_id}_temp"] = temp
    
                freq_match = re.search(r"AI core (\d+) frequency: ([\d\.]+)", line)
                if freq_match:
                    core_id = int(freq_match.group(1))
                    freq = int(freq_match.group(2))
                    ai_freq[f"ai_core_{core_id}_freq"] = freq
            
    except subprocess.TimeoutExpired:
        process.kill()
        print("AI metrics timeout")
    except Exception as e:
        print(f"Error getting AI metrics: {e}")

    # Process the UDP metrics data with thread safety
    with ai_run_metrics_lock:
        if ai_run_metrics_raw is not None:
            try:
                # Decode the bytes to string
                ai_run_metrics_decode = ai_run_metrics_raw.decode("utf-8")
                # Parse the JSON string (use json.loads, not json.load)
                ai_run_metrics_data = json.loads(ai_run_metrics_decode)
                
                # Reset the dictionary before updating
                ai_run_metrics = {
                    "ai_core_0_usage": 0.0,
                    "ai_core_1_usage": 0.0,
                    "ai_core_2_usage": 0.0,
                    "ai_core_3_usage": 0.0,
                    "active_core": 4,
                }
                
                # Update with received data
                if "0" in ai_run_metrics_data:
                    ai_run_metrics["ai_core_0_usage"], ai_run_metrics["active_core"] = ai_run_metrics_data["0"], 1
                if "1" in ai_run_metrics_data:
                    ai_run_metrics["ai_core_1_usage"], ai_run_metrics["active_core"] = ai_run_metrics_data["1"], 2
                if "2" in ai_run_metrics_data:
                    ai_run_metrics["ai_core_2_usage"], ai_run_metrics["active_core"] = ai_run_metrics_data["2"], 3
                if "3" in ai_run_metrics_data:
                    ai_run_metrics["ai_core_3_usage"], ai_run_metrics["active_core"] = ai_run_metrics_data["3"], 4
                
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                print(f"Error parsing AI run metrics: {e}")
            except Exception as e:
                print(f"Unexpected error processing AI metrics: {e}")

    return ai_temp, ai_freq, ai_run_metrics

def get_system_info():
    global ai_run_metrics_raw, global_pwr_var

    per_core_usage = psutil.cpu_percent(interval=0.1, percpu=True)
    core_usage = {f"core_{i}_usage": usage for i, usage in enumerate(per_core_usage)}
    # core_frequencies = read_cpu_frequencies()
    # --- Per-core CPU frequencies ---
    core_frequencies = {}

    # Try reading from sysfs first (Linux only)
    sysfs_paths = sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"))
    if sysfs_paths:
        for i, path in enumerate(sysfs_paths):
            try:
                with open(path) as f:
                    # scaling_cur_freq is in kHz, convert to MHz
                    core_frequencies[f"core_{i}_frequency"] = int(f.read().strip()) / 1000
            except Exception:
                core_frequencies[f"core_{i}_frequency"] = None
    elif hasattr(psutil, "cpu_freq"):
        # Fall back to psutil (may only return one object)
        freq_info = psutil.cpu_freq(percpu=True)
        if freq_info:
            for i, freq in enumerate(freq_info):
                core_frequencies[f"core_{i}_frequency"] = freq.current

    sys_temp = None
    temps = {}
    sensor_data = psutil.sensors_temperatures()
    if sensor_data:
        for name, entries in sensor_data.items():
            for entry in entries:
                label = entry.label if entry.label else "unknown"
                temps[f"{name}_{label}"] = entry.current
    sys_temp = max(temps.values())

    mem = psutil.virtual_memory()
    memory_usage = (mem.used / mem.total) * 100
    total_memory = mem.total
    swap_usage = psutil.swap_memory().percent
    total_swap = psutil.swap_memory().total
    total_disk_usage = psutil.disk_usage('/').percent   # Get disk usage for '/'
    total_disk_size = psutil.disk_usage('/').total  # Get disk size for '/'
    num_threads = psutil.cpu_count(logical=True)
    num_cores = psutil.cpu_count(logical=False)
    uptime_seconds = time.time() - psutil.boot_time()
    # Network info for specific interfaces
    interfaces = [DOCKER_INTERFACE_ID, LOCAL_INTERFACE_ID, FM_INTERFACE_ID, VIRTUAL_INTERFACE_ID]
    net_before = psutil.net_io_counters(pernic=True)
    time.sleep(0.1)
    net_after = psutil.net_io_counters(pernic=True)

    network_stats = {}
    for iface in interfaces:
        if iface in net_before and iface in net_after:
            net_b = net_before[iface]
            net_a = net_after[iface]
            network_stats[iface] = {
                "bytes_sent": net_a.bytes_sent,
                "bytes_recv": net_a.bytes_recv,
                "upload_speed": (net_a.bytes_sent - net_b.bytes_sent) / 0.1,
                "download_speed": (net_a.bytes_recv - net_b.bytes_recv) / 0.1,
                "packets_sent": net_a.packets_sent,
                "packets_recv": net_a.packets_recv,
            }
    
    cpu_power = get_power_from_sensor("ina220-i2c-0-40")
    # print(f"CPU Power: {cpu_power} W" if cpu_power is not None else "CPU Power: N/A")

    ai_total_pwr = get_power_from_sensor("ina220-i2c-0-44")
    ai_total_pwr2 = ai_total_pwr
    ratio_power_p = ai_total_pwr/4

    if ratio_power_p < global_pwr_var:
        global_pwr_var = ratio_power_p

    ai_total_pwr = ai_total_pwr - global_pwr_var


    ai_temps, ai_freqs, ai_run_metrics = get_ai_metrics()
    # print(ai_temps)
    # print(ai_run_metrics)
    
    ai_core_power = {
        "ai_core_0_pwr": global_pwr_var,
        "ai_core_1_pwr": global_pwr_var,
        "ai_core_2_pwr": global_pwr_var,
        "ai_core_3_pwr": global_pwr_var,
    }

    # print(f"ai_run_metrics {ai_run_metrics}")
    for i in range(ai_run_metrics["active_core"]):
        index_ =  f"ai_core_{i}_pwr"
        ai_core_power[index_] = ai_total_pwr/ai_run_metrics["active_core"]

    ai_run_metrics_raw = None

    imu_data = imu_manager.get_data() if imu_manager is not None else {
        "accel_x": 0.0, "accel_y": 0.0, "accel_z": 0.0,
        "gyro_x": 0.0, "gyro_y": 0.0, "gyro_z": 0.0,
        "roll": 0.0, "pitch": 0.0, "yaw": 0.0,
        "calibration_state": "idle", "calibration_progress": 0
    }

    system_info = {
        "memory_usage": memory_usage,
        "total_memory": total_memory,
        "swap_usage": swap_usage,
        "total_swap": total_swap,
        "num_threads": num_threads,
        "num_cores": num_cores,
        "uptime_seconds": uptime_seconds,
        "per_core_usage": core_usage,
        "per_core_freq": core_frequencies,
        "sys_temp": sys_temp,
        "network": network_stats,
        "cpu_power": cpu_power,
        "total_disk_usage": total_disk_usage,
        "total_disk_size": total_disk_size,
        "progress_update": progress_update,
        "ai_temps": ai_temps,
        "ai_freqs": ai_freqs,
        "ai_total_pwr": ai_total_pwr2,
        "ai_run_metrics": ai_run_metrics,
        "per_ai_core_pwrs": ai_core_power,
        "imu_data": imu_data
    }
    # print(f"System Info: {system_info}")
    return system_info

def main():
    global imu_manager

    # Initialize IMU Manager (shares stop_event with main program)
    imu_manager = IMUManager(
        executable_path=IMU_EXECUTABLE_PATH,
        udp_port=IMU_UDP_PORT,
        sample_rate=IMU_SAMPLE_RATE,
        calibration_samples=IMU_CALIBRATION_SAMPLES,
        listen_ip=INTERNAL_STREAM_IP,
        recv_buffer=RECV_BUFFER,
        socket_timeout=SOCKET_TIMEOUT,
        stop_event=stop_event
    )

    # All the threads
    threading.Thread(target=listen_for_messages, daemon=True).start()
    threading.Thread(target=run_iperf3_server, daemon=True).start()
    threading.Thread(target=listener_ai_run, daemon=True).start()
    # Note: IMU listener thread is managed internally by IMUManager

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while not stop_event.is_set():
        try:
            client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client_socket.settimeout(SOCK_TOUT)

            print("Attempting to connect to host system...")
            client_socket.connect((HOST_IP, SYSINFO_PORT))
            print("Connected to host system!")

            # Start/restart IMU daemon for fresh calibration on each connection
            print("Starting IMU daemon for this connection...")
            imu_manager.start()

            while not stop_event.is_set():
                try:
                    system_info = get_system_info()
                    client_socket.sendall((json.dumps(system_info) + "\n").encode())
                    time.sleep(0.3)
                except (socket.error, BrokenPipeError) as e:
                    print(f"Connection lost: {e}")
                    break  # Break to outer loop to reconnect

        except (socket.error, socket.timeout, ConnectionRefusedError) as e:
            if not stop_event.is_set():  # Only print if not shutting down
                print(f"Connection failed: {e}. Retrying in 1 second...")
                stop_event.wait(timeout=TIME_BEFORE_RETRY)

        finally:
            if 'client_socket' in locals() and client_socket.fileno() != -1:
                client_socket.close()
            # Stop IMU daemon when connection is lost
            imu_manager.stop()

if __name__ == "__main__":
    main()
