"""Workbench for one RUT plugged into the Pi.

One install signs in, waits until the router answers, sets the admin password,
writes the local firmware on the cable address, applies Config 1 or Config 2
on that same address, then waits for the router's new address. Answering on
the new address confirms the config was uploaded.

Factory units always answer on the cable address (192.168.1.1) until the
config file is uploaded. After that they move to 192.168.100.1 — that answer
is the confirmation the config landed.

The password that was already on the router is held in memory for that job
only. It is not written to the database. The admin password set here is fixed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from rutkeeper.crypto import SecretBox
from rutkeeper.db import DB
from rutkeeper.rms import (
    add_rut,
    format_mac,
    name_for,
    prefix_for,
    read_access_token,
    remember_device,
)
from rutkeeper.rutos import RutosClient, RutosError, api_base

FOLDERS = {
    "firmware": "Firmware",
    "config1": "Config 1",
    "config2": "Config 2",
}
FIXED_ADMIN_PASSWORD = "Enter@qua1!"
# Factory LAN before any config file; stays here through firmware and config upload.
CABLE_HOST = "192.168.1.1"
# LAN after the config file is applied — answer here confirms the upload.
POST_CONFIG_HOST = "192.168.100.1"
# A factory reset comes back on the cable address and stays there.
CABLE_AFTER_CONFIG_SECONDS = 60
_HOLD_PHASES = {"busy", "rebooting", "await-address"}
_WAIT_PHASES = {"rebooting", "await-address"}


class Busy(Exception):
    pass


class SimRouter:
    """Stand-in so the bench buttons can be tried with no hardware."""

    def __init__(self) -> None:
        self.admin_password = ""
        self.firmware = ""
        self.config = ""
        self.password_changes: list[str] = []
        self.actions: list[tuple[str, str]] = []
        self.wireless_name = ""
        self.wireless_key = ""
        self.rms: list[dict[str, str]] = []

    def accept(self, passwords: list[str]) -> str:
        for password in _unique(passwords):
            if not self.admin_password or password == self.admin_password:
                if not self.admin_password:
                    self.admin_password = password
                return password
        raise RutosError("Login failed.")

    def upload_firmware(self, filename: str, content: bytes) -> None:
        if not content:
            raise RutosError("Firmware file is empty.")
        self.actions.append(("firmware", self.admin_password))
        self.firmware = filename

    def upgrade_staged(self) -> None:
        if not self.firmware:
            raise RutosError("No firmware has been uploaded.")

    def upload_backup(self, filename: str, content: bytes) -> None:
        if not content:
            raise RutosError("Config file is empty.")
        self.config = filename

    def apply_backup(self) -> None:
        if not self.config:
            raise RutosError("No config has been uploaded.")
        self.actions.append(("config", self.admin_password))

    def replace_password(self, password: str) -> None:
        """What a config file does when it carries its own admin password."""
        if password:
            self.admin_password = password

    def set_admin_password(self, current: str, new: str) -> str:
        if current != self.admin_password:
            raise RutosError("Wrong current password.")
        self.admin_password = new
        self.password_changes.append(new)
        self.actions.append(("password", new))
        return "simulated"

    def set_wireless_name(self, ssid: str, key: str) -> int:
        self.wireless_name = ssid
        self.wireless_key = key
        self.actions.append(("wireless", ssid))
        return 1

    def note_rms(self, name: str, serial: str, mac: str) -> None:
        self.rms.append({"name": name, "serial": serial, "mac": mac})
        self.actions.append(("rms", name))

    def close(self) -> None:
        return None


class LiveRouter:
    def __init__(self, client: RutosClient):
        self.client = client

    def upload_firmware(self, filename: str, content: bytes) -> None:
        self.client.upload_firmware(filename, content)

    def upgrade_staged(self) -> None:
        self.client.upgrade_staged()

    def upload_backup(self, filename: str, content: bytes) -> None:
        self.client.upload_backup(filename, content)

    def apply_backup(self) -> None:
        self.client.apply_backup()

    def set_admin_password(self, current: str, new: str) -> str:
        return self.client.set_admin_password(current, new)

    def set_wireless_name(self, ssid: str, key: str) -> int:
        return self.client.set_wireless_ssids(ssid, key)

    @property
    def password_change_required(self) -> bool:
        return bool(getattr(self.client, "password_change_required", False))

    def close(self) -> None:
        self.client.close()


class Bench:
    def __init__(self, db: DB, secrets: SecretBox, directory: Path):
        self.db = db
        self.secrets = secrets
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sim = SimRouter()
        self._ensure_folders()
        self._session_password = ""
        # Label password for this job only. A factory reset puts it back, so
        # reconnects after a reboot still try it. It is not written to disk.
        self._factory_password = ""
        self._label_serial = ""
        self._label_mac = ""
        self._lock = threading.Lock()
        self._inflight = False
        self._job_id = 0
        self._job_thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._was_http = False
        self.status: dict[str, object] = {
            "ping": False,
            "http": False,
            "rtt_ms": None,
            "phase": "offline",
            "job": "",
            "detail": "Plug the router into this Pi.",
            "error": "",
            "ready_tick": 0,
            "step": "",
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="rutkeeper-bench", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, object]:
        settings = self.db.settings()
        with self._lock:
            status = dict(self.status)
        status.update(
            {
                "host": settings.get("bench_host") or CABLE_HOST,
                "next_host": settings.get("bench_next_host") or POST_CONFIG_HOST,
                "username": settings.get("bench_username") or "admin",
                "simulate": settings.get("bench_simulate") == "1",
                "has_target_password": True,
                "files_root": str(self.directory),
                "firmware_name": self._name("firmware"),
                "config1_name": self._name("config1"),
                "config2_name": self._name("config2"),
                "config1_has_password": bool(settings.get("bench_config1_password_enc")),
                "config2_has_password": bool(settings.get("bench_config2_password_enc")),
                "firmware_ready": self.chosen("firmware") is not None,
                "config1_ready": self.chosen("config1") is not None,
                "config2_ready": self.chosen("config2") is not None,
            }
        )
        return status

    def save_setup(self, host: str, username: str, next_host: str, simulate: bool) -> None:
        values = {
            "bench_host": _host_only(host) or CABLE_HOST,
            # Config upload moves the router here; default matches the usual post-config LAN.
            "bench_next_host": _optional_host(next_host) or POST_CONFIG_HOST,
            "bench_username": (username or "admin").strip()[:80] or "admin",
            "bench_simulate": "1" if simulate else "0",
        }
        self.db.set_settings(values)
        with self._lock:
            inflight = self._inflight
        if inflight:
            return
        if simulate:
            self._mark(ping=True, http=True, rtt_ms=1, detail="Simulated router is ready.")
        else:
            self.sim = SimRouter()

    def save_config_password(self, slot: str, password: str) -> None:
        if slot not in {"config1", "config2"}:
            raise RutosError("Choose config 1 or config 2.")
        key = "bench_config1_password_enc" if slot == "config1" else "bench_config2_password_enc"
        self.db.set_settings({key: self.secrets.encrypt(password) if password else ""})

    def start_job(
        self,
        action: str,
        typed_password: str,
        slot: str = "",
        serial: str = "",
        mac: str = "",
    ) -> None:
        # Manual sign-in can hunt for the router on cable or post-config addresses
        # even when the saved Address now is stale after an interrupted install.
        if action != "login":
            self._require_lan()
        else:
            self._release_wait()
        with self._lock:
            if self._inflight:
                raise Busy(action)
            self._job_id += 1
            job_id = self._job_id
            self._inflight = True
        self._cancel.clear()
        self._set(phase="busy", job=action, step="login", error="", detail="Signing in.")
        thread = threading.Thread(
            target=self._run_safe,
            args=(action, typed_password, slot, job_id, serial, mac),
            name=f"rutkeeper-bench-{action}",
            daemon=True,
        )
        self._job_thread = thread
        thread.start()

    def run_inline(
        self,
        action: str,
        typed_password: str,
        slot: str = "",
        serial: str = "",
        mac: str = "",
    ) -> None:
        if action != "login":
            self._require_lan()
        else:
            self._release_wait()
        with self._lock:
            if self._inflight:
                raise Busy(action)
            self._job_id += 1
            job_id = self._job_id
            self._inflight = True
        self._cancel.clear()
        self._set(phase="busy", job=action, step="login", error="", detail="Signing in.")
        self._run_safe(action, typed_password, slot, job_id, serial, mac)

    def _release_wait(self) -> None:
        """Let Sign in interrupt a reboot or new-address wait.

        A factory reset puts the router back on 192.168.1.1 with the label
        password while the bench is still waiting for the previous address.
        """
        job = self._job_thread
        phase = str(self.status.get("phase") or "")
        if job is None or not job.is_alive() or phase not in _WAIT_PHASES:
            return
        self._cancel.set()
        job.join(timeout=15)
        if job.is_alive():
            return
        self._cancel.clear()

    def _run_safe(
        self,
        action: str,
        typed_password: str,
        slot: str = "",
        job_id: int = 0,
        serial: str = "",
        mac: str = "",
    ) -> None:
        if typed_password and typed_password != FIXED_ADMIN_PASSWORD:
            self._factory_password = typed_password
        if serial.strip():
            self._label_serial = serial.strip()
        if mac.strip():
            self._label_mac = mac.strip()
        try:
            if action == "install":
                self._install(typed_password, slot)
            elif action == "login":
                self._manual_sign_in(typed_password)
            elif action == "firmware":
                self._flash(typed_password)
            elif action in {"config1", "config2"}:
                self._apply_config(action, typed_password)
            else:
                raise RutosError("Unknown workbench action.")
        except Exception as exc:  # noqa: BLE001 — show it on the bench
            message = str(exc) or exc.__class__.__name__
            self._set(phase="ready" if self.status.get("http") else "offline", job="", error=message, detail=message)
            self.db.add_event("error", "workbench", message)
        finally:
            with self._lock:
                if self._job_id == job_id:
                    self._inflight = False
                    self._factory_password = ""
                    self._label_serial = ""
                    self._label_mac = ""

    def _stopped(self) -> None:
        if self._cancel.is_set():
            raise RutosError("Stopped waiting. Sign in with the password on the router now.")

    def _install(self, typed_password: str, slot: str) -> None:
        if slot not in {"config1", "config2"}:
            raise RutosError("Select Config 1 or Config 2.")
        if self.chosen(slot) is None:
            raise RutosError(f"Put a config file in the {FOLDERS[slot]} folder.")
        if self.chosen("firmware") is None:
            raise RutosError("Put a firmware file in the Firmware folder.")
        self._require_registration()
        # Factory units always start on the cable address until the config lands.
        self._pin_cable_address()
        self._sign_in(typed_password)
        self._set_fixed_password()
        # Firmware reboot stays on the cable address. The address only changes
        # after the config file is uploaded.
        self._flash(FIXED_ADMIN_PASSWORD, hold=True, reconnect=True)
        self._apply_config(slot, FIXED_ADMIN_PASSWORD)

    def _pin_cable_address(self) -> None:
        """Keep Address now on the factory cable IP until config upload finishes."""
        current = _host_only(self.db.setting("bench_host") or CABLE_HOST)
        if current != CABLE_HOST:
            self.db.set_settings({"bench_host": CABLE_HOST})
            self.db.add_event(
                "info",
                "workbench",
                f"Address now reset to {CABLE_HOST} until the config is uploaded.",
            )
        else:
            self.db.set_settings({"bench_host": CABLE_HOST})
        if not _optional_host(self.db.setting("bench_next_host") or ""):
            self.db.set_settings({"bench_next_host": POST_CONFIG_HOST})

    def _post_config_host(self) -> str:
        return _optional_host(self.db.setting("bench_next_host") or "") or POST_CONFIG_HOST

    def _sign_in(self, typed_password: str) -> None:
        self._set(step="login", detail=f"Signing in at {CABLE_HOST}.")
        router, password = self._connect(self._candidates(typed_password), host=CABLE_HOST)
        router.close()
        self._session_password = password
        self._set(step="ready", detail=f"Router answered at {CABLE_HOST}.")

    def _manual_sign_in(self, typed_password: str) -> None:
        """Sign in without flashing — recover after an interrupted install.

        Tries the cable address first, then the post-config address, then
        whatever Address now still holds. Updates Address now to wherever
        the router answers.
        """
        self._set(step="login", detail="Signing in. Looking for the router on known addresses.")
        passwords = self._candidates(typed_password, FIXED_ADMIN_PASSWORD)
        router, password, host = self._connect_any(passwords)
        try:
            if getattr(router, "password_change_required", False):
                how = router.set_admin_password(password, FIXED_ADMIN_PASSWORD)
                self.db.add_event(
                    "info",
                    "workbench",
                    f"Admin password replaced ({how}). The previous password was not stored.",
                )
                password = FIXED_ADMIN_PASSWORD
        finally:
            router.close()
        self._session_password = password
        if host:
            current = _host_only(self.db.setting("bench_host") or CABLE_HOST)
            if host != current:
                self.db.set_settings({"bench_host": host})
                self.db.add_event("info", "workbench", f"Signed in at {host}. Address now was updated.")
            else:
                self.db.add_event("info", "workbench", f"Signed in at {host}.")
        # step "ready" is the install row "Wait for the router". A finished
        # sign-in must not land there, or the checklist looks stuck on that row.
        # Skip the ready_tick bump so this login does not auto-start Install.
        with self._lock:
            self._was_http = True
        self._note_probe(True, True, 1)
        self._set(
            phase="ready",
            job="",
            step="signed-in",
            error="",
            detail=f"Signed in at {host or 'the router'}. Ready to continue.",
        )

    def _set_fixed_password(self) -> None:
        self._set(step="password", detail=f"Setting the admin password at {CABLE_HOST}.")
        router, used = self._connect(self._candidates(self._session_password), host=CABLE_HOST)
        try:
            if used != FIXED_ADMIN_PASSWORD or getattr(router, "password_change_required", False):
                how = router.set_admin_password(used, FIXED_ADMIN_PASSWORD)
                self.db.add_event("info", "workbench", f"Admin password replaced ({how}). The previous password was not stored.")
            else:
                self.db.add_event("info", "workbench", "Router already had the fixed admin password.")
        finally:
            router.close()
        self._session_password = FIXED_ADMIN_PASSWORD
        confirm, _used = self._connect([FIXED_ADMIN_PASSWORD], host=CABLE_HOST)
        confirm.close()

    def _flash(self, typed_password: str, *, hold: bool = False, reconnect: bool = True) -> None:
        path = self.chosen("firmware")
        if path is None:
            raise RutosError("Put a firmware file in the Firmware folder.")
        name = path.name
        content = path.read_bytes()
        # Firmware never changes the LAN address; stay on the cable IP.
        self._pin_cable_address()
        if not self._session_password:
            self._set(step="login", detail=f"Signing in at {CABLE_HOST}.")
        router, password = self._connect(self._candidates(typed_password), host=CABLE_HOST)
        self._session_password = password
        try:
            self._set(step="firmware", detail=f"Uploading {name} at {CABLE_HOST}.")
            router.upload_firmware(name, content)
            self._set(step="firmware", detail="Starting the firmware install. Settings stay, including the admin password.")
            try:
                router.upgrade_staged()
            except RutosError as exc:
                if "reach" not in str(exc).lower():
                    raise
        finally:
            router.close()
        self.db.add_event(
            "warn",
            "workbench",
            f"Firmware install started from {name}. Waiting for the router to reboot at {CABLE_HOST}.",
        )
        self._set(
            phase="rebooting",
            step="firmware-reboot",
            job=self.status.get("job") or "firmware",
            detail=f"Router is rebooting after firmware. Still checking {CABLE_HOST}.",
        )
        if not reconnect:
            return
        self._wait_until_back(CABLE_HOST)
        self._set(step="firmware-reboot", detail=f"Signing in again at {CABLE_HOST}.")
        router, password = self._connect(self._candidates(typed_password), host=CABLE_HOST)
        router.close()
        self._session_password = password
        self.db.add_event("info", "workbench", f"Router is back at {CABLE_HOST} after the firmware install.")
        if hold:
            self._set(phase="busy", job="install", step="config", error="", detail="Firmware is in. Applying the selected config.")
            return
        self._set(phase="ready", job="", step="done", error="", detail=f"Firmware is in. The router answered again at {CABLE_HOST}.")

    def _await_new_address(self) -> None:
        """Wait until the router answers on the post-config address.

        Keep checking the cable address (192.168.1.1) through firmware and the
        config upload. Only after the config file is uploaded do we look for
        192.168.100.1 — answering there confirms the config was applied.
        """
        job = "install" if self.status.get("job") == "install" else str(self.status.get("job") or "")
        nxt = self._post_config_host()
        self._set(
            phase="await-address",
            step="new-address",
            job=job or "install",
            error="",
            detail=f"Config uploaded. Waiting for the router at {nxt}. Answering there confirms the upload.",
        )
        if self.db.setting("bench_simulate") == "1":
            self.db.set_settings({"bench_host": nxt})
            self._set(detail=f"Simulated router answered at {nxt}. Config upload confirmed.")
            self.db.add_event("info", "workbench", f"Router answered at {nxt} after the config. Upload confirmed.")
            return
        deadline = time.monotonic() + 30 * 60
        cable_since: float | None = None
        while time.monotonic() < deadline:
            self._stopped()
            nxt = self._post_config_host()
            ping_ok, http_ok, rtt = probe(nxt)
            self._note_probe(ping_ok, http_ok, rtt)
            if http_ok:
                self.db.set_settings({"bench_host": nxt})
                self._set(detail=f"Router answered at {nxt}. Config upload confirmed.")
                self.db.add_event("info", "workbench", f"Router answered at {nxt} after the config. Upload confirmed.")
                return
            if nxt != CABLE_HOST:
                _cable_ping, cable_http, _cable_rtt = probe(CABLE_HOST)
                if cable_http:
                    now = time.monotonic()
                    cable_since = now if cable_since is None else cable_since
                    if now - cable_since >= CABLE_AFTER_CONFIG_SECONDS:
                        raise RutosError(
                            f"The router answered at {CABLE_HOST}, not at {nxt}. "
                            "A factory reset puts it back on the cable address with the label password. "
                            "Press Sign in."
                        )
                    self._set(
                        detail=(
                            f"Router answered at {CABLE_HOST}. Still waiting for {nxt}. "
                            "Sign in with the label password if this unit was factory reset."
                        )
                    )
                    if self._cancel.wait(3):
                        self._stopped()
                    continue
            cable_since = None
            self._set(detail=f"Waiting for {nxt}. Answering there confirms the config was uploaded.")
            if self._cancel.wait(3):
                self._stopped()
        raise RutosError(f"The router did not answer at {self._post_config_host()} within 30 minutes.")

    def _apply_config(self, slot: str, typed_password: str) -> None:
        path = self.chosen(slot)
        if path is None:
            raise RutosError(f"Put a config file in the {FOLDERS.get(slot, 'config')} folder.")
        self._require_registration()
        target = FIXED_ADMIN_PASSWORD
        name = path.name
        content = path.read_bytes()
        embedded_key = "bench_config1_password_enc" if slot == "config1" else "bench_config2_password_enc"
        embedded = self.secrets.decrypt(self.db.setting(embedded_key))
        # Config is applied on the cable address; the LAN only moves afterward.
        self._pin_cable_address()
        self._set(
            phase="busy",
            job="install" if self.status.get("job") == "install" else slot,
            step="config",
            error="",
            detail=f"Signing in at {CABLE_HOST} to apply {name}.",
        )
        router, password = self._connect(self._candidates(typed_password, embedded), host=CABLE_HOST)
        try:
            self._set(step="config", detail=f"Uploading {name} at {CABLE_HOST}.")
            router.upload_backup(name, content)
            router.apply_backup()
            if isinstance(router, SimRouter):
                router.replace_password(embedded)
        finally:
            router.close()
        nxt = self._post_config_host()
        self.db.add_event("warn", "workbench", f"Applied {name} at {CABLE_HOST}. Waiting for the router at {nxt}.")
        self._set(phase="rebooting", step="config", detail=f"Config uploaded at {CABLE_HOST}. Waiting for {nxt}.")
        # Only now leave the cable IP — seeing the next address confirms success.
        self._await_new_address()
        self._set(step="new-address", detail=f"Signing in at {nxt} to confirm the admin password.")
        router, used = self._connect(self._candidates(typed_password, embedded, password), host=nxt)
        try:
            if used != target:
                how = router.set_admin_password(used, target)
                self.db.add_event("info", "workbench", f"Admin password replaced ({how}). The previous password was not stored.")
            else:
                self.db.add_event("info", "workbench", "Router already had the fixed admin password.")
        finally:
            router.close()
        confirm, _used = self._connect([target], host=nxt)
        confirm.close()
        wifi_name = self._name_and_register(nxt, target, slot)
        self._session_password = ""
        label = "Config 1" if slot == "config1" else "Config 2"
        self._set(
            phase="ready",
            job="",
            step="done",
            error="",
            detail=f"{label} is on. Wireless name is {wifi_name}. The router is in RMS.",
        )
        self.db.add_event("info", "workbench", f"{label} applied. Wireless name is {wifi_name}. The router is in RMS.")

    def _device_log(self) -> Path:
        return self.db.path.parent / "configured-devices.txt"

    def _require_registration(self) -> None:
        """Serial, MAC, token file, and company id are needed before the config is uploaded."""
        if not self._label_serial.strip() or not self._label_mac.strip():
            raise RutosError(
                "Scan the router label before Install. RMS needs the serial number and MAC address from that code."
            )
        format_mac(self._label_mac)
        read_access_token(self.directory)
        company = (self.db.setting("rms_company_id") or "").strip()
        if not company.isdigit():
            raise RutosError("Set the RMS company id on the RMS page.")

    def _name_and_register(self, host: str, password: str, slot: str) -> str:
        """Name the Wi-Fi from the device log, then add the unit to RMS."""
        serial = self._label_serial.strip()
        mac_raw = self._label_mac.strip()
        if not serial or not mac_raw:
            raise RutosError(
                "Scan the router label before Install. RMS needs the serial number and MAC address from that code."
            )
        mac = format_mac(mac_raw)
        token = read_access_token(self.directory)
        company = (self.db.setting("rms_company_id") or "").strip()
        if not company.isdigit():
            raise RutosError("Set the RMS company id on the RMS page.")
        log_path = self._device_log()
        number, wifi_name, known = name_for(log_path, serial, prefix_for(slot))
        self._set(step="wireless", detail=f"Setting the wireless name and password at {host}.")
        router, _used = self._connect([password], host=host)
        try:
            router.set_wireless_name(wifi_name, password)
        finally:
            router.close()
        if not known:
            remember_device(log_path, number, wifi_name, serial, mac)
        self.db.add_event("info", "workbench", f"Wireless name set to {wifi_name}.")
        self._set(step="rms", detail=f"Adding {wifi_name} to RMS from this Pi.")
        if self.db.setting("bench_simulate") == "1":
            self.sim.note_rms(wifi_name, serial, mac)
        else:
            try:
                add_rut(
                    token=token,
                    company_id=company,
                    name=wifi_name,
                    password=password,
                    serial=serial,
                    mac=mac,
                    on_retry=lambda nxt, total: self._set(
                        detail=f"RMS did not answer. Trying again ({nxt} of {total})."
                    ),
                )
            except RutosError as exc:
                raise RutosError(
                    f"{exc} {wifi_name} stays in the device log, so the next run keeps that name."
                ) from exc
        self.db.add_event("info", "workbench", f"{wifi_name} added to RMS.")
        return wifi_name

    def _candidates(self, *extra: str) -> list[str]:
        return _unique([*extra, self._session_password, self._factory_password])

    def _lookup_hosts(self) -> list[str]:
        """Addresses to try when finding a router after an interruption.

        Prefer the factory cable address, then the post-config address, then
        whatever Address now still holds from a previous job.
        """
        settings = self.db.settings()
        host = _host_only(settings.get("bench_host") or CABLE_HOST)
        nxt = _optional_host(settings.get("bench_next_host") or "") or POST_CONFIG_HOST
        return _unique([CABLE_HOST, nxt, host])

    def _connect(self, passwords: list[str], host: str | None = None):
        passwords = _unique(passwords)
        if not passwords:
            raise RutosError(
                "Type the password currently on this router. TUA needs it once to sign in, then replaces it."
            )
        if self.db.setting("bench_simulate") == "1":
            used = self.sim.accept(passwords)
            return self.sim, used
        settings = self.db.settings()
        target = _host_only(host or settings.get("bench_host") or CABLE_HOST)
        username = settings.get("bench_username") or "admin"
        last: RutosError | None = None
        for password in passwords:
            client = RutosClient(target, username, password, verify_tls=False, timeout=40)
            try:
                client.login()
            except RutosError as exc:
                last = exc
                client.close()
                continue
            router = LiveRouter(client)
            if router.password_change_required and password != FIXED_ADMIN_PASSWORD:
                try:
                    how = router.set_admin_password(password, FIXED_ADMIN_PASSWORD)
                    self.db.add_event(
                        "info",
                        "workbench",
                        f"Admin password replaced ({how}). The previous password was not stored.",
                    )
                    client.password = FIXED_ADMIN_PASSWORD
                    client.token = ""
                    client.password_change_required = False
                    client.login()
                    password = FIXED_ADMIN_PASSWORD
                except Exception:
                    router.close()
                    raise
            return router, password
        raise last or RutosError("The router refused the password.")

    def _connect_any(self, passwords: list[str]):
        """Try known LAN addresses until one accepts the password."""
        passwords = _unique(passwords)
        if not passwords:
            raise RutosError(
                "Type the password currently on this router. TUA needs it once to sign in, then replaces it."
            )
        if self.db.setting("bench_simulate") == "1":
            used = self.sim.accept(passwords)
            host = _host_only(self.db.setting("bench_host") or CABLE_HOST)
            return self.sim, used, host
        refused: RutosError | None = None
        missed: RutosError | None = None
        tried: list[str] = []
        for host in self._lookup_hosts():
            tried.append(host)
            self._set(detail=f"Signing in at {host}.")
            try:
                router, password = self._connect(passwords, host=host)
            except RutosError as exc:
                # A later address that is simply down must not hide a real
                # password refusal from the router that did answer.
                if exc.status is None:
                    missed = exc
                else:
                    refused = exc
                continue
            return router, password, host
        where = ", ".join(tried) if tried else "the LAN"
        raise refused or missed or RutosError(f"Could not sign in at {where}.")

    def _wait_until_back(self, host: str | None = None) -> None:
        """Ping/check one address until the router answers again after a reboot."""
        if self.db.setting("bench_simulate") == "1":
            self._mark(ping=False, http=False, rtt_ms=None, detail="Simulated reboot.")
            time.sleep(0.05)
            self._mark(ping=True, http=True, rtt_ms=1, detail="Simulated router is back.")
            return
        target = _host_only(host or self.db.setting("bench_host") or CABLE_HOST)
        started = time.monotonic()
        deadline = started + 8 * 60
        saw_down = False
        while time.monotonic() < deadline:
            self._stopped()
            ping_ok, http_ok, rtt = probe(target)
            self._mark(
                ping=ping_ok,
                http=http_ok,
                rtt_ms=rtt,
                detail=f"Waiting for the router at {target}.",
            )
            if not http_ok:
                saw_down = True
            elif saw_down or time.monotonic() - started > 45:
                return
            if self._cancel.wait(3):
                self._stopped()
        raise RutosError(f"The router did not come back at {target} within 8 minutes.")

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                self._probe_once()
            except Exception:
                pass
            self._stop.wait(2)

    def _probe_once(self) -> None:
        if self.db.setting("bench_simulate") == "1":
            phase = str(self.status.get("phase") or "offline")
            if phase in _HOLD_PHASES:
                return
            current = str(self.status.get("detail") or "")
            keep = phase == "ready" and current and not current.startswith("Plug the router")
            self._mark(
                ping=True,
                http=True,
                rtt_ms=1,
                detail=current if keep else "Simulated router is ready.",
            )
            return
        phase = str(self.status.get("phase") or "offline")
        if phase in _HOLD_PHASES:
            return
        host = _host_only(self.db.setting("bench_host") or CABLE_HOST)
        ping_ok, http_ok, rtt = probe(host)
        signed_in = str(self.status.get("step") or "") == "signed-in"
        detail = ""
        if not http_ok:
            # After a finished or interrupted job, Address now may still be the
            # post-config IP while the next unit answers on the cable address.
            for alt in self._lookup_hosts():
                if alt == host:
                    continue
                alt_ping, alt_http, alt_rtt = probe(alt)
                if alt_http:
                    self.db.set_settings({"bench_host": alt})
                    host = alt
                    ping_ok, http_ok, rtt = alt_ping, alt_http, alt_rtt
                    detail = f"Router answered at {alt}. Address now was updated."
                    self.db.add_event("info", "workbench", detail)
                    break
                if alt_ping and not ping_ok:
                    ping_ok, rtt = alt_ping, alt_rtt
                    detail = f"Ping answered at {alt}. Waiting for the web interface."
        if http_ok:
            if signed_in:
                detail = str(self.status.get("detail") or "") or "Signed in. Ready to continue."
            else:
                detail = detail or "Router is ready."
        elif ping_ok:
            detail = detail or "Ping answered. Waiting for the web interface."
        else:
            detail = "No answer yet. Plug the router into this Pi."
        if signed_in and not http_ok:
            with self._lock:
                self.status["step"] = ""
        self._mark(ping=ping_ok, http=http_ok, rtt_ms=rtt, detail=detail)

    def _require_lan(self) -> None:
        """Firmware, config, and password changes run only on a router that answers here."""
        if self.db.setting("bench_simulate") == "1":
            return
        for host in self._lookup_hosts():
            if lan_ready(host):
                current = _host_only(self.db.setting("bench_host") or CABLE_HOST)
                if host != current:
                    self.db.set_settings({"bench_host": host})
                    self.db.add_event("info", "workbench", f"Router answered at {host}. Address now was updated.")
                return
        raise RutosError("Plug the router into this Pi. Actions start only when it answers on the LAN.")

    def _mark(self, *, ping: bool, http: bool, rtt_ms: int | None, detail: str) -> None:
        with self._lock:
            phase = str(self.status.get("phase") or "offline")
            if phase not in _HOLD_PHASES:
                self.status["phase"] = "ready" if http else "offline"
                if detail:
                    self.status["detail"] = detail
            self.status["ping"] = ping
            self.status["http"] = http
            self.status["rtt_ms"] = rtt_ms
            if http and not self._was_http:
                self.status["ready_tick"] = int(self.status.get("ready_tick") or 0) + 1
            self._was_http = http

    def _note_probe(self, ping: bool, http: bool, rtt_ms: int | None) -> None:
        with self._lock:
            self.status["ping"] = ping
            self.status["http"] = http
            self.status["rtt_ms"] = rtt_ms
            if http and not self._was_http:
                self.status["ready_tick"] = int(self.status.get("ready_tick") or 0) + 1
            self._was_http = http

    def _set(self, **fields: object) -> None:
        with self._lock:
            self.status.update(fields)
        self._pace()

    def _pace(self) -> None:
        """Hold each simulated step long enough for the bench page to show it."""
        if self.db.setting("bench_simulate") != "1":
            return
        try:
            delay = float(os.environ.get("RUTKEEPER_DEMO_DELAY", "0.8") or 0)
        except ValueError:
            delay = 0
        if delay > 0:
            time.sleep(min(delay, 3))

    def _ensure_folders(self) -> None:
        for name in (*FOLDERS.values(), "api"):
            try:
                (self.directory / name).mkdir(parents=True, exist_ok=True)
            except OSError:
                continue

    def chosen(self, slot: str) -> Path | None:
        folder = self.directory / FOLDERS[slot]
        if not folder.is_dir():
            return None
        files = [path for path in folder.iterdir() if path.is_file() and not path.name.startswith(".")]
        if not files:
            return None
        return max(files, key=lambda path: (path.stat().st_mtime, path.name))

    def _name(self, slot: str) -> str:
        path = self.chosen(slot)
        return path.name if path else ""


def probe(host: str) -> tuple[bool, bool, int | None]:
    host = _host_only(host)
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "1", host],
            capture_output=True,
            check=False,
            timeout=3,
        )
        ping_ok = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ping_ok = False
    rtt = int((time.monotonic() - started) * 1000) if ping_ok else None
    http_ok = False
    for port in (443, 80):
        try:
            with socket.create_connection((host, port), 1.5):
                http_ok = True
                break
        except OSError:
            continue
    return ping_ok, http_ok, rtt


def lan_ready(host: str) -> bool:
    """True when the router's web port answers on the LAN."""
    _ping, http_ok, _rtt = probe(host)
    return http_ok


def files_root(data: Path) -> Path:
    """Parent of the Firmware, Config 1, and Config 2 folders."""
    env = os.environ.get("RUTKEEPER_FILES")
    return Path(env) if env else data


def connect_host(host: str) -> str:
    """Validate a bench address and return the host without a scheme."""
    api_base(host if "://" in host or host.startswith("demo") else "https://" + host)
    return _host_only(host)


def _optional_host(host: str) -> str:
    text = (host or "").strip()
    if not text:
        return ""
    return _host_only(text)


def _host_only(host: str) -> str:
    text = (host or "").strip()
    text = text.removeprefix("https://").removeprefix("http://")
    text = text.split("/")[0]
    if "@" in text:
        text = text.split("@", 1)[1]
    return text or CABLE_HOST


def _unique(values: list[str]) -> list[str]:
    found: list[str] = []
    for value in values:
        if value and value not in found:
            found.append(value)
    return found


def _safe_name(name: str) -> str:
    import re

    base = Path(name or "").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (cleaned or "file.bin")[:80]
