"""Run the TUA dashboard."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn

from rutkeeper.app import create_app, default_data_dir
from rutkeeper.tls import ensure_tls


def _truthy(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def use_tls(host: str) -> bool:
    """Offer HTTPS for live camera scanning on phones."""
    flag = _truthy(os.environ.get("RUTKEEPER_TLS"))
    if flag is not None:
        return flag
    return host not in {"127.0.0.1", "localhost", "::1"}


def tls_port() -> int:
    return int(os.environ.get("RUTKEEPER_TLS_PORT", "8742"))


async def _serve(http: uvicorn.Server, https: uvicorn.Server | None) -> None:
    if https is None:
        await http.serve()
        return
    https.install_signal_handlers = False
    await asyncio.gather(http.serve(), https.serve())


def main() -> None:
    host = os.environ.get("RUTKEEPER_HOST", "127.0.0.1")
    port = int(os.environ.get("RUTKEEPER_PORT", "8741"))
    data = Path(os.environ.get("RUTKEEPER_DATA") or default_data_dir())
    app = create_app(data)
    display = "127.0.0.1" if host in {"0.0.0.0", "::"} else host

    # HTTP on the main port so http://<pi-ip>:8741 works in a browser.
    http = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="info", lifespan="on")
    )
    https: uvicorn.Server | None = None
    print(f"TUA listening on http://{display}:{port}", flush=True)

    if use_tls(host):
        cert, key = ensure_tls(data)
        secure_port = tls_port()
        https = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=secure_port,
                log_level="info",
                lifespan="off",
                ssl_certfile=str(cert),
                ssl_keyfile=str(key),
            )
        )
        print(
            f"TUA live scan on https://{display}:{secure_port} "
            "(accept the self-signed certificate once)",
            flush=True,
        )

    asyncio.run(_serve(http, https))


if __name__ == "__main__":
    main()
