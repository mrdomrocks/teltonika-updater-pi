# Rutkeeper

Rutkeeper is a small appliance for a Raspberry Pi. It checks Teltonika routers for a stable firmware package, stores device backups on the Pi, and can push a known-good config or install that firmware. Unattended installs stay off until you arm them.

The dashboard listens on port **8741**.

## What the search is actually describing

Searching for “automate Teltonika firmware and config update” leads to Teltonika’s own March 2026 community thread, [Automated firmware updates on RMS](https://community.teltonika.lt/t/automated-firmware-updates-on-rms/18332), plus the wiki pages for [firmware updates in RMS](https://wiki.teltonika-networks.com/view/How_to_upgrade_a_router%27s_firmware_version_via_RMS%3F) and [configuration backups](https://wiki.teltonika-networks.com/view/Configuration_Backup_RutOS).

Someone wanted a RUTM10 to look for a new **stable** firmware on the first Monday of the month at 04:00, take a backup, then install it. Teltonika support said they do not recommend automatic updates, and described this workaround:

1. In RMS Task Manager, build a task group that runs on the router.
2. Check `GET /firmware/device/updates/status`. If `stable_version` is `newest`, stop.
3. Otherwise take a backup (`sysupgrade -b /tmp/pre_update_backup.tar.gz`) and upload it to RMS.
4. `POST /firmware/actions/fota_download` with `{"data": {"type": "stable"}}`.
5. `POST /firmware/actions/upgrade` with `{"data": {"suppress_validation": 1, "keep_settings": 1}}`.
6. Put a script on **each router** that calls `POST https://api.rms.teltonika-networks.com/devices/tasks/groups/{id}` with a bearer token, and run it from crontab at 04:00 on the 1st.

Config rollout is separate. A backup applies only to routers with the same product code, and only when their firmware is the same or newer than the firmware the backup was taken on. RMS can push one backup to many devices. On RutOS 7.22 and later there are two backup types; Teltonika R&D has recommended the older **device backup** while the newer configuration backup is still incomplete.

Rutkeeper keeps that sequence on the Pi instead of a crontab and an API token on every router.

## What it does

- Talks to RutOS 7.06 or newer on the LAN: `POST /api/login`, then the firmware and backup endpoints above.
- Saves backups on the Pi, with product code, firmware version, and a SHA-256.
- Pushes a backup only when the product code matches and the file is not from a newer firmware.
- Installs stable firmware only. A manual install asks you to confirm and takes a backup first. The monthly job installs only when **both** the site is armed and that router has auto-apply on.
- Default window: first Monday at 04:00, on the Pi’s clock. If the Pi is off, that window is skipped.
- Optional RMS path: the same task-group call support published, fired from the Pi. Use it for routers you cannot reach on the LAN. Do not schedule the same router both ways.

Three simulated routers are loaded on first start so the screens are visible before any hardware is added. Remove them from the fleet page when you add real ones.

## Run it here

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m rutkeeper
```

Open http://127.0.0.1:8741

Set `RUTKEEPER_HOST`, `RUTKEEPER_PORT`, and `RUTKEEPER_DATA` to change the bind address, port, and data directory. Passwords and the RMS token are encrypted with a key created in that directory.

## Install on a Raspberry Pi

Raspberry Pi OS Bookworm or newer, on the same LAN as the routers.

```bash
sudo ./install/install.sh
```

That installs to `/opt/rutkeeper`, keeps data in `/var/lib/rutkeeper`, and starts a systemd service bound to `0.0.0.0:8741`. Then open `http://<pi-address>:8741` from a machine on that LAN.

A user install, without systemd, goes into `~/.local/rutkeeper`:

```bash
./install/install.sh --user
```

Do not publish port 8741 to the internet. The Pi stores router admin passwords. Limit the port to your management LAN.

To update a system install, pull the new files and run `sudo ./install/install.sh` again. The data directory is left in place.

Stop it with `sudo systemctl stop rutkeeper`. Logs: `journalctl -u rutkeeper -f`.

## Add a router

1. On the router, use an admin account. RutOS 7.06 or newer.
2. In Rutkeeper, add the LAN address (`192.168.1.1` is enough). Leave TLS verification off unless you installed a real certificate on the router.
3. Check firmware. A waiting stable package shows as “Update waiting”.
4. Save a device backup before you change anything.
5. Install from the router page only when you want the reboot. Keep-settings is on by default, which sends the upgrade body support published.

To roll a config out, mark a backup as a profile (or upload one and record its product code and firmware). Push it from the router page or the Backups page. The router reboots, and the admin password inside the file becomes the router password.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

## Limits

- Firmware and backup responses vary across RutOS builds. A failed call is stored on the router page, including the router’s error text, rather than treated as success.
- The stable package is downloaded by the router from Teltonika, not served by the Pi. The router needs its own route to the internet for that step.
- Rutkeeper will not catch up a window it missed while it was stopped.
- This does not replace RMS if the routers are only reachable through the cloud. Use the RMS page for that, with a token from RMS → Account settings → API.
