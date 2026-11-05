"""Shared database identity, explicit credential migration and runtime ownership.

Named locks are held on a dedicated MySQL connection, never on a pooled session.
All three roles must belong to the same launcher generation. A lost connection
terminates the service so it cannot continue external work after losing ownership.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import asynccontextmanager
import os
from pathlib import Path
import signal
import uuid

import pymysql

from common.security.vault import (
    CredentialUnavailable, SCHEMA_VERSION, encrypt, decrypt, is_encrypted,
    read_keyring, token_context,
)

ROLES = ("backend-web", "websocket", "scheduler")


def is_activated():
    try:
        value = json.loads(Path(os.environ["YTX_ACTIVATION_FILE"]).read_text())
        return value.get("runtime_id") == os.environ.get("YTX_RUNTIME_ID")
    except (OSError, ValueError, KeyError):
        return False


async def after_activation(callback):
    """Defer background work until the launcher has checked all application roles."""
    while not is_activated():
        await asyncio.sleep(.2)
    result = callback()
    if inspect.isawaitable(result):
        await result


class ActivationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/static/uploads/face"):
            from starlette.responses import JSONResponse
            await JSONResponse({"detail": "截图需要授权访问"}, status_code=404)(scope, receive, send)
            return
        if scope["type"] in ("http", "websocket") and scope.get("path") != "/health" and not is_activated():
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                from starlette.responses import JSONResponse
                await JSONResponse({"success": False, "message": "应用正在完成启动检查，请稍后重试"}, status_code=503)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def connect():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "yutaixian"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "yutaixian_data"),
        charset="utf8mb4", autocommit=True, connect_timeout=5,
        read_timeout=10, write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
    )


def identity(connection, *, required=True):
    with connection.cursor() as c:
        c.execute("SHOW TABLES LIKE 'ytx_instance'")
        if not c.fetchone():
            if required:
                raise CredentialUnavailable("实例尚未迁移，请使用统一启动器")
            return None
        c.execute("SELECT * FROM ytx_instance WHERE id=1")
        row = c.fetchone()
        if not row and required:
            raise CredentialUnavailable("实例标识缺失，不能启动任务")
        return row


def validate(connection):
    ring = read_keyring()
    row = identity(connection)
    if (row["instance_id"] != ring["instance_id"] or
            row["key_id"] not in ring["keys"] or
            row["credential_version"] != SCHEMA_VERSION or row["state"] != "ready"):
        raise CredentialUnavailable("实例、密钥或凭据版本不兼容，禁止启动")
    with connection.cursor() as c:
        c.execute("SELECT @@server_uuid AS uuid")
        if c.fetchone()["uuid"] != row["mysql_uuid"]:
            raise CredentialUnavailable("MySQL 实例身份不匹配，禁止连接副本或空库")
    return row


def migrate(connection):
    """Called only by the stopped-app launcher, or before fresh DB first use.

    DDL is idempotent. Credential rows and the version marker commit together;
    interruption rolls back all data changes. Never generates a replacement key.
    """
    ring = read_keyring()
    with connection.cursor() as c:
        c.execute("SELECT GET_LOCK('ytx:credential-migration', 0) AS acquired")
        if c.fetchone()["acquired"] != 1:
            raise CredentialUnavailable("另一项凭据迁移正在进行")
        try:
            old = identity(connection, required=False)
            if old:
                validate(connection)
            c.execute("""CREATE TABLE IF NOT EXISTS ytx_instance (
                id INT PRIMARY KEY, instance_id VARCHAR(64) NOT NULL,
                mysql_uuid VARCHAR(64) NOT NULL, key_id VARCHAR(64) NOT NULL,
                credential_version INT NOT NULL, state VARCHAR(16) NOT NULL,
                runtime_id VARCHAR(64) NULL) ENGINE=InnoDB""")
            c.execute("SELECT @@server_uuid AS uuid")
            mysql_uuid = c.fetchone()["uuid"]
            c.execute("SHOW TABLES LIKE 'ytx_accounts'")
            accounts_exist = bool(c.fetchone())
            if accounts_exist:
                c.execute("SHOW COLUMNS FROM ytx_accounts LIKE 'credential_id'")
                if not c.fetchone():
                    c.execute("ALTER TABLE ytx_accounts ADD COLUMN credential_id VARCHAR(32) NULL")
                c.execute("SHOW COLUMNS FROM ytx_accounts LIKE 'proxy_pass'")
                column = c.fetchone()
                if column and column["Type"].lower() != "text":
                    c.execute("ALTER TABLE ytx_accounts MODIFY proxy_pass TEXT NULL")
            if not old and accounts_exist:
                c.execute("SELECT id, owner_id, credential_id, cookie, login_password, proxy_pass, metadata FROM ytx_accounts")
                account_rows = c.fetchall()
                c.execute("SHOW TABLES LIKE 'ytx_token_cache'")
                token_rows = []
                if c.fetchone():
                    c.execute("SELECT id, user_id, token FROM ytx_token_cache")
                    token_rows = c.fetchall()
                if account_rows or token_rows:
                    # A recoverable pre-upgrade snapshot, itself encrypted. Never
                    # write a fresh plaintext dump while migrating old credentials.
                    backup_root = Path(os.environ.get("BACKUP_DIR", "backups"))
                    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    backup_path = backup_root / f"credential-upgrade-{ring['instance_id']}.ytxenc"
                    if not backup_path.exists():
                        payload = json.dumps({"version": 1, "accounts": account_rows, "tokens": token_rows}, default=str)
                        protected = encrypt(payload, ("credential-upgrade-backup",))
                        assert decrypt(protected, ("credential-upgrade-backup",)) == payload
                        descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                        with os.fdopen(descriptor, "w") as backup:
                            backup.write(protected)
                            backup.flush()
                            os.fsync(backup.fileno())
                    else:
                        decrypt(backup_path.read_text(), ("credential-upgrade-backup",))
            connection.begin()
            try:
                if accounts_exist:
                    c.execute("SELECT id, owner_id, credential_id, cookie, login_password, proxy_pass, metadata FROM ytx_accounts FOR UPDATE")
                    for account in c.fetchall():
                        credential_id = account["credential_id"] or uuid.uuid4().hex
                        values = []
                        for field in ("cookie", "login_password", "proxy_pass"):
                            context = ("account", credential_id, account["owner_id"], field)
                            values.append(encrypt(account[field], context))
                        metadata = account["metadata"]
                        metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
                        if isinstance(metadata, dict) and metadata.get("cookies_refresh_snapshot"):
                            snapshot = metadata["cookies_refresh_snapshot"]
                            if isinstance(snapshot, list):
                                snapshot = json.dumps(snapshot, ensure_ascii=False)
                            metadata["cookies_refresh_snapshot"] = encrypt(snapshot, ("account", credential_id, account["owner_id"], "browser_snapshot"))
                        c.execute("UPDATE ytx_accounts SET credential_id=%s, cookie=%s, login_password=%s, proxy_pass=%s, metadata=%s WHERE id=%s",
                                  (credential_id, *values, json.dumps(metadata) if metadata is not None else None, account["id"]))
                c.execute("SHOW TABLES LIKE 'ytx_token_cache'")
                if c.fetchone():
                    c.execute("SELECT id, user_id, token FROM ytx_token_cache FOR UPDATE")
                    for token in c.fetchall():
                        c.execute("UPDATE ytx_token_cache SET token=%s WHERE id=%s",
                                  (encrypt(token["token"], token_context(token["user_id"])), token["id"]))
                if not old:
                    c.execute("INSERT INTO ytx_instance (id,instance_id,mysql_uuid,key_id,credential_version,state) VALUES (1,%s,%s,%s,%s,'ready')",
                              (ring["instance_id"], mysql_uuid, ring["active_key_id"], SCHEMA_VERSION))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            c.execute("SELECT RELEASE_LOCK('ytx:credential-migration')")


def lock_name(instance_id, role):
    return f"ytx:{instance_id[:32]}:{role}"


class RuntimeOwner:
    def __init__(self, role):
        self.role = role
        self.connection = None

    def acquire(self):
        runtime_id = os.environ.get("YTX_RUNTIME_ID")
        if not runtime_id:
            raise CredentialUnavailable("缺少应用运行标识，请通过统一启动入口运行")
        conn = connect()
        try:
            row = validate(conn)
            names = [lock_name(row["instance_id"], role) for role in ROLES]
            conn.begin()
            with conn.cursor() as c:
                c.execute("SELECT runtime_id FROM ytx_instance WHERE id=1 FOR UPDATE")
                previous = c.fetchone()["runtime_id"]
                if previous != runtime_id:
                    for name in names:
                        c.execute("SELECT IS_USED_LOCK(%s) AS owner", (name,))
                        if c.fetchone()["owner"] is not None:
                            raise CredentialUnavailable("另一运行模式仍持有账号任务，拒绝竞争启动")
                c.execute("SELECT GET_LOCK(%s,0) AS acquired", (lock_name(row["instance_id"], self.role),))
                if c.fetchone()["acquired"] != 1:
                    raise CredentialUnavailable("该应用服务已运行，拒绝重复启动")
                c.execute("UPDATE ytx_instance SET runtime_id=%s WHERE id=1", (runtime_id,))
            conn.commit()
            self.connection = conn
        except BaseException:
            conn.rollback()
            conn.close()
            raise

    async def monitor(self):
        while True:
            await asyncio.sleep(1)
            try:
                await asyncio.to_thread(self.connection.ping, reconnect=False)
                # Removing/corrupting the key must not be interpreted as an account
                # without a configured password by a renewal fallback.
                read_keyring()
            except Exception:
                from loguru import logger
                logger.error("实例锁或凭据密钥不可用，停止服务并保留账号状态")
                os.kill(os.getpid(), signal.SIGTERM)
                return


@asynccontextmanager
async def runtime_owner(role):
    owner = RuntimeOwner(role)
    await asyncio.to_thread(owner.acquire)
    monitor = asyncio.create_task(owner.monitor())
    try:
        yield
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        owner.connection.close()
