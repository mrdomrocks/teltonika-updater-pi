"""Register one configured RUT with Teltonika RMS.

The access token is a one-line Notepad file in the api folder next to Firmware.
The device log lives in the data directory and supplies the next AQC Q1/ or AQC Q2/ number.
"""

from __future__ import annotations

import re
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
    try:
        with httpx.Client(timeout=40, transport=transport) as client:
            response = client.post(
                RMS_DEVICES_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        raise RutosError(f"Could not reach RMS: {exc.__class__.__name__}.") from exc
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
