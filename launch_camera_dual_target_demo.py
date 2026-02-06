#!/usr/bin/env python3
"""
Camera in Dual target setup Launcher
Launches camera scripts on host and TOPAZ via SSH
"""

import subprocess
import time
import paramiko
import sys
import os

HOST_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "display_demo.sh")
TARGET_USER = "user"
TARGET_HOST = "10.42.1.7"
TARGET_PASSWORD = "user"
STARTUP_DELAY = 5.0
TARGET_SCRIPT = "/home/user/Remote_Systems_Interact/setup_webcam.sh"

def launch_host_script():
    """Launch the host script as a background process"""
    print("Starting host script...")
    # Use DEVNULL instead of PIPE to prevent buffer blocking
    # When using PIPE, the buffer fills up (~64KB) and blocks the host process
    process = subprocess.Popen(
        ["bash", HOST_SCRIPT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"Host script started (PID: {process.pid})")
    return process

def launch_target_script_via_ssh():
    """SSH to target and launch the target script"""
    print(f"Connecting to {TARGET_USER}@{TARGET_HOST}...")
    
    try:
        # Create SSH client
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Connect with password
        ssh.connect(
            TARGET_HOST,
            username=TARGET_USER,
            password=TARGET_PASSWORD,
            timeout=10
        )
        print("SSH connection established")
        
        # Wait for host to be ready
        print(f"Waiting {STARTUP_DELAY} seconds for host to initialize...")
        time.sleep(STARTUP_DELAY)
        
        # Launch target script with sudo in background (nohup keeps it running after SSH closes)
        print("Starting target script with sudo...")
        # Use echo to pipe password to sudo -S (read password from stdin)
        command = f"echo '{TARGET_PASSWORD}' | sudo -S nohup bash {TARGET_SCRIPT} > /dev/null 2>&1 &"
        ssh.exec_command(command)
        
        print("Target script started")
        
        # Keep SSH connection object around (don't close it immediately)
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


def main():
    print("=" * 60)
    print("CAMERA LAUNCHER")
    print("=" * 60)
    
    # Step 1: Launch host script
    host_process = launch_host_script()
    
    # Step 2: SSH and launch target script
    ssh_connection = launch_target_script_via_ssh()
    
    print("\n" + "=" * 60)
    print("System launched successfully!")
    print("=" * 60)
    print(f"Host script PID: {host_process.pid}")
    print(f"Target script: Running on {TARGET_HOST}")
    print("\nThis script will keep running. Press Ctrl+C to exit.")
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
        sys.exit(0)


if __name__ == "__main__":
    main()