#!/usr/bin/python3
"""
dashboard_launcher.py -- a three-screen window around run_hpc.sh.

run_hpc.sh already does the hard part: it takes --local / --edge-device /
--manual, prompts for whatever address the flags imply, starts the far-end
programs over ssh, and stops them again when it exits. What it does not do is
survive being run by somebody who does not remember which flag pairs with which
cable. This wraps it:

    screen 1  tick the flags                    --local / --edge-device / --manual
    screen 2  fill in the addresses those imply, Start greys out until they parse
    screen 3  it is running: the browser opens itself once :5000 answers;
              End, Open Dashboard, and Edge Camera when there is a board

Nothing here re-implements run_hpc.sh. The flags and addresses are handed to it
verbatim, so `--vlm-host`/`--topaz-host` are always passed and its interactive
prompt is never reached -- which is the point, since a Tk window has no terminal
to prompt on.

WHICH PYTHON THIS NEEDS
-----------------------
The system one, /usr/bin/python3: paramiko lives in its dist-packages and
nowhere else on this box. That is not what `python3` means here, though --
run_hpc.sh's .venv-hpc is first on PATH in the usual shell, so
`python3 dashboard_launcher.py` gets the venv.

The venv is the dangerous case rather than an obvious one. It CAN import
tkinter, because tkinter is stdlib and a venv reads it from the base prefix, so
the launcher starts and looks entirely healthy; only paramiko is missing, and
paramiko is used by nothing but the Edge Camera button. Left alone that shows up
as a camera that fails mid-demo, having already started the viewer, which is the
worst possible moment to discover an interpreter is wrong.

So the shebang picks the right interpreter for `./dashboard_launcher.py`, and
require_paramiko() below re-execs into it for every other way of starting this.

HOW STOPPING WORKS
------------------
run_hpc.sh tears the far end down from a trap on INT/TERM/HUP/EXIT, and that
trap is the only thing that stops the VLM app and the Topaz target. So End does
not kill the process: it sends SIGINT to run_hpc.sh's whole PROCESS GROUP, which
is exactly what Ctrl+C in a terminal delivers, and then waits for the trap to
finish its ssh teardown before escalating. start_new_session=True at launch is
what makes that group exist to signal.

The signal goes through `sudo` because run_hpc.sh is started under sudo and a
uid-1001 process cannot signal root's. Passwordless sudo is what makes that work
unattended; `sudo -n` everywhere means a box without it fails loudly instead of
hanging on a password prompt no window is going to answer.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import messagebox, scrolledtext, ttk

SYSTEM_PYTHON = "/usr/bin/python3"

# This file lives in dashboard_launcher/, one level down. The scripts it drives
# are not the launcher's own -- run_hpc.sh and display_demo.sh are run by hand
# and by other launchers too -- so they stay at the repo root and are reached
# by going up, never by assuming they sit next to this file.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RUN_HPC = os.path.join(REPO, "run_hpc.sh")
DISPLAY_DEMO = os.path.join(REPO, "display_demo.sh")

# Defaults copied from run_hpc.sh so the two agree on what a plain Enter means.
DEFAULT_VLM_HOST = "10.42.0.3"
DEFAULT_VLM_PORT = "8001"
DEFAULT_TOPAZ_HOST = "10.42.0.7"
DEFAULT_TOPAZ_PORT = "54322"

# Where the dashboard lives, once it is up.
#
# Readiness is always measured against uvicorn itself on the loopback, because
# that is the process run_hpc.sh actually starts and it is the same in every
# mode -- nginx only proxies to it. Where the BROWSER is pointed is a separate
# question, and it is not the same address:
#
#   --local / --edge-device   nginx is out of the path, and run_hpc.sh tells the
#                             operator to use this box's own :5000. The page is
#                             built for that: it addresses the VLM box by its
#                             own origin instead of the /vlm/ prefix.
#
#   neither (the lab LAN)     the page uses /vlm/ and /topaz/, which only exist
#                             in the nginx config, so :5000 direct would load a
#                             shell whose panels 404. It has to be the site.
#
# Opening the wrong one of those does not fail loudly -- it comes up as a
# dashboard with dead panels -- which is why the choice is made from the flags
# rather than by defaulting to localhost everywhere.
BACKEND_PROBE = "http://127.0.0.1:5000/"
DIRECT_URL = "http://localhost:5000/"
NGINX_URL = "https://nexon.aicraft.com.au/"
BROWSER = "firefox"
# -new-window, not a bare URL: a bare URL opens a tab inside whatever
# Firefox window happens to be in front, which buries the dashboard in
# the operator's own browsing. A window of its own is also the one that
# can be closed at the end of a demo without losing anything else.
BROWSER_ARGS = ["-new-window"]

# combined_hpc.py imports torch and opens InfluxDB before uvicorn binds, and the
# far-end boxes are started first, so the gap between Start and a live port is
# tens of seconds on a cold run. Long enough to look hung, hence the countdown
# in the log; capped so a backend that never comes up stops polling.
BACKEND_TIMEOUT = 180.0
BACKEND_POLL = 1.0

# The direct cable to the VLM box. `nexon_local` is a NetworkManager profile
# that is already bound to this device -- the launcher only re-activates it, so
# picking "Local VLM" cannot leave the link on whatever profile was up last
# (e.g. enp4s0f0-static, which points the same wire somewhere else).
# The device is enp4s0f0; there is no bare enp4s0 on this machine.
VLM_IFACE = "enp4s0f0"
VLM_PROFILE = "nexon_local"

# The Topaz board, for the camera only. run_hpc.sh has its own copy of these for
# the dashboard backend and is not affected by what is here.
TOPAZ_SSH_USER = "user"
TOPAZ_SSH_PASS = "user"
TOPAZ_SUDO_PASS = "user"
TOPAZ_WEBCAM_SCRIPT = "/home/user/setup_webcam.sh"
# setup_webcam.sh backgrounds iris_usb_webcam.sh and then runs
# usb_webcam_capture.sh in the foreground. Stopping the camera has to reach both
# -- killing only the script we launched would leave the streamer holding the
# board's USB device, and the next Edge Camera would come up black.
TOPAZ_WEBCAM_PATTERNS = ("usb_webcam_capture.sh", "iris_usb_webcam")

# display_demo.py binds its socket and starts listening straight away, but the
# board's capture script connects once and gives up if nothing is there yet.
# Two seconds is the gap the viewer needs to be listening first.
CAMERA_SSH_DELAY = 2.0

# How long each stage of the teardown gets before escalating. INT is the long
# one on purpose: it is the signal that triggers run_hpc.sh's trap, and that
# trap spends most of its time in ssh teardowns of the far-end boxes.
STOP_STAGES = (("INT", 30.0), ("TERM", 10.0), ("KILL", 5.0))


def valid_ipv4(text):
    """Same rule as run_hpc.sh's valid_ipv4, so a value this accepts is never
    then rejected by the script it is about to be handed to."""
    if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", text):
        return False
    return all(int(octet) <= 255 for octet in text.split("."))


def valid_port(text):
    return bool(re.fullmatch(r"[0-9]{1,5}", text)) and 1 <= int(text) <= 65535


class Launcher:
    def __init__(self, root):
        self.root = root
        root.title("Nexon Dashboard Launcher")
        root.minsize(560, 380)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Screen 1 choices.
        self.local = tk.BooleanVar(value=False)
        self.edge = tk.BooleanVar(value=False)
        self.manual = tk.BooleanVar(value=False)

        # Screen 2 fields. Kept on the instance rather than rebuilt with the
        # frame so that Back -> Next comes back to what was already typed.
        self.vlm_host = tk.StringVar(value=DEFAULT_VLM_HOST)
        self.vlm_port = tk.StringVar(value=DEFAULT_VLM_PORT)
        self.topaz_host = tk.StringVar(value=DEFAULT_TOPAZ_HOST)
        self.topaz_port = tk.StringVar(value=DEFAULT_TOPAZ_PORT)
        for var in (self.vlm_host, self.vlm_port, self.topaz_host, self.topaz_port):
            var.trace_add("write", lambda *_: self.refresh_start_button())

        self.proc = None            # run_hpc.sh, under sudo, in its own session
        self.stopped = False        # End already ran, or is running
        self.cam_proc = None        # display_demo.sh
        self.cam_ssh = None         # paramiko client holding the board's channel
        self.cam_running = False
        self.cam_lock = threading.Lock()
        # Screen 3's widgets. None until it is built, and read from worker
        # threads, so they need a value from the start rather than a hasattr.
        self.end_btn = None
        self.cam_btn = None
        self.open_btn = None
        self.status = None

        self.body = ttk.Frame(root, padding=16)
        self.body.pack(fill="both", expand=True)
        self.log_widget = None
        self.show_config()

    # ---------------------------------------------------------------- helpers

    def clear(self):
        for child in self.body.winfo_children():
            child.destroy()

    def log(self, text):
        """Append a line to screen 3's pane. Safe from any thread: the actual
        widget call is marshalled onto the Tk thread, which is the only one
        allowed to touch widgets."""
        def append():
            if self.log_widget is None or not self.log_widget.winfo_exists():
                return
            self.log_widget.configure(state="normal")
            self.log_widget.insert("end", text.rstrip("\n") + "\n")
            self.log_widget.see("end")
            self.log_widget.configure(state="disabled")
        self.ui(append)

    def ui(self, fn, *args):
        """Run fn on the Tk thread. Every caller is a worker thread finishing a
        teardown, and a teardown routinely outlives the window that started it --
        so both scheduling onto a destroyed root and touching a destroyed widget
        are normal here, not errors to propagate into the thread."""
        def guarded():
            try:
                fn(*args)
            except tk.TclError:
                pass
        try:
            self.root.after(0, guarded)
        except (tk.TclError, RuntimeError):
            pass

    def widget_do(self, widget, fn):
        """ui(), for a screen-3 widget that may not exist yet or any more."""
        def guarded():
            if widget is not None and widget.winfo_exists():
                fn(widget)
        self.ui(guarded)

    # --------------------------------------------------------------- screen 1

    def show_config(self):
        self.clear()
        ttk.Label(self.body, text="Configuration",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        ttk.Label(self.body, wraplength=520, foreground="#555",
                  text="Tick what is cabled to this box. The next screen asks "
                       "only for the addresses these imply.").pack(anchor="w", pady=(4, 14))

        ttk.Checkbutton(self.body, variable=self.local,
                        text="Local VLM").pack(anchor="w")
        ttk.Label(self.body, wraplength=500, foreground="#555",
                  text="The VLM box is on the far end of a direct cable. Its panel is "
                       "served from the box's own origin instead of nginx's /vlm/ "
                       "prefix, and %s is put back on the %s profile."
                       % (VLM_IFACE, VLM_PROFILE)).pack(anchor="w", padx=(24, 0), pady=(0, 10))

        ttk.Checkbutton(self.body, variable=self.edge,
                        text="Edge device connected").pack(anchor="w")
        ttk.Label(self.body, wraplength=500, foreground="#555",
                  text="The Topaz target board is cabled straight to this box. Its "
                       "dashboard backend runs here instead of on a second machine, "
                       "and the Edge Camera button becomes available."
                  ).pack(anchor="w", padx=(24, 0), pady=(0, 10))

        ttk.Checkbutton(self.body, variable=self.manual,
                        text="Manual").pack(anchor="w")
        ttk.Label(self.body, wraplength=500, foreground="#555",
                  text="Do not start or stop the VLM app and the Topaz target over "
                       "ssh -- you are running those two yourself. The addresses are "
                       "still needed, since the dashboard still has to point at them."
                  ).pack(anchor="w", padx=(24, 0), pady=(0, 10))

        row = ttk.Frame(self.body)
        row.pack(fill="x", side="bottom")
        ttk.Button(row, text="Next  >", command=self.show_details).pack(side="right")

    # --------------------------------------------------------------- screen 2

    def show_details(self):
        self.clear()
        ttk.Label(self.body, text="Details",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")

        form = ttk.Frame(self.body)
        form.pack(fill="x", pady=(12, 0))
        form.columnconfigure(1, weight=1)
        self.fields = []   # (StringVar, validator, label) for the Start check
        row = 0

        if self.local.get():
            ttk.Label(form, text="VLM box", font=("TkDefaultFont", 10, "bold")
                      ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
            row += 1
            row = self._field(form, row, "IP address", self.vlm_host, valid_ipv4, "VLM box IP")
            row = self._field(form, row, "Port", self.vlm_port, valid_port, "VLM app port")
            ttk.Label(form, foreground="#555", wraplength=480,
                      text="%s will be re-activated on the %s profile at Start."
                           % (VLM_IFACE, VLM_PROFILE)
                      ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 12))
            row += 1

        if self.edge.get():
            ttk.Label(form, text="Topaz target board", font=("TkDefaultFont", 10, "bold")
                      ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
            row += 1
            row = self._field(form, row, "IP address", self.topaz_host, valid_ipv4, "Topaz board IP")
            row = self._field(form, row, "Command port", self.topaz_port, valid_port,
                              "Topaz command socket port")

        if not self.fields:
            ttk.Label(self.body, foreground="#555", wraplength=500,
                      text="Nothing to fill in: with neither direct link ticked, the "
                           "dashboard is served on the lab LAN behind nginx, and every "
                           "address it needs is already configured."
                      ).pack(anchor="w", pady=(8, 0))

        self.hint = ttk.Label(self.body, foreground="#a00", wraplength=500, text="")
        self.hint.pack(anchor="w", pady=(10, 0))

        row_btn = ttk.Frame(self.body)
        row_btn.pack(fill="x", side="bottom")
        ttk.Button(row_btn, text="<  Back", command=self.show_config).pack(side="left")
        self.start_btn = ttk.Button(row_btn, text="Start", command=self.start)
        self.start_btn.pack(side="right")
        self.refresh_start_button()

    def _field(self, parent, row, label, var, validator, what):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(24, 8), pady=2)
        ttk.Entry(parent, textvariable=var, width=20).grid(row=row, column=1, sticky="w", pady=2)
        self.fields.append((var, validator, what))
        return row + 1

    def refresh_start_button(self):
        """Grey Start out until every field on screen 2 parses. Called from a
        trace on the vars, so it also fires while the window is on another
        screen -- hence the existence check."""
        if not hasattr(self, "start_btn") or not self.start_btn.winfo_exists():
            return
        bad = [what for var, check, what in getattr(self, "fields", [])
               if not check(var.get().strip())]
        if bad:
            self.start_btn.state(["disabled"])
            self.hint.configure(text="Still needed: " + ", ".join(bad))
        else:
            self.start_btn.state(["!disabled"])
            self.hint.configure(text="")

    # --------------------------------------------------------------- screen 3

    def show_running(self):
        self.clear()
        ttk.Label(self.body, text="Running",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        ttk.Label(self.body, foreground="#555", wraplength=620,
                  text="%s opens in a Firefox window of its own, by itself, "
                       "once the backend answers. Open Dashboard does it again "
                       "-- for a window closed by mistake."
                       % self.dashboard_url()).pack(anchor="w", pady=(2, 10))

        self.log_widget = scrolledtext.ScrolledText(self.body, height=16, width=84,
                                                    state="disabled", wrap="word",
                                                    font=("TkFixedFont", 9))
        self.log_widget.pack(fill="both", expand=True)

        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(10, 0))
        self.end_btn = ttk.Button(row, text="End", command=self.on_end)
        self.end_btn.pack(side="left")
        self.open_btn = ttk.Button(row, text="Open Dashboard", command=self.open_browser)
        self.open_btn.pack(side="left", padx=(8, 0))
        if self.edge.get():
            self.cam_btn = ttk.Button(row, text="Edge Camera", command=self.on_camera)
            self.cam_btn.pack(side="left", padx=(8, 0))
        else:
            self.cam_btn = None
        self.status = ttk.Label(row, foreground="#555", text="")
        self.status.pack(side="right")

    # ----------------------------------------------------------------- start

    def start(self):
        if self.local.get() and not self.apply_vlm_profile():
            return

        argv = ["sudo", "-n", "bash", RUN_HPC]
        if self.local.get():
            argv += ["--local",
                     "--vlm-host", self.vlm_host.get().strip(),
                     "--vlm-port", self.vlm_port.get().strip()]
        if self.edge.get():
            argv += ["--edge-device",
                     "--topaz-host", self.topaz_host.get().strip(),
                     "--topaz-port", self.topaz_port.get().strip()]
        if self.manual.get():
            argv.append("--manual")

        self.show_running()
        self.log("$ " + " ".join(shlex.quote(a) for a in argv))
        try:
            self.proc = subprocess.Popen(
                argv, cwd=REPO,
                stdin=subprocess.DEVNULL,           # no terminal: never let it prompt
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                start_new_session=True,             # gives End a process group to signal
            )
        except OSError as exc:
            self.log("error: could not start run_hpc.sh: %s" % exc)
            messagebox.showerror("Start failed", str(exc))
            return
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        threading.Thread(target=self._await_backend, daemon=True).start()
        self.status.configure(text="dashboard running")

    def apply_vlm_profile(self):
        """Put the direct-link NIC back on the VLM profile. A failure here is
        shown but not fatal: the profile is usually already up, and the operator
        may well have the link arranged some other way."""
        cmd = ["sudo", "-n", "nmcli", "connection", "up", VLM_PROFILE, "ifname", VLM_IFACE]
        try:
            done = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = str(exc)
        else:
            if done.returncode == 0:
                return True
            detail = (done.stderr or done.stdout).strip()
        return messagebox.askokcancel(
            "Network profile",
            "Could not bring %s up on %s:\n\n%s\n\nStart the dashboard anyway?"
            % (VLM_PROFILE, VLM_IFACE, detail or "nmcli failed"))

    def _pump(self, proc, prefix=""):
        """Drain a child's output into the log. Also the reason stdout is a pipe
        at all: left unread it fills at ~64KB and blocks the child mid-run."""
        try:
            for line in proc.stdout:
                self.log(prefix + line.rstrip("\n"))
        except (ValueError, OSError):
            pass

    def _watch(self):
        """Notice run_hpc.sh exiting on its own -- a bad venv, a port already
        taken -- so screen 3 does not keep claiming it is running."""
        proc = self.proc
        proc.wait()
        if self.stopped:
            return
        self.log("run_hpc.sh exited with status %s." % proc.returncode)
        self.widget_do(self.status, lambda w: w.configure(text="dashboard stopped"))
        self.widget_do(self.end_btn, lambda w: w.state(["disabled"]))

    # --------------------------------------------------------------- browser

    def dashboard_url(self):
        """Which address to hand the browser. See the BACKEND_PROBE comment:
        the direct-link shells and the nginx shell are different pages, and
        each only works on its own origin."""
        return DIRECT_URL if (self.local.get() or self.edge.get()) else NGINX_URL

    def _backend_answers(self):
        """True once something is serving HTTP on :5000.

        Any status counts, 500 included. The question here is whether uvicorn
        has bound and is replying at all -- a route that errors is still a
        backend that is up, and waiting for a 200 would hang on a dashboard
        that merely has one panel misconfigured."""
        try:
            urllib.request.urlopen(BACKEND_PROBE, timeout=2).close()
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError):
            return False

    def _await_backend(self):
        """Poll until the backend answers, then open the browser once.

        Deliberately a probe rather than a line-match on run_hpc.sh's output:
        uvicorn prints "Application startup complete" before it has finished
        binding in some configurations, and the log text is not ours to depend
        on. A socket that answers is the thing we actually care about."""
        proc = self.proc
        deadline = time.time() + BACKEND_TIMEOUT
        self.log("Waiting for the backend on %s ..." % BACKEND_PROBE)
        told = 0.0
        while time.time() < deadline:
            # Give up quietly if End was pressed or run_hpc.sh died -- _watch
            # reports the exit, and a browser opening onto a dead port after
            # the operator has already stopped everything is just confusing.
            if self.stopped or proc is None or proc.poll() is not None:
                return
            if self._backend_answers():
                self.log("Backend is up.")
                self.open_browser()
                return
            waited = time.time() - (deadline - BACKEND_TIMEOUT)
            if waited - told >= 15:
                told = waited
                self.log("  ... still waiting (%ds)" % waited)
            time.sleep(BACKEND_POLL)
        self.log("Backend did not answer within %ds -- not opening the browser. "
                 "Use Open Dashboard once it comes up." % BACKEND_TIMEOUT)

    def open_browser(self):
        """Start the browser detached from us.

        start_new_session matters here for the opposite reason it does at
        Start: it keeps the browser out of any process group this launcher
        signals, so End tears down the dashboard without also shutting the
        operator's window in their face."""
        url = self.dashboard_url()
        try:
            subprocess.Popen(
                [BROWSER] + BROWSER_ARGS + [url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError as exc:
            self.log("could not start %s: %s -- open %s yourself."
                     % (BROWSER, exc, url))
            return
        self.log("Opened %s in %s." % (url, BROWSER))

    # ------------------------------------------------------------------ stop

    def stop_all(self, then=None):
        """End, and also what the window's X does. Runs off the Tk thread: the
        ssh teardown inside run_hpc.sh's trap takes seconds, and a frozen window
        for that long reads as a crash."""
        if self.stopped:
            if then:
                self.ui(then)
            return
        self.stopped = True
        self.widget_do(self.end_btn, lambda w: w.state(["disabled"]))
        self.widget_do(self.cam_btn, lambda w: w.state(["disabled"]))
        self.widget_do(self.status, lambda w: w.configure(text="stopping..."))
        threading.Thread(target=self._stop_worker, args=(then,), daemon=True).start()

    def _stop_worker(self, then):
        if self.cam_running:
            self.log("Stopping the edge camera...")
            self._stop_camera()
        self._stop_dashboard()
        self.log("Everything stopped.")
        self.widget_do(self.status, lambda w: w.configure(text="stopped"))
        if then:
            self.ui(then)

    def _stop_dashboard(self):
        proc = self.proc
        if proc is None or proc.poll() is not None:
            self.log("Dashboard: nothing of ours still running.")
            return
        pgid = proc.pid   # start_new_session made it the group leader
        self.log("Stopping the dashboard (this also stops the far-end programs)...")
        for sig, budget in STOP_STAGES:
            # Signalling the GROUP, not the pid, is what makes this equivalent to
            # Ctrl+C: run_hpc.sh's backgrounded ssh clients are in that group and
            # its trap relies on them having been hit too.
            #
            # Through sudo because run_hpc.sh is root's; through bash because its
            # builtin kill takes a negative pid on every shell this may meet.
            subprocess.run(["sudo", "-n", "bash", "-c", "kill -%s -- -%d" % (sig, pgid)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
            deadline = time.time() + budget
            while time.time() < deadline:
                if proc.poll() is not None:
                    self.log("Dashboard: stopped (SIG%s)." % sig)
                    self._sweep_group(pgid)
                    return
                time.sleep(0.2)
            self.log("Dashboard: still up %.0fs after SIG%s, escalating." % (budget, sig))
        self.log("warning: the dashboard survived SIGKILL.")
        self._sweep_group(pgid)

    def _sweep_group(self, pgid):
        """run_hpc.sh exiting does not by itself empty its process group, and a
        survivor there still holds whatever port or device it had.

        SIGINT is why: a shell started `cmd &` non-interactively sets SIGINT to
        ignored in the child, so the group signal that makes run_hpc.sh's trap
        fire is the one signal its backgrounded ssh clients are deaf to.
        Measured -- a `sleep 600 &` under the same group outlived the SIGINT
        that stopped its parent. remote_launch.sh does TERM each ssh client it
        started, so this is normally a no-op; it is here for the launch that
        died before those traps were installed, and for anything that learns to
        background itself later.

        Only ever our own session: pgid is the process group this launcher
        created with start_new_session, so nothing outside what we started can
        be in it."""
        def group_alive():
            return subprocess.run(
                ["sudo", "-n", "bash", "-c", "kill -0 -- -%d" % pgid],
                stdin=subprocess.DEVNULL, capture_output=True).returncode == 0

        if not group_alive():
            return
        self.log("Dashboard: something is still in its process group, clearing it.")
        for sig in ("TERM", "KILL"):
            subprocess.run(["sudo", "-n", "bash", "-c", "kill -%s -- -%d" % (sig, pgid)],
                           stdin=subprocess.DEVNULL, capture_output=True)
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if not group_alive():
                    self.log("Dashboard: process group clear.")
                    return
                time.sleep(0.2)
        self.log("warning: the dashboard's process group survived SIGKILL.")

    # ---------------------------------------------------------------- camera

    def on_camera(self):
        if self.cam_running:
            return
        self.cam_running = True
        self.cam_btn.state(["disabled"])
        self.cam_btn.configure(text="Camera running")
        threading.Thread(target=self._camera_worker, daemon=True).start()

    def _camera_worker(self):
        self.log("")
        self.log("Edge camera: starting the viewer on this machine...")
        try:
            self.cam_proc = subprocess.Popen(
                ["bash", DISPLAY_DEMO], cwd=REPO,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True)
        except OSError as exc:
            self.log("Edge camera: could not start the viewer: %s" % exc)
            self._camera_finished()
            return
        threading.Thread(target=self._pump, args=(self.cam_proc, "  [viewer] "),
                         daemon=True).start()

        # The viewer has to own its socket before the board dials in.
        time.sleep(CAMERA_SSH_DELAY)
        if self.cam_proc.poll() is not None:
            self.log("Edge camera: the viewer exited immediately -- see its output "
                     "above. The board was NOT asked to start, so nothing is left "
                     "running on it.")
            self._camera_finished()
            return

        if self._start_remote_webcam():
            self.log("Edge camera: the feed appears in a few seconds. "
                     "Press q IN THE VIDEO WINDOW to quit it -- that is the only "
                     "clean way out, so there is deliberately no stop button here.")
        self.cam_proc.wait()
        self.log("Edge camera: the viewer has closed.")
        self._stop_camera()
        self._camera_finished()

    def _camera_finished(self):
        self.cam_running = False
        self.cam_proc = None
        def reset(w):
            if not self.stopped:
                w.configure(text="Edge Camera")
                w.state(["!disabled"])
        self.widget_do(self.cam_btn, reset)

    def _ssh_topaz(self):
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.topaz_host.get().strip(),
                       username=TOPAZ_SSH_USER, password=TOPAZ_SSH_PASS,
                       timeout=10, allow_agent=False, look_for_keys=False)
        return client

    def _start_remote_webcam(self):
        host = self.topaz_host.get().strip()
        self.log("Edge camera: asking %s@%s to start the webcam..." % (TOPAZ_SSH_USER, host))
        try:
            self.cam_ssh = self._ssh_topaz()
            chan = self.cam_ssh.get_transport().open_session()
            # The sudo password goes down the channel's STDIN, never into the
            # command -- an argument would be world-readable in the board's ps,
            # which is the same reason remote_launch.sh feeds bash on stdin.
            chan.exec_command("sudo -S -p '' bash %s" % shlex.quote(TOPAZ_WEBCAM_SCRIPT))
            chan.sendall(TOPAZ_SUDO_PASS + "\n")
            threading.Thread(target=self._pump_channel, args=(chan,), daemon=True).start()
        except Exception as exc:                 # paramiko raises a wide family
            self.log("Edge camera: could not start the board's webcam: %s" % exc)
            self.log("             The viewer is still up; press q in it to close.")
            self._close_cam_ssh()
            return False
        self.log("Edge camera: board webcam started.")
        return True

    def _pump_channel(self, chan):
        """setup_webcam.sh stays in the foreground for as long as the camera
        runs, so this channel is also how its output reaches the log."""
        try:
            buf = b""
            while True:
                data = chan.recv(4096)
                if not data:
                    break
                buf += data
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    self.log("  [board] " + line.decode("utf-8", "replace").rstrip("\r"))
        except Exception:
            pass

    def _stop_camera(self):
        """Take the board's webcam back down. Only ever called for a camera this
        launcher started, and only by pattern -- there is no pidfile on that side
        to prove ownership the way remote_launch.sh does.

        Two threads reach here for the same camera: End's teardown, and the
        worker waking from the viewer's q-exit (which End itself causes, by
        terminating the viewer). The lock makes the loser a no-op instead of a
        second pkill against a board that has already let go of the device."""
        with self.cam_lock:
            if not self.cam_running:
                return
            self.cam_running = False
        self._close_cam_ssh()
        # finally, not sequential: an unreachable board must not leave the
        # viewer window open with nothing left to feed it. Ending the launcher
        # would then close the window and orphan that process.
        try:
            self._kill_remote_webcam()
        finally:
            self._kill_viewer()

    def _kill_remote_webcam(self):
        host = self.topaz_host.get().strip()
        try:
            client = self._ssh_topaz()
        except Exception as exc:
            self.log("Edge camera: could not reach %s to stop the board's webcam: %s"
                     % (host, exc))
            return
        try:
            chan = client.get_transport().open_session()
            # `bash -s`, with BOTH the password and the script on stdin, for the
            # reason remote_launch.sh spells out: anything in the command line is
            # visible in the board's own ps. Here that would be worse than a
            # leaked password -- the patterns themselves would be in the argv of
            # the very shell running the pkill, so `pkill -f usb_webcam_capture`
            # would match its own parent and kill the script halfway through,
            # before the second pattern ever ran. With the script on stdin that
            # shell's command line is `bash -s` and matches nothing.
            chan.exec_command("sudo -S -p '' bash -s")
            script = "".join("pkill -f %s || true\n" % shlex.quote(pat)
                             for pat in TOPAZ_WEBCAM_PATTERNS)
            chan.sendall(TOPAZ_SUDO_PASS + "\n" + script + "exit 0\n")
            chan.shutdown_write()
            chan.recv_exit_status()
            self.log("Edge camera: board webcam stopped.")
        except Exception as exc:
            self.log("Edge camera: stopping the board's webcam failed: %s" % exc)
        finally:
            client.close()

    def _kill_viewer(self):
        """The viewer normally exits first (q pressed) and this is a no-op. It is
        not one when End or the window's X is what got here."""
        proc = self.cam_proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _close_cam_ssh(self):
        if self.cam_ssh is not None:
            try:
                self.cam_ssh.close()
            except Exception:
                pass
            self.cam_ssh = None

    # ----------------------------------------------------------------- exits

    def on_end(self):
        self.stop_all()

    def on_close(self):
        """The window's X. Closing the launcher has to take the dashboard with
        it -- leaving it running would leave the far-end programs running too,
        with nothing left that knows how to stop them."""
        if self.proc is None or self.proc.poll() is not None:
            self.root.destroy()
            return
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)   # one teardown only
        self.stop_all(then=self.root.destroy)


def require_paramiko():
    """Make sure the Edge Camera's ssh will work, now, while a failure is still
    just a message on a terminal.

    Checked at startup and not at the button for the reason in the header: the
    venv imports tkinter happily and only falls over on paramiko, so a launcher
    started the wrong way looks fine right up until the camera is wanted.

    Re-exec rather than refuse, because `python3 dashboard_launcher.py` is the
    natural thing to type and there is no reason it should not work. The guard
    variable is what stops that becoming a loop if the system interpreter cannot
    import paramiko either."""
    try:
        import paramiko  # noqa: F401
        return
    except ImportError:
        pass

    # No "am I already the system interpreter" test here, deliberately. The
    # obvious one -- comparing realpath(sys.executable) against SYSTEM_PYTHON --
    # is WRONG for the exact case this exists to fix: .venv-hpc/bin/python3 is a
    # symlink to /usr/bin/python3, so the two realpaths are equal and the venv
    # would be mistaken for the system python and never re-exec. The failed
    # import above is the only signal that matters, and the guard variable is
    # what keeps a re-exec that does not help from looping.
    if not os.environ.get("RSI_LAUNCHER_REEXEC") and os.access(SYSTEM_PYTHON, os.X_OK):
        os.environ["RSI_LAUNCHER_REEXEC"] = "1"
        os.execv(SYSTEM_PYTHON, [SYSTEM_PYTHON, os.path.abspath(__file__)] + sys.argv[1:])

    raise SystemExit(
        "error: paramiko is missing, so the Edge Camera could not reach the Topaz\n"
        "       board. Running under %s.\n"
        "       Install it with:  sudo apt install python3-paramiko\n"
        "       or start the launcher with:  %s %s"
        % (sys.executable, SYSTEM_PYTHON, os.path.abspath(__file__)))


def check_environment():
    """--check: answer "will this work" without starting anything."""
    import paramiko
    print("interpreter   : %s" % sys.executable)
    print("paramiko      : %s" % paramiko.__version__)
    print("tkinter       : %s" % tk.TkVersion)
    for name, path in (("run_hpc.sh", RUN_HPC), ("display_demo.sh", DISPLAY_DEMO)):
        print("%-14s: %s" % (name, path if os.access(path, os.R_OK) else "MISSING (%s)" % path))
    print("display       : %s" % (os.environ.get("DISPLAY") or "unset"))
    browser = shutil.which(BROWSER)
    print("%-14s: %s" % (BROWSER, browser or "MISSING (the browser will not open)"))
    print("dashboard url : %s  (direct link)" % DIRECT_URL)
    print("              : %s  (lab LAN, via nginx)" % NGINX_URL)


def main():
    if not os.access(RUN_HPC, os.R_OK):
        raise SystemExit("error: %s is missing." % RUN_HPC)
    require_paramiko()
    if "--check" in sys.argv[1:]:
        check_environment()
        return
    root = tk.Tk()
    Launcher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
