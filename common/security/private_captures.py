"""Private verification images; short-lived links are issued after owner checks."""
import hashlib
import hmac
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode

from common.security.vault import read_keyring, _decode


def capture_root():
    root = Path(os.environ.get("YTX_PRIVATE_DIR", "private")) / "face"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def safe_subject(subject):
    value = str(subject)
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) else hashlib.sha256(value.encode()).hexdigest()


def _signature(filename, expires):
    ring = read_keyring()
    key = _decode(ring["keys"][ring["active_key_id"]])
    return hmac.new(key, f"private-capture:v1:{ring['instance_id']}:{filename}:{expires}".encode(), hashlib.sha256).hexdigest()


def capture_url(filename):
    if not re.fullmatch(r"face_verify_[A-Za-z0-9_-]+\.jpg", filename):
        raise ValueError("无效截图文件名")
    expires = int(time.time()) + 300
    return "/api/v1/face-verification/image/" + filename + "?" + urlencode({"expires": expires, "signature": _signature(filename, expires)})


def verify_capture(filename, expires, signature):
    if not re.fullmatch(r"face_verify_[A-Za-z0-9_-]+\.jpg", filename):
        return None
    if not int(time.time()) <= expires <= int(time.time()) + 300:
        return None
    if not hmac.compare_digest(_signature(filename, expires), signature):
        return None
    path = capture_root() / filename
    return path if path.is_file() and not path.is_symlink() else None


def refresh_capture_url(value):
    if isinstance(value, str) and value.startswith(("/api/v1/face-verification/image/", "/static/uploads/face/")):
        filename = value.split("?", 1)[0].rsplit("/", 1)[-1]
        return capture_url(filename)
    return value
