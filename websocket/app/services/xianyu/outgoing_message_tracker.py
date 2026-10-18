"""Match short-lived automatic message echoes without retaining their contents."""
import hashlib
import time
from collections import OrderedDict
from uuid import uuid4


class OutgoingMessageTracker:
    def __init__(self, ttl=120, max_entries=2000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._pending = OrderedDict()

    @staticmethod
    def _key(account_id, chat_id, content):
        return (str(account_id), str(chat_id).split('@')[0],
                hashlib.sha256(str(content).encode('utf-8')).digest())

    def _prune(self):
        now = time.monotonic()
        while self._pending:
            token, (_, recorded_at, _) = next(iter(self._pending.items()))
            if now - recorded_at < self.ttl:
                break
            self._pending.pop(token)

    def remember(self, account_id, chat_id, content):
        self._prune()
        token = uuid4().hex
        self._pending[token] = (self._key(account_id, chat_id, content),
                                time.monotonic(), time.time())
        while len(self._pending) > self.max_entries:
            self._pending.popitem(last=False)
        return token

    def discard(self, token):
        self._pending.pop(token, None)

    def consume_echo(self, account_id, chat_id, content, timestamp_ms=None):
        self._prune()
        key = self._key(account_id, chat_id, content)
        try:
            message_time = float(timestamp_ms) / 1000 if timestamp_ms else None
        except (TypeError, ValueError):
            message_time = None
        for token, (expected, _, sent_at) in self._pending.items():
            # A delayed push from before this send is not its echo. Allow small
            # clock skew between the local machine and the IM server.
            if key == expected and (message_time is None or message_time >= sent_at - 2):
                self._pending.pop(token)
                return True
        return False


outgoing_message_tracker = OutgoingMessageTracker()
