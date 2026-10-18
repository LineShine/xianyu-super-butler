"""Resolve the repository settings without a Docker executable or daemon."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from dotenv import dotenv_values

VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^}]*))?\}")


def private_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, ensure_ascii=False, indent=2)
    temporary.replace(path)


def values(root: Path) -> dict[str, str]:
    # dotenv's interpolation is disabled so passwords containing ${...} remain
    # literal. Expansion applies to the checked-in template, never to secrets.
    return {k: v for k, v in dotenv_values(root / ".env", interpolate=False).items() if v is not None}


def expand(text: str, settings: dict[str, str]) -> str:
    def substitute(match):
        key, operator, default = match.groups()
        value = settings.get(key)
        if value is None or (operator == ":-" and value == ""):
            return default or ""
        return value
    return VARIABLE.sub(substitute, text)


def read_config(root: Path, *, native=True) -> dict:
    """Resolve native service settings. No container configuration is used."""
    settings = values(root)
    template = json.loads((root / "config/native.json").read_text())
    result = {"services": {}}
    ports = {"backend-web": ("BACKEND_WEB_PORT", 8189, 8089), "websocket": ("WEBSOCKET_PORT", 8190, 8090),
             "scheduler": ("SCHEDULER_PORT", 8191, 8091), "frontend": ("FRONTEND_PORT", 9100, 80),
             "mysql": ("LOCAL_MYSQL_PORT", 24306, 3306), "redis": ("LOCAL_REDIS_PORT", 27379, 6379)}
    for name, (key, default, target) in ports.items():
        port = settings.get(key, str(default))
        if not port.isdigit() or not 1024 <= int(port) <= 65535:
            raise RuntimeError(f"{key} 必须是 1024 至 65535 的端口。")
        environment = {k: expand(str(v), settings) for k, v in template.get("services", {}).get(name, {}).get("environment", {}).items()}
        volumes = []
        if name in ("mysql", "redis"):
            volumes = [{"type": "bind", "source": str(root / "yutaixian_data/native" / name), "target": "/var/lib/mysql" if name == "mysql" else "/data"}]
        elif name != "frontend":
            for directory in ("static", "backups", "browser_data", "logs"):
                volumes.append({"type": "bind", "source": str(root / "yutaixian_data" / directory), "target": "/app/" + directory})
        result["services"][name] = {"environment": environment, "ports": [{"host_ip": "127.0.0.1", "published": port, "target": target}], "volumes": volumes}
    for name in ("backend-web", "websocket", "scheduler"):
        env = result["services"][name]["environment"]
        env["FRONTEND_PUBLIC_URL"] = settings.get("FRONTEND_PUBLIC_URL", "http://127.0.0.1:" + result["services"]["frontend"]["ports"][0]["published"])
        env["BACKEND_WEB_PUBLIC_URL"] = settings.get("BACKEND_WEB_PUBLIC_URL", "http://127.0.0.1:" + result["services"]["backend-web"]["ports"][0]["published"])
        env["AUTO_START_WEBSOCKET"] = settings.get("AUTO_START_WEBSOCKET", "true")
        env["AUTO_START_CRAWL_JOBS"] = settings.get("AUTO_START_CRAWL_JOBS", "true")
    return result


def app_environment(root: Path, service: str) -> dict[str, str]:
    from local_dev import local_environment
    return local_environment(read_config(root), service)
