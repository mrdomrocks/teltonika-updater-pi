"""Workbench for one RUT plugged into the Pi.

The firmware image and two config files live on the Pi. A watcher pings the
router address. One install signs in, writes the local firmware, waits for
the router, applies Config 1 or Config 2, then replaces the admin password
with the single password stored on the Pi.

The password that was already on the router is held in memory for that job
only. It is not written to the database.
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
from rutkeeper.rutos import RutosClient, RutosError, api_base

SLOTS = {
    "firmware": "firmware.bin",
    "config1": "config-1.bin",
    "config2": "config-2.bin",
}
NAME_KEYS = {
    "firmware": "bench_firmware_name",
    "config1": "bench_config1_name",
    "config2": "bench_config2_name",
}


class Busy(Exception):
    pass


class SimRouter:
    """Stand-in so the bench buttons can be tried with no hardware."""

    def __init__(self) -> None:
        self.admin_password = ""
        self.firmware = ""
        self.config = ""
        self.password_changes: list[str] = []

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

    def replace_password(self, password: str) -> None:
        """What a config file does when it carries its own admin password."""
        if password:
            self.admin_password = password

    def set_admin_password(self, current: str, new: str) -> str:
        if current != self.admin_password:
            raise RutosError("Wrong current password.")
        self.admin_password = new
        self.password_changes.append(new)
        return "simulated"

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

    def close(self) -> None:
        self.client.close()


class Bench:
    def __init__(self, db: DB, secrets: SecretBox, directory: Path):
        self.db = db
        self.secrets = secrets
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sim = SimRouter()
        self._session_password = ""
        self._lock = threading.Lock()
        self._inflight = False
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
                "host": settings.get("bench_host") or "192.168.1.1",
                "username": settings.get("bench_username") or "admin",
                "simulate": settings.get("bench_simulate") == "1",
                "has_target_password": bool(settings.get("bench_target_password_enc")),
                "firmware_name": settings.get("bench_firmware_name") or "",
                "config1_name": settings.get("bench_config1_name") or "",
                "config2_name": settings.get("bench_config2_name") or "",
                "config1_has_password": bool(settings.get("bench_config1_password_enc")),
                "config2_has_password": bool(settings.get("bench_config2_password_enc")),
                "firmware_ready": self._path("firmware").exists(),
                "config1_ready": self._path("config1").exists(),
                "config2_ready": self._path("config2").exists(),
            }
        )
        return status

    def save_setup(self, host: str, username: str, target_password: str, simulate: bool) -> None:
        cleaned = _host_only(host)
        values = {
            "bench_host": cleaned,
            "bench_username": (username or "admin").strip()[:80] or "admin",
            "bench_simulate": "1" if simulate else "0",
        }
        if target_password:
            values["bench_target_password_enc"] = self.secrets.encrypt(target_password)
        self.db.set_settings(values)
        if simulate:
            self._mark(ping=True, http=True, rtt_ms=1, detail="Simulated router is ready.")
        else:
            self.sim = SimRouter()

    def save_config_password(self, slot: str, password: str) -> None:
        if slot not in {"config1", "config2"}:
            raise RutosError("Choose config 1 or config 2.")
        key = "bench_config1_password_enc" if slot == "config1" else "bench_config2_password_enc"
        self.db.set_settings({key: self.secrets.encrypt(password) if password else ""})

    def store_file(self, slot: str, filename: str, content: bytes) -> None:
        if slot not in SLOTS:
            raise RutosError("Unknown file slot.")
        limit = 80 * 1024 * 1024 if slot == "firmware" else 32 * 1024 * 1024
        if not content:
            raise RutosError("That file is empty.")
        if len(content) > limit:
            raise RutosError("That file is too large to store on this Pi.")
        path = self._path(slot)
        path.write_bytes(content)
        path.chmod(0o600)
        self.db.set_settings({NAME_KEYS[slot]: _safe_name(filename)})
        self.db.add_event("info", "workbench", f"Stored {_safe_name(filename)} on this Pi.")

    def start_job(self, action: str, typed_password: str, slot: str = "") -> None:
        with self._lock:
            if self._inflight:
                raise Busy(action)
            self._inflight = True
        self._set(phase="busy", job=action, step="login", error="", detail="Signing in.")
        thread = threading.Thread(
            target=self._run_safe,
            args=(action, typed_password, slot),
            name=f"rutkeeper-bench-{action}",
            daemon=True,
        )
        thread.start()

    def run_inline(self, action: str, typed_password: str, slot: str = "") -> None:
        with self._lock:
            if self._inflight:
                raise Busy(action)
            self._inflight = True
        self._set(phase="busy", job=action, step="login", error="", detail="Signing in.")
        self._run_safe(action, typed_password, slot)

    def _run_safe(self, action: str, typed_password: str, slot: str = "") -> None:
        try:
            if action == "install":
                self._install(typed_password, slot)
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
                self._inflight = False

    def _install(self, typed_password: str, slot: str) -> None:
        if slot not in {"config1", "config2"}:
            raise RutosError("Select Config 1 or Config 2.")
        if not self._path(slot).exists():
            raise RutosError("Store that config on the Pi before installing.")
        self._flash(typed_password, hold=True)
        self._apply_config(slot, typed_password)

    def _flash(self, typed_password: str, *, hold: bool = False) -> None:
        path = self._path("firmware")
        if not path.exists():
            raise RutosError("Store a firmware file on the Pi first.")
        name = self.db.setting("bench_firmware_name") or "firmware.bin"
        content = path.read_bytes()
        self._set(step="login", detail="Signing in.")
        router, password = self._connect(self._candidates(typed_password))
        self._session_password = password
        try:
            self._set(step="firmware", detail=f"Uploading {name}.")
            router.upload_firmware(name, content)
            self._set(step="firmware", detail="Starting the firmware install. Settings stay so this login still works.")
            try:
                router.upgrade_staged()
            except RutosError as exc:
                if "reach" not in str(exc).lower():
                    raise
        finally:
            router.close()
        self.db.add_event("warn", "workbench", f"Firmware install started from {name}. Waiting for the router to reboot.")
        self._set(phase="rebooting", step="firmware-reboot", job="firmware", detail="Router is rebooting after firmware.")
        self._wait_until_back()
        self._set(step="firmware-reboot", detail="Signing in again.")
        router, password = self._connect(self._candidates(typed_password))
        router.close()
        self._session_password = password
        self.db.add_event("info", "workbench", "Router is back after the firmware install.")
        if hold:
            self._set(phase="busy", job="install", step="config", error="", detail="Firmware is in. Applying the selected config.")
            return
        self._set(phase="ready", job="", step="done", error="", detail="Firmware is in. The router answered again.")

    def _apply_config(self, slot: str, typed_password: str) -> None:
        path = self._path(slot)
        if not path.exists():
            raise RutosError("Store that config file on the Pi first.")
        target = self.secrets.decrypt(self.db.setting("bench_target_password_enc"))
        if not target:
            raise RutosError("Set the password every router should end up with before applying a config.")
        name = self.db.setting(NAME_KEYS[slot]) or slot
        content = path.read_bytes()
        embedded_key = "bench_config1_password_enc" if slot == "config1" else "bench_config2_password_enc"
        embedded = self.secrets.decrypt(self.db.setting(embedded_key))
        self._set(step="config", detail=f"Signing in to apply {name}.")
        router, password = self._connect(self._candidates(typed_password, embedded))
        try:
            self._set(step="config", detail=f"Uploading {name}.")
            router.upload_backup(name, content)
            router.apply_backup()
            if isinstance(router, SimRouter):
                router.replace_password(embedded)
        finally:
            router.close()
        self.db.add_event("warn", "workbench", f"Applied {name}. Waiting for the router to come back so the password can be replaced.")
        self._set(phase="rebooting", step="config-reboot", job=slot, detail="Router is rebooting after the config.")
        self._wait_until_back()
        self._set(step="password", detail="Signing in to set the shared password.")
        router, used = self._connect(self._candidates(typed_password, embedded, password))
        try:
            if used != target:
                how = router.set_admin_password(used, target)
                self.db.add_event("info", "workbench", f"Admin password replaced ({how}). The previous password was not stored.")
            else:
                self.db.add_event("info", "workbench", "Router already accepted the shared password.")
        finally:
            router.close()
        confirm, _used = self._connect([target])
        confirm.close()
        self._session_password = ""
        label = "Config 1" if slot == "config1" else "Config 2"
        self._set(phase="ready", job="", step="done", error="", detail=f"{label} is on. The admin password is now the shared one.")
        self.db.add_event("info", "workbench", f"{label} applied and the admin password is the shared password.")

    def _candidates(self, *extra: str) -> list[str]:
        target = self.secrets.decrypt(self.db.setting("bench_target_password_enc"))
        return _unique([*extra, self._session_password, target])

    def _connect(self, passwords: list[str]):
        passwords = _unique(passwords)
        if not passwords:
            raise RutosError(
                "Type the password currently on this router. TUA needs it once to sign in, then replaces it."
            )
        if self.db.setting("bench_simulate") == "1":
            used = self.sim.accept(passwords)
            return self.sim, used
        settings = self.db.settings()
        host = settings.get("bench_host") or "192.168.1.1"
        username = settings.get("bench_username") or "admin"
        last: RutosError | None = None
        for password in passwords:
            client = RutosClient(host, username, password, verify_tls=False, timeout=40)
            try:
                client.login()
            except RutosError as exc:
                last = exc
                client.close()
                continue
            return LiveRouter(client), password
        raise last or RutosError("The router refused the password.")

    def _wait_until_back(self) -> None:
        if self.db.setting("bench_simulate") == "1":
            self._mark(ping=False, http=False, rtt_ms=None, detail="Simulated reboot.")
            time.sleep(0.05)
            self._mark(ping=True, http=True, rtt_ms=1, detail="Simulated router is back.")
            return
        host = _host_only(self.db.setting("bench_host") or "192.168.1.1")
        started = time.monotonic()
        deadline = started + 8 * 60
        saw_down = False
        while time.monotonic() < deadline:
            ping_ok, http_ok, rtt = probe(host)
            self._mark(ping=ping_ok, http=http_ok, rtt_ms=rtt, detail="Waiting for the router to answer again.")
            if not http_ok:
                saw_down = True
            elif saw_down or time.monotonic() - started > 45:
                return
            time.sleep(3)
        raise RutosError("The router did not come back within 8 minutes.")

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
            if phase in {"busy", "rebooting"}:
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
        if phase in {"busy", "rebooting"}:
            return
        host = _host_only(self.db.setting("bench_host") or "192.168.1.1")
        ping_ok, http_ok, rtt = probe(host)
        if http_ok:
            detail = "Router is ready."
        elif ping_ok:
            detail = "Ping answered. Waiting for the web interface."
        else:
            detail = "No answer yet. Plug the router into this Pi."
        self._mark(ping=ping_ok, http=http_ok, rtt_ms=rtt, detail=detail)

    def _mark(self, *, ping: bool, http: bool, rtt_ms: int | None, detail: str) -> None:
        with self._lock:
            phase = str(self.status.get("phase") or "offline")
            if phase not in {"busy", "rebooting"}:
                self.status["phase"] = "ready" if http else "offline"
                if phase not in {"busy", "rebooting"} and detail:
                    self.status["detail"] = detail
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

    def _path(self, slot: str) -> Path:
        return self.directory / SLOTS[slot]


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


def connect_host(host: str) -> str:
    """Validate a bench address and return the host without a scheme."""
    api_base(host if "://" in host or host.startswith("demo") else "https://" + host)
    return _host_only(host)


def _host_only(host: str) -> str:
    text = (host or "").strip()
    text = text.removeprefix("https://").removeprefix("http://")
    text = text.split("/")[0]
    if "@" in text:
        text = text.split("@", 1)[1]
    return text or "192.168.1.1"


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
