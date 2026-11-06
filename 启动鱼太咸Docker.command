#!/bin/bash
cd "$(dirname "$0")" || exit 1
echo "鱼太咸 Docker：自动启动 Docker Desktop、本机数据库和应用，无需在 Docker 中再点 Start。"
start_docker() {
    if ! command -v docker >/dev/null 2>&1 && [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
        export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
    fi
    if ! command -v docker >/dev/null 2>&1; then
        echo "未找到 Docker Desktop，请安装后重新运行本脚本。"
        return 1
    fi
    echo "正在启动 Docker Desktop 并等待引擎就绪……"
    if ! docker desktop start --timeout 180; then
        echo "Docker Desktop 未能就绪，请查看上方提示；原有数据保持不变。"
        return 1
    fi
    bash dev.sh switch docker
}
start_docker
RESULT=$?
if [ "$RESULT" -eq 0 ]; then
    echo "Docker 模式已就绪：http://127.0.0.1:9999"
    open "http://127.0.0.1:9999"
fi
read -r -p "按回车关闭窗口…"
exit "$RESULT"
