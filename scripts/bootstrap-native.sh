#!/bin/bash
# Bootstrap tools only. No databases, services or existing data are changed here.
set -e
WORK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$WORK_DIR/yutaixian_data/local_dev"
mkdir -p "$RUNTIME"
chmod 700 "$RUNTIME"

find_brew() {
    if command -v brew >/dev/null 2>&1; then command -v brew
    elif [ -x /opt/homebrew/bin/brew ]; then echo /opt/homebrew/bin/brew
    elif [ -x /usr/local/bin/brew ]; then echo /usr/local/bin/brew
    elif [ -x /home/linuxbrew/.linuxbrew/bin/brew ]; then echo /home/linuxbrew/.linuxbrew/bin/brew
    fi
}

BREW_BIN="$(find_brew)"
if [ -z "$BREW_BIN" ]; then
    echo "首次运行：准备 Homebrew，用于安装本机运行组件（可能需要系统密码）。"
    INSTALLER="$(mktemp)"
    trap 'rm -f "$INSTALLER"' EXIT
    curl --fail --location --proto '=https' --tlsv1.2 https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh -o "$INSTALLER"
    /bin/bash "$INSTALLER"
    BREW_BIN="$(find_brew)"
    [ -n "$BREW_BIN" ] || { echo "Homebrew 尚未准备完成，请按上方提示处理后重试。"; exit 1; }
fi
BREW_PREFIX="$($BREW_BIN --prefix)"
export PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:$PATH"
export HOMEBREW_NO_AUTO_UPDATE=1
if ! command -v uv >/dev/null 2>&1; then
    echo "首次运行：安装 Python 环境管理工具 uv。"
    if ! "$BREW_BIN" install uv; then
        echo "官方源安装未完成，使用清华镜像重试。"
        HOMEBREW_BOTTLE_DOMAIN="${HOMEBREW_BOTTLE_DOMAIN:-https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles}" "$BREW_BIN" install uv
    fi
fi
LAUNCHER_PYTHON="$RUNTIME/launcher.venv/bin/python"
if [ ! -x "$LAUNCHER_PYTHON" ]; then
    echo "首次运行：准备启动器的 Python 环境。"
    uv venv --python 3.12 "$RUNTIME/launcher.venv"
fi
STAMP="$(cksum < "$WORK_DIR/scripts/requirements-launcher.txt")"
if [ ! -f "$RUNTIME/launcher-requirements.stamp" ] || [ "$(cat "$RUNTIME/launcher-requirements.stamp")" != "$STAMP" ]; then
    uv pip install --python "$LAUNCHER_PYTHON" --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r "$WORK_DIR/scripts/requirements-launcher.txt"
    echo "$STAMP" > "$RUNTIME/launcher-requirements.stamp"
fi
exec "$LAUNCHER_PYTHON" "$WORK_DIR/scripts/local_stack.py" "$@"
