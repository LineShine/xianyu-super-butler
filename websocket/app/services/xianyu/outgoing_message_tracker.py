"""Match automatic echoes, using platform message IDs for seller commands."""
import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from uuid import uuid4


def _identifier(value):
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and value else None


def incoming_message_identity(message):
    """Read the platform message ID; request mid and client uuid are different IDs."""
    raw = message.get('raw_message', message) if isinstance(message, dict) else {}
    body = raw.get('1', {}) if isinstance(raw, dict) else {}
    if not isinstance(body, dict):
        return None, None
    message_id = _identifier(body.get('3'))
    info = body.get('10', {})
    if not message_id and isinstance(info, dict):
        for name in ('bizTag', 'extJson'):
            value = info.get(name)
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    continue
            if isinstance(value, dict):
                message_id = _identifier(value.get('messageId'))
                if message_id:
                    break
    return message_id, None


def _snapshot_message_ids(snapshot):
    if isinstance(snapshot, list):
        return set().union(*(_snapshot_message_ids(part) for part in snapshot))
    if not isinstance(snapshot, dict):
        return set()
    result = {_identifier(snapshot.get('messageId'))}
    ack = snapshot.get('ack')
    body = ack.get('body') if isinstance(ack, dict) else None
    if isinstance(body, dict):
        result.add(_identifier(body.get('messageId')))
        # Some IM responses wrap the platform message model in body['1'].
        nested = body.get('1')
        if isinstance(nested, dict):
            result.add(_identifier(nested.get('messageId')))
    result.discard(None)
    return result


def snapshot_matches_message(snapshot, message):
    message_id = message if isinstance(message, str) else incoming_message_identity(message)[0]
    return bool(message_id and not message_id.startswith('fp:')
                and message_id in _snapshot_message_ids(snapshot))


@dataclass
class _PendingSend:
    key: tuple
    recorded_at: float
    sent_at: float
    request_mid: str | None = None
    ack_future: object = None
    response: dict | None = None


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
            token, pending = next(iter(self._pending.items()))
            if now - pending.recorded_at < self.ttl:
                break
            self._pending.pop(token)

    def remember(self, account_id, chat_id, content, *, request_mid=None, ack_future=None):
        self._prune()
        token = uuid4().hex
        self._pending[token] = _PendingSend(self._key(account_id, chat_id, content),
                                            time.monotonic(), time.time(), request_mid, ack_future)
        while len(self._pending) > self.max_entries:
            self._pending.popitem(last=False)
        return token

    def discard(self, token):
        self._pending.pop(token, None)

    def record_response(self, account_id, response):
        """Bind request ACKs before waking early echoes, including late ACKs."""
        if not isinstance(response, dict):
            return
        headers = response.get('headers')
        mid = headers.get('mid') if isinstance(headers, dict) else None
        if not mid:
            return
        self._prune()
        for pending in self._pending.values():
            if pending.key[0] == str(account_id) and pending.request_mid == mid:
                pending.response = response

    @staticmethod
    def _matches(pending, message):
        response = pending.response
        future = pending.ack_future
        if response is None and future is not None and future.done() and not future.cancelled():
            try:
                response = future.result()
            except Exception:
                pass
        return snapshot_matches_message({'ack': response}, message)

    async def matches_command_echo(self, account_id, chat_id, content, message, *, wait_timeout=10):
        """Never classify a distinct seller message by matching text or elapsed time."""
        if not incoming_message_identity(message)[0]:
            return False
        self._prune()
        key = self._key(account_id, chat_id, content)
        candidates = [pending for pending in self._pending.values() if pending.key == key]
        if any(self._matches(pending, message) for pending in candidates):
            return True
        futures = {asyncio.shield(pending.ack_future) for pending in candidates
                   if pending.ack_future is not None and not pending.ack_future.done()}
        deadline = asyncio.get_running_loop().time() + wait_timeout
        try:
            while futures:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                done, futures = await asyncio.wait(futures, timeout=remaining,
                                                   return_when=asyncio.FIRST_COMPLETED)
                for future in done:
                    if not future.cancelled():
                        future.exception()
                if any(self._matches(pending, message) for pending in candidates):
                    return True
            return False
        finally:
            # Cancel only shielding wrappers; the send's ACK wait must remain alive.
            for future in futures:
                future.cancel()

    def consume_echo(self, account_id, chat_id, content, timestamp_ms=None, *, max_age_seconds=None):
        self._prune()
        key = self._key(account_id, chat_id, content)
        try:
            message_time = float(timestamp_ms) / 1000 if timestamp_ms else None
        except (TypeError, ValueError):
            message_time = None
        for token, pending in self._pending.items():
            if max_age_seconds is not None and time.monotonic() - pending.recorded_at > max_age_seconds:
                continue
            # A delayed push from before this send is not its echo. Allow small
            # clock skew between the local machine and the IM server.
            if key == pending.key and (message_time is None or message_time >= pending.sent_at - 2):
                self._pending.pop(token)
                return True
        return False


outgoing_message_tracker = OutgoingMessageTracker()
