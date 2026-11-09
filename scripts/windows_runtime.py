"""Windows native launcher. No imports from the POSIX process supervisors."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser

from windows_setup import Setup, SERVICES, BrowserInstallRequired, read_json, write_json
from windows_process import Event, Job, Mutex, identity, matches, name_for

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))


@contextmanager
def environment(values):
    old = os.environ.copy()
    try:
        os.environ.update(values)
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


class Runtime:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.data = self.root / "yutaixian_data"
        self.runtime = self.data / "local_dev"
        self.setup = Setup(self.root)
        self.children = {}
        self.events = {}
        self.job = self.stop_event = None
        self.state = None
        self.group = None

    def ensure_directories(self):
        from common.security.windows_acl import protect
        if self.data.is_symlink() or self.data.is_junction():
            raise RuntimeError("数据目录不能使用符号链接或目录联接")
        self.data.mkdir(exist_ok=True)
        protect(self.data)
        for name in ("local_dev/logs", "native", "static", "backups", "private/face", "browser_runtime/win32"):
            (self.data / name).mkdir(parents=True, exist_ok=True)

    def config(self):
        from native_config import read_config
        config = read_config(self.root)
        ports = [entry["ports"][0]["published"] for entry in config["services"].values()]
        if len(ports) != len(set(ports)):
            raise RuntimeError("六个本机服务必须使用不同端口")
        env = config["services"]["backend-web"]["environment"]
        if not env["MYSQL_PASSWORD"] or not env["REDIS_PASSWORD"] or not env["INTERNAL_API_TOKEN"]:
            raise RuntimeError(".env 缺少数据库密码、Redis 密码或内部服务令牌，请恢复原配置")
        if env["MYSQL_USER"].lower() == "root":
            raise RuntimeError("应用数据库账户不能使用 root")
        if not str(env["REDIS_DB"]).isdigit() or not 0 <= int(env["REDIS_DB"]) < 16:
            raise RuntimeError("REDIS_DB 必须在 0 至 15 之间")
        return config

    def ensure_config(self):
        from common.security.windows_acl import protect
        env_file = self.root / ".env"
        if env_file.exists():
            protect(env_file)
            self.config()
            return
        existing = self.data / "instance.json"
        native = self.data / "native"
        if existing.exists() or (native.exists() and any(native.iterdir())):
            raise RuntimeError("已有数据但 .env 缺失，请恢复原配置；未生成替代密码")
        values = {"MYSQL_ROOT_PASSWORD": secrets.token_hex(24), "MYSQL_DATABASE": "yutaixian_data",
                  "MYSQL_USER": "yutaixian", "MYSQL_PASSWORD": secrets.token_hex(24),
                  "REDIS_PASSWORD": secrets.token_hex(24), "REDIS_DB": "0", "INTERNAL_API_TOKEN": secrets.token_hex(32)}
        with env_file.open("x", encoding="utf-8") as output:
            protect(env_file)
            output.write("# 本机启动器首次生成，请保留并单独备份实例密钥。\n")
            output.writelines(f"{key}={value}\n" for key, value in values.items())
        self.config()

    def path_for(self, group):
        return self.runtime / ("infrastructure.json" if group == "infra" else "state.json")

    def owned(self, group, state=None):
        state = state if state is not None else read_json(self.path_for(group))
        if (state.get("platform") != "win32" or state.get("group") != group
                or state.get("root") != str(self.root) or not state.get("token") or not matches(state.get("owner"))):
            return False
        try:
            with Event(name_for(self.root, group + "-stop", state["token"]), existing=True):
                return True
        except OSError:
            return False

    def preflight_ports(self, group):
        config = self.config()
        names = ("mysql", "redis") if group == "infra" else (*SERVICES, "frontend")
        for name in names:
            port = int(config["services"][name]["ports"][0]["published"])
            with socket.socket() as probe:
                # Windows SO_REUSEADDR allows stealing a live listener; never use it.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    raise RuntimeError(f"{name} 端口 {port} 被占用或被系统保留；请调整 .env，未停止其他程序") from None

    def check_existing_instance(self):
        from common.security.vault import read_keyring
        instance = read_json(self.data / "instance.json")
        if not instance:
            return None
        config = self.config()["services"]["backend-web"]["environment"]
        if (instance.get("database_provider") != "native" or instance.get("platform") != "win32"
                or instance.get("database_name") != config["MYSQL_DATABASE"] or instance.get("redis_db") != config["REDIS_DB"]):
            raise RuntimeError("现有实例的平台、数据库或 Redis DB 不匹配，拒绝启动替代实例")
        ring = read_keyring(Path(instance["keyring_file"]))
        if ring["instance_id"] != instance["instance_id"]:
            raise RuntimeError("密钥与实例不匹配")
        return instance

    def initialize_instance(self):
        from common.security.vault import initialize_keyring
        from windows_infra import mysql_connection
        instance = self.check_existing_instance()
        if instance:
            return instance
        with mysql_connection(self.config()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SHOW TABLES LIKE 'ytx_instance'")
                if cursor.fetchone():
                    raise RuntimeError("数据库已有加密实例，请恢复 instance.json 和原密钥")
                cursor.execute("SHOW TABLES LIKE 'ytx_accounts'")
                if cursor.fetchone():
                    cursor.execute("SELECT COUNT(*) FROM ytx_accounts WHERE cookie LIKE 'ytxenc:%%' OR login_password LIKE 'ytxenc:%%'")
                    if cursor.fetchone()[0]:
                        raise RuntimeError("数据库已有密文，不能创建替代密钥")
        instance_id = uuid.uuid4().hex
        key = Path(os.environ["LOCALAPPDATA"]) / "Yutaixian/secrets" / instance_id / "keyring.json"
        initialize_keyring(key, instance_id)
        config = self.config()["services"]["backend-web"]["environment"]
        instance = {"version": 1, "platform": "win32", "instance_id": instance_id, "database_provider": "native",
                    "application_mode": "native", "keyring_file": str(key), "database_name": config["MYSQL_DATABASE"],
                    "redis_db": str(config["REDIS_DB"])}
        write_json(self.data / "instance.json", instance)
        return instance

    def app_environment(self, name, runtime_id):
        from local_dev import local_environment
        instance = self.check_existing_instance()
        if not instance:
            raise RuntimeError("实例尚未初始化")
        env = os.environ.copy()
        env.update(local_environment(self.config(), name))
        # The Windows C runtime treats IANA TZ strings as UTC. Keep the
        # project's Beijing wall clock without changing the machine time zone.
        env["TZ"] = "CST-8"
        env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", LOCAL_STACK_MANAGED="1",
                   PLAYWRIGHT_BROWSERS_PATH=str(self.setup.browsers),
                   YTX_INSTANCE_ID=instance["instance_id"], YTX_KEYRING_FILE=instance["keyring_file"],
                   YTX_RUNTIME_ID=runtime_id, YTX_ACTIVATION_FILE=str(self.runtime / "activation.json"),
                   YTX_BROWSER_ROOT=str(self.data / "browser_runtime/win32"), YTX_PRIVATE_DIR=str(self.data / "private"),
                   YTX_LOG_DIR=str(self.data / "logs/native" / name),
                   PYTHONPATH=os.pathsep.join((str(self.root / name), str(self.root))))
        return env

    def assert_roles_free(self):
        from common.security.runtime import ROLES, lock_name
        from windows_infra import mysql_connection
        instance = self.check_existing_instance()
        with mysql_connection(self.config()) as connection:
            with connection.cursor() as cursor:
                for name in ROLES:
                    cursor.execute("SELECT IS_USED_LOCK(%s)", (lock_name(instance["instance_id"], name),))
                    if cursor.fetchone()[0] is not None:
                        raise RuntimeError("仍有应用持有数据库所有权锁，拒绝竞争启动")

    def migrate(self):
        self.assert_roles_free()
        from common.security.runtime import connect, migrate
        with environment(self.app_environment("backend-web", "preflight")):
            with connect() as connection:
                migrate(connection)
        legacy = self.data / "static/uploads/face"
        if legacy.is_dir():
            for source in legacy.glob("*.jpg"):
                target = self.data / "private/face" / source.name
                if source.is_file() and not source.is_symlink() and not target.exists():
                    source.rename(target)

    def validate_database(self):
        from windows_infra import check_native
        from common.security.runtime import connect, validate
        check_native(self.root, self.config())
        with environment(self.app_environment("backend-web", "preflight")):
            with connect() as connection:
                validate(connection)

    def start_group(self, group):
        state = read_json(self.path_for(group))
        if self.owned(group, state):
            if state.get("phase") != "running":
                raise RuntimeError("服务正在启动或停止，请稍后查看 Status")
            return False
        if any(matches(record) for record in state.get("children", {}).values()):
            raise RuntimeError("管理器已退出但仍有进程身份匹配，拒绝接管；请检查日志")
        self.preflight_ports(group)
        token = uuid.uuid4().hex
        state = {"platform": "win32", "root": str(self.root), "group": group, "token": token,
                 "phase": "starting", "children": {}, "error": "", "owner": None}
        if group == "apps":
            state["runtime_id"] = uuid.uuid4().hex
            write_json(self.runtime / "application.json", {"mode": "native", "runtime_id": state["runtime_id"], "phase": "starting"})
        write_json(self.path_for(group), state)
        with (self.setup.logs / (group + "-supervisor.log")).open("a", encoding="utf-8") as output:
            child = subprocess.Popen([sys.executable, str(SOURCE_ROOT / "scripts/windows_runtime.py"), "_supervise",
                                      "--root", str(self.root), "--group", group, "--token", token],
                                     cwd=self.root, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                     creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
                                     env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
        try:
            deadline = time.monotonic() + 420
            while time.monotonic() < deadline:
                current = read_json(self.path_for(group))
                if current.get("token") != token:
                    raise RuntimeError("启动请求已被替换")
                if current.get("phase") == "running" and self.owned(group, current):
                    return True
                if current.get("phase") == "failed" or child.poll() is not None:
                    raise RuntimeError(current.get("error") or f"{group} 管理器退出，请查看日志")
                time.sleep(.2)
            raise RuntimeError("服务启动等待超时")
        except BaseException:
            self.stop_group(group)
            raise

    def deactivate(self):
        (self.runtime / "activation.json").unlink(missing_ok=True)

    def stop_group(self, group):
        state = read_json(self.path_for(group))
        if not state:
            return
        if group == "apps":
            self.deactivate()
        if not self.owned(group, state):
            if any(matches(record) for record in state.get("children", {}).values()):
                raise RuntimeError("管理器归属无法确认；未对残留进程按 PID 强杀")
            return
        with Event(name_for(self.root, group + "-stop", state["token"]), existing=True) as event:
            event.set()
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            current = read_json(self.path_for(group))
            if current.get("token") != state["token"]:
                raise RuntimeError("停止期间运行代次发生变化")
            if not matches(state["owner"]):
                if any(matches(record) for record in current.get("children", {}).values()):
                    raise RuntimeError("管理器退出后仍有子进程，请检查日志")
                if current.get("phase") not in ("stopped", "failed"):
                    current.update(phase="stopped", children={})
                    write_json(self.path_for(group), current)
                return
            time.sleep(.2)
        raise RuntimeError("停止超时，未使用过期 PID 终止进程")

    def start(self, *, restart=False, browser=True):
        state = read_json(self.path_for("apps"))
        if self.owned("apps", state) and state.get("phase") == "running" and not restart:
            self.validate_database()
            self.check_health()
            print("鱼太咸已经运行：" + self.url(), flush=True)
            if browser:
                webbrowser.open(self.url())
            return
        self.ensure_directories()
        if self.owned("apps"):
            # Never replace libraries under a running service. Restart the prepared
            # environment; use Stop + Prepare to update lockfiles/dependencies.
            self.check_existing_instance()
        else:
            self.setup.prepare()
        self.ensure_config()
        self.check_existing_instance()  # Missing keys fail before databases start.
        migration = read_json(self.runtime / "data-migration.json")
        if migration and migration.get("phase") not in ("complete", "activated"):
            raise RuntimeError("数据迁移尚未完成，未启用自动任务")
        self.preflight_ports("apps") if not self.owned("apps") else None
        started_infra = False
        try:
            started_infra = self.start_group("infra")
            from windows_infra import check_native
            check_native(self.root, self.config())
            self.initialize_instance()
            if self.owned("apps"):
                self.validate_database()
                self.stop_group("apps")
            self.migrate()  # Initialize credential version before backend creates tables.
            self.start_group("apps")
            self.check_health()
        except BaseException:
            self.stop_group("apps")
            if started_infra:
                self.stop_group("infra")
            raise
        print("鱼太咸已启动：" + self.url(), flush=True)
        if browser:
            webbrowser.open(self.url())

    def url(self):
        port = self.config()["services"]["frontend"]["ports"][0]["published"]
        return f"http://127.0.0.1:{port}"

    def health(self, name):
        port = self.config()["services"][name]["ports"][0]["published"]
        url = f"http://127.0.0.1:{port}" + ("/" if name == "frontend" else "/health")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=3) as response:
            if response.status != 200:
                return False
            if name == "frontend":
                return True
            result = json.load(response)
            return result.get("success") is True and result.get("data", {}).get("database") == "connected"

    def check_health(self):
        for name in (*SERVICES, "frontend"):
            if not self.health(name):
                raise RuntimeError(name + " 健康检查失败")

    def status(self):
        labels = {"running": "运行中", "starting": "启动中", "stopping": "停止中", "stopped": "已停止", "failed": "失败"}
        for group, label in (("infra", "MySQL / Redis"), ("apps", "应用")):
            state = read_json(self.path_for(group))
            live = self.owned(group, state)
            print(label + "：" + (labels.get(state.get("phase"), "未知") if live else "未运行"))
            if state.get("error"):
                print("最近错误：" + state["error"])
        print("日志目录：" + str(self.setup.logs))

    def save(self):
        self.state["children"] = {name: child.identity for name, child in self.children.items() if child.poll() is None}
        write_json(self.path_for(self.group), self.state)

    def spawn(self, name, args, *, cwd=None, env=None):
        with (self.setup.logs / (name + ".log")).open("a", encoding="utf-8") as output:
            child = self.job.spawn(args, cwd=cwd or self.data, env=env or os.environ.copy(), output=output)
        self.children[name] = child
        self.save()
        return child

    def forget(self, name):
        self.children.pop(name).close()
        self.save()

    def require_alive(self, child, name):
        if self.stop_event.wait():
            raise RuntimeError("启动已取消")
        if child.poll() is not None:
            raise RuntimeError(f"{name} 意外退出，详情见 {self.setup.logs / (name + '.log')}")

    def wait_exit(self, child, timeout):
        deadline = time.monotonic() + timeout
        while child.poll() is None:
            if self.stop_event.wait(200):
                raise RuntimeError("启动已取消")
            if time.monotonic() >= deadline:
                raise RuntimeError("初始化等待超时")

    def spawn_app(self, name):
        ports = self.config()["services"]
        if name == "frontend":
            tools = read_json(self.setup.tools_file)
            env = os.environ.copy()
            env["LOCAL_BACKEND_URL"] = "http://127.0.0.1:" + ports["backend-web"]["ports"][0]["published"]
            args = [tools["node"], self.root / "frontend/node_modules/vite/bin/vite.js", "preview", "--host", "127.0.0.1",
                    "--port", ports[name]["ports"][0]["published"], "--strictPort"]
            child = self.spawn(name, args, cwd=self.root / "frontend", env=env)
        else:
            event = Event(name_for(self.root, name + "-stop", uuid.uuid4().hex))
            self.events[name] = event
            env = self.app_environment(name, self.state["runtime_id"])
            env["YTX_STOP_EVENT"] = event.name
            tools = read_json(self.setup.tools_file)
            python = tools.get("service_python", {}).get(name, str(self.root / name / ".venv/Scripts/python.exe"))
            child = self.spawn(name, [python, SOURCE_ROOT / "scripts/windows_service.py", "--root", self.root, "--service", name], env=env)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            self.require_alive(child, name)
            try:
                if self.health(name):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.3)
        raise RuntimeError(name + " 就绪超时，请查看日志")

    def stop_apps(self):
        self.deactivate()
        for event in self.events.values():
            event.set()
        deadline = time.monotonic() + 90
        while any(child.poll() is None for name, child in self.children.items() if name != "frontend"):
            if time.monotonic() >= deadline:
                raise RuntimeError("应用正常退出超时，本实例 Job 将回收剩余进程")
            time.sleep(.2)

    def restart_requested_service(self):
        request_file = self.runtime / "restart-request.json"
        request = read_json(request_file)
        if not request:
            return
        request_file.unlink(missing_ok=True)
        name = request.get("service")
        age = time.time() - request.get("requested_at", 0)
        if request.get("token") != self.state["token"] or name not in SERVICES or not 0 <= age < 30:
            return
        self.deactivate()
        self.events[name].set()
        self.children[name].wait(90)
        self.forget(name)
        self.events.pop(name).close()
        self.spawn_app(name)
        self.check_health()
        if not self.stop_event.wait():
            write_json(self.runtime / "activation.json", {"runtime_id": self.state["runtime_id"]})

    def supervise(self, group, token):
        self.group = group
        with Mutex(name_for(self.root, group + "-owner")):
            self.state = read_json(self.path_for(group))
            if self.state.get("token") != token or self.state.get("phase") != "starting":
                raise RuntimeError("启动请求已失效")
            self.children, self.events = {}, {}
            failure = ""
            infrastructure = None
            with Event(name_for(self.root, group + "-stop", token)) as stop, Job(name_for(self.root, group + "-job", token)) as job:
                self.stop_event, self.job = stop, job
                self.state["owner"] = identity(os.getpid())
                self.save()
                try:
                    if group == "infra":
                        from windows_infra import Infrastructure
                        infrastructure = Infrastructure(self)
                        infrastructure.start()
                    else:
                        self.validate_database()
                        self.deactivate()
                        for name in (*SERVICES, "frontend"):
                            self.spawn_app(name)
                        self.check_health()
                        if stop.wait():
                            raise RuntimeError("启动已取消")
                        write_json(self.runtime / "activation.json", {"runtime_id": self.state["runtime_id"]})
                        write_json(self.runtime / "application.json", {"mode": "native", "runtime_id": self.state["runtime_id"], "phase": "running"})
                    self.state["phase"] = "running"
                    self.save()
                    while not stop.wait(300):
                        if group == "apps":
                            self.restart_requested_service()
                        for name, child in self.children.items():
                            if child.poll() is not None:
                                raise RuntimeError(f"{name} 意外退出，详情见 {self.setup.logs / (name + '.log')}")
                except Exception as exc:
                    from common.security.redaction import redact
                    failure = redact(str(exc))
                    import traceback
                    print(redact(traceback.format_exc()), flush=True)
                finally:
                    self.state["phase"] = "stopping"
                    self.save()
                    try:
                        if group == "apps":
                            self.stop_apps()
                        elif infrastructure:
                            infrastructure.shutdown()
                    except Exception as exc:
                        failure = failure or str(exc)
                    job.terminate()
                    for child in self.children.values():
                        child.wait(15)
                        child.close()
                    self.children.clear()
                    for event in self.events.values():
                        event.close()
                    self.state.update(phase="failed" if failure else "stopped", error=failure)
                    self.save()
                    if group == "apps":
                        write_json(self.runtime / "application.json", {"mode": "native", "runtime_id": self.state["runtime_id"], "phase": self.state["phase"]})


def main():
    parser = argparse.ArgumentParser(description="鱼太咸 Windows 本机运行")
    parser.add_argument("action", choices=("Prepare", "Start", "Stop", "Restart", "Status", "StartInfrastructure",
                                          "StopInfrastructure", "StartApplication", "StopApplication", "_supervise"))
    parser.add_argument("--root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--group", choices=("infra", "apps"))
    parser.add_argument("--token")
    args = parser.parse_args()
    runtime = Runtime(args.root)
    try:
        if args.action == "_supervise":
            runtime.supervise(args.group, args.token)
            return 0
        with Mutex(name_for(runtime.root, "operation")):
            if args.action == "Status":
                runtime.status()
            elif args.action == "Prepare":
                if runtime.owned("apps") or runtime.owned("infra"):
                    raise RuntimeError("请先 Stop，再 Prepare 更新运行依赖")
                runtime.ensure_directories()
                runtime.setup.prepare(probe_browsers=True)
                runtime.ensure_config()
            elif args.action in ("Start", "Restart", "StartApplication"):
                runtime.start(restart=args.action == "Restart", browser=not args.no_browser)
            elif args.action in ("Stop", "StopApplication"):
                runtime.stop_group("apps")
                if args.action == "Stop":
                    runtime.stop_group("infra")
                print("服务已停止，数据与密钥已保留。")
            elif args.action == "StartInfrastructure":
                if runtime.owned("infra"):
                    from windows_infra import check_native
                    check_native(runtime.root, runtime.config())
                    return 0
                if runtime.owned("apps"):
                    raise RuntimeError("请先停止应用，再准备基础服务")
                runtime.ensure_directories()
                runtime.setup.prepare()
                runtime.ensure_config()
                runtime.check_existing_instance()
                runtime.start_group("infra")
            elif args.action == "StopInfrastructure":
                if runtime.owned("apps"):
                    raise RuntimeError("请先停止应用，再停止基础服务")
                runtime.stop_group("infra")
        return 0
    except BrowserInstallRequired as exc:
        print(str(exc), flush=True)
        return 86
    except Exception as exc:
        print("无法完成：" + str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
