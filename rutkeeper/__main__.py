"""Run the Rutkeeper dashboard."""

from __future__ import annotations

import os

import uvicorn

from rutkeeper.app import create_app


def main() -> None:
    host = os.environ.get("RUTKEEPER_HOST", "127.0.0.1")
    port = int(os.environ.get("RUTKEEPER_PORT", "8741"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
