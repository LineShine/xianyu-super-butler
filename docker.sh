#!/bin/bash
set -euo pipefail
WORK_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORK_DIR"
COMMAND="${1:-start}"
if [ "$(uname -s)" = "Darwin" ]; then
    case "$COMMAND" in
        start) exec bash dev.sh switch docker ;;
        prepare|build) exec bash dev.sh build ;;
        restart|update) exec bash dev.sh switch docker --restart ;;
        stop|status|logs) exec bash dev.sh "$@" ;;
        *) echo "操作：prepare build start stop restart status logs update"; exit 2 ;;
    esac
fi
command -v docker >/dev/null || { echo "请先安装 Docker Engine 和 Compose v2。"; exit 1; }
command -v python3 >/dev/null || { echo "请先安装 Python 3.11+ 和 venv。"; exit 1; }
RUNTIME="$WORK_DIR/yutaixian_data/local_dev"
mkdir -p "$RUNTIME"
chmod 700 "$RUNTIME"
PYTHON="$RUNTIME/launcher.venv/bin/python"
[ -x "$PYTHON" ] || python3 -m venv "$RUNTIME/launcher.venv"
STAMP="$(cksum < scripts/requirements-launcher.txt)"
if [ ! -f "$RUNTIME/launcher-requirements.stamp" ] || [ "$(cat "$RUNTIME/launcher-requirements.stamp")" != "$STAMP" ]; then
    "$PYTHON" -m pip install -r scripts/requirements-launcher.txt
    echo "$STAMP" > "$RUNTIME/launcher-requirements.stamp"
fi
exec "$PYTHON" scripts/docker_linux.py "$COMMAND" "${@:2}"
