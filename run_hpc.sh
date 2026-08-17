#!/usr/bin/env bash
# Launch combined_hpc.py inside its own virtualenv.
#
# Use this instead of "python3 combined_hpc.py" -- the bare command picks up the
# system interpreter, which resolves imports from a mix of apt dist-packages,
# /usr/local (sudo pip) and ~/.local (pip --user). The venv is the whole point.
#
#     ./run_hpc.sh                 # serve on 0.0.0.0:5000
#
# Rebuild the venv from scratch (see set_up_proxy_server.md) if it goes missing.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO/.venv-hpc"

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "error: $VENV is missing or broken. Rebuild it (see installation.md):" >&2
    echo "         python3 -m venv $VENV" >&2
    echo "         $VENV/bin/pip install --upgrade pip setuptools wheel" >&2
    echo "         $VENV/bin/pip install -r $REPO/requirements-hpc.txt" >&2
    echo "       If venv reports 'ensurepip is not available', first run:" >&2
    echo "         sudo apt install python3-pip python3.8-venv" >&2
    exit 1
fi

cd "$REPO"
exec "$VENV/bin/python" combined_hpc.py "$@"
