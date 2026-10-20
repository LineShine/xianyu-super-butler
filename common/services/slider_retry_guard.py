"""Durable, account-scoped stop after three completed slider failures."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from common.models.risk_control_log import YtxRiskControlLog
from common.models.ytx_account import YtxAccount

SLIDER_FAILURE_LIMIT = 3
SLIDER_GUARD_KEY = "slider_retry_guard"
MANUAL_RESUME_KEY = "manual_verification_resume"
SLIDER_PAUSE_REASON = "滑块验证连续失败3次，已暂停自动重试，请在闲鱼App完成验证后手动启用"


def slider_guard_state(account: YtxAccount) -> dict:
    return dict((account.metadata_json or {}).get(SLIDER_GUARD_KEY) or {})


def is_slider_paused(account: YtxAccount) -> bool:
    return bool(slider_guard_state(account).get("paused_at"))


async def account_slider_paused(account_id: str) -> bool:
    from common.db.session import async_session_maker

    async with async_session_maker() as session:
        account = (await session.execute(
            select(YtxAccount).where(YtxAccount.account_id == account_id)
        )).scalar_one_or_none()
        return bool(account and is_slider_paused(account))


async def reset_slider_guard(session, account: YtxAccount) -> None:
    """Enable/login or confirmed manual verification starts a new failure series."""
    await session.refresh(account, with_for_update=True)
    latest_id = (await session.execute(
        select(YtxRiskControlLog.id).where(
            YtxRiskControlLog.account_identifier == account.account_id
        ).order_by(YtxRiskControlLog.id.desc()).limit(1).with_for_update()
    )).scalar_one_or_none() or 0
    metadata = dict(account.metadata_json or {})
    # An explicit enable/login supersedes any older connection recovery task.
    metadata.pop("manual_verification_hold", None)
    metadata.pop(MANUAL_RESUME_KEY, None)
    metadata[SLIDER_GUARD_KEY] = {
        "consecutive_failures": 0,
        "resume_after_log_id": int(latest_id),
    }
    account.metadata_json = metadata


def apply_slider_result(account: YtxAccount, log: YtxRiskControlLog, previous_status: str) -> None:
    """Called under the account row lock, in the same transaction as the log."""
    if (
        log.event_type != "slider_captcha"
        or log.call_type != "local"
        or previous_status != "processing"
        or log.processing_status not in {"success", "failed", "error"}
        or account.status != "active"
        or is_slider_paused(account)
    ):
        return
    state = slider_guard_state(account)
    if log.id <= int(state.get("resume_after_log_id", 0)):
        return
    failures = 0 if log.processing_status == "success" else int(state.get("consecutive_failures", 0)) + 1
    state["consecutive_failures"] = min(failures, SLIDER_FAILURE_LIMIT)
    if failures >= SLIDER_FAILURE_LIMIT:
        state["paused_at"] = datetime.now(timezone.utc).isoformat()
        account.status = "disabled"
        account.disable_reason = SLIDER_PAUSE_REASON
    metadata = dict(account.metadata_json or {})
    metadata[SLIDER_GUARD_KEY] = state
    account.metadata_json = metadata


async def update_slider_log(session, log_id: int, values: dict) -> bool:
    """Serialize all completion writers per account; duplicate updates count once."""
    account_id = (await session.execute(
        select(YtxRiskControlLog.account_identifier).where(YtxRiskControlLog.id == log_id)
    )).scalar_one_or_none()
    account = (await session.execute(
        select(YtxAccount).where(YtxAccount.account_id == account_id).with_for_update()
    )).scalar_one_or_none() if account_id else None
    log = (await session.execute(
        select(YtxRiskControlLog).where(YtxRiskControlLog.id == log_id).with_for_update()
    )).scalar_one_or_none()
    if log is None:
        return False
    previous_status = log.processing_status
    for key, value in values.items():
        setattr(log, key, value)
    if account is not None:
        apply_slider_result(account, log, previous_status)
    return True
