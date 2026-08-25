"""
target_topaz2_edac.py — EDAC demo controller, target side.

Drives the AICRAFT EDAC demo (Reed-Solomon RS(255,223) parity kept in a 256 KB
SPI FRAM) on behalf of the dashboard's EDAC page. Lives beside
target_topaz2_standalone.py and is imported by it; that file only routes
"edac_*" commands here, so nothing about EDAC leaks into the 1300 lines of
metrics/IMU/AI-workload code around it.

Kept separate for two reasons beyond tidiness:

  * the import is guarded on the caller's side, so a board where the EDAC repo
    has not been deployed still runs metrics, IMU and the AI workloads exactly
    as before — the EDAC page simply reports itself unavailable; and
  * the EDAC wrapper is a *third-party* package loaded off sys.path
    (/home/user/test/AICRAFT_EDAC_DEMO/python). Confining that path fiddling to
    one module keeps it out of the agent's own import graph.

THE DEMO LOOP
-------------
    load  -> copy a pristine test image into a scratch directory
    protect -> RS parity computed and written to a FRAM slot
    inject  -> one of nine radiation / flash fault models corrupts the copy
    recover -> parity rebuilds the file; CRC32 says whether it worked

Three details of the C tool shape this module, all of them learned the hard way
and all of them handled here rather than left to the page:

  1. `inject` corrupts its target IN PLACE. So the demo never touches
     test_data/ — a working copy is made in WORK_DIR and everything happens
     there. An interrupted demo therefore cannot leave the source images
     damaged.

  2. A multi-layer file can be recovered only ONCE per encode: recovery
     consumes the intermediate .LN.par files. Three of the four demo images are
     multi-layer, so after every successful recovery this module copies the
     recovered bytes back over the working file and re-encodes. The page can
     then loop corrupt -> recover as many times as the operator likes.

  3. Every wrapper call BLOCKS (subprocess.run underneath). Each command runs on
     its own worker thread and a lock serialises them, so the agent's message
     listener is never held up by an encode.

Progress goes back to the host as JSON on IMAGE_PORT, one object per
connection, the same channel the AI workloads use. Image bytes ride along
base64-encoded in an "edac_image" message; the host decodes them and does the
rendering, which keeps every display decision on the side of the link that can
be edited without redeploying to the board.
"""

import base64
import os
import shutil
import sys
import threading
import traceback

# The EDAC wrapper is a separate checkout, not installed into this interpreter.
# Adding its python/ directory is all that is needed — the package is pure
# Python 3.8+ with no dependencies of its own.
EDAC_ROOT = "/home/user/test/AICRAFT_EDAC_DEMO"
EDAC_PYTHON_PATH = os.path.join(EDAC_ROOT, "python")
EDAC_BINARY = os.path.join(EDAC_ROOT, "build", "edac_demo")

if EDAC_PYTHON_PATH not in sys.path:
    sys.path.insert(0, EDAC_PYTHON_PATH)

from edac import EdacDemo, EdacError, ErrorType   # noqa: E402

# Real SPI FRAM only. The point of the demo is that parity lives on separate
# radiation-hard storage, so quietly falling back to a file on the same eMMC
# would demonstrate the opposite of what it claims.
FRAM_DEVICE = "/dev/spidev0.0"

# Pristine sources, scanned for the page's image menu. Never written to.
DEMO_IMAGE_DIRS = (
    os.path.join(EDAC_ROOT, "test_data"),
    os.path.join(EDAC_ROOT, "test_data2"),
)
DEMO_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

# Scratch space for the copy the demo actually damages. Deliberately short and
# outside the EDAC checkout: the slot table stores the full path and truncates
# names at 63 characters.
WORK_DIR = "/home/user/edac_work"

# FRAM geometry, mirrored from src/spi_fram.h and src/edac_slots.h so the page
# can draw the chip to scale without a round trip.
FRAM_SIZE_BYTES = 0x40000     # 256 KiB
FRAM_SLOT_COUNT = 16
FRAM_SLOT_BYTES = 0x3E00      # 15.5 KB of parity pool per slot

# encode() on a multi-megabyte file does real work; the wrapper's own default is
# 60 s, which the largest demo image can approach on this board.
ENCODE_TIMEOUT = 300.0
RECOVER_TIMEOUT = 300.0

# Guard against a hand-made request asking for a file too large to base64 into
# one progress message. The demo images top out at 1.3 MB.
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class EdacDemoController:
    """One EDAC demo session: which file is loaded and how far along it is.

    Every public run_* method returns immediately and does its work on a worker
    thread, reporting through `publish` (the agent's send_progress_update). One
    operation at a time — `_busy` rejects overlapping commands rather than
    queueing them, because two encodes racing on the same slot would interleave
    their SPI traffic.
    """

    # Phases the page's stepper follows. "recovered" is functionally the same
    # state as "protected" (see the re-encode in _recover), but the page needs
    # to know a recovery just happened so it can show the verdict.
    PHASES = ("idle", "loaded", "protected", "corrupted", "recovered")

    def __init__(self, publish):
        self.publish = publish
        self._lock = threading.Lock()
        self._busy = False

        self.demo = EdacDemo(
            backend="real",
            device=FRAM_DEVICE,
            binary=EDAC_BINARY,
            cwd=EDAC_ROOT,
        )

        self.phase = "idle"
        self.file_id = None          # catalogue id of the loaded image
        self.file_label = None       # what the page shows in its menu
        self.source_path = None      # pristine original, never written to
        self.work_path = None        # the copy the demo damages
        self.slot_index = None
        self.layers = None
        self.last_fault = None       # human name of the last injected fault
        self.image_seq = 0           # cache-buster for the host's stage files

        # Last thing said to the page, repeated by a bare _state() (see there).
        self._last_message = ""
        self._last_detail = ""
        self._last_verdict = None

        os.makedirs(WORK_DIR, exist_ok=True)

    # -- catalogue ---------------------------------------------------------

    def catalogue(self):
        """The demo images available on this board, newest scan each time.

        Sorted by size so the page's menu runs small -> large, which also runs
        single-layer -> multi-layer: the cheapest demo is first.
        """
        files = []
        for directory in DEMO_IMAGE_DIRS:
            if not os.path.isdir(directory):
                continue
            group = os.path.basename(directory)
            for name in sorted(os.listdir(directory)):
                if not name.lower().endswith(DEMO_IMAGE_EXTS):
                    continue
                path = os.path.join(directory, name)
                if not os.path.isfile(path):
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                files.append({
                    "id": "{}/{}".format(group, name),
                    "label": name,
                    "group": group,
                    "size": size,
                    "path": path,
                })
        files.sort(key=lambda f: f["size"])
        return files

    def _find(self, file_id):
        for entry in self.catalogue():
            if entry["id"] == file_id:
                return entry
        return None

    # -- FRAM state --------------------------------------------------------

    def fram_snapshot(self):
        """The slot table plus the geometry the page draws it with.

        Never raises: an unreadable table (an unformatted store, most likely)
        is a state the page has to render, not a crash. `error` carries the
        reason so the page can say why the slots are blank.
        """
        snapshot = {
            "size_bytes": FRAM_SIZE_BYTES,
            "slot_count": FRAM_SLOT_COUNT,
            "slot_bytes": FRAM_SLOT_BYTES,
            "device": FRAM_DEVICE,
            "slots": [],
            "slots_used": 0,
            "error": None,
        }
        try:
            result = self.demo.list_slots()
        except EdacError as exc:
            snapshot["error"] = str(exc)
            return snapshot

        snapshot["slot_count"] = result.slot_count or FRAM_SLOT_COUNT
        snapshot["slots_used"] = result.slots_used
        for slot in result.slots:
            snapshot["slots"].append({
                "index": slot.index,
                "in_use": slot.in_use,
                "name": slot.basename,
                "size": slot.size,
                "layers": slot.layers,
                "fram_offset": slot.fram_offset,
                "fram_size": slot.fram_size,
                # True for the slot this demo session owns, so the page can
                # highlight "ours" among any slots left by other work.
                "active": slot.in_use and slot.index == self.slot_index,
            })
        return snapshot

    # -- outgoing messages -------------------------------------------------

    def _state(self, message=None, detail="", verdict=None, fault=None,
               include_fram=True, reset_stages=None):
        """One 'here is where the demo stands' message.

        Sent after every step and on demand, so a page that loads (or reloads)
        mid-demo can render the whole thing from a single object.

        The message, detail and verdict are STICKY: calling this with no message
        repeats the last one instead of blanking it. That matters because every
        operation ends with a bare _state() from _guarded() to clear the busy
        flag — without stickiness that trailing message would wipe the status
        line and, worse, take the recovery verdict banner down with it a
        moment after it appeared. Passing a message explicitly replaces all
        three, so a new step still clears the previous step's verdict.

        `reset_stages` names frames that the step about to run makes stale — a
        new Load invalidates all three, a new fault invalidates the previous
        recovery. It is sent BEFORE the replacement frame so the host never
        drops the image it has only just been given.
        """
        if message is None:
            message = self._last_message
            detail = self._last_detail
            verdict = self._last_verdict
        else:
            self._last_message = message
            self._last_detail = detail
            self._last_verdict = verdict

        payload = {
            "type": "edac_state",
            "phase": self.phase,
            "busy": self._busy,
            "message": message,
            "detail": detail,
            "file_id": self.file_id,
            "file_label": self.file_label,
            "slot_index": self.slot_index,
            "layers": self.layers,
            "last_fault": fault if fault is not None else self.last_fault,
            "image_seq": self.image_seq,
            "device": FRAM_DEVICE,
        }
        payload["verdict"] = verdict
        if reset_stages:
            payload["reset_stages"] = list(reset_stages)
        if include_fram:
            payload["fram"] = self.fram_snapshot()
        self.publish(payload)

    def _error(self, message, detail=""):
        self.phase = self.phase if self.phase in self.PHASES else "idle"
        self._last_message = message
        self._last_detail = detail
        self._last_verdict = None
        self.publish({
            "type": "edac_error",
            "message": message,
            "detail": detail,
            "phase": self.phase,
            "busy": False,
        })

    def _send_image(self, stage, path):
        """Ship one stage's bytes to the host, base64 in a JSON message.

        The raw file goes over, not a rendering of it: decoding a *corrupted*
        image is the interesting part and belongs on the host, where it can be
        changed without redeploying to the board. A file too damaged to decode
        is a result the host renders as such.
        """
        try:
            size = os.path.getsize(path)
            if size > MAX_IMAGE_BYTES:
                self._error("Image too large to display",
                            "{} bytes exceeds the {} byte transfer limit"
                            .format(size, MAX_IMAGE_BYTES))
                return
            with open(path, "rb") as handle:
                blob = handle.read()
        except OSError as exc:
            self._error("Could not read {} image".format(stage), str(exc))
            return

        self.image_seq += 1
        self.publish({
            "type": "edac_image",
            "stage": stage,
            "name": os.path.basename(path),
            "ext": os.path.splitext(path)[1].lower().lstrip("."),
            "size": len(blob),
            "seq": self.image_seq,
            "b64": base64.b64encode(blob).decode("ascii"),
        })

    # -- command entry points ----------------------------------------------

    def handle(self, message):
        """Route one "edac_*" command from the host onto a worker thread.

        Returns True when the message was ours, so the caller's dispatch chain
        can fall through to its other handlers otherwise.
        """
        if not message.startswith("edac_"):
            return False

        parts = message.split(":")
        command = parts[0]
        args = parts[1:]

        handlers = {
            "edac_status": (self._status, ()),
            "edac_selftest": (self._selftest, ()),
            "edac_load": (self._load, (args[0] if args else "",)),
            "edac_protect": (self._protect, ()),
            "edac_inject": (self._inject, (args[0] if args else "1",
                                           args[1] if len(args) > 1 else "")),
            "edac_recover": (self._recover, ()),
            "edac_reset": (self._reset, ()),
            "edac_format": (self._format, ()),
        }
        entry = handlers.get(command)
        if entry is None:
            print("Unknown EDAC command: {}".format(message))
            return True

        func, func_args = entry
        threading.Thread(target=self._guarded, args=(func, func_args),
                         daemon=True).start()
        return True

    def _guarded(self, func, args):
        """Run one operation with the busy flag held and every failure reported.

        A wrapper call that raises must still leave the page in a usable state,
        so EdacError (and anything else) becomes an edac_error message plus a
        fresh state rather than a dead thread and a spinner that never stops.
        """
        with self._lock:
            if self._busy:
                self._error("EDAC is busy", "another operation is still running")
                return
            self._busy = True
        try:
            func(*args)
        except EdacError as exc:
            print("EDAC error: {}".format(exc))
            self._error("EDAC operation failed", str(exc))
        except Exception as exc:                        # noqa: BLE001
            traceback.print_exc()
            self._error("EDAC operation failed", str(exc))
        finally:
            with self._lock:
                self._busy = False
            self._state()

    # -- operations --------------------------------------------------------

    def _status(self):
        """Everything a freshly opened page needs: menu, FRAM table, phase."""
        self.publish({
            "type": "edac_catalogue",
            "files": [{k: v for k, v in f.items() if k != "path"}
                      for f in self.catalogue()],
            "faults": [{"index": t.index, "name": t.name,
                        "description": t.description, "category": t.category}
                       for t in self._fault_types()],
            "device": FRAM_DEVICE,
        })
        self._state("Ready")

    def _fault_types(self):
        """The nine fault models, read out of the binary rather than hardcoded.

        Falls back to the IntEnum's names if `inject list` cannot be run, so the
        page's menu is never empty.
        """
        try:
            return self.demo.error_types()
        except EdacError:
            class _Fallback:
                def __init__(self, t):
                    self.index = int(t)
                    self.name = t.name.replace("_", " ").title()
                    self.description = ""
                    self.category = "Radiation" if t.is_radiation else "Flash/eMMC"
            return [_Fallback(t) for t in ErrorType]

    def _selftest(self):
        """FRAM identity + write/read-back round trip. A failure is a verdict."""
        self._state("Checking FRAM…", include_fram=False)
        result = self.demo.selftest()
        self._state("FRAM self-test {}".format("passed" if result.passed else "FAILED"),
                    detail=result.summary(),
                    verdict={"kind": "selftest", "passed": result.passed,
                             "summary": result.summary()})

    def _load(self, file_id):
        """Take a fresh working copy of a pristine demo image and show it.

        Any slot this session previously owned is released first: repeated
        loads would otherwise walk through all 16 slots in a long demo session.
        """
        entry = self._find(file_id)
        if entry is None:
            self._error("No such demo image", file_id)
            return

        self._release_slot()

        work_path = os.path.join(WORK_DIR, entry["label"])
        self.file_id = entry["id"]
        self.file_label = entry["label"]
        self.source_path = entry["path"]
        self.work_path = work_path
        self.slot_index = None
        self.layers = None
        self.last_fault = None
        self.phase = "loaded"

        # Announce the new phase and drop the old run's frames first, so the
        # host has already forgotten them when the fresh original arrives.
        self._state("Loading {}…".format(entry["label"]), include_fram=False,
                    reset_stages=("original", "corrupted", "recovered"))
        shutil.copy2(entry["path"], work_path)
        self._clear_parity_files(work_path)

        self._send_image("original", work_path)
        self._state("{} loaded — not yet protected".format(entry["label"]),
                    detail="{:,} bytes".format(entry["size"]))

    def _protect(self):
        """Compute RS parity and store the final layer in a FRAM slot."""
        if self.work_path is None:
            self._error("Nothing to protect", "choose an image first")
            return

        self._state("Computing parity and writing to FRAM…", include_fram=False)
        result = self.demo.encode(self.work_path, timeout=ENCODE_TIMEOUT)

        self.slot_index = result.slot_index
        self.layers = result.layers
        self.phase = "protected"
        self._state(
            "Protected — parity stored in FRAM slot {}".format(result.slot_index),
            detail="{} parity layer(s)".format(result.layers or "?"))

    def _inject(self, type_arg, seed_arg):
        """Corrupt the working copy with one of the nine fault models."""
        if self.phase not in ("protected", "recovered"):
            self._error("Protect the file first",
                        "injecting into an unprotected file leaves nothing to "
                        "recover it with")
            return

        try:
            fault_type = int(type_arg)
        except ValueError:
            self._error("Invalid fault type", type_arg)
            return
        try:
            seed = int(seed_arg) if str(seed_arg).strip() else None
        except ValueError:
            seed = None

        # The previous cycle's recovery is about to be meaningless — drop it
        # before the new corrupted frame lands.
        self._state("Injecting fault…", include_fram=False,
                    reset_stages=("corrupted", "recovered"))
        result = self.demo.inject_error(fault_type, self.work_path, seed=seed)

        self.last_fault = result.type_name or "type {}".format(fault_type)
        self.phase = "corrupted"
        self._send_image("corrupted", self.work_path)
        self._state(
            "{} injected".format(self.last_fault),
            detail="{:,} byte(s), {:,} bit(s) affected".format(
                result.bytes_affected or 0, result.bits_affected or 0),
            fault=self.last_fault,
            verdict={"kind": "inject",
                     "fault": self.last_fault,
                     "bytes": result.bytes_affected,
                     "bits": result.bits_affected})

    def _recover(self):
        """Rebuild the file from FRAM parity, verify it, and re-arm the demo.

        The re-encode at the end is what makes the page's corrupt/recover cycle
        repeatable: recovery consumes the intermediate .LN.par files, so without
        it a second recovery of a multi-layer file would fail with "protection
        data incomplete" — and three of the four demo images are multi-layer.
        """
        if self.phase != "corrupted":
            self._error("Nothing to recover", "inject a fault first")
            return

        recovered_path = os.path.join(WORK_DIR, "recovered_" + self.file_label)
        self._state("Reading parity from FRAM and rebuilding…", include_fram=False)
        result = self.demo.recover(self.file_label, recovered_path,
                                   timeout=RECOVER_TIMEOUT)

        self.phase = "recovered"
        self._send_image("recovered", recovered_path)

        verdict = {
            "kind": "recover",
            "passed": result.passed,
            "repaired": result.repaired,
            "unrepairable": result.unrepairable,
            "summary": result.summary(),
        }

        if result.passed:
            # Put the good bytes back and rebuild the parity chain, so the page
            # can go straight round again without another Protect click.
            try:
                shutil.copy2(recovered_path, self.work_path)
                re_encoded = self.demo.encode(self.work_path, timeout=ENCODE_TIMEOUT)
                self.slot_index = re_encoded.slot_index
                self.layers = re_encoded.layers
                verdict["rearmed"] = True
            except EdacError as exc:
                # The recovery itself still succeeded — say so, and only warn
                # that the next round needs a manual Protect.
                print("EDAC re-arm failed: {}".format(exc))
                verdict["rearmed"] = False
                verdict["rearm_error"] = str(exc)

        self._state(
            "Integrity check {}".format("PASSED" if result.passed else "FAILED"),
            detail=result.summary(),
            verdict=verdict)

    def _reset(self):
        """Drop this session's slot and scratch files, back to an empty page."""
        self._state("Resetting…", include_fram=False)
        self._release_slot()
        if self.work_path:
            self._clear_parity_files(self.work_path)
            for path in (self.work_path,
                         os.path.join(WORK_DIR, "recovered_" + (self.file_label or ""))):
                try:
                    os.remove(path)
                except OSError:
                    pass

        self.phase = "idle"
        self.file_id = None
        self.file_label = None
        self.source_path = None
        self.work_path = None
        self.slot_index = None
        self.layers = None
        self.last_fault = None
        self._state("Reset")

    def _format(self):
        """Wipe the whole slot table. Destructive; the page confirms first."""
        self._state("Formatting FRAM…", include_fram=False)
        result = self.demo.format_store()
        self.slot_index = None
        self.layers = None
        if self.phase in ("protected", "corrupted", "recovered"):
            self.phase = "loaded"
        self._state("FRAM formatted — {} slots available"
                    .format(result.slot_count or FRAM_SLOT_COUNT),
                    detail=result.summary())

    # -- housekeeping ------------------------------------------------------

    def _release_slot(self):
        """Free the slot this session owns, if it still has one.

        delete() raises when no slot matches, which is the normal case on a
        first load — not worth surfacing.
        """
        if not self.file_label:
            return
        try:
            self.demo.delete(self.file_label)
        except EdacError:
            pass
        self.slot_index = None
        self.layers = None

    @staticmethod
    def _clear_parity_files(work_path):
        """Remove any .LN.par files left beside a working file.

        A recovery that fails partway can leave an orphan layer behind, and a
        stale .L1.par next to a freshly copied file would be decoded against
        the wrong data.
        """
        for layer in range(1, 8):
            try:
                os.remove("{}.L{}.par".format(work_path, layer))
            except OSError:
                pass


# Module-level singleton, created by the agent at startup. None until then, so
# a command arriving before setup() is ignored rather than crashing the
# listener thread.
controller = None


def setup(publish):
    """Build the controller. Returns None (and logs) if EDAC is unusable here.

    Called from the agent's main(); a board without the FRAM or without the
    EDAC checkout gets a working agent and an EDAC page that says so, rather
    than an agent that will not start.
    """
    global controller
    try:
        controller = EdacDemoController(publish)
        print("EDAC demo ready ({} via {})".format(EDAC_BINARY, FRAM_DEVICE))
    except Exception as exc:                            # noqa: BLE001
        controller = None
        print("EDAC demo unavailable: {}".format(exc))
    return controller


def handle_message(message):
    """Dispatch one host command. True when it was an EDAC command."""
    if not message.startswith("edac_"):
        return False
    if controller is None:
        print("EDAC command ignored (controller unavailable): {}".format(message))
        return True
    return controller.handle(message)
