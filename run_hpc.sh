#!/usr/bin/env bash
# Launch combined_hpc.py inside its own virtualenv.
#
# Use this instead of "python3 combined_hpc.py" -- the bare command picks up the
# system interpreter, which resolves imports from a mix of apt dist-packages,
# /usr/local (sudo pip) and ~/.local (pip --user). The venv is the whole point.
#
#     ./run_hpc.sh                 # serve on 0.0.0.0:5000 (lab LAN, behind nginx)
#
# Two independent "direct link" flags change where a panel comes from when this
# box is cabled straight to another machine, with no router and no nginx. They
# are orthogonal -- use either, both, or neither:
#
#   --local         the VLM box is on the far end of the cable
#   --edge-device   the Topaz target board is, and its dashboard backend runs
#                   HERE instead of on a second machine
#
#     sudo bash run_hpc.sh --local
#     sudo bash run_hpc.sh --local --vlm-host 10.42.0.3        # same, no prompt
#     sudo bash run_hpc.sh --edge-device
#     sudo bash run_hpc.sh --edge-device --topaz-host 10.42.0.7
#     sudo bash run_hpc.sh --local --edge-device               # both links
#
# Why --local matters: with nginx out of the path the browser cannot use the
# /vlm/ prefix, so the dashboard's VLM panel is pointed at the VLM box's own
# origin (http://<vlm host>:8001/) instead.
#
# Why --edge-device matters: normally the Topaz dashboard is a SECOND backend on
# a SECOND machine (combined_topaz2.py, reached through nginx's /topaz/). This
# flag mounts that same app inside combined_hpc.py, so one process serves both
# dashboards and terminates the target board's sockets itself -- see
# edge_device.py. The /topaz/ URLs the dashboard already uses resolve to the
# mounted app unchanged, so nothing in the browser has to know which it is.
#
# Both flags reach combined_hpc.py as environment variables -- see the
# "direct-link mode" comment in that file for why environment and not argv.
#
# Rebuild the venv from scratch (see set_up_proxy_server.md) if it goes missing.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO/.venv-hpc"

# Usual addresses on a direct link. Only prompt defaults -- the lab-LAN address
# lives in combined_hpc.py and the target default in combined_topaz2.py; neither
# is affected by these.
DEFAULT_VLM_HOST="10.42.0.3"
DEFAULT_VLM_PORT="8001"
DEFAULT_TOPAZ_HOST="10.42.0.7"
DEFAULT_TOPAZ_PORT="54322"

usage() {
    cat <<'USAGE'
usage: run_hpc.sh [--local] [--vlm-host IP] [--vlm-port PORT]
                  [--edge-device] [--topaz-host IP] [--topaz-port PORT]
                  [extra args...]

  --local            Direct-link mode for the VLM panel: serve it from the VLM
                     box's own origin instead of nginx's /vlm/ prefix. Use when
                     this box is cabled straight to the VLM box.
  --vlm-host IP      The VLM box's address on that link. Implies --local, and
                     skips the interactive prompt.
  --vlm-port PORT    Only if the VLM app has moved off its default (8001).

  --edge-device      Run the Topaz dashboard backend in THIS process, mounted at
                     /topaz/, instead of on a second machine behind nginx. Use
                     when the Topaz target board is cabled straight to this box.
  --topaz-host IP    The target board's address on that link. Implies
                     --edge-device, and skips the interactive prompt.
  --topaz-port PORT  Only if the target's command socket has moved off 54322.

The two modes are independent; either, both or neither may be given.
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

# Ask for an address on the terminal, not stdin: this is usually run under sudo
# and may be launched with stdin redirected, and a silently skipped prompt would
# start the server pointed at the wrong box. Echoes the answer; the caller reads
# it with $(...), so prompt and error text go to /dev/tty and stderr.
#   $1 what to call the box   $2 default address   $3 the flag that skips this
prompt_for_ip() {
    local what="$1" default="$2" flag="$3" answer=""
    if [[ ! -r /dev/tty ]]; then
        echo "error: need to prompt for the $what IP but there is no terminal." >&2
        echo "       Pass it directly:  sudo bash run_hpc.sh $flag $default" >&2
        exit 1
    fi
    printf '%s IP address on the direct link [%s]: ' "$what" "$default" > /dev/tty
    read -r answer < /dev/tty || true
    echo "${answer:-$default}"
}

# $1 the address   $2 what to call it in an error
require_valid_ipv4() {
    if ! valid_ipv4 "$1"; then
        echo "error: '$1' is not a valid IPv4 address for the $2." >&2
        exit 1
    fi
}

# $1 the port   $2 what to call it in an error
require_valid_port() {
    if [[ ! "$1" =~ ^[0-9]+$ ]] || (($1 < 1 || $1 > 65535)); then
        echo "error: '$1' is not a valid port for the $2." >&2
        exit 1
    fi
}

LOCAL_MODE=0
VLM_HOST=""
VLM_PORT=""
EDGE_DEVICE=0
TOPAZ_HOST=""
TOPAZ_PORT=""
PASSTHRU=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --local)         LOCAL_MODE=1; shift ;;
        --vlm-host)      VLM_HOST="${2:-}"; LOCAL_MODE=1; shift 2 ;;
        --vlm-host=*)    VLM_HOST="${1#*=}"; LOCAL_MODE=1; shift ;;
        --vlm-port)      VLM_PORT="${2:-}"; shift 2 ;;
        --vlm-port=*)    VLM_PORT="${1#*=}"; shift ;;
        --edge-device)   EDGE_DEVICE=1; shift ;;
        --topaz-host)    TOPAZ_HOST="${2:-}"; EDGE_DEVICE=1; shift 2 ;;
        --topaz-host=*)  TOPAZ_HOST="${1#*=}"; EDGE_DEVICE=1; shift ;;
        --topaz-port)    TOPAZ_PORT="${2:-}"; shift 2 ;;
        --topaz-port=*)  TOPAZ_PORT="${1#*=}"; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               PASSTHRU+=("$1"); shift ;;
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
        VLM_HOST="$(prompt_for_ip "VLM box" "$DEFAULT_VLM_HOST" "--local --vlm-host")"
    fi
    VLM_HOST="${VLM_HOST//[[:space:]]/}"
    require_valid_ipv4 "$VLM_HOST" "VLM box"

    VLM_PORT="${VLM_PORT:-$DEFAULT_VLM_PORT}"
    require_valid_port "$VLM_PORT" "VLM app"

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
fi

if [[ $EDGE_DEVICE -eq 1 ]]; then
    if [[ -z "$TOPAZ_HOST" ]]; then
        TOPAZ_HOST="$(prompt_for_ip "Topaz target board" "$DEFAULT_TOPAZ_HOST" \
                                    "--edge-device --topaz-host")"
    fi
    TOPAZ_HOST="${TOPAZ_HOST//[[:space:]]/}"
    require_valid_ipv4 "$TOPAZ_HOST" "Topaz target board"

    TOPAZ_PORT="${TOPAZ_PORT:-$DEFAULT_TOPAZ_PORT}"
    require_valid_port "$TOPAZ_PORT" "Topaz target command socket"

    export HPC_EDGE_DEVICE=1
    export TOPAZ_TARGET_IP="$TOPAZ_HOST"
    export TOPAZ_TARGET_PORT="$TOPAZ_PORT"

    # No reachability check here, deliberately. Unlike the VLM box we do not poll
    # the target: it connects to US (metrics on :12346, progress on :55556) and
    # this address is only used to push control messages the other way. A board
    # that is up but idle need not have anything listening yet, so a failed probe
    # would say nothing useful.
    echo "Edge-device mode: Topaz backend runs here, target board at $TOPAZ_HOST:$TOPAZ_PORT"
    echo "    No second machine and no nginx needed for the Topaz panel."
fi

if [[ $LOCAL_MODE -eq 1 || $EDGE_DEVICE -eq 1 ]]; then
    echo "Direct-link mode. Open the dashboard on this box's link address, e.g.:"
    echo "    http://<this box>:5000/        (production shell)"
    echo "    http://<this box>:5000/test    (testing shell)"
fi

cd "$REPO"
exec "$VENV/bin/python" combined_hpc.py ${PASSTHRU[@]+"${PASSTHRU[@]}"}
