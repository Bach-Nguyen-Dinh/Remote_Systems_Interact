import os
from nat_manager import NATManager

# =============================================================================
# Configuration
# =============================================================================

# Host scripts (local)
CURR_DIR = os.path.dirname(os.path.abspath(__file__))
SETUP_NAT_HOST_SCRIPT = os.path.join(CURR_DIR, "setup_nat_host.sh")

# Local sudo password for running NAT setup
HOST_SUDO_PASSWORD = "cXqW$90&10"

# HPC Target Configuration
HPC_HOST = "10.42.0.101"
HPC_USER = "sarthak"
HPC_PASSWORD = "password"
HPC_NAT_SETUP_SCRIPT = "/home/sarthak/Remote_Systems_Interact/setup_nat_target_hpc_ubuntu.sh"

# Topaz Target Configuration
TOPAZ_HOST = "10.42.1.7"

# Timing
PING_TIMEOUT = 5   # seconds for ping verification
PING_COUNT = 3     # number of pings for verification


nat_manager = None


def main():
    global nat_manager

    nat_manager = NATManager(
        ping_count=PING_COUNT,
        ping_timeout=PING_TIMEOUT,
        hpc_host= HPC_HOST,
        hpc_user= HPC_USER,
        hpc_password=HPC_PASSWORD,
        hpc_nat_setup_script=HPC_NAT_SETUP_SCRIPT,
        topaz_host= TOPAZ_HOST,
        host_sudo_password=HOST_SUDO_PASSWORD
    )

    nat_manager.setup_network()


if __name__ == "__main__":
    main()