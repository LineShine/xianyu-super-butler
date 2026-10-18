"""鱼太咸新安装的系统设置默认值。

数据库初始化和 Web 设置补全共用此定义，只补齐缺失项，不覆盖已保存设置。
默认值按 2026-10-18 的产品配置整理；不复制邮箱凭据、实例密钥或运行状态。
远程服务凭据保持空值，冷却截止时间从 0 开始。
"""
from __future__ import annotations

DEFAULT_SYSTEM_SETTINGS: dict[str, tuple[str, str | None]] = {
    'login_captcha_enabled': ('false', '登录页是否启用滑动验证码'),
    'registration_enabled': ('true', '登录页是否显示注册入口'),
    'show_default_login_info': ('true', '登录页是否展示默认账号密码提示'),
    'navigation.hidden_menu_keys': ('[]', '普通用户隐藏的菜单项；空列表表示全部显示'),
    'disclaimer.title': ('使用说明', '系统免责声明标题'),
    'disclaimer.content': ('请妥善保管账号和业务数据，并遵守平台规则。', '系统免责声明正文'),
    'disclaimer.checkbox_text': ('我已阅读并同意以上免责声明', '免责声明勾选提示文案'),
    'disclaimer.agree_button_text': ('同意并继续', '免责声明同意按钮文案'),
    'disclaimer.disagree_button_text': ('不同意', '免责声明不同意按钮文案'),
    'login.system_name': ('鱼太咸', '登录页系统名称'),
    'login.system_title': ('鱼太咸', '登录页系统标题'),
    'login.system_description': ('让店铺管理更轻松', '登录页系统描述'),
    'theme.effect': ('solid', '系统主题效果（solid-纯色，gradient-炫彩）'),
    'theme.color_preset': ('ocean', '系统主题颜色预设'),
    'theme.font_family': ('system', '系统主题字体预设'),
    'log.retention_days': ('7', '日志保留天数（所有模块生效，修改后实时刷新各服务日志策略）'),
    'account.face_verify_timeout_disable': ('false', '人脸验证超时是否自动禁用账号'),
    'proxy.api_url': ('', '代理 API 的 URL（可能较长，用于配置外部代理服务）'),
    'proxy.enabled': ('false', '是否启用代理（true/false）'),
    'user.register_default_days': ('', '注册用户默认有效天数（空=不设置到期日）'),
    'captcha.real_mouse_weight_local': ('1', 'real_mouse过滑块本地排队权重'),
    'captcha.real_mouse_weight_remote': ('1', 'real_mouse过滑块远程排队权重'),
    'captcha.block_remote_calls': ('true', '是否禁止外部远程调用backend-web过滑块接口'),
    'captcha.local_slider_disabled': ('false', '本机是否停止处理滑块；缓存缺失时仍请求Token接口'),
    'captcha.remote_processing_max': ('20', '远程调用允许的最大处理中滑块日志数，0=不限制'),
    'captcha.remote_cooldown_seconds': ('600', '远程调用达到处理中上限后的冷却秒数，0=不冷却'),
    'captcha.remote_cooldown_until': ('0', '远程过滑块调用冷却截止时间戳'),
    'captcha.slider_mode': ('browser', '滑块滑动方式：browser/real_mouse'),
    'password_login.mode': ('browser', '账号密码登录模式：protocol/browser'),
    'password_login.remote_url': ('', '协议登录远程接口URL'),
    'password_login.remote_secret_key': ('', '协议登录远程接口秘钥'),
    'token.api_mode': ('web', 'Token获取方式：web-网页接口/remote-远程接口'),
    'token.remote_url': ('', 'Token远程接口URL'),
    'token.remote_secret_key': ('', 'Token远程接口秘钥'),
}
