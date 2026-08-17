# Setting Up the Nginx Reverse-Proxy + SSL on This Machine

This manual describes the nginx reverse-proxy + HTTPS setup **for this box as it is
now** — a single machine (`andromeda`, LAN `192.168.0.18`) that runs *everything*:
nginx, InfluxDB, and the combined HPC program. It reproduces the setup by hand so it
can be rebuilt or moved. Every step says *what it does* and *what changing it means*.

> **This supersedes the older Grafana-based design.** There is no longer a Grafana
> dashboard on port 3000, no kiosk `<iframe>` pages, and nothing proxied to `:3000`.
> [`combined_hpc.py`](combined_hpc.py) now serves its **own frontend** directly at
> `/` (and `/monitor`, `/sar_app`) and exposes the live metrics it collects at
> `/system_metrics`. nginx simply front-doors that one app.
>
> (It also supersedes the even older two-machine host + target design — no separate
> `flask_api.py`, no `TARGET_IP`/command socket, no remote host.)

---

## 0. What this machine is (read this first)

One box. nginx is the "front door"; **everything** it proxies to is a single app on
`127.0.0.1:5000` (localhost) of this same machine:

```
                       Internet
                          │
        DNS: nexon.aicraft.com.au ──► 49.176.249.9   (home router / public IP)
                          │
             router port-forward 80, 443 ─► 192.168.0.18   (THIS box)
                          │
                          ▼  https (443), http (80 → redirect to 443)
        ┌───────────────────────────────────────────────┐
        │                  nginx 1.18                    │  ← this manual
        │  - terminates TLS (Let's Encrypt)              │
        │  - Basic-Auth login gate                       │
        │  - proxies every path → combined_hpc           │
        └───────────────────────┬───────────────────────┘
                                │ all paths (/, /sar_app, /system_metrics, …)
                                ▼
                        127.0.0.1:5000
                ┌──────────────────────────────────┐
                │  combined_hpc.py                  │
                │  - serves the frontend (/,        │
                │    /monitor, /sar_app)            │
                │  - REST API + /system_metrics     │
                │  - in-process metrics collector   │
                └──────────────┬────────────────────┘
                               │ writes metrics in-process
                               ▼
                        127.0.0.1:8086
                        ┌──────────────────────────┐
                        │ InfluxDB 1.x             │  historical store; no
                        │                          │  dashboard reads it now
                        └──────────────────────────┘
```

### The moving parts

| Component | Listens on | Role | Covered here? |
|-----------|-----------|------|----------------|
| **nginx** | `0.0.0.0:80`, `0.0.0.0:443` | Reverse proxy, TLS, login gate | ✅ this manual |
| **certbot / Let's Encrypt** | (issues the cert) | Free HTTPS certificate | ✅ this manual |
| **combined_hpc.py** | `0.0.0.0:5000` | Serves the frontend + REST API + metrics collector (writes to InfluxDB) | ⛖ run only — see cross-ref |
| **InfluxDB** | `127.0.0.1:8086` | Time-series store the collector writes to (optional for the frontend) | ⛖ install only — see cross-ref |

> **Cross-refs (do these first):**
> - [`native_install_grafana_influxdb.md`](native_install_grafana_influxdb.md) — install InfluxDB (you can skip the Grafana half; it is no longer used)
> - [`combined_hpc.py`](combined_hpc.py) — the port-5000 app (frontend + API + collector)
>
> nginx starts fine without the backend, but any URL returns **502 Bad Gateway** until
> combined_hpc is up. The **frontend** works even if InfluxDB is down — `/system_metrics`
> serves an in-memory snapshot; InfluxDB only affects the historical write path.

---

## 1. Prerequisites

- **This box**: Ubuntu 20.04+ with nginx from the Ubuntu repo (1.18+). Hostname
  `andromeda`, LAN IP `192.168.0.18`.
- **A domain** pointing at your public IP. The current value is
  `nexon.aicraft.com.au`. If you use a different domain, substitute it everywhere it
  appears below.
- **Router port-forwarding**: forward external **TCP 80 and 443** to
  **`192.168.0.18`** (this box). Without this, clients on the internet and Let's
  Encrypt cannot reach nginx. (You do *not* need to forward 5000/8086 — those stay on
  localhost behind nginx, which is the security benefit.)
- **Ports 80 and 443 open** in any host firewall (`ufw allow 80,443/tcp`).

```bash
sudo apt update && sudo apt upgrade -y
```

---

## 2. DNS — point the domain at your public IP

nginx picks the site by `server_name`, and Let's Encrypt validates by reaching the
domain over the internet.

1. Find your public IP: `curl -s ifconfig.me ; echo` (currently `49.176.249.9`).
2. In your DNS provider create an **A record**: `nexon.aicraft.com.au → <public-IP>`.
3. Verify: `dig +short nexon.aicraft.com.au` must return your public IP.

**If DNS is wrong,** certbot (Step 6) fails — Let's Encrypt connects back over HTTP
to prove ownership, and it must land on this box (via the 80/443 forward).

---

## 3. Bring up the backend on this box (do this first)

The proxy is useless with nothing behind it. Confirm the app (and, optionally,
InfluxDB) is listening on localhost:

```bash
curl -I http://127.0.0.1:5000     # combined_hpc → expect 200 (serves the frontend at /)
curl -I http://127.0.0.1:8086     # InfluxDB → expect 404 + X-Influxdb-Version header (fine)
```

### 3a. Run combined_hpc.py (the port-5000 app)

Each app runs from its own virtualenv, so use the launcher rather than a bare
`python3` — see [§3c](#3c-virtualenvs-one-per-app) for how the venvs are built:

```bash
/home/sarthak/Remote_Systems_Interact/run_hpc.sh
```

It binds `0.0.0.0:5000`, writes metrics straight into InfluxDB, and serves the
frontend it renders itself:

- `/` → the combined dashboard (`index/hpc/combined_dashboard.html`)
- `/monitor`, `/system_monitor` → the live system-utilisation page
- `/sar_app` → the SAR-process page (upload + delete)
- `/system_metrics` → JSON snapshot the frontend polls for live metrics

For production, run it under systemd or `tmux`/`screen` so it survives logout. It also
shells out to `ethtool`/fan/diagnostic tools, so it is typically started with the
privileges those need.

### 3b. Create the upload folder (needed for the CPHD upload feature)

Uploads land in a `user_data/` subfolder of the CPHD store. That store is root-owned,
so create the subfolder once and give this app's user write access:

```bash
sudo mkdir -p /home/public/sar/sar-server/data/cphd/user_data
sudo chown "$(whoami)" /home/public/sar/sar-server/data/cphd/user_data
```

### 3c. Virtualenvs (one per app)

Each app has its own venv and its own pinned manifest. They are deliberately
separate: `combined_hpc` needs an imaging + numeric stack (Pillow, sarpy, numpy,
scipy, psutil), while `combined_topaz2` reads its metrics off a socket and needs
none of that — keeping them apart is what lets the Topaz ARM box install without
a compiler.

| App | Venv | Manifest | Launcher |
|---|---|---|---|
| `combined_hpc.py` (:5000) | `.venv-hpc` | `requirements-hpc.txt` | `run_hpc.sh` |
| `combined_topaz2.py` (:5001) | `.venv-topaz2` | `requirements-topaz2.txt` | `run_topaz2.sh` |

Build them with:

```bash
sudo apt install python3-pip python3.8-venv      # once per machine; see note below
cd /home/sarthak/Remote_Systems_Interact
python3 -m venv .venv-hpc
.venv-hpc/bin/pip install --upgrade pip setuptools wheel
.venv-hpc/bin/pip install -r requirements-hpc.txt
```

Same shape for `.venv-topaz2` with `requirements-topaz2.txt`.

> `python3.8-venv` is required because Debian/Ubuntu strip `ensurepip` out of the
> base `python3` package — without it, `python3 -m venv` fails with *"ensurepip
> is not available"*. The `pip` upgrade matters too: `ensurepip` seeds pip 20.0.2,
> which predates the modern dependency resolver.

The venvs are build output and are gitignored; the manifests and launchers are
committed. They are **not portable** — a venv bakes in absolute paths and the
host's architecture, so never copy `.venv-*` between machines. Rebuilding from
the manifest is the only supported move.

📖 **Full detail — prerequisites, external tools, dependency policy, lockfiles,
venv activation, the ARM/wheelhouse path, and troubleshooting — is in
[`installation.md`](installation.md).** That file is the authority; this section
is just enough to get the backend up before configuring nginx.

---

## 4. Install nginx

```bash
sudo apt install -y nginx
sudo systemctl status nginx
nginx -v
```

Ubuntu's package creates the layout this setup uses: `/etc/nginx/sites-available/`
(configs live here) and `/etc/nginx/sites-enabled/` (symlinks to the *active* ones;
`nginx.conf` includes `sites-enabled/*`). You write the file in `sites-available/`
and symlink it into `sites-enabled/` to turn it on.

---

## 5. Create the nginx site config (HTTP-only first)

Two stages, because the final config references SSL cert files that don't exist until
certbot runs. Start HTTP-only.

Because a single app now serves everything, the config is small: one catch-all
`location /` → `:5000`, plus one special-cased block for the large CPHD upload.

Create `/etc/nginx/sites-available/nexon-ssl.conf`:

```nginx
server {
    listen 80;
    server_name nexon.aicraft.com.au;

    # ---- large CPHD upload : must not be capped or buffered ----
    location = /upload_cphd {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        client_max_body_size 0;        # nginx default is 1 MB → without this, uploads 413
        proxy_request_buffering off;   # stream multi-GB bodies straight to the app
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # ---- everything else → combined_hpc.py (frontend + API + metrics) ----
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

Enable it, drop the default site, test, reload:

```bash
sudo ln -s /etc/nginx/sites-available/nexon-ssl.conf /etc/nginx/sites-enabled/nexon-ssl.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

### How the routing rules work

nginx matches `location` by **specificity**, not file order:

- `location = /upload_cphd` — an **exact** match, so it wins over the catch-all for
  that one URL. The body cap is removed and request buffering is off so a multi-GB
  CPHD streams straight through to the app instead of being buffered or rejected (413).
- `location /` (no `=`) — the **catch-all**: the frontend pages (`/`, `/monitor`,
  `/sar_app`), every API path (`/system_metrics`, `/storage_info`, `/send_message`,
  the `/images/…`, `/sar_log/…`, `/iperf3/…` families, …), and all static assets fall
  through here to combined_hpc on **:5000**.

Because there is a single backend, you **no longer need a per-route `location` for
each endpoint** — anything the app adds is automatically reached through the
catch-all. Only routes that need *different proxy behaviour* (like the upload's
un-buffered, uncapped body) need their own block. The proxied header notes:
- `Host $host` — the backend sees the real hostname.
- `X-Real-IP` / `X-Forwarded-For` — the client's real IP (else the backend sees `127.0.0.1`).
- `X-Forwarded-Proto $scheme` — tells the app the request came over https.
- `Upgrade`/`Connection` + `proxy_http_version 1.1` — WebSockets / live streaming, if
  the frontend uses them.

---

## 6. Obtain the SSL certificate

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d nexon.aicraft.com.au
```

certbot verifies domain ownership over port 80, writes the cert to
`/etc/letsencrypt/live/nexon.aicraft.com.au/`, edits the config to add `listen 443
ssl;`, and reloads nginx. When asked **"redirect HTTP to HTTPS?"**, choose **redirect
(option 2)**.

**If certbot fails:** DNS not pointing here, port 80 not forwarded/open, or another
service on port 80. Fix and re-run. Don't copy `/etc/letsencrypt/` from another
machine — just issue a fresh cert here.

> **"Invalid response … acme-challenge/… : 404" on the first run — then it works on
> re-run.** Expected with this config. The catch-all `location /` sends *everything*
> (including `/.well-known/acme-challenge/…`) to combined_hpc on `:5000`, which 404s the
> challenge file. certbot's nginx plugin injects its own temporary `location` for the
> challenge, so **simply re-running `certbot --nginx …` succeeds.** Renewal (Step 10)
> uses the same plugin mechanism, so no permanent change is needed. Only if you switch
> to `certbot certonly --webroot` would you need to add, above the catch-all:
> `location /.well-known/acme-challenge/ { root /var/www/html; }`.

---

## 7. Install the final SSL config (with the login gate)

After certbot succeeds, replace `/etc/nginx/sites-available/nexon-ssl.conf` with this
curated version. It is the same routing as Step 5, plus TLS, the HTTP→HTTPS redirect,
and the **Basic-Auth login** (Step 8 creates the password file):

```nginx
server {
    server_name nexon.aicraft.com.au;

    # ---- login gate: prompt for username/password before anything loads ----
    auth_basic "RSAT — login required";
    auth_basic_user_file /etc/nginx/.htpasswd;

    # ---- large CPHD upload : must not be capped or buffered ----
    location = /upload_cphd {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        client_max_body_size 0;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # ---- everything else → combined_hpc.py (frontend + API + metrics) ----
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    listen 443 ssl;
    ssl_certificate     /etc/letsencrypt/live/nexon.aicraft.com.au/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nexon.aicraft.com.au/privkey.pem;
}

server {
    listen 80;
    server_name nexon.aicraft.com.au;
    return 301 https://$host$request_uri;
}
```

```bash
sudo nginx -t
sudo systemctl reload nginx
```

- The **443 block** is the real site (HTTPS + login). All routing lives here.
- The **80 block** redirects plain HTTP to HTTPS.
- `auth_basic` sits at the server level, so the login prompt gates the frontend, the
  SAR app, **and** the upload/delete endpoints — which is also what stops random
  internet users from filling your disk with uploads.

> The Basic-Auth prompt does **not** interfere with `certbot renew`: renewal uses the
> port-80 HTTP-01 challenge, which the nginx plugin serves outside the auth'd 443 block.

---

## 8. Create the login credentials

```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd <username>     # prompts for a password
# add more users later WITHOUT -c (or you overwrite the file):
sudo htpasswd /etc/nginx/.htpasswd <another-user>
sudo systemctl reload nginx
```

Basic Auth is encrypted because it rides inside TLS. It's the browser's native
username/password popup — a single shared account list, no logout button, no branded
form. If you later want a styled login page, per-user accounts, or sign-out, put an
auth layer (e.g. Authelia or oauth2-proxy) in front instead; the routing above is
unchanged.

---

## 9. Verify

```bash
sudo nginx -t && sudo systemctl status nginx

# HTTP redirects to HTTPS (expect 301 → https://…)
curl -I http://nexon.aicraft.com.au

# HTTPS now demands login (expect 401 without creds, 200 with)
curl -Ik https://nexon.aicraft.com.au/
curl -Ik -u <username>:<password> https://nexon.aicraft.com.au/

# Frontend reachable through the proxy (expect 200)
curl -Ik -u <username>:<password> https://nexon.aicraft.com.au/

# SAR app reachable (expect 200; the page contains "Upload .cphd")
curl -sk -u <username>:<password> https://nexon.aicraft.com.au/sar_app | grep -o "Upload .cphd"

# Live metrics endpoint (expect JSON)
curl -sk -u <username>:<password> https://nexon.aicraft.com.au/system_metrics

# Free-space endpoint (expect JSON with "free")
curl -sk -u <username>:<password> https://nexon.aicraft.com.au/storage_info

# Certificate is valid and for the right domain
echo | openssl s_client -connect nexon.aicraft.com.au:443 -servername nexon.aicraft.com.au 2>/dev/null | openssl x509 -noout -subject -dates
```

Then open `https://nexon.aicraft.com.au/` in a browser: after the login prompt you
should see the combined dashboard with a valid padlock, and the SAR panel (with the
**Upload .cphd** button) reachable at `/sar_app`.

**Reading errors:**
- **401** everywhere → that's the login gate working; supply credentials.
- **502 Bad Gateway** → the backend is down. Start it with `./run_hpc.sh` (`:5000`).
- **413 on upload** → the `/upload_cphd` block (or its `client_max_body_size 0`) is missing.
- **certbot 404 on the acme-challenge** (during Step 6) → normal on the first run; just
  re-run `certbot --nginx …` (see the note in Step 6).
- Logs: `sudo tail -f /var/log/nginx/error.log`.

---

## 10. Certificate auto-renewal

Let's Encrypt certs expire every 90 days; the apt certbot install adds a systemd timer
that renews automatically.

```bash
systemctl list-timers | grep certbot     # timer should be scheduled
sudo certbot renew --dry-run              # must succeed
```

---

## 11. Bring-up / rebuild checklist

1. ☐ `apt update && apt upgrade`
2. ☐ DNS `nexon.aicraft.com.au` → your public IP (`dig` to confirm)
3. ☐ Router: forward TCP **80** and **443** → **192.168.0.18**; open host firewall
4. ☐ Install & start **InfluxDB** ([`native_install_grafana_influxdb.md`](native_install_grafana_influxdb.md) — Grafana half optional/unused)
5. ☐ Build `.venv-hpc` from `requirements-hpc.txt` (Step 3c); start it with **`./run_hpc.sh`** (`:5000`); create `user_data/` with correct ownership (Step 3b)
6. ☐ `apt install nginx`
7. ☐ Write HTTP-only `nexon-ssl.conf`, symlink into `sites-enabled/`, remove `default`
8. ☐ `nginx -t && systemctl reload nginx`
9. ☐ `certbot --nginx -d nexon.aicraft.com.au` (choose redirect)
10. ☐ Install the final SSL config (Step 7); create `.htpasswd` (Step 8); `nginx -t && reload`
11. ☐ Verify (Step 9) and confirm renewal (Step 10)

---

## Appendix A — HTTP routes (all reached via the catch-all `/`)

Everything below is served by [`combined_hpc.py`](combined_hpc.py) on `:5000` and
reaches the browser through the single `location /` proxy. Only `/upload_cphd` needs
its own nginx block (big body, no buffering); the rest need nothing special.

| Route | Method | Purpose | nginx match |
|-------|--------|---------|-------------|
| `/` | GET | Serve the combined dashboard (the frontend) | catch-all `/` |
| `/monitor`, `/system_monitor` | GET | Live system-utilisation page | catch-all `/` |
| `/sar_app` | GET | SAR-process frontend (upload + delete) | catch-all `/` |
| `/system_metrics` | GET | JSON metrics snapshot the frontend polls | catch-all `/` |
| `/storage_info` | GET | Free disk space for the upload UI | catch-all `/` |
| `/upload_cphd` | POST | Stream-upload a `.cphd` into `user_data/` | `= /upload_cphd` (big body, no buffering) |
| `/delete_upload` | POST | Delete one uploaded `.cphd` | catch-all `/` |
| `/send_message` | POST | Control commands (RUN/SIZE/STOPSAR/scan/…) | catch-all `/` |
| `/get_cphd_files` | GET | CPHD picker list | catch-all `/` |
| `/get_cphd_file_properties` | GET | Selected CPHD metadata | catch-all `/` |
| `/get_tif_file_properties` | GET | Processed-output properties | catch-all `/` |
| `/images/<file>` | GET | Processed SAR image (webp) | catch-all `/` |
| `/sar_log/image/<type>` | GET | Profiler log plots | catch-all `/` |
| `/sar_log/status` | GET | Log version (analysis refresh) | catch-all `/` |
| `/sar_colored_image` | GET | Colorized SAR image | catch-all `/` |
| `/iperf3/lw_eth_adt_results` | GET | Network-test results | catch-all `/` |
| `/iperf3/up_eth_adt_results` | GET | Network-test results | catch-all `/` |

## Appendix B — Key facts (this machine)

| Item | Value |
|------|-------|
| Hostname | `andromeda` |
| LAN IP | `192.168.0.18` (also `10.42.0.101` on a second interface) |
| Public IP | `49.176.249.9` (home router) |
| Domain | `nexon.aicraft.com.au` |
| Router forwards | TCP 80, 443 → `192.168.0.18` |
| nginx | `1.18+`, active site `sites-available/nexon-ssl.conf` (symlinked in `sites-enabled/`) |
| Cert | `/etc/letsencrypt/live/nexon.aicraft.com.au/` |
| combined_hpc.py | `0.0.0.0:5000` (frontend + API + collector) |
| InfluxDB | `127.0.0.1:8086` (collector write target; no dashboard reads it) |
| CPHD store | `/home/public/sar/sar-server/data/cphd/` (uploads in `user_data/`) |
| Login | nginx Basic Auth, `/etc/nginx/.htpasswd` |

## Appendix C — Reference: global `nginx.conf`

The stock Ubuntu `nginx.conf` is used unmodified; the only lines that matter are the
defaults `user www-data;`, `include /etc/nginx/sites-enabled/*;`, and `gzip on;`. You
should not need to edit it. Optional hardening: set `ssl_protocols TLSv1.2 TLSv1.3;`
and uncomment `server_tokens off;` in the `http {}` block.

---

# Future Features

> This section is **not part of the current live setup**. It documents planned
> enhancements to the proxy. The setup above (Steps 0–11) is what runs today; nothing
> below is applied yet. Each subsection is self-contained and can be adopted
> independently.

## FF-1. Per-user landing pages with a hard admin / rsat boundary

**Goal.** Two accounts behind the same login, each locked to its own area:

- `admin` logs in → lands on the admin dashboard at `/`, and **cannot** open `/rsat`.
- `rsat` logs in → lands on the RSAT dashboard at `/rsat`, and **cannot** open `/`.

The wrong user is bounced to their own home page, so neither ever sees the other's
interface.

### Why not two separate `.htpasswd` files (one per location)?

The obvious approach — a `.htpasswd_admin` gate on `/` and a separate `.htpasswd_rsat`
gate on `/rsat` — **breaks the RSAT dashboard**, because of how browsers scope
Basic-Auth credentials:

- Two `auth_basic` files create two separate **login realms**.
- A browser only auto-sends a realm's credentials to paths **inside** the area it
  logged into.
- The RSAT page loads from `/rsat`, but its JavaScript fetches `/system_metrics`,
  `/storage_info`, `/images/…`, `/send_message`, … — all at paths **outside** `/rsat`.
- So the browser won't send the rsat credentials to those endpoints → each data XHR
  returns **401** → the dashboard's live data silently fails to load.

**The fix:** keep **one** login file containing **both** users (so credentials are
cached for the whole origin and every data call carries them), then enforce the hard
per-page boundary by **username** via nginx's `$remote_user`. Same result, no broken
XHRs.

### FF-1a. nginx config

Replace the `location` blocks in the **443** server of
`/etc/nginx/sites-available/nexon-ssl.conf` with:

```nginx
server {
    server_name nexon.aicraft.com.au;

    # ---- ONE login gate; the file holds BOTH admin and rsat ----
    auth_basic "RSAT — login required";
    auth_basic_user_file /etc/nginx/.htpasswd;

    # ================= ADMIN-ONLY pages =================
    location = / {
        if ($remote_user != "admin") { return 302 /rsat; }   # bounce rsat to its own home
        proxy_pass http://127.0.0.1:5000;
        include /etc/nginx/rsat_proxy.conf;
    }
    location = /sar_app {
        if ($remote_user != "admin") { return 302 /rsat; }
        proxy_pass http://127.0.0.1:5000;
        include /etc/nginx/rsat_proxy.conf;
    }

    # ================= RSAT-ONLY pages =================
    location = /rsat {
        if ($remote_user != "rsat") { return 302 /; }         # bounce admin to its own home
        proxy_pass http://127.0.0.1:5000;
        include /etc/nginx/rsat_proxy.conf;
    }
    location = /sar_app_rsat {
        if ($remote_user != "rsat") { return 302 /; }
        proxy_pass http://127.0.0.1:5000;
        include /etc/nginx/rsat_proxy.conf;
    }

    # ================= shared big upload =================
    location = /upload_cphd {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        client_max_body_size 0;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # ========= everything else: shared API/data/assets (both users) =========
    location / {
        proxy_pass http://127.0.0.1:5000;
        include /etc/nginx/rsat_proxy.conf;
    }

    listen 443 ssl;
    ssl_certificate     /etc/letsencrypt/live/nexon.aicraft.com.au/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nexon.aicraft.com.au/privkey.pem;
}

server {
    listen 80;
    server_name nexon.aicraft.com.au;
    return 301 https://$host$request_uri;
}
```

> `if (…) { return …; }` is the one form of `if` that is officially safe inside a
> `location` — it short-circuits, and when the condition is false nginx falls through
> to `proxy_pass`. The **302 to the user's own home** (instead of a bare 403) means an
> admin who types `/rsat` just lands back on `/`, and vice-versa. Swap
> `return 302 …;` for `return 403;` to hard-deny instead.

### FF-1b. Shared proxy-header snippet (write once)

Create `/etc/nginx/rsat_proxy.conf` so the header block isn't repeated in every
location:

```nginx
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

### FF-1c. Create the single login file (both users)

```bash
sudo htpasswd -c /etc/nginx/.htpasswd admin     # prompts for admin's password
sudo htpasswd    /etc/nginx/.htpasswd rsat       # note: NO -c, or you wipe the file
sudo nginx -t && sudo systemctl reload nginx
```

### FF-1d. Verify the boundary

```bash
# admin lands on the admin dashboard
curl -Ik -u admin:PW  https://nexon.aicraft.com.au/            # 200

# rsat asking for / is bounced to /rsat
curl -Ik -u rsat:PW   https://nexon.aicraft.com.au/            # 302 → /rsat

# rsat lands on its own dashboard
curl -Ik -u rsat:PW   https://nexon.aicraft.com.au/rsat        # 200

# admin asking for /rsat is bounced to /
curl -Ik -u admin:PW  https://nexon.aicraft.com.au/rsat        # 302 → /

# shared data works for BOTH (required for the pages to function)
curl -sk -u rsat:PW   https://nexon.aicraft.com.au/system_metrics   # JSON
curl -sk -u admin:PW  https://nexon.aicraft.com.au/system_metrics   # JSON
```

### FF-1e. Which routes are gated where

| Bucket | Routes | Who |
|--------|--------|-----|
| Admin-only | `/`, `/sar_app` | `admin` |
| RSAT-only | `/rsat`, `/sar_app_rsat` | `rsat` |
| Shared (both) | `/monitor`, `/system_metrics`, `/storage_info`, `/upload_cphd`, `/images/…`, `/send_message`, `/branding/…`, all other APIs/assets | both |

To gate another page (e.g. make `/monitor` admin-only), add a
`location = /monitor { if ($remote_user != "admin") { return 302 /rsat; } … }` block
like the others. **Keep the shared data endpoints open to both** — that's what makes
each dashboard actually load.

### FF-1f. Limitation — page-level, not data-level

The wall is on the **pages/URLs**, not the underlying data: `rsat` can still
`curl /system_metrics` and see the same metrics `admin` does, because both dashboards
need that endpoint. If the **data itself** must be partitioned per user, that has to
happen inside [`combined_hpc.py`](combined_hpc.py) — add
`proxy_set_header X-Remote-User $remote_user;` to `rsat_proxy.conf` and have the app
authorize on that header.
