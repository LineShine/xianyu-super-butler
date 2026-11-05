"""Seller-only commands: durable at-most-once claim, bounded script, fixed recipient."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from weakref import WeakValueDictionary

from loguru import logger
from sqlalchemy import select, update

from common.db.redis_client import distributed_lock
from common.db.session import async_session_maker
from common.models.auto_reply_message_log import YtxAutoReplyMessageLog as ReplyLog
from common.models.ytx_account import YtxAccount
from common.models.ytx_catalog_item import YtxCatalogItem
from common.models.ytx_order import YtxOrder
from common.services import code_generation_config as config
from app.services.xianyu import code_timing
from common.utils.xianyu_message_parser import decode_first_content, interpret_content

SCRIPT_PATH = Path(__file__).resolve().parents[4] / "common/custom_func/generate_code.py"
SCRIPT_TIMEOUT = 5.0
ACK_TIMEOUT = 10.0
_chat_locks: WeakValueDictionary = WeakValueDictionary()


def normalize_id(value) -> str:
    return str(value or "").removesuffix("@goofish")


def original_timestamp(message: dict) -> int | None:
    raw = (message.get("raw_message") or {}).get("1", {})
    value = raw.get("5") if isinstance(raw, dict) else None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        # IM sync timestamps are milliseconds. Do not invent a timestamp for old pushes.
        return int(number) if math.isfinite(number) and 1_000_000_000_000 <= number < 253402214400000 and number.is_integer() else None
    except (TypeError, ValueError, OverflowError):
        return None


def source_key(message: dict) -> str:
    body = message["raw_message"]["1"]
    mid = body.get("3")
    if not mid:
        for key in ("bizTag", "extJson"):
            info = body.get("10", {}).get(key)
            if isinstance(info, str):
                try:
                    info = json.loads(info)
                except ValueError:
                    continue
            if isinstance(info, dict) and info.get("messageId"):
                mid = info["messageId"]
                break
    if mid:
        mid = str(mid)
        return mid if len(mid) <= 128 else "id:" + hashlib.sha256(mid.encode()).hexdigest()
    # Use semantic fields, not transport wrappers that differ between native and Web forwarding.
    value = [normalize_id(message.get("chat_id")), normalize_id(message.get("send_user_id")),
             original_timestamp(message), message.get("send_message")]
    return "fp:" + hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _has_non_text_content(value) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            key = str(key).lower().replace("_", "")
            if any(part in key for part in ("quote", "replied", "replymessage", "refermessage", "dxcard")) or key in {"reply", "reference"}:
                if child:
                    return True
            if key in {"image", "audio", "video", "card", "atusers"} and child:
                return True
            if _has_non_text_content(child):
                return True
    elif isinstance(value, list):
        return any(_has_non_text_content(child) for child in value)
    elif isinstance(value, str) and value.startswith("{"):
        try:
            return _has_non_text_content(json.loads(value))
        except ValueError:
            pass
    return False


def is_seller_command(message: dict, seller_id: str) -> bool:
    if message.get("send_message") not in config.COMMANDS:
        return False
    if not seller_id or normalize_id(message.get("send_user_id")) != normalize_id(seller_id):
        return False
    return is_plain_chat_message(message)


def is_plain_chat_message(message: dict) -> bool:
    """Verify the decoded text and envelope; never promote previews, quotes or cards to commands."""
    if message.get("is_plain_text") is not True:
        return False
    raw = message.get("raw_message")
    if not isinstance(raw, dict) or not isinstance(raw.get("1"), dict) or raw.get("chatType"):
        return False
    body = raw["1"]
    info = body.get("10", {})
    if not isinstance(info, dict) or normalize_id(info.get("senderUserId")) != normalize_id(message.get("send_user_id")):
        return False
    if (body.get("conversationType", 1) not in (1, "1") or _has_non_text_content(body)
            or info.get("isAutoReply") or info.get("automatic")):
        return False
    payload = body.get("6", {}).get("3", {})
    content = decode_first_content([payload.get("5"), payload.get("1")])
    return bool(content and content.get("contentType") == 1
                and not _has_non_text_content(content)
                and interpret_content(content)[0] == message["send_message"]
                and normalize_id(body.get("2")) == normalize_id(message.get("chat_id"))
                and normalize_id(message.get("chat_id")))


@asynccontextmanager
async def conversation_lock(account_id: str, chat_id: str):
    """Self-service uses the same locks as manual commands, but never queues buyer submissions."""
    lock_key = (account_id, normalize_id(chat_id))
    local = _chat_locks.setdefault(lock_key, asyncio.Lock())
    if local.locked():
        yield None
        return
    async with local:
        key = hashlib.sha256(json.dumps(lock_key).encode()).hexdigest()
        async with distributed_lock(f"code_generation:{key}", expire=90, blocking=False) as lock:
            yield lock if lock.is_locked else None


class CommandStopped(Exception):
    def __init__(self, reason: str, detail: str):
        self.reason, self.detail = reason, detail
        super().__init__(detail)


@dataclass(frozen=True)
class Claim:
    log_id: int
    account_pk: int
    owner_id: int
    chat_id: str
    buyer_id: str
    item_id: str
    enabled_at: int
    timestamp_ms: int
    received: code_timing.Receipt


@dataclass(frozen=True)
class SendOutcome:
    status: str
    reason: str
    detail: str | None
    snapshot: dict


async def run_code_script() -> str:
    """No shell, no user argv; cancel/timeout always reap the child process."""
    process = None
    try:
        async with asyncio.timeout(SCRIPT_TIMEOUT):
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, str(SCRIPT_PATH), cwd=str(SCRIPT_PATH.parent),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            ))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Cancellation can race subprocess creation; recover the handle to reap it below.
                process = await spawn
                raise
            # Bound output as well as runtime; an accidental debug dump cannot be sent.
            output = b""
            while True:
                chunk = await process.stdout.read(64 - len(output))
                if not chunk:
                    break
                output += chunk
                if len(output) >= 64:
                    raise CommandStopped("code_invalid_output", "脚本输出超出六位口令格式")
            await process.wait()
            if process.returncode != 0:
                raise CommandStopped("code_script_failed", f"口令脚本退出码：{process.returncode}")
            if not re.fullmatch(rb"[A-Za-z0-9]{6}(?:\r?\n)?", output):
                raise CommandStopped("code_invalid_output", "脚本必须只输出六位大小写字母或数字")
            return output.rstrip(b"\r\n").decode("ascii")
    except TimeoutError as exc:
        raise CommandStopped("code_script_timeout", "口令脚本执行超过 5 秒，已停止") from exc
    except OSError as exc:
        raise CommandStopped("code_script_failed", f"无法启动口令脚本：{type(exc).__name__}") from exc
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


class CodeGenerationService:
    def __init__(self, account_id: str, instance):
        self.account_id = account_id
        self.instance = instance

    async def handle(self, message: dict, websocket) -> bool:
        seller = normalize_id(getattr(self.instance, "myid", ""))
        if not is_seller_command(message, seller):
            return False
        code_timing.receipt(message)
        chat_id = normalize_id(message["chat_id"])
        lock_key = (self.account_id, chat_id)
        local_lock = _chat_locks.setdefault(lock_key, asyncio.Lock())
        claim = None
        try:
            # Local FIFO avoids competing push handlers overtaking commands in one process.
            async with local_lock:
                key = hashlib.sha256(json.dumps(lock_key).encode()).hexdigest()
                async with distributed_lock(f"code_generation:{key}", expire=60, timeout=300) as lock:
                    if not lock.is_locked:
                        await self._claim(message, forced_error=CommandStopped("code_busy", "会话处理繁忙，请重新发送指令"))
                        return True
                    claim = await self._claim(message)
                    if claim is None:
                        return True
                    try:
                        # The finite pipeline is shorter than the Redis lease. Never retry a claimed command.
                        async with asyncio.timeout(30):
                            code = await self.generate_code(claim, lock)
                            await self._send(claim, code, websocket)
                    except CommandStopped as exc:
                        await self._finish(claim.log_id, process_status="failed", decision_reason=exc.reason,
                                           error_message=exc.detail, send_status="failed", send_fail_reason=exc.detail)
                    except TimeoutError:
                        await self._finish(claim.log_id, process_status="failed", decision_reason="code_send_unknown",
                                           error_message="处理超时；发送结果未知，不会自动补发", send_status="unknown")
        except asyncio.CancelledError:
            if claim:
                await self._finish(claim.log_id, process_status="failed", decision_reason="code_cancelled",
                                   error_message="任务已取消；不会自动补发，请先检查聊天记录", send_status="unknown")
            raise
        except Exception as exc:
            logger.error(f"【{self.account_id}】口令生成异常: {type(exc).__name__}")
            if claim:
                await self._finish(claim.log_id, process_status="failed", decision_reason="code_error",
                                   error_message=f"处理异常：{type(exc).__name__}；不会自动补发", send_status="unknown")
            else:
                # No committed claim => no script execution. Let the normal reply log record the error.
                raise
        return True

    async def generate_code(self, claim: Claim, lock, *, record_output: bool = True) -> str:
        """Internal trusted entry: caller must durably claim and authorize the fixed recipient first."""
        if not await lock.extend():
            raise CommandStopped("code_busy", "会话锁已失效，未执行脚本")
        await self._recheck(claim)
        code = await run_code_script()
        if not await lock.extend():
            raise CommandStopped("code_busy", "会话锁已失效，已停止发送")
        await self._recheck(claim)
        if record_output:
            await self._finish(claim.log_id, reply_text=code, reply_mode="text", reply_segments=[code])
        return code

    async def _claim(self, message: dict, forced_error: CommandStopped | None = None) -> Claim | None:
        chat_id, key = normalize_id(message["chat_id"]), source_key(message)
        timestamp = original_timestamp(message)
        received = code_timing.receipt(message)
        async with async_session_maker() as session:
            account = (await session.execute(select(YtxAccount).where(
                YtxAccount.account_id == self.account_id,
            ).with_for_update())).scalars().first()
            if account is None:
                raise CommandStopped("code_account_inactive", "账号已删除")
            # Current/locking read matters under MySQL REPEATABLE READ after another transaction commits.
            existing = (await session.execute(select(ReplyLog).where(
                ReplyLog.account_id == self.account_id, ReplyLog.chat_id == chat_id,
                ReplyLog.reply_strategy == "code_generation", ReplyLog.source_message_id == key,
            ).with_for_update())).scalars().first()
            if existing:
                # Holding the conversation lock means no previous command is still running.
                # A crash may have left a committed claim without a final result; never replay it.
                if existing.process_status == "processing" and forced_error is None:
                    existing.process_status = "failed"
                    existing.decision_reason = "code_send_unknown"
                    existing.send_status = "unknown"
                    existing.error_message = "上次处理已中断，结果未知；请先检查聊天记录，不会自动补发"
                    existing.send_fail_reason = existing.error_message
                    await session.commit()
                return None
            record = ReplyLog(
                owner_id=account.owner_id, account_pk=account.id, account_id=self.account_id,
                account_name=account.remark or account.display_name or self.account_id,
                chat_id=chat_id, source_message_id=key,
                sender_user_id=normalize_id(message["send_user_id"]),
                sender_user_name=message.get("send_user_name"), source_message=message["send_message"],
                source_message_time=datetime.fromtimestamp(timestamp / 1000) if timestamp and timestamp < 253402214400000 else None,
                reply_strategy="code_generation", matched_rule_type="code_generation", reply_mode="none",
                process_status="processing", decision_reason="processing", send_status="unknown",
                raw_message_json=message["raw_message"],
            )
            session.add(record)
            target = None
            try:
                if forced_error:
                    raise forced_error
                if timestamp is None:
                    raise CommandStopped("code_invalid_time", "消息缺少有效的原始毫秒时间，未执行脚本")
                if not code_timing.is_recent(received, config.MAX_MESSAGE_AGE_MS):
                    raise CommandStopped("code_stale", "只处理本机最近 5 分钟内接收的指令")
                if account.status != "active" or normalize_id(account.unb or account.account_id) != normalize_id(message["send_user_id"]):
                    raise CommandStopped("code_account_inactive", "当前卖家账号未启用或身份不符")
                if await self._is_recorded_automatic_echo(session, message, timestamp):
                    raise CommandStopped("automatic_message_echo", "自动消息回传，不执行口令指令")
                item_id, buyer_id = await self._resolve_target(session, account, message)
                record.item_id = item_id
                items = (await session.execute(select(YtxCatalogItem).where(
                    YtxCatalogItem.owner_id == account.owner_id, YtxCatalogItem.account_pk == account.id,
                    YtxCatalogItem.item_id == item_id,
                ).with_for_update())).scalars().all()
                if not items:
                    raise CommandStopped("code_context", "当前账号没有此商品，未执行脚本")
                if any(not config.enabled(item.metadata_json) for item in items):
                    raise CommandStopped("code_disabled", "该商品未开启口令生成")
                if any(type(item.metadata_json.get(config.ENABLED_AT_KEY)) is not int for item in items):
                    raise CommandStopped("code_stale", "商品缺少有效的口令开启版本")
                since = items[0].metadata_json[config.ENABLED_AT_KEY]
                record.item_title = items[0].title
                record.context_snapshot = {"buyer_id": buyer_id, "item_id": item_id, "enabled_at": since,
                                           "source_timestamp_ms": timestamp, "received_local_ms": received.local_ms, "claim": "at_most_once"}
                target = (buyer_id, item_id, since)
            except CommandStopped as exc:
                record.process_status = "skipped"
                record.decision_reason = exc.reason
                record.error_message = exc.detail
            await session.flush()
            log_id, account_pk, owner_id = record.id, account.id, account.owner_id
            await session.commit()  # Never launch a child until the durable claim succeeds.
            if target is None:
                return None
            return Claim(log_id, account_pk, owner_id, chat_id, *target, timestamp, received)

    async def _resolve_target(self, session, account, message: dict) -> tuple[str, str]:
        chat = normalize_id(message["chat_id"])
        seller = normalize_id(message["send_user_id"])
        info = message["raw_message"]["1"]["10"]
        buyers = {normalize_id(info[key]) for key in ("receiverUserId", "toUserId", "buyerId") if info.get(key)}
        buyers.discard(seller)
        explicit_items = {str(message["item_id"])} if message.get("item_id") else set()
        for key in ("bizTag", "extJson"):
            value = info.get(key)
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = None
            if isinstance(value, dict) and value.get("itemId"):
                explicit_items.add(str(value["itemId"]))
        if isinstance(info.get("reminderUrl"), str):
            explicit_items.update(parse_qs(urlsplit(info["reminderUrl"]).query).get("itemId", []))
        if len(explicit_items) > 1:
            raise CommandStopped("code_context", "消息中的商品信息不一致，已停止")
        items = set()
        # Only this account's exact single-chat context is eligible. Never use the active Web tab.
        rows = (await session.execute(select(ReplyLog.sender_user_id, ReplyLog.item_id).where(
            ReplyLog.account_pk == account.id, ReplyLog.owner_id == account.owner_id,
            ReplyLog.account_id == self.account_id, ReplyLog.chat_id.in_([chat, f"{chat}@goofish"]),
            ReplyLog.sender_user_id.notin_([seller, f"{seller}@goofish", "unknown", "", "0"]),
            ReplyLog.decision_reason.notin_(["system_message", "auto_delivery_trigger"]),
        ).distinct())).all()
        orders = (await session.execute(select(YtxOrder.buyer_id, YtxOrder.item_id).where(
            YtxOrder.account_id == self.account_id, YtxOrder.owner_id == account.owner_id,
            YtxOrder.chat_id.in_([chat, f"{chat}@goofish"]),
        ).distinct())).all()
        for buyer, item in [*rows, *orders]:
            buyer = normalize_id(buyer)
            if buyer.isdigit() and buyer != "0" and buyer != seller:
                buyers.add(buyer)
                if item:
                    items.add(str(item))
        if explicit_items:
            items = explicit_items
        if len(buyers) != 1 or len(items) != 1 or not next(iter(buyers)).isdigit():
            raise CommandStopped("code_context", "无法确认本账号同一会话的唯一商品和买家，已停止；请先在商品会话中与买家聊天")
        return items.pop(), buyers.pop()

    async def _is_recorded_automatic_echo(self, session, message, timestamp) -> bool:
        # On restart the in-memory echo tracker is empty. Persisted outgoing reply logs
        # conservatively exclude command-shaped automatic replies near their original send time.
        from common.models.code_self_service import CodeSelfServiceAttempt
        # Persist the visible automatic command before dispatch, so even a process crash
        # cannot turn its echo into a new manual command.
        automatic = (await session.execute(select(CodeSelfServiceAttempt.id).where(
            CodeSelfServiceAttempt.account_pk == select(YtxAccount.id).where(
                YtxAccount.account_id == self.account_id).scalar_subquery(),
            CodeSelfServiceAttempt.chat_id == normalize_id(message["chat_id"]),
            CodeSelfServiceAttempt.marker_at_ms.between(
                timestamp - config.AUTOMATIC_ECHO_WINDOW_MS, timestamp + config.AUTOMATIC_ECHO_WINDOW_MS),
        ).limit(1))).first()
        if automatic:
            return True
        sent_at = datetime.fromtimestamp(code_timing.receipt(message).local_ms / 1000)
        row = (await session.execute(select(ReplyLog.id).where(
            ReplyLog.account_id == self.account_id,
            ReplyLog.chat_id == normalize_id(message["chat_id"]),
            ReplyLog.reply_strategy.notin_(["code_generation", "none"]),
            ReplyLog.reply_text == message["send_message"],
            ReplyLog.created_at.between(sent_at - timedelta(milliseconds=config.AUTOMATIC_ECHO_WINDOW_MS),
                                       sent_at + timedelta(milliseconds=config.AUTOMATIC_ECHO_WINDOW_MS)),
        ).limit(1))).first()
        return row is not None

    async def _recheck(self, claim: Claim) -> None:
        async with async_session_maker() as session:
            account = (await session.execute(select(YtxAccount).where(
                YtxAccount.id == claim.account_pk, YtxAccount.account_id == self.account_id,
                YtxAccount.owner_id == claim.owner_id,
            ))).scalars().first()
            if (account is None or account.status != "active"
                    or normalize_id(account.unb or account.account_id) != normalize_id(self.instance.myid)):
                raise CommandStopped("code_account_inactive", "执行期间账号已停用，未发送口令")
            items = (await session.execute(select(YtxCatalogItem).where(
                YtxCatalogItem.account_pk == claim.account_pk, YtxCatalogItem.owner_id == claim.owner_id,
                YtxCatalogItem.item_id == claim.item_id,
            ))).scalars().all()
            if not items or any(not config.enabled(item.metadata_json) for item in items):
                raise CommandStopped("code_disabled", "执行期间商品已关闭口令生成或被删除，未发送口令")
            if any(item.metadata_json.get(config.ENABLED_AT_KEY) != claim.enabled_at or
                   not code_timing.is_recent(claim.received, config.MAX_MESSAGE_AGE_MS) for item in items):
                raise CommandStopped("code_stale", "商品已重新开启或指令已过期，未发送口令")

    def check_connection(self, websocket) -> None:
        connection = getattr(self.instance, "connection_manager", None)
        if (getattr(self.instance, "ws", websocket) is not websocket
                or getattr(self.instance, "_registered_ws", websocket) is not websocket
                or (connection is not None and connection.is_ws_closed(websocket))):
            raise CommandStopped("code_account_inactive", "账号消息连接已断开或发生变化，未发送口令")

    async def send_text_confirmed(self, claim: Claim, text: str, websocket) -> SendOutcome:
        self.check_connection(websocket)
        result = await self.instance.send_msg(websocket, claim.chat_id, claim.buyer_id, text)
        result = result if isinstance(result, dict) else {}
        snapshot = {key: value for key, value in result.items() if key != "send_future"}
        future, mid = result.get("send_future"), result.get("mid")
        status, reason, detail = "unknown", "code_send_unknown", "未收到闲鱼明确确认，不会自动补发；请先检查聊天记录"
        try:
            if result.get("success") is False and not result.get("delivery_uncertain"):
                status, reason, detail = "failed", "code_send_failed", result.get("error_message") or "消息发送失败"
            elif future is not None:
                response = await asyncio.wait_for(asyncio.shield(future), ACK_TIMEOUT)
                snapshot["ack"] = response
                rejected = self.instance._extract_send_reject_reason(response)
                if rejected:
                    status, reason, detail = "failed", "code_send_failed", rejected
                elif isinstance(response, dict) and str(response.get("code")) == "200":
                    status, reason, detail = "success", "code_sent", None
                elif isinstance(response, dict) and response.get("code") is not None:
                    status, reason, detail = "failed", "code_send_failed", f"闲鱼发送响应码：{response['code']}"
        except (TimeoutError, ConnectionError):
            pass
        finally:
            if mid:
                pending = getattr(self.instance, "_pending_mid_futures", {})
                if pending.get(mid) is future:
                    pending.pop(mid, None)
            if future is not None and not future.done():
                future.cancel()
        return SendOutcome(status, reason, detail, snapshot)

    async def _send(self, claim: Claim, code: str, websocket) -> None:
        outcome = await self.send_text_confirmed(claim, code, websocket)
        await self._finish(claim.log_id, process_status="success" if outcome.status == "success" else "failed",
                           decision_reason=outcome.reason, send_status=outcome.status, send_fail_reason=outcome.detail,
                           error_message=outcome.detail, send_result_json=outcome.snapshot)

    async def _finish(self, log_id: int, **values) -> None:
        async with async_session_maker() as session:
            await session.execute(update(ReplyLog).where(
                ReplyLog.id == log_id, ReplyLog.account_id == self.account_id,
                ReplyLog.reply_strategy == "code_generation",
            ).values(**values))
            await session.commit()
