#!/usr/bin/env bash
# odoo-envd kit — thin human/cron entrypoint.
#
# The operational logic lives in the engine's Python flows (core/flows.py);
# this shim runs the *same* code paths the HTTP API uses, in the foreground,
# with no API dependency. Every command is logged to envd/logs/<job>.log.
#
# Usage:
#   envd-kit.sh <flow> [branch] [--param KEY=VALUE ...]
#   envd-kit.sh deploy feature/x
#   envd-kit.sh backup feature/x --param kind=daily
#
# Environment:
#   ENVD_CONFIG   path to envd.toml (default /etc/odoo-envd/envd.toml)

set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ENGINE_DIR}/.venv/bin/python"
if [ ! -x "${PY}" ]; then
    PY="$(command -v python3)"
fi

export ENVD_CONFIG="${ENVD_CONFIG:-/etc/odoo-envd/envd.toml}"

exec "${PY}" -m envd flow "$@" --config "${ENVD_CONFIG}"
