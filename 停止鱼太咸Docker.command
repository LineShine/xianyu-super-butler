#!/bin/bash
cd "$(dirname "$0")" || exit 1
echo "鱼太咸 Docker：停止当前实例的应用、MySQL 和 Redis，保留数据、密钥和镜像。"
bash dev.sh stop
RESULT=$?
if [ "$RESULT" -eq 0 ]; then
    echo "鱼太咸已停止。Docker Desktop 和其他 Docker 项目保持原状。"
fi
read -r -p "按回车关闭窗口…"
exit "$RESULT"
