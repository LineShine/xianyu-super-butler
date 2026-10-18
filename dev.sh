#!/bin/bash
# 方式五统一入口。macOS / Linux；不需要手动激活虚拟环境。
set -e
WORK_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORK_DIR"
exec bash "$WORK_DIR/scripts/bootstrap-native.sh" "$@"
