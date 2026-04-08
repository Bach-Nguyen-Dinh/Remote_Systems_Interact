#!/usr/bin/env python3
"""
Simple Demo Launcher GUI
Select and run demo scenarios with one click.
Tracks browser tab closures via heartbeat mechanism.
"""

import tkinter as tk
from tkinter import messagebox
import subprocess
import os
import signal
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

CURR_DIR = os.path.dirname(os.path.abspath(__file__))

DEMOS = {
    "Server Standalone": os.path.join(CURR_DIR, "launch_hpc_standalone_demo.py"),
    "Pulsar Standalone": os.path.join(CURR_DIR, "launch_topaz2_standalone_demo.py"),
    "Dual Target (Server + Pulsar)": os.path.join(CURR_DIR, "launch_dual_target_demo.py"),
}

CAMERA_SCRIPTS = {
    "Pulsar Standalone": os.path.join(CURR_DIR, "launch_camera_standalone.py"),
    "Dual Target (Server + Pulsar)": os.path.join(CURR_DIR, "launch_camera_dual_target_demo.py"),
}

# Network profile configuration
NETWORK_INTERFACE = "enx98fc84e12360"
TOPAZ_PROFILE = "enx98fc84e12360-static"
DEFAULT_PROFILE = "Profile 1"

# Heartbeat configuration
HEARTBEAT_PORT = 8765
HEARTBEAT_TIMEOUT = 5  # seconds without heartbeat = tab closed

# Target SSH credentials for cleanup
TARGETS = {
    "hpc": {
        "host": "10.42.0.101",
        "user": "sarthak",
        "password": "password",
        "process": "target_hpc_ubuntu.py"
    },
    "topaz": {
        "host": "10.42.1.7",
        "user": "user",
        "password": "user",
        "process_standalone": "target_topaz2_standalone.py",
        "process_dual": "target_topaz2.py"
    }
}

# Host processes to kill for each demo
HOST_PROCESSES = {
    "Server Standalone": ["host.py"],
    "Pulsar Standalone": ["host_topaz2_standalone.py"],
    "Dual Target (Server + Pulsar)": ["host.py", "host_topaz2.py"]
}


class HeartbeatHandler(BaseHTTPRequestHandler):
    """HTTP handler for heartbeat requests from browser tabs."""

    launcher = None  # Will be set by DemoLauncher

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        """Handle heartbeat GET requests."""
        if self.path.startswith("/heartbeat"):
            # Extract tab ID from query string
            tab_id = "default"
            if "?" in self.path:
                query = self.path.split("?")[1]
                for param in query.split("&"):
                    if param.startswith("tab="):
                        tab_id = param.split("=")[1]

            if self.launcher:
                self.launcher.receive_heartbeat(tab_id)

            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()


class DemoLauncher:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Demo Launcher")
        self.root.geometry("480x450")
        self.root.resizable(False, False)

        self.current_process = None
        self.current_demo = None

        # Camera tracking
        self.camera_process = None
        self.camera_demo = None

        # Heartbeat tracking
        self.heartbeats = {}  # tab_id -> last_heartbeat_time
        self.expected_tabs = []  # tabs we expect for current demo
        self.heartbeat_server = None
        self.heartbeat_thread = None
        self.monitor_thread = None
        self.running = True

        self.build_ui()
        self.start_heartbeat_server()

    def build_ui(self):
        # Title
        title = tk.Label(self.root, text="Demo Launcher", font=("Arial", 16, "bold"))
        title.pack(pady=(15, 10))

        self.buttons = {}
        self.camera_buttons = {}

        # --- Server Standalone (no camera - camera runs on Topaz only) ---
        hpc_frame = tk.LabelFrame(
            self.root, text=" Server Standalone ",
            font=("Arial", 11, "bold"), padx=10, pady=8
        )
        hpc_frame.pack(fill="x", padx=15, pady=4)

        btn_hpc = tk.Button(
            hpc_frame, text="Launch Interface", font=("Arial", 11), height=1,
            command=lambda: self.launch_demo("Server Standalone", DEMOS["Server Standalone"])
        )
        btn_hpc.pack(fill="x")
        self.buttons["Server Standalone"] = btn_hpc

        # --- Topaz Standalone (demo + camera side by side) ---
        topaz_frame = tk.LabelFrame(
            self.root, text=" Topaz Standalone ",
            font=("Arial", 11, "bold"), padx=10, pady=8
        )
        topaz_frame.pack(fill="x", padx=15, pady=4)

        topaz_inner = tk.Frame(topaz_frame)
        topaz_inner.pack(fill="x")

        btn_topaz = tk.Button(
            topaz_inner, text="Launch Interface", font=("Arial", 11), height=1,
            command=lambda: self.launch_demo("Pulsar Standalone", DEMOS["Pulsar Standalone"])
        )
        btn_topaz.pack(side=tk.LEFT, expand=True, fill="x", padx=(0, 3))
        self.buttons["Pulsar Standalone"] = btn_topaz

        btn_topaz_cam = tk.Button(
            topaz_inner, text="Launch Camera", font=("Arial", 11), height=1,
            command=lambda: self.launch_camera("Pulsar Standalone")
        )
        btn_topaz_cam.pack(side=tk.LEFT, expand=True, fill="x", padx=(3, 0))
        self.camera_buttons["Pulsar Standalone"] = btn_topaz_cam

        # --- Dual Target (demo + camera side by side) ---
        dual_frame = tk.LabelFrame(
            self.root, text=" Dual Target (Server + Pulsar) ",
            font=("Arial", 11, "bold"), padx=10, pady=8
        )
        dual_frame.pack(fill="x", padx=15, pady=4)

        dual_inner = tk.Frame(dual_frame)
        dual_inner.pack(fill="x")

        btn_dual = tk.Button(
            dual_inner, text="Launch Interface", font=("Arial", 11), height=1,
            command=lambda: self.launch_demo("Dual Target (Server + Pulsar)", DEMOS["Dual Target (Server + Pulsar)"])
        )
        btn_dual.pack(side=tk.LEFT, expand=True, fill="x", padx=(0, 3))
        self.buttons["Dual Target (Server + Pulsar)"] = btn_dual

        btn_dual_cam = tk.Button(
            dual_inner, text="Launch Camera", font=("Arial", 11), height=1,
            command=lambda: self.launch_camera("Dual Target (Server + Pulsar)")
        )
        btn_dual_cam.pack(side=tk.LEFT, expand=True, fill="x", padx=(3, 0))
        self.camera_buttons["Dual Target (Server + Pulsar)"] = btn_dual_cam

        # --- Status Section ---
        status_frame = tk.Frame(self.root)
        status_frame.pack(pady=(15, 5), padx=25, anchor="w")

        # Demo status row
        demo_row = tk.Frame(status_frame)
        demo_row.pack(anchor="w")

        self.status_indicator = tk.Canvas(demo_row, width=16, height=16, highlightthickness=0)
        self.status_indicator.pack(side=tk.LEFT, padx=(0, 6))
        self.indicator_circle = self.status_indicator.create_oval(2, 2, 14, 14, fill="gray")

        self.status_label = tk.Label(demo_row, text="Demo: Not running", font=("Arial", 10))
        self.status_label.pack(side=tk.LEFT)

        # Camera status row
        cam_row = tk.Frame(status_frame)
        cam_row.pack(anchor="w", pady=(4, 0))

        self.cam_status_indicator = tk.Canvas(cam_row, width=16, height=16, highlightthickness=0)
        self.cam_status_indicator.pack(side=tk.LEFT, padx=(0, 6))
        self.cam_indicator_circle = self.cam_status_indicator.create_oval(2, 2, 14, 14, fill="gray")

        self.cam_status_label = tk.Label(cam_row, text="Camera: Not running", font=("Arial", 10))
        self.cam_status_label.pack(side=tk.LEFT)

        # --- Stop Buttons ---
        stop_frame = tk.Frame(self.root)
        stop_frame.pack(pady=(8, 15))

        self.stop_btn = tk.Button(
            stop_frame, text="Stop Demo", font=("Arial", 10, "bold"),
            width=16, bg="#d9534f", fg="white", state=tk.DISABLED,
            command=self.stop_demo
        )
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        self.stop_cam_btn = tk.Button(
            stop_frame, text="Stop Camera", font=("Arial", 10, "bold"),
            width=16, bg="#d9534f", fg="white", state=tk.DISABLED,
            command=self.stop_camera
        )
        self.stop_cam_btn.pack(side=tk.LEFT, padx=5)

    def start_heartbeat_server(self):
        """Start the heartbeat HTTP server in a background thread."""
        HeartbeatHandler.launcher = self

        try:
            self.heartbeat_server = HTTPServer(("127.0.0.1", HEARTBEAT_PORT), HeartbeatHandler)
            self.heartbeat_server.timeout = 1  # handle_request() returns after 1s if no request
            self.heartbeat_thread = threading.Thread(target=self._run_heartbeat_server, daemon=True)
            self.heartbeat_thread.start()
            print(f"Heartbeat server started on port {HEARTBEAT_PORT}")
        except Exception as e:
            print(f"Failed to start heartbeat server: {e}")

    def _run_heartbeat_server(self):
        """Run the heartbeat server."""
        while self.running:
            self.heartbeat_server.handle_request()

    def receive_heartbeat(self, tab_id):
        """Record a heartbeat from a browser tab."""
        self.heartbeats[tab_id] = time.time()

    def start_monitoring(self):
        """Start monitoring heartbeats for tab closures."""
        self.monitor_thread = threading.Thread(target=self._monitor_heartbeats, daemon=True)
        self.monitor_thread.start()

    def _monitor_heartbeats(self):
        """Monitor heartbeats and stop demo when tabs are closed."""
        # Phase 1: Wait for all expected tabs to send at least one heartbeat
        print("Waiting for browser tabs to connect...")
        max_wait = 60  # Max 60 seconds to wait for tabs to open
        waited = 0

        while self.running and self.current_demo and waited < max_wait:
            tabs_connected = sum(1 for tab_id in self.expected_tabs if tab_id in self.heartbeats)
            if tabs_connected == len(self.expected_tabs):
                print(f"All {tabs_connected} tab(s) connected. Monitoring for closures.")
                break
            time.sleep(1)
            waited += 1

        if waited >= max_wait:
            print("Timeout waiting for tabs - monitoring anyway")

        # Phase 2: Monitor for tab closures (heartbeats stopping)
        while self.running and self.current_demo:
            if not self.expected_tabs:
                time.sleep(1)
                continue

            now = time.time()
            tabs_alive = 0

            for tab_id in self.expected_tabs:
                last_beat = self.heartbeats.get(tab_id, 0)
                # Only count as alive if we've received a heartbeat recently
                if last_beat > 0 and (now - last_beat) < HEARTBEAT_TIMEOUT:
                    tabs_alive += 1

            # For dual target: wait for ALL tabs to close
            # For single: wait for the one tab to close
            # Only trigger if we had tabs connected and now all are gone
            all_tabs_were_connected = all(tab_id in self.heartbeats for tab_id in self.expected_tabs)
            if tabs_alive == 0 and all_tabs_were_connected:
                # All tabs closed - stop demo
                self.root.after(0, self._auto_stop_demo)
                break

            time.sleep(1)

    def _auto_stop_demo(self):
        """Called from monitor thread to stop demo (runs in main thread)."""
        if self.current_demo:
            print(f"All browser tabs closed - stopping {self.current_demo}")
            self.stop_demo()

    def switch_network_profile(self, demo_name):
        """Switch network profile based on selected demo."""
        if demo_name == "Pulsar Standalone":
            profile = TOPAZ_PROFILE
        else:
            profile = DEFAULT_PROFILE

        try:
            subprocess.run(
                ["nmcli", "connection", "up", profile],
                check=True,
                capture_output=True,
                timeout=10
            )
        except subprocess.CalledProcessError as e:
            messagebox.showwarning("Network", f"Failed to switch to profile '{profile}':\n{e.stderr.decode()}")
        except Exception as e:
            messagebox.showwarning("Network", f"Network switch error: {e}")

    def launch_demo(self, name, script):
        # Stop current demo if running
        if self.current_process:
            self.stop_demo()

        # Switch network profile before launching
        self.switch_network_profile(name)

        # Clear heartbeats and set expected tabs
        self.heartbeats = {}
        if name == "Dual Target (Server + Pulsar)":
            self.expected_tabs = ["hpc", "topaz"]
        elif name == "Server Standalone":
            self.expected_tabs = ["hpc"]
        else:  # Topaz Standalone
            self.expected_tabs = ["topaz"]

        # Launch new demo
        try:
            self.current_process = subprocess.Popen(
                ["python3", script],
                preexec_fn=os.setsid  # Create new process group for clean termination
            )
            self.current_demo = name
            self.update_status(name, running=True)

            # Highlight active button
            for btn_name, btn in self.buttons.items():
                if btn_name == name:
                    btn.config(bg="#5cb85c", fg="white")
                else:
                    btn.config(bg="#d9d9d9", fg="black")

            # Start monitoring heartbeats
            self.start_monitoring()

        except Exception as e:
            messagebox.showerror("Error", f"Failed to Launch Interface:\n{e}")

    def launch_camera(self, demo_name):
        """Launch camera for the given demo configuration."""
        if self.camera_process:
            self.stop_camera()

        # Switch network profile (camera needs the right network)
        self.switch_network_profile(demo_name)

        # Dual Target: Topaz is only reachable via HPC NAT router,
        # so ensure NAT is set up before trying to SSH to Topaz.
        if demo_name == "Dual Target (Server + Pulsar)":
            nat_script = os.path.join(CURR_DIR, "launch_nat_setup.py")
            result = subprocess.run(
                ["python3", nat_script],
                capture_output=True,
                timeout=30
            )
            if result.returncode != 0:
                messagebox.showerror(
                    "Network",
                    f"NAT setup failed. Cannot reach Topaz.\n{result.stderr.decode()}"
                )
                return

        script = CAMERA_SCRIPTS[demo_name]

        try:
            self.camera_process = subprocess.Popen(
                ["python3", script],
                preexec_fn=os.setsid
            )
            self.camera_demo = demo_name
            self.update_camera_status(demo_name, running=True)

            # Highlight active camera button (blue to distinguish from green demo)
            for btn_name, btn in self.camera_buttons.items():
                if btn_name == demo_name:
                    btn.config(bg="#337ab7", fg="white")
                else:
                    btn.config(bg="#d9d9d9", fg="black")

            # Monitor camera process for self-exit
            self._check_camera_process()

        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch camera:\n{e}")

    def stop_camera(self):
        """Stop the camera process."""
        if self.camera_process:
            try:
                os.killpg(os.getpgid(self.camera_process.pid), signal.SIGTERM)
                self.camera_process.wait(timeout=5)
            except:
                try:
                    os.killpg(os.getpgid(self.camera_process.pid), signal.SIGKILL)
                except:
                    pass

            self.camera_process = None
            self.camera_demo = None
            self.update_camera_status(None, running=False)

            for btn in self.camera_buttons.values():
                btn.config(bg="#d9d9d9", fg="black")

    def _check_camera_process(self):
        """Periodically check if camera process has exited on its own."""
        if self.camera_process and self.camera_process.poll() is not None:
            # Camera exited on its own
            self.camera_process = None
            self.camera_demo = None
            self.update_camera_status(None, running=False)
            for btn in self.camera_buttons.values():
                btn.config(bg="#d9d9d9", fg="black")
        elif self.camera_process:
            self.root.after(1000, self._check_camera_process)

    def kill_host_processes(self, demo_name):
        """Kill local host processes for the given demo."""
        processes = HOST_PROCESSES.get(demo_name, [])
        for proc_name in processes:
            try:
                # Use SIGKILL (-9) to force kill
                subprocess.run(
                    ["pkill", "-9", "-f", proc_name],
                    capture_output=True,
                    timeout=5
                )
                print(f"Killed host process: {proc_name}")
            except Exception as e:
                print(f"Error killing {proc_name}: {e}")

        # Also kill any processes holding the ports used by host scripts
        # HPC uses: 5000 (Flask), 12345 (metrics), 55555 (images), 29102 (nettest)
        # Topaz Standalone uses: 5001 (Flask), 12345 (metrics), 55555 (data), 8080 (images)
        # Dual Topaz uses: 5001 (Flask), 12346 (metrics), 55556 (images), 29103 (nettest)
        ports_to_free = []
        if demo_name == "Server Standalone":
            ports_to_free = [5000, 12345, 55555, 29102]
        elif demo_name == "Pulsar Standalone":
            ports_to_free = [5001, 12345, 55555, 8080]
        elif demo_name == "Dual Target (Server + Pulsar)":
            ports_to_free = [5000, 5001, 12345, 12346, 55555, 55556, 29102, 29103, 8080]

        for port in ports_to_free:
            try:
                # Find and kill process using this port
                result = subprocess.run(
                    ["fuser", "-k", f"{port}/tcp"],
                    capture_output=True,
                    timeout=5
                )
                if result.returncode == 0:
                    print(f"Freed port {port}")
            except Exception as e:
                pass  # Port might not be in use, that's fine

        # Brief pause to let processes fully terminate
        time.sleep(0.5)

    def kill_target_processes(self, demo_name):
        """SSH to targets and kill target processes and their children (iperf3, etc)."""
        try:
            import paramiko
        except ImportError:
            print("paramiko not available - cannot kill remote targets")
            return

        targets_to_kill = []

        if demo_name == "Server Standalone":
            targets_to_kill.append(("hpc", TARGETS["hpc"]["process"], [5201]))  # iperf3 default port
        elif demo_name == "Pulsar Standalone":
            targets_to_kill.append(("topaz", TARGETS["topaz"]["process_standalone"], [8888, 8889, 5201]))  # AI UDP + IMU UDP + iperf3
        elif demo_name == "Dual Target (Server + Pulsar)":
            targets_to_kill.append(("hpc", TARGETS["hpc"]["process"], [5201]))
            targets_to_kill.append(("topaz", TARGETS["topaz"]["process_dual"], [8888, 8889, 5201]))

        for target_key, process_name, ports in targets_to_kill:
            target = TARGETS[target_key]
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(
                    target["host"],
                    username=target["user"],
                    password=target["password"],
                    timeout=5
                )

                # Build list of kill commands
                commands = [
                    f"pkill -9 -f '{process_name}'",
                    "pkill -9 iperf3",
                    "pkill -9 -f 'ai_tool'",  # AI tool processes
                    "pkill -9 -f 'iim42652'",  # IMU process (old name)
                    "pkill -9 -f 'ai_server.py'",  # AI smoke detection
                    "pkill -9 -f 'ai_ship.py'",  # AI ship detection
                    "pkill -9 -f 'topaz_imu/main'",  # IMU daemon
                    "pkill -9 -f 'RSS'",  # Small object detection
                ]

                # Add fuser commands to free ports
                for port in ports:
                    commands.append(f"fuser -k {port}/tcp 2>/dev/null || true")
                    commands.append(f"fuser -k {port}/udp 2>/dev/null || true")

                # Execute all commands
                for cmd in commands:
                    full_cmd = f"echo '{target['password']}' | sudo -S {cmd}"
                    ssh.exec_command(full_cmd)
                    time.sleep(0.1)

                # Wait a bit for processes to die
                time.sleep(0.5)

                ssh.close()
                print(f"Killed target process on {target['host']}: {process_name} (and child processes)")
            except Exception as e:
                print(f"Error killing target on {target['host']}: {e}")

    def stop_demo(self):
        if self.current_process:
            demo_name = self.current_demo

            # Show stopping state and disable buttons while cleanup runs
            self.update_status(demo_name, stopping=True)
            self.stop_btn.config(state=tk.DISABLED)
            for btn in self.buttons.values():
                btn.config(state=tk.DISABLED)

            # Run full cleanup in background thread to avoid freezing the UI
            threading.Thread(
                target=self._stop_demo_cleanup,
                args=(demo_name,),
                daemon=True
            ).start()

    def _stop_demo_cleanup(self, demo_name):
        """Run all demo cleanup steps, then update UI when done."""
        # Kill the launcher script process group
        try:
            os.killpg(os.getpgid(self.current_process.pid), signal.SIGTERM)
            self.current_process.wait(timeout=5)
        except:
            try:
                os.killpg(os.getpgid(self.current_process.pid), signal.SIGKILL)
            except:
                pass

        # Kill host processes and free ports (synchronous)
        if demo_name:
            self.kill_host_processes(demo_name)
            # Kill target processes (synchronous - wait for SSH cleanup to finish)
            self.kill_target_processes(demo_name)

        # All cleanup done - update UI from main thread
        self.root.after(0, self._stop_demo_finalize)

    def _stop_demo_finalize(self):
        """Called on main thread after all cleanup is done."""
        self.current_process = None
        self.current_demo = None
        self.expected_tabs = []
        self.heartbeats = {}
        self.update_status(None, running=False)

        # Reset button colors and re-enable
        for btn in self.buttons.values():
            btn.config(bg="#d9d9d9", fg="black", state=tk.NORMAL)

    def update_status(self, demo_name, running=False, stopping=False):
        if stopping:
            self.status_indicator.itemconfig(self.indicator_circle, fill="orange")
            self.status_label.config(text=f"Stopping: {demo_name}...")
            self.stop_btn.config(state=tk.DISABLED)
        elif running:
            self.status_indicator.itemconfig(self.indicator_circle, fill="green")
            self.status_label.config(text=f"Demo: {demo_name}")
            self.stop_btn.config(state=tk.NORMAL)
        else:
            self.status_indicator.itemconfig(self.indicator_circle, fill="gray")
            self.status_label.config(text="Demo: Not running")
            self.stop_btn.config(state=tk.DISABLED)

    def update_camera_status(self, demo_name, running):
        if running:
            self.cam_status_indicator.itemconfig(self.cam_indicator_circle, fill="#337ab7")
            self.cam_status_label.config(text=f"Camera: {demo_name}")
            self.stop_cam_btn.config(state=tk.NORMAL)
        else:
            self.cam_status_indicator.itemconfig(self.cam_indicator_circle, fill="gray")
            self.cam_status_label.config(text="Camera: Not running")
            self.stop_cam_btn.config(state=tk.DISABLED)

    def on_close(self):
        running_items = []
        if self.current_process:
            running_items.append("a demo")
        if self.camera_process:
            running_items.append("a camera")

        if running_items:
            msg = f"Still running: {' and '.join(running_items)}. Stop and exit?"
            if not messagebox.askyesno("Confirm Exit", msg):
                return
            if self.current_process:
                self.stop_demo()
            if self.camera_process:
                self.stop_camera()

        self.running = False
        if self.heartbeat_server:
            self.heartbeat_server.server_close()

        self.root.destroy()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()


if __name__ == "__main__":
    app = DemoLauncher()
    app.run()
