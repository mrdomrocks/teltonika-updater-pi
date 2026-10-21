"""Workbench for one RUT plugged into the Pi.

One install signs in, waits until the router answers, sets the admin password,
writes the local firmware on the cable address, applies Config 1 or Config 2
on that same address, then waits for the router's new address. Answering on
the new address confirms the config was uploaded.

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
from rutkeeper.rutos import RutosClient, RutosError, api_base

FOLDERS = {
    "firmware": "Firmware",
    "config1": "Config 1",
    "config2": "Config 2",
}
FIXED_ADMIN_PASSWORD = "Enter@qua1!"
_HOLD_PHASES = {"busy", "rebooting", "await-address"}


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
        self._ensure_folders()
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
                "next_host": settings.get("bench_next_host") or "",
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
            "bench_host": _host_only(host),
            # Config upload moves the router here; default matches the usual post-config LAN.
            "bench_next_host": _optional_host(next_host) or "192.168.100.1",
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

    def start_job(self, action: str, typed_password: str, slot: str = "") -> None:
        # Manual sign-in can hunt for the router on cable or post-config addresses
        # even when the saved Address now is stale after an interrupted install.
        if action != "login":
            self._require_lan()
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
        if action != "login":
            self._require_lan()
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
                self._inflight = False

    def _install(self, typed_password: str, slot: str) -> None:
        if slot not in {"config1", "config2"}:
            raise RutosError("Select Config 1 or Config 2.")
        if self.chosen(slot) is None:
            raise RutosError(f"Put a config file in the {FOLDERS[slot]} folder.")
        if self.chosen("firmware") is None:
            raise RutosError("Put a firmware file in the Firmware folder.")
        self._sign_in(typed_password)
        self._set_fixed_password()
        # Firmware reboot stays on the cable address (usually 192.168.1.1).
        # The address only changes after the config is uploaded.
        self._flash(FIXED_ADMIN_PASSWORD, hold=True, reconnect=True)
        self._apply_config(slot, FIXED_ADMIN_PASSWORD)

    def _sign_in(self, typed_password: str) -> None:
        self._set(step="login", detail="Signing in.")
        router, password = self._connect(self._candidates(typed_password))
        router.close()
        self._session_password = password
        self._set(step="ready", detail="Router answered.")

    def _manual_sign_in(self, typed_password: str) -> None:
        """Sign in without flashing — recover after an interrupted install.

        Tries Address now, then the post-config address, then the usual cable
        address. Updates Address now to wherever the router answers.
        """
        self._set(step="login", detail="Signing in. Looking for the router on known addresses.")
        passwords = self._candidates(typed_password, FIXED_ADMIN_PASSWORD)
        router, password, host = self._connect_any(passwords)
        router.close()
        self._session_password = password
        if host:
            current = _host_only(self.db.setting("bench_host") or "192.168.1.1")
            if host != current:
                self.db.set_settings({"bench_host": host})
                self.db.add_event("info", "workbench", f"Signed in at {host}. Address now was updated.")
            else:
                self.db.add_event("info", "workbench", f"Signed in at {host}.")
        self._mark(ping=True, http=True, rtt_ms=1, detail=f"Signed in at {host or 'the router'}. Ready to continue.")
        self._set(phase="ready", job="", step="ready", error="", detail=f"Signed in at {host or 'the router'}. Ready to continue.")

    def _set_fixed_password(self) -> None:
        self._set(step="password", detail="Setting the admin password.")
        router, used = self._connect(self._candidates(self._session_password))
        try:
            if used != FIXED_ADMIN_PASSWORD:
                how = router.set_admin_password(used, FIXED_ADMIN_PASSWORD)
                self.db.add_event("info", "workbench", f"Admin password replaced ({how}). The previous password was not stored.")
            else:
                self.db.add_event("info", "workbench", "Router already had the fixed admin password.")
        finally:
            router.close()
        self._session_password = FIXED_ADMIN_PASSWORD
        confirm, _used = self._connect([FIXED_ADMIN_PASSWORD])
        confirm.close()

    def _flash(self, typed_password: str, *, hold: bool = False, reconnect: bool = True) -> None:
        path = self.chosen("firmware")
        if path is None:
            raise RutosError("Put a firmware file in the Firmware folder.")
        name = path.name
        content = path.read_bytes()
        if not self._session_password:
            self._set(step="login", detail="Signing in.")
        router, password = self._connect(self._candidates(typed_password))
        self._session_password = password
        try:
            self._set(step="firmware", detail=f"Uploading {name}.")
            router.upload_firmware(name, content)
            self._set(step="firmware", detail="Starting the firmware install. Settings stay, including the admin password.")
            try:
                router.upgrade_staged()
            except RutosError as exc:
                if "reach" not in str(exc).lower():
                    raise
        finally:
            router.close()
        self.db.add_event("warn", "workbench", f"Firmware install started from {name}. Waiting for the router to reboot.")
        self._set(phase="rebooting", step="firmware-reboot", job=self.status.get("job") or "firmware", detail="Router is rebooting after firmware.")
        if not reconnect:
            return
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

    def _await_new_address(self) -> None:
        """Wait until the router answers on the post-config address.

        The cable address (usually 192.168.1.1) stays in use through firmware
        and the config upload. Answering on the next address confirms the
        config was applied successfully.
        """
        job = "install" if self.status.get("job") == "install" else str(self.status.get("job") or "")
        self._set(
            phase="await-address",
            step="new-address",
            job=job or "install",
            error="",
            detail="Waiting for the router on its new address. Answering there confirms the config was uploaded.",
        )
        if self.db.setting("bench_simulate") == "1":
            nxt = _optional_host(self.db.setting("bench_next_host") or "") or "192.168.100.1"
            self.db.set_settings({"bench_host": nxt})
            self._set(detail=f"Simulated router answered at {nxt}. Config upload confirmed.")
            self.db.add_event("info", "workbench", f"Router answered at {nxt} after the config. Upload confirmed.")
            return
        deadline = time.monotonic() + 30 * 60
        while time.monotonic() < deadline:
            nxt = _optional_host(self.db.setting("bench_next_host") or "") or "192.168.100.1"
            ping_ok, http_ok, rtt = probe(nxt)
            self._note_probe(ping_ok, http_ok, rtt)
            if http_ok:
                self.db.set_settings({"bench_host": nxt})
                self._set(detail=f"Router answered at {nxt}. Config upload confirmed.")
                self.db.add_event("info", "workbench", f"Router answered at {nxt} after the config. Upload confirmed.")
                return
            self._set(detail=f"Waiting for {nxt}. Answering there confirms the config was uploaded.")
            time.sleep(3)
        raise RutosError("The router did not answer on the new address within 30 minutes.")

    def _apply_config(self, slot: str, typed_password: str) -> None:
        path = self.chosen(slot)
        if path is None:
            raise RutosError(f"Put a config file in the {FOLDERS.get(slot, 'config')} folder.")
        target = FIXED_ADMIN_PASSWORD
        name = path.name
        content = path.read_bytes()
        embedded_key = "bench_config1_password_enc" if slot == "config1" else "bench_config2_password_enc"
        embedded = self.secrets.decrypt(self.db.setting(embedded_key))
        self._set(phase="busy", job="install" if self.status.get("job") == "install" else slot, step="config", error="", detail=f"Signing in to apply {name}.")
        router, password = self._connect(self._candidates(typed_password, embedded))
        try:
            self._set(step="config", detail=f"Uploading {name}.")
            router.upload_backup(name, content)
            router.apply_backup()
            if isinstance(router, SimRouter):
                router.replace_password(embedded)
        finally:
            router.close()
        self.db.add_event("warn", "workbench", f"Applied {name}. Waiting for the router on its new address.")
        self._set(phase="rebooting", step="config", detail="Config uploaded. Waiting for the router on its new address.")
        # The LAN address stays at the cable IP until the config is applied.
        # Seeing the next address (usually 192.168.100.1) is the success check.
        self._await_new_address()
        self._set(step="new-address", detail="Signing in to confirm the admin password.")
        router, used = self._connect(self._candidates(typed_password, embedded, password))
        try:
            if used != target:
                how = router.set_admin_password(used, target)
                self.db.add_event("info", "workbench", f"Admin password replaced ({how}). The previous password was not stored.")
            else:
                self.db.add_event("info", "workbench", "Router already had the fixed admin password.")
        finally:
            router.close()
        confirm, _used = self._connect([target])
        confirm.close()
        self._session_password = ""
        label = "Config 1" if slot == "config1" else "Config 2"
        self._set(phase="ready", job="", step="done", error="", detail=f"{label} is on. The admin password is set.")
        self.db.add_event("info", "workbench", f"{label} applied and the admin password is set.")

    def _candidates(self, *extra: str) -> list[str]:
        return _unique([*extra, self._session_password])

    def _lookup_hosts(self) -> list[str]:
        """Addresses to try when finding a router after an interruption."""
        settings = self.db.settings()
        host = _host_only(settings.get("bench_host") or "192.168.1.1")
        nxt = _optional_host(settings.get("bench_next_host") or "") or "192.168.100.1"
        return _unique([host, "192.168.1.1", nxt])

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
        target = _host_only(host or settings.get("bench_host") or "192.168.1.1")
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
            return LiveRouter(client), password
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
            host = _host_only(self.db.setting("bench_host") or "192.168.1.1")
            return self.sim, used, host
        last: RutosError | None = None
        tried: list[str] = []
        for host in self._lookup_hosts():
            tried.append(host)
            self._set(detail=f"Signing in at {host}.")
            try:
                router, password = self._connect(passwords, host=host)
            except RutosError as exc:
                last = exc
                continue
            return router, password, host
        where = ", ".join(tried) if tried else "the LAN"
        raise last or RutosError(f"Could not sign in at {where}.")

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
        host = _host_only(self.db.setting("bench_host") or "192.168.1.1")
        ping_ok, http_ok, rtt = probe(host)
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
            detail = detail or "Router is ready."
        elif ping_ok:
            detail = detail or "Ping answered. Waiting for the web interface."
        else:
            detail = "No answer yet. Plug the router into this Pi."
        self._mark(ping=ping_ok, http=http_ok, rtt_ms=rtt, detail=detail)

    def _require_lan(self) -> None:
        """Firmware, config, and password changes run only on a router that answers here."""
        if self.db.setting("bench_simulate") == "1":
            return
        for host in self._lookup_hosts():
            if lan_ready(host):
                current = _host_only(self.db.setting("bench_host") or "192.168.1.1")
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
        for name in FOLDERS.values():
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
