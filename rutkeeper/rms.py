"""Register one configured RUT with Teltonika RMS.

The access token is a one-line Notepad file in the api folder next to Firmware.
The device log lives in the data directory and supplies the next AQC Q1/ or AQC Q2/ number.
"""

from __future__ import annotations

import random
import re
import socket
import struct
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx

from rutkeeper.rutos import RutosError

RMS_DEVICES_URL = "https://api.rms.teltonika-networks.com/devices"
RMS_HOST = "api.rms.teltonika-networks.com"
TOKEN_FILE = "rms.txt"
NAME_PREFIX = "AQC Q1/"
# The bench cable's router. A default route via these gateways is not the hotspot.
BENCH_GATEWAYS = {"192.168.1.1", "192.168.100.1"}


def prefix_for(slot: str) -> str:
    """Config 1 is AQC Q1/. Config 2 is AQC Q2/."""
    return "AQC Q2/" if slot == "config2" else NAME_PREFIX


def default_routes(route_text: str) -> list[tuple[int, str, str]]:
    """Default gateways from /proc/net/route as (metric, interface, gateway)."""
    rows: list[tuple[int, str, str]] = []
    for line in route_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7 or parts[1] != "00000000":
            continue
        try:
            flags = int(parts[3], 16)
            gateway = socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
            metric = int(parts[6])
        except (ValueError, OSError):
            continue
        if flags & 0x2:
            rows.append((metric, parts[0], gateway))
    rows.sort()
    return rows


def hotspot_source(route_text: str, addresses: dict[str, str]) -> str | None:
    """IPv4 of a default route that is not the bench cable."""
    for _metric, iface, gateway in default_routes(route_text):
        if gateway in BENCH_GATEWAYS:
            continue
        address = addresses.get(iface, "")
        if not address or address.startswith(("169.254.", "192.168.100.")):
            continue
        return address
    return None


def internet_source() -> str | None:
    """Hotspot address on this Pi, when the routing table lists one."""
    try:
        text = Path("/proc/net/route").read_text(encoding="utf-8")
    except OSError:
        return None
    addresses: dict[str, str] = {}
    for _metric, iface, _gateway in default_routes(text):
        addresses.setdefault(iface, _iface_ipv4(iface))
    return hotspot_source(text, addresses)


def _iface_ipv4(name: str) -> str:
    try:
        import fcntl
    except ImportError:
        return ""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
    except OSError:
        return ""
    finally:
        sock.close()
    return socket.inet_ntoa(packed[20:24])


def _dns_query(host: str) -> bytes:
    header = struct.pack(">HHHHHH", random.randint(0, 65535), 0x0100, 1, 0, 0, 0)
    question = b"".join(bytes([len(label)]) + label.encode("ascii") for label in host.split(".") if label)
    question += b"\x00" + struct.pack(">HH", 1, 1)
    return header + question


def _dns_a(packet: bytes) -> str | None:
    if len(packet) < 12:
        return None
    qdcount, ancount = struct.unpack(">HHHHHH", packet[:12])[2:4]

    def skip_name(index: int) -> int:
        while index < len(packet):
            length = packet[index]
            if length == 0:
                return index + 1
            if length & 0xC0 == 0xC0:
                return index + 2
            index += 1 + length
        return index

    offset = 12
    for _ in range(qdcount):
        offset = skip_name(offset) + 4
    for _ in range(ancount):
        offset = skip_name(offset)
        if offset + 10 > len(packet):
            return None
        rtype, rclass, _ttl, rdlength = struct.unpack(">HHIH", packet[offset : offset + 10])
        offset += 10
        data = packet[offset : offset + rdlength]
        offset += rdlength
        if rtype == 1 and rclass == 1 and len(data) == 4:
            return socket.inet_ntoa(data)
    return None


def _lookup_ipv4(host: str, source: str | None) -> str | None:
    """Ask the hotspot path for an A record. The bench router is often the system DNS and has no WAN."""
    query = _dns_query(host)
    for server in ("1.1.1.1", "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(2)
            if source:
                try:
                    sock.bind((source, 0))
                except OSError:
                    pass
            sock.sendto(query, (server, 53))
            packet, _peer = sock.recvfrom(512)
        except OSError:
            continue
        finally:
            sock.close()
        found = _dns_a(packet)
        if found:
            return found
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    for info in infos:
        address = info[4][0]
        if address:
            return address
    return None


@contextmanager
def _pin_ipv4(host: str, ip: str | None):
    if not ip:
        yield
        return
    original = socket.getaddrinfo

    def pinned(hostname, port, family=0, socktype=0, proto=0, flags=0):
        if hostname == host and family in (0, socket.AF_UNSPEC, socket.AF_INET):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]
        return original(hostname, port, family, socktype, proto, flags)

    socket.getaddrinfo = pinned
    try:
        yield
    finally:
        socket.getaddrinfo = original


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
    source = None
    resolved = None
    if transport is None:
        source = internet_source()
        resolved = _lookup_ipv4(RMS_HOST, source)
        transport = httpx.HTTPTransport(local_address=source or "0.0.0.0")
    response: httpx.Response | None = None
    with _pin_ipv4(RMS_HOST, resolved):
        with httpx.Client(timeout=timeout, transport=transport, trust_env=False) as client:
            for attempt in range(1, attempts + 1):
                try:
                    response = client.post(RMS_DEVICES_URL, json=body, headers=headers)
                    break
                except httpx.HTTPError as exc:
                    if attempt == attempts:
                        raise RutosError(_connection_message(exc, source)) from exc
                    if on_retry is not None:
                        on_retry(attempt + 1, attempts)
                    if pause:
                        time.sleep(pause)
    if response is None:
        raise RutosError(_connection_message(OSError("the connection failed before RMS answered"), source))
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


def _connection_message(exc: BaseException, source: str | None = None) -> str:
    """Turn a failed TCP/TLS attempt into a bench message. The token is not included."""
    cause = str(exc).strip()
    lowered = cause.lower()
    via = f" Tried from the hotspot address {source}." if source else ""
    if any(word in lowered for word in ("certificate", "ssl", "tls", "cert")):
        return f"The Pi rejected the RMS certificate. Set the Pi clock, then run this again.{via}"
    if any(word in lowered for word in ("name or service", "nodename", "name resolution", "no address associated")):
        return f"The Pi could not look up RMS through the hotspot.{via}"
    if any(word in lowered for word in ("network is unreachable", "no route", "host is unreachable")):
        return (
            "The Pi has no route to RMS from the hotspot. "
            "The bench cable is taking the default route."
            + via
        )
    if not cause or lowered in {"connecterror", "networkerror", "connecttimeout"}:
        cause = "the connection failed before RMS answered"
    return f"The Pi could not open a connection to RMS ({cause}).{via}"


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
