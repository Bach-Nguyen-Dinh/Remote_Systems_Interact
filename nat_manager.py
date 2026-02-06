import subprocess
import time
import paramiko
import os

# =============================================================================
# Configuration
# =============================================================================

# Local sudo password for running NAT setup
HOST_SUDO_PASSWORD = "cXqW$90&10"

# Timing
PING_TIMEOUT = 5   # seconds for ping verification
PING_COUNT = 3     # number of pings for verification
SSH_TIMEOUT = 10
NAT_SETUP_TIMEOUT = 30
WAIT_ROUTE_EFFECT = 1


# =============================================================================
# Network Setup Functions
# =============================================================================

class NATManager:
    def __init__(
        self,
        ping_count: int=PING_COUNT,
        ping_timeout: int=PING_TIMEOUT,
        hpc_host: str = "10.42.0.101", 
        hpc_user: str = "sarthak", 
        hpc_password: str = "password", 
        hpc_nat_setup_script: str = "/home/sarthak/Remote_Systems_Interact/setup_nat_target_hpc_ubuntu.sh",
        topaz_host: str = "10.42.1.7",
        host_sudo_password: str = HOST_SUDO_PASSWORD 
    ):
        self.ping_count = ping_count
        self.ping_timeout = ping_timeout
        self.hpc_host = hpc_host
        self.hpc_user = hpc_user
        self.hpc_password = hpc_password
        self.hpc_nat_setup_script = hpc_nat_setup_script
        self.topaz_host = topaz_host
        self.host_sudo_password = host_sudo_password
        # Fix the missing attribute from your original code
        self.setup_nat_host_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "setup_nat_host.sh"
        )


    def ping_host(self, ip_address) -> bool:
        """Ping a host to verify connectivity."""
        print(f"  Pinging {ip_address}...")
        try:
            result = subprocess.run(
                ["ping", "-c", str(self.ping_count), "-W", str(self.ping_timeout), ip_address],
                capture_output=True,
                text=True,
                timeout=self.ping_timeout * self.ping_count + 5
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


    def run_nat_setup_on_hpc(self) -> bool:
        """SSH to HPC target and run NAT setup script with sudo."""
        print(f"\nSetting up NAT on HPC target ({self.hpc_host})...")

        ssh = None
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(self.hpc_host, username=self.hpc_user, password=self.hpc_password, timeout=SSH_TIMEOUT)

            # Run NAT setup script with sudo
            command = f"echo '{self.hpc_password}' | sudo -S bash {self.hpc_nat_setup_script}"
            stdin, stdout, stderr = ssh.exec_command(command)

            # Wait for command to complete
            exit_status = stdout.channel.recv_exit_status()

            if exit_status == 0:
                print(f"  ✓ NAT setup on HPC completed successfully")
                return True
            else:
                error_output = stderr.read().decode().strip()
                print(f"  ✗ NAT setup on HPC failed with exit code {exit_status}")
                if error_output:
                    print(f"    Error: {error_output}")
                return False

        except paramiko.AuthenticationException:
            print(f"  ✗ Authentication failed for {self.hpc_user}@{self.hpc_host}")
            return False
        except Exception as e:
            print(f"  ✗ Error setting up NAT on HPC: {e}")
            return False
        finally:
            if ssh:
                ssh.close()


    def run_nat_setup_on_host(self) -> bool:
        """Run NAT setup script on the host machine with sudo."""
        print(f"\nSetting up NAT on host...")

        try:
            # Use sudo -S to read password from stdin
            process = subprocess.Popen(
                ["sudo", "-S", "bash", self.setup_nat_host_script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            stdout, stderr = process.communicate(input=self.host_sudo_password + "\n", timeout=NAT_SETUP_TIMEOUT)

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
            process.kill()
            print(f"  ✗ NAT setup on host timed out")
            return False
        except Exception as e:
            print(f"  ✗ Error setting up NAT on host: {e}")
            return False


    def setup_network(self) -> bool:
        """Set up network routing between host and both targets."""
        print("\n" + "=" * 60)
        print("NETWORK SETUP")
        print("=" * 60)

        # Step 1: Verify direct connection to HPC
        print("\nStep 1: Verifying direct connection to HPC target...")
        if not self.ping_host(self.hpc_host):
            print("\nERROR: Cannot reach HPC target. Please check:")
            print(f"  - Network cable connection to {self.hpc_host}")
            print(f"  - IP configuration on interface enx98fc84e12360")
            return False

        # Step 2: Run NAT setup on HPC first (required before host NAT)
        print("\nStep 2: Setting up NAT routing on HPC target...")
        if not self.run_nat_setup_on_hpc():
            print("\nERROR: Failed to set up NAT on HPC target")
            return False

        # Step 3: Run NAT setup on host
        print("\nStep 3: Setting up NAT routing on host...")
        if not self.run_nat_setup_on_host():
            print("\nERROR: Failed to set up NAT on host")
            return False

        # Step 4: Verify connection to Topaz through HPC
        print("\nStep 4: Verifying connection to Topaz target (via HPC)...")
        time.sleep(WAIT_ROUTE_EFFECT)  # Brief pause for routing to take effect
        if not self.ping_host(self.topaz_host):
            print("\nERROR: Cannot reach Topaz target through HPC. Please check:")
            print(f"  - Network configuration on HPC target")
            print(f"  - Connection between HPC and Topaz")
            return False

        print("\n✓ Network setup completed successfully!")
        return True