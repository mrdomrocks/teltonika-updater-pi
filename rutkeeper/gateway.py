"""One public port for HTTP redirect and HTTPS app traffic.

Browsers need HTTPS for the live camera. Opening http:// on an HTTPS-only
port looks like a connection reset. This gateway peeks at the first byte:
TLS is terminated and proxied to the local app; plain HTTP is redirected to
https:// on the same host and port.
"""

from __future__ import annotations

import select
import socket
import ssl
import threading
from pathlib import Path


def ssl_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


def start_gateway(
    host: str,
    port: int,
    backend_host: str,
    backend_port: int,
    cert: Path,
    key: Path,
) -> threading.Thread:
    ctx = ssl_context(cert, key)
    bind_host = host
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind((bind_host, port))
    listen.listen(128)
    listen.settimeout(1.0)

    stop = threading.Event()

    def run() -> None:
        try:
            while not stop.is_set():
                try:
                    client, _addr = listen.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                thread = threading.Thread(
                    target=_handle_client,
                    args=(client, ctx, backend_host, backend_port, port),
                    daemon=True,
                )
                thread.start()
        finally:
            try:
                listen.close()
            except OSError:
                pass

    thread = threading.Thread(target=run, name="tua-gateway", daemon=True)
    thread.stop_event = stop  # type: ignore[attr-defined]
    thread.listen_socket = listen  # type: ignore[attr-defined]
    thread.start()
    return thread


def _handle_client(
    client: socket.socket,
    ctx: ssl.SSLContext,
    backend_host: str,
    backend_port: int,
    public_port: int,
) -> None:
    client.settimeout(15)
    try:
        first = client.recv(1, socket.MSG_PEEK)
    except OSError:
        client.close()
        return
    if not first:
        client.close()
        return

    if first[0] != 0x16:
        _redirect_http(client, public_port)
        return

    try:
        tls = ctx.wrap_socket(client, server_side=True)
    except ssl.SSLError:
        try:
            client.close()
        except OSError:
            pass
        return

    try:
        backend = socket.create_connection((backend_host, backend_port), timeout=15)
    except OSError:
        try:
            tls.close()
        except OSError:
            pass
        return

    try:
        _relay(tls, backend)
    finally:
        for sock in (tls, backend):
            try:
                sock.close()
            except OSError:
                pass


def _redirect_http(client: socket.socket, public_port: int) -> None:
    try:
        raw = client.recv(8192)
    except OSError:
        raw = b""
    path = "/"
    host = ""
    if raw:
        text = raw.decode("iso-8859-1", errors="replace")
        lines = text.split("\r\n")
        parts = lines[0].split()
        if len(parts) >= 2 and parts[1]:
            path = parts[1]
        for line in lines[1:]:
            if line.lower().startswith("host:"):
                host = line.split(":", 1)[1].strip()
                break
    if not host:
        host = f"127.0.0.1:{public_port}"
    location_host = _host_with_port(host, public_port)
    location = f"https://{location_host}{path}"
    payload = (
        "HTTP/1.1 308 Permanent Redirect\r\n"
        f"Location: {location}\r\n"
        "Content-Length: 0\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        client.sendall(payload)
    except OSError:
        pass
    try:
        client.close()
    except OSError:
        pass


def _host_with_port(host: str, port: int) -> str:
    if host.startswith("["):
        if "]:" in host:
            return host
        return f"{host}:{port}"
    if host.count(":") == 1:
        return host
    if host.count(":") > 1:
        return f"[{host}]:{port}"
    if port in {80, 443}:
        return host
    return f"{host}:{port}"


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        try:
            readable, _, errored = select.select(sockets, [], sockets, 60)
        except (ValueError, OSError):
            return
        if errored:
            return
        if not readable:
            return
        for src in readable:
            dst = right if src is left else left
            try:
                data = src.recv(65536)
            except OSError:
                return
            if not data:
                return
            try:
                dst.sendall(data)
            except OSError:
                return
