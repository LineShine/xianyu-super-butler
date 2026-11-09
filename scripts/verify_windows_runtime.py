"""Isolated Windows smoke test, reusing prepared binaries without live accounts.

Run with the portable launcher Python after Prepare. The fixture copies source,
links only disposable dependency caches, and uses independent ports, DB and key.
It never writes an installer validation marker.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sys
import tempfile
import time
import urllib.request
import uuid

from windows_setup import read_json, write_json, SERVICES
from windows_runtime import Runtime, SOURCE_ROOT, environment
from windows_process import Handle, open_process, process_identity, terminate_process, matches
from windows_infra import mysql_connection, redis_connection
from common.security.vault import encrypt, decrypt


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.2)
    raise RuntimeError("验收条件等待超时")


def free_ports():
    listeners = []
    try:
        for _ in range(6):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listeners.append(listener)
        return [listener.getsockname()[1] for listener in listeners]
    finally:
        for listener in listeners:
            listener.close()


def request(url, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as response:
        return json.load(response)


def copy_dependencies(source, destination):
    # Hard links reuse immutable dependency files without requiring junction
    # privileges. Fall back to ordinary copies across volumes/filesystems.
    def link(source, destination):
        try:
            # Windows image sections cannot reliably share executable hard-link
            # aliases across the source checkout and a running fixture.
            if Path(source).suffix.lower() in (".exe", ".dll", ".pyd", ".node"):
                return shutil.copy2(source, destination)
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
    shutil.copytree(source, destination, copy_function=link)


def fixture(root):
    for name in (*SERVICES, "common", "config"):
        shutil.copytree(SOURCE_ROOT / name, root / name,
                        ignore=shutil.ignore_patterns(".venv", "__pycache__", "logs", "static", "browser_data", "tests"))
    (root / "frontend").mkdir()
    for name in ("package.json", "package-lock.json", "vite.config.ts"):
        shutil.copy2(SOURCE_ROOT / "frontend" / name, root / "frontend" / name)
    shutil.copytree(SOURCE_ROOT / "frontend/dist", root / "frontend/dist")
    copy_dependencies(SOURCE_ROOT / "frontend/node_modules", root / "frontend/node_modules")
    runtime = Runtime(root)
    runtime.ensure_directories()
    (runtime.data / "windows").mkdir()
    copy_dependencies(SOURCE_ROOT / "yutaixian_data/windows/browsers", runtime.setup.browsers)
    runtime.ensure_config()
    names = ("BACKEND_WEB_PORT", "WEBSOCKET_PORT", "SCHEDULER_PORT", "FRONTEND_PORT", "LOCAL_MYSQL_PORT", "LOCAL_REDIS_PORT")
    with (root / ".env").open("a", encoding="utf-8") as output:
        output.writelines(f"{name}={port}\n" for name, port in zip(names, free_ports()))
        output.write("AUTO_START_WEBSOCKET=false\nAUTO_START_SCHEDULER=false\nAUTO_START_CRAWL_JOBS=false\n")
    tools = read_json(SOURCE_ROOT / "yutaixian_data/local_dev/windows-tools.json")
    tools["service_python"] = {name: str(SOURCE_ROOT / name / ".venv/Scripts/python.exe") for name in SERVICES}
    write_json(runtime.setup.tools_file, tools)
    # Binaries/browsers have already passed Prepare; the fixture never reinstalls
    # into shared dependency caches or mutates the source checkout's runtime.
    runtime.setup.prepare = lambda: None
    return runtime


def verify(runtime, passed):
    runtime.start(browser=False)
    passed.append("fresh-start-and-health")
    first_apps = read_json(runtime.path_for("apps"))
    infrastructure = read_json(runtime.path_for("infra"))
    runtime.start(browser=False)
    assert read_json(runtime.path_for("apps"))["owner"] == first_apps["owner"]
    assert read_json(runtime.path_for("infra"))["children"] == infrastructure["children"]
    passed.append("duplicate-start")
    # The isolated fixture tests password authentication without an external
    # CAPTCHA provider. This setting never touches the user instance.
    with mysql_connection(runtime.config()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO ytx_system_settings (`key`, value) VALUES ('login_captcha_enabled', 'false') ON DUPLICATE KEY UPDATE value='false'")
    login = request(runtime.url() + "/api/v1/auth/login", {"username": "admin", "password": "admin123"})
    assert login["success"] and login["token"], "fresh administrator login failed"
    passed.append("frontend-api-proxy-and-admin-login")
    env = runtime.app_environment("backend-web", first_apps["runtime_id"])
    context = ("account", uuid.uuid4().hex, 1, "login_password")
    with environment(env):
        protected = encrypt("windows-synthetic-password", context)
        with mysql_connection(runtime.config()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO ytx_accounts (owner_id,account_id,credential_id,cookie,login_password,login_method,status) VALUES (1,'windows-fixture',%s,%s,%s,'password','disabled')", (context[1], encrypt("unb=windows-fixture", (*context[:3], "cookie")), protected))
                cursor.execute("INSERT INTO ytx_orders (owner_id,order_no,status,buyer_nick) VALUES (1,'windows-fixture-order','closed','persisted')")
    with redis_connection(runtime.config()) as client:
        client.set("ytx:windows-fixture", "retained", ex=600)
        start_ttl, started = client.pttl("ytx:windows-fixture"), time.monotonic()
    runtime.start(restart=True, browser=False)
    restarted = read_json(runtime.path_for("apps"))
    assert restarted["runtime_id"] != first_apps["runtime_id"]
    assert read_json(runtime.path_for("infra"))["children"] == infrastructure["children"]
    with redis_connection(runtime.config()) as client:
        assert client.get("ytx:windows-fixture") == b"retained"
        assert abs(client.pttl("ytx:windows-fixture") - (start_ttl - (time.monotonic() - started) * 1000)) < 3000
    passed.append("application-restart-preserves-db-processes-and-ttl")
    from common.security.runtime import RuntimeOwner
    with environment({**env, "YTX_RUNTIME_ID": "competing-windows-test"}):
        try:
            RuntimeOwner("backend-web").acquire()
        except RuntimeError:
            pass
        else:
            raise AssertionError("another generation obtained the role lock")
    passed.append("cross-generation-lock-rejection")
    before = read_json(runtime.path_for("apps"))
    write_json(runtime.runtime / "restart-request.json", {"service": "scheduler", "token": before["token"], "requested_at": time.time()})
    wait_for(lambda: read_json(runtime.path_for("apps")).get("children", {}).get("scheduler", {}).get("pid") not in (None, before["children"]["scheduler"]["pid"]), 120)
    wait_for(lambda: read_json(runtime.runtime / "activation.json").get("runtime_id") == before["runtime_id"], 120)
    assert read_json(runtime.path_for("infra"))["children"] == infrastructure["children"]
    runtime.check_health()
    passed.append("system-service-restart")
    # Terminate only the exact fixture supervisor handle, then verify job cleanup.
    record = read_json(runtime.path_for("apps"))
    with Handle(open_process(0x101001, False, record["owner"]["pid"])) as handle:
        assert process_identity(handle.handle, record["owner"]["pid"]) == record["owner"]
        assert terminate_process(handle.handle, 29)
    wait_for(lambda: not any(matches(child) for child in record["children"].values()))
    runtime.start(browser=False)
    passed.append("supervisor-crash-recovery")
    runtime.stop_group("apps")
    instance = read_json(runtime.data / "instance.json")
    key = Path(instance["keyring_file"])
    saved = key.with_suffix(".saved")
    key.rename(saved)
    try:
        try:
            runtime.start(browser=False)
        except RuntimeError:
            pass
        else:
            raise AssertionError("missing key was accepted")
        assert not key.exists() and not runtime.owned("apps")
    finally:
        saved.rename(key)
    passed.append("missing-key-fails-without-replacement")
    runtime.stop_group("infra")
    runtime.preflight_ports("apps")
    runtime.preflight_ports("infra")
    listener = socket.socket()
    frontend_port = int(runtime.config()["services"]["frontend"]["ports"][0]["published"])
    listener.bind(("127.0.0.1", frontend_port))
    listener.listen()
    try:
        try:
            runtime.start(browser=False)
        except RuntimeError:
            pass
        else:
            raise AssertionError("port conflict was accepted")
        with socket.create_connection(listener.getsockname(), timeout=2):
            pass
        assert not runtime.owned("apps") and not runtime.owned("infra")
    finally:
        listener.close()
    passed.append("port-conflict-preserves-unrelated-listener")
    runtime.start(browser=False)
    with environment(env):
        with mysql_connection(runtime.config()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT login_password FROM ytx_accounts WHERE account_id='windows-fixture'")
                assert decrypt(cursor.fetchone()[0], context) == "windows-synthetic-password"
                cursor.execute("SELECT buyer_nick FROM ytx_orders WHERE order_no='windows-fixture-order'")
                assert cursor.fetchone()[0] == "persisted"
    with redis_connection(runtime.config()) as client:
        assert client.get("ytx:windows-fixture") == b"retained"
    passed.append("full-stop-start-persistence-and-decryption")
    passed.append("chinese-and-space-checkout-path")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留合成实例供诊断")
    args = parser.parse_args()
    fixture_parent = SOURCE_ROOT / "yutaixian_data/windows-validation"
    fixture_parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="中文 空格-", dir=fixture_parent)).resolve()
    passed, runtime, succeeded = [], None, False
    report = {"os": platform.platform(), "python": sys.version, "checks": passed}
    try:
        runtime = fixture(root)
        verify(runtime, passed)
        succeeded = True
        print("Windows integration checks passed: " + str(len(passed)), flush=True)
    finally:
        if runtime:
            runtime.stop_group("apps")
            runtime.stop_group("infra")
        report["status"] = "passed" if succeeded else "failed"
        report["dependencies"] = {entry["name"]: entry["version"] for entry in read_json(SOURCE_ROOT / "launcher/windows/dependencies.json")["dependencies"]}
        write_json(SOURCE_ROOT / "build_output/windows-runtime-validation.json", report)
        print("Fixture: " + str(root), flush=True)
        if succeeded and not args.keep:
            instance = read_json(root / "yutaixian_data/instance.json")
            key = Path(instance["keyring_file"])
            expected = Path(os.environ["LOCALAPPDATA"]) / "Yutaixian/secrets" / instance["instance_id"]
            assert key.resolve().parent == expected.resolve()
            key.unlink()
            expected.rmdir()
            assert root.is_relative_to(fixture_parent.resolve()) and root.name.startswith("中文 空格-")
            shutil.rmtree(root)
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
