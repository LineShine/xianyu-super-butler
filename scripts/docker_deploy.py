"""Source-built applications. Mac shared DB and fresh Linux full are explicit."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from local_stack import APPS, read_json, write_json
from native_config import read_config, values, private_json


class DockerDeployment:
    def __init__(self, stack):
        self.stack, self.root = stack, stack.root
        self.path = stack.runtime / "compose.json"

    def daemon(self):
        try:
            subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            subprocess.run(["docker", "compose", "version"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("Docker 引擎或 Compose 不可用，请启动 Docker Desktop；原模式未停止") from None

    def fingerprint(self):
        digest = hashlib.sha256()
        excluded = {".git", ".venv", "venv", "node_modules", "__pycache__", "logs", "tests", "browser_data", "browser_runtime", "backups", "private", "dist", "uploads"}
        sources = []
        for directory in (*APPS, "common", "docker"):
            for parent, directories, files in os.walk(self.root / directory):
                directories[:] = sorted(name for name in directories if name not in excluded and not name.startswith(".") and not name.endswith(".egg-info") and name != "build")
                for name in files:
                    if name.startswith(".env") or name.endswith((".pyc", ".log")) or name == "keyring.json":
                        continue
                    path = Path(parent) / name
                    if path.is_file() and not path.is_symlink():
                        sources.append(path)
        for path in sorted(sources):
            digest.update(str(path.relative_to(self.root)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()[:20]

    def prepare_build(self):
        self.daemon()
        fingerprint = self.fingerprint()
        built = read_json(self.stack.runtime / "images.json")
        for service in APPS:
            tag = f"yutaixian-{service}:secure-{fingerprint}"
            exists = subprocess.run(["docker", "image", "inspect", tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            if not exists:
                command = ["docker", "build", "-t", tag, "-f", "docker/Dockerfile.frontend" if service == "frontend" else "docker/Dockerfile.app"]
                if service != "frontend":
                    command += ["--build-arg", f"SERVICE={service}"]
                self.stack.command(command + ["."], label=f"从当前源码构建 {service} 镜像")
            built[service] = tag
        built["source_fingerprint"] = fingerprint
        write_json(self.stack.runtime / "images.json", built)

    def config(self, instance, runtime, *, full=False, preflight=False):
        source = read_config(self.root)
        images = read_json(self.stack.runtime / "images.json")
        if images.get("source_fingerprint") != self.fingerprint():
            raise RuntimeError("镜像与当前源码不一致，请先构建；不能用旧程序读取新密文")
        services = {}
        metadata = read_json(self.root / "yutaixian_data/native/metadata.json")
        data = self.root / "yutaixian_data"
        for directory in ("static", "backups", "private", "logs", "browser_runtime/linux"):
            (data / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        for service in APPS:
            published = source["services"][service]["ports"][0]
            config = {"image": images[service], "init": True, "restart": "no", "stop_grace_period": "180s",
                      "labels": {"org.yutaixian.instance": instance["instance_id"], "org.yutaixian.credential-version": "1"},
                      "ports": [f"127.0.0.1:{published['published']}:{published['target']}"]}
            if service == "frontend":
                config["depends_on"] = {"backend-web": {"condition": "service_healthy"}}
            else:
                env = dict(source["services"][service]["environment"])
                env.update(HOST="0.0.0.0", MYSQL_HOST="mysql" if full else "host.docker.internal",
                           MYSQL_PORT="3306" if full else str(source["services"]["mysql"]["ports"][0]["published"]),
                           REDIS_HOST="redis" if full else "host.docker.internal",
                           REDIS_PORT="6379" if full else str(source["services"]["redis"]["ports"][0]["published"]),
                           YTX_INSTANCE_ID=instance["instance_id"], YTX_KEYRING_FILE="/run/yutaixian/keyring.json",
                           YTX_RUNTIME_ID=runtime["runtime_id"], YTX_BROWSER_ROOT="/app/data/browser_data",
                           YTX_PRIVATE_DIR="/app/private", YTX_ACTIVATION_FILE="/app/control/activation.json")
                if not full:
                    env.update(YTX_EXPECTED_MYSQL_UUID=metadata["server_uuid"], YTX_EXPECTED_REDIS_RUN_ID=metadata["redis_run_id"])
                if preflight:
                    env["YTX_ALLOW_LEGACY_PREFLIGHT"] = "1"
                # Compose interpolation must never reinterpret a password containing $.
                config["environment"] = {key: str(value).replace("$", "$$") for key, value in env.items()}
                config["volumes"] = [
                    {"type": "bind", "source": instance["keyring_file"], "target": "/run/yutaixian/keyring.json", "read_only": True},
                    {"type": "bind", "source": str(self.stack.runtime), "target": "/app/control", "read_only": True},
                    *[{"type": "bind", "source": str(data / directory), "target": target} for directory, target in
                      (("static", "/app/static"), ("backups", "/app/backups"), ("private", "/app/private"),
                       ("browser_runtime/linux", "/app/data/browser_data"), (f"logs/{service}", "/app/service/logs"))]]
                (data / "logs" / service).mkdir(parents=True, exist_ok=True, mode=0o700)
                config["shm_size"] = "512m"
                config["healthcheck"] = {"test": ["CMD", "python", "-c", f"import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:{published['target']}/health',timeout=3)); assert r.get('success') and r.get('data',{{}}).get('database')=='connected'"],
                                          "interval": "5s", "timeout": "5s", "retries": 24, "start_period": "30s"}
                if service != "backend-web":
                    config["depends_on"] = {"backend-web": {"condition": "service_healthy"}}
            services[service] = config
        if full:
            settings = values(self.root)
            services["mysql"] = {"image": "mysql:8.4", "restart": "unless-stopped",
                "command": ["--max-connections=4000", "--character-set-server=utf8mb4", "--collation-server=utf8mb4_unicode_ci", "--default-time-zone=+08:00"],
                "environment": {key: settings[key].replace("$", "$$") for key in ("MYSQL_ROOT_PASSWORD", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD")},
                "volumes": [{"type": "bind", "source": str(data / "docker/mysql"), "target": "/var/lib/mysql"}],
                "healthcheck": {"test": ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 --silent"], "interval": "5s", "timeout": "5s", "retries": 40}, "stop_grace_period": "180s"}
            redis_conf = data / "docker/redis.conf"
            redis_conf.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            redis_conf.write_text('bind 0.0.0.0\nprotected-mode yes\nappendonly yes\nappendfsync everysec\ndir /data\nrequirepass ' + json.dumps(settings["REDIS_PASSWORD"]) + '\n')
            os.chmod(redis_conf, 0o644)  # Containing host directory is 0700; container Redis runs unprivileged.
            for name in ("mysql", "redis"):
                (data / "docker" / name).mkdir(parents=True, exist_ok=True, mode=0o700)
            services["redis"] = {"image": "redis:7.4-alpine", "restart": "unless-stopped",
                "environment": {"REDIS_PASSWORD": settings["REDIS_PASSWORD"].replace("$", "$$")},
                "healthcheck": {"test": ["CMD-SHELL", 'REDISCLI_AUTH="$${REDIS_PASSWORD}" redis-cli ping | grep -q PONG'], "interval": "5s", "timeout": "5s", "retries": 30},
                "command": ["redis-server", "/usr/local/etc/redis.conf"],
                "volumes": [{"type": "bind", "source": str(data / "docker/redis"), "target": "/data"},
                            {"type": "bind", "source": str(redis_conf), "target": "/usr/local/etc/redis.conf", "read_only": True}],
                "stop_grace_period": "180s"}
        return {"name": "ytx-" + instance["instance_id"][:12], "services": services}

    def compose(self, args, *, path=None, label="Docker 操作"):
        self.stack.command(["docker", "compose", "-f", str(path or self.path), *args], label=label)

    def preflight_shared(self, instance):
        candidate = self.stack.runtime / "compose-preflight.json"
        private_json(candidate, self.config(instance, {"runtime_id": "preflight"}, preflight=True))
        self.compose(["run", "--rm", "--no-deps", "backend-web", "python", "/app/preflight.py"], path=candidate,
                     label="在真实容器中检查本机 MySQL、Redis、实例和密钥")

    def start_apps(self, instance, runtime, *, full=False):
        private_json(self.path, self.config(instance, runtime, full=full))
        self.compose(["up", "-d", "--wait", "--wait-timeout", "300", *APPS], label="启动并检查 Docker 应用健康状态")

    def stop_apps(self):
        if not self.path.exists():
            raise RuntimeError("缺少原 Docker 应用清单，请恢复后停止；未启动另一模式")
        self.daemon()
        # Send only SIGTERM; never force-kill a still-running task or remove volumes.
        if not self.running():
            return
        self.compose(["kill", "--signal", "SIGTERM", *APPS], label="请求 Docker 账号连接和任务正常退出")
        deadline = time.monotonic() + 180
        while self.running() and time.monotonic() < deadline:
            time.sleep(.5)
        if self.running():
            raise RuntimeError("Docker 应用仍在退出，未强制结束，已阻止切换")

    def containers(self):
        if not self.path.exists():
            return []
        result = subprocess.run(["docker", "compose", "-f", str(self.path), "ps", "-a", "--format", "json"], capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise RuntimeError("无法确认 Docker 容器状态")
        output = result.stdout.strip()
        if output.startswith("["):
            return json.loads(output)
        return [json.loads(line) for line in output.splitlines() if line]

    def running(self):
        return any(row.get("Service") in APPS and row.get("State") == "running" for row in self.containers())

    def healthy(self):
        return {row.get("Service") for row in self.containers() if row.get("State") == "running" and row.get("Health") == "healthy"} >= set(APPS)

    def status(self):
        self.daemon()
        subprocess.run(["docker", "compose", "-f", str(self.path), "ps"])
