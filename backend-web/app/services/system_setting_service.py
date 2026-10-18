"""
系统设置服务

功能：
1. 系统设置CRUD操作
2. 敏感设置过滤
3. 键值对存储和查询
4. XSS防护
"""
from __future__ import annotations
from common.db.default_system_settings import DEFAULT_SYSTEM_SETTINGS
from common.utils.product_settings import is_removed_setting
from typing import Dict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from common.models.system_setting import SystemSetting
from common.services.remote_token_api import TOKEN_REMOTE_SECRET_KEY_SETTING_KEY, TOKEN_REMOTE_URL_SETTING_KEY
from common.services.token_api_mode import TOKEN_API_MODE_SETTING_KEY, normalize_token_api_mode
from common.utils.text_utils import escape_xss
SENSITIVE_KEYS = {'admin_password_hash', 'security.internal_api_token', 'security.jwt_secret_key'}
NO_ESCAPE_KEYS = {'registration_enabled', 'show_default_login_info', 'login_captcha_enabled', 'disclaimer.title', 'disclaimer.content', 'disclaimer.checkbox_text', 'disclaimer.agree_button_text', 'disclaimer.disagree_button_text', 'login.system_name', 'login.system_title', 'login.system_description', 'theme.effect', 'theme.color_preset', 'theme.font_family', 'navigation.hidden_menu_keys', 'smtp_port', 'smtp_use_tls', 'smtp_use_ssl', 'log.retention_days', 'account.face_verify_timeout_disable', 'user.register_default_days', 'proxy.api_url', 'proxy.enabled', 'captcha.remote_service_url', 'captcha.remote_secret_key', 'captcha.remote_pass_cookies', 'captcha.block_remote_calls', 'captcha.local_slider_disabled', 'captcha.remote_processing_max', 'captcha.remote_cooldown_seconds', 'captcha.remote_cooldown_until', 'captcha.slider_mode', 'captcha.real_mouse_weight_local', 'captcha.real_mouse_weight_remote', 'password_login.mode', 'password_login.remote_url', 'password_login.remote_secret_key', 'token.api_mode', TOKEN_REMOTE_URL_SETTING_KEY, TOKEN_REMOTE_SECRET_KEY_SETTING_KEY, 'security.internal_api_token'}

class SystemSettingService:
    """Provides CRUD helpers for global system settings."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def ensure_default_settings(self) -> None:
        existing_stmt = select(SystemSetting.key).where(SystemSetting.key.in_(tuple(DEFAULT_SYSTEM_SETTINGS.keys())))
        existing_result = await self.session.execute(existing_stmt)
        existing_keys = set(existing_result.scalars().all())
        missing_records = [SystemSetting(key=key, value=value, description=description) for (key, (value, description)) in DEFAULT_SYSTEM_SETTINGS.items() if key not in existing_keys]
        if not missing_records:
            return
        self.session.add_all(missing_records)
        await self.session.commit()

    async def list_settings(self, include_sensitive: bool=False) -> Dict[str, str]:
        await self.ensure_default_settings()
        stmt = select(SystemSetting)
        result = await self.session.execute(stmt)
        settings: Dict[str, str] = {}
        for entry in result.scalars().all():
            if is_removed_setting(entry.key):
                continue
            if entry.key in {'security.internal_api_token', 'security.jwt_secret_key'}:
                continue
            if not include_sensitive and entry.key in SENSITIVE_KEYS:
                continue
            settings[entry.key] = entry.value
        password_login_mode = str(settings.get('password_login.mode') or '').strip().lower()
        settings['password_login.mode'] = password_login_mode if password_login_mode in {'protocol', 'browser'} else 'browser'
        slider_mode = str(settings.get('captcha.slider_mode') or '').strip().lower()
        settings['captcha.slider_mode'] = slider_mode if slider_mode in {'browser', 'real_mouse'} else 'browser'
        settings[TOKEN_API_MODE_SETTING_KEY] = normalize_token_api_mode(settings.get(TOKEN_API_MODE_SETTING_KEY))
        return settings

    async def set_setting(self, key: str, value: str, description: str | None=None) -> None:
        if is_removed_setting(key):
            raise ValueError('此功能不属于鱼太咸')
        if key in SENSITIVE_KEYS:
            raise ValueError('敏感设置不能通过通用接口修改')
        stmt = select(SystemSetting).where(SystemSetting.key == key)
        result = await self.session.execute(stmt)
        record = result.scalars().first()
        safe_value = value if key in NO_ESCAPE_KEYS else escape_xss(value)
        safe_description = escape_xss(description) if description else None
        if record:
            record.value = safe_value
            if description is not None:
                record.description = safe_description
        else:
            record = SystemSetting(key=key, value=safe_value, description=safe_description)
        self.session.add(record)
        await self.session.commit()

    async def set_settings(self, settings: Dict[str, tuple[str, str | None]]) -> None:
        """在同一事务中批量保存系统设置。

        Args:
            settings: ``{设置键: (设置值, 设置说明)}`` 映射。

        Returns:
            无返回值；全部设置成功后统一提交。
        """
        if not settings:
            return
        if any((is_removed_setting(key) for key in settings)):
            raise ValueError('此功能不属于鱼太咸')
        sensitive_keys = set(settings).intersection(SENSITIVE_KEYS)
        if sensitive_keys:
            raise ValueError('敏感设置不能通过通用接口修改')
        stmt = select(SystemSetting).where(SystemSetting.key.in_(tuple(settings.keys())))
        result = await self.session.execute(stmt)
        records = {record.key: record for record in result.scalars().all()}
        for (key, (value, description)) in settings.items():
            safe_value = value if key in NO_ESCAPE_KEYS else escape_xss(value)
            safe_description = escape_xss(description) if description else None
            record = records.get(key)
            if record:
                record.value = safe_value
                if description is not None:
                    record.description = safe_description
            else:
                record = SystemSetting(key=key, value=safe_value, description=safe_description)
            self.session.add(record)
        await self.session.commit()
