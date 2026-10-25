"""Account-scoped lease for background login/renewal across service processes.

The verification queue may pause an in-flight renewal, but it waits for its
browser to finish before opening a human session. Late Cookie writes are rejected.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from contextvars import ContextVar

from sqlalchemy import select

from common.db.session import async_session_maker
from common.models.ytx_account import YtxAccount

RENEWAL_KEY = "credential_renewal_lease"
LEASE_SECONDS = 120
_current = ContextVar("credential_renewal", default=None)


def verification_holds_credentials(account) -> bool:
    meta = account.metadata_json or {}
    return bool(meta.get("manual_verification_hold") or
                (meta.get("manual_verification_queue") or {}).get("status") in {"queued", "processing"})


def renewal_in_progress(account) -> bool:
    return (account.metadata_json or {}).get(RENEWAL_KEY, {}).get("expires_at", 0) > time.time()


async def verification_pending(account_id: str) -> bool:
    async with async_session_maker() as db:
        account = (await db.execute(select(YtxAccount).where(
            YtxAccount.account_id == account_id
        ))).scalar_one_or_none()
        return bool(account and verification_holds_credentials(account))


async def _lease(account_id: str, lease_id: str, action: str) -> bool:
    async with async_session_maker() as db:
        account = (await db.execute(select(YtxAccount).where(
            YtxAccount.account_id == account_id
        ).with_for_update())).scalar_one_or_none()
        if account is None:
            return False
        meta = dict(account.metadata_json or {})
        current = meta.get(RENEWAL_KEY) or {}
        if action == "acquire":
            if verification_holds_credentials(account) or renewal_in_progress(account):
                return False
        elif current.get("id") != lease_id:
            return False
        if action == "release":
            meta.pop(RENEWAL_KEY, None)
        else:
            meta[RENEWAL_KEY] = {"id": lease_id, "expires_at": time.time() + LEASE_SECONDS}
        account.metadata_json = meta
        await db.commit()
        return True


@contextlib.asynccontextmanager
async def credential_renewal(account_id: str):
    """Nested calls in the same task share one lease; other accounts stay independent."""
    task = asyncio.current_task()
    current = _current.get()
    if current and current[0] == account_id and current[1] is task:
        yield True
        return
    lease_id = uuid.uuid4().hex
    if not await _lease(account_id, lease_id, "acquire"):
        yield False
        return
    token = _current.set((account_id, task))

    async def keep_alive():
        try:
            while True:
                await asyncio.sleep(30)
                if not await _lease(account_id, lease_id, "extend"):
                    task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Stop requests when ownership cannot be maintained.
            task.cancel()

    heartbeat = asyncio.create_task(keep_alive())
    try:
        yield True
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        _current.reset(token)
        await asyncio.shield(_lease(account_id, lease_id, "release"))
