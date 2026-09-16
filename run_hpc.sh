#!/usr/bin/env bash
# Launch combined_hpc.py inside its own virtualenv.
#
# Use this instead of "python3 combined_hpc.py" -- the bare command picks up the
# system interpreter, which resolves imports from a mix of apt dist-packages,
# /usr/local (sudo pip) and ~/.local (pip --user). The venv is the whole point.
#
#     ./run_hpc.sh                 # serve on 0.0.0.0:5000 (lab LAN, behind nginx)
#
# Direct-link mode -- this box cabled straight to the VLM box, no router, no
# nginx. Prompts for the VLM box's address on that link:
#
#     sudo bash run_hpc.sh --local
#     sudo bash run_hpc.sh --local --vlm-host 10.42.0.3    # same, no prompt
#
# Why it matters: with nginx out of the path the browser cannot use the /vlm/
# prefix, so the dashboard's VLM panel is pointed at the VLM box's own origin
# (http://<vlm host>:8001/) instead. The flag is passed to combined_hpc.py as
# environment variables -- see the "direct-link mode" comment in that file for
# why environment and not argv.
#
# Rebuild the venv from scratch (see set_up_proxy_server.md) if it goes missing.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO/.venv-hpc"

# The VLM box's usual address on a direct link. Only a prompt default -- the
# lab-LAN address lives in combined_hpc.py and is not affected by this.
DEFAULT_VLM_HOST="10.42.0.3"
DEFAULT_VLM_PORT="8001"

usage() {
    cat <<'USAGE'
usage: run_hpc.sh [--local] [--vlm-host IP] [--vlm-port PORT] [extra args...]

  --local            Direct-link mode: serve the dashboard's VLM panel from the
                     VLM box's own origin instead of nginx's /vlm/ prefix. Use
                     when this box is cabled straight to the VLM box.
  --vlm-host IP      The VLM box's address on that link. Implies --local, and
                     skips the interactive prompt.
  --vlm-port PORT    Only if the VLM app has moved off its default (8001).

Anything else is passed through to combined_hpc.py unchanged.
USAGE
}

# Reject a typo'd address here rather than letting it surface later as a panel
# that just says "disconnected".
valid_ipv4() {
    local ip="$1" octet
    [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    local IFS=.
    for octet in $ip; do
        ((10#$octet <= 255)) || return 1
    done
    return 0
}

LOCAL_MODE=0
VLM_HOST=""
VLM_PORT=""
PASSTHRU=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --local)        LOCAL_MODE=1; shift ;;
        --vlm-host)     VLM_HOST="${2:-}"; LOCAL_MODE=1; shift 2 ;;
        --vlm-host=*)   VLM_HOST="${1#*=}"; LOCAL_MODE=1; shift ;;
        --vlm-port)     VLM_PORT="${2:-}"; shift 2 ;;
        --vlm-port=*)   VLM_PORT="${1#*=}"; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              PASSTHRU+=("$1"); shift ;;
    esac
done

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "error: $VENV is missing or broken. Rebuild it (see installation.md):" >&2
    echo "         python3 -m venv $VENV" >&2
    echo "         $VENV/bin/pip install --upgrade pip setuptools wheel" >&2
    echo "         $VENV/bin/pip install -r $REPO/requirements-hpc.txt" >&2
    echo "       If venv reports 'ensurepip is not available', first run:" >&2
    echo "         sudo apt install python3-pip python3.8-venv" >&2
    exit 1
fi

if [[ $LOCAL_MODE -eq 1 ]]; then
    if [[ -z "$VLM_HOST" ]]; then
        # Read from the terminal, not stdin: this is usually run under sudo and
        # may be launched with stdin redirected, and a silently skipped prompt
        # would start the server pointed at the wrong box.
        if [[ ! -r /dev/tty ]]; then
            echo "error: --local needs to prompt for the VLM box IP but there is no terminal." >&2
            echo "       Pass it directly:  sudo bash run_hpc.sh --local --vlm-host $DEFAULT_VLM_HOST" >&2
            exit 1
        fi
        printf 'VLM box IP address on the direct link [%s]: ' "$DEFAULT_VLM_HOST" > /dev/tty
        read -r VLM_HOST < /dev/tty || true
        VLM_HOST="${VLM_HOST:-$DEFAULT_VLM_HOST}"
    fi

    VLM_HOST="${VLM_HOST//[[:space:]]/}"
    if ! valid_ipv4 "$VLM_HOST"; then
        echo "error: '$VLM_HOST' is not a valid IPv4 address." >&2
        exit 1
    fi

    VLM_PORT="${VLM_PORT:-$DEFAULT_VLM_PORT}"
    if [[ ! "$VLM_PORT" =~ ^[0-9]+$ ]] || ((VLM_PORT < 1 || VLM_PORT > 65535)); then
        echo "error: '$VLM_PORT' is not a valid port." >&2
        exit 1
    fi

    export HPC_LOCAL_MODE=1
    export HPC_VLM_HOST="$VLM_HOST"
    export HPC_VLM_PORT="$VLM_PORT"

    # Advisory only. The box may simply not be up yet, and the dashboard recovers
    # a device on its own once it starts answering, so a failure here must not
    # stop the server -- it only saves a round trip when the address is wrong.
    if command -v curl >/dev/null 2>&1; then
        if curl -sf --max-time 3 -o /dev/null "http://$VLM_HOST:$VLM_PORT/health"; then
            echo "VLM box answering at http://$VLM_HOST:$VLM_PORT"
        else
            echo "warning: no answer from http://$VLM_HOST:$VLM_PORT/health yet." >&2
            echo "         Starting anyway -- the VLM panel appears on its own once it responds." >&2
        fi
    fi

    echo "Direct-link mode. Open the dashboard on this box's link address, e.g.:"
    echo "    http://<this box>:5000/        (production shell)"
    echo "    http://<this box>:5000/test    (testing shell)"
fi

cd "$REPO"
exec "$VENV/bin/python" combined_hpc.py ${PASSTHRU[@]+"${PASSTHRU[@]}"}
