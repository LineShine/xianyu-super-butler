"""Human-operated verification: isolated browser, authenticated frames and real pointer input.

Official Token requests happen on an explicit request or a queued official
challenge; only human input is forwarded. Confirmed success resumes that account.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import sys
import time
import uuid
from functools import wraps
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert

from app.services.manual_captcha_diagnostics import VerificationDiagnostics
from app.services.official_token_observer import OfficialTokenObserver

from common.db.session import async_session_maker
from common.models.risk_control_log import YtxRiskControlLog
from common.models.token_cache import TokenCache
from common.models.ytx_account import YtxAccount
from common.services.captcha.token_response import extract_token_captcha_url, is_token_expired_response
from common.services.im_token_api import (
    build_im_token_api_url, extract_im_access_token, request_im_token,
)
from common.utils.auth_scope import resolve_owner_scope
from common.services import manual_verification_queue as verification_queue
from common.services.slider_retry_guard import MANUAL_RESUME_KEY, reset_slider_guard
from common.utils.xianyu_utils import generate_device_id, trans_cookies
from common.utils.time_utils import get_beijing_now_naive

LEASE_KEY = "manual_captcha_session"
SESSION_SECONDS = verification_queue.SESSION_SECONDS
WIDTH, HEIGHT = 900, 560
OFFICIAL_DOMAINS = ("goofish.com", "taobao.com")
RESOURCE_DOMAINS = OFFICIAL_DOMAINS + (
    "alicdn.com", "aliyun.com", "aliyuncs.com", "mmstat.com", "alibaba.com",
    "alipay.com", "tbcdn.cn",
)
# Captcha 1.0 also loads resources outside the domains above. Keep these
# exact: aliapp.org itself can contain unrelated application subdomains.
# https://help.aliyun.com/zh/captcha/captcha1-0/support/faq-about-the-web-frontend
CAPTCHA_RESOURCE_HOSTS = frozenset({"ynuf.aliapp.org", "us.ynuf.aliapp.org"})


def native_window_available(client_host: str) -> bool:
    return (os.environ.get("LOCAL_STACK_MANAGED") == "1"
            and sys.platform in ("darwin", "win32")
            and client_host in ("127.0.0.1", "::1"))


def allowed_url(url: str, *, challenge: bool = False) -> bool:
    """Allow only HTTPS official pages/resources; never a user-provided arbitrary URL."""
    try:
        parsed = urlsplit(url)
        domains = OFFICIAL_DOMAINS if challenge else RESOURCE_DOMAINS
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https" and parsed.port in (None, 443)
            and not parsed.username and not parsed.password
            and (any(host == d or host.endswith("." + d) for d in domains)
                 or (not challenge and host in CAPTCHA_RESOURCE_HOSTS))
            and (not challenge or any(p in parsed.path for p in ("punish", "captcha", "validate")))
        )
    except ValueError:
        return False


async def restrict_verification_request(route, diagnostics: VerificationDiagnostics):
    if allowed_url(route.request.url):
        await route.continue_()
    else:
        diagnostics.blocked(route.request)
        await route.abort()


def cookie_string(cookies: dict) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def token_return_codes(response: object) -> list[str]:
    values = response.get("ret", []) if isinstance(response, dict) else []
    values = [values] if isinstance(values, str) else values
    if not isinstance(values, (list, tuple)):
        return []
    codes = [str(value).split("::", 1)[0] for value in values]
    return [code for code in codes if re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", code)][:3]


def verification_outcome(result) -> tuple[str, str, bool]:
    """A failed Token check is not evidence of a failed human slider gesture."""
    response = result.response_json
    codes = token_return_codes(response)
    suffix = f"（{', '.join(codes)}）" if codes else ""
    if extract_im_access_token(response):
        return "success", "闲鱼接口验证通过，验证结果已保存。", False
    if "FAIL_SYS_SESSION_EXPIRED" in codes:
        return "login_expired", "闲鱼登录已过期，请到账号管理重新登录；滑块通过不能恢复登录。" + suffix, True
    if is_token_expired_response(response) or "FAIL_SYS_TOKEN_EMPTY" in codes:
        if getattr(result, "response_cookies", {}).get("_m_h5_tk"):
            return "signing_cookie_updated", "网页签名凭据已更新，请间隔十秒后点击「验证结果」再检查；本次未判定滑块失败。" + suffix, False
        return "signing_cookie_invalid", "网页签名凭据失效，请重新登录或重新打开验证；本次未判定滑块失败。" + suffix, True
    if getattr(result, "status_code", 0) == 429 or any("LIMIT" in code for code in codes):
        return "rate_limited", "闲鱼接口暂时限流，请稍后再验证；本次未判定滑块失败。" + suffix, True
    if getattr(result, "status_code", 0) >= 500:
        return "service_unavailable", "闲鱼接口暂时异常，请稍后再验证；本次未判定滑块失败。" + suffix, True
    if "FAIL_SYS_USER_VALIDATE" in codes or "RGV587_ERROR" in codes or extract_token_captcha_url(response):
        return "verification_pending", "闲鱼接口仍要求安全验证。若页面已提示完成，请稍等后点击「验证结果」；不要重复拖动已通过的滑块。" + suffix, False
    if "SUCCESS" in codes:
        return "missing_token", "闲鱼接口返回成功但缺少登录 Token，账号暂未恢复。" + suffix, False
    return "api_rejected", "闲鱼接口未返回可用 Token，暂不能确认账号已恢复。" + suffix, False


def token_failure_message(response: object) -> str:
    values = response.get("ret", []) if isinstance(response, dict) else []
    values = [values] if isinstance(values, str) else values
    codes = [str(value).split("::", 1)[0] for value in values or []]
    # Never expose a response body, signed URL, Cookie or Token in a UI/log error.
    codes = [code for code in codes if re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", code)]
    if "FAIL_SYS_SESSION_EXPIRED" in codes:
        return "闲鱼登录已过期，请到账号管理重新登录；仅拖动滑块无法恢复登录。"
    if is_token_expired_response(response):
        return "闲鱼登录凭据仍已过期，请到账号管理重新登录。"
    if "FAIL_SYS_TOKEN_EMPTY" in codes:
        return "闲鱼未接受当前网页凭据（FAIL_SYS_TOKEN_EMPTY），请到账号管理重新登录后再验证。"
    suffix = f"（返回码：{', '.join(codes[:3])}）" if codes else ""
    return f"闲鱼未返回可用验证链接{suffix}。请在闲鱼 App 检查登录与安全验证；未自动重试。"


def check_account(account, owner_scope: int | None, *, require_paused: bool = True) -> None:
    if account is None or account.status == "deleted" or (
        owner_scope is not None and account.owner_id != owner_scope
    ):
        raise HTTPException(404, "账号不存在或无权操作")
    if require_paused and account.status != "disabled":
        raise HTTPException(409, "请先在账号管理中暂停该账号，再进行手动验证，避免后台任务同时请求")


def within_deadline(method):
    """A slow check/screenshot must not hold the FIFO past the one-minute deadline."""
    @wraps(method)
    async def bounded(self, session, *args, **kwargs):
        task = asyncio.current_task()
        session.operation_tasks.add(task)
        try:
            remaining = session.expires_at - time.time()
            if remaining <= 0:
                raise HTTPException(410, "手动验证已超时")
            async with asyncio.timeout(remaining):
                return await method(self, session, *args, **kwargs)
        except TimeoutError:
            raise HTTPException(410, "手动验证一分钟超时，已自动跳过") from None
        finally:
            session.operation_tasks.discard(task)
    return bounded


@dataclass
class ManualSession:
    id: str
    operator_id: int
    owner_id: int
    account_pk: int
    account_id: str
    original_cookie: str
    device_id: str
    log_id: int
    cookies: dict
    expires_at: float = field(default_factory=lambda: time.time() + SESSION_SECONDS)
    status: str = "loading"
    message: str = "正在打开闲鱼验证页面…"
    page: object = None
    browser: object = None
    context: object = None
    playwright: object = None
    init_task: asyncio.Task | None = None
    expiry_task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    screenshot_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    diagnostics: VerificationDiagnostics = field(default_factory=VerificationDiagnostics)
    browser_user_agent: str = ""
    interaction_mode: str = "embedded"
    official_observer: OfficialTokenObserver | None = None
    official_recorded_generation: int = 0
    last_input_time_ms: float | None = None
    last_input_dispatched: float = 0
    interactions: int = 0
    check_count: int = 0
    last_check: float = 0
    pressed: bool = False
    sequence: int = 0
    refresh_used: bool = False
    queued: bool = False
    initial_url: str = ""
    requested_at: float = 0
    operation_tasks: set = field(default_factory=set)
    checks: list[str] = field(default_factory=list)


class ManualCaptchaManager:
    def __init__(self):
        self.sessions: dict[str, ManualSession] = {}
        self.start_lock = asyncio.Lock()
        self.worker_id = uuid.uuid4().hex
        self.recovery_tasks: set[asyncio.Task] = set()

    async def claim(self, user, client_id: str, *, native_window: bool = False) -> dict:
        async with self.start_lock:
            data = await verification_queue.advance(user=user, client_id=client_id, claim_request=True, worker_id=self.worker_id)
            claim = data.pop("claim", None)
            if not claim:
                return data
            sid = claim["id"]
            session = self.sessions.get(sid)
            if session is None:
                session = ManualSession(
                    id=sid, operator_id=user.id, owner_id=claim["owner_id"],
                    account_pk=claim["account_pk"], account_id=claim["account_id"],
                    original_cookie=claim["cookie"], cookies=trans_cookies(claim["cookie"]),
                    device_id=claim["device_id"] or generate_device_id(claim["account_id"]),
                    log_id=claim["log_id"], expires_at=claim["expires_at"],
                    queued=True, initial_url=claim["url"], requested_at=claim["requested_at"],
                    interaction_mode="native" if native_window else "embedded",
                )
                async with async_session_maker() as db:
                    account = await db.get(YtxAccount, session.account_pk)
                    check_account(account, session.owner_id)
                    if account.cookie != session.original_cookie or session.cookies.get("unb") != session.account_id:
                        await verification_queue.finish(session.account_id, sid, "cancelled", "账号登录信息已变化或 Cookie 与账号不匹配，请重新登录")
                        return {"available": False, "pending_count": data["pending_count"],
                                "message": "账号登录信息已变化或 Cookie 与账号不匹配，请重新登录"}
                    if not claim["device_id"]:
                        cached = (await db.execute(select(TokenCache).where(
                            TokenCache.user_id.in_([session.account_id, f"chat_{session.account_id}"])
                        ).order_by(TokenCache.updated_at.desc()).limit(1))).scalar_one_or_none()
                        if cached and cached.device_id:
                            session.device_id = cached.device_id
                    if account.proxy_type not in (None, "", "none"):
                        await verification_queue.finish(session.account_id, sid, "failed", "该账号使用代理，请在闲鱼 App 完成验证")
                        return {"available": False, "pending_count": data["pending_count"],
                                "message": "该账号使用代理，请在闲鱼 App 完成验证"}
                self.sessions[sid] = session
                session.init_task = asyncio.create_task(self._open(session))
                session.expiry_task = asyncio.create_task(self._expire(session))
            data["session"] = self.describe(session)
            return data

    async def run_queue(self):
        # Deadlines progress even if all Web tabs are closed; no browser is opened
        # until an authenticated operator claims the head of the queue.
        while True:
            try:
                for session in list(self.sessions.values()):
                    if session.expires_at <= time.time():
                        await self.close(session)
                await verification_queue.advance()
            except asyncio.CancelledError:
                raise
            except Exception:
                from loguru import logger
                logger.warning("手动验证队列暂不可用，将稍后检查（不请求闲鱼）")
            await asyncio.sleep(1)

    async def access(self, sid: str, user) -> ManualSession:
        session = self.sessions.get(sid)
        if not session or session.operator_id != user.id:
            raise HTTPException(404, "验证窗口不存在或已过期，请重新打开")
        if session.expires_at <= time.time():
            await self.close(session)
            raise HTTPException(410, "手动验证一分钟超时，已自动跳过")
        scope, _ = resolve_owner_scope(user)
        async with async_session_maker() as db:
            account = await db.get(YtxAccount, session.account_pk)
            check_account(account, scope, require_paused=session.status != "success")
            if account.owner_id != session.owner_id or account.cookie != session.original_cookie:
                raise HTTPException(409, "账号信息已变化，请关闭验证窗口后重新打开")
        return session

    def describe(self, session: ManualSession) -> dict:
        return {
            "session_id": session.id, "account_id": session.account_id,
            "status": session.status, "message": session.message,
            "width": WIDTH, "height": HEIGHT,
            "expires_at": session.expires_at,
            "sequence": session.sequence,
            "interaction_mode": session.interaction_mode,
        }

    async def _probe(self, session: ManualSession):
        # Deliberately use the official web endpoint: no third-party remote settings
        # and no automatic fallback/retry. An explicit manual action may inspect a
        # paused account. Only confirmed success may reset its retry guard.
        if session.context is None:
            raise RuntimeError("验证浏览器会话不可用")
        url = build_im_token_api_url("web")
        # Select cookies for the actual API host; unrelated taobao.com cookies
        # with the same name must never overwrite this request's signing cookie.
        scoped = await session.context.cookies([url])
        cookies = self._scoped_cookie_values(scoped)
        session.cookies = cookies
        result = await request_im_token(
            cookie_string(cookies), session.device_id, api_mode="web", timeout_seconds=20,
            browser_context=session.context, browser_user_agent=session.browser_user_agent,
        )
        session.cookies = self._scoped_cookie_values(await session.context.cookies([url]))
        return result

    @staticmethod
    def _scoped_cookie_values(cookies: list[dict]) -> dict:
        # More specific domain/path wins if the browser has duplicate names.
        ordered = sorted(cookies, key=lambda c: (len(c.get("path", "/")), len(c.get("domain", ""))))
        return {cookie["name"]: cookie["value"] for cookie in ordered}

    async def _page_hint(self, session: ManualSession) -> str:
        await session.diagnostics.observe_page(session.page, official_page=session.official_observer is not None)
        return session.diagnostics.page_state

    async def _observed_token(self, session: ManualSession):
        observer = session.official_observer
        result = observer.result if observer else None
        if result is not None:
            generation = observer.generation
            session.cookies = self._scoped_cookie_values(
                await session.context.cookies([build_im_token_api_url("web")])
            )
            # The official page creates its own IM device. The saved Token and
            # the resumed WebSocket must use that exact device, not our old one.
            session.device_id = result.device_id
            if session.official_recorded_generation != generation:
                await self._record_check(session, result, phase="official_page")
                session.official_recorded_generation = generation
        return result

    async def _load_official_page(self, session: ManualSession):
        session.official_observer = OfficialTokenObserver()
        session.official_observer.attach(session.page)
        session.diagnostics.add("entry=official_message_page token_transport=page_observed")
        # Let the site's own SDK initialize credentials and handle verification.
        # No APIRequestContext request, injected SDK call, or replayed challenge.
        await session.page.goto("https://www.goofish.com/im", wait_until="domcontentloaded", timeout=25000)
        session.status = "ready"
        session.message = "请在官网消息页完成登录或安全验证。系统会观察官网返回的登录结果；消息列表正常加载后可点击「验证结果」。"
        await session.page.bring_to_front()

    @staticmethod
    def _diagnostic_text(session: ManualSession) -> str:
        return "\n".join(filter(None, [session.diagnostics.text(), "\n".join(session.checks)]))

    async def _persist_diagnostics(self, session: ManualSession):
        diagnostics = session.diagnostics
        async with diagnostics.save_lock:
            if diagnostics.saved_generation == diagnostics.generation:
                return
            generation = diagnostics.generation
            try:
                async with async_session_maker() as db:
                    # A late screenshot may finish after successful verification.
                    # The status predicate must be evaluated by the database.
                    await db.execute(update(YtxRiskControlLog).where(
                        YtxRiskControlLog.id == session.log_id,
                        YtxRiskControlLog.account_identifier == session.account_id,
                        YtxRiskControlLog.processing_status == "processing",
                    ).values(error_message=self._diagnostic_text(session)))
                    await db.commit()
                diagnostics.saved_generation = generation
            except Exception:
                logger.warning(f"验证诊断暂时无法保存 log_id={session.log_id}")

    async def _record_check(self, session: ManualSession, result=None, *, phase="check", error=""):
        category = verification_outcome(result)[0] if result is not None else error
        codes = ",".join(token_return_codes(result.response_json)) if result is not None else ""
        page_hint = await self._page_hint(session)
        detail = (
            f"{get_beijing_now_naive():%H:%M:%S} {phase}={session.check_count} "
            f"result={category} ret={codes or 'none'} "
            f"http={getattr(result, 'status_code', 0)} "
            f"duration_ms={int(getattr(result, 'duration_seconds', 0) * 1000)} page={page_hint}"
        )
        logger.info(f"【{session.account_id}】手动验证检查 log_id={session.log_id} {detail}")
        session.checks = (session.checks + [detail])[-6:]
        try:
            async with async_session_maker() as db:
                log = await db.get(YtxRiskControlLog, session.log_id)
                if log and log.account_identifier == session.account_id and log.processing_status == "processing":
                    if category != "success":
                        log.error_message = self._diagnostic_text(session)
                    await db.commit()
        except Exception:
            logger.warning(f"【{session.account_id}】验证诊断记录暂时无法保存 log_id={session.log_id}")

    async def _open(self, session: ManualSession):
        try:
            from playwright.async_api import async_playwright
            from common.utils.browser_utils import ensure_playwright_browser_path, get_chromium_executable_path
            ensure_playwright_browser_path()
            session.playwright = await async_playwright().start()
            executable = get_chromium_executable_path()
            headed = session.interaction_mode == "native"
            session.browser = await session.playwright.chromium.launch(
                headless=not headed, **({"executable_path": executable} if executable else {})
            )
            session.context = await session.browser.new_context(
                viewport={"width": 1280 if headed else WIDTH, "height": 800 if headed else HEIGHT}, locale="zh-CN",
                service_workers="block", accept_downloads=False,
            )
            await session.context.add_cookies([
                {"name": name, "value": value, "domain": ".goofish.com", "path": "/", "secure": True}
                for name, value in session.cookies.items()
            ])

            async def restrict(route):
                await restrict_verification_request(route, session.diagnostics)

            await session.context.route("**/*", restrict)
            session.page = await session.context.new_page()
            session.page.set_default_timeout(8000)
            identity = await session.page.evaluate("({userAgent:navigator.userAgent, platform:navigator.platform})")
            session.browser_user_agent = identity["userAgent"]
            session.diagnostics.add(
                f"browser chromium={session.browser.version} platform={identity['platform']} headless={str(not headed).lower()}"
            )
            session.diagnostics.attach(session.page)
            if headed:
                await self._observe_native_input(session)
                await self._load_official_page(session)
            else:
                await self._load_challenge(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Browser exception text can contain the entire signed challenge URL.
            await self._finish(session, "failed", "验证页面打开失败，请检查本机浏览器环境或网络后重试。未自动重试。")

    def _native_activity(self, session: ManualSession, kind: str):
        if session.interaction_mode != "native" or session.status != "ready":
            return
        if kind == "down":
            session.pressed = True
            session.interactions += 1
        elif kind in ("up", "cancel"):
            session.pressed = False
        elif kind == "key":
            session.interactions += 1
        else:
            return
        session.diagnostics.add(f"human_input mode=native event={kind} interaction={session.interactions}")

    async def _observe_native_input(self, session: ManualSession):
        # Observe genuine user activity only. No generated input, automation
        # masking or changes to the official verification callbacks.
        await session.page.expose_binding("__ytxManualActivity", lambda _, kind: self._native_activity(session, kind))
        await session.page.add_init_script("""(() => {
            const report = (event, kind) => {
                if (event.isTrusted) void window.__ytxManualActivity(kind).catch(() => {});
            };
            window.addEventListener('pointerdown', event => {
                if (event.button === 0) report(event, 'down');
            }, {capture: true, passive: true});
            window.addEventListener('pointerup', event => report(event, 'up'), {capture: true, passive: true});
            window.addEventListener('pointercancel', event => report(event, 'cancel'), {capture: true, passive: true});
            window.addEventListener('keyup', event => {
                if (['Enter', ' '].includes(event.key)) report(event, 'key');
            }, {capture: true, passive: true});
        })();""")

    @within_deadline
    async def focus(self, session: ManualSession) -> dict:
        async with session.lock:
            if session.interaction_mode != "native" or session.status != "ready" or session.page is None:
                raise HTTPException(409, "当前没有可显示的本机验证窗口")
            await session.page.bring_to_front()
            return self.describe(session)

    async def _load_challenge(self, session: ManualSession):
        result = await self._probe(session)
        await self._record_check(session, result, phase="open")
        token = extract_im_access_token(result.response_json)
        if token:
            await self._save_success(session, token, "闲鱼接口已恢复正常，无需滑块验证。")
            return
        missing_token = (
            isinstance(result.response_json, dict)
            and "FAIL_SYS_TOKEN_EMPTY" in str(result.response_json.get("ret", ""))
        )
        if ((is_token_expired_response(result.response_json) or missing_token)
                and result.response_cookies.get("_m_h5_tk") and not session.refresh_used):
            # This is signing-cookie rotation, not a rejected CAPTCHA. Ask for
            # an explicit second request instead of entering an automatic loop.
            session.status = "refresh_required"
            session.message = "闲鱼要求更新网页凭据，已下发更新信息。可点击「继续打开验证」再获取一次；不会自动重试。"
            return
        url = extract_token_captcha_url(result.response_json)
        if not allowed_url(url, challenge=True):
            _, message, terminal = verification_outcome(result)
            await self._finish(session, "failed", message if terminal else token_failure_message(result.response_json))
            return
        await self._show_challenge(session, url)

    async def _show_challenge(self, session: ManualSession, url: str):
        if session.expires_at <= time.time():
            raise HTTPException(410, "验证已超时")
        host = urlsplit(url).hostname
        cookie_domains = set()
        if host == "taobao.com" or host.endswith(".taobao.com"):
            cookie_domains.add(".taobao.com")
        await session.context.add_cookies([
            {"name": name, "value": value, "domain": domain, "path": "/", "secure": True}
            for domain in cookie_domains for name, value in session.cookies.items()
        ])
        await session.page.goto(url, wait_until="domcontentloaded", timeout=25000)
        session.status = "ready"
        if session.interaction_mode == "native":
            session.message = "请在新打开的浏览器窗口中直接拖动滑块，页面提示完成后回到这里点击「验证结果」。"
            await session.page.bring_to_front()
        else:
            session.message = "请手动拖动滑块，等待页面提示完成后点击右下角「验证结果」；一分钟内未完成将自动跳过。"

    @within_deadline
    async def refresh(self, session: ManualSession) -> dict:
        async with session.lock:
            if session.status != "refresh_required" or session.refresh_used:
                raise HTTPException(409, "当前验证不能再次获取，请关闭窗口后重试")
            session.refresh_used = True
            try:
                await self._load_challenge(session)
            except Exception:
                await self._finish(session, "failed", "获取验证页面失败，请检查网络或重新登录；未自动重试。")
            return self.describe(session)

    @within_deadline
    async def snapshot(self, session: ManualSession) -> dict:
        # Capturing frames must not hold the input lock and delay a human drag.
        async with session.screenshot_lock:
            data = self.describe(session)
            if session.status == "ready":
                try:
                    shot = None
                    if session.interaction_mode == "native":
                        if session.page.is_closed():
                            await self._finish(session, "cancelled", "本机验证窗口已关闭，该账号保持暂停。")
                            return self.describe(session)
                    else:
                        shot = await session.page.screenshot(type="jpeg", quality=80, timeout=8000)
                    if session.official_observer is not None:
                        async with session.lock:
                            if session.status != "ready":
                                return self.describe(session)
                            result = await self._observed_token(session)
                            token = extract_im_access_token(result.response_json) if result else None
                            if token:
                                await self._save_success(session, token, "官网消息登录已确认成功。")
                                return self.describe(session)
                    await self._page_hint(session)
                    await self._persist_diagnostics(session)
                    if session.status != "ready":
                        return self.describe(session)
                    if session.diagnostics.page_state == "page_reports_failure":
                        code = session.diagnostics.page_error_code
                        suffix = f"（{code}）" if code else ""
                        session.message = f"闲鱼页面提示验证失败{suffix}，已记录诊断。可点击验证框重试，或关闭后在闲鱼 App 处理。"
                    elif session.diagnostics.page_state == "page_reports_success":
                        session.message = "闲鱼页面提示验证通过，请点击右下角「验证结果」确认账号是否已恢复。"
                    data = self.describe(session)
                    if shot is not None:
                        data["image"] = "data:image/jpeg;base64," + base64.b64encode(shot).decode("ascii")
                except Exception:
                    if session.status == "ready":
                        await self._finish(session, "failed", "验证页面已关闭或无法显示，请关闭窗口后重试。")
                    data = self.describe(session)
            return data

    @within_deadline
    async def input(self, session: ManualSession, sequence: int, events: list) -> dict:
        async with session.lock:
            if session.status != "ready":
                raise HTTPException(409, "当前验证页面不可操作")
            if session.interaction_mode == "native":
                raise HTTPException(409, "请直接操作本机验证窗口")
            if sequence <= session.sequence:
                return {"sequence": session.sequence}
            if sequence != session.sequence + 1:
                raise HTTPException(409, "操作顺序异常，请关闭窗口后重试")
            previous = session.last_input_time_ms
            for event in events:
                captured = getattr(event, "time_ms", None)
                if captured is not None:
                    if previous is not None and captured < previous:
                        raise HTTPException(409, "操作时间顺序异常，请关闭窗口后重试")
                    previous = captured
            for event in events:
                captured = getattr(event, "time_ms", None)
                if captured is not None and session.last_input_time_ms is not None:
                    # Preserve the human's sampled intervals; never synthesize
                    # points, interpolate a path, or compress a delayed batch.
                    gap = (captured - session.last_input_time_ms) / 1000
                    wait = gap - (time.monotonic() - session.last_input_dispatched)
                    if wait > 0:
                        await asyncio.sleep(wait)
                if event.kind in ("down", "move", "up"):
                    await session.page.mouse.move(event.x, event.y)
                    if event.kind == "down" and not session.pressed:
                        await session.page.mouse.down()
                        session.pressed = True
                        session.interactions += 1
                        session.diagnostics.add(f"human_input mode=embedded event=down interaction={session.interactions}")
                    elif event.kind == "up" and session.pressed:
                        await session.page.mouse.up()
                        session.pressed = False
                        session.diagnostics.add(f"human_input mode=embedded event=up interaction={session.interactions}")
                elif event.kind == "wheel":
                    await session.page.mouse.wheel(0, event.delta)
                elif event.kind == "key":
                    await session.page.keyboard.press(event.key)
                    session.interactions += 1
                session.last_input_time_ms = captured
                session.last_input_dispatched = time.monotonic()
            session.sequence = sequence
            return {"sequence": sequence}

    @within_deadline
    async def check(self, session: ManualSession) -> dict:
        async with session.lock:
            if session.status != "ready":
                return self.describe(session)
            if session.pressed or not session.interactions:
                raise HTTPException(409, "请先在验证页面完成操作，再检查结果")
            if time.time() - session.last_check < 10:
                raise HTTPException(429, "请等待十秒后再检查，避免频繁请求闲鱼")
            session.last_check = time.time()
            session.check_count += 1
            try:
                result = (await self._observed_token(session) if session.official_observer is not None
                          else await self._probe(session))
                if result is None:
                    session.message = "官网尚未返回可用的消息登录结果，请在官网窗口完成登录或验证后再检查。"
                    if session.check_count >= 3:
                        await self._finish(session, "failed", session.message + "已达到三次检查上限。")
                    return self.describe(session)
                await self._record_check(session, result)
                token = extract_im_access_token(result.response_json)
                _, message, terminal = verification_outcome(result)
                if token:
                    await self._save_success(session, token, message)
                elif terminal or session.check_count >= 3:
                    if session.check_count >= 3:
                        message = "已达到三次接口检查上限，本次验证停止。" + message
                    await self._finish(session, "failed", message)
                else:
                    session.message = f"第 {session.check_count}/3 次检查：{message}"
            except HTTPException:
                raise
            except Exception as exc:
                error = "network_timeout" if "Timeout" in type(exc).__name__ else "request_error"
                await self._record_check(session, error=error)
                await self._finish(session, "failed", "检查接口超时或请求异常，无法确认验证结果；本次已停止，未判定滑块失败。")
            return self.describe(session)

    async def _save_success(self, session: ManualSession, token: str, message: str):
        from common.services.token_renewal_cache_service import random_token_cache_expiry
        message += "账号已自动启用，正在恢复连接。"
        async with async_session_maker() as db:
            # Use the queue's lock order so expiry cannot race with success.
            if session.queued:
                await verification_queue._lock(db)
            account = (await db.execute(select(YtxAccount).where(
                YtxAccount.id == session.account_pk
            ).with_for_update())).scalar_one_or_none()
            check_account(account, session.owner_id)
            lease = (account.metadata_json or {}).get(verification_queue.QUEUE_KEY if session.queued else LEASE_KEY) or {}
            if (account.cookie != session.original_cookie or lease.get("id") != session.id
                    or (session.queued and lease.get("status") != "processing")):
                raise HTTPException(409, "账号登录信息已变化，请重新打开验证窗口")
            if session.expires_at <= time.time():
                raise HTTPException(410, "验证已超时，未保存结果")
            if session.cookies.get("unb") != account.account_id:
                raise HTTPException(409, "验证账号不匹配，未保存结果")
            # refresh() inside reset_slider_guard must precede all credential edits.
            await reset_slider_guard(db, account)
            account.cookie = cookie_string(session.cookies)
            account.status, account.disable_reason = "active", None
            expire_at, _ = random_token_cache_expiry()
            # Both the automation connection and online-chat cache must see the
            # verified token; otherwise re-enabling would reuse the stale token.
            for user_id in (account.account_id, f"chat_{account.account_id}"):
                statement = insert(TokenCache).values(
                    user_id=user_id, token=token, device_id=session.device_id, expire_at=expire_at,
                )
                await db.execute(statement.on_duplicate_key_update(
                    token=token, device_id=session.device_id, expire_at=expire_at, renew_expire_at=None,
                ))
            log = await db.get(YtxRiskControlLog, session.log_id)
            if log:
                log.processing_status = "success"
                log.processing_result = message
                # A recovered session must not retain an earlier check under
                # "failure reason"; keep its bounded diagnostics in the detail.
                log.error_message = None
                if self._diagnostic_text(session):
                    log.event_description = ((log.event_description or "")
                                             + "\n验证诊断记录：\n" + self._diagnostic_text(session))
            if session.queued:
                await verification_queue._finish(db, account, lease, "success", message)
            account.metadata_json = {**(account.metadata_json or {}), MANUAL_RESUME_KEY: session.id}
            if session.expires_at <= time.time():
                raise HTTPException(410, "验证已超时，未保存结果")
            await db.commit()
            session.original_cookie = account.cookie
            session.status, session.message = "success", message
            # Once success is committed, closing/expiring the modal must not
            # cancel connection recovery. It never holds the verification FIFO.
            recovery = asyncio.create_task(self._resume_account(session))
            self.recovery_tasks.add(recovery)
            recovery.add_done_callback(self.recovery_tasks.discard)
        await asyncio.shield(recovery)
        await self._close_browser(session)

    async def _resume_account(self, session: ManualSession):
        from app.services.websocket_client import websocket_client

        def still_current(account):
            return bool(account and account.owner_id == session.owner_id
                        and account.status == "active" and account.cookie == session.original_cookie
                        and (account.metadata_json or {}).get(MANUAL_RESUME_KEY) == session.id)

        try:
            async with async_session_maker() as db:
                if not still_current(await db.get(YtxAccount, session.account_pk)):
                    return
            try:
                # One bounded local start request, not another CAPTCHA request.
                result = await websocket_client.http_client.post(
                    f"{websocket_client.base_url}/internal/accounts/{session.account_id}/start",
                    json={}, timeout=8, max_retries=1,
                )
                started = isinstance(result, dict) and result.get("success") is True
            except Exception:
                started = False
            async with async_session_maker() as db:
                account = (await db.execute(select(YtxAccount).where(
                    YtxAccount.id == session.account_pk
                ).with_for_update())).scalar_one_or_none()
                if not still_current(account):
                    return  # A newer login, pause or verification takes precedence.
                meta = dict(account.metadata_json or {})
                meta.pop(MANUAL_RESUME_KEY, None)
                if not started:
                    account.status = "disabled"
                    account.disable_reason = "手动验证已通过，但连接服务启动失败，请检查服务后重新启用"
                    meta[verification_queue.HOLD_KEY] = True
                    session.status = "failed"
                    session.message = account.disable_reason + "；已继续下一项。"
                    log = await db.get(YtxRiskControlLog, session.log_id)
                    if log:
                        # The official verification succeeded, even if the local
                        # connection service is temporarily unavailable.
                        log.processing_result = session.message
                account.metadata_json = meta
                await db.commit()
        except Exception:
            from loguru import logger
            logger.warning("手动验证后的连接恢复状态暂不可用，请检查账号在线状态")

    async def _finish(self, session: ManualSession, status: str, message: str):
        session.status, session.message = status, message
        try:
            async with async_session_maker() as db:
                log = await db.get(YtxRiskControlLog, session.log_id)
                if log and log.processing_status == "processing":
                    log.processing_status = status
                    log.processing_result = message
                    if self._diagnostic_text(session):
                        log.error_message = self._diagnostic_text(session)
                    await db.commit()
                elif (log and log.processing_status == "cancelled"
                      and log.account_identifier == session.account_id and self._diagnostic_text(session)):
                    # The durable queue may expire this log before browser cleanup.
                    # Preserve the observed cause without changing its final status.
                    log.error_message = self._diagnostic_text(session)
                    await db.commit()
        finally:
            await self._close_browser(session)
            if session.queued:
                await verification_queue.finish(session.account_id, session.id, status, message)

    async def _close_browser(self, session: ManualSession):
        if session.official_observer is not None:
            await session.official_observer.close()
        await session.diagnostics.close()
        for attr, method in (("context", "close"), ("browser", "close"), ("playwright", "stop")):
            resource = getattr(session, attr)
            if resource:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(getattr(resource, method)(), timeout=2)
                setattr(session, attr, None)
        session.page = None

    async def close(self, session: ManualSession):
        tasks = [t for t in session.operation_tasks if t is not asyncio.current_task() and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if session.init_task and not session.init_task.done():
            session.init_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.init_task
        async with session.lock:
            if session.status in ("ready", "loading", "refresh_required"):
                await self._finish(session, "cancelled", "手动验证已关闭或一分钟超时，已释放队列；该账号保持暂停，其他任务继续")
            else:
                await self._close_browser(session)
            async with async_session_maker() as db:
                account = (await db.execute(select(YtxAccount).where(
                    YtxAccount.id == session.account_pk
                ).with_for_update())).scalar_one_or_none()
                if account:
                    meta = dict(account.metadata_json or {})
                    lease = meta.get(LEASE_KEY) or {}
                    if lease.get("id") == session.id:
                        meta[LEASE_KEY] = {**lease, "expires_at": 0}
                        account.metadata_json = meta
                        await db.commit()
            self.sessions.pop(session.id, None)
        if session.expiry_task and session.expiry_task is not asyncio.current_task():
            session.expiry_task.cancel()

    async def _expire(self, session: ManualSession):
        await asyncio.sleep(max(0, session.expires_at - time.time()))
        await self.close(session)

    async def shutdown(self):
        await asyncio.gather(*(self.close(s) for s in list(self.sessions.values())), return_exceptions=True)
        if self.recovery_tasks:
            await asyncio.gather(*list(self.recovery_tasks), return_exceptions=True)


manual_captcha_manager = ManualCaptchaManager()
