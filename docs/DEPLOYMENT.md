# Deployment on the board

> CI reproduces ARM64 Ubuntu 20.04 (see [COMPATIBILITY.md](COMPATIBILITY.md)).
> Validate the live desktop and record controlled acceptance separately on every installation.

Target: LubanCat RK3588, ARM64, Ubuntu 20.04.6, glibc 2.31, XFCE on Xvnc (measured as `:10`,
1280x720, depth 16), Chrome showing the Xianyu chat page. The system Python 3.8 is **not** used;
Python 3.10 comes from Conda, which installs into its own prefix and leaves system glibc and Python alone.

## 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y scrot xdotool xclip libgl1 libglib2.0-0
# optional alternative OCR backend:
sudo apt-get install -y tesseract-ocr tesseract-ocr-chi-sim
```

`scrot` and `xdotool` were already present on the reference board; `xclip` is needed for the
default clipboard-based reply typing (CJK text). `libgl1`/`libglib2.0-0` are required by the
`opencv-python` wheel used by RapidOCR.

## 2. Python 3.10 via Conda

Reuse an existing Conda installation when present. Keep the runtime under `/opt` so the
`xvm` web service can access it with `ProtectHome=true`. For example, clone an existing
Python 3.10 environment with `conda create --clone <existing-py310> -p /opt/xianyu-monitor/env`.
Do not use a venv whose interpreter or standard library lives under `/root`.
Only install Conda if no suitable installation exists:

```bash
curl -fsSLO https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
sudo bash Miniforge3-Linux-aarch64.sh -b -p /opt/conda
sudo mkdir -p /opt/xianyu-monitor
sudo /opt/conda/bin/conda create -y -p /opt/xianyu-monitor/env python=3.10
```

## 3. Application

```bash
sudo git clone https://github.com/<owner>/xianyu-visual-monitor.git /opt/xianyu-monitor/src
cd /opt/xianyu-monitor/src
sudo /opt/xianyu-monitor/env/bin/pip install -r requirements/ocr.txt
sudo /opt/xianyu-monitor/env/bin/pip install --no-deps .
```

For exactly the package set that CI tested on ARM64 Ubuntu 20.04, add the lock file:
`pip install -c requirements/constraints-focal-arm64-py310.txt -r requirements/ocr.txt`.

## 4. User, directories and private configuration

```bash
sudo useradd --system --home /var/lib/xianyu-monitor --shell /usr/sbin/nologin xvm
sudo install -d -o root -g xvm -m 2770 /var/lib/xianyu-monitor      # setgid: files stay group xvm
sudo install -d -o root -g xvm -m 0750 /etc/xianyu-monitor
sudo install -o root -g xvm -m 0640 deploy/monitor.env.example /etc/xianyu-monitor/monitor.env
sudoedit /etc/xianyu-monitor/monitor.env
```

Fill in the database password, `XVM_SECRET_KEY`, the SMTP account and **`XVM_NOTIFY_TO`** (the
private recipient address). This file is the only place secrets live; it is not in the repository.

## 5. MySQL

Reuse the existing MySQL 8 instance managed by 1Panel on local port 3306. Do not
install another server, recreate the container, or remove existing volumes. Docker and
Compose need no reinstall. Use a dedicated database and account with a random password
that differs from SSH and MySQL administrator passwords.

Copy `deploy/mysql-init.sql` to a private file outside this checkout, replace its password
placeholder there, and run it through the existing container. Container TCP connections
arrive from the bridge gateway, so the application's account uses host `%`; grants remain
limited to `xianyu_monitor.*`. Keep port 3306 restricted to trusted hosts.

```bash
install -m 0600 deploy/mysql-init.sql /root/xvm-init.sql
sudoedit /root/xvm-init.sql
# Use the actual existing 1Panel container name. Password is prompted, never passed via -p.
docker cp /root/xvm-init.sql <existing-mysql-container>:/tmp/xvm-init.sql
docker exec -it <existing-mysql-container> mysql -uroot -p
# At the mysql prompt: SOURCE /tmp/xvm-init.sql;
# Remove both temporary SQL files after successful initialization.
cd /opt/xianyu-monitor/src
sudo -u xvm env XVM_ENV_FILE=/etc/xianyu-monitor/monitor.env /opt/xianyu-monitor/env/bin/xianyu-monitor migrate
sudo -u xvm env XVM_ENV_FILE=/etc/xianyu-monitor/monitor.env /opt/xianyu-monitor/env/bin/xianyu-monitor create-admin --username owner
```

## 6. X display access for the worker

The existing desktop belongs to `root` (Xvnc started by xrdp/sesman with `-auth .Xauthority`
and `-nolisten tcp`). The worker must be able to read that X authority cookie and reach the
local X socket. The **web UI never needs X access** and always runs as `xvm`.

* **Option A (default unit):** run the worker as `root` (`User=root`, `Group=xvm`). Discovery
  reads the `Xvnc` command line from `/proc` and resolves `-auth` relative to the Xvnc process
  working directory. Files it writes are group-readable by `xvm`.
* **Option B (least privilege):** run the worker as `xvm` and grant access from *inside* the
  desktop session after every session start:
  ```bash
  # in a terminal inside the Xvnc desktop (as root):
  xhost +SI:localuser:xvm
  ```
  Then set `User=xvm` in the worker unit and `XVM_CAPTURE_DISPLAY=:10` (or keep `auto`).
  Alternatively, merge the cookie into a file readable by `xvm`
  (`xauth -f /root/.Xauthority extract - :10 | sudo -u xvm xauth -f /var/lib/xianyu-monitor/.Xauthority merge -`)
  and set `XVM_CAPTURE_XAUTHORITY`. The cookie changes whenever Xvnc restarts.

`PrivateTmp` stays disabled for the worker because the X socket is in `/tmp/.X11-unix`.
If several X servers run, set `XVM_CAPTURE_DISPLAY` explicitly; the worker refuses to guess.

Check access:

```bash
sudo env XVM_ENV_FILE=/etc/xianyu-monitor/monitor.env /opt/xianyu-monitor/env/bin/xianyu-monitor check-config
sudo env XVM_ENV_FILE=/etc/xianyu-monitor/monitor.env /opt/xianyu-monitor/env/bin/xianyu-monitor capture-once --out /var/lib/xianyu-monitor/probe.png
```

## 7. Calibration

Follow [CONFIGURATION.md § Calibration](CONFIGURATION.md#calibration). In short: load the example
profile, compare the overlay with your frame, adjust, then capture anchors from a normal frame.

```bash
sudo -u xvm env XVM_ENV_FILE=/etc/xianyu-monitor/monitor.env /opt/xianyu-monitor/env/bin/xianyu-monitor \
  load-calibration /opt/xianyu-monitor/src/src/xianyu_monitor/examples/calibration.example.json
```

## 8. systemd

```bash
sudo cp deploy/systemd/xianyu-monitor-web.service deploy/systemd/xianyu-monitor-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xianyu-monitor-web
sudo systemctl enable --now xianyu-monitor-worker
journalctl -u xianyu-monitor-worker -f
```

The web UI listens on `127.0.0.1:8080`. From your computer: `ssh -L 8080:127.0.0.1:8080 <board>`
then open `http://127.0.0.1:8080`. If you bind it to the LAN, keep it off the public Internet
and consider a TLS reverse proxy (then set `XVM_SESSION_COOKIE_SECURE=true`).

## 9. First run checklist

1. Overview shows the worker as *reporting* and status `ok` (not `not_calibrated` or
   `layout_mismatch`).
2. Stay in **baseline** mode while the visible list fills the customer directory. Import other
   known customer names on the Customers page (one per line). The visible first screen is
   not a complete history.
3. Resolve review items.
4. Configure SMTP, then use *Test connection* and *Send test email* on the Notifications page.
5. Switch to **active** mode and enable new-customer emails (type `ENABLE`). Dry-run stays on
   until you disable it, and while it is on, emails are recorded but not sent.
6. Keyword replies: add rules (they start disabled), test them, set reply mode to *Review only*,
   approve a few replies manually, and only then consider *Automatic* plus *Reply sending*.

## 10. Upgrades, backup and removal

```bash
cd /opt/xianyu-monitor/src && sudo git pull
sudo /opt/xianyu-monitor/env/bin/pip install -r requirements/ocr.txt && sudo /opt/xianyu-monitor/env/bin/pip install --no-deps .
sudo -u xvm env XVM_ENV_FILE=/etc/xianyu-monitor/monitor.env /opt/xianyu-monitor/env/bin/xianyu-monitor migrate
sudo systemctl restart xianyu-monitor-worker xianyu-monitor-web
```

Back up with `mysqldump --single-transaction xianyu_monitor`. To remove, disable both units and
delete `/opt/xianyu-monitor`, `/var/lib/xianyu-monitor`, `/etc/xianyu-monitor` and the database.
