"""
edge_device.py — run the Topaz edge-device backend *inside* combined_hpc.py.

THE TWO TOPOLOGIES
------------------
Normally the Topaz dashboard is a second backend on a second machine:

    browser --https--> nginx --/topaz/--> 192.168.0.32:5001 (combined_topaz2.py)
                         |                      ^
                         \\--/--> :5000 (combined_hpc.py)   |
                                                            \\-- sockets -- Topaz target board

That needs two boxes running two backends. `run_hpc.sh --edge-device` collapses
it to one: the Topaz target board is cabled straight to THIS machine, and this
module mounts combined_topaz2's app inside combined_hpc's, so one process serves
both dashboards and terminates the target's metric/progress sockets itself:

    browser --http--> :5000 (combined_hpc.py)
                          |-- /            the HPC shell
                          \\-- /topaz/...   combined_topaz2's app, mounted here
                                    ^
                                    \\-- sockets -- Topaz target board

WHY MOUNTING IS A DROP-IN
-------------------------
The Topaz pages were already written to use *relative* URLs only (iframe
src="orientation", fetch("system_metrics"), ...) so they work behind nginx's
/topaz/ prefix -- a leading slash would escape it. A Starlette mount strips its
prefix exactly the way `proxy_pass http://...:5001/;` does, so those same pages
work unchanged under MOUNT_PATH, and the HPC shells' existing "/topaz/" iframe
default resolves to the mounted app with no frontend change at all. That is why
MOUNT_PATH is not configurable: it has to equal the prefix the shells and nginx
already agree on.

WHAT STAYS THE SAME
-------------------
Nothing here changes the two-box deployment. Every entry point is a no-op unless
HPC_EDGE_DEVICE is set, combined_topaz2 is imported lazily so a normal run never
even loads it, and combined_topaz2.py itself runs unmodified as its own server.

CONFIGURED BY ENVIRONMENT, not argv, for the same reason as --local: this module
is imported by combined_hpc.py, which is also run as `uvicorn combined_hpc:app`
where sys.argv belongs to uvicorn. `run_hpc.sh --edge-device` prompts for the
address and exports these:

    HPC_EDGE_DEVICE=1         mount the Topaz app in-process
    TOPAZ_TARGET_IP=10.42.0.7 the target board's address on the direct link
    TOPAZ_TARGET_PORT=54322   only if its command socket ever moves
"""

import os

# The prefix the mounted Topaz app answers on. Must match what the HPC shells
# already frame ("/topaz/", "/topaz/test") and what nginx proxies in the two-box
# deployment -- see "WHY MOUNTING IS A DROP-IN" above. Not a setting.
MOUNT_PATH = "/topaz"

# Device-health keys in combined_hpc.DEVICES that this mode takes over. Both
# name the same mounted app; they differ only in which Topaz shell they frame
# (/topaz/ vs /topaz/test), which is the browser's business, not ours.
DEVICE_KEYS = ("topaz", "topaz_test")


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


ENABLED = _env_flag("HPC_EDGE_DEVICE")

# Where the Topaz app is imported from. A module path rather than an import so
# that a normal (two-box) run never loads it -- see `_topaz()`.
_TOPAZ_MODULE = "combined_topaz2"
_topaz_module = None


def _topaz():
    """Import combined_topaz2 on first use and cache it.

    Lazy on purpose: in the normal two-box deployment this module is imported by
    combined_hpc.py and does nothing, and loading a second FastAPI app (plus its
    optional Pillow/InfluxDB probing) for a mode that is switched off would be
    pure cost -- and would turn any problem in that file into a failure to start
    the HPC dashboard at all.

    ONE VIRTUALENV, TWO APPS. In the two-box deployment each app has its own venv
    (.venv-hpc here, .venv-topaz2 on the Topaz box); embedded, combined_topaz2
    runs under .venv-hpc. That works because requirements-topaz2.txt is a strict
    subset of requirements-hpc.txt at identical pins -- deliberately, so the ARM
    box needs no compiler. Nothing enforces it, though, so a dependency added to
    requirements-topaz2.txt and not to requirements-hpc.txt surfaces exactly
    here. Fail with the fix rather than a bare traceback: the mode was asked for
    explicitly, so carrying on without it would be a silent lie, but the message
    has to say what to do."""
    global _topaz_module
    if _topaz_module is None:
        import importlib
        try:
            _topaz_module = importlib.import_module(_TOPAZ_MODULE)
        except ImportError as exc:
            raise RuntimeError(
                f"--edge-device needs {_TOPAZ_MODULE}'s dependencies in this "
                f"virtualenv, and importing it failed:\n"
                f"    {exc}\n"
                f"requirements-topaz2.txt is meant to stay a SUBSET of "
                f"requirements-hpc.txt so that one venv serves both apps. Fix "
                f"with:\n"
                f"    .venv-hpc/bin/pip install -r requirements-hpc.txt\n"
                f"and if something was added to requirements-topaz2.txt, add it "
                f"to requirements-hpc.txt too."
            ) from exc
    return _topaz_module


def target_address():
    """(ip, port) of the Topaz target board, as combined_topaz2 resolved it.

    Read through that module rather than re-read from the environment here, so
    there is exactly one place the default lives."""
    t = _topaz()
    return t.TARGET_IP, t.TARGET_PORT


def mount(app):
    """Mount the Topaz app under MOUNT_PATH. No-op unless enabled.

    Call once, after `app` is created. Returns whether it mounted."""
    if not ENABLED:
        return False
    app.mount(MOUNT_PATH, _topaz().app, name="topaz")
    return True


def start():
    """Start the Topaz target<->host receivers. No-op unless enabled.

    Call from the host app's lifespan, on the event loop that serves the app:
    Starlette does NOT run a mounted sub-app's lifespan, so without this the
    Topaz app would mount and serve pages while never hearing from the target.
    combined_topaz2.start_background_workers() is the same function its own
    lifespan calls -- embedding changes nothing about how the receivers run."""
    if not ENABLED:
        return False
    return _topaz().start_background_workers()


def apply_to_devices(devices):
    """Rewrite combined_hpc's DEVICES for in-process Topaz. No-op unless enabled.

    The health gate exists so a panel shows a "disconnected" placeholder instead
    of nginx's raw 502 when the box behind it is off. Here there is no second box
    and no nginx: the Topaz app is in this process, so it is up exactly when the
    dashboard serving the question is up. `local` says so, and the probe loop
    skips the round trip rather than asking us about ourselves over HTTP.

    "src" is deliberately left alone. The shells' own defaults ("/topaz/",
    "/topaz/test") already resolve to the mounted app, and the server inventing
    frame URLs is the thing the DEVICES comment in combined_hpc.py warns off --
    the prefixes differ per shell, so each shell owns its own."""
    if not ENABLED:
        return False
    ip, port = target_address()
    for key in DEVICE_KEYS:
        entry = devices.get(key)
        if entry is None:
            continue
        entry["local"] = True
        entry["detail"] = f"served in-process at {MOUNT_PATH}/ (target {ip}:{port})"
    return True


# ----------------------------------------------------------------------------
# Startup reporting
# ----------------------------------------------------------------------------
# Image sets the Topaz vision pages display. They are read from directories on
# whichever host serves the Topaz app -- which in this mode is THIS box, not the
# one those directories were staged on. list_webp() already degrades to an empty
# list when a directory is missing, so a gap shows up as an empty picker rather
# than an error; naming them at startup is what makes that diagnosable.
def missing_demo_dirs():
    """Topaz demo image directories that are not on this box. Empty if enabled
    is off, or if everything the vision pages need is present."""
    if not ENABLED:
        return []
    t = _topaz()
    wanted = [
        ("small object detection, input",  t.SMALL_OBJ_INPUT_DIR),
        ("small object detection, output", t.SMALL_OBJ_OUTPUT_DIR),
        ("AI smoke, input",                t.AI_SMOKE_INPUT_DIR),
        ("AI smoke, output",               t.AI_SMOKE_OUTPUT_DIR),
        ("AI ship, input",                 t.AI_SHIP_INPUT_DIR),
        ("AI ship, output",                t.AI_SHIP_OUTPUT_DIR),
    ]
    wanted += [("autonomous navigation, " + kind, path)
               for kind, path in sorted(t.AUTO_NAV_DIRS.items())]
    return [(label, path) for label, path in wanted if not os.path.isdir(path)]


def startup_lines():
    """Lines describing this mode for the server's startup output. Empty when
    the mode is off, so the caller can print them unconditionally."""
    if not ENABLED:
        return []

    t = _topaz()
    ip, port = target_address()
    lines = [
        f"Edge-device mode: Topaz dashboard mounted in-process at {MOUNT_PATH}/ "
        f"(no second backend, no nginx needed)",
        f"    target board   {ip}:{port} (control messages)",
        f"    listening for  metrics on :{t.SYSINFO_PORT}, "
        f"workload progress on :{t.DATA_PORT}",
    ]

    missing = missing_demo_dirs()
    if missing:
        lines.append(f"    warning: {len(missing)} Topaz demo image director"
                     f"{'y is' if len(missing) == 1 else 'ies are'} not on this box; "
                     f"those pickers will be empty:")
        lines += [f"        {label}: {path}" for label, path in missing]
    return lines
