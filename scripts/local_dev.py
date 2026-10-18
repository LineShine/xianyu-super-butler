"""Run local source using managed native MySQL/Redis, without Docker."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {"backend-web": 8089, "websocket": 8090, "scheduler": 8091}


def read_config(root: Path = ROOT) -> dict:
    from native_config import read_config as native_config
    return native_config(root)


def published_port(service: dict, target: int, *, loopback: bool = False) -> str:
    ports = [p for p in service.get("ports", []) if int(p["target"]) == target]
    if loopback:
        ports = [p for p in ports if p.get("host_ip") == "127.0.0.1"]
    if len(ports) != 1 or not str(ports[0].get("published", "")).isdigit():
        raise RuntimeError(f"端口 {target} 缺少明确的宿主机端口映射。")
    return str(ports[0]["published"])


def bind_source(service: dict, target: str) -> str:
    for mount in service.get("volumes", []):
        if mount["target"] == target and mount["type"] == "bind":
            return mount["source"]
    raise RuntimeError(f"共享目录 {target} 必须使用现有 bind 数据目录。")


def local_environment(config: dict, service: str) -> dict[str, str]:
    services = config["services"]
    selected = services[service]
    env = {k: str(v) for k, v in selected["environment"].items() if v is not None}
    env.update(
        ENVIRONMENT="development", HOST="127.0.0.1",
        MYSQL_HOST="127.0.0.1", MYSQL_PORT=published_port(services["mysql"], 3306, loopback=True),
        REDIS_HOST="127.0.0.1", REDIS_PORT=published_port(services["redis"], 6379, loopback=True),
        STATIC_DIR=bind_source(selected, "/app/static"),
    )
    if "BACKUP_DIR" in env:
        env["BACKUP_DIR"] = bind_source(selected, "/app/backups")
    for name, target in SERVICES.items():
        port = published_port(services[name], target)
        key = name.upper().replace("-", "_")
        env[f"{key}_SERVICE_URL"] = f"http://127.0.0.1:{port}"
        if name == service:
            env[f"{key}_PORT"] = port
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="使用统一入口准备的本机 MySQL / Redis 运行源码")
    parser.add_argument("service", nargs="?", choices=SERVICES)
    parser.add_argument("--check", action="store_true", help="仅校验共享数据目录和本机端口，不启动服务")
    parser.add_argument("--python", help="服务虚拟环境中的 Python 路径；默认使用 <服务>/.venv")
    args = parser.parse_args()
    if not args.service and not args.check:
        parser.error("请指定服务，或使用 --check")
    try:
        config = read_config()
        service = args.service or "backend-web"
        env = local_environment(config, service)
        from native_infra import check_native
        check_native(config)
        print(f"MySQL: {env['MYSQL_HOST']}:{env['MYSQL_PORT']}/{env['MYSQL_DATABASE']}", flush=True)
        print(f"Redis: {env['REDIS_HOST']}:{env['REDIS_PORT']} DB {env['REDIS_DB']}", flush=True)
        if args.check:
            print("本机 MySQL / Redis 实例身份及连接校验通过。")
            return 0
        executable = Path(args.python).expanduser().absolute() if args.python else ROOT / service / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not executable.is_file():
            raise RuntimeError(f"请先创建 {service}/.venv 并安装该服务依赖，或用 --python 指定已有环境。")
        listen_port = int(env[f"{service.upper().replace('-', '_')}_PORT"])
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", listen_port))
        process_env = os.environ.copy()
        process_env.update(env)
        process_env["PYTHONPATH"] = os.pathsep.join((str(ROOT / service), str(ROOT)))
        # Browser profiles use cwd/browser_data; share Docker's existing profiles.
        workdir = Path(bind_source(config["services"]["websocket"], "/app/browser_data")).parent
        workdir.mkdir(parents=True, exist_ok=True)
        os.chdir(workdir)
        os.execve(str(executable), [str(executable), str(ROOT / service / "main.py")], process_env)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"无法启动：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
