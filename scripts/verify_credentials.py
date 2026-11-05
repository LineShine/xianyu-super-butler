"""Read-only audit; print counts, never secret values."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_dev import local_environment
from native_config import read_config
from deployment import instance_environment
from common.security.runtime import connect, validate
from common.security.vault import decrypt, token_context


def main():
    os.environ.update(local_environment(read_config(ROOT), "backend-web"))
    os.environ.update(instance_environment(ROOT))
    with connect() as connection:
        instance = validate(connection)
        with connection.cursor() as c:
            c.execute("SELECT owner_id, credential_id, cookie, login_password, proxy_pass, metadata FROM ytx_accounts")
            accounts = c.fetchall()
            for account in accounts:
                for field in ("cookie", "login_password", "proxy_pass"):
                    decrypt(account[field], ("account", account["credential_id"], account["owner_id"], field))
                metadata = account["metadata"]
                metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
                if (metadata or {}).get("cookies_refresh_snapshot"):
                    decrypt(metadata["cookies_refresh_snapshot"], ("account", account["credential_id"], account["owner_id"], "browser_snapshot"))
            c.execute("SELECT user_id, token FROM ytx_token_cache")
            tokens = c.fetchall()
            for token in tokens:
                decrypt(token["token"], token_context(token["user_id"]))
    print(json.dumps({"verified": True, "accounts": len(accounts), "token_rows": len(tokens), "credential_version": instance["credential_version"]}))


if __name__ == "__main__":
    main()
