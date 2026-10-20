#!/usr/bin/env bash
set -euo pipefail
cd /workspace
mkdir -p "${RUTKEEPER_DATA:-/tmp/rutkeeper-data}"
export RUTKEEPER_HOST="${RUTKEEPER_HOST:-0.0.0.0}"
export RUTKEEPER_PORT="${RUTKEEPER_PORT:-8741}"
export RUTKEEPER_DATA="${RUTKEEPER_DATA:-/tmp/rutkeeper-data}"
# Cloud preview stays on HTTP; the Pi service enables TLS for live label scanning.
export RUTKEEPER_TLS="${RUTKEEPER_TLS:-0}"
exec .venv/bin/python -m rutkeeper
