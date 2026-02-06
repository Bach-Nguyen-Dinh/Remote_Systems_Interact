"""
Central configuration for all launch scripts.

All constants used by launch_nat_setup.py, launch_dual_target_demo.py,
launch_hpc_standalone_demo.py, and launch_topaz2_standalone_demo.py
are defined here for easy maintenance.
"""

import os

# =============================================================================
# Base Directory
# =============================================================================

CURR_DIR = os.path.dirname(os.path.abspath(__file__))

# =============================================================================
# Network Hosts and Credentials
# =============================================================================

# Host (local machine) sudo password
HOST_SUDO_PASSWORD = "cXqW$90&10"

# HPC Target
HPC_HOST = "10.42.0.101"
HPC_USER = "sarthak"
HPC_PASSWORD = "password"

# Topaz Target
TOPAZ_HOST = "10.42.1.7"
TOPAZ_USER = "user"
TOPAZ_PASSWORD = "user"

# =============================================================================
# Local Script Paths (host-side)
# =============================================================================

HOST_HPC_SCRIPT = os.path.join(CURR_DIR, "host.py")
HOST_TOPAZ_SCRIPT = os.path.join(CURR_DIR, "host_topaz2.py")
HOST_TOPAZ_STANDALONE_SCRIPT = os.path.join(CURR_DIR, "host_topaz2_standalone.py")
LAUNCH_NAT_SETUP_SCRIPT = os.path.join(CURR_DIR, "launch_nat_setup.py")
SETUP_NAT_HOST_SCRIPT = os.path.join(CURR_DIR, "setup_nat_host.sh")

# =============================================================================
# Remote Script Paths (target-side)
# =============================================================================

HPC_TARGET_SCRIPT = "/home/sarthak/Remote_Systems_Interact/target_hpc_ubuntu.py"
HPC_NAT_SETUP_SCRIPT = "/home/sarthak/Remote_Systems_Interact/setup_nat_target_hpc_ubuntu.sh"
TOPAZ_TARGET_SCRIPT = "/home/user/Remote_Systems_Interact/target_topaz2.py"
TOPAZ_STANDALONE_TARGET_SCRIPT = "/home/user/Remote_Systems_Interact/target_topaz2_standalone.py"

# =============================================================================
# Dashboard Wrapper Paths
# =============================================================================

DASHBOARD_WRAPPER_HPC = os.path.join(CURR_DIR, "dashboard_wrapper_hpc.html")
DASHBOARD_WRAPPER_TOPAZ = os.path.join(CURR_DIR, "dashboard_wrapper_topaz.html")

# =============================================================================
# Timing Constants
# =============================================================================

PING_TIMEOUT = 5   # seconds for ping verification
PING_COUNT = 3     # number of pings for verification
STARTUP_DELAY = 5  # seconds between host and target startup
