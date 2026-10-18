"""One-command local services with dependency preparation and owned processes.

Only the supervisor owns service process groups. Persisted PIDs are never
signalled unless their command line still contains this script and its token.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
import webbrowser

import local_dev

ROOT = Path(__file__).resolve().parents[1]
APPS = ("backend-web", "websocket", "scheduler", "frontend")


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, ensure_ascii=False)
    temporary.replace(path)


class LocalStack:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.runtime = self.root / "yutaixian_data/local_dev"
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.logs = self.runtime / "logs"
        self.logs.mkdir(exist_ok=True, mode=0o700)
        self.state_path = self.runtime / "state.json"
        self.browsers = self.runtime / "browsers"
        self.children: list[tuple[str, subprocess.Popen]] = []
        self.stopping = False

    @contextmanager
    def lock(self):
        with (self.runtime / "operation.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("另一个启动或停止操作正在进行，请稍后查看状态。")
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def log_file(self, name: str):
        path = self.logs / f"{name}.log"
        handle = path.open("a")
        os.chmod(path, 0o600)
        return handle

    def command(self, args: list[str], *, cwd=None, env=None, label="操作"):
        print(label, flush=True)
        with self.log_file("setup") as output:
            output.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {label}\n")
            output.flush()
            result = subprocess.run(args, cwd=cwd or self.root, env=env, stdout=output, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f"{label}失败。详情见 {self.logs / 'setup.log'}")

    def prerequisites(self):
        from native_infra import NativeInfrastructure
        NativeInfrastructure(self, local_dev.read_config(self.root)).prepare_tools()

    def ensure_config(self):
        path = self.root / ".env"
        if path.exists():
            return
        for service in ("mysql", "redis"):
            data = self.root / "yutaixian_data" / service / "data"
            if (data.exists() and any(data.iterdir())) or (self.root / "yutaixian_data/native/metadata.json").exists():
                raise RuntimeError("检测到已有数据但缺少根目录 .env。请恢复原配置，不能重新生成数据库密码。")
        values = {
            "MYSQL_ROOT_PASSWORD": secrets.token_hex(24), "MYSQL_DATABASE": "yutaixian_data",
            "MYSQL_USER": "yutaixian", "MYSQL_PASSWORD": secrets.token_hex(24),
            "REDIS_PASSWORD": secrets.token_hex(24), "REDIS_DB": "0",
            "INTERNAL_API_TOKEN": secrets.token_hex(32),
        }
        with path.open("x") as output:
            os.chmod(path, 0o600)
            output.write("# 本地统一启动入口首次生成，请保留此配置\n")
            output.writelines(f"{key}={value}\n" for key, value in values.items())
        print("首次使用：已生成独立随机密码并保存到根目录 .env。", flush=True)

    def fingerprint(self, *paths: Path) -> str:
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.read_bytes())
        digest.update(str(self.root).encode())
        return digest.hexdigest()

    def prepare(self, *, force=False):
        self.ensure_config()
        self.prerequisites()
        print(f"正在检查运行环境，首次下载可能需要几分钟。详细进度：{self.logs / 'setup.log'}", flush=True)
        manifest_path = self.runtime / "dependencies.json"
        manifest = read_json(manifest_path)
        uv = shutil.which("uv")
        env = os.environ.copy()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.browsers)
        for service in local_dev.SERVICES:
            directory = self.root / service
            python = directory / ".venv/bin/python"
            created = not python.exists()
            if created:
                if uv:
                    self.command([uv, "venv", "--python", "3.12", str(directory / ".venv")], label=f"准备 {service} 的 Python 环境")
                else:
                    base = next((shutil.which(p) for p in ("python3.12", "python3.11") if shutil.which(p)), None)
                    if not base:
                        raise RuntimeError("创建服务环境需要 uv 或 Python 3.12 / 3.11，请先安装其中一个。")
                    self.command([base, "-m", "venv", str(directory / ".venv")], label=f"准备 {service} 的 Python 环境")
            fingerprint = self.fingerprint(directory / "pyproject.toml", directory / "requirements.lock")
            fingerprint += subprocess.check_output([str(python), "-c", "import sys; print(sys.version)"], text=True)
            if force or created or manifest.get(service) != fingerprint:
                command = [uv, "pip", "install", "--python", str(python)] if uv else [str(python), "-m", "pip", "install"]
                self.command(command + ["--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple", "-c", str(directory / "requirements.lock"), "-e", str(directory)], env=env, label=f"安装 {service} 依赖（首次可能需要几分钟）")
            for browser in ("playwright", "patchright") if service != "scheduler" else ("playwright",):
                # Launch an empty, temporary browser: checking just the main
                # executable misses interrupted headless-shell downloads and
                # missing native Linux libraries.
                probe = f"from {browser}.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()"
                ready = subprocess.run([str(python), "-c", probe], env=env, capture_output=True).returncode == 0
                if not ready:
                    self.command([str(python), "-m", browser, "install", "chromium"], env=env, label=f"准备 {service} 的 {browser} 浏览器")
                    self.command([str(python), "-c", probe], env=env, label=f"验证 {service} 的 {browser} 浏览器")
            manifest[service] = fingerprint
            write_json(manifest_path, manifest)
        frontend = self.root / "frontend"
        fingerprint = self.fingerprint(frontend / "package.json", frontend / "package-lock.json")
        fingerprint += subprocess.check_output(["node", "-p", "process.versions.node"], text=True)
        if force or manifest.get("frontend") != fingerprint or not (frontend / "node_modules/vite/bin/vite.js").exists():
            self.command(["npm", "ci", "--include=dev"], cwd=frontend, label="安装前端依赖")
            manifest["frontend"] = fingerprint
            write_json(manifest_path, manifest)
        print("运行依赖已就绪。", flush=True)

    def ensure_infrastructure(self, config):
        from native_infra import NativeInfrastructure
        NativeInfrastructure(self, config)

    def owned_supervisor(self, state: dict) -> bool:
        pid, token = state.get("pid"), state.get("token")
        if not isinstance(pid, int) or pid <= 1 or not token:
            return False
        result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "args="], capture_output=True, text=True)
        return result.returncode == 0 and str(self.root / "scripts/local_stack.py") in result.stdout and f"_serve {token}" in result.stdout

    def ports(self, config):
        targets = {"mysql": 3306, "redis": 6379, **local_dev.SERVICES, "frontend": 80}
        return {name: int(local_dev.published_port(config["services"][name], target)) for name, target in targets.items() if name in config["services"]}

    def preflight_ports(self, config, previous):
        for name, port in self.ports(config).items():
            with socket.socket() as probe:
                # Match Uvicorn/Vite: a recently closed connection in TIME_WAIT
                # must not prevent an immediate retry or restart.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", port))
                except PermissionError:
                    raise RuntimeError(f"当前用户没有监听端口 {port} 的权限；请使用 1024 及以上端口，或先配置系统管理员授权。")
                except OSError:
                    raise RuntimeError(f"端口 {port} 被其他程序占用，请关闭该程序后重试。")

    def start(self, *, open_browser=True):
        state = read_json(self.state_path)
        if self.owned_supervisor(state):
            if state.get("phase") == "running":
                print(f"鱼太咸已经运行：{state['url']}", flush=True)
                if open_browser:
                    webbrowser.open(state["url"])
                return
            if state.get("phase") in ("starting", "stopping"):
                raise RuntimeError("鱼太咸正在启动或停止，请稍后查看状态。")
        migration = read_json(self.runtime / "data-migration.json")
        if migration and migration.get("phase") not in ("complete", "activated"):
            raise RuntimeError("数据迁移尚未通过校验，请重新运行 migrate 续跑；应用和自动任务未启动。")
        self.prepare()  # A dependency failure must never interrupt the old deployment.
        config = local_dev.read_config(self.root)
        previous = {}
        self.preflight_ports(config, previous)
        self.ensure_infrastructure(config)
        state = {"token": secrets.token_hex(16), "phase": "starting", "pid": None, "children": {},
                 "url": f"http://127.0.0.1:{self.ports(config)['frontend']}", "error": ""}
        process = None
        try:
            write_json(self.state_path, state)
            with self.log_file("supervisor") as output:
                process = subprocess.Popen([sys.executable, str(self.root / "scripts/local_stack.py"), "_serve", state["token"]], cwd=self.root, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
            print("正在启动本机 MySQL、Redis 和四个应用服务，请稍候……", flush=True)
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                current = read_json(self.state_path)
                if current.get("token") != state["token"]:
                    raise RuntimeError("启动状态已变化，请查看状态后重试。")
                if current.get("phase") == "running":
                    print(f"启动成功：{current['url']}\n服务在后台运行，关闭当前终端不会停止服务。", flush=True)
                    if open_browser:
                        webbrowser.open(current["url"])
                    return
                if current.get("phase") == "failed" or process.poll() is not None:
                    raise RuntimeError(current.get("error") or f"启动进程退出，详情见 {self.logs / 'supervisor.log'}")
                time.sleep(0.5)
            raise RuntimeError("启动等待超时，请查看服务日志。")
        except BaseException:
            if process is not None and process.poll() is None:
                # Use the owned Popen handle even if cancellation happened before
                # the supervisor had time to publish its PID.
                process.terminate()
                try:
                    process.wait(timeout=180)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("本地服务未能及时退出，请查看 supervisor.log。")
            current = read_json(self.state_path)
            if process is not None and current.get("children"):
                raise RuntimeError("管理进程异常退出，请查看 supervisor.log 并确认本地服务进程已停止。")
            raise

    def child_command(self, name, port):
        if name == "frontend":
            node = read_json(self.runtime / "native-tools.json").get("node") or shutil.which("node")
            return [node, str(self.root / "frontend/node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", str(port), "--strictPort"]
        return [sys.executable, str(self.root / "scripts/local_dev.py"), name]

    def start_databases(self, config):
        from native_infra import NativeInfrastructure
        NativeInfrastructure(self, config).start()

    def wait_ready(self, name, process, port, timeout=90):
        url = f"http://127.0.0.1:{port}" + ("/" if name == "frontend" else "/health")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stopping:
                raise RuntimeError("启动已取消。")
            if process.poll() is not None:
                raise RuntimeError(f"{name} 启动退出，详情见 {self.logs / (name + '.log')}")
            try:
                with opener.open(url, timeout=2) as response:
                    if response.status == 200:
                        if name == "frontend":
                            return
                        health = json.load(response)
                        if health.get("success") is True and health.get("data", {}).get("database") == "connected":
                            return
            except (OSError, TimeoutError, ValueError):
                pass
            time.sleep(0.3)
        raise RuntimeError(f"{name} 未能就绪，详情见 {self.logs / (name + '.log')}")

    def cleanup_children(self):
        for name, process in reversed(self.children):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=90 if name in ("mysql", "redis") else 20)
            except subprocess.TimeoutExpired:
                if name in ("mysql", "redis"):
                    raise RuntimeError(f"{name} 仍在保存数据，未强制结束，请查看对应日志。")
            # The main process may have exited before its browser/helper children.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)

    def serve(self, token):
        state = read_json(self.state_path)
        if state.get("token") != token or state.get("phase") != "starting":
            raise RuntimeError("启动请求已失效。")
        state["pid"] = os.getpid()
        write_json(self.state_path, state)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stopping", True))
        failure = ""
        try:
            config = local_dev.read_config(self.root)
            ports = self.ports(config)
            self.start_databases(config)
            state["children"] = {name: process.pid for name, process in self.children}
            write_json(self.state_path, state)
            env = os.environ.copy()
            env.update(PYTHONUNBUFFERED="1", PLAYWRIGHT_BROWSERS_PATH=str(self.browsers), LOCAL_BACKEND_URL=f"http://127.0.0.1:{ports['backend-web']}", LOCAL_STACK_MANAGED="1")
            for name in APPS:
                with self.log_file(name) as output:
                    process = subprocess.Popen(self.child_command(name, ports[name]), cwd=self.root / "frontend" if name == "frontend" else self.root, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
                self.children.append((name, process))
                state["children"][name] = process.pid
                write_json(self.state_path, state)
                self.wait_ready(name, process, ports[name])
            migration = read_json(self.runtime / "data-migration.json")
            if migration.get("phase") == "complete":
                migration["phase"] = "activated"
                write_json(self.runtime / "data-migration.json", migration)
            state["phase"] = "running"
            write_json(self.state_path, state)
            while not self.stopping:
                request_path = self.runtime / "restart-request.json"
                request = read_json(request_path)
                if request:
                    request_path.unlink(missing_ok=True)
                    name = request.get("service")
                    if request.get("token") == token and name in local_dev.SERVICES and time.time() - request.get("requested_at", 0) < 30:
                        old = next(child for key, child in self.children if key == name)
                        os.killpg(old.pid, signal.SIGTERM)
                        try:
                            old.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            pass
                        try:
                            os.killpg(old.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        old.wait(timeout=5)
                        self.children = [(key, child) for key, child in self.children if child is not old]
                        with self.log_file(name) as output:
                            child = subprocess.Popen(self.child_command(name, ports[name]), cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
                        self.children.append((name, child))
                        state["children"][name] = child.pid
                        write_json(self.state_path, state)
                        self.wait_ready(name, child, ports[name])
                for name, process in self.children:
                    if process.poll() is not None:
                        raise RuntimeError(f"{name} 意外退出，详情见 {self.logs / (name + '.log')}")
                time.sleep(0.5)
        except BaseException as exc:
            failure = str(exc) or type(exc).__name__
        finally:
            state["phase"] = "stopping"
            write_json(self.state_path, state)
            try:
                self.cleanup_children()
            except Exception as exc:
                failure = (failure + "；" if failure else "") + str(exc)
            remaining = {name: process.pid for name, process in self.children if process.poll() is None}
            state.update(phase="failed" if failure else "stopped", error=failure, children=remaining)
            write_json(self.state_path, state)

    def stop(self):
        state = read_json(self.state_path)
        if not self.owned_supervisor(state):
            print("没有本入口管理的运行进程；未停止其他程序。", flush=True)
            return
        os.kill(state["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            current = read_json(self.state_path)
            if current.get("phase") in ("stopped", "failed") and current.get("token") == state["token"]:
                if current.get("children"):
                    raise RuntimeError(current.get("error") or "部分进程仍在退出，请查看日志。")
                print("本机应用、MySQL 和 Redis 已停止；数据已保留。", flush=True)
                return
            if not self.owned_supervisor(state):
                raise RuntimeError("管理进程已经退出，请查看日志并确认服务端口已释放。")
            time.sleep(0.3)
        raise RuntimeError("停止等待超时，请查看 supervisor.log；未按过期 PID 强制终止程序。")

    def status(self):
        state = read_json(self.state_path)
        live = self.owned_supervisor(state)
        labels = {"starting": "启动中", "running": "运行中", "stopping": "停止中", "stopped": "已停止", "failed": "启动或运行失败"}
        print("方式五：" + (labels.get(state.get("phase"), "未启动") if live else "未运行"))
        if live and state.get("phase") == "running":
            print(state["url"])
        if state.get("error"):
            print(state["error"])
        print(f"日志目录：{self.logs}")


def main():
    parser = argparse.ArgumentParser(description="方式五统一启动：自动准备本机 MySQL、Redis 和应用，无需 Docker")
    parser.add_argument("command", nargs="?", default="start", choices=("start", "stop", "restart", "status", "setup", "logs", "migrate", "_serve"))
    parser.add_argument("target", nargs="?", help="logs 的服务名，或内部启动标识")
    parser.add_argument("--no-open", action="store_true", help="启动成功后不自动打开浏览器")
    parser.add_argument("--from", dest="source", help="原项目目录（仅 migrate 使用）")
    parser.add_argument("--dry-run", action="store_true", help="只检查迁移清单，不停服务、不改数据")
    args = parser.parse_args()
    stack = LocalStack()
    try:
        if args.command == "_serve":
            stack.serve(args.target)
        elif args.command == "status":
            stack.status()
        elif args.command == "logs":
            target = args.target or "supervisor"
            if target not in (*APPS, "mysql", "redis", "migration", "setup", "supervisor"):
                raise RuntimeError("日志名称可选 backend-web、websocket、scheduler、frontend、mysql、redis、migration、setup、supervisor。")
            path = stack.logs / f"{target}.log"
            if not path.exists():
                raise RuntimeError("尚无该服务日志。")
            subprocess.run(["tail", "-n", "100", "-f", str(path)])
        else:
            with stack.lock():
                if args.command in ("stop", "restart"):
                    stack.stop()
                if args.command in ("start", "restart"):
                    stack.start(open_browser=not args.no_open)
                elif args.command == "migrate":
                    if not args.source:
                        raise RuntimeError("请通过 --from 指定原项目目录。")
                    from data_migration import run
                    run(stack, args.source, dry_run=args.dry_run)
                elif args.command == "setup":
                    state = read_json(stack.state_path)
                    if stack.owned_supervisor(state):
                        raise RuntimeError("方式五正在运行，请先停止再更新依赖。")
                    stack.prepare(force=True)
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"未完成：{exc}", file=sys.stderr)
        return 1
    except Exception:
        with stack.log_file("setup") as output:
            traceback.print_exc(file=output)
        print(f"未完成：数据库连接或准备过程出错，请查看 {stack.logs / 'setup.log'}。原数据和迁移备份均保留。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
