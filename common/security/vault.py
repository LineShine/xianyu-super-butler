"""Versioned authenticated credential storage. Applications never create keys.

The unattended trust root is a private keyring outside the checkout/data backup.
Possession of both this file and the database permits decryption by design.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import stat
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from common.security.redaction import register_secret

PREFIX = "ytxenc:1:"
SCHEMA_VERSION = 1


class CredentialUnavailable(RuntimeError):
    """Not equivalent to missing account credentials; never disable an account."""


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value, altchars=b"-_", validate=True)


def keyring_path() -> Path:
    value = os.environ.get("YTX_KEYRING_FILE", "")
    if not value:
        raise CredentialUnavailable("凭据密钥未配置，请通过统一启动入口启动或恢复密钥")
    return Path(value).expanduser().absolute()


def read_keyring(path: Path | None = None) -> dict:
    path = path or keyring_path()
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise ValueError("not a regular file")
        if os.name != "nt" and info.st_mode & 0o077:
            raise ValueError("key permissions")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["version"] != 1 or len(_decode(data["keys"][data["active_key_id"]])) != 32:
            raise ValueError("invalid keyring")
        expected = os.environ.get("YTX_INSTANCE_ID")
        if expected and data["instance_id"] != expected:
            raise ValueError("wrong instance")
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CredentialUnavailable("凭据密钥不可用，请检查权限或恢复原密钥；账号配置未清除") from None


def initialize_keyring(path: Path, instance_id: str) -> dict:
    """Explicit bootstrap only; caller must first reject an encrypted DB without keys."""
    if path.exists() or path.is_symlink():
        data = read_keyring(path)
        if data["instance_id"] != instance_id:
            raise CredentialUnavailable("密钥与实例不匹配")
        return data
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    key_id = uuid.uuid4().hex
    data = {"version": 1, "instance_id": instance_id, "active_key_id": key_id,
            "keys": {key_id: _encode(secrets.token_bytes(32))}}
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = read_keyring(path)
        if existing["instance_id"] != instance_id:
            raise CredentialUnavailable("密钥与实例不匹配")
        return existing
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
        handle.flush()
        os.fsync(handle.fileno())
    return data


def is_encrypted(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def _aad(context: list | tuple, instance_id: str) -> bytes:
    return json.dumps([SCHEMA_VERSION, instance_id, *context], ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def encrypt(value: str | None, context: list | tuple) -> str | None:
    if value is None or value == "":
        return value
    if value.startswith("ytxenc:") and not is_encrypted(value):
        raise CredentialUnavailable("凭据格式版本不受支持，禁止旧程序覆盖新密文")
    if is_encrypted(value):
        # Never accept a foreign/corrupted envelope merely because it has our prefix.
        decrypt(value, context)
        return value
    register_secret(value)
    ring = read_keyring()
    aad = _aad(context, ring["instance_id"])
    dek, nonce, wrap_nonce = secrets.token_bytes(32), secrets.token_bytes(12), secrets.token_bytes(12)
    key_id = ring["active_key_id"]
    wrapped = AESGCM(_decode(ring["keys"][key_id])).encrypt(wrap_nonce, dek, aad)
    payload = {"k": key_id, "n": _encode(nonce), "w": _encode(wrap_nonce),
               "d": _encode(wrapped), "c": _encode(AESGCM(dek).encrypt(nonce, value.encode("utf-8"), aad))}
    return PREFIX + _encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def decrypt(value: str | None, context: list | tuple) -> str | None:
    if value is None or value == "":
        return value
    if not is_encrypted(value):
        raise CredentialUnavailable("检测到未迁移的凭据，自动任务已停止，请运行凭据迁移")
    try:
        ring = read_keyring()
        payload = json.loads(_decode(value[len(PREFIX):]))
        aad = _aad(context, ring["instance_id"])
        dek = AESGCM(_decode(ring["keys"][payload["k"]])).decrypt(
            _decode(payload["w"]), _decode(payload["d"]), aad)
        plaintext = AESGCM(dek).decrypt(_decode(payload["n"]), _decode(payload["c"]), aad).decode("utf-8")
        register_secret(plaintext)
        return plaintext
    except CredentialUnavailable:
        raise
    except Exception:
        raise CredentialUnavailable("凭据校验失败，请恢复正确密钥或凭据；账号配置未清除") from None


def account_context(account, field: str, *, owner_id=None) -> tuple:
    return ("account", account.credential_id, account.owner_id if owner_id is None else owner_id, field)


def token_context(user_id: str) -> tuple:
    return ("im-token", str(user_id))


def secret_attribute(field: str):
    """Preserve instance-level business reads, while SQL expressions see ciphertext.

    New assignments are transient until the flush hook encrypts them. Persisted
    plaintext is rejected. Projection queries must load the owning model instead.
    """
    from sqlalchemy import inspect
    from sqlalchemy.ext.hybrid import hybrid_property

    def get(obj):
        value = getattr(obj, "_" + field)
        if value is None or value == "":
            return value
        state = inspect(obj)
        if not is_encrypted(value) and (state.transient or state.pending or state.attrs["_" + field].history.has_changes()):
            return value
        return decrypt(value, account_context(obj, field))

    def set_(obj, value):
        if value and value.startswith("ytxenc:"):
            raise ValueError("凭据输入必须是新值，不能提交密文")
        setattr(obj, "_" + field, value)

    return hybrid_property(get, set_).expression(lambda cls: getattr(cls, "_" + field))


def encrypt_account(mapper, connection, account):
    from sqlalchemy import inspect
    state = inspect(account)
    if not account.credential_id:
        account.credential_id = uuid.uuid4().hex
    history = state.attrs.owner_id.history
    old_owner = history.deleted[0] if history.deleted else account.owner_id
    for field in ("cookie", "login_password", "proxy_pass"):
        value = getattr(account, "_" + field)
        if value and is_encrypted(value) and old_owner != account.owner_id:
            value = decrypt(value, account_context(account, field, owner_id=old_owner))
        if value and (not is_encrypted(value) or old_owner != account.owner_id):
            setattr(account, "_" + field, encrypt(value, account_context(account, field)))


    snapshot = (account.metadata_json or {}).get("cookies_refresh_snapshot")
    if snapshot:
        context = account_context(account, "browser_snapshot")
        if old_owner != account.owner_id and is_encrypted(snapshot):
            snapshot = decrypt(snapshot, account_context(account, "browser_snapshot", owner_id=old_owner))
        if isinstance(snapshot, list):
            snapshot = json.dumps(snapshot, ensure_ascii=False)
        protected = encrypt(snapshot, context)
        if protected != (account.metadata_json or {}).get("cookies_refresh_snapshot"):
            account.metadata_json = {**(account.metadata_json or {}), "cookies_refresh_snapshot": protected}


def encrypt_token(mapper, connection, token):
    if token._token and not is_encrypted(token._token):
        token._token = encrypt(token._token, token_context(token.user_id))
