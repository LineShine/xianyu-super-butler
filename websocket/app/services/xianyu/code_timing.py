"""Local reception time for code flows; elapsed timers do not follow wall-clock jumps."""
from dataclasses import dataclass
import time

_WALL_ORIGIN_MS = int(time.time() * 1000)
_MONOTONIC_ORIGIN = time.monotonic()


def now_ms() -> int:
    """Persistent timer coordinate, anchored to local time at process startup."""
    return _WALL_ORIGIN_MS + int((time.monotonic() - _MONOTONIC_ORIGIN) * 1000)


@dataclass(frozen=True)
class Receipt:
    local_ms: int
    monotonic: float


def receipt(message: dict) -> Receipt:
    # This object is created internally, never taken from raw/forwarded JSON.
    value = message.get('_code_receipt')
    if not isinstance(value, Receipt):
        value = Receipt(int(time.time() * 1000), time.monotonic())
        message['_code_receipt'] = value
    return value


def is_recent(received: Receipt, max_age_ms: int) -> bool:
    return 0 <= (time.monotonic() - received.monotonic) * 1000 <= max_age_ms
