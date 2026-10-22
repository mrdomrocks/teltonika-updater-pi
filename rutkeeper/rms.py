"""Register one configured RUT with Teltonika RMS.

The access token is a one-line Notepad file in the api folder next to Firmware.
The device log lives in the data directory and supplies the next AQC Q1/ or AQC Q2/ number.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx

from rutkeeper.rutos import RutosError

RMS_DEVICES_URL = "https://api.rms.teltonika-networks.com/devices"
TOKEN_FILE = "rms.txt"
NAME_PREFIX = "AQC Q1/"


def prefix_for(slot: str) -> str:
    """Config 1 is AQC Q1/. Config 2 is AQC Q2/."""
    return "AQC Q2/" if slot == "config2" else NAME_PREFIX


def token_path(directory: Path) -> Path:
    return directory / "api" / TOKEN_FILE


def read_access_token(directory: Path) -> str:
    """First non-empty line of api/rms.txt. The file is not copied anywhere else."""
    path = token_path(directory)
    if not path.is_file():
        raise RutosError(f"Put the RMS access token in {path}. Save it from Notepad as one line.")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RutosError(f"Could not read {path}.") from exc
    for line in text.splitlines():
        token = line.strip()
        if token and not token.startswith("#"):
            return token
    raise RutosError(f"{path} is empty. Paste the RMS access token on the first line.")


def format_mac(value: str) -> str:
    """RMS wants a LAN MAC as AA:BB:CC:DD:EE:FF. The label prints it as 12 hex digits."""
    digits = re.sub(r"[^0-9A-Fa-f]", "", value or "")
    if len(digits) != 12:
        raise RutosError("The label MAC address must be 12 hex digits.")
    pairs = [digits[i : i + 2].upper() for i in range(0, 12, 2)]
    return ":".join(pairs)


def read_devices(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    rows: list[dict[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("number,"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        rows.append(
            {
                "number": parts[0],
                "name": parts[1],
                "serial": parts[2],
                "mac": parts[3],
            }
        )
    return rows


def name_for(path: Path, serial: str, prefix: str = NAME_PREFIX) -> tuple[int, str, bool]:
    """Next number for this config, or the name already given to this serial under that prefix."""
    serial = (serial or "").strip()
    prefix = prefix or NAME_PREFIX
    rows = read_devices(path)
    for row in rows:
        if serial and row["serial"] == serial and row["name"].startswith(prefix):
            return int(row["number"]), row["name"], True
    numbers = [
        int(row["number"])
        for row in rows
        if row["name"].startswith(prefix) and str(row["number"]).isdigit()
    ]
    number = 1 + max(numbers, default=0)
    return number, f"{prefix}{number}", False


def remember_device(path: Path, number: int, name: str, serial: str, mac: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        if fresh:
            handle.write("number,name,serial,mac,added\n")
        handle.write(f"{number},{name},{serial},{mac},{stamp}\n")


def add_rut(
    *,
    token: str,
    company_id: str,
    name: str,
    password: str,
    serial: str,
    mac: str,
    transport: httpx.BaseTransport | None = None,
    attempts: int = 4,
    pause: float = 5,
    on_retry: Callable[[int, int], None] | None = None,
) -> None:
    """Add one RUT. password_confirmation is the admin password now on the router."""
    company = (company_id or "").strip()
    if not company.isdigit():
        raise RutosError("Set the RMS company id on the RMS page.")
    body = {
        "data": [
            {
                "company_id": int(company),
                "device_series": "rut",
                "mac": format_mac(mac),
                "serial": (serial or "").strip(),
                "name": name,
                "auto_credit_enable": True,
                "password_confirmation": password,
            }
        ]
    }
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    timeout = httpx.Timeout(40.0, connect=10.0)
    response: httpx.Response | None = None
    with httpx.Client(timeout=timeout, transport=transport, trust_env=False) as client:
        for attempt in range(1, attempts + 1):
            try:
                response = client.post(RMS_DEVICES_URL, json=body, headers=headers)
                break
            except httpx.HTTPError as exc:
                if attempt == attempts:
                    raise RutosError(_connection_message(exc)) from exc
                if on_retry is not None:
                    on_retry(attempt + 1, attempts)
                if pause:
                    time.sleep(pause)
    if response is None:
        raise RutosError("The Pi could not open a connection to RMS. It needs its own internet connection.")
    payload: object
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("success") is False):
        message = _rms_error(payload, f"RMS refused {name} ({response.status_code}).")
        raise RutosError(message)
    if isinstance(payload, dict):
        data = payload.get("data")
        errors = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errors, list) and errors:
            raise RutosError(_rms_error(payload, f"RMS refused {name}."))


def _connection_message(exc: BaseException) -> str:
    """Turn a failed TCP/TLS attempt into a bench message. The token is not included."""
    cause = str(exc).strip()
    lowered = cause.lower()
    if any(word in lowered for word in ("certificate", "ssl", "tls", "cert")):
        return "The Pi rejected the RMS certificate. Set the Pi clock, then run this again."
    if any(word in lowered for word in ("name or service", "nodename", "name resolution", "no address associated")):
        return "The Pi could not look up RMS. Its DNS or internet connection is down."
    if any(word in lowered for word in ("network is unreachable", "no route", "host is unreachable")):
        return (
            "The Pi has no route to RMS. Give the Pi internet on a network other than the bench cable, "
            "and do not let that cable replace the default gateway."
        )
    if not cause or lowered in {"connecterror", "networkerror", "connecttimeout"}:
        cause = "the connection failed before RMS answered"
    return f"The Pi could not open a connection to RMS ({cause}). It needs its own internet connection."


def _rms_error(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("message") or first.get("error") or fallback)
            return str(first)
        if payload.get("message"):
            return str(payload["message"])
    return fallback
