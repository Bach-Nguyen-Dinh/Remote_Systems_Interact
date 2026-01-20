#!/usr/bin/env python3
"""
udp_listener.py

Listens on localhost:LISTEN_PORT for UDP packets containing JSON (utf-8).
Prints each received JSON message with a timestamp and can optionally
append messages to a file (json lines).

Usage:
    python3 udp_listener.py

Press Ctrl+C to stop.
"""

import socket
import json
import threading
import signal

# Listener configuration
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8889
RECV_BUFFER = 65536    # 64KB, should be enough for typical JSON payloads
SOCKET_TIMEOUT = 1.0   # seconds - allows clean shutdown checks
LOG_FILE = None        # set to "received.jsonl" to save incoming messages (one JSON per line)

stop_event = threading.Event()

def signal_handler(sig, frame):
    print("\nStopping listener...")
    stop_event.set()

def pretty_print(obj):
    try:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    except Exception:
        print(repr(obj))

def listener_loop(host=LISTEN_HOST, port=LISTEN_PORT, logfile=LOG_FILE):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
    except Exception as e:
        print(f"Failed to bind to {host}:{port}: {e}")
        return

    sock.settimeout(SOCKET_TIMEOUT)
    print(f"Listening for UDP JSON on {host}:{port} (press Ctrl+C to stop)\n")

    recv_count = 0
    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(RECV_BUFFER)
            print(data)
        except socket.timeout:
            continue
        except Exception as e:
            print(f"Socket error: {e}")
            break

        # recv_count += 1
        # print(f"[{recv_count}] Received {len(data)} bytes from {addr}")

        # # Try to decode JSON
        # try:
        #     text = data.decode("utf-8", errors="strict")
        #     obj = json.loads(text)
        #     pretty_print(obj)
        # except UnicodeDecodeError:
        #     print("Failed to decode bytes as UTF-8. Raw bytes:")
        #     print(data)
        #     obj = None
        # except json.JSONDecodeError:
        #     # If it isn't valid JSON, print the raw decoded text
        #     try:
        #         text = data.decode("utf-8", errors="replace")
        #         print("Received text (not valid JSON):")
        #         print(text)
        #     except Exception:
        #         print("Received data could not be decoded to text.")
        #     obj = None
        # except Exception as e:
        #     print(f"Unexpected parse error: {e}")
        #     obj = None

        # # Optionally append to a JSON lines log file
        # if logfile and obj is not None:
        #     try:
        #         with open(logfile, "a", encoding="utf-8") as f:
        #             # write one valid JSON object per line
        #             f.write(json.dumps({"count": recv_count, "from": addr, "payload": obj}, ensure_ascii=False) + "\n")
        #     except Exception as e:
        #         print(f"Failed to write to log file: {e}")

        # print("-" * 60)

    sock.close()
    print(f"Listener stopped. Total messages received: {recv_count}")

def main():
    # Handle Ctrl+C
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # If you want to enable logging to disk, set LOG_FILE below or via env/args.
    listener_loop()

if __name__ == "__main__":
    main()
