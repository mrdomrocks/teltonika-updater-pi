"""Run the TUA dashboard."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from rutkeeper.app import create_app, default_data_dir
from rutkeeper.gateway import start_gateway
from rutkeeper.tls import ensure_tls


def _truthy(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def use_tls(host: str) -> bool:
    """HTTPS on the workbench port so phones can live-scan on the same URL."""
    flag = _truthy(os.environ.get("RUTKEEPER_TLS"))
    if flag is not None:
        return flag
    return host not in {"127.0.0.1", "localhost", "::1"}


def backend_port() -> int:
    return int(os.environ.get("RUTKEEPER_BACKEND_PORT", "18741"))


def main() -> None:
    host = os.environ.get("RUTKEEPER_HOST", "127.0.0.1")
    port = int(os.environ.get("RUTKEEPER_PORT", "8741"))
    data = Path(os.environ.get("RUTKEEPER_DATA") or default_data_dir())
    app = create_app(data)
    display = "127.0.0.1" if host in {"0.0.0.0", "::"} else host

    if not use_tls(host):
        print(f"TUA listening on http://{display}:{port}", flush=True)
        uvicorn.run(app, host=host, port=port, log_level="info")
        return

    cert, key = ensure_tls(data)
    inner = backend_port()
    start_gateway(host, port, "127.0.0.1", inner, cert, key)
    print(f"TUA listening on https://{display}:{port}", flush=True)
    print("http:// on this port redirects to https:// (accept the certificate once).", flush=True)
    # App stays on loopback HTTP; the gateway terminates TLS on the public port.
    uvicorn.run(app, host="127.0.0.1", port=inner, log_level="info")


if __name__ == "__main__":
    main()
