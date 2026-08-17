# Installation

How to get `combined_hpc.py` and `combined_topaz2.py` running from a clean
machine. For the nginx/TLS/login layer in front of them, see
[`set_up_proxy_server.md`](set_up_proxy_server.md).

---

## 1. Target platform

These are the exact versions this project is built and tested against. Both apps
run on **Python 3.8**, which is end-of-life — that constraint drives most of the
decisions below, so do not casually "upgrade" past it without retesting.

| | Version |
|---|---|
| OS | Ubuntu 20.04.6 LTS (Focal Fossa) |
| Kernel | 5.15.0-139-generic |
| Python | 3.8.10 (`/usr/bin/python3.8`, the distro interpreter) |
| Architecture | `x86_64` for the HPC box; `aarch64` for the Topaz/RDB box |

Confirm what you have before starting:

```bash
lsb_release -d          # Description: Ubuntu 20.04.6 LTS
python3 -V              # Python 3.8.10
uname -m                # x86_64
```

---

## 2. Prerequisites

### 2a. APT packages (needed before anything else)

A stock Ubuntu 20.04 install ships `python3` but **not** pip, and **not** the
venv support module. Install both:

```bash
sudo apt update
sudo apt install python3-pip python3.8-venv
```

> **Why `python3.8-venv` is not optional.** Debian and Ubuntu strip the
> `ensurepip` module out of the base `python3` package and ship it separately.
> Without it, `python3 -m venv .venv-hpc` builds the directory skeleton and then
> fails with *"The virtual environment was not created successfully because
> ensurepip is not available"*. This is a distro packaging decision, not a bug in
> this project.

Verify:

```bash
python3 -m pip --version      # pip 20.0.2 ... (python 3.8)
python3 -c "import ensurepip; print('ok')"
```

### 2b. External tools the apps shell out to

The apps invoke these by name on `PATH`. Missing ones do not stop the server
from booting — the affected feature just fails at the point it is used.

| Tool | Used by | Purpose | Needed for the server to start? |
|---|---|---|---|
| `iperf3` | `combined_hpc` | Bandwidth test server + client runs | No, but the `NETRUN:` commands fail |
| `profiler` | `combined_hpc` | Launches the SAR workload (vendor tool) | No, but `RUN:` fails |
| `diagnostic` | `combined_hpc` | AI-card power/temperature polling (vendor tool) | No, those metrics stay empty |
| `ethtool` | `combined_hpc` | Link speed/NIC queries | No |
| `sudo` | `combined_hpc` | Wraps the privileged tools above | Depends on your sudoers setup |

`profiler` and `diagnostic` are **vendor binaries, not apt packages** — on this
machine they live in `/usr/local/bin`. They ship with the AI-card SDK and must be
installed separately.

```bash
for c in iperf3 profiler diagnostic ethtool; do
    printf '%-12s %s\n' "$c" "$(command -v $c || echo 'MISSING')"
done
```

### 2c. InfluxDB (metrics sink)

Both apps write metrics to InfluxDB on localhost. Neither app creates the
database, and both tolerate it being down — they log the failure and keep
serving. Install per
[`grafana_influxdb_target_setup.md`](grafana_influxdb_target_setup.md), then
confirm:

```bash
systemctl is-active influxd        # expect: active
curl -I http://127.0.0.1:8086      # expect 404 + X-Influxdb-Version header
```

Connection settings are hardcoded in both apps: host `localhost`, port `8086`,
database `system_metrics`, user/password `root`/`root`.

### 2d. Upload directory (HPC only)

CPHD uploads land in a `user_data/` subfolder of the root-owned CPHD store, so
create it once and hand it to the app's user:

```bash
sudo mkdir -p /home/public/sar/sar-server/data/cphd/user_data
sudo chown "$(whoami)" /home/public/sar/sar-server/data/cphd/user_data
```

---

## 3. Create the virtual environments

Each app gets its **own** venv. They are deliberately separate: `combined_hpc`
needs an imaging and numeric stack (Pillow, sarpy, numpy, scipy, psutil) while
`combined_topaz2` reads its metrics off a socket and needs none of it. That is
what keeps the ARM box installable without a compiler.

| App | Venv | Manifest | Launcher | Size |
|---|---|---|---|---|
| `combined_hpc.py` (:5000) | `.venv-hpc` | `requirements-hpc.txt` | `run_hpc.sh` | ~380 MB |
| `combined_topaz2.py` (:5001) | `.venv-topaz2` | `requirements-topaz2.txt` | `run_topaz2.sh` | ~61 MB |

```bash
cd /home/sarthak/Remote_Systems_Interact

# --- HPC box ---
python3 -m venv .venv-hpc
.venv-hpc/bin/pip install --upgrade pip setuptools wheel
.venv-hpc/bin/pip install -r requirements-hpc.txt

# --- Topaz/RDB box ---
python3 -m venv .venv-topaz2
.venv-topaz2/bin/pip install --upgrade pip setuptools wheel
.venv-topaz2/bin/pip install -r requirements-topaz2.txt
```

> **Do not skip the pip upgrade.** `ensurepip` seeds the venv with pip 20.0.2,
> which predates pip's modern dependency resolver and will happily install a
> conflicting set. The upgrade brings it to pip 25.x. Python 3.8 has no
> `--upgrade-deps` flag for `venv` (that arrived in 3.9), so it must be a
> separate command.

You only build the venv for the app that box actually runs. Nothing stops you
building both on one machine — this repo does, for testing.

### Verify

```bash
.venv-hpc/bin/pip check          # expect: No broken requirements found.
.venv-hpc/bin/python -c "import combined_hpc as m; print(len(m.app.routes), 'routes')"   # 27 routes

.venv-topaz2/bin/pip check
.venv-topaz2/bin/python -c "import combined_topaz2 as m; print(len(m.app.routes), 'routes')"  # 43 routes
```

---

## 4. Activating a venv

**You do not need to activate anything to run the apps.** The launchers call the
venv's interpreter by its full path, which is exactly equivalent:

```bash
./run_hpc.sh          # or ./run_topaz2.sh
```

Activation exists for *interactive* work — installing a package, poking at a
REPL, running a script with a bare `python`. When you do want it:

```bash
source .venv-hpc/bin/activate     # note: "source", not "./activate"
python -V                         # now resolves to the venv's 3.8.10
pip install something             # installs INTO .venv-hpc, not system-wide
deactivate                        # return to the normal shell
```

Two things that trip people up:

- It must be `source .venv-hpc/bin/activate` (or the shorthand
  `. .venv-hpc/bin/activate`). Running `./activate` starts a *child* shell that
  exits immediately, so your prompt is unchanged and nothing happens. The script
  edits `PATH` in the shell it runs in, so it has to run in yours.
- Activation is per-terminal and does not survive a new tab, a reconnect, or a
  reboot. That is precisely why the launchers exist — never rely on a human
  having activated the right venv before starting a service.

`activate` is not magic: all it does is put `.venv-hpc/bin` at the front of
`PATH`. `.venv-hpc/bin/python foo.py` achieves the same thing with no state.

---

## 5. Run the apps

```bash
./run_hpc.sh          # binds 0.0.0.0:5000
./run_topaz2.sh       # binds 0.0.0.0:5001, plus target sockets 12346 and 55556
```

| Port | Bound by | Purpose |
|---|---|---|
| 5000 | `combined_hpc` | HTTP: frontend + REST API |
| 5001 | `combined_topaz2` | HTTP: frontend + REST API |
| 12346 | `combined_topaz2` | Target → host system-metrics stream |
| 55556 | `combined_topaz2` | Target → host workload-progress stream |
| 5201 | `iperf3` (spawned by `combined_hpc`) | Bandwidth tests |
| 8086 | InfluxDB | Metrics sink |

Smoke test:

```bash
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/          # 200
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/system_metrics   # 200
```

Both apps run under uvicorn. They can also be started as ASGI modules, which is
the form to use with a process manager:

```bash
.venv-hpc/bin/uvicorn combined_hpc:app --host 0.0.0.0 --port 5000
```

For production, run under systemd or `tmux`/`screen` so they survive logout.
`combined_hpc` shells out to privileged tools, so it is typically started with
the privileges those need.

---

## 6. Dependency management

Each app has **two** manifests:

- `requirements-<app>.txt` — direct dependencies only, commented, hand-edited.
  **This is the one you change.**
- `requirements-<app>.lock.txt` — the full transitive closure from `pip freeze`,
  which is what makes a rebuild reproducible.

Everything is pinned with `==`. Because Python 3.8 is end-of-life, an unpinned
install drifts onto releases that have dropped 3.8 and either refuses to resolve
or quietly installs something older than what was tested.

After editing a manifest, refresh its lock:

```bash
.venv-hpc/bin/pip install -r requirements-hpc.txt
.venv-hpc/bin/pip freeze --exclude-editable | grep -v '^pkg_resources==' > requirements-hpc.lock.txt
```

> The `grep` drops `pkg_resources==0.0.0`, which Ubuntu's `python3-venv` injects
> into every venv it creates. No such package exists on PyPI, so leaving it in
> makes the lock file uninstallable.

To reproduce a known-good environment exactly, install from the lock and skip
resolution entirely:

```bash
.venv-hpc/bin/pip install --no-deps -r requirements-hpc.lock.txt
```

### Deliberate omissions

- **GDAL (`osgeo`)** is not a dependency. `get_resolution_from_tif()` tries it
  first but falls back to reading GeoTIFF tags via Pillow, and it has never been
  installed here. Adding it means system `libgdal` plus a version-matched
  binding, not a plain `pip install`.
- **`sarpy`** is in the HPC manifest but imported lazily inside a `try/except`.
  Without it the app still starts; the CPHD properties panel just comes back
  empty.
- **`influxdb`** is optional for `combined_topaz2` (guarded by `try/except`) and
  required for `combined_hpc`.

---

## 7. The Topaz / RDB box (ARM)

`combined_topaz2.py` runs on the ARM target at `192.168.0.32`, and its venv must
be built **on that machine**. A venv bakes in absolute paths and the host
architecture, so `.venv-topaz2` cannot be copied there from the x86_64 box —
and neither can `requirements-topaz2.lock.txt` be assumed to resolve identically.
Install from `requirements-topaz2.txt` and generate a separate lock there.

If that box has no internet, build a wheelhouse on a machine that does:

```bash
pip download -r requirements-topaz2.txt -d wheelhouse \
    --platform manylinux2014_aarch64 --python-version 38 --only-binary=:all:
# copy wheelhouse/ across, then on the ARM box:
.venv-topaz2/bin/pip install --no-index --find-links wheelhouse -r requirements-topaz2.txt
```

---

## 8. Troubleshooting

**`ensurepip is not available`** — `python3.8-venv` is missing. See §2a.

**`No module named venv` / no `pip3`** — `python3-pip` is missing. See §2a.

**`error: .venv-hpc is missing or broken`** from a launcher — the venv was never
built, or was deleted. Rebuild it per §3.

**Imports resolve to the wrong package version** — you are probably running the
system interpreter instead of the venv. The venvs are isolated (user-site
disabled, no `dist-packages` on the path); a bare `python3` is not. Check with:

```bash
.venv-hpc/bin/python -c "import sys, site; print(site.ENABLE_USER_SITE, [p for p in sys.path if 'dist-packages' in p or '.local' in p])"
# expect: False []
```

**`ImportError: cannot import name 'default_timer' from 'timeit'`** — the repo
contains a `timeit.py` that shadows the Python standard library for anything run
from this directory. Rename it if it starts causing trouble.

**Port already in use on :5000** — an old instance is still running:

```bash
ss -ltnp | grep ':5000'
```

Note that `combined_hpc` spawns an `iperf3` server on :5201 which can outlive the
parent if the parent is killed abruptly; kill it separately.

**502 from nginx** — the backend is down; start it with `./run_hpc.sh`. See
[`set_up_proxy_server.md`](set_up_proxy_server.md).

---

## 9. Quick reference

```bash
# one-time system prep
sudo apt install python3-pip python3.8-venv

# build
cd /home/sarthak/Remote_Systems_Interact
python3 -m venv .venv-hpc
.venv-hpc/bin/pip install --upgrade pip setuptools wheel
.venv-hpc/bin/pip install -r requirements-hpc.txt

# run
./run_hpc.sh

# interactive work inside the venv
source .venv-hpc/bin/activate
deactivate
```

The venvs are build output and are gitignored. The manifests and launchers are
committed — rebuilding from them is the only supported way to stand up a new box.
