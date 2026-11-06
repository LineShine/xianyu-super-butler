"""Explicit fresh-Linux container deployment; never adopts native database files."""
import argparse
import os
import sys
import uuid

from local_stack import LocalStack, read_json, write_json
from deployment import initialize_instance, instance_file
from docker_deploy import DockerDeployment
from native_config import private_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "build", "start", "stop", "restart", "status", "logs", "update"))
    parser.add_argument("service", nargs="?")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise RuntimeError("完整容器模式只用于全新 Linux；当前 Mac 使用 bash dev.sh switch docker")
    stack = LocalStack()
    with stack.lock():
        instance = read_json(instance_file(stack.root))
        if (stack.root / "yutaixian_data/native/metadata.json").exists() or (instance and instance["database_provider"] != "docker"):
            raise RuntimeError("发现本机数据库实例，禁止切换为另一套容器数据库")
        data = stack.root / "yutaixian_data/docker"
        if not instance and any((data / name).exists() and any((data / name).iterdir()) for name in ("mysql", "redis")):
            raise RuntimeError("已有容器数据但实例配置缺失；请恢复实例配置及原密钥")
        stack.ensure_config()
        instance = initialize_instance(stack.root, provider="docker")
        deployment = DockerDeployment(stack)
        deployment.daemon()
        if args.command in ("prepare", "build", "start", "restart", "update"):
            deployment.prepare_build()
        if args.command in ("prepare", "build"):
            print("镜像和实例配置已准备；尚未启动服务。")
        elif args.command == "status":
            deployment.status()
        elif args.command == "logs":
            import subprocess
            from local_stack import APPS
            if args.service and args.service not in (*APPS, "mysql", "redis"):
                raise RuntimeError("未知服务名")
            subprocess.run(["docker", "compose", "-f", str(deployment.path), "logs", "-f", "--tail", "100", *([args.service] if args.service else [])])
        elif args.command == "stop":
            deployment.stop_apps()
            deployment.compose(["stop", "--timeout", "180", "mysql", "redis"], label="停止容器基础服务并保留数据")
        else:
            deployment.check_project_ownership(instance)
            if args.command == "start" and deployment.healthy():
                print("容器应用已运行；未重复启动。")
                return
            if deployment.path.exists():
                deployment.stop_apps()
            (stack.runtime / "activation.json").unlink(missing_ok=True)
            runtime = {"mode": "docker", "runtime_id": uuid.uuid4().hex, "phase": "starting"}
            write_json(stack.runtime / "application.json", runtime)
            private_json(deployment.path, deployment.config(instance, runtime, full=True))
            deployment.compose(["up", "-d", "--wait", "--wait-timeout", "240", "mysql", "redis"], label="启动持久化 MySQL / Redis")
            deployment.compose(["run", "--rm", "--no-deps", "backend-web", "python", "/app/preflight.py", "--initialize"], label="检查实例并初始化凭据版本")
            try:
                deployment.start_apps(instance, runtime, full=True)
                write_json(stack.runtime / "activation.json", {"runtime_id": runtime["runtime_id"]})
                runtime["phase"] = "running"
            except BaseException:
                deployment.stop_apps()
                runtime["phase"] = "failed"
                raise
            finally:
                write_json(stack.runtime / "application.json", runtime)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"部署未完成：{exc}", file=sys.stderr)
        raise SystemExit(1)
