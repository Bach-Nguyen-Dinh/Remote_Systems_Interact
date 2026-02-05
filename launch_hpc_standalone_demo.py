#!/usr/bin/env python3
"""
HPC System Standalone Launcher
Launches host script, target script via SSH, and Grafana interface
"""

import subprocess
import time
import webbrowser
import paramiko
import sys
import os

# Configuration
CURR_DIR = os.path.dirname(os.path.abspath(__file__))
HOST_SCRIPT = os.path.join(CURR_DIR, "host.py")

# HPC Target Configuration
TARGET_HOST = "10.42.0.101"
TARGET_USER = "sarthak"
TARGET_PASSWORD = "password"
TARGET_SCRIPT = "/home/sarthak/Remote_Systems_Interact/target_hpc_ubuntu.py"

# Dashboard wrapper (sends heartbeats to demo launcher)
DASHBOARD_WRAPPER = os.path.join(CURR_DIR, "dashboard_wrapper_hpc.html")

# Timing
STARTUP_DELAY = 5  # seconds to wait between host and target startup


def launch_host_script():
    """Launch the host script as a background process"""
    print("Starting host script...")
    process = subprocess.Popen(
        ["python3", HOST_SCRIPT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"Host script started (PID: {process.pid})")
    return process


def launch_target_script_via_ssh():
    """SSH to target and launch the target script"""
    print(f"Connecting to {TARGET_USER}@{TARGET_HOST}...")

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        ssh.connect(
            TARGET_HOST,
            username=TARGET_USER,
            password=TARGET_PASSWORD,
            timeout=10
        )
        print("SSH connection established")

        print(f"Waiting {STARTUP_DELAY} seconds for host to initialize...")
        time.sleep(STARTUP_DELAY)

        print("Starting target script with sudo...")
        command = f"echo '{TARGET_PASSWORD}' | sudo -S nohup python3 {TARGET_SCRIPT} > /dev/null 2>&1 &"
        ssh.exec_command(command)

        print("Target script started")
        return ssh

    except paramiko.AuthenticationException:
        print("ERROR: Authentication failed. Check username/password.")
        sys.exit(1)
    except paramiko.SSHException as e:
        print(f"ERROR: SSH connection failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Unexpected error during SSH: {e}")
        sys.exit(1)


def open_grafana_interface():
    """Open Grafana interface in default browser"""
    print("Opening Grafana interface...")
    webbrowser.open(f"file://{DASHBOARD_WRAPPER}")
    print("Grafana interface opened in browser")


def main():
    print("=" * 60)
    print("HPC SYSTEM STANDALONE LAUNCHER")
    print("=" * 60)
    print("\nThis launcher will:")
    print("  1. Start host.py locally")
    print("  2. SSH to HPC target and start target_hpc_ubuntu.py")
    print("  3. Open Grafana dashboard in browser")
    print("")

    # Step 1: Launch host script
    host_process = launch_host_script()

    # Step 2: SSH and launch target script
    ssh_connection = launch_target_script_via_ssh()

    # Step 3: Open Grafana
    time.sleep(1)
    open_grafana_interface()

    print("\n" + "=" * 60)
    print("SYSTEM LAUNCHED SUCCESSFULLY!")
    print("=" * 60)
    print(f"\nHost Script:")
    print(f"  - host.py PID: {host_process.pid}")
    print(f"\nTarget Script:")
    print(f"  - HPC target: {TARGET_USER}@{TARGET_HOST}")
    print(f"\nGrafana Dashboard:")
    print(f"  - {DASHBOARD_WRAPPER}")
    print("\n" + "-" * 60)
    print("This script will keep running. Press Ctrl+C to exit.")
    print("Note: Stopping this script won't stop the host/target processes.")
    print("=" * 60)

    # Keep script running
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\nShutting down launcher...")
        ssh_connection.close()
        print("SSH connection closed. Host and target scripts are still running.")
        print("\nTo stop all processes, run:")
        print(f"  - On host: pkill -f 'python3.*host.py'")
        print(f"  - On HPC:  ssh {TARGET_USER}@{TARGET_HOST} 'sudo pkill -f target_hpc_ubuntu.py'")
        sys.exit(0)


if __name__ == "__main__":
    main()
