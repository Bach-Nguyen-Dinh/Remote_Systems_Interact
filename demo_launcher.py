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
    "HPC Standalone": os.path.join(CURR_DIR, "launch_hpc_standalone_demo.py"),
    "Topaz Standalone": os.path.join(CURR_DIR, "launch_topaz2_standalone_demo.py"),
    "Dual Target (HPC + Topaz)": os.path.join(CURR_DIR, "launch_dual_target_demo.py"),
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
    "HPC Standalone": ["host.py"],
    "Topaz Standalone": ["host_topaz2_standalone.py"],
    "Dual Target (HPC + Topaz)": ["host.py", "host_topaz2.py"]
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
        self.root.geometry("400x350")
        self.root.resizable(False, False)

        self.current_process = None
        self.current_demo = None

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
        title = tk.Label(self.root, text="Select Demo", font=("Arial", 16, "bold"))
        title.pack(pady=15)

        # Demo buttons
        self.buttons = {}
        for name, script in DEMOS.items():
            btn = tk.Button(
                self.root,
                text=name,
                font=("Arial", 12),
                width=30,
                height=2,
                command=lambda n=name, s=script: self.launch_demo(n, s)
            )
            btn.pack(pady=5)
            self.buttons[name] = btn

        # Status
        self.status_frame = tk.Frame(self.root)
        self.status_frame.pack(pady=20)

        self.status_indicator = tk.Canvas(self.status_frame, width=20, height=20)
        self.status_indicator.pack(side=tk.LEFT, padx=5)
        self.indicator_circle = self.status_indicator.create_oval(2, 2, 18, 18, fill="gray")

        self.status_label = tk.Label(self.status_frame, text="No demo running", font=("Arial", 10))
        self.status_label.pack(side=tk.LEFT)

        # Stop button
        self.stop_btn = tk.Button(
            self.root,
            text="Stop Current Demo",
            font=("Arial", 11),
            width=20,
            bg="#d9534f",
            fg="white",
            state=tk.DISABLED,
            command=self.stop_demo
        )
        self.stop_btn.pack(pady=10)

    def start_heartbeat_server(self):
        """Start the heartbeat HTTP server in a background thread."""
        HeartbeatHandler.launcher = self

        try:
            self.heartbeat_server = HTTPServer(("127.0.0.1", HEARTBEAT_PORT), HeartbeatHandler)
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
        if demo_name == "Topaz Standalone":
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
        if name == "Dual Target (HPC + Topaz)":
            self.expected_tabs = ["hpc", "topaz"]
        elif name == "HPC Standalone":
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
            messagebox.showerror("Error", f"Failed to launch demo:\n{e}")

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
        if demo_name == "HPC Standalone":
            ports_to_free = [5000, 12345, 55555, 29102]
        elif demo_name == "Topaz Standalone":
            ports_to_free = [5001, 12345, 55555, 8080]
        elif demo_name == "Dual Target (HPC + Topaz)":
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

        if demo_name == "HPC Standalone":
            targets_to_kill.append(("hpc", TARGETS["hpc"]["process"], [5201]))  # iperf3 default port
        elif demo_name == "Topaz Standalone":
            targets_to_kill.append(("topaz", TARGETS["topaz"]["process_standalone"], [8888, 8889, 5201]))  # AI UDP + IMU UDP + iperf3
        elif demo_name == "Dual Target (HPC + Topaz)":
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

            # First kill the launcher script process group
            try:
                os.killpg(os.getpgid(self.current_process.pid), signal.SIGTERM)
                self.current_process.wait(timeout=5)
            except:
                try:
                    os.killpg(os.getpgid(self.current_process.pid), signal.SIGKILL)
                except:
                    pass

            # Kill host processes
            if demo_name:
                self.kill_host_processes(demo_name)
                # Kill target processes in background thread to avoid blocking UI
                threading.Thread(
                    target=self.kill_target_processes,
                    args=(demo_name,),
                    daemon=True
                ).start()

            self.current_process = None
            self.current_demo = None
            self.expected_tabs = []
            self.heartbeats = {}
            self.update_status(None, running=False)

            # Reset button colors
            for btn in self.buttons.values():
                btn.config(bg="#d9d9d9", fg="black")

    def update_status(self, demo_name, running):
        if running:
            self.status_indicator.itemconfig(self.indicator_circle, fill="green")
            self.status_label.config(text=f"Running: {demo_name}")
            self.stop_btn.config(state=tk.NORMAL)
        else:
            self.status_indicator.itemconfig(self.indicator_circle, fill="gray")
            self.status_label.config(text="No demo running")
            self.stop_btn.config(state=tk.DISABLED)

    def on_close(self):
        self.running = False
        if self.heartbeat_server:
            self.heartbeat_server.shutdown()
        if self.current_process:
            if messagebox.askyesno("Confirm Exit", "A demo is running. Stop it and exit?"):
                self.stop_demo()
                self.root.destroy()
        else:
            self.root.destroy()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()


if __name__ == "__main__":
    app = DemoLauncher()
    app.run()
