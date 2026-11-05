"""Application-only mode switches; native database supervisor has its own lifetime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

from local_stack import LocalStack, read_json, write_json
from native_config import read_config, private_json
from native_infra import check_native, mysql_connection
import local_dev

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def instance_file(root):
    return root / "yutaixian_data/instance.json"


def instance_environment(root):
    instance = read_json(instance_file(root))
    if not instance:
        raise RuntimeError("缺少实例配置，请运行 bash dev.sh start")
    runtime = read_json(root / "yutaixian_data/local_dev/application.json")
    return {"YTX_INSTANCE_ID": instance["instance_id"],
            "YTX_KEYRING_FILE": instance["keyring_file"],
            "YTX_RUNTIME_ID": runtime.get("runtime_id", ""),
            "YTX_BROWSER_ROOT": str(root / "yutaixian_data/browser_runtime" / sys.platform),
            "YTX_PRIVATE_DIR": str(root / "yutaixian_data/private"),
            "YTX_ACTIVATION_FILE": str(root / "yutaixian_data/local_dev/activation.json")}


def initialize_instance(root, *, provider="native"):
    from common.security.vault import initialize_keyring, read_keyring
    path = instance_file(root)
    existing = read_json(path)
    if existing:
        if existing["database_provider"] != provider:
            raise RuntimeError("已有实例的数据提供方式不同；禁止自动创建另一套数据库")
        config_env = read_config(root)["services"]["backend-web"]["environment"]
        if existing.get("database_name") != config_env["MYSQL_DATABASE"] or existing.get("redis_db") != str(config_env["REDIS_DB"]):
            raise RuntimeError("数据库名称或 Redis DB 与已有实例不一致，禁止切换到空库")
        ring = read_keyring(Path(existing["keyring_file"]))
        if ring["instance_id"] != existing["instance_id"]:
            raise RuntimeError("实例与密钥不匹配")
        return existing
    # An existing encrypted database must never receive a newly generated key.
    if provider == "native":
        with mysql_connection(read_config(root)) as connection:
            with connection.cursor() as c:
                c.execute("SHOW TABLES LIKE 'ytx_instance'")
                if c.fetchone():
                    raise RuntimeError("数据库已有加密实例，请恢复 instance.json 和原密钥，不能重新初始化")
                c.execute("SHOW TABLES LIKE 'ytx_accounts'")
                if c.fetchone():
                    c.execute("SELECT COUNT(*) FROM ytx_accounts WHERE cookie LIKE 'ytxenc:%%' OR login_password LIKE 'ytxenc:%%'")
                    if c.fetchone()[0]:
                        raise RuntimeError("发现已有密文，请恢复原实例和密钥")
    instance_id = uuid.uuid4().hex
    base = (Path.home() / "Library/Application Support/Yutaixian" if sys.platform == "darwin"
            else Path.home() / ".config/yutaixian")
    key_path = base / "secrets" / instance_id / "keyring.json"
    initialize_keyring(key_path, instance_id)
    instance = {"version": 1, "instance_id": instance_id, "database_provider": provider,
                "application_mode": "native" if provider == "native" else "docker",
                "keyring_file": str(key_path),
                "database_name": read_config(root)["services"]["backend-web"]["environment"]["MYSQL_DATABASE"],
                "redis_db": str(read_config(root)["services"]["backend-web"]["environment"]["REDIS_DB"])}
    private_json(path, instance)
    return instance


class InfrastructureStack(LocalStack):
    """Only this supervisor may open native MySQL/Redis physical directories."""
    def __init__(self, root=None):
        super().__init__(root) if root else super().__init__()
        self.state_path = self.runtime / "infrastructure.json"

    def owned_supervisor(self, state):
        pid, token = state.get("pid"), state.get("token")
        if not isinstance(pid, int) or pid <= 1 or not token:
            return False
        result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "args="], capture_output=True, text=True)
        return result.returncode == 0 and str(self.root / "scripts/local_stack.py") in result.stdout and f"_infra {token}" in result.stdout

    def ports(self, config):
        return {name: port for name, port in super().ports(config).items() if name in ("mysql", "redis")}

    def start(self, **kwargs):
        if self.owned_supervisor(read_json(self.state_path)):
            check_native(read_config(self.root))
            return
        config = read_config(self.root)
        self.preflight_ports(config, {})
        state = {"token": secrets.token_hex(16), "phase": "starting", "children": {}, "error": ""}
        write_json(self.state_path, state)
        with self.log_file("infrastructure") as log:
            child = subprocess.Popen([sys.executable, str(self.root / "scripts/local_stack.py"), "_infra", state["token"]],
                                     cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                current = read_json(self.state_path)
                if current.get("phase") == "running":
                    check_native(config)
                    return
                if current.get("phase") == "failed" or child.poll() is not None:
                    raise RuntimeError(current.get("error") or "基础服务启动失败")
                time.sleep(.3)
            raise RuntimeError("基础服务就绪超时")
        except BaseException:
            child.terminate()
            child.wait(timeout=180)
            raise

    def serve(self, token):
        import signal
        state = read_json(self.state_path)
        if state.get("token") != token or state.get("phase") != "starting":
            raise RuntimeError("基础服务启动请求已失效")
        state["pid"] = os.getpid()
        write_json(self.state_path, state)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stopping", True))
        failure = ""
        try:
            self.start_databases(read_config(self.root))
            state.update(phase="running", children={name: child.pid for name, child in self.children})
            write_json(self.state_path, state)
            while not self.stopping:
                if any(child.poll() is not None for _, child in self.children):
                    raise RuntimeError("基础服务意外退出")
                time.sleep(.5)
        except BaseException as exc:
            failure = str(exc)
        finally:
            state["phase"] = "stopping"
            write_json(self.state_path, state)
            try:
                self.cleanup_children()
            except Exception as exc:
                failure = str(exc)
            state.update(phase="failed" if failure else "stopped", error=failure,
                         children={name: child.pid for name, child in self.children if child.poll() is None})
            write_json(self.state_path, state)


class Deployment:
    def __init__(self, stack):
        self.stack, self.root = stack, stack.root
        self.infra = InfrastructureStack(stack.root)
        self.application_path = stack.runtime / "application.json"

    def docker(self):
        from docker_deploy import DockerDeployment
        return DockerDeployment(self.stack)

    def stop_apps(self):
        record = read_json(self.application_path)
        if record.get("mode") == "docker":
            try:
                self.docker().daemon()
            except RuntimeError:
                # Docker Desktop may be closed. Native remains usable only after
                # independently proving that no container owns an application role.
                from common.security.runtime import lock_name, ROLES
                instance = read_json(instance_file(self.root))
                with mysql_connection(read_config(self.root)) as connection:
                    with connection.cursor() as cursor:
                        for role in ROLES:
                            cursor.execute("SELECT IS_USED_LOCK(%s)", (lock_name(instance["instance_id"], role),))
                            if cursor.fetchone()[0] is not None:
                                raise RuntimeError("Docker 不可用但仍有应用持锁，已阻止切换")
                print("Docker 已关闭且应用锁均已释放，将独立启动原生应用。")
            else:
                self.docker().stop_apps()
        else:
            self.stack.stop()
        # Fail closed after an interrupted supervisor; don't trust only its PID.
        config = read_config(self.root)
        self.stack.preflight_ports(config, {})
        (self.stack.runtime / "activation.json").unlink(missing_ok=True)

    def switch(self, target, *, restart=False, open_browser=True):
        if target not in ("native", "docker"):
            raise RuntimeError("运行方式只能是 native 或 docker")
        self.stack.ensure_config()
        instance = read_json(instance_file(self.root))
        if instance and instance.get("database_provider") != "native":
            raise RuntimeError("完整容器实例请使用 bash docker.sh；不能切换为本机基础服务")
        previous = read_json(self.application_path)
        native_state = read_json(self.stack.state_path)
        legacy = self.stack.owned_supervisor(native_state) and "mysql" in native_state.get("children", {})
        if target == "native":
            self.stack.prepare()
        else:
            self.docker().prepare_build()  # Daemon, build failure cannot stop old apps.
        self.stack.prerequisites()
        if not legacy:
            self.infra.start()
        check_native(read_config(self.root))
        instance = initialize_instance(self.root)
        if target == "docker":
            self.docker().preflight_shared(instance)  # Real container -> same loopback DB.
        else:
            env = local_dev.local_environment(read_config(self.root), "backend-web")
            env.update(instance_environment(self.root))
            from common.security.vault import read_keyring
            read_keyring(Path(instance["keyring_file"]))
            old_env = os.environ.copy()
            try:
                os.environ.update(env)
                from common.security.runtime import connect, identity, validate
                with connect() as connection:
                    if identity(connection, required=False):
                        validate(connection)
            finally:
                os.environ.clear()
                os.environ.update(old_env)
        if previous.get("mode") == target and not restart:
            if target == "native" and self.stack.owned_supervisor(native_state) and native_state.get("phase") == "running":
                print("原生应用已运行；未重复启动。")
                return
            if target == "docker" and self.docker().healthy():
                print("Docker 应用已运行；未重复启动。")
                return
        print("目标环境预检查通过，正在等待旧应用退出……", flush=True)
        self.stop_apps()
        if legacy:
            print("首次升级：旧管理器已完整停止，启动独立基础服务管理器。", flush=True)
            self.infra.start()
        check_native(read_config(self.root))
        legacy_captures = self.root / "yutaixian_data/static/uploads/face"
        private_captures = self.root / "yutaixian_data/private/face"
        private_captures.mkdir(parents=True, exist_ok=True, mode=0o700)
        if legacy_captures.is_dir():
            for capture in legacy_captures.glob("*.jpg"):
                if capture.is_file() and not capture.is_symlink():
                    target_path = private_captures / capture.name
                    if not target_path.exists():
                        capture.rename(target_path)
                        target_path.chmod(0o600)
        env = local_dev.local_environment(read_config(self.root), "backend-web")
        env.update(instance_environment(self.root))
        old_env = os.environ.copy()
        try:
            os.environ.update(env)
            from common.security.runtime import connect, migrate
            with connect() as connection:
                migrate(connection)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        runtime = {"mode": target, "runtime_id": uuid.uuid4().hex, "phase": "starting"}
        write_json(self.application_path, runtime)
        try:
            if target == "docker":
                self.docker().start_apps(instance, runtime)
            else:
                self.stack.start(open_browser=open_browser, prepared=True)
            write_json(self.stack.runtime / "activation.json", {"runtime_id": runtime["runtime_id"]})
            runtime["phase"] = "running"
            instance["application_mode"] = target
            private_json(instance_file(self.root), instance)
        except BaseException:
            runtime["phase"] = "failed"
            self.stop_apps()
            raise
        finally:
            write_json(self.application_path, runtime)

    def stop(self):
        self.stop_apps()
        self.infra.stop()
        print("应用、MySQL 和 Redis 已停止；数据与密钥均保留。")

    def status(self):
        instance = read_json(instance_file(self.root))
        print(f"数据库提供方式：{instance.get('database_provider', 'native')}；应用方式：{instance.get('application_mode', 'native')}")
        self.infra.status()
        if instance.get("application_mode") == "docker":
            self.docker().status()
        else:
            self.stack.status()
