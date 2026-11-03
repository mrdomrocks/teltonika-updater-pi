"""Register one configured RUT with Teltonika RMS.

The access token is a one-line Notepad file in the api folder next to Firmware.
The device log lives in the data directory and supplies the next AQC Q1/ or AQC Q2/ number.
"""

from __future__ import annotations

import os
import random
import re
import socket
import struct
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
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
# Outgoing interface for one socket. Unprivileged, unlike SO_BINDTODEVICE.
IP_UNICAST_IF = 50


@dataclass(frozen=True)
class Hotspot:
    address: str
    interface: str
    gateway: str


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


def hotspot_path(route_text: str, addresses: dict[str, str]) -> Hotspot | None:
    """First default route that is not the bench cable."""
    for _metric, iface, gateway in default_routes(route_text):
        if gateway in BENCH_GATEWAYS:
            continue
        address = addresses.get(iface, "")
        if not address or address.startswith(("169.254.", "192.168.100.")):
            continue
        return Hotspot(address, iface, gateway)
    return None


def hotspot_source(route_text: str, addresses: dict[str, str]) -> str | None:
    """IPv4 of a default route that is not the bench cable."""
    found = hotspot_path(route_text, addresses)
    return found.address if found else None


def bench_route_preferred(route_text: str) -> bool:
    """True when a bench gateway wins, or ties, the default route."""
    bench_metric: int | None = None
    other_metric: int | None = None
    for metric, _iface, gateway in default_routes(route_text):
        if gateway in BENCH_GATEWAYS:
            if bench_metric is None:
                bench_metric = metric
        elif other_metric is None:
            other_metric = metric
    if bench_metric is None or other_metric is None:
        return False
    return bench_metric <= other_metric


def _route_table() -> str:
    try:
        return Path("/proc/net/route").read_text(encoding="utf-8")
    except OSError:
        return ""


def internet_path(route_text: str | None = None) -> Hotspot | None:
    """Hotspot route on this Pi, when the routing table lists one."""
    text = _route_table() if route_text is None else route_text
    if not text:
        return None
    addresses: dict[str, str] = {}
    for _metric, iface, _gateway in default_routes(text):
        addresses.setdefault(iface, _iface_ipv4(iface))
    return hotspot_path(text, addresses)


def internet_source() -> str | None:
    """Hotspot address on this Pi, when the routing table lists one."""
    found = internet_path()
    return found.address if found else None


def interface_socket_options(interface: str, ifindex: int | None = None) -> list[tuple[int, int, bytes]]:
    """Pin a socket to one interface. A source address alone still follows the default route.

    Set this before connect(). httpx applies its socket_options only after the
    TCP handshake, and by then the kernel has already chosen the bench cable.
    """
    if not interface and ifindex is None:
        return []
    index = ifindex
    if index is None:
        if not hasattr(socket, "if_nametoindex"):
            return []
        try:
            index = socket.if_nametoindex(interface)
        except OSError:
            return []
    packed = struct.pack("I", socket.htonl(index))
    if ifindex is None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, packed)
        except OSError:
            return []
        finally:
            probe.close()
    return [(socket.IPPROTO_IP, IP_UNICAST_IF, packed)]


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


def _lookup_ipv4(host: str, source: str | None, interface: str = "") -> str | None:
    """Ask the hotspot path for an A record. The bench router is often the system DNS and has no WAN."""
    query = _dns_query(host)
    pinned = interface_socket_options(interface)
    for server in ("1.1.1.1", "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(2)
            for option in pinned:
                try:
                    sock.setsockopt(*option)
                except OSError:
                    pass
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


def _open_tcp(
    host: str,
    port: int,
    timeout: float | None,
    local_address: str | None,
    early_options: list[tuple[int, int, bytes]],
) -> socket.socket:
    """Connect an IPv4 socket after the interface pin is in place."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        for option in early_options:
            sock.setsockopt(*option)
        if timeout is not None:
            sock.settimeout(timeout)
        if local_address:
            sock.bind((local_address, 0))
        sock.connect((host, port))
        return sock
    except BaseException:
        sock.close()
        raise


def _hotspot_transport(
    source: str | None,
    early_options: list[tuple[int, int, bytes]],
) -> httpx.BaseTransport:
    """HTTP transport whose TCP handshake leaves through the hotspot interface."""
    import httpcore
    from httpcore._backends.sync import SyncBackend, SyncStream

    class _PinnedBackend(SyncBackend):
        def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: list[tuple[int, int, int | bytes]] | None = None,
        ) -> SyncStream:
            try:
                sock = _open_tcp(host, port, timeout, local_address, early_options)
            except TimeoutError as exc:
                raise httpcore.ConnectTimeout(str(exc) or "timed out") from exc
            except OSError as exc:
                raise httpcore.ConnectError(str(exc) or "connect failed") from exc
            try:
                for option in socket_options or []:
                    sock.setsockopt(*option)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except BaseException:
                sock.close()
                raise
            return SyncStream(sock)

    transport = httpx.HTTPTransport(local_address=source or "0.0.0.0")
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RutosError("The HTTP library on this Pi cannot pin RMS to the hotspot.")
    pool._network_backend = _PinnedBackend()
    return transport


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


def _mac_key(value: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", value or "").upper()


def name_for(path: Path, serial: str, prefix: str = NAME_PREFIX, mac: str = "") -> tuple[int, str, bool]:
    """Next number for this config, or the name already given to this serial and MAC."""
    serial = (serial or "").strip()
    prefix = prefix or NAME_PREFIX
    mac_key = _mac_key(mac)
    rows = read_devices(path)
    for row in rows:
        if not serial or row["serial"] != serial or not str(row["name"]).startswith(prefix):
            continue
        row_key = _mac_key(row["mac"])
        if mac_key and row_key and mac_key != row_key:
            continue
        return int(row["number"]), row["name"], True
    numbers = [
        int(row["number"])
        for row in rows
        if str(row["name"]).startswith(prefix) and str(row["number"]).isdigit()
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
        handle.flush()
        os.fsync(handle.fileno())


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
    interface = ""
    bench_wins = False
    resolved = None
    options: list[tuple[int, int, bytes]] = []
    pinned = True
    if transport is None:
        routes = _route_table()
        bench_wins = bench_route_preferred(routes)
        found = internet_path(routes)
        if found is not None:
            source = found.address
            interface = found.interface
        resolved = _lookup_ipv4(RMS_HOST, source, interface)
        options = interface_socket_options(interface)
        pinned = bool(options)
        transport = _hotspot_transport(source, options)
    response: httpx.Response | None = None
    with _pin_ipv4(RMS_HOST, resolved):
        with httpx.Client(timeout=timeout, transport=transport, trust_env=False) as client:
            for attempt in range(1, attempts + 1):
                try:
                    response = client.post(RMS_DEVICES_URL, json=body, headers=headers)
                    break
                except httpx.HTTPError as exc:
                    if attempt == attempts:
                        raise RutosError(
                            _connection_message(exc, source, interface, bench_wins, pinned=pinned)
                        ) from exc
                    if on_retry is not None:
                        on_retry(attempt + 1, attempts)
                    if pause:
                        time.sleep(pause)
    if response is None:
        raise RutosError(
            _connection_message(
                OSError("the connection failed before RMS answered"),
                source,
                interface,
                bench_wins,
                pinned=pinned,
            )
        )
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


def _connection_message(
    exc: BaseException,
    source: str | None = None,
    interface: str = "",
    bench_preferred: bool = False,
    pinned: bool = True,
) -> str:
    """Turn a failed TCP/TLS attempt into a bench message. The token is not included."""
    cause = str(exc).strip()
    lowered = cause.lower()
    via = ""
    if source:
        via = f" Tried from the hotspot address {source}"
        if interface:
            via += f" on {interface}"
        via += "."
    bench = " The bench cable is the preferred default route." if bench_preferred else ""
    pin = " The hotspot interface was not pinned." if interface and not pinned else ""
    if any(word in lowered for word in ("certificate", "ssl", "tls", "cert")):
        return f"The Pi rejected the RMS certificate. Set the Pi clock, then run this again.{via}{pin}"
    if any(word in lowered for word in ("name or service", "nodename", "name resolution", "no address associated")):
        return f"The Pi could not look up RMS through the hotspot.{via}{bench}{pin}"
    if any(word in lowered for word in ("network is unreachable", "no route", "host is unreachable")):
        return (
            "The Pi has no route to RMS from the hotspot. "
            "The bench cable is taking the default route."
            + via
            + pin
        )
    if not cause or lowered in {"connecterror", "networkerror", "connecttimeout"}:
        cause = "the connection failed before RMS answered"
    return f"The Pi could not open a connection to RMS ({cause}).{via}{bench}{pin}"


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
