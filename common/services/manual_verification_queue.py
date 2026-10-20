"""Durable FIFO for human CAPTCHA verification, shared by all local services.

The coordinator row contains no credentials. Challenges stay on their owning account,
are never returned by queue APIs, and are erased when the request finishes.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from common.db.session import async_session_maker
from common.models.risk_control_log import YtxRiskControlLog
from common.models.system_setting import SystemSetting
from common.models.ytx_account import YtxAccount
from common.utils.auth_scope import resolve_owner_scope
from common.utils.xianyu_utils import trans_cookies

QUEUE_KEY = "manual_verification_queue"
HOLD_KEY = "manual_verification_hold"
EVENT_TYPE = "manual_slider_queue"
LOCK_KEY = "internal.manual_verification_queue_lock"
SESSION_SECONDS = 60
PENDING = {"queued", "processing"}
PAUSE_REASON = "闲鱼触发滑块风控，已暂停该账号，等待手动验证"


def official_challenge(url: str) -> bool:
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        return (u.scheme == "https" and u.port in (None, 443)
                and not u.username and not u.password
                and any(host == d or host.endswith("." + d) for d in ("goofish.com", "taobao.com"))
                and any(p in u.path for p in ("punish", "captcha", "validate")))
    except ValueError:
        return False


def cookie_version(cookie: str) -> str:
    return hashlib.sha256(cookie.encode()).hexdigest()


def state_of(account) -> dict:
    return dict((account.metadata_json or {}).get(QUEUE_KEY) or {})


async def _lock(db):
    statement = insert(SystemSetting).values(key=LOCK_KEY, value="", description="手动验证队列事务锁")
    await db.execute(statement.on_duplicate_key_update(key=LOCK_KEY))
    await db.execute(select(SystemSetting).where(SystemSetting.key == LOCK_KEY).with_for_update())


async def held(account_id: str) -> bool:
    async with async_session_maker() as db:
        account = (await db.execute(select(YtxAccount).where(YtxAccount.account_id == account_id))).scalar_one_or_none()
        return bool(account and account.status == "disabled" and (account.metadata_json or {}).get(HOLD_KEY))


async def enqueue(account_id: str, *, url: str = "", device_id: str = "", user=None,
                  expected_cookie: str | None = None, response_cookies: dict | None = None) -> int | None:
    """Deduplicate per account; pause only the account that actually hit a challenge.

    Automated callers must supply an official challenge. A user may explicitly
    request a fresh challenge for an already disabled account.
    """
    if user is None and not official_challenge(url):
        return None
    scope = resolve_owner_scope(user)[0] if user is not None else None
    async with async_session_maker() as db:
        await _lock(db)
        account = (await db.execute(select(YtxAccount).where(
            YtxAccount.account_id == account_id).with_for_update())).scalar_one_or_none()
        if account is None or account.status == "deleted" or (scope is not None and account.owner_id != scope):
            if user is not None:
                raise HTTPException(404, "账号不存在或无权操作")
            return None
        state = state_of(account)
        if state.get("status") in PENDING:
            return int(state["log_id"])
        if user is not None and account.status != "disabled":
            raise HTTPException(409, "请先暂停该账号，再进行手动验证")
        if user is None and account.status != "active":
            return None  # A late in-flight response must not revive a stopped request.
        if expected_cookie is not None and account.cookie != expected_cookie:
            return None  # New login/Cookie wins over an older response.
        if user is not None and time.time() - state.get("requested_at", 0) < SESSION_SECONDS:
            raise HTTPException(429, "请间隔一分钟后再获取验证页面")
        if response_cookies:
            cookies = trans_cookies(account.cookie)
            cookies.update(response_cookies)
            if cookies.get("unb") != account.account_id:
                return None
            account.cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if account.status == "active":
            account.status = "disabled"
            account.disable_reason = PAUSE_REASON
        log = YtxRiskControlLog(
            owner_id=account.owner_id, account_pk=account.id, account_identifier=account.account_id,
            event_type=EVENT_TYPE, captcha_engine="manual", call_type="local", processing_status="queued",
            event_description="闲鱼滑块风控：等待用户手动验证" if user is None else "用户主动申请手动验证",
            processing_result="已进入手动验证队列，轮到此账号后最多等待一分钟",
        )
        db.add(log)
        await db.flush()
        meta = dict(account.metadata_json or {})
        meta[HOLD_KEY] = True
        meta[QUEUE_KEY] = {
            "id": uuid.uuid4().hex, "log_id": log.id, "status": "queued",
            "requested_at": time.time(), "expires_at": 0,
            "url": url if official_challenge(url) else "", "device_id": device_id,
            "cookie_version": cookie_version(account.cookie),
        }
        account.metadata_json = meta
        await db.commit()
        return int(log.id)


async def _finish(db, account, state: dict, status: str, message: str):
    log = await db.get(YtxRiskControlLog, state["log_id"])
    if log and log.processing_status in PENDING:
        log.processing_status, log.processing_result = status, message
    meta = dict(account.metadata_json or {})
    meta[QUEUE_KEY] = {k: state[k] for k in ("id", "log_id", "requested_at")}
    meta[QUEUE_KEY].update(status=status, expires_at=0)
    account.metadata_json = meta


async def finish(account_id: str, sid: str, status: str, message: str):
    async with async_session_maker() as db:
        await _lock(db)
        account = (await db.execute(select(YtxAccount).where(
            YtxAccount.account_id == account_id).with_for_update())).scalar_one_or_none()
        state = state_of(account) if account else {}
        if state.get("id") == sid and state.get("status") in PENDING:
            await _finish(db, account, state, status, message)
            await db.commit()


async def advance(*, user=None, client_id: str = "", claim_request: bool = False, worker_id: str = "") -> dict:
    """Activate one FIFO head, expire it without a browser/client, optionally claim.

    The singleton SQL row lock serializes enqueue/claim/expire across workers and
    service processes. A client claim is bound to both the operator and browser tab.
    """
    scope = resolve_owner_scope(user)[0] if user is not None else None
    async with async_session_maker() as db:
        await _lock(db)
        accounts = (await db.execute(select(YtxAccount).where(
            YtxAccount.metadata_json[QUEUE_KEY]["status"].as_string().in_(PENDING)
        ).with_for_update())).scalars().all()
        accounts = sorted(accounts, key=lambda a: int(state_of(a)["log_id"]))
        now = time.time()
        pending = []
        for account in accounts:
            state = state_of(account)
            if account.status != "disabled" or state.get("cookie_version") != cookie_version(account.cookie):
                await _finish(db, account, state, "cancelled", "账号状态或登录信息已变化，已退出验证队列")
            elif state["status"] == "processing" and state["expires_at"] <= now:
                await _finish(db, account, state, "cancelled", "手动验证一分钟超时，已跳过；该账号保持暂停，其他任务继续")
            else:
                pending.append(account)
        if not pending:
            await db.commit()
            return {"pending_count": 0, "available": False}
        account = pending[0]
        state = state_of(account)
        if state["status"] == "queued":
            state.update(status="processing", expires_at=now + SESSION_SECONDS)
            log = await db.get(YtxRiskControlLog, state["log_id"])
            if log:
                log.processing_status, log.processing_result = "processing", "等待用户手动拖动滑块，限时一分钟"
        visible = scope is None or account.owner_id == scope
        result = {"pending_count": sum(scope is None or a.owner_id == scope for a in pending), "available": False}
        if user is not None and visible:
            owned = not state.get("client_id") or (state.get("client_id") == client_id and state.get("operator_id") == user.id)
            result.update(available=owned, account_id=account.account_id,
                          account_name=account.remark or account.display_name or account.account_id,
                          expires_at=state["expires_at"])
            if client_id and owned and claim_request and (not state.get("worker_id") or state["worker_id"] == worker_id):
                state.update(client_id=client_id, operator_id=user.id, worker_id=worker_id)
                # Private server-to-server handoff. API callers must strip this field.
                result["claim"] = {**state, "account_pk": account.id, "owner_id": account.owner_id,
                                   "account_id": account.account_id, "cookie": account.cookie}
        meta = dict(account.metadata_json or {})
        meta[QUEUE_KEY] = state
        account.metadata_json = meta
        await db.commit()
        return result
