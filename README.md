# TUA

TUA is a workbench for a Raspberry Pi. Plug in one Teltonika RUT, and the Pi pings it until the router answers on the LAN. The firmware image and two config files stay on the Pi. In the browser, type the password on that router and pick Config 1 or Config 2. When the router answers, TUA writes the firmware, waits for the router, applies the config, then sets the admin password to a single password you chose.

The dashboard listens on port **8741**.

## Workbench

1. Open the Workbench page and set the router address. A RUT on the cable is usually `192.168.1.1`. Give the Pi an address on that subnet that is not `.1`.
2. Store the firmware file, Config 1, and Config 2.
3. Set the password every router should end up with.
4. If a config file contains its own admin password, type that password next to the file. TUA needs it to sign back in after the file is applied, then it overwrites it.
5. Type the password that is on **this** router right now, or press **Scan the label** on a phone to read the QR code live. That value is not saved. Pick Config 1 or Config 2.
6. Plug the router in. The page pings every couple of seconds. When the router answers on the LAN, the status turns ready, the tab beeps, a browser alert fires if you allowed alerts, and Install starts. If the router is already answering, press Install.
7. The page shows each step: sign in, write the firmware (`/firmware/actions/upload_device_firmware`, settings kept so the same login still works), wait for the reboot, apply the config, wait again, then change the admin password to the shared one. The password that was on the router, and the one inside the config, are not written to disk. None of this starts while the router is silent on the LAN.

TUA cannot sign in with no password at all. The password already on the unit is required for that one login. It is held in memory for the job and then discarded.

The log page sits beside the workbench. Firmware, config, and password changes run only when the router is plugged in and answers on the LAN.

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

TUA keeps that sequence on the Pi. It does not call RMS and it does not store an access token. The same jobs run from the workbench when the router is plugged in and answers on the LAN.

## What it does

- Talks to RutOS 7.06 or newer on the LAN: `POST /api/login`, then the firmware and backup endpoints above.
- Saves backups on the Pi, with product code, firmware version, and a SHA-256.
- Installs the firmware file stored on the Pi when the router answers on the LAN. If the password and a config are already filled in, Install starts on its own. It also starts when you press Install while the router is already answering.

## Run it here

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m rutkeeper
```

Open http://127.0.0.1:8741

Set `RUTKEEPER_HOST`, `RUTKEEPER_PORT`, and `RUTKEEPER_DATA` to change the bind address, port, and data directory. Passwords are encrypted with a key created in that directory. Binding away from localhost turns on HTTPS automatically so phones can use the live label scanner; set `RUTKEEPER_TLS=0` to force plain HTTP.

## Install on a Raspberry Pi

Raspberry Pi OS Bookworm or newer, on the same LAN as the routers.

```bash
sudo ./install/install.sh
```

That installs to `/opt/rutkeeper`, keeps data in `/var/lib/rutkeeper`, and starts a systemd service bound to `0.0.0.0:8741` with HTTPS. Open `https://<pi-address>:8741` from a machine on that LAN and accept the self-signed certificate once. Phones need that HTTPS page before **Scan the label** can open the camera and fill the password from the QR code.

A user install, without systemd, goes into `~/.local/rutkeeper`:

```bash
./install/install.sh --user
```

Do not publish port 8741 to the internet. The Pi stores router admin passwords. Limit the port to your management LAN.

To update a system install from Cursor, not from GitHub:

```bash
sudo apt-get install -y git ca-certificates
sudo git clone --origin cursor --branch cursor/remove-rms-lan-actions-3854 \
  https://origin.cursor.com/git/mrdomrocks/teltonika-updater-pi.git \
  /opt/rutkeeper-src
sudo /opt/rutkeeper-src/install/update.sh cursor/remove-rms-lan-actions-3854
```

That fetches the branch, installs it to `/opt/rutkeeper`, and restarts the service. Router passwords and the firmware and config folders are left in place. The next update reuses that branch:

```bash
sudo /opt/rutkeeper-src/install/update.sh
```

Pass `main` when that branch should be what the Pi runs. `RUTKEEPER_GIT_REMOTE` overrides the Cursor URL.

Stop it with `sudo systemctl stop rutkeeper`. Logs: `journalctl -u rutkeeper -f`.

## On the router

Use an admin account on RutOS 7.06 or newer. On the workbench, set the address (`192.168.1.1` is enough) and type the password that is on that router. Install runs from the workbench when you want the reboot.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

## Limits

- Firmware and backup responses vary across RutOS builds. A failed call is shown on the workbench, including the router’s error text, rather than treated as success.
- The firmware file used by Install is the one stored on the Pi.
- TUA will not catch up a window it missed while it was stopped.
- Routers that are only reachable through the cloud are out of scope. TUA acts when the router is plugged in and answers on the LAN.
