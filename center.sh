#!/usr/bin/env bash
# Запуск центра на Linux/macOS/VPS: ./center.sh [--no-browser] [--demo] [--port 8800]
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
$PY -c "import aiohttp, yaml, jinja2, bs4, lxml" 2>/dev/null || $PY -m pip install -r requirements.txt
if command -v npm >/dev/null && [ ! -d bots/lisbot/node_modules ]; then (cd bots/lisbot && npm ci --omit=dev); fi
exec $PY -m center "$@"
