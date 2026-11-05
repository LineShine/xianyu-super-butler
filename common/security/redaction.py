"""Last-mile redaction for logging. Never store these values in log extras."""
from collections import deque
import re
import threading

_values = deque(maxlen=2048)
_lock = threading.Lock()
_field = re.compile(r'''(?i)(["']?(?:password|passwd|login_password|proxy_pass|cookie|cookies|cookies_str|cookie_value|token|access_token|refresh_token|authorization|secret|signature|x5sec|x5secdata|sgcookie|x-api-key|x-internal-token|_m_h5_tk(?:_enc)?)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,}&]+)''')


def register_secret(value):
    if not isinstance(value, str) or len(value) < 4:
        return
    with _lock:
        _values.append(value)
        if len(value) >= 20:
            _values.append(value[:20])
        if ";" in value:
            for pair in value.split(";"):
                if "=" in pair:
                    secret = pair.split("=", 1)[1].strip()
                    if len(secret) >= 8:
                        _values.append(secret)


def redact(value):
    text = str(value)
    with _lock:
        secrets = tuple(_values)
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(https?://[^\s?]+\?)[^\s\"']+", r"\1[REDACTED]", text)
    return _field.sub(lambda match: match[1] + "[REDACTED]", text)


def redact_record(record):
    record["message"] = redact(record["message"])
    record["extra"] = {key: redact(value) for key, value in record["extra"].items()}
    if record["exception"] is not None:
        # Exception values from HTTP/SQL/browser libraries can embed entire
        # credential payloads. Keep the type and sanitized message, not locals.
        error = record["exception"]
        record["message"] += f" | {error.type.__name__}: {redact(error.value)}"
        record["exception"] = None
