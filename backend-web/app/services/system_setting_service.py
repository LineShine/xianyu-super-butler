"""
系统设置服务

功能：
1. 系统设置CRUD操作
2. 敏感设置过滤
3. 键值对存储和查询
4. XSS防护
"""
from __future__ import annotations
from common.utils.product_settings import is_removed_setting
from typing import Dict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from common.models.system_setting import SystemSetting
from common.services.remote_token_api import TOKEN_REMOTE_SECRET_KEY_SETTING_KEY, TOKEN_REMOTE_URL_SETTING_KEY
from common.services.token_api_mode import TOKEN_API_MODE_SETTING_KEY, normalize_token_api_mode
from common.utils.text_utils import escape_xss
SENSITIVE_KEYS = {'admin_password_hash', 'security.internal_api_token', 'security.jwt_secret_key'}
DEFAULT_DISCLAIMER_CONTENT = '数据存储说明\n1. 本系统在运行过程中，为保障服务正常运行，会存储用户账号密码、登录 Cookie、商品信息、卡券信息等业务数据。\n2. 上述数据仅用于系统功能运行、自动化处理和业务管理，不作为其他用途。\n3. 请您自行确认服务器环境、账号权限和数据保管措施的安全性。\n\n用户须知\n1. 用户应确保使用本系统的行为符合相关平台规则和法律法规。\n2. 因用户自身违规操作、账号共享、密码泄露、服务器安全问题导致的损失，由用户自行承担。\n3. 建议用户定期备份重要数据，因系统故障、第三方平台变更、不可抗力等导致的异常或损失，本系统不承担责任。\n4. 本系统依赖第三方平台接口和网络环境，无法保证服务始终连续、稳定、无中断。\n\n隐私与风险提示\n1. 请勿在未充分评估风险的情况下接入生产环境或敏感账号。\n2. 使用本系统即表示您已充分理解并接受相关风险，并愿意自行承担相应责任。'
DEFAULT_LOGIN_SYSTEM_TITLE = '让店铺管理更轻松'
DEFAULT_LOGIN_SYSTEM_DESCRIPTION = '自动回复、智能客服、订单管理、数据分析，一站式解决闲鱼运营难题'
DEFAULT_SYSTEM_SETTINGS: dict[str, tuple[str, str | None]] = {'disclaimer.title': ('免责声明', '系统免责声明标题'), 'disclaimer.content': (DEFAULT_DISCLAIMER_CONTENT, '系统免责声明正文'), 'disclaimer.checkbox_text': ('我已阅读并同意以上免责声明', '免责声明勾选提示文案'), 'disclaimer.agree_button_text': ('同意并继续', '免责声明同意按钮文案'), 'disclaimer.disagree_button_text': ('不同意', '免责声明不同意按钮文案'), 'login.system_name': ('鱼太咸', '登录页系统名称'), 'login.system_title': (DEFAULT_LOGIN_SYSTEM_TITLE, '登录页系统标题'), 'login.system_description': (DEFAULT_LOGIN_SYSTEM_DESCRIPTION, '登录页系统描述'), 'theme.effect': ('solid', '系统主题效果（solid-纯色，gradient-炫彩）'), 'theme.color_preset': ('ocean', '系统主题颜色预设'), 'theme.font_family': ('system', '系统主题字体预设'), 'log.retention_days': ('7', '日志保留天数（所有模块生效，修改后实时刷新各服务日志策略）'), 'account.face_verify_timeout_disable': ('true', '人脸验证超时是否自动禁用账号'), 'proxy.api_url': ('', '代理 API 的 URL（可能较长，用于配置外部代理服务）'), 'proxy.enabled': ('false', '是否启用代理（true/false）'), 'user.register_default_days': ('', '注册用户默认有效天数（空=不设置到期日）'), 'captcha.real_mouse_weight_local': ('1', 'real_mouse过滑块本地排队权重'), 'captcha.real_mouse_weight_remote': ('1', 'real_mouse过滑块远程排队权重'), 'captcha.block_remote_calls': ('true', '是否禁止外部远程调用backend-web过滑块接口'), 'captcha.local_slider_disabled': ('false', '本机是否停止处理滑块；缓存缺失时仍请求Token接口'), 'captcha.remote_processing_max': ('20', '远程调用允许的最大处理中滑块日志数，0=不限制'), 'captcha.remote_cooldown_seconds': ('600', '远程调用达到处理中上限后的冷却秒数，0=不冷却'), 'captcha.remote_cooldown_until': ('0', '远程过滑块调用冷却截止时间戳'), 'captcha.slider_mode': ('browser', '滑块滑动方式：browser/real_mouse'), 'password_login.mode': ('browser', '账号密码登录模式：protocol/browser'), 'password_login.remote_url': ('', '协议登录远程接口URL'), 'password_login.remote_secret_key': ('', '协议登录远程接口秘钥'), 'token.api_mode': ('web', 'Token获取方式：web-网页接口/remote-远程接口'), 'token.remote_url': ('', 'Token远程接口URL'), 'token.remote_secret_key': ('', 'Token远程接口秘钥')}
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
