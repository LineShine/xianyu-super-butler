#!/bin/bash
cd "$(dirname "$0")" || exit 1
bash dev.sh stop
RESULT=$?
read -r -p "按回车关闭窗口…"
exit "$RESULT"
