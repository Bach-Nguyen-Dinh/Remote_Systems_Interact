#!/usr/bin/env python3
"""
udp_listener.py

Listens for UDP datagrams on 127.0.0.1:8888, decodes JSON messages,
prints them to stdout and appends to a log file.

Usage:
    python3 udp_listener.py
"""

import socket
import json
import time
import argparse
from datetime import datetime

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8888
BUFFER_SIZE = 65536  # 64 KiB, enough for reasonably large messages
LOG_FILE = "udp_listener.log"


def make_socket(host=LISTEN_HOST, port=LISTEN_PORT):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Allow reuse in case of quick restarts
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    return s


def pretty_print(msg_obj):
    try:
        pretty = json.dumps(msg_obj, indent=2, ensure_ascii=False)
    except Exception:
        pretty = str(msg_obj)
    return pretty


def main(save_to_file=True):
    sock = make_socket()
    print(f"Listening for UDP on {LISTEN_HOST}:{LISTEN_PORT} (Ctrl-C to quit)")

    if save_to_file:
        f = open(LOG_FILE, "a", encoding="utf-8")
        print(f"Appending received messages to {LOG_FILE}")
    else:
        f = None

    try:
        while True:
            try:
                data, addr = sock.recvfrom(BUFFER_SIZE)
                ts = datetime.utcnow().isoformat() + "Z"
                # try decode as utf-8 text first
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    text = None

                received = {
                    "timestamp": ts,
                    "from": f"{addr[0]}:{addr[1]}",
                    "raw_bytes_len": len(data),
                    "text": text,
                    "json": None,
                    "json_error": None,
                }

                if text:
                    try:
                        parsed = json.loads(text)
                        received["json"] = parsed
                    except Exception as e:
                        received["json_error"] = repr(e)

                # Print a concise one-line summary
                if received["json"] is not None:
                    print(f"[{ts}] JSON from {received['from']} — {len(data)} bytes")
                    print(pretty_print(received["json"]))
                else:
                    print(f"[{ts}] Raw from {received['from']} — {len(data)} bytes")
                    if received["text"]:
                        print("Text:", received["text"])
                    if received["json_error"]:
                        print("JSON parse error:", received["json_error"])

                # Write to file (newline-delimited JSON)
                if f:
                    log_entry = {
                        "timestamp": ts,
                        "from": received["from"],
                        "raw_bytes_len": received["raw_bytes_len"],
                        "text": received["text"],
                        "json": received["json"],
                        "json_error": received["json_error"],
                    }
                    f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                    f.flush()

            except KeyboardInterrupt:
                print("\nInterrupted by user — exiting.")
                break
            except Exception as e:
                # Keep listening on transient errors
                print("Listener error:", repr(e))
                time.sleep(0.1)

    finally:
        sock.close()
        if f:
            f.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="UDP JSON listener for localhost:8888")
    parser.add_argument("--no-log", dest="nolog", action="store_true",
                        help="Do not append received messages to a log file")
    args = parser.parse_args()
    main(save_to_file=not args.nolog)
