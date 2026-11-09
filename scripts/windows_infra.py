"""Private MySQL/Redis, using only executables and data owned by this checkout."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import time

from windows_setup import read_json, write_json


def identity_hash(config):
    env = config["services"]["backend-web"]["environment"]
    return hashlib.sha256(json.dumps({key: env[key] for key in
                          ("MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD")}, sort_keys=True).encode()).hexdigest()


def port(config, name):
    return int(config["services"][name]["ports"][0]["published"])


def mysql_connection(config, **kwargs):
    import pymysql
    env = config["services"]["backend-web"]["environment"]
    options = dict(host="127.0.0.1", port=port(config, "mysql"), user=env["MYSQL_USER"],
                   password=env["MYSQL_PASSWORD"], database=env["MYSQL_DATABASE"], charset="utf8mb4",
                   connect_timeout=3, read_timeout=15, write_timeout=15, autocommit=True)
    options.update(kwargs)
    return pymysql.connect(**options)


def redis_connection(config):
    import redis
    env = config["services"]["backend-web"]["environment"]
    return redis.Redis(host="127.0.0.1", port=port(config, "redis"), password=env["REDIS_PASSWORD"],
                       db=int(env["REDIS_DB"]), socket_connect_timeout=3, socket_timeout=10,
                       single_connection_client=True)


def check_native(root, config):
    metadata = read_json(Path(root) / "yutaixian_data/native/metadata.json")
    if metadata.get("platform") != "win32" or metadata.get("status") != "ready" or metadata.get("identity_hash") != identity_hash(config):
        raise RuntimeError("本机基础服务记录与配置不一致，禁止连接其他数据库")
    with mysql_connection(config) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT @@server_uuid")
            if cursor.fetchone()[0] != metadata.get("server_uuid"):
                raise RuntimeError("MySQL 实例身份不匹配")
    with redis_connection(config) as client:
        if client.info("server")["run_id"] != metadata.get("redis_run_id"):
            raise RuntimeError("Redis 实例身份不匹配")


class Infrastructure:
    def __init__(self, owner):
        self.owner = owner
        self.config = owner.config()
        self.directory = owner.root / "yutaixian_data/native"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.metadata_file = self.directory / "metadata.json"
        self.metadata = read_json(self.metadata_file)
        self.tools = read_json(owner.setup.tools_file)
        self.mysql_dir, self.redis_dir = self.directory / "mysql", self.directory / "redis"
        self.mysql = self.redis = None

    def save(self):
        write_json(self.metadata_file, self.metadata)

    def admin(self):
        return mysql_connection(self.config, user="root", password=self.metadata["admin_password"], database=None)

    def mysql_args(self):
        return [self.tools["mysql"], "--no-defaults", "--console",
                "--basedir=" + str(Path(self.tools["mysql"]).parent.parent), "--datadir=.",
                "--bind-address=127.0.0.1", f"--port={port(self.config, 'mysql')}", "--mysqlx=0",
                "--skip-log-bin", "--character-set-server=utf8mb4", "--collation-server=utf8mb4_unicode_ci",
                "--default-time-zone=+08:00", "--max-allowed-packet=256M", "--max-connections=4000"]

    def start(self):
        import pymysql
        from common.security.windows_acl import protect
        env = self.config["services"]["backend-web"]["environment"]
        expected_hash = identity_hash(self.config)
        if self.metadata:
            if self.metadata.get("platform") != "win32" or self.metadata.get("identity_hash") != expected_hash:
                raise RuntimeError("基础服务数据来自其他平台或 .env 已改变，请恢复原配置")
            if self.metadata.get("mysql_initialized") and not (self.mysql_dir / "mysql.ibd").is_file():
                raise RuntimeError("已有实例的 MySQL 数据缺失，拒绝创建替代空库")
        else:
            if (self.owner.data / "instance.json").exists():
                raise RuntimeError("已有实例缺少基础服务记录，请恢复原数据，拒绝创建替代空库")
            if any(p.exists() and any(p.iterdir()) for p in (self.mysql_dir, self.redis_dir)):
                raise RuntimeError("数据目录已有内容但管理记录缺失，拒绝覆盖")
            self.metadata = {"platform": "win32", "status": "initializing", "identity_hash": expected_hash,
                             "admin_password": secrets.token_hex(32)}
            self.save()
        if self.metadata.get("status") == "ready" and not any((self.redis_dir / name).exists() for name in ("appendonlydir", "appendonly.aof", "dump.rdb")):
            raise RuntimeError("已有实例的 Redis 持久化数据缺失，请恢复原数据")
        if not self.metadata.get("data_initialized"):
            if self.mysql_dir.exists() and any(self.mysql_dir.iterdir()):
                # Only a never-published bootstrap may be quarantined after interruption.
                assert self.mysql_dir.resolve().is_relative_to(self.directory.resolve())
                self.mysql_dir.rename(self.directory / ("incomplete-mysql-" + secrets.token_hex(6)))
            self.mysql_dir.mkdir(exist_ok=True)
            child = self.owner.spawn("mysql-initialize", [self.tools["mysql"], "--no-defaults", "--initialize-insecure",
                    "--basedir=" + str(Path(self.tools["mysql"]).parent.parent), "--datadir=."], cwd=self.mysql_dir)
            self.owner.wait_exit(child, 180)
            if child.poll() != 0:
                raise RuntimeError("MySQL 初始化失败，请查看 mysql-initialize.log")
            self.owner.forget("mysql-initialize")
            self.metadata["data_initialized"] = True
            self.save()
        init_file = self.directory / "mysql-init.sql"
        args = self.mysql_args()
        if not self.metadata.get("mysql_initialized"):
            def literal(value):
                return pymysql.converters.escape_str(str(value), {})
            database = chr(96) + env["MYSQL_DATABASE"].replace(chr(96), chr(96) * 2) + chr(96)
            statements = ["ALTER USER 'root'@'localhost' IDENTIFIED BY " + literal(self.metadata["admin_password"]) + ";",
                          "CREATE DATABASE IF NOT EXISTS " + database + " CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"]
            for host in ("localhost", "127.0.0.1"):
                account = literal(env["MYSQL_USER"]) + "@" + literal(host)
                statements.extend(["CREATE USER IF NOT EXISTS " + account + " IDENTIFIED BY " + literal(env["MYSQL_PASSWORD"]) + ";",
                                   "ALTER USER " + account + " IDENTIFIED BY " + literal(env["MYSQL_PASSWORD"]) + ";",
                                   "GRANT ALL ON " + database + ".* TO " + account + ";"])
            # Parent is private; protect the empty file before writing credentials.
            with init_file.open("w", encoding="utf-8") as output:
                protect(init_file)
                output.write("\n".join(statements) + "\n")
            args.append("--init-file=../mysql-init.sql")
        self.mysql = self.owner.spawn("mysql", args, cwd=self.mysql_dir)
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                self.owner.require_alive(self.mysql, "mysql")
                try:
                    with self.admin() as connection:
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT @@server_uuid")
                            server_uuid = cursor.fetchone()[0]
                    break
                except pymysql.MySQLError:
                    time.sleep(.3)
            else:
                raise RuntimeError("MySQL 就绪超时，请查看 mysql.log")
        finally:
            init_file.unlink(missing_ok=True)
        if self.metadata.get("server_uuid") not in (None, server_uuid):
            raise RuntimeError("MySQL 物理数据已改变，实例身份不匹配")
        self.metadata.update(mysql_initialized=True, server_uuid=server_uuid)
        self.save()
        self.redis_dir.mkdir(exist_ok=True)
        # Relative paths avoid Cygwin drive-name and Unicode path conversion.
        redis_config = self.redis_dir / "redis.conf"
        with redis_config.open("w", encoding="utf-8") as output:
            protect(redis_config)
            output.write("\n".join(["bind 127.0.0.1", f"port {port(self.config, 'redis')}", "protected-mode yes",
                         "daemonize no", 'dir "."', "dbfilename dump.rdb", "appendonly yes", "appendfsync everysec",
                         "save 60 1", "maxmemory 256mb", "maxmemory-policy allkeys-lru",
                         "requirepass " + json.dumps(env["REDIS_PASSWORD"], ensure_ascii=False), ""]))
        self.redis = self.owner.spawn("redis", [self.tools["redis"], "redis.conf"], cwd=self.redis_dir)
        import redis
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            self.owner.require_alive(self.redis, "redis")
            try:
                with redis_connection(self.config) as client:
                    client.ping()
                    run_id = client.info("server")["run_id"]
                break
            except redis.RedisError:
                time.sleep(.3)
        else:
            raise RuntimeError("Redis 就绪超时，请查看 redis.log")
        self.metadata.update(status="ready", redis_run_id=run_id)
        self.save()
        check_native(self.owner.root, self.config)

    def shutdown(self):
        # We own the child handles; use authenticated protocol shutdown first.
        # Job closure is the bounded fallback and also cleans forked Redis workers.
        errors = []
        if self.redis and self.redis.poll() is None:
            try:
                with redis_connection(self.config) as client:
                    client.shutdown(save=True)
                self.redis.wait(30)
            except Exception:
                errors.append("Redis 正常关闭未完成")
        if self.mysql and self.mysql.poll() is None:
            try:
                with self.admin() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SHUTDOWN")
                self.mysql.wait(90)
            except Exception:
                if self.mysql.poll() is None:
                    errors.append("MySQL 正常关闭未完成")
        if errors:
            raise RuntimeError("；".join(errors) + "，本实例 Job 将回收剩余进程；数据保留供恢复")
