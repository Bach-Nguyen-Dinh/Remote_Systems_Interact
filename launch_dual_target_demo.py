#!/usr/bin/env python3
"""
Dual Target System Launcher
Launches host scripts for both HPC and Topaz systems, sets up network routing,
and starts target scripts via SSH on remote machines.

Architecture:
  Host (this machine)
    ├── host.py          → connects to HPC target (10.42.0.101)
    └── host_topaz2.py   → connects to Topaz target (10.42.1.7)

  HPC Target (10.42.0.101) - acts as router to Topaz
    └── target_hpc_ubuntu.py

  Topaz Target (10.42.1.7)
    └── target_topaz2.py
"""

import subprocess
import time
import webbrowser
import paramiko
import sys

from config_launch import (
    HOST_HPC_SCRIPT, HOST_TOPAZ_SCRIPT, LAUNCH_NAT_SETUP_SCRIPT,
    HPC_HOST, HPC_USER, HPC_PASSWORD, HPC_TARGET_SCRIPT,
    TOPAZ_HOST, TOPAZ_USER, TOPAZ_PASSWORD, TOPAZ_TARGET_SCRIPT,
    DASHBOARD_WRAPPER_HPC, DASHBOARD_WRAPPER_TOPAZ, STARTUP_DELAY,
)

# =============================================================================
# Host Script Functions
# =============================================================================

def launch_host_script(script_path, name):
    """Launch a host script as a background process."""
    print(f"  Starting {name}...")
    process = subprocess.Popen(
        ["python3", script_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"  ✓ {name} started (PID: {process.pid})")
    return process


def launch_host_scripts():
    """Launch both host scripts."""
    print("\n" + "=" * 60)
    print("STARTING HOST SCRIPTS")
    print("=" * 60)

    processes = {}

    processes['hpc'] = launch_host_script(HOST_HPC_SCRIPT, "host.py (HPC)")
    processes['topaz'] = launch_host_script(HOST_TOPAZ_SCRIPT, "host_topaz2.py (Topaz)")

    print(f"\nWaiting {STARTUP_DELAY} seconds for hosts to initialize...")
    time.sleep(STARTUP_DELAY)

    return processes


# =============================================================================
# Target Script Functions
# =============================================================================

def launch_target_script_via_ssh(host, user, password, script_path, name):
    """SSH to target and launch the target script with sudo."""
    print(f"  Connecting to {user}@{host}...")

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=user, password=password, timeout=10)

        print(f"  ✓ SSH connection established to {name}")

        # Launch target script with sudo in background
        print(f"  Starting {name} target script with sudo...")
        command = f"echo '{password}' | sudo -S nohup python3 {script_path} > /dev/null 2>&1 &"
        ssh.exec_command(command)

        print(f"  ✓ {name} target script started")

        return ssh

    except paramiko.AuthenticationException:
        print(f"  ✗ Authentication failed for {user}@{host}")
        return None
    except paramiko.SSHException as e:
        print(f"  ✗ SSH connection failed to {host}: {e}")
        return None
    except Exception as e:
        print(f"  ✗ Unexpected error connecting to {host}: {e}")
        return None


def launch_target_scripts():
    """Launch target scripts on both remote machines via SSH."""
    print("\n" + "=" * 60)
    print("STARTING TARGET SCRIPTS VIA SSH")
    print("=" * 60)

    ssh_connections = {}

    # Launch HPC target first (it's the router)
    ssh_connections['hpc'] = launch_target_script_via_ssh(
        HPC_HOST, HPC_USER, HPC_PASSWORD, HPC_TARGET_SCRIPT, "HPC"
    )

    if ssh_connections['hpc'] is None:
        print("\nERROR: Failed to start HPC target script")
        return None

    time.sleep(2)  # Brief pause between target launches

    # Launch Topaz target
    ssh_connections['topaz'] = launch_target_script_via_ssh(
        TOPAZ_HOST, TOPAZ_USER, TOPAZ_PASSWORD, TOPAZ_TARGET_SCRIPT, "Topaz"
    )

    if ssh_connections['topaz'] is None:
        print("\nWARNING: Failed to start Topaz target script")
        # Continue anyway - HPC is running

    return ssh_connections


# =============================================================================
# Grafana Dashboard Functions
# =============================================================================

def open_grafana_dashboards():
    """Open Grafana dashboards in the default browser."""
    print("\n" + "=" * 60)
    print("OPENING GRAFANA DASHBOARDS")
    print("=" * 60)

    print("  Opening HPC dashboard...")
    webbrowser.open(f"file://{DASHBOARD_WRAPPER_HPC}")

    time.sleep(1)  # Brief pause between opening tabs

    print("  Opening Topaz dashboard...")
    webbrowser.open(f"file://{DASHBOARD_WRAPPER_TOPAZ}")

    print("  ✓ Dashboards opened in browser")


# =============================================================================
# Main
# =============================================================================

def main():
    print("=" * 60)
    print("DUAL TARGET SYSTEM LAUNCHER")
    print("=" * 60)
    print("\nThis launcher will:")
    print("  1. Set up network routing (NAT) between host and targets")
    print("  2. Start host.py and host_topaz2.py locally")
    print("  3. SSH to targets and start target scripts")
    print("  4. Open Grafana dashboards in browser")
    print("")

    # Step 1: Network setup via launch_nat_setup.py
    result = subprocess.run([sys.executable, LAUNCH_NAT_SETUP_SCRIPT])
    if result.returncode != 0:
        print("\n" + "=" * 60)
        print("LAUNCH ABORTED - Network setup failed")
        print("=" * 60)
        sys.exit(1)

    # Step 2: Launch host scripts
    host_processes = launch_host_scripts()

    # Step 3: Launch target scripts via SSH
    ssh_connections = launch_target_scripts()

    # Step 4: Open Grafana dashboards
    time.sleep(2)  # Wait for targets to connect
    open_grafana_dashboards()

    # Summary
    print("\n" + "=" * 60)
    print("SYSTEM LAUNCHED SUCCESSFULLY!")
    print("=" * 60)
    print(f"\nHost Scripts:")
    print(f"  - host.py (HPC)      PID: {host_processes['hpc'].pid}")
    print(f"  - host_topaz2.py     PID: {host_processes['topaz'].pid}")
    print(f"\nTarget Scripts:")
    print(f"  - HPC target:        {HPC_USER}@{HPC_HOST}")
    print(f"  - Topaz target:      {TOPAZ_USER}@{TOPAZ_HOST}")
    print(f"\nGrafana Dashboards:")
    print(f"  - HPC:   {DASHBOARD_WRAPPER_HPC}")
    print(f"  - Topaz: {DASHBOARD_WRAPPER_TOPAZ}")
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

        # Close SSH connections
        if ssh_connections:
            for name, ssh in ssh_connections.items():
                if ssh:
                    ssh.close()
                    print(f"  Closed SSH connection to {name}")

        print("\nSSH connections closed.")
        print("Host and target scripts are still running.")
        print("\nTo stop all processes, run:")
        print(f"  - On host: pkill -f 'python3.*host.py' && pkill -f 'python3.*host_topaz2.py'")
        print(f"  - On HPC:  ssh {HPC_USER}@{HPC_HOST} 'sudo pkill -f target_hpc_ubuntu.py'")
        print(f"  - On Topaz: ssh {TOPAZ_USER}@{TOPAZ_HOST} 'sudo pkill -f target_topaz2.py'")
        sys.exit(0)


if __name__ == "__main__":
    main()
