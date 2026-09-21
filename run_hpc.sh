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
# Each flag also STARTS the program on the box it names, over ssh, and stops it
# again when this exits -- so a demo needs one terminal instead of three. See
# remote_launch.sh, which also explains why stopping it needs more than closing
# the connection. Pass --manual to switch all of that off and run those two
# programs yourself, exactly as before this existed.
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

# What to run on the far-end boxes, and how to log in. Overridable from the
# environment so a box that differs needs no edit here. The passwords are in
# the clear because these are lab boxes on a direct cable or the lab LAN with
# one shared login each -- fixture, not a secret. Point them elsewhere with
# e.g. VLM_SSH_USER=... TOPAZ_SSH_PASS=... ./run_hpc.sh ...
VLM_SSH_USER="${VLM_SSH_USER:-bach}"
VLM_SSH_PASS="${VLM_SSH_PASS:-password}"
VLM_START_CMD="${VLM_START_CMD:-bash /home/keshav/workspace/vlm_web_app/start.sh}"
# Logged in as bach, but the app is RUN AS keshav. Not a stylistic choice: under
# any other account it dies during model loading with
#     Segmentation fault (core dumped) uvicorn main:app --host 0.0.0.0 --port 8001
# while as its owner it is serving in about eight seconds. bach's own password
# authorises the sudo. Set VLM_RUN_AS= (empty) to run it as the login user.
VLM_RUN_AS="${VLM_RUN_AS:-keshav}"
# start.sh runs uvicorn in the foreground without exec'ing it, so uvicorn's
# command line keeps no trace of start.sh. The "is it already up" check has to
# match uvicorn itself or it would miss a running server and start a second.
VLM_RUNNING_PATTERN="${VLM_RUNNING_PATTERN:-uvicorn main:app}"

TOPAZ_SSH_USER="${TOPAZ_SSH_USER:-user}"
TOPAZ_SSH_PASS="${TOPAZ_SSH_PASS:-user}"
TOPAZ_SUDO_PASS="${TOPAZ_SUDO_PASS:-user}"
# Needs root for the board's SPI/IMU and EDAC devices. -u so its output reaches
# the log as it happens rather than in 4KB blocks.
TOPAZ_START_CMD="${TOPAZ_START_CMD:-python3 -u /home/user/Remote_Systems_Interact/target_topaz2_standalone.py}"
TOPAZ_RUNNING_PATTERN="${TOPAZ_RUNNING_PATTERN:-target_topaz2_standalone.py}"

usage() {
    cat <<'USAGE'
usage: run_hpc.sh [--local] [--vlm-host IP] [--vlm-port PORT]
                  [--edge-device] [--topaz-host IP] [--topaz-port PORT]
                  [--manual] [extra args...]

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

  --manual           Leave the far-end programs alone: do not start them over
                     ssh, and do not stop them on exit. Use when you are
                     starting the VLM app and the Topaz target yourself.
                     --no-remote-launch is accepted as the same thing.

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
REMOTE_LAUNCH=1
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
        --manual|--no-remote-launch) REMOTE_LAUNCH=0; shift ;;
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

# Starting and stopping the far-end programs lives in its own file: it is all
# ssh mechanics and none of it is about this launcher.
if [[ -r "$REPO/remote_launch.sh" ]]; then
    # shellcheck source=remote_launch.sh
    source "$REPO/remote_launch.sh"
    RL_ENABLED=$REMOTE_LAUNCH
else
    echo "warning: $REPO/remote_launch.sh is missing; the far-end programs will" >&2
    echo "         not be started or stopped from here." >&2
    REMOTE_LAUNCH=0
    remote_register() { :; }
    remote_launch_preflight() { :; }
    remote_install_traps() { :; }
    remote_start_all() { :; }
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

    if [[ $REMOTE_LAUNCH -eq 1 ]]; then
        # A sudo password is only wanted when we actually sudo, i.e. when
        # VLM_RUN_AS names an account to run the app as.
        vlm_sudo=""
        [[ -n "$VLM_RUN_AS" ]] && vlm_sudo="$VLM_SSH_PASS"
        remote_register vlm "VLM app" "$VLM_SSH_USER" "$VLM_HOST" "$VLM_SSH_PASS" \
                        "$vlm_sudo" "$VLM_RUNNING_PATTERN" "$VLM_START_CMD" "$VLM_RUN_AS"
    fi

    # Advisory only. The box may simply not be up yet, and the dashboard recovers
    # a device on its own once it starts answering, so a failure here must not
    # stop the server -- it only saves a round trip when the address is wrong.
    if command -v curl >/dev/null 2>&1; then
        if curl -sf --max-time 3 -o /dev/null "http://$VLM_HOST:$VLM_PORT/health"; then
            echo "VLM box answering at http://$VLM_HOST:$VLM_PORT"
        elif [[ $REMOTE_LAUNCH -eq 1 ]]; then
            # Not a warning: we are about to start it ourselves.
            echo "VLM box not answering yet at http://$VLM_HOST:$VLM_PORT -- starting it over ssh."
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
    if [[ $REMOTE_LAUNCH -eq 1 ]]; then
        remote_register topaz "Topaz target" "$TOPAZ_SSH_USER" "$TOPAZ_HOST" \
                        "$TOPAZ_SSH_PASS" "$TOPAZ_SUDO_PASS" \
                        "$TOPAZ_RUNNING_PATTERN" "$TOPAZ_START_CMD"
    fi

    echo "Edge-device mode: Topaz backend runs here, target board at $TOPAZ_HOST:$TOPAZ_PORT"
    echo "    No second machine and no nginx needed for the Topaz panel."
fi

if [[ $LOCAL_MODE -eq 1 || $EDGE_DEVICE -eq 1 ]]; then
    echo "Direct-link mode. Open the dashboard on this box's link address, e.g.:"
    echo "    http://<this box>:5000/        (production shell)"
    echo "    http://<this box>:5000/test    (testing shell)"
fi

# Traps before the first launch, so a box that comes up is still torn down if
# the next one fails. Starting the far end first is deliberate: the target
# board dials in to US and simply retries until we are listening, and the VLM
# panel recovers on its own once it answers, so neither minds being early.
remote_launch_preflight || true
remote_install_traps
remote_start_all

cd "$REPO"
# Deliberately NOT exec: exec replaces this shell, and the traps above go with
# it -- the far-end programs would then outlive Ctrl+C, which is the whole
# thing remote_launch.sh exists to prevent. Run it as a child instead and let
# the traps fire when it returns. `|| status=$?` keeps `set -e` from skipping
# them when uvicorn exits non-zero, which is what Ctrl+C looks like.
status=0
"$VENV/bin/python" combined_hpc.py ${PASSTHRU[@]+"${PASSTHRU[@]}"} || status=$?
exit $status
