#!/usr/bin/env bash
# shellcheck shell=bash
#
# Start the far-end programs over SSH, and make sure they stop when we stop.
# Sourced by run_hpc.sh; not meant to be run on its own.
#
# WHAT IT IS FOR
# --------------
# --local and --edge-device each name a box on the far end of a direct cable,
# and each of those boxes has to be running one program before the dashboard
# shows anything at all:
#
#   --local         VLM box      bash /home/keshav/.../vlm_web_app/start.sh
#   --edge-device   Topaz board  sudo python3 .../target_topaz2_standalone.py
#
# Both used to be started by hand, from two more terminals, before every demo.
#
# WHY CTRL+C DOES NOT REACH THEM ON ITS OWN
# -----------------------------------------
# Killing the local ssh client does not stop the remote command. Measured on
# the Topaz board, same command both times:
#
#   ssh     board 'sleep 400'   kill the client -> remote sleep STILL RUNNING
#   ssh -tt board 'sleep 400'   kill the client -> remote sleep GONE
#
# Without a terminal the remote command owns no tty, so sshd merely closes its
# pipes on disconnect, and a process that neither reads nor writes them never
# finds out. With -tt sshd allocates a pty and the command leads a session on
# it, so dropping the connection hangs that pty up -- and a pty hangup is a
# SIGHUP to the whole foreground process group. Confirmed to reach both shapes
# actually used here: a root-owned child behind sudo, and a grandchild
# (start.sh's uvicorn, which start.sh does not exec).
#
# That hangup is the mechanism. Everything else below is either setup for it,
# or a check that it really happened.
#
# TWO THINGS THAT LOOK LIKE DETAILS AND ARE NOT
# ---------------------------------------------
# 1. The command goes to `bash -s` ON STDIN, not as an ssh command argument.
#    An argument is world-readable in the remote `ps`, and these commands carry
#    a sudo password. Fed on stdin it never reaches any command line: remote
#    `ps` shows `bash -s` and nothing else.
#
# 2. -tt is for the LAUNCH only, never the teardown. With -tt the remote stdin
#    is a pty, and a pty never reports EOF -- a `bash -s` fed that way runs the
#    script and then blocks forever waiting for more. Harmless for a program
#    meant to run until we stop it; an outright hang for a short teardown
#    script. Teardown therefore uses a plain connection, where stdin is a real
#    pipe and EOF arrives normally.
#
# HOW A STOP ACTUALLY GOES
# ------------------------
#   1. kill the local ssh client -> pty hangup -> SIGHUP to the remote group.
#   2. reconnect and escalate INT -> TERM -> KILL over the recorded process
#      GROUP, in case step 1 did not take (a future sudo with use_pty, or a
#      start.sh that learns to daemonise).
# Step 2 is normally a no-op that reports "already-stopped" in about a second.
#
# Killing the GROUP rather than a pid matters: the launched program leads its
# group, so start.sh's uvicorn and sudo's python3 are in it and go down with
# it. A pattern kill would not do -- uvicorn's command line keeps no trace of
# the start.sh that spawned it, so matching "start.sh" would kill the wrapper
# and orphan the server. The group is read back from a per-run pidfile, so a
# stale file from a crashed run is never reused against a recycled pid.

# Scoped by uid: this is normally run under sudo but not always, and a log file
# left behind by root is unwritable by the same script run as a user -- which
# fails the redirection, and takes the whole launch down with it.
RL_LOG_DIR="${TMPDIR:-/tmp}/run_hpc-remote-$(id -u)"
RL_RUN_ID="$$-$(date +%s)"
RL_ENABLED=1          # cleared by --no-remote-launch

# UserKnownHostsFile=/dev/null: these are lab boxes that get re-imaged, and a
# changed host key must not turn into a blocking prompt in front of a demo.
# The link is a direct cable or the lab LAN, and the passwords are in this
# repo already, so key pinning is not what is protecting anything here.
# ServerAliveInterval: if the cable is pulled the client gives up in ~15s,
# which is what delivers the hangup above when the link dies rather than us.
RL_SSH_OPTS=(
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o LogLevel=ERROR
    -o ConnectTimeout=8
    -o ServerAliveInterval=5
    -o ServerAliveCountMax=3
)

_RL_TAGS=()
declare -A _RL_LABEL _RL_USER _RL_HOST _RL_PASS _RL_SUDO _RL_PATTERN _RL_CMD
declare -A _RL_SUDOUSER
declare -A _RL_LOG _RL_PIDFILE _RL_SSHPID _RL_LAUNCHED
_RL_STOPPED=0

# Register one box. Nothing connects until remote_start_all.
#   $1 tag       short key, also the log file name
#   $2 label     what to call it in messages
#   $3 user      ssh user
#   $4 host      ssh host
#   $5 pass      ssh password
#   $6 sudo_pass sudo password, "" when the command needs no sudo
#   $7 pattern   pgrep -f pattern, used ONLY to notice it is already running
#   $8 cmd       the command to run, already sudo-free (see $6)
#   $9 run_as    optional: run the command as THIS account instead of root,
#                i.e. `sudo -u <run_as>`. For a program that has to run as its
#                owner while we log in as somebody else. Empty means root when
#                $6 is set, and no sudo at all when it is not.
remote_register() {
    local tag="$1"
    _RL_TAGS+=("$tag")
    _RL_LABEL[$tag]="$2";   _RL_USER[$tag]="$3";    _RL_HOST[$tag]="$4"
    _RL_PASS[$tag]="$5";    _RL_SUDO[$tag]="$6";    _RL_PATTERN[$tag]="$7"
    _RL_CMD[$tag]="$8";     _RL_SUDOUSER[$tag]="${9:-}"
    _RL_LOG[$tag]="$RL_LOG_DIR/$tag.log"
    _RL_PIDFILE[$tag]="/tmp/run_hpc-remote-$tag-$RL_RUN_ID.pid"
}

# sshpass is what lets this run unattended. Without it ssh would stop on a
# password prompt that nothing is going to answer.
remote_launch_preflight() {
    ((${#_RL_TAGS[@]})) || return 0
    if ! command -v sshpass >/dev/null 2>&1; then
        echo "warning: sshpass is not installed, so the far-end programs cannot be" >&2
        echo "         started from here. Install it with:" >&2
        echo "             sudo apt install sshpass" >&2
        echo "         or start them by hand and re-run with --no-remote-launch." >&2
        RL_ENABLED=0
        return 1
    fi
    mkdir -p "$RL_LOG_DIR" 2>/dev/null || true
    # A log we cannot write would fail the launch redirection rather than just
    # losing the log, so fall back to somewhere we certainly can.
    if ! : > "$RL_LOG_DIR/.probe" 2>/dev/null; then
        RL_LOG_DIR="$(mktemp -d 2>/dev/null)" || RL_LOG_DIR="/tmp"
        local tag
        for tag in "${_RL_TAGS[@]}"; do _RL_LOG[$tag]="$RL_LOG_DIR/$tag.log"; done
    fi
    rm -f "$RL_LOG_DIR/.probe" 2>/dev/null
    return 0
}

# Run a short script on the far end and print what it prints. Plain connection,
# no pty, script on stdin -- see note 2 in the header.
#   $1 tag, then the script arrives on this function's own stdin
# Run a short script on the far end and print what it prints. Plain
# connection, no pty, script on stdin -- see note 2 in the header.
#
# stderr is deliberately NOT discarded: the caller folds it into the reply so a
# failure explains itself. Swallowing it here once turned a permissions error
# into a bogus "box unreachable".
#
# SSHPASS is EXPORTED rather than passed as a `VAR=x cmd` prefix. Measured:
# while one of our own backgrounded `sshpass -e ssh -tt` launches is still
# live, the prefix form makes a second sshpass exit 255 with no output at all,
# and the export form succeeds at the same instant. Since teardown always runs
# with a launch still live, the prefix form fails exactly when it matters. It
# is `local`, so it leaves the caller's environment alone.
#
# The script is read into a variable first because a pipe can only be consumed
# once, and a retry has to be able to send it again.
#   $1 tag, then the script arrives on this function's own stdin
_rl_ssh_script() {
    local tag="$1" script out rc attempt
    local SSHPASS="${_RL_PASS[$tag]}"
    export SSHPASS
    script="$(cat)"
    for attempt in 1 2; do
        out="$(sshpass -e ssh "${RL_SSH_OPTS[@]}" \
                "${_RL_USER[$tag]}@${_RL_HOST[$tag]}" 'bash -s' 2>&1 <<< "$script")"
        rc=$?
        # 255 is ssh's own "connection failed", the only failure worth a second
        # try; a non-zero from the remote script is an answer, not an error.
        ((rc != 255)) && break
        sleep 1
    done
    printf '%s' "$out"
    return "$rc"
}

# Ask the far end what is already running. Read-only: it never kills anything.
# The pattern lives in a variable rather than a command line so that pgrep
# cannot match the very shell running it -- the classic way this check reports
# itself as a hit.
#
# It doubles as the reachability AND login check, deliberately: a box that is
# off or a password that is wrong is then reported once, here, instead of
# becoming a "started" message for a launch that never happened. Prints the
# Answers on stdout, tagged: "RSI_OK <pids>" or "RSI_ERR <reason>". The reason
# travels in the output rather than in a global because every caller reads this
# through $(...), which is a subshell -- a global set in here would be dropped
# on the way out, and the caller would report a blank reason.
_rl_probe() {
    local tag="$1" out rc
    out="$(SSHPASS="${_RL_PASS[$tag]}" sshpass -e ssh "${RL_SSH_OPTS[@]}" \
            "${_RL_USER[$tag]}@${_RL_HOST[$tag]}" 'bash -s' 2>&1 <<EOF
PAT=$(printf '%q' "${_RL_PATTERN[$tag]}")
printf 'RSI_OK '
pgrep -f "\$PAT" 2>/dev/null | head -5 | tr '\n' ' '
EOF
    )"
    rc=$?
    out="${out//$'\r'/}"
    if ((rc != 0)) || [[ "$out" != *RSI_OK* ]]; then
        local reason
        reason="$(printf '%s' "$out" | grep -v '^[[:space:]]*$' | tail -1)"
        # sshpass kills ssh without a word when the password is refused, so an
        # empty reason is itself the diagnosis and has to be spelled out.
        [[ -n "$reason" ]] || reason="ssh exited $rc (wrong password, or the box is not answering)"
        printf 'RSI_ERR %s' "$reason"
        return 1
    fi
    printf 'RSI_OK %s' "${out##*RSI_OK}"
    return 0
}

# A backgrounded ssh that has exited stays a zombie until it is reaped, and
# `kill -0` SUCCEEDS on a zombie -- so "did it die" has to read the process
# state, not just probe for existence.
_rl_ssh_dead() {
    local st
    st="$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' ')"
    [[ -z "$st" || "$st" == Z* ]]
}

# The script the far end runs to start the program. Writes its own pid first:
# under a pty that pid is also the process group id, which is what a stop
# signals.
_rl_launch_script() {
    local tag="$1"
    printf 'printf %%s "$$" > %q\n' "${_RL_PIDFILE[$tag]}"
    if [[ -n "${_RL_SUDO[$tag]}" ]]; then
        # Password piped to sudo -S by a shell builtin, so it stays out of the
        # process table. `exit` keeps the wrapper from blocking on the pty once
        # the program finishes, which is how an early exit becomes visible.
        printf 'PW=%q\n' "${_RL_SUDO[$tag]}"
        local as=""
        [[ -n "${_RL_SUDOUSER[$tag]}" ]] && as="-u $(printf '%q' "${_RL_SUDOUSER[$tag]}") "
        printf 'printf "%%s\\n" "$PW" | sudo -S -p "" %s%s\n' "$as" "${_RL_CMD[$tag]}"
        printf 'exit $?\n'
    else
        printf 'exec %s\n' "${_RL_CMD[$tag]}"
    fi
}

remote_start_all() {
    ((${#_RL_TAGS[@]})) || return 0
    ((RL_ENABLED)) || return 0

    local tag probe running
    for tag in "${_RL_TAGS[@]}"; do
        probe="$(_rl_probe "$tag")"
        if [[ "$probe" == RSI_ERR* ]]; then
            echo "warning: cannot reach ${_RL_USER[$tag]}@${_RL_HOST[$tag]} -- ${probe#RSI_ERR }" >&2
            echo "         ${_RL_LABEL[$tag]} not started; its panel stays down until it is." >&2
            echo "         Start it by hand with:  ${_RL_CMD[$tag]}" >&2
            continue
        fi
        running="${probe#RSI_OK }"
        if [[ -n "${running// /}" ]]; then
            # Left alone on purpose: a second copy would only collide on the
            # port it binds. It is left alone on the way OUT too -- no pidfile
            # is written for it, so the teardown has no group to signal. We
            # stop what we started and nothing else: someone else's server,
            # started from their own terminal, is not ours to kill.
            echo "${_RL_LABEL[$tag]}: already running on ${_RL_HOST[$tag]}, not starting a second copy."
            continue
        fi

        : > "${_RL_LOG[$tag]}" 2>/dev/null || true
        SSHPASS="${_RL_PASS[$tag]}" sshpass -e ssh "${RL_SSH_OPTS[@]}" -tt \
            "${_RL_USER[$tag]}@${_RL_HOST[$tag]}" 'bash -s' \
            >>"${_RL_LOG[$tag]}" 2>&1 < <(_rl_launch_script "$tag") &
        _RL_SSHPID[$tag]=$!
        _RL_LAUNCHED[$tag]=1
        echo "${_RL_LABEL[$tag]}: starting on ${_RL_USER[$tag]}@${_RL_HOST[$tag]}  (log: ${_RL_LOG[$tag]})"
    done

    # The probe already proved the box answers and the login works, so whatever
    # can still go wrong now goes wrong fast: a missing path, a bad
    # interpreter, a sudo password the box rejects. Three seconds is enough for
    # that to reach the log, and none of it is spent waiting on a connect.
    ((${#_RL_LAUNCHED[@]})) || return 0
    sleep 3
    local pid
    for tag in "${_RL_TAGS[@]}"; do
        pid="${_RL_SSHPID[$tag]:-}"
        [[ -n "$pid" ]] || continue
        _rl_ssh_dead "$pid" || continue
        echo "warning: ${_RL_LABEL[$tag]} exited immediately. Last lines of its log:" >&2
        sed -e 's/\r$//' -e 's/^/         /' "${_RL_LOG[$tag]}" 2>/dev/null | tail -5 >&2
        echo "         Start it by hand with:  ${_RL_CMD[$tag]}" >&2
        unset "_RL_SSHPID[$tag]"
        unset "_RL_LAUNCHED[$tag]"
    done
    return 0
}

# The script the far end runs to stop the program. Escalates INT -> TERM ->
# KILL over two handles at once, because neither alone is sufficient:
#
#   the process GROUP, read back from the pidfile. Exact, and the only handle
#   that catches children the pattern cannot see -- start.sh's uvicorn keeps no
#   trace of start.sh in its command line, so matching "start.sh" would kill the
#   wrapper and orphan the server.
#
#   the launch PATTERN, because `sudo -u` escapes that group entirely. Measured
#   on the VLM box, where sudo runs the command under its own pty:
#       pid 1081658 pgid 1081658 sid 1081658  bash -s          <- recorded
#       pid 1081666 pgid 1081666 sid 1081666  sudo -u keshav   <- new session
#       pid 1081728 pgid 1081667 sid 1081666  uvicorn main:app <- out of reach
#   The group there holds nothing but the wrapper, so a group-only teardown
#   reports success while the server keeps the port.
#
# Killing by pattern is only safe because we get here ONLY when the pidfile we
# wrote at launch is still present -- that is the proof this process is ours.
# Without it we stop at "no-pidfile" and touch nothing, which is what keeps a
# server somebody else started out of reach.
#
# `kill -0` on a negative pid is the portable "does this group still exist"
# test; this board's ps rejects --pgid.
_rl_teardown_script() {
    local tag="$1"
    printf 'PF=%q\n' "${_RL_PIDFILE[$tag]}"
    printf 'PW=%q\n' "${_RL_SUDO[$tag]}"
    printf 'PAT=%q\n' "${_RL_PATTERN[$tag]}"
    cat <<'BODY'
PG=$(cat "$PF" 2>/dev/null | tr -dc '0-9')
# The pidfile is removed only on the way out, never up front: _rl_ssh_script
# resends this script if ssh itself fails, and a version that deleted the file
# first would come back the second time as "no-pidfile" -- reporting that we
# had launched nothing, about a process it had just killed.
_done() { rm -f "$PF" 2>/dev/null; echo "$1"; exit 0; }
if [ -z "$PG" ]; then echo "no-pidfile"; exit 0; fi
if [ -n "$PW" ]; then
    _k() { printf '%s\n' "$PW" | sudo -S -p '' kill "$@" 2>/dev/null; }
else
    _k() { kill "$@" 2>/dev/null; }
fi
_grp()  { _k -0 -"$PG"; }
_pat()  { pgrep -f "$PAT" 2>/dev/null | tr '\n' ' '; }
_left() { _grp && return 0; [ -n "$(_pat)" ]; }

_left || _done "already-stopped"
for sig in INT TERM KILL; do
    _k -"$sig" -"$PG"
    p=$(_pat)
    [ -n "$p" ] && _k -"$sig" $p
    n=0
    while [ "$n" -lt 8 ]; do
        _left || _done "stopped-by-$sig"
        sleep 0.25
        n=$((n + 1))
    done
done
_left && echo "STILL-RUNNING" || _done "stopped-by-KILL"
BODY
}

_rl_stop_one() {
    local tag="$1" pid="${_RL_SSHPID[$tag]:-}" verdict
    # Nothing of ours to stop: the box was unreachable, or it was already
    # running and we left it alone. Connecting just to discover that would add
    # a connect timeout to every Ctrl+C and could only report a false alarm.
    [[ -n "${_RL_LAUNCHED[$tag]:-}" ]] || return 0

    # Step 1: drop the connection. On a Ctrl+C the terminal has usually already
    # done this for us -- a background job of a non-interactive shell shares
    # our process group and got the same SIGINT -- so this often finds it gone.
    if [[ -n "$pid" ]]; then
        kill -TERM "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    fi

    # Step 2: confirm, and finish the job if the hangup did not.
    verdict="$(_rl_teardown_script "$tag" | _rl_ssh_script "$tag" 2>&1 | tr -d '\r' | tr '\n' ' ')"
    verdict="${verdict%"${verdict##*[![:space:]]}"}"
    case "$verdict" in
        already-stopped|stopped-by-*)
            echo "${_RL_LABEL[$tag]}: stopped." ;;
        no-pidfile)
            echo "${_RL_LABEL[$tag]}: nothing of ours left running." ;;
        STILL-RUNNING)
            echo "warning: ${_RL_LABEL[$tag]} survived SIGKILL on ${_RL_HOST[$tag]}." >&2 ;;
        "")
            # Nothing came back at all: the box is off or the cable is out.
            # Whatever we started went down with the link either way.
            echo "${_RL_LABEL[$tag]}: could not confirm (${_RL_HOST[$tag]} unreachable)." >&2 ;;
        *)
            # Something answered but not in the vocabulary above. Say what it
            # actually was -- guessing "unreachable" here once sent me looking
            # at the network for what was really a permissions error.
            echo "warning: ${_RL_LABEL[$tag]}: unexpected teardown reply: $verdict" >&2 ;;
    esac
}

remote_stop_all() {
    ((${#_RL_TAGS[@]})) || return 0
    ((RL_ENABLED)) || return 0
    ((_RL_STOPPED)) && return 0
    _RL_STOPPED=1
    # Never let a failed cleanup step abort the rest of the cleanup: this runs
    # from a trap, under the caller's `set -e`.
    set +e

    ((${#_RL_LAUNCHED[@]})) || return 0
    echo
    echo "Stopping the far-end programs..."
    local tag
    for tag in "${_RL_TAGS[@]}"; do
        _rl_stop_one "$tag" &
    done
    wait
    return 0
}

# Cover every way this script can end. INT/TERM/HUP re-raise after cleaning up
# so the exit status still reads as "killed by a signal" to anything upstream.
remote_install_traps() {
    trap 'remote_stop_all' EXIT
    local sig
    for sig in INT TERM HUP; do
        trap "remote_stop_all; trap - $sig EXIT; kill -$sig \$\$" "$sig"
    done
}
