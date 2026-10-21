"""Teltonika label QR codes.

The code on the router is a Wi-Fi setup string with the device login added:

WIFI:T:WPA;S:<ssid>;P:<wifi password>;SN:<serial>;I:<imei>;M:<mac>;U:admin;PW:<device password>;B:<batch>;

PW is the admin password. P is only the Wi-Fi password.
Keep this in step with rutkeeper/static/label.js.
"""

from __future__ import annotations


class LabelError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_label(text: str) -> dict[str, str]:
    """Return the device password and username from a label scan."""
    raw = (text or "").replace("\ufeff", "").replace("\x00", "").strip()
    if not raw:
        raise LabelError("empty")
    if raw.lower().startswith(("http://", "https://")):
        raise LabelError("unrecognized")

    body = raw[5:] if raw[:5].upper() == "WIFI:" else raw
    structured = ";" in body or _looks_like_fields(body)
    if structured:
        fields: dict[str, str] = {}
        for part in _split_unescaped(body, ";"):
            part = part.strip()
            if not part:
                continue
            bits = _split_unescaped(part, ":")
            if len(bits) < 2:
                continue
            key = bits[0].strip().upper()
            value = _unescape(":".join(bits[1:])).strip()
            if key:
                fields[key] = value
        password = fields.get("PW") or ""
        if password:
            return {
                "password": password,
                "username": fields.get("U") or "",
                "serial": fields.get("SN") or "",
                "mac": fields.get("M") or "",
            }
        if fields:
            raise LabelError("no-password")

    if ";" not in raw and len(raw) <= 128:
        return {"password": raw, "username": "", "serial": "", "mac": ""}
    raise LabelError("unrecognized")


def _looks_like_fields(body: str) -> bool:
    stripped = body.lstrip()
    mark = stripped.find(":")
    if mark <= 0:
        return False
    key = stripped[:mark]
    return key.isascii() and key.isalpha() and len(key) <= 8


def _split_unescaped(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in text:
        if escaped:
            buf.append("\\")
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == sep:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if escaped:
        buf.append("\\")
    parts.append("".join(buf))
    return parts


def _unescape(value: str) -> str:
    out: list[str] = []
    escaped = False
    for ch in value:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            out.append(ch)
    if escaped:
        out.append("\\")
    return "".join(out)
