"""Detect missing buyer messages or seller commands and recover the account.

History is evidence only: never dispatch it to automation or mark messages read.
Use server message timestamps for the startup boundary, monotonic time for grace.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict

from loguru import logger

from common.utils.xianyu_message_parser import load_content_json, interpret_content
from common.utils.xianyu_utils import generate_mid
from common.services.code_generation_config import COMMANDS

OFFLINE_REASON = '消息接收异常，自动回复和实时发货可能失效，请重新登录此账号'
INTERVAL = 30
GRACE = 60
LOGIN_RETRY_INTERVAL = 60
LOGIN_WAIT_LIMIT = 600


def object_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


class MessageSyncHealth:
    def __init__(self, instance):
        self.instance = instance
        self.unhealthy = False
        self.failure_timestamp = 0
        self.failure_directions = set()
        self.boundary = None
        self.seen = OrderedDict()
        self.conversations = OrderedDict()
        self.pending = OrderedDict()
        self.task = None
        self.recovery_attempts = 0
        self.password_login_attempts = 0
        self.recovery_stage = ''
        self.detail = ''
        self.verification_url = ''
        self.screenshot_path = ''
        self.login_retry_at = None
        self.login_wait_started = None
        self.awaiting_rebuild = False
        self.verification_boundary_pending = False
        self.gap_detected = False
        self.events = []

    def status(self):
        stage = ('unhealthy' if self.unhealthy else self.recovery_stage or
                 ('recovering' if self.recovery_attempts else 'monitoring'))
        return {
            'message_sync_status': stage,
            'message_sync_retries': self.recovery_attempts,
            'message_sync_password_logins': self.password_login_attempts,
            'message_sync_detail': self.detail,
            'message_sync_verification_url': self.verification_url,
            'message_sync_screenshot_path': self.screenshot_path,
            'relogin_required': self.unhealthy,
            'offline_reason': (self.detail or OFFLINE_REASON) if self.unhealthy else '',
        }

    def recovery_snapshot(self):
        """Carry an incident across automatic instance replacement, never its task/socket."""
        return {name: getattr(self, name).copy() if isinstance(getattr(self, name), (set, list))
                else getattr(self, name) for name in (
                    'unhealthy', 'failure_timestamp', 'failure_directions', 'recovery_attempts',
                    'password_login_attempts', 'recovery_stage', 'detail', 'events',
                    'login_retry_at', 'login_wait_started', 'awaiting_rebuild')}

    def restore_recovery(self, snapshot):
        for name, value in snapshot.items():
            setattr(self, name, value)
        if self.awaiting_rebuild:
            self.awaiting_rebuild = False
            self.verification_boundary_pending = True
            self.recovery_stage = 'verifying'
            self.detail = '账号实例已重建，等待对应方向的新消息验证接收'
            self.events.append(('success', self.detail))

    def require_verification(self, message, screenshot_path=None, verification_url=None):
        # Called on the event loop, not directly by the browser worker thread.
        self.recovery_stage = 'verification_required'
        self.detail = '自动账密登录需要人脸或安全验证，请完成验证后等待登录继续'
        self.screenshot_path = screenshot_path or ''
        self.verification_url = verification_url or ''
        self.events.append(('processing', self.detail))

    def mark_unhealthy(self, reason):
        self.unhealthy = True
        self.awaiting_rebuild = False
        self.recovery_stage = ''
        self.login_retry_at = None
        self.detail = reason
        self.verification_url = self.screenshot_path = ''
        self.events.append(('failed', reason))

    def reset_baseline(self):
        self.gap_detected = False
        self.pending.clear()
        self.conversations.clear()
        self.boundary = None

    def observe(self, raw):
        body = object_value(raw.get('1'))
        info = object_value(body.get('10'))
        sender = str(info.get('senderUserId') or '').split('@')[0]
        if not sender.isdigit() or sender == '0':
            return
        cid, mid = str(body.get('2') or '').split('@')[0], str(body.get('3') or '')
        if not cid or not mid:
            return
        key = (cid, mid)
        self.seen[key] = True
        self.seen.move_to_end(key)
        while len(self.seen) > 20000:
            self.seen.popitem(last=False)
        self.pending.pop(key, None)
        try:
            stamp = int(body.get('5') or 0)
        except (ValueError, TypeError):
            return
        direction = 'seller' if sender == str(self.instance.myid) else 'buyer'
        if (not self.awaiting_rebuild and not self.verification_boundary_pending
                and (self.unhealthy or self.recovery_attempts) and stamp > self.failure_timestamp
                and (not self.failure_directions or direction in self.failure_directions)):
            self.failure_directions.discard(direction)
            if self.failure_directions:
                return
            self.unhealthy = False
            self.events.append(('success', f'自动重连 {self.recovery_attempts} 次、'
                                f'账密登录 {self.password_login_attempts} 次后已确认收到新的聊天消息'))
            self.recovery_attempts = 0
            self.password_login_attempts = 0
            self.recovery_stage = self.detail = ''
            self.verification_url = self.screenshot_path = ''
            self.login_retry_at = self.login_wait_started = None
            self.gap_detected = False
            self.pending.clear()
            logger.info(f'【{self.instance.cookie_id}】消息正文接收已恢复')

    async def request(self, ws, path, body):
        mid = generate_mid()
        future = asyncio.get_running_loop().create_future()
        self.instance._pending_mid_futures[mid] = future
        try:
            await ws.send(json.dumps({'lwp': path, 'headers': {'mid': mid}, 'body': body}))
            response = await asyncio.wait_for(future, timeout=10)
            result = object_value(response.get('body'))
            if str(response.get('code')) != '200' or result.get('reason'):
                raise ConnectionError('IM health query rejected')
            return result
        finally:
            self.instance._pending_mid_futures.pop(mid, None)
            if not future.done():
                future.cancel()

    def candidates(self, body):
        if not isinstance(body.get('userConvs'), list):
            raise ValueError('Missing conversation list')
        entries = []
        for row in body['userConvs']:
            conv = object_value(object_value(row).get('singleChatUserConversation', row))
            single = object_value(conv.get('singleChatConversation'))
            cid = str(single.get('cid') or '').split('@')[0]
            msg = object_value(object_value(conv.get('lastMessage')).get('message'))
            try:
                stamp = int(msg.get('createAt') or msg.get('time') or 0)
            except (ValueError, TypeError):
                continue
            if cid and msg.get('messageId') and stamp > 0:
                entries.append((cid, str(msg['messageId']), stamp))
        return entries

    def check_history(self, cid, body):
        models = body.get('userMessageModels')
        if not isinstance(models, list):
            raise ValueError('Missing message history')
        now = time.monotonic()
        confirmed = set()
        for model in models:
            msg = object_value(object_value(model).get('message'))
            ext = object_value(msg.get('extension'))
            sender = str(ext.get('senderUserId') or '').split('@')[0]
            mid = str(msg.get('messageId') or '')
            try:
                stamp = int(msg.get('createAt') or msg.get('time') or 0)
            except (ValueError, TypeError):
                continue
            if not mid or not sender.isdigit() or sender == '0':
                continue
            # Seller commands also require cross-device delivery. History remains
            # evidence only; it must never execute or replay a command.
            custom = object_value(object_value(msg.get('content')).get('custom'))
            decoded = load_content_json(custom.get('data'))
            biz = object_value(ext.get('extJson'))
            if (not decoded or interpret_content(decoded)[2] not in ('text', 'image')
                    or biz.get('msgArg1') == 'MsgTips' or ext.get('isAutoReply') or ext.get('automatic')
                    or stamp <= (self.boundary or 0)):
                continue
            direction = 'seller' if sender == str(self.instance.myid) else 'buyer'
            if direction == 'seller' and (decoded.get('contentType') != 1
                    or interpret_content(decoded)[0] not in COMMANDS):
                continue
            key = (cid, mid)
            confirmed.add(key)
            if key in self.seen:
                self.pending.pop(key, None)
                continue
            first, count, _ = self.pending.get(key, (now, 0, stamp))
            self.pending[key] = (first, count + 1, stamp)
            if count >= 1 and now - first >= GRACE and not self.unhealthy:
                self.gap_detected = True
                self.failure_timestamp = max(self.failure_timestamp, stamp)
                self.failure_directions.add(direction)
                logger.warning(f'【{self.instance.cookie_id}】消息接收异常: chat_id={cid}, '
                               f'message_id={mid}, 多次查询确认新消息未进入实时处理')
        # Recalled/removed messages no longer prove missing delivery.
        for key in list(self.pending):
            if key[0] == cid and key not in confirmed:
                self.pending.pop(key, None)
        while len(self.pending) > 200:
            self.pending.popitem(last=False)

    async def poll(self, ws):
        body = await self.request(ws, '/r/Conversation/listNewestPagination', [9007199254740991, 20])
        entries = self.candidates(body)
        if self.boundary is None:
            if not entries:
                return  # No clock-based guesses for an empty/new account.
            self.boundary = max(stamp for _, _, stamp in entries)
            self.verification_boundary_pending = False
            if self.recovery_attempts:
                self.failure_timestamp = max(self.failure_timestamp, self.boundary)
            self.conversations.update((cid, mid) for cid, mid, _ in entries)
            logger.info(f'【{self.instance.cookie_id}】消息健康检查基线已建立: conversations={len(entries)}')
            return
        changed = [cid for cid, mid, stamp in entries
                   if stamp > self.boundary and self.conversations.get(cid) != mid]
        # Pending evidence is rechecked even when the last message stays unchanged.
        chats = list(dict.fromkeys([key[0] for key in self.pending] + changed))[:5]
        for cid in chats:
            history = await self.request(ws, '/r/MessageManager/listUserMessages',
                                         [f'{cid}@goofish', False, 9007199254740991, 20, False])
            self.check_history(cid, history)
        for cid, mid, stamp in entries:
            if cid in chats or stamp <= self.boundary:
                self.conversations[cid] = mid
                self.conversations.move_to_end(cid)
        while len(self.conversations) > 2000:
            self.conversations.popitem(last=False)

    async def write_risk_log(self, status, result):
        from sqlalchemy import select
        from common.db.session import async_session_maker
        from common.models.ytx_account import YtxAccount
        from common.models.risk_control_log import YtxRiskControlLog
        async with async_session_maker() as db:
            account = (await db.execute(select(YtxAccount).where(
                YtxAccount.account_id == self.instance.cookie_id))).scalars().first()
            if account:
                db.add(YtxRiskControlLog(
                    owner_id=account.owner_id, account_pk=account.id,
                    account_identifier=self.instance.cookie_id, event_type='message_sync',
                    event_description='消息接收异常：聊天记录存在新买家消息或卖家口令指令，但实时消息链路未收到',
                    processing_status=status, processing_result=result,
                    error_message=result if status == 'failed' else None, call_type='local'))
                await db.commit()

    async def flush_events(self):
        while self.events:
            status, result = self.events[0]
            await self.write_risk_log(status, result)
            self.events.pop(0)

    async def recover(self):
        if self.unhealthy or self.awaiting_rebuild:
            return
        self.gap_detected = False
        while self.recovery_attempts < 2:
            self.recovery_attempts += 1
            attempt = self.recovery_attempts
            try:
                ok = await asyncio.wait_for(self.instance._reconnect_with_new_token(), timeout=75)
            except Exception:
                ok = False
            if self.recovery_attempts == 0:
                return  # A fresh push already verified recovery while reconnecting.
            self.events.append(('success' if ok else 'failed',
                f'第 {attempt}/2 次自动重连：' +
                ('连接已恢复，等待对应方向的新消息验证接收' if ok else '连接恢复失败')))
            if ok:
                # Reconnection need not replay old messages. Only new misses may
                # fail this attempt; an idle account remains pending verification.
                self.reset_baseline()
                return
        await self.recover_with_password()

    async def recover_with_password(self):
        if self.password_login_attempts:
            self.mark_unhealthy('两次 Token 重连和一次账密登录后仍漏收新消息，请手动重新登录并检查账号')
            return
        if self.login_retry_at is not None and time.monotonic() < self.login_retry_at:
            return
        if self.login_wait_started is None:
            self.login_wait_started = time.monotonic()
        elif time.monotonic() - self.login_wait_started >= LOGIN_WAIT_LIMIT:
            self.mark_unhealthy('自动账密登录等待超过 10 分钟，请检查正在进行的登录或验证，并手动重新登录')
            return
        self.login_retry_at = None
        self.password_login_attempts = 1
        self.awaiting_rebuild = True  # Old-socket pushes cannot verify the replacement.
        self.recovery_stage = 'relogging_in'
        self.detail = '两次 Token 重连后仍漏收消息，正在使用已保存账密重新登录'
        self.events.append(('processing', self.detail))
        try:
            result = await self.instance._cookie_token_manager.try_password_login_refresh(
                '消息漏收，两次 Token 重连未恢复', force_password_login=True,
                on_verification=self.require_verification)
        except asyncio.CancelledError:
            if not getattr(self.instance, '_instance_restart_scheduled', False):
                self.mark_unhealthy('自动账密登录已中断，请检查登录任务或手动重新登录')
            raise
        except Exception as exc:
            logger.warning(f'【{self.instance.cookie_id}】消息恢复账密登录异常: {type(exc).__name__}')
            result = False
        if result is True:
            # The scheduled replacement copies this state after stopping this task.
            # Registration/heartbeats alone must not turn the account healthy.
            self.recovery_stage = 'verifying'
            self.detail = '账密登录成功，Cookie 已更新，等待账号实例重建和新消息验证'
            self.verification_url = self.screenshot_path = ''
            self.events.append(('success', self.detail))
            return
        self.awaiting_rebuild = False
        if result == 'skipped_cooldown':
            # No password was submitted. Retry lease/cooldown contention, bounded
            # independently of history activity (an idle account must not stall it).
            self.password_login_attempts = 0
            self.login_retry_at = time.monotonic() + LOGIN_RETRY_INTERVAL
            self.recovery_stage = 'recovering'
            self.detail = '登录正在冷却或其他登录任务进行中，稍后自动重试'
            return
        reasons = {
            'no_credentials': '自动重连已重试 2 次，未配置可用账密，请配置账密后登录或扫码登录',
            'verification_required': '自动账密登录需要人脸或安全验证，请在账号管理中完成验证或重新登录',
            'credential_unavailable': '保存的登录凭据无法解密，请恢复原密钥或重新保存账密后登录',
            'account_disabled': '账号已停用，自动恢复已停止，请检查账号状态',
        }
        reason = reasons.get(result, '自动重连已重试 2 次，账密登录或账号重建失败，请检查登录日志并手动重新登录')
        if self.recovery_stage == 'verification_required':
            reason = reasons['verification_required']
        self.mark_unhealthy(reason)

    async def run(self):
        delay = INTERVAL
        while True:
            try:
                ws = self.instance._registered_ws
                if ws is not None:
                    await self.poll(ws)
                delay = INTERVAL
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Query failures/rate limiting alone are not proof of missing pushes.
                logger.warning(f'【{self.instance.cookie_id}】消息健康检查暂不可用: {type(exc).__name__}')
                delay = min(delay * 2, 300)
            try:
                # A due login retry follows an already confirmed incident; a
                # subsequent history-query failure must not postpone it forever.
                if ((self.login_retry_at is not None and time.monotonic() >= self.login_retry_at)
                        or (self.gap_detected and not self.unhealthy and any(
                        count >= 2 and time.monotonic() - first >= GRACE
                        for first, count, _ in self.pending.values()))):
                    await self.recover()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f'【{self.instance.cookie_id}】消息恢复暂不可用: {type(exc).__name__}')
            # A history query failure must not prevent recording a recovery that
            # was already observed on the live socket (or block future checks).
            try:
                await self.flush_events()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f'【{self.instance.cookie_id}】消息健康日志写入暂不可用: {type(exc).__name__}')
            await asyncio.sleep(delay)

    def start(self, ws):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
