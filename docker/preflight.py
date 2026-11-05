"""Runs inside the target image; outputs only non-secret identities."""
import json
import os
import sys

import redis
from common.security.vault import read_keyring
from common.security.runtime import connect, identity, validate, migrate


def main():
    ring = read_keyring()
    with connect() as connection:
        row = identity(connection, required=False)
        if row:
            validate(connection)
        elif os.environ.get("YTX_ALLOW_LEGACY_PREFLIGHT") != "1" and "--initialize" not in sys.argv:
            raise RuntimeError("实例未初始化")
        if "--initialize" in sys.argv:
            migrate(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT @@server_uuid AS uuid")
            mysql_uuid = cursor.fetchone()["uuid"]
        if os.environ.get("YTX_EXPECTED_MYSQL_UUID") and mysql_uuid != os.environ["YTX_EXPECTED_MYSQL_UUID"]:
            raise RuntimeError("MySQL 实例不匹配")
    with redis.Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]),
                     password=os.environ["REDIS_PASSWORD"], db=int(os.environ.get("REDIS_DB", 0)),
                     socket_connect_timeout=5, socket_timeout=5) as client:
        client.ping()
        run_id = client.info("server")["run_id"]
        if os.environ.get("YTX_EXPECTED_REDIS_RUN_ID") and run_id != os.environ["YTX_EXPECTED_REDIS_RUN_ID"]:
            raise RuntimeError("Redis 实例不匹配")
    print(json.dumps({"success": True, "mysql_uuid": mysql_uuid, "redis_run_id": run_id,
                      "instance_id": ring["instance_id"], "credential_version": 1}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"容器连接或实例预检查失败（{type(exc).__name__}）；未放宽数据库监听，也未创建替代库。", file=sys.stderr)
        raise SystemExit(1)
