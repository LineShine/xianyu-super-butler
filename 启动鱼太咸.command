#!/bin/bash
cd "$(dirname "$0")" || exit 1
echo "鱼太咸：未运行时直接启动，已运行时自动重启。"
bash dev.sh switch native --restart
RESULT=$?
read -r -p "按回车关闭窗口…"
exit "$RESULT"
