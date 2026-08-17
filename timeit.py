#!/usr/bin/env python3
"""Time how long a command takes to complete.

Usage:
    ./timeit.py <command> [args...]

Examples:
    ./timeit.py sleep 3
    ./timeit.py sudo profiler /home/sarthak/workspace/SAR_codebase/cphd_aic.py \
        --metrics_interval_ms 500 --csv_write_interval_s 5 -- --file 2023-10-22-15-20-28_CPHD.cphd

The command's own output is passed through unchanged; the elapsed
wall-clock time is printed at the end (and on Ctrl-C / failure).
"""

import subprocess
import sys
import time


def format_duration(seconds: float) -> str:
    """Return a human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.3f} s"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{int(mins)}m {secs:.2f}s ({seconds:.3f} s total)"
    hours, mins = divmod(mins, 60)
    return f"{int(hours)}h {int(mins)}m {secs:.1f}s ({seconds:.3f} s total)"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    command = sys.argv[1:]

    print(f">>> Running: {' '.join(command)}\n", file=sys.stderr)

    start = time.perf_counter()
    try:
        result = subprocess.run(command)
        returncode = result.returncode
    except KeyboardInterrupt:
        elapsed = time.perf_counter() - start
        print(f"\n>>> Interrupted after {format_duration(elapsed)}", file=sys.stderr)
        return 130
    except FileNotFoundError:
        print(f">>> Command not found: {command[0]}", file=sys.stderr)
        return 127
    elapsed = time.perf_counter() - start

    print(f"\n>>> Elapsed: {format_duration(elapsed)}", file=sys.stderr)
    print(f">>> Exit code: {returncode}", file=sys.stderr)
    return returncode


if __name__ == "__main__":
    sys.exit(main())
