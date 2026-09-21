#!/bin/bash
# Entry point for the desktop icon (rsi-dashboard.desktop).
#
# Why this exists instead of pointing the icon straight at the .py:
#
#   1. A double-clicked .desktop file has no terminal attached. Anything the
#      launcher writes before its window opens -- a missing module, a bad path,
#      a syntax error -- would go to the session journal and nowhere the user
#      will look, so the icon would just appear to do nothing. Everything on
#      stderr is captured here and shown in a dialog instead.
#
#   2. /usr/bin/python3 explicitly, NOT bare python3. dashboard_launcher.py
#      needs paramiko for the Edge Camera button, and paramiko is in the system
#      dist-packages only -- .venv-hpc does not have it. The desktop session's
#      PATH does not currently contain that venv, but a shell profile could put
#      it there tomorrow, and this would then break at the worst moment.
#
# Run it from a terminal too if you like; it behaves the same either way.

set -u

here=$(dirname "$(readlink -f "$0")")
launcher="$here/dashboard_launcher.py"
python=/usr/bin/python3

fail() {
    # %b, not %s: the messages below carry \n as two characters, and neither
    # zenity nor a terminal would turn those into line breaks on their own.
    msg=$(printf '%b' "$1")
    # zenity when there is a display to show it on, stderr otherwise.
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v zenity >/dev/null; then
        zenity --error --no-wrap --title="Dashboard Launcher" --text="$msg" 2>/dev/null
    fi
    printf '%s\n' "$msg" >&2
    exit 1
}

[ -f "$launcher" ] || fail "Cannot find dashboard_launcher.py next to this script.\nLooked in: $here"
[ -x "$python" ]   || fail "$python is missing, so the launcher cannot start."

# stderr goes to a file rather than through tee, so that it is certain to be
# complete by the time it is read back -- a process substitution can still be
# flushing after the launcher has exited.
errlog=$(mktemp -t rsi-launcher-XXXXXX.log)
"$python" "$launcher" "$@" 2>"$errlog"
status=$?

cat "$errlog" >&2          # a terminal run still sees everything it said
detail=$(tail -n 25 "$errlog")
rm -f "$errlog"

if [ "$status" -ne 0 ]; then
    fail "The launcher exited with status $status.\n\n${detail:-(it printed nothing)}"
fi
