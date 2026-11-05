"""Opt-in real Mac/Docker round trip, using only disposable local data and keys.

Run with backend-web/.venv/bin/python after `bash dev.sh build`.
No live account or production database is used. All workers are deactivated.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_stack import LocalStack, APPS, read_json, write_json
from local_dev import local_environment
from native_config import read_config, private_json
from native_infra import NativeInfrastructure, mysql_connection, redis_connection, check_native
from docker_deploy import DockerDeployment
from common.security.vault import initialize_keyring, encrypt, decrypt
from common.security.runtime import connect, migrate, validate, RuntimeOwner


@contextmanager
def environment(values):
    previous = os.environ.copy()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


class Fixture:
    def __init__(self, root):
        self.root = root
        shutil.copytree(ROOT / "config", root / "config")
        sockets = []
        for _ in range(6):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        keys = ("LOCAL_MYSQL_PORT", "LOCAL_REDIS_PORT", "BACKEND_WEB_PORT", "WEBSOCKET_PORT", "SCHEDULER_PORT", "FRONTEND_PORT")
        ports = {key: str(sock.getsockname()[1]) for key, sock in zip(keys, sockets)}
        config = {**ports, "MYSQL_DATABASE": "ytx_fixture", "MYSQL_USER": "fixture", "MYSQL_PASSWORD": "fixture-mysql-secret",
                  "REDIS_PASSWORD": "fixture-redis-secret", "REDIS_DB": "4", "MYSQL_ROOT_PASSWORD": "fixture-root-secret",
                  "INTERNAL_API_TOKEN": "fixture-only-internal-token-32-characters", "AUTO_START_WEBSOCKET": "false",
                  "AUTO_START_CRAWL_JOBS": "false", "AUTO_START_SCHEDULER": "false", "LOG_LEVEL": "WARNING",
                  "BACKEND_DB_POOL_SIZE": "600", "WEBSOCKET_DB_POOL_SIZE": "600", "SCHEDULER_DB_POOL_SIZE": "600"}
        (root / ".env").write_text("\n".join(f"{key}={value}" for key, value in config.items()) + "\n")
        (root / ".env").chmod(0o600)
        for sock in sockets:
            sock.close()
        self.apps = LocalStack(root)
        self.databases = LocalStack(root)
        self.config = read_config(root)
        self.infra = NativeInfrastructure(self.databases, self.config)
        self.instance = {"version": 1, "instance_id": uuid.uuid4().hex, "database_provider": "native", "application_mode": "native",
                         "database_name": "ytx_fixture", "redis_db": "4", "keyring_file": str(root / "private-keys/keyring.json")}
        initialize_keyring(Path(self.instance["keyring_file"]), self.instance["instance_id"])
        private_json(root / "yutaixian_data/instance.json", self.instance)
        self.runtime_id = uuid.uuid4().hex
        self.docker = DockerDeployment(self.apps)
        self.image_manifest = read_json(ROOT / "yutaixian_data/local_dev/images.json")
        self.docker.fingerprint = lambda: self.image_manifest["source_fingerprint"]
        write_json(self.apps.runtime / "images.json", self.image_manifest)

    def env(self, service):
        values = local_environment(self.config, service)
        values.update(YTX_INSTANCE_ID=self.instance["instance_id"], YTX_KEYRING_FILE=self.instance["keyring_file"],
                      YTX_RUNTIME_ID=self.runtime_id, YTX_ACTIVATION_FILE=str(self.apps.runtime / "activation.json"),
                      YTX_BROWSER_ROOT=str(self.root / "yutaixian_data/browser_runtime/darwin"),
                      YTX_PRIVATE_DIR=str(self.root / "yutaixian_data/private"),
                      YTX_LOG_DIR=str(self.apps.logs / service),
                      PYTHONPATH=os.pathsep.join((str(ROOT / service), str(ROOT))),
                      PLAYWRIGHT_BROWSERS_PATH=str(ROOT / "yutaixian_data/local_dev/browsers"))
        return values

    def start_native(self):
        print("fixture: starting native applications", flush=True)
        self.apps.preflight_ports(self.config, {})
        for service in APPS:
            env = {**os.environ, **(self.env(service) if service != "frontend" else {})}
            port = self.apps.ports(self.config)[service]
            if service == "frontend":
                command = [shutil.which("node"), str(ROOT / "frontend/node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", str(port), "--strictPort"]
                cwd = ROOT / "frontend"
                env["LOCAL_BACKEND_URL"] = f"http://127.0.0.1:{self.apps.ports(self.config)['backend-web']}"
            else:
                command = [str(ROOT / service / ".venv/bin/python"), str(ROOT / service / "main.py")]
                cwd = self.root / "yutaixian_data"
            with self.apps.log_file(service) as output:
                child = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=output, start_new_session=True)
            self.apps.children.append((service, child))
            self.apps.wait_ready(service, child, port, timeout=120)
        write_json(self.apps.runtime / "activation.json", {"runtime_id": self.runtime_id})

    def stop_native(self):
        self.apps.cleanup_children()
        self.apps.children.clear()
        (self.apps.runtime / "activation.json").unlink(missing_ok=True)

    def probe(self, *, container=False, modify=False):
        code = '''import json,os
from pathlib import Path
from common.security.runtime import connect,validate
from common.security.vault import decrypt
with connect() as connection:
 row=validate(connection)
 with connection.cursor() as c:
  c.execute("SELECT buyer_nick FROM ytx_orders WHERE order_no='fixture-order'")
  assert c.fetchone()['buyer_nick']==EXPECTED
  c.execute("SELECT credential_id,owner_id,login_password FROM ytx_accounts WHERE account_id='fixture-account'")
  account=c.fetchone()
  assert decrypt(account['login_password'],('account',account['credential_id'],account['owner_id'],'login_password'))=='fixture-login-secret'
  if MODIFY:
   c.execute("UPDATE ytx_orders SET buyer_nick='docker-updated' WHERE order_no='fixture-order'")
assert (Path(os.environ['STATIC_DIR'])/'uploads/images/fixture.txt').read_text()=='shared-image-fixture'
print('fixture probe passed')
'''.replace("EXPECTED", repr("native-created" if modify else "docker-updated")).replace("MODIFY", repr(modify))
        if container:
            self.docker.compose(["run", "--rm", "--no-deps", "backend-web", "python", "-c", code], label="fixture: checking container reads shared credentials and order")
        else:
            result = subprocess.run([str(ROOT / "backend-web/.venv/bin/python"), "-c", code], env={**os.environ, **self.env("backend-web")}, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stderr)
            print(result.stdout.strip(), flush=True)

    def check_credential_api(self):
        import httpx
        from jose import jwt
        with environment(self.env("backend-web")):
            with connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT value FROM ytx_system_settings WHERE `key`='security.jwt_secret_key'")
                    secret = cursor.fetchone()["value"]
        token = jwt.encode({"sub": "1", "exp": int(time.time()) + 300}, secret, algorithm="HS256")
        base = f"http://127.0.0.1:{self.apps.ports(self.config)['backend-web']}/api/v1"
        with httpx.Client(headers={"Authorization": "Bearer " + token}, timeout=10, trust_env=False) as client:
            response = client.get(base + "/cookies/details")
            response.raise_for_status()
            assert "fixture-login-secret" not in response.text and "unb=fixture-account" not in response.text
            assert '"has_password":true' in response.text
            for action, value in (("preserve", None), ("replace", "temporary-new-password"), ("clear", None), ("replace", "fixture-login-secret")):
                payload = {"username": "fixture-user", "password_action": action}
                if value is not None:
                    payload["login_password"] = value
                result = client.put(base + "/cookies/fixture-account/login-info", json=payload)
                assert result.json()["success"], result.text
            proxy = client.put(base + "/proxy/fixture-account", json={"proxy_type": "http", "proxy_host": "127.0.0.1", "proxy_port": 4444, "proxy_pass": "fixture-proxy-secret", "password_action": "replace"})
            assert proxy.json()["success"], proxy.text
            response = client.get(base + "/proxy/fixture-account")
            assert "fixture-proxy-secret" not in response.text and response.json()["data"]["has_proxy_password"]
        ws = f"http://127.0.0.1:{self.apps.ports(self.config)['websocket']}"
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.post(ws + "/password-login", json={"account_id": "fixture-account", "account": "fake", "password": "fake", "user_id": 1})
            assert response.status_code in (401, 403) or response.json().get("code") in (401, 403), response.text
            response = client.get(ws + "/password-login/check/not-my-session", params={"user_id": 2}, headers={"X-Internal-Token": self.env("websocket")["INTERNAL_API_TOKEN"]})
            assert response.status_code == 404 or response.json().get("code") == 404, response.text
        print("fixture: credential write-only API, preserve/replace/clear and internal authentication passed", flush=True)

    def run(self, *, native_only=False):
        self.infra.prepare_tools()
        self.infra.start()
        check_native(self.config)
        initial = read_json(self.infra.metadata_path)
        with environment(self.env("backend-web")):
            with connect() as connection:
                migrate(connection)
        self.start_native()
        print("fixture: native health passed", flush=True)
        # Check cross-generation and duplicate role rejection while native workers hold locks.
        with environment({**self.env("backend-web"), "YTX_RUNTIME_ID": "competing-generation"}):
            try:
                RuntimeOwner("backend-web").acquire()
                raise AssertionError("competing runtime acquired ownership")
            except RuntimeError as exc:
                if isinstance(exc, AssertionError):
                    raise
        with environment(self.env("backend-web")):
            with connect() as connection:
                with connection.cursor() as cursor:
                    cid = uuid.uuid4().hex
                    cursor.execute("INSERT INTO ytx_accounts (owner_id,account_id,credential_id,cookie,login_password,login_method,status) VALUES (1,'fixture-account',%s,%s,%s,'password','disabled')",
                                   (cid, encrypt("unb=fixture-account", ("account", cid, 1, "cookie")), encrypt("fixture-login-secret", ("account", cid, 1, "login_password"))))
                    cursor.execute("INSERT INTO ytx_orders (owner_id,order_no,status,buyer_nick) VALUES (1,'fixture-order','closed','native-created')")
        self.check_credential_api()
        image = self.root / "yutaixian_data/static/uploads/images/fixture.txt"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_text("shared-image-fixture")
        client = redis_connection(self.config, db=4)
        try:
            client.set("ytx:fixture:ttl", "native-value", ex=600)
            started = time.monotonic()
            initial_ttl = client.pttl("ytx:fixture:ttl")
            if native_only:
                return
            self.docker.preflight_shared(self.instance)
            self.stop_native()
            self.runtime_id = uuid.uuid4().hex
            runtime = {"mode": "docker", "runtime_id": self.runtime_id}
            self.docker.start_apps(self.instance, runtime)
            write_json(self.apps.runtime / "activation.json", {"runtime_id": self.runtime_id})
            assert self.docker.healthy()
            self.check_credential_api()
            self.probe(container=True, modify=True)
            # Static file is served through the frontend reverse proxy.
            with urllib.request.urlopen(f"http://127.0.0.1:{self.apps.ports(self.config)['frontend']}/static/uploads/images/fixture.txt") as response:
                assert response.read() == b"shared-image-fixture"
            self.docker.stop_apps()
            self.runtime_id = uuid.uuid4().hex
            self.start_native()
            self.probe()
            check_native(self.config)
            current = read_json(self.infra.metadata_path)
            assert current["server_uuid"] == initial["server_uuid"]
            assert client.info("server")["run_id"] == initial["redis_run_id"]
            assert client.get("ytx:fixture:ttl") == b"native-value"
            expected_ttl = initial_ttl - (time.monotonic() - started) * 1000
            assert abs(client.pttl("ytx:fixture:ttl") - expected_ttl) < 3000
            print("PASS: native -> Docker -> native; same MySQL UUID, Redis PID identity, value/TTL, credential key and shared files", flush=True)
        finally:
            client.close()

    def run_full_containers(self):
        self.instance["database_provider"] = "docker"
        private_json(self.root / "yutaixian_data/instance.json", self.instance)
        original_config = self.docker.config
        def fixture_config(instance, runtime, **kwargs):
            value = original_config(instance, runtime, full=True)
            for service in ("mysql", "redis"):
                published = self.config["services"][service]["ports"][0]
                value["services"][service]["ports"] = [f"127.0.0.1:{published['published']}:{published['target']}"]
            return value
        self.docker.config = fixture_config
        runtime = {"mode": "docker", "runtime_id": self.runtime_id}
        private_json(self.docker.path, self.docker.config(self.instance, runtime))
        self.docker.compose(["up", "-d", "--wait", "--wait-timeout", "240", "mysql", "redis"], label="fixture: starting fresh Linux database containers")
        self.docker.compose(["run", "--rm", "--no-deps", "backend-web", "python", "/app/preflight.py", "--initialize"], label="fixture: initializing fresh container instance")
        self.docker.start_apps(self.instance, runtime, full=True)
        write_json(self.apps.runtime / "activation.json", {"runtime_id": self.runtime_id})
        with environment(self.env("backend-web")):
            with connect() as connection:
                initial_uuid = validate(connection)["mysql_uuid"]
                with connection.cursor() as cursor:
                    cid = uuid.uuid4().hex
                    cursor.execute("INSERT INTO ytx_accounts (owner_id,account_id,credential_id,cookie,login_password,login_method,status) VALUES (1,'fixture-account',%s,%s,%s,'password','disabled')", (cid, encrypt("unb=fixture-account", ("account", cid, 1, "cookie")), encrypt("fixture-login-secret", ("account", cid, 1, "login_password"))))
                    cursor.execute("INSERT INTO ytx_orders (owner_id,order_no,status,buyer_nick) VALUES (1,'fixture-order','closed','native-created')")
        image = self.root / "yutaixian_data/static/uploads/images/fixture.txt"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_text("shared-image-fixture")
        self.check_credential_api()
        self.probe(container=True, modify=True)
        with redis_connection(self.config, db=4) as client:
            initial_run = client.info("server")["run_id"]
            client.set("ytx:fixture:full", "retained", ex=300)
        self.docker.stop_apps()
        self.runtime_id = uuid.uuid4().hex
        self.docker.start_apps(self.instance, {"runtime_id": self.runtime_id}, full=True)
        write_json(self.apps.runtime / "activation.json", {"runtime_id": self.runtime_id})
        self.probe(container=True)
        with environment(self.env("backend-web")):
            with connect() as connection:
                assert validate(connection)["mysql_uuid"] == initial_uuid
        with redis_connection(self.config, db=4) as client:
            assert client.info("server")["run_id"] == initial_run
            assert client.get("ytx:fixture:full") == b"retained" and 0 < client.ttl("ytx:fixture:full") < 300
        print("PASS: fresh Linux six-container deployment, encrypted credentials, authenticated API, application restart retains MySQL/Redis", flush=True)

    def cleanup(self):
        if self.docker.path.exists():
            self.docker.compose(["down", "--timeout", "180"], label="fixture: removing only disposable application containers")
        try:
            self.apps.cleanup_children()
        finally:
            self.databases.cleanup_children()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-only", action="store_true")
    parser.add_argument("--full", action="store_true", help="Test fresh Linux containers inside Docker using disposable data")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="ytx-shared-fixture-"))
    fixture = Fixture(root)
    try:
        if args.full:
            fixture.run_full_containers()
        else:
            fixture.run(native_only=args.native_only)
    except BaseException:
        print(f"Fixture failed; diagnostic files preserved at {root}", file=sys.stderr)
        raise
    finally:
        fixture.cleanup()
    shutil.rmtree(root)


if __name__ == "__main__":
    main()
