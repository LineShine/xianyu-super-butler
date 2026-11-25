"""Message recovery uses real saved-credential login, with no live browser/API calls."""
import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.services.xianyu.cookie_manager import CookieManager
from app.services.xianyu.cookie_token_manager import CookieTokenManager
from app.services.xianyu.message_sync_health import MessageSyncHealth
from app.services.xianyu.xianyu_async import XianyuAsync
from common.security.vault import CredentialUnavailable


class PasswordRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.parent = SimpleNamespace(
            cookie_id='fixture', myid='1001', user_id=7, cookies_str='unb=1001; sid=old',
            cookies={'unb': '1001', 'sid': 'old'}, current_token='old-token', _safe_str=str,
            _last_password_login_time={}, _password_error_cooldown_time={},
            send_token_refresh_notification=AsyncMock(), restart_instance=AsyncMock(return_value=True))
        self.manager = CookieTokenManager(self.parent)
        self.parent.message_sync_health = MessageSyncHealth(self.parent)
        self.info = {'cookie_value': 'unb=1001; sid=externally-updated', 'username': 'fixture-user',
                     'password': 'synthetic-test-password', 'show_browser': False, 'status': 'active'}
        self.slider = Mock()
        self.slider.login_with_password_playwright.return_value = {'unb': '1001', 'sid': 'new'}

        def patched(target, **kwargs):
            p = patch(target, **kwargs)
            self.addCleanup(p.stop)
            return p.start()

        self.renew = patched('common.services.cookie_renew_api_service.cookie_renew_api_service.renew',
                             new_callable=AsyncMock, return_value=SimpleNamespace(
                                 deferred=False, success=True, updated_cookie_names=[], renew_method='api'))
        patched('common.services.account_credential_guard.verification_pending',
                new_callable=AsyncMock, return_value=False)
        patched('common.db.compat.db_manager.add_account_login_log')
        self.disable = patched('common.db.compat.db_manager.disable_account')
        patched('app.services.captcha.xianyu_slider_stealth.XianyuSliderStealth', return_value=self.slider)

        async def browser_task(fn):
            return await asyncio.to_thread(fn)
        self.browser = patched('app.services.xianyu.cookie_token_manager.run_browser_task',
                               new_callable=AsyncMock, side_effect=browser_task)
        self.manager._load_account_info = AsyncMock(return_value=self.info)
        self.manager.update_config_cookies = AsyncMock(return_value=True)
        self.manager._delete_cached_token = AsyncMock(return_value=True)

    async def login(self, **kwargs):
        return await self.manager._try_password_login_refresh('fixture message gap', force_password_login=True, **kwargs)

    async def test_forced_login_bypasses_silent_renewal_and_changed_db_cookie(self):
        order = []
        async def save():
            order.append('save')
            return True
        async def invalidate():
            order.append('invalidate')
            return True
        async def restart(reason):
            self.assertIsNone(self.parent.current_token)
            order.append('restart')
            return True
        self.manager.update_config_cookies.side_effect = save
        self.manager._delete_cached_token.side_effect = invalidate
        self.parent.restart_instance.side_effect = restart
        self.assertTrue(await self.login())
        self.renew.assert_not_awaited()
        self.assertEqual(self.slider.login_with_password_playwright.call_args.kwargs['account'], 'fixture-user')
        self.assertEqual(self.slider.login_with_password_playwright.call_args.kwargs['password'], 'synthetic-test-password')
        self.assertEqual(order, ['save', 'invalidate', 'restart'])
        self.assertEqual(self.parent.cookies['sid'], 'new')
        self.slider.close.assert_called_once()

    async def test_existing_session_expiry_still_uses_successful_silent_renewal(self):
        self.assertTrue(await self.manager._try_password_login_refresh('fixture expired'))
        self.renew.assert_awaited_once()
        self.browser.assert_not_awaited()

    async def test_persistence_failure_restores_cookie_without_token_clear_or_restart(self):
        self.manager.update_config_cookies.return_value = False
        self.assertFalse(await self.login())
        self.assertEqual(self.parent.cookies['sid'], 'old')
        self.assertEqual(self.parent.current_token, 'old-token')
        self.manager._delete_cached_token.assert_not_awaited()
        self.parent.restart_instance.assert_not_awaited()

    async def test_cache_invalidation_failure_does_not_rebuild_with_old_token(self):
        self.manager._delete_cached_token.return_value = False
        self.assertFalse(await self.login())
        self.parent.restart_instance.assert_not_awaited()
        self.assertIsNone(self.parent.current_token)

    async def test_wrong_account_cookie_is_never_written(self):
        self.slider.login_with_password_playwright.return_value = {'unb': '9999', 'sid': 'new'}
        self.assertFalse(await self.login())
        self.manager.update_config_cookies.assert_not_awaited()
        self.parent.restart_instance.assert_not_awaited()

    async def test_no_credentials_or_disabled_account_never_starts_browser(self):
        for changes, result in [({'password': ''}, 'no_credentials'),
                                ({'password': 'fixture', 'status': 'disabled'}, 'account_disabled')]:
            self.info.update(changes)
            self.assertEqual(await self.login(), result)
        self.browser.assert_not_awaited()
        self.disable.assert_not_called()

    async def test_unreadable_saved_credentials_are_distinct_from_cooldown(self):
        self.manager._load_account_info.side_effect = CredentialUnavailable('fixture')
        self.assertEqual(await self.login(), 'credential_unavailable')
        self.browser.assert_not_awaited()
        self.disable.assert_not_called()

    async def test_browser_verification_callback_is_delivered_on_event_loop(self):
        loop = asyncio.get_running_loop()
        notices = []
        def on_verification(message, screenshot, url):
            self.assertIs(asyncio.get_running_loop(), loop)
            notices.append((message, screenshot, url))
        def browser_login(**kwargs):
            kwargs['notification_callback']('人脸验证', '/test.png', 'https://example.test/verify')
            return None
        self.slider.login_with_password_playwright.side_effect = browser_login
        self.assertFalse(await self.login(on_verification=on_verification))
        self.assertEqual(len(notices), 1)
        self.parent.restart_instance.assert_not_awaited()

    async def test_existing_manual_login_or_verification_has_priority(self):
        @asynccontextmanager
        async def lease(account):
            yield True
        from app.services.captcha.password_login_state import password_login_state
        password_login_state.start_processing('fixture')
        try:
            with patch('common.services.account_credential_guard.credential_renewal', lease):
                self.assertEqual(await self.manager.try_password_login_refresh(force_password_login=True), 'skipped_cooldown')
                self.assertTrue(password_login_state.is_processing('fixture'))
        finally:
            password_login_state.finish_processing('fixture')
        self.browser.assert_not_awaited()

        @asynccontextmanager
        async def held(account):
            yield False
        with patch('common.services.account_credential_guard.credential_renewal', held), \
             patch('common.services.account_credential_guard.verification_pending', AsyncMock(return_value=True)):
            self.assertEqual(await self.manager.try_password_login_refresh(force_password_login=True), 'verification_required')
        self.browser.assert_not_awaited()

    async def test_new_login_ignores_even_repopulated_expired_cache(self):
        self.manager._require_fresh_token = True
        with patch('app.services.xianyu.cookie_token_manager.async_session_maker') as db:
            self.assertIsNone(await self.manager._get_cached_token(allow_expired=True))
        db.assert_not_called()
        self.assertTrue(self.manager._last_cache_lookup_succeeded)

    async def test_missing_instance_manager_reports_failure(self):
        with patch('app.services.xianyu.cookie_manager.get_manager', return_value=None):
            self.assertFalse(await XianyuAsync._restart_instance(self.parent))


class InstanceReplacementTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_instance_inherits_incident_and_requires_fresh_token(self):
        parent = SimpleNamespace(cookie_id='fixture', myid='1001')
        health = MessageSyncHealth(parent)
        health.recovery_attempts = 2
        health.password_login_attempts = 1
        health.failure_directions = {'buyer'}
        health.awaiting_rebuild = True
        new = SimpleNamespace(main=AsyncMock(), _cookie_token_manager=SimpleNamespace(_require_fresh_token=False))
        new.message_sync_health = MessageSyncHealth(new)
        manager = CookieManager()
        manager._check_user_expired_and_disable = AsyncMock(return_value=False)
        with patch('app.services.xianyu.xianyu_async.XianyuAsync', return_value=new):
            await manager._run_xianyu('fixture', 'synthetic-cookie', 7, health.recovery_snapshot())
        self.assertEqual(new.message_sync_health.recovery_attempts, 2)
        self.assertEqual(new.message_sync_health.password_login_attempts, 1)
        self.assertEqual(new.message_sync_health.failure_directions, {'buyer'})
        self.assertEqual(new.message_sync_health.status()['message_sync_status'], 'verifying')
        self.assertTrue(new._cookie_token_manager._require_fresh_token)
        new.main.assert_awaited_once()

    async def test_automatic_replacement_transfers_state_after_old_task_stops(self):
        manager = CookieManager()
        old = SimpleNamespace(cookie_id='fixture', myid='1001')
        old.message_sync_health = MessageSyncHealth(old)
        old.message_sync_health.recovery_attempts = 2
        old.message_sync_health.password_login_attempts = 1
        old.message_sync_health.awaiting_rebuild = True
        async def old_main():
            try:
                await asyncio.Future()
            finally:
                old.message_sync_health.events.append(('success', 'cleanup finished'))
        task = asyncio.create_task(old_main())
        await asyncio.sleep(0)
        manager.tasks['fixture'] = task
        manager.instances['fixture'] = old
        manager._run_xianyu = AsyncMock()
        self.assertTrue(await manager._add_cookie_async('fixture', 'synthetic-cookie', 7, replacing_instance=old))
        await manager.tasks['fixture']
        self.assertTrue(task.cancelled())
        state = manager._run_xianyu.call_args.args[3]
        self.assertEqual(state['password_login_attempts'], 1)
        self.assertIn(('success', 'cleanup finished'), state['events'])

    async def test_delayed_restart_cannot_replace_newer_login_or_disabled_account(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                manager = CookieManager()
                old, new = object(), object()
                manager.instances['fixture'] = old if disabled else new
                manager.cookie_status['fixture'] = not disabled
                manager._run_xianyu = AsyncMock()
                self.assertFalse(await manager._add_cookie_async('fixture', 'obsolete-cookie', 7, replacing_instance=old))
                manager._run_xianyu.assert_not_called()
                self.assertNotIn('fixture', manager.cookies)
