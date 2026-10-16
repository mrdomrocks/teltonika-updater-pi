"""Pure helpers for RutOS firmware status, versions, and backup compatibility.

Teltonika's March 2026 guidance for unattended stable updates is:

1. GET /firmware/device/updates/status and stop when stable_version is "newest".
2. Take a backup before changing anything.
3. POST /firmware/actions/fota_download {"data": {"type": "stable"}}.
4. POST /firmware/actions/upgrade {"data": {"suppress_validation": 1, "keep_settings": 1}}.

Response shapes differ across RutOS 7.x builds, so parsing stays defensive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FirmwareReport:
    current: str
    available: str
    state: str  # current, available, unknown
    size_bytes: int | None
    detail: str


def unwrap(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _version_from_status(status: dict) -> str:
    data = unwrap(status)
    static = data.get("static") if isinstance(data.get("static"), dict) else {}
    return _text(
        data.get("version")
        or data.get("fw_version")
        or static.get("fw_version")
        or static.get("version")
    )


def _size(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def parse_firmware(status: object, updates: object) -> FirmwareReport:
    """Read current firmware and whether a stable package is waiting.

    Known update payloads:

    - {"data": {"stable_version": "newest"}} — nothing to install
    - {"data": {"device": {"version": "RUTX_R_00.07.11.3", "size": "25266645"}}}
    - {"data": {"stable_version": "RUT2_R_00.07.18"}}
    """
    current = _version_from_status(status if isinstance(status, dict) else {})
    data = unwrap(updates)
    device = data.get("device") if isinstance(data.get("device"), dict) else {}
    size = _size(device.get("size") if device else data.get("size"))

    offered = _text(device.get("version") or data.get("version") or data.get("fw_version"))
    stable = data.get("stable_version")
    stable_text = _text(stable) if not isinstance(stable, dict) else _text(stable.get("version"))

    if isinstance(stable, dict):
        offered = offered or _text(stable.get("version"))
        size = size or _size(stable.get("size"))

    if offered and current and offered == current:
        offered = ""
    if stable_text and current and stable_text == current:
        stable_text = ""

    if offered:
        return FirmwareReport(
            current=current,
            available=offered,
            state="available",
            size_bytes=size,
            detail=f"Stable package {offered} is available.",
        )

    if stable_text.lower() == "newest":
        return FirmwareReport(
            current=current,
            available="",
            state="current",
            size_bytes=None,
            detail="stable_version is newest. Teltonika reports no newer stable firmware.",
        )

    if stable_text and stable_text.lower() != "newest":
        return FirmwareReport(
            current=current,
            available=stable_text,
            state="available",
            size_bytes=size,
            detail=f"Stable package {stable_text} is available.",
        )

    if not data:
        return FirmwareReport(
            current=current,
            available="",
            state="unknown",
            size_bytes=None,
            detail="The router returned an empty firmware update status.",
        )

    if current:
        return FirmwareReport(
            current=current,
            available="",
            state="current",
            size_bytes=None,
            detail="No newer stable version was listed.",
        )

    return FirmwareReport(
        current="",
        available="",
        state="unknown",
        size_bytes=None,
        detail="Firmware status did not include a version.",
    )


def firmware_tuple(version: str) -> tuple[int, ...]:
    """Compare Teltonika build numbers, ignoring the series prefix.

    RUTX_R_00.07.10.2 and RUT2_R_00.07.18 both put the comparable tail after _R_.
    """
    text = version or ""
    if "_R_" in text:
        text = text.split("_R_", 1)[1]
    parts: list[int] = []
    for piece in text.split("."):
        match = re.fullmatch(r"\d+", piece.strip())
        if match:
            parts.append(int(match.group(0)))
    return tuple(parts)


def backup_compatibility(
    backup_firmware: str,
    device_firmware: str,
    backup_code: str,
    device_code: str,
) -> tuple[bool, str]:
    """Teltonika accepts a backup only on the same product code, and only when
    the target firmware is the same or newer than the backup's firmware.
    """
    backup_code = (backup_code or "").strip()
    device_code = (device_code or "").strip()
    if not device_code:
        return False, "Product code is unknown. Check the router before pushing a config."
    if not backup_code:
        return False, "This backup has no product code, so it cannot be matched safely."
    if backup_code != device_code:
        return (
            False,
            f"Product code {device_code} does not match the backup ({backup_code}). "
            "Teltonika rejects backups from a different product code.",
        )
    if backup_firmware and device_firmware:
        if firmware_tuple(backup_firmware) > firmware_tuple(device_firmware):
            return (
                False,
                f"Backup firmware {backup_firmware} is newer than this router ({device_firmware}). "
                "Teltonika rejects backups taken on a newer build.",
            )
    elif not device_firmware:
        return False, "Firmware version is unknown. Check the router before pushing a config."
    return True, "Product code matches and the backup is not from a newer firmware."


def identity_from_status(payload: object) -> dict[str, str]:
    data = unwrap(payload)
    static = data.get("static") if isinstance(data.get("static"), dict) else {}
    mnf = data.get("mnfinfo") if isinstance(data.get("mnfinfo"), dict) else {}
    product = _text(
        mnf.get("product_code")
        or mnf.get("product")
        or data.get("product_code")
        or mnf.get("name")
    )
    model = _text(static.get("model") or static.get("device_name") or data.get("model"))
    serial = _text(mnf.get("serial") or mnf.get("sn") or data.get("serial"))
    firmware = _text(static.get("fw_version") or static.get("version") or data.get("fw_version"))
    return {
        "product_code": product,
        "model": model,
        "serial": serial,
        "firmware": firmware,
    }


def human_size(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def schedule_due(now: datetime, mode: str, hour: int, minute: int) -> bool:
    if mode in ("", "manual"):
        return False
    if now.hour != hour or now.minute != minute:
        return False
    if mode == "daily":
        return True
    if mode == "mondays":
        return now.weekday() == 0
    if mode == "first_monday":
        return now.weekday() == 0 and now.day <= 7
    return False


def slot_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M")


def upgrade_body(keep_settings: bool) -> dict:
    """Body Teltonika support published for the upgrade task."""
    flag = 1 if keep_settings else 0
    return {"data": {"suppress_validation": flag, "keep_settings": flag}}


def fota_body(channel: str = "stable") -> dict:
    return {"data": {"type": channel}}


def progress_phase(payload: object) -> str:
    """Return ready, downloading, error, missing, or unknown."""
    if not isinstance(payload, dict):
        return "unknown"
    if payload.get("_http_status") == 404:
        return "missing"
    data = unwrap(payload)
    percent = None
    for key in ("percent", "progress", "percentage"):
        if key in data:
            try:
                percent = float(data[key])
            except (TypeError, ValueError):
                percent = None
            break
    if percent is not None:
        if percent >= 100:
            return "ready"
        if percent > 0:
            return "downloading"

    blobs = []
    for key in ("status", "state", "message", "description", "result"):
        if key in data:
            blobs.append(str(data[key]))
    if not blobs:
        blobs.append(str(data))
    text = " ".join(blobs).lower()
    if any(word in text for word in ("fail", "error", "invalid")):
        return "error"
    if any(word in text for word in ("downloaded", "complete", "success", "ready", "done")):
        return "ready"
    if any(word in text for word in ("download", "progress", "running", "pending")):
        return "downloading"
    return "unknown"
