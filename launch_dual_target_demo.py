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
import os

# =============================================================================
# Configuration
# =============================================================================

# Host scripts (local)
CURR_DIR = os.path.dirname(os.path.abspath(__file__))
HOST_HPC_SCRIPT = os.path.join(CURR_DIR, "host.py")
HOST_TOPAZ_SCRIPT = os.path.join(CURR_DIR, "host_topaz2.py")
SETUP_NAT_HOST_SCRIPT = os.path.join(CURR_DIR, "setup_nat_host.sh")

# Local sudo password for running NAT setup
HOST_SUDO_PASSWORD = "cXqW$90&10"

# HPC Target Configuration
HPC_HOST = "10.42.0.101"
HPC_USER = "sarthak"
HPC_PASSWORD = "password"
HPC_TARGET_SCRIPT = "/home/sarthak/Remote_Systems_Interact/target_hpc_ubuntu.py"
HPC_NAT_SETUP_SCRIPT = "/home/sarthak/Remote_Systems_Interact/setup_nat_target_hpc_ubuntu.sh"

# Topaz Target Configuration
TOPAZ_HOST = "10.42.1.7"
TOPAZ_USER = "user"
TOPAZ_PASSWORD = "user"
TOPAZ_TARGET_SCRIPT = "/home/user/Remote_Systems_Interact/target_topaz2.py"

# Grafana Dashboard URLs
GRAFANA_URL_HPC = "http://localhost:3000/d/debfk50vlpszerasdfd/1ed47f7?orgId=1&from=now-5m&to=now&timezone=browser&refresh=1s&kiosk=1"
GRAFANA_URL_TOPAZ = "http://localhost:3000/d/debfk50vlpszasdasdf/system-monitor-and-control-topaz-land-nav?orgId=1&from=now-5m&to=now&timezone=browser&refresh=1s&kiosk=1"

# Timing
STARTUP_DELAY = 5  # seconds between host and target startup
PING_TIMEOUT = 5   # seconds for ping verification
PING_COUNT = 3     # number of pings for verification

# =============================================================================
# Network Setup Functions
# =============================================================================

def ping_host(ip_address, count=PING_COUNT, timeout=PING_TIMEOUT):
    """Ping a host to verify connectivity."""
    print(f"  Pinging {ip_address}...")
    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout), ip_address],
            capture_output=True,
            text=True,
            timeout=timeout * count + 5
        )
        if result.returncode == 0:
            print(f"  ✓ {ip_address} is reachable")
            return True
        else:
            print(f"  ✗ {ip_address} is not reachable")
            return False
    except subprocess.TimeoutExpired:
        print(f"  ✗ Ping to {ip_address} timed out")
        return False
    except Exception as e:
        print(f"  ✗ Error pinging {ip_address}: {e}")
        return False


def run_nat_setup_on_hpc():
    """SSH to HPC target and run NAT setup script with sudo."""
    print(f"\nSetting up NAT on HPC target ({HPC_HOST})...")

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(HPC_HOST, username=HPC_USER, password=HPC_PASSWORD, timeout=10)

        # Run NAT setup script with sudo
        command = f"echo '{HPC_PASSWORD}' | sudo -S bash {HPC_NAT_SETUP_SCRIPT}"
        stdin, stdout, stderr = ssh.exec_command(command)

        # Wait for command to complete
        exit_status = stdout.channel.recv_exit_status()

        if exit_status == 0:
            print(f"  ✓ NAT setup on HPC completed successfully")
        else:
            error_output = stderr.read().decode().strip()
            print(f"  ! NAT setup on HPC returned exit code {exit_status}")
            if error_output:
                print(f"    Error: {error_output}")

        ssh.close()
        return True

    except paramiko.AuthenticationException:
        print(f"  ✗ Authentication failed for {HPC_USER}@{HPC_HOST}")
        return False
    except Exception as e:
        print(f"  ✗ Error setting up NAT on HPC: {e}")
        return False


def run_nat_setup_on_host():
    """Run NAT setup script on the host machine with sudo."""
    print(f"\nSetting up NAT on host...")

    try:
        # Use sudo -S to read password from stdin
        process = subprocess.Popen(
            ["sudo", "-S", "bash", SETUP_NAT_HOST_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        stdout, stderr = process.communicate(input=HOST_SUDO_PASSWORD + "\n", timeout=30)

        if process.returncode == 0:
            print(f"  ✓ NAT setup on host completed successfully")
            return True
        else:
            print(f"  ! NAT setup on host returned exit code {process.returncode}")
            if stderr:
                # Filter out the password prompt from stderr
                error_lines = [l for l in stderr.split('\n') if l and 'password' not in l.lower()]
                if error_lines:
                    print(f"    Error: {' '.join(error_lines)}")
            return True  # Continue even with warnings

    except subprocess.TimeoutExpired:
        print(f"  ✗ NAT setup on host timed out")
        return False
    except Exception as e:
        print(f"  ✗ Error setting up NAT on host: {e}")
        return False


def setup_network():
    """Set up network routing between host and both targets."""
    print("\n" + "=" * 60)
    print("NETWORK SETUP")
    print("=" * 60)

    # Step 1: Verify direct connection to HPC
    print("\nStep 1: Verifying direct connection to HPC target...")
    if not ping_host(HPC_HOST):
        print("\nERROR: Cannot reach HPC target. Please check:")
        print(f"  - Network cable connection to {HPC_HOST}")
        print(f"  - IP configuration on interface enx98fc84e12360")
        return False

    # Step 2: Run NAT setup on HPC first (required before host NAT)
    print("\nStep 2: Setting up NAT routing on HPC target...")
    if not run_nat_setup_on_hpc():
        print("\nERROR: Failed to set up NAT on HPC target")
        return False

    # Step 3: Run NAT setup on host
    print("\nStep 3: Setting up NAT routing on host...")
    if not run_nat_setup_on_host():
        print("\nERROR: Failed to set up NAT on host")
        return False

    # Step 4: Verify connection to Topaz through HPC
    print("\nStep 4: Verifying connection to Topaz target (via HPC)...")
    time.sleep(1)  # Brief pause for routing to take effect
    if not ping_host(TOPAZ_HOST):
        print("\nERROR: Cannot reach Topaz target through HPC. Please check:")
        print(f"  - Network configuration on HPC target")
        print(f"  - Connection between HPC and Topaz")
        return False

    print("\n✓ Network setup completed successfully!")
    return True


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
    webbrowser.open(GRAFANA_URL_HPC)

    time.sleep(1)  # Brief pause between opening tabs

    print("  Opening Topaz dashboard...")
    webbrowser.open(GRAFANA_URL_TOPAZ)

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

    # Step 1: Network setup
    if not setup_network():
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
    print(f"  - HPC:   {GRAFANA_URL_HPC[:60]}...")
    print(f"  - Topaz: {GRAFANA_URL_TOPAZ[:60]}...")
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
