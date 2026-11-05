"""Synthetic credentials only; authenticated encryption and existing ORM behavior."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from common.models.ytx_account import YtxAccount
from common.models.token_cache import TokenCache
from common.security.vault import (
    CredentialUnavailable, initialize_keyring, encrypt, decrypt, is_encrypted,
    read_keyring, account_context,
)
from common.utils.cookie_refresh import get_cookie_refresh_snapshot, set_cookie_refresh_snapshot
from common.security.redaction import redact, register_secret


class VaultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.key = Path(temporary.name) / "keyring.json"
        initialize_keyring(self.key, "fixture-instance")
        environment = patch.dict(os.environ, {"YTX_KEYRING_FILE": str(self.key), "YTX_INSTANCE_ID": "fixture-instance"})
        environment.start()
        self.addCleanup(environment.stop)
        self.engine = create_engine("sqlite://")
        YtxAccount.__table__.create(self.engine)
        TokenCache.__table__.create(self.engine)
        self.addCleanup(self.engine.dispose)

    def test_randomized_authenticated_envelopes_and_context_binding(self):
        context = ("account", "fixture", 1, "login_password")
        first, second = encrypt("test-only-password", context), encrypt("test-only-password", context)
        self.assertNotEqual(first, second)
        self.assertNotIn("test-only-password", first)
        self.assertEqual(decrypt(first, context), "test-only-password")
        for wrong in (("account", "fixture", 2, "login_password"), ("account", "other", 1, "login_password"), ("account", "fixture", 1, "cookie")):
            with self.assertRaises(CredentialUnavailable):
                decrypt(first, wrong)
        with self.assertRaises(CredentialUnavailable):
            decrypt(first[:-5] + "AAAAA", context)

    def test_missing_key_never_generates_or_clears_credentials(self):
        value = encrypt("secret-fixture", ("fixture",))
        self.key.unlink()
        with self.assertRaises(CredentialUnavailable):
            decrypt(value, ("fixture",))
        self.assertFalse(self.key.exists())

    def test_permissions_and_wrong_instance_rejected(self):
        self.key.chmod(0o644)
        with self.assertRaises(CredentialUnavailable):
            read_keyring()
        self.key.chmod(0o600)
        with patch.dict(os.environ, {"YTX_INSTANCE_ID": "another"}):
            with self.assertRaises(CredentialUnavailable):
                read_keyring()

    def test_orm_round_trip_owner_transfer_and_portable_cookie_snapshot(self):
        with Session(self.engine, expire_on_commit=False) as session:
            account = YtxAccount(id=1, owner_id=10, account_id="fixture", cookie="unb=fixture; auth=synthetic-value",
                                 username="fixture", login_password="fixture-password", proxy_pass="proxy-fixture",
                                 login_method="password")
            account.metadata_json = set_cookie_refresh_snapshot({}, [{"name": "auth", "value": "snapshot-secret", "domain": ".goofish.com"}])
            session.add(account)
            session.commit()
            credential_id = account.credential_id
            raw = session.execute(text("SELECT cookie, login_password, proxy_pass, metadata FROM ytx_accounts")).one()
            for value in raw[:3]:
                self.assertTrue(is_encrypted(value))
            self.assertNotIn("snapshot-secret", raw[3])
            account.owner_id = 20
            session.commit()
            self.assertEqual(account.credential_id, credential_id)
            self.assertEqual(account.login_password, "fixture-password")
            self.assertEqual(get_cookie_refresh_snapshot(account.metadata_json, account=account)[0]["value"], "snapshot-secret")
        with Session(self.engine) as session:
            account = session.get(YtxAccount, 1)
            self.assertEqual(account.proxy_pass, "proxy-fixture")
            with self.assertRaises(CredentialUnavailable):
                decrypt(account._login_password, ("account", credential_id, 10, "login_password"))
            account.remark = "ordinary update retains secrets"
            session.commit()
            self.assertEqual(account.login_password, "fixture-password")

    def test_persisted_plaintext_and_corrupt_secret_fail_closed(self):
        with Session(self.engine) as session:
            session.add(YtxAccount(id=1, owner_id=1, account_id="fixture", cookie="fixture-cookie", login_method="qr"))
            session.commit()
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE ytx_accounts SET cookie='legacy-plaintext'"))
        with Session(self.engine) as session:
            with self.assertRaises(CredentialUnavailable):
                _ = session.get(YtxAccount, 1).cookie

    def test_secret_logs_strip_values_and_connection_urls(self):
        register_secret("fixture-password-123456789")
        output = redact('password=unknown cookie="unb=private;auth=secret" fixture-password-123456789 https://user:secret@proxy/path?signature=abc')
        for secret in ("unknown", "unb=private", "fixture-password-123456789", "user:secret", "signature=abc"):
            self.assertNotIn(secret, output)


if __name__ == "__main__":
    unittest.main()
