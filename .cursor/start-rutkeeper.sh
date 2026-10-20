#!/usr/bin/env bash
set -euo pipefail
cd /workspace
mkdir -p "${RUTKEEPER_DATA:-/tmp/rutkeeper-data}"
export RUTKEEPER_HOST="${RUTKEEPER_HOST:-0.0.0.0}"
export RUTKEEPER_PORT="${RUTKEEPER_PORT:-8741}"
export RUTKEEPER_DATA="${RUTKEEPER_DATA:-/tmp/rutkeeper-data}"
exec .venv/bin/python -m rutkeeper
