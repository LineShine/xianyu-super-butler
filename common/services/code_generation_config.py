"""Per-item command configuration, stored alongside existing item metadata."""
import time

COMMANDS = frozenset({"生成口令", "口令生成"})
ENABLED_KEY = "code_generation_enabled"
ENABLED_AT_KEY = "code_generation_enabled_at"
MAX_MESSAGE_AGE_MS = 5 * 60 * 1000
AUTOMATIC_ECHO_WINDOW_MS = 5_000


def enabled(metadata: dict | None) -> bool:
    return (metadata or {}).get(ENABLED_KEY) is True


def set_enabled(metadata: dict | None, value: bool) -> dict:
    result = dict(metadata or {})
    if value and not enabled(result):
        result[ENABLED_AT_KEY] = int(time.time() * 1000)
    result[ENABLED_KEY] = value
    if not value:
        result.pop(ENABLED_AT_KEY, None)
    return result
