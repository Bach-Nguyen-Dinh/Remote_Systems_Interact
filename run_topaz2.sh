#!/usr/bin/env bash
# Launch combined_topaz2.py inside its own virtualenv.
#
# Use this instead of "python3 combined_topaz2.py" -- the bare command picks up
# the system interpreter, whose imports come from a mix of apt dist-packages,
# /usr/local (sudo pip) and ~/.local (pip --user). The venv is the whole point.
#
#     ./run_topaz2.sh              # serve on 0.0.0.0:5001, plus the target
#                                  # sockets on 12346 (metrics) and 55556 (progress)
#
# Rebuild the venv from scratch (see set_up_proxy_server.md) if it goes missing.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO/.venv-topaz2"

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "error: $VENV is missing or broken. Rebuild it (see installation.md):" >&2
    echo "         python3 -m venv $VENV" >&2
    echo "         $VENV/bin/pip install --upgrade pip setuptools wheel" >&2
    echo "         $VENV/bin/pip install -r $REPO/requirements-topaz2.txt" >&2
    echo "       If venv reports 'ensurepip is not available', first run:" >&2
    echo "         sudo apt install python3-pip python3.8-venv" >&2
    exit 1
fi

cd "$REPO"
exec "$VENV/bin/python" combined_topaz2.py "$@"
