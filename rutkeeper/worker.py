"""Checks, backups, firmware installs, and config pushes.

Manual installs ask for a confirmation checkbox and always take a backup first.
The monthly job only installs when two switches are on: the site is armed, and
that router has auto-apply enabled. Teltonika does not recommend unattended
upgrades; both switches default off.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from rutkeeper.crypto import SecretBox
from rutkeeper.db import DB, utcnow
from rutkeeper.demo import DemoSession
from rutkeeper.firmware import (
    backup_compatibility,
    human_size,
    identity_from_status,
    parse_firmware,
    progress_phase,
    schedule_due,
    slot_key,
)
from rutkeeper.rms import RmsClient, RmsError, parse_id_list
from rutkeeper.rutos import RutosClient, RutosError

SessionFactory = Callable[[dict, str], Any]


def scrub(text: str) -> str:
    return re.sub(
        r'(?i)(password|token|authorization)(["\s:=]+)[^\s,"\']+',
        r"\1\2[redacted]",
        text or "",
    )


def default_session(device: dict, password: str) -> Any:
    if device.get("is_demo") or str(device.get("host", "")).startswith("demo://"):
        return DemoSession(device)
    return RutosClient(
        device["host"],
        device.get("username") or "admin",
        password,
        verify_tls=bool(device.get("verify_tls")),
    )


class Busy(Exception):
    pass


class Runner:
    def __init__(self, db: DB, secrets: SecretBox, backup_dir: Path, session_for: SessionFactory | None = None):
        self.db = db
        self.secrets = secrets
        self.backup_dir = backup_dir
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.session_for = session_for or default_session
        self._guard = threading.Lock()
        self._inflight: set[int] = set()
        self._schedule_lock = threading.Lock()
        self.demo_delay = 0.0

    def password_for(self, device: dict) -> str:
        return self.secrets.decrypt(device.get("password_enc") or "")

    def start(self, device_id: int, action: str, **kwargs: Any) -> None:
        with self._guard:
            if device_id in self._inflight:
                raise Busy(action)
            self._inflight.add(device_id)
        self.db.update_device(device_id, {"busy": 1, "busy_action": action, "last_error": ""})
        thread = threading.Thread(
            target=self._run_safe,
            args=(device_id, action, kwargs),
            name=f"rutkeeper-{action}-{device_id}",
            daemon=True,
        )
        thread.start()

    def run_inline(self, device_id: int, action: str, **kwargs: Any) -> None:
        with self._guard:
            if device_id in self._inflight:
                raise Busy(action)
            self._inflight.add(device_id)
        self.db.update_device(device_id, {"busy": 1, "busy_action": action, "last_error": ""})
        self._run_safe(device_id, action, kwargs)

    def _run_safe(self, device_id: int, action: str, kwargs: dict) -> None:
        try:
            if self.demo_delay:
                device = self.db.device(device_id)
                if device and (device.get("is_demo") or str(device.get("host", "")).startswith("demo://")):
                    time.sleep(self.demo_delay)
            if action == "check":
                self.check(device_id)
            elif action == "backup":
                self.backup(device_id, kind=kwargs.get("kind"))
            elif action == "update":
                self.update(device_id)
            elif action == "push":
                self.push(device_id, int(kwargs["backup_id"]))
            else:
                raise RuntimeError(f"Unknown action {action}.")
        except Exception as exc:  # noqa: BLE001 — record and unlock the device
            message = scrub(str(exc)) or exc.__class__.__name__
            self.db.update_device(
                device_id,
                {"last_error": message, "update_state": "offline" if "route" in message.lower() or "reach" in message.lower() else self._state_or_unknown(device_id)},
            )
            self.db.add_event("error", action, message, device_id=device_id)
        finally:
            self.db.update_device(device_id, {"busy": 0, "busy_action": ""})
            with self._guard:
                self._inflight.discard(device_id)

    def _state_or_unknown(self, device_id: int) -> str:
        device = self.db.device(device_id)
        if not device:
            return "unknown"
        state = device.get("update_state") or "unknown"
        if state == "offline":
            return "offline"
        return state if state in {"current", "available", "unknown", "offline"} else "unknown"

    def _open(self, device: dict) -> Any:
        return self.session_for(device, self.password_for(device))

    def check(self, device_id: int) -> dict[str, Any]:
        device = self._require(device_id)
        session = self._open(device)
        try:
            system = session.system_status()
            firmware = session.firmware_status()
            updates = session.updates_status()
        finally:
            _close(session)
        identity = identity_from_status(system)
        report = parse_firmware(firmware, updates)
        current = report.current or identity.get("firmware") or device.get("firmware") or ""
        fields = {
            "product_code": identity.get("product_code") or device.get("product_code") or "",
            "model": identity.get("model") or device.get("model") or "",
            "serial": identity.get("serial") or device.get("serial") or "",
            "firmware": current,
            "update_version": report.available,
            "update_state": report.state,
            "last_seen": utcnow(),
            "last_error": "",
            "last_detail": scrub(json.dumps({"firmware": firmware, "updates": updates})[:8000]),
        }
        if report.state == "available" and device.get("is_demo"):
            fields["update_version"] = report.available
        self.db.update_device(device_id, fields)
        size = f" ({human_size(report.size_bytes)})" if report.size_bytes else ""
        self.db.add_event(
            "info",
            "check",
            f"{device['name']}: {report.detail}{size}",
            device_id=device_id,
        )
        return fields

    def backup(self, device_id: int, kind: str | None = None) -> dict[str, Any]:
        device = self._require(device_id)
        # Refresh identity first so the file is stamped with product code and firmware.
        if not device.get("is_demo"):
            try:
                self.check(device_id)
                device = self._require(device_id)
            except Exception:
                device = self._require(device_id)
        chosen = kind or self.db.setting("backup_kind", "device") or "device"
        session = self._open(self._require(device_id))
        try:
            content, filename = session.download_backup(chosen)
        finally:
            _close(session)
        if not content:
            raise RutosError("The router returned an empty backup.")
        digest = hashlib.sha256(content).hexdigest()
        stored = f"{digest[:16]}-{_safe_name(filename)}"
        path = self.backup_dir / stored
        path.write_bytes(content)
        path.chmod(0o600)
        backup_id = self.db.insert_backup(
            {
                "device_id": device_id,
                "filename": _safe_name(filename),
                "stored_name": stored,
                "sha256": digest,
                "product_code": device.get("product_code") or "",
                "firmware": device.get("firmware") or "",
                "kind": chosen,
                "size_bytes": len(content),
                "is_profile": 0,
                "profile_name": "",
                "note": "",
            }
        )
        self.db.update_device(device_id, {"last_backup_at": utcnow(), "last_error": "", "last_seen": utcnow()})
        self.db.add_event(
            "info",
            "backup",
            f"Saved a {chosen} backup from {device['name']} ({len(content)} bytes).",
            device_id=device_id,
        )
        return {"id": backup_id, "sha256": digest, "size": len(content)}

    def update(self, device_id: int) -> None:
        device = self._require(device_id)
        report_fields = self.check(device_id)
        device = self._require(device_id)
        if report_fields.get("update_state") != "available":
            self.db.add_event(
                "info",
                "update",
                f"{device['name']} has no stable package waiting. Firmware was not changed.",
                device_id=device_id,
            )
            return
        self.backup(device_id)
        device = self._require(device_id)
        session = self._open(device)
        try:
            session.fota_download("stable")
            self.db.add_event(
                "info",
                "update",
                f"Asked {device['name']} to download the stable firmware.",
                device_id=device_id,
            )
            phase = self._wait_for_download(session, device)
            if phase == "error":
                raise RutosError("The router reported a firmware download error. Upgrade was not started.")
            keep = bool(device.get("keep_settings", 1))
            session.upgrade(keep)
        finally:
            _close(session)
        new_version = device.get("update_version") or ""
        fields: dict[str, Any] = {"last_error": "", "last_seen": utcnow()}
        if device.get("is_demo") and new_version:
            fields.update({"firmware": new_version, "update_version": "", "update_state": "current"})
        else:
            fields["update_state"] = "unknown"
        self.db.update_device(device_id, fields)
        kept = "Settings are kept." if device.get("keep_settings", 1) else "Settings are cleared."
        self.db.add_event(
            "warn",
            "update",
            f"Upgrade started on {device['name']}. The router will reboot and drop off the LAN for a few minutes. {kept}",
            device_id=device_id,
        )

    def _wait_for_download(self, session: Any, device: dict) -> str:
        if device.get("is_demo") or str(device.get("host", "")).startswith("demo://"):
            return progress_phase(session.progress())
        deadline = time.monotonic() + 12 * 60
        unknown_since = time.monotonic()
        while time.monotonic() < deadline:
            try:
                phase = progress_phase(session.progress())
            except RutosError:
                phase = "unknown"
            if phase in {"ready", "error"}:
                return phase
            if phase == "missing":
                time.sleep(60)
                self.db.add_event(
                    "warn",
                    "update",
                    f"{device['name']} has no progress endpoint. Waiting 60 seconds, then starting the upgrade.",
                    device_id=device["id"],
                )
                return "ready"
            if phase == "unknown":
                if time.monotonic() - unknown_since > 90:
                    self.db.add_event(
                        "warn",
                        "update",
                        f"{device['name']} did not report download progress. Starting the upgrade after 90 seconds, matching Teltonika's task sequence.",
                        device_id=device["id"],
                    )
                    return "ready"
            else:
                unknown_since = time.monotonic()
            time.sleep(8)
        raise RutosError("Firmware download did not finish within 12 minutes. Upgrade was not started.")

    def push(self, device_id: int, backup_id: int) -> None:
        device = self._require(device_id)
        backup = self.db.backup(backup_id)
        if not backup:
            raise RutosError("That backup is no longer on this Pi.")
        ok, reason = backup_compatibility(
            backup.get("firmware") or "",
            device.get("firmware") or "",
            backup.get("product_code") or "",
            device.get("product_code") or "",
        )
        if not ok:
            raise RutosError(reason)
        path = self.backup_dir / backup["stored_name"]
        if not path.exists():
            raise RutosError("The backup file is missing from disk.")
        content = path.read_bytes()
        session = self._open(device)
        try:
            session.upload_backup(backup["filename"], content)
            session.apply_backup()
        finally:
            _close(session)
        label = backup.get("profile_name") or backup.get("filename")
        self.db.add_event(
            "warn",
            "push",
            f"Applied {label} to {device['name']}. The router will reboot. The backup includes the admin password.",
            device_id=device_id,
        )
        self.db.update_device(device_id, {"last_error": "", "last_seen": utcnow()})

    def tick(self, now: datetime | None = None) -> None:
        if not self._schedule_lock.acquire(blocking=False):
            return
        try:
            self._tick(now or datetime.now().astimezone())
        finally:
            self._schedule_lock.release()

    def _tick(self, now: datetime) -> None:
        settings = self.db.settings()
        mode = settings.get("when") or "manual"
        try:
            hour = int(settings.get("hour") or 4)
            minute = int(settings.get("minute") or 0)
        except ValueError:
            hour, minute = 4, 0
        if not schedule_due(now, mode, hour, minute):
            self._note_missed(now, mode, hour, minute, settings)
            return
        slot = slot_key(now)
        if settings.get("last_slot") == slot:
            return
        self.db.set_settings({"last_slot": slot})
        armed = settings.get("armed") == "1"
        self.db.add_event("info", "schedule", f"Scheduled pass started ({mode} at {hour:02d}:{minute:02d}).")
        for device in self.db.devices():
            if not device.get("schedule_enabled"):
                continue
            try:
                self.run_inline(device["id"], "check")
            except Busy:
                continue
            fresh = self.db.device(device["id"])
            if not fresh or fresh.get("last_error"):
                continue
            waiting = fresh.get("update_state") == "available"
            if waiting and fresh.get("auto_apply") and armed:
                try:
                    self.run_inline(device["id"], "update")
                except Busy:
                    continue
                continue
            try:
                self.run_inline(device["id"], "backup")
            except Busy:
                continue
            if waiting:
                self.db.add_event(
                    "info",
                    "schedule",
                    f"{fresh['name']} has a stable update. It was not installed. Arm the site and enable auto-apply on the router to allow that.",
                    device_id=fresh["id"],
                )
        if settings.get("rms_in_schedule") == "1":
            if not armed:
                self.db.add_event(
                    "info",
                    "rms",
                    "RMS task group was not fired. Updates are disarmed, and that group installs firmware.",
                )
            else:
                try:
                    self.run_rms(scheduled=True)
                except (RmsError, ValueError) as exc:
                    self.db.add_event("error", "rms", scrub(str(exc)))

    def _note_missed(self, now: datetime, mode: str, hour: int, minute: int, settings: dict) -> None:
        if mode in ("", "manual"):
            return
        slot_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now <= slot_today:
            return
        if not schedule_due(slot_today, mode, hour, minute):
            return
        day = now.strftime("%Y-%m-%d")
        if settings.get("missed_note") == day or (settings.get("last_slot") or "").startswith(day):
            return
        self.db.set_settings({"missed_note": day})
        self.db.add_event(
            "warn",
            "schedule",
            f"The {hour:02d}:{minute:02d} window passed while TUA was stopped. It will not catch up.",
        )

    def run_rms(self, *, scheduled: bool = False) -> object:
        settings = self.db.settings()
        token = self.secrets.decrypt(settings.get("rms_token_enc") or "")
        if not token:
            raise RmsError("Add an RMS access token first.")
        device_ids = parse_id_list(settings.get("rms_device_ids") or "")
        task_ids = parse_id_list(settings.get("rms_task_ids") or "")
        client = RmsClient(token, settings.get("rms_base") or "https://api.rms.teltonika-networks.com")
        try:
            payload = client.run_group(settings.get("rms_group_id") or "", device_ids, task_ids)
        finally:
            client.close()
        how = "Scheduled RMS run" if scheduled else "RMS task group"
        self.db.add_event(
            "warn",
            "rms",
            f"{how} accepted for group {settings.get('rms_group_id')} on device ids {device_ids}.",
        )
        return payload

    def _require(self, device_id: int) -> dict:
        device = self.db.device(device_id)
        if not device:
            raise RutosError("Router not found.")
        return device


def _close(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        close()


def _safe_name(name: str) -> str:
    base = Path(name).name if name else "backup.bin"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (cleaned or "backup.bin")[:80]
