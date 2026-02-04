"""
IMU Manager Module

A reusable module for managing IMU daemon communication and data collection.
Designed for portability across different target programs.

Usage:
    from imu_manager import IMUManager

    imu = IMUManager(
        executable_path="/path/to/imu/executable",
        udp_port=8889,
        sample_rate=10,
        calibration_samples=100
    )

    # Start the IMU daemon and listener
    imu.start()

    # Get current IMU data
    data = imu.get_data()

    # Recalibrate
    imu.restart()

    # Stop when done
    imu.stop()
"""

import socket
import subprocess
import threading
import json
import os
import signal
import time


class IMUManager:
    """
    Manages IMU daemon lifecycle and data collection via UDP.

    The IMU daemon is an external executable that reads from hardware sensors,
    performs sensor fusion, and streams data over UDP. This class handles:
    - Starting/stopping the daemon process
    - Listening for UDP data packets
    - Parsing calibration and measurement messages
    - Providing thread-safe access to the latest IMU data
    """

    def __init__(
        self,
        executable_path: str,
        udp_port: int = 8889,
        sample_rate: int = 10,
        calibration_samples: int = 100,
        listen_ip: str = "127.0.0.1",
        recv_buffer: int = 65536,
        socket_timeout: float = 1.0,
        stop_event: threading.Event = None
    ):
        """
        Initialize the IMU Manager.

        Args:
            executable_path: Path to the IMU daemon executable
            udp_port: UDP port for receiving IMU data
            sample_rate: IMU sampling rate in Hz
            calibration_samples: Number of samples for gyro calibration
            listen_ip: IP address to bind the UDP listener
            recv_buffer: UDP receive buffer size
            socket_timeout: Socket timeout for clean shutdown checks
            stop_event: Optional external threading.Event for coordinated shutdown.
                       If not provided, an internal event is created.
        """
        # Configuration
        self.executable_path = executable_path
        self.udp_port = udp_port
        self.sample_rate = sample_rate
        self.calibration_samples = calibration_samples
        self.listen_ip = listen_ip
        self.recv_buffer = recv_buffer
        self.socket_timeout = socket_timeout

        # State
        self._daemon_process = None
        self._listener_thread = None
        self._data_raw = None
        self._data_lock = threading.Lock()
        self._calibration_status = {"state": "idle", "progress": 0}
        self._calibration_lock = threading.Lock()

        # Use external stop event if provided, otherwise create internal one
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._owns_stop_event = stop_event is None

    @property
    def is_running(self) -> bool:
        """Check if the IMU daemon is currently running."""
        return (
            self._daemon_process is not None
            and self._daemon_process.poll() is None
        )

    @property
    def calibration_status(self) -> dict:
        """Get the current calibration status (thread-safe)."""
        with self._calibration_lock:
            return self._calibration_status.copy()

    def start(self) -> bool:
        """
        Start the IMU daemon and data listener thread.

        Returns:
            True if started successfully, False otherwise.
        """
        if self.is_running:
            print("IMU daemon already running, stopping first...")
            self.stop()

        # Reset stop event if we own it
        if self._owns_stop_event:
            self._stop_event.clear()

        with self._calibration_lock:
            self._calibration_status = {"state": "starting", "progress": 0}

        # Start the daemon process
        if not self._start_daemon():
            return False

        # Start the listener thread
        self._listener_thread = threading.Thread(
            target=self._listener_loop,
            daemon=True,
            name="IMU-Listener"
        )
        self._listener_thread.start()

        return True

    def stop(self):
        """Stop the IMU daemon and listener thread."""
        # Signal listener to stop if we own the event
        if self._owns_stop_event:
            self._stop_event.set()

        # Stop the daemon process
        self._stop_daemon()

        # Wait for listener thread to finish
        if self._listener_thread is not None and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=2.0)
            self._listener_thread = None

        with self._calibration_lock:
            self._calibration_status = {"state": "idle", "progress": 0}

    def restart(self) -> bool:
        """
        Restart the IMU daemon for recalibration.

        Returns:
            True if restarted successfully, False otherwise.
        """
        print("Restarting IMU daemon for recalibration...")
        self.stop()
        time.sleep(0.5)  # Brief pause before restart
        return self.start()

    def get_data(self) -> dict:
        """
        Get the latest IMU data.

        Returns:
            Dictionary containing:
            - accel_x, accel_y, accel_z: Accelerometer readings
            - gyro_x, gyro_y, gyro_z: Gyroscope readings
            - roll, pitch, yaw: Orientation angles
            - calibration_state: Current calibration state
            - calibration_progress: Calibration progress (0-100)
        """
        with self._calibration_lock:
            cal_state = self._calibration_status.get("state", "idle")
            cal_progress = self._calibration_status.get("progress", 0)

        default_data = {
            "accel_x": 0.0, "accel_y": 0.0, "accel_z": 0.0,
            "gyro_x": 0.0, "gyro_y": 0.0, "gyro_z": 0.0,
            "roll": 0.0, "pitch": 0.0, "yaw": 0.0,
            "calibration_state": cal_state,
            "calibration_progress": cal_progress
        }

        with self._data_lock:
            if self._data_raw is None:
                return default_data

            raw_gyro = self._data_raw.get("raw_gyro", [0, 0, 0])
            raw_accel = self._data_raw.get("raw_accel", [0, 0, 0])

            return {
                "accel_x": raw_accel[0] if len(raw_accel) > 0 else 0.0,
                "accel_y": raw_accel[1] if len(raw_accel) > 1 else 0.0,
                "accel_z": raw_accel[2] if len(raw_accel) > 2 else 0.0,
                "gyro_x": raw_gyro[0] if len(raw_gyro) > 0 else 0.0,
                "gyro_y": raw_gyro[1] if len(raw_gyro) > 1 else 0.0,
                "gyro_z": raw_gyro[2] if len(raw_gyro) > 2 else 0.0,
                "roll": self._data_raw.get("roll", 0.0),
                "pitch": self._data_raw.get("pitch", 0.0),
                "yaw": self._data_raw.get("yaw", 0.0),
                "calibration_state": cal_state,
                "calibration_progress": cal_progress
            }

    def get_raw_data(self) -> dict:
        """
        Get the raw IMU data packet as received from the daemon.

        Returns:
            The raw data dictionary, or None if no data available.
        """
        with self._data_lock:
            return self._data_raw.copy() if self._data_raw else None

    def _start_daemon(self) -> bool:
        """Start the IMU daemon subprocess."""
        try:
            cmd = [
                "sudo",
                self.executable_path,
                "--rate", str(self.sample_rate),
                "--cal", str(self.calibration_samples),
                "--host", str(self.listen_ip),
                "--port", str(self.udp_port)
            ]
            print(f"Starting IMU daemon: {' '.join(cmd)}")

            self._daemon_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid  # Create new process group for clean termination
            )
            print(f"IMU daemon started with PID {self._daemon_process.pid}")
            return True

        except FileNotFoundError:
            print(f"IMU executable not found: {self.executable_path}")
            with self._calibration_lock:
                self._calibration_status = {
                    "state": "error",
                    "progress": 0,
                    "error": "executable not found"
                }
            return False
        except Exception as e:
            print(f"Error starting IMU daemon: {e}")
            with self._calibration_lock:
                self._calibration_status = {
                    "state": "error",
                    "progress": 0,
                    "error": str(e)
                }
            return False

    def _stop_daemon(self):
        """Stop the IMU daemon subprocess."""
        if self._daemon_process is None:
            return

        if self._daemon_process.poll() is None:
            print(f"Stopping IMU daemon (PID {self._daemon_process.pid})...")
            try:
                # Send SIGTERM to process group
                os.killpg(os.getpgid(self._daemon_process.pid), signal.SIGTERM)
                try:
                    self._daemon_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # Force kill if SIGTERM didn't work
                    os.killpg(os.getpgid(self._daemon_process.pid), signal.SIGKILL)
                    self._daemon_process.wait(timeout=1)
                print("IMU daemon stopped")
            except Exception as e:
                print(f"Error stopping IMU daemon: {e}")

        self._daemon_process = None

    def _listener_loop(self):
        """UDP listener loop for IMU data from the daemon."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.listen_ip, self.udp_port))
            print(f"IMU data listener bound to {self.listen_ip}:{self.udp_port}")
        except Exception as e:
            print(f"Failed to bind IMU listener to {self.listen_ip}:{self.udp_port}: {e}")
            return

        sock.settimeout(self.socket_timeout)

        while not self._stop_event.is_set():
            try:
                data, addr = sock.recvfrom(self.recv_buffer)
                self._process_message(data)
            except socket.timeout:
                continue
            except Exception as e:
                if not self._stop_event.is_set():
                    print(f"IMU listener error: {e}")
                break

        sock.close()
        print("IMU data listener stopped")

    def _process_message(self, data: bytes):
        """Process a received UDP message from the IMU daemon."""
        try:
            msg = json.loads(data.decode("utf-8"))
            msg_type = msg.get("type", "")

            if msg_type == "calibration_start":
                with self._calibration_lock:
                    self._calibration_status = {
                        "state": "calibrating",
                        "progress": 0,
                        "duration_sec": msg.get("duration_sec", 0)
                    }
                print(f"IMU calibration started ({msg.get('duration_sec', 0):.1f}s)")

            elif msg_type == "calibration_progress":
                with self._calibration_lock:
                    self._calibration_status["progress"] = msg.get("progress", 0)

            elif msg_type == "calibration_complete":
                with self._calibration_lock:
                    self._calibration_status = {
                        "state": "running",
                        "progress": 100,
                        "gyro_offset": msg.get("gyro_offset", [0, 0, 0]),
                        "accel_offset": msg.get("accel_offset", [0, 0, 0])
                    }
                print(f"IMU calibration complete. Gyro offset: {msg.get('gyro_offset')}")

            elif msg_type == "imu_data":
                with self._data_lock:
                    self._data_raw = msg

            elif msg_type == "shutdown":
                print("IMU daemon shutdown notification received")
                with self._calibration_lock:
                    self._calibration_status = {"state": "shutdown", "progress": 0}

        except json.JSONDecodeError as e:
            print(f"IMU JSON decode error: {e}")
