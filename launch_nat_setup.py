from nat_manager import NATManager
from config_launch import (
    HPC_HOST, HPC_USER, HPC_PASSWORD, HPC_NAT_SETUP_SCRIPT,
    TOPAZ_HOST, HOST_SUDO_PASSWORD, PING_TIMEOUT, PING_COUNT,
)

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