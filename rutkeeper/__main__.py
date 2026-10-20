"""Run the TUA dashboard."""

from __future__ import annotations

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
    """Live camera needs HTTPS on a phone. Enable TLS away from localhost."""
    flag = _truthy(os.environ.get("RUTKEEPER_TLS"))
    if flag is not None:
        return flag
    return host not in {"127.0.0.1", "localhost", "::1"}


def main() -> None:
    host = os.environ.get("RUTKEEPER_HOST", "127.0.0.1")
    port = int(os.environ.get("RUTKEEPER_PORT", "8741"))
    data = Path(os.environ.get("RUTKEEPER_DATA") or default_data_dir())
    kwargs: dict[str, object] = {
        "app": create_app(data),
        "host": host,
        "port": port,
        "log_level": "info",
    }
    if use_tls(host):
        cert, key = ensure_tls(data)
        kwargs["ssl_certfile"] = str(cert)
        kwargs["ssl_keyfile"] = str(key)
        scheme = "https"
    else:
        scheme = "http"
    display = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print(f"TUA listening on {scheme}://{display}:{port}", flush=True)
    uvicorn.run(**kwargs)


if __name__ == "__main__":
    main()
