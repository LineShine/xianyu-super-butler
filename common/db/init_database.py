"""
数据库初始化脚本

功能：
1. 创建所有数据表（如果不存在）
2. 创建默认管理员用户 (admin/admin123)
3. 初始化系统设置

使用方法：
- 服务器启动时自动调用
- 也可以手动运行：python -m common.db.init_database
"""
from __future__ import annotations
import logging
import warnings
from contextlib import asynccontextmanager, contextmanager
from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SAWarning
from common.db.default_publish_addresses import REMOVED_PUBLISH_ADDRESS_PREFIXES, build_default_publish_addresses
from common.db.auto_relist_schema import ensure_auto_relist_schema
from common.db.session import async_engine, async_session_maker
from common.utils.time_utils import get_beijing_now_naive
from common.utils.security import generate_secret_key, get_password_hash
DDL_LOCK_WAIT_TIMEOUT_SECONDS = 30
INIT_ADVISORY_LOCK_NAME = 'ytx:db_init'
INIT_ADVISORY_LOCK_WAIT_SECONDS = 300

@contextmanager
def suppress_db_warnings():
    """
    上下文管理器：抑制数据库初始化时的重复警告日志
    
    包括：
    - Table 'xxx' already exists
    - Duplicate entry 'xxx' for key 'PRIMARY'
    """
    sqlalchemy_logger = logging.getLogger('sqlalchemy.engine')
    original_level = sqlalchemy_logger.level
    warnings.filterwarnings('ignore', category=SAWarning)

    class DBInitFilter(logging.Filter):
        """过滤数据库初始化时的常见警告"""
        IGNORE_PATTERNS = ['already exists', 'Duplicate entry']

        def filter(self, record):
            msg = record.getMessage()
            for pattern in self.IGNORE_PATTERNS:
                if pattern in msg:
                    return False
            return True
    db_filter = DBInitFilter()
    loggers_to_filter = [logging.getLogger('sqlalchemy.engine'), logging.getLogger('sqlalchemy.pool'), logging.getLogger('asyncmy'), logging.getLogger()]
    for lg in loggers_to_filter:
        lg.addFilter(db_filter)
    try:
        yield
    finally:
        for lg in loggers_to_filter:
            lg.removeFilter(db_filter)
        warnings.filterwarnings('default', category=SAWarning)

def describe_ddl_error(exc: Exception) -> str:
    """
    把建表/改表异常转成带排查提示的信息。

    Args:
        exc: DDL 执行抛出的异常。
    Returns:
        原始错误信息；锁等待超时时追加持锁方的排查指引。
    """
    message = str(exc)
    lowered = message.lower()
    if 'lock wait timeout' in lowered or 'metadata lock' in lowered:
        return f'{message} ｜ 该表被其他连接的事务或 ALTER 占用元数据锁，已按 {DDL_LOCK_WAIT_TIMEOUT_SECONDS} 秒上限放弃等待。排查：SELECT trx_mysql_thread_id, trx_started, trx_query FROM information_schema.innodb_trx ORDER BY trx_started; 确认非必要事务后 KILL 对应线程再重启服务'
    return message

async def _apply_lock_wait_timeout(conn) -> None:
    """
    给当前连接设置锁等待上限，避免建表/改表卡在元数据锁上无限等待。

    Args:
        conn: 已建立的数据库连接。
    Returns:
        无返回值；非 MySQL 或无权限时只记录告警，沿用数据库默认值。
    """
    try:
        await conn.execute(text(f'SET SESSION lock_wait_timeout = {DDL_LOCK_WAIT_TIMEOUT_SECONDS}'))
        await conn.execute(text(f'SET SESSION innodb_lock_wait_timeout = {DDL_LOCK_WAIT_TIMEOUT_SECONDS}'))
    except Exception as exc:
        logger.warning(f'设置锁等待上限失败，本次自检沿用数据库默认超时: {exc}')

@asynccontextmanager
async def ddl_connection():
    """
    获取执行建表/改表用的连接，已带锁等待上限。

    Returns:
        事务内的数据库连接，退出时自动提交。
    """
    async with async_engine.begin() as conn:
        await _apply_lock_wait_timeout(conn)
        yield conn

@asynccontextmanager
async def init_advisory_lock():
    """
    用 MySQL 命名锁把多服务的启动自检串行化。

    拿不到锁（超时或数据库不支持）时不阻断启动，只记录告警后继续执行自检：
    此时建表/改表已有 30 秒锁等待上限，最坏情况是单张表报错并在日志中写明，
    不会像以前那样无提示地永久卡住。

    Returns:
        上下文本身不返回值，退出时释放命名锁。
    """
    acquired = False
    conn = None
    try:
        conn = await async_engine.connect()
        result = await conn.execute(text('SELECT GET_LOCK(:lock_name, :wait_seconds)'), {'lock_name': INIT_ADVISORY_LOCK_NAME, 'wait_seconds': INIT_ADVISORY_LOCK_WAIT_SECONDS})
        acquired = result.scalar() == 1
        if not acquired:
            logger.warning(f'等待 {INIT_ADVISORY_LOCK_WAIT_SECONDS} 秒仍未取得数据库自检锁 {INIT_ADVISORY_LOCK_NAME}，可能有其他服务正在执行自检；本次继续执行，如遇表被占用会在日志中逐表提示')
    except Exception as exc:
        logger.warning(f'获取数据库自检锁失败（不影响自检执行）: {exc}')
    try:
        yield
    finally:
        if conn is not None:
            try:
                if acquired:
                    await conn.execute(text('SELECT RELEASE_LOCK(:lock_name)'), {'lock_name': INIT_ADVISORY_LOCK_NAME})
            except Exception as exc:
                logger.warning(f'释放数据库自检锁失败（连接关闭后会自动释放）: {exc}')
            finally:
                await conn.close()

class DatabaseInitializer:
    """数据库初始化器"""
    DEFAULT_SETTINGS = (('disclaimer.title', '免责声明', '系统免责声明标题'),
     ('disclaimer.content',
      '数据存储说明\n'
      '1. 本系统在运行过程中，为保障服务正常运行，会存储用户账号密码、登录 Cookie、商品信息、卡券信息等业务数据。\n'
      '2. 上述数据仅用于系统功能运行、自动化处理和业务管理，不作为其他用途。\n'
      '3. 请您自行确认服务器环境、账号权限和数据保管措施的安全性。\n'
      '\n'
      '用户须知\n'
      '1. 用户应确保使用本系统的行为符合相关平台规则和法律法规。\n'
      '2. 因用户自身违规操作、账号共享、密码泄露、服务器安全问题导致的损失，由用户自行承担。\n'
      '3. 建议用户定期备份重要数据，因系统故障、第三方平台变更、不可抗力等导致的异常或损失，本系统不承担责任。\n'
      '4. 本系统依赖第三方平台接口和网络环境，无法保证服务始终连续、稳定、无中断。\n'
      '\n'
      '隐私与风险提示\n'
      '1. 请勿在未充分评估风险的情况下接入生产环境或敏感账号。\n'
      '2. 使用本系统即表示您已充分理解并接受相关风险，并愿意自行承担相应责任。',
      '系统免责声明正文'),
     ('disclaimer.checkbox_text', '我已阅读并同意以上免责声明', '免责声明勾选提示文案'),
     ('disclaimer.agree_button_text', '同意并继续', '免责声明同意按钮文案'),
     ('disclaimer.disagree_button_text', '不同意', '免责声明不同意按钮文案'),
     ('login.system_name', '鱼太咸', '登录页系统名称'),
     ('login.system_title', '让店铺管理更轻松', '登录页系统标题'),
     ('login.system_description', '自动回复、智能客服、订单管理、数据分析，一站式解决闲鱼运营难题', '登录页系统描述'),
     ('theme.effect', 'solid', '系统主题效果（solid-纯色，gradient-炫彩）'),
     ('theme.color_preset', 'ocean', '系统主题颜色预设'),
     ('log.retention_days', '7', '日志保留天数（所有模块生效，修改后重启服务生效）'),
     ('password_login.mode', 'browser', '账号密码登录模式：protocol/browser'),
     ('captcha.real_mouse_weight_local', '1', 'real_mouse过滑块本地排队权重'),
     ('captcha.real_mouse_weight_remote', '1', 'real_mouse过滑块远程排队权重'),
     ('captcha.block_remote_calls', 'true', '是否禁止外部远程调用backend-web过滑块接口'),
     ('captcha.local_slider_disabled', 'false', '本机是否停止处理滑块；缓存缺失时仍请求Token接口'),
     ('captcha.slider_mode', 'browser', '滑块滑动方式：browser/real_mouse'),
     ('token.api_mode', 'web', 'Token获取方式：web-网页接口/remote-远程接口'),
     ('captcha.remote_processing_max', '20', '远程调用允许的最大处理中滑块日志数，0=不限制'),
     ('captcha.remote_cooldown_seconds', '600', '远程调用达到处理中上限后的冷却秒数，0=不冷却'),
     ('captcha.remote_cooldown_until', '0', '远程过滑块调用冷却截止时间戳'),
     ('show_default_login_info', 'true', '登录页是否展示默认账号密码提示'))
    DEFAULT_SCHEDULED_TASKS = (('redelivery', '补发货任务', 5, True, '定时补发货任务'), ('rate', '补评价任务', 20, True, '定时补评价任务'), ('polish', '擦亮任务', 60, True, '定时擦亮商品任务'), ('day_switch', '平台日切换任务', 60, True, '定时执行平台日切换任务'), ('cleanup_browser_data', '清理被禁用账号浏览器数据任务', 600, False, '定时清理被禁用账号的浏览器数据'), ('cleanup_unconfigured_browser_data', '清理未配置账号密码的浏览器数据任务', 10800, False, '定时清理未配置登录账号密码（username 或 login_password 为空）账号的浏览器数据'), ('fetch_orders', '获取闲鱼订单任务', 600, True, '定时获取闲鱼订单数据'), ('fetch_pending_orders', '获取待发货订单任务', 60, True, '定时获取待发货订单并同步收货人姓名/手机号/地址等信息'), ('fetch_refund_orders', '退款订单获取任务', 120, True, '定时获取退款订单数据，更新订单状态并触发退款订单注销'), ('fetch_items', '获取闲鱼商品任务', 1200, True, '定时获取所有启用账号的闲鱼在售商品并入库（新增或更新）'), ('login_renew', '登录续期任务', 600, False, '定时执行闲鱼账号登录续期'), ('token_renewal', 'Token续期任务', 20, True, '定时为未来1小时内到期的启用账号预取IM Token，并写入续期到期日'), ('cookies_refresh', 'COOKIES续期任务', 600, False, '定时执行闲鱼账号浏览器COOKIES续期'), ('api_cookie_renew', '接口续期Cookies任务', 3600, True, '定时通过 hasLogin.do 接口为启用账号续期Cookies并同步Set-Cookie'), ('close_notice', '关闭账号消息通知任务', 600, False, '定时关闭账号消息通知'), ('red_flower', '求小红花任务', 300, True, '定时自动求小红花'), ('db_backup', '数据库备份任务', 3600, True, '定时备份数据库所有表结构与数据到文件'), ('delivery_timeout', '发货超时检测任务', 60, True, '定时将超过阈值仍处于 unknown 的自动发货消息日志标记为 timeout'), ('listing_monitor', '商品监控任务', 60, True, '定时执行商品监控：按监控类型调用闲鱼搜索接口采集商品并入库，每次记录监控日志'), ('seller_fill', '采集商品卖家ID补全', 60, True, '定时查询采集商品中卖家ID为空的数据，调用商品详情接口补全卖家真实ID与详情'), ('dm_send', '采集商品发送私信', 60, True, '定时查询卖家ID已补全且未私信的采集商品，用监控任务配置的私信账号发起私信'), ('auto_order', '采集商品自动下单', 60, True, '定时查询已私信且未下单的采集商品，用监控任务配置的下单账号创建订单（拍下，不自动付款）'), ('image_cleanup', '图片清理', 1200, True, '定时扫描卡券与素材库专属图片目录，删除已删除对象遗留的孤儿图片（仅清理各自目录，不影响其它功能图片）'), ('auto_relist_scan', '商品自动续售', 5, True, '检测已成交并完成发货的商品，确认旧商品下架后使用原素材自动续售'))
    TABLES_DDL = {
        'ytx_users': """
                CREATE TABLE IF NOT EXISTS ytx_users (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '用户ID',
                    external_id VARCHAR(64) COMMENT '外部ID',
                    username VARCHAR(64) NOT NULL UNIQUE COMMENT '用户名',
                    email VARCHAR(255) NOT NULL UNIQUE COMMENT '邮箱',
                    phone VARCHAR(32) COMMENT '手机号',
                    password_hash VARCHAR(255) NOT NULL COMMENT '密码哈希',
                    status ENUM('ACTIVE', 'INACTIVE', 'SUSPENDED', 'DELETED') DEFAULT 'ACTIVE' COMMENT '用户状态',
                    role ENUM('ADMIN', 'OPERATOR', 'MEMBER') DEFAULT 'MEMBER' COMMENT '用户角色',
                    account_limit INT DEFAULT NULL COMMENT '可添加账号数量',
                    last_login_at DATETIME COMMENT '最后登录时间',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_external_id (external_id),
                    INDEX idx_username (username),
                    INDEX idx_email (email),
                    INDEX idx_user_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户表';
            """,
        'ytx_user_settings': """
                CREATE TABLE IF NOT EXISTS ytx_user_settings (
                    id INT PRIMARY KEY AUTO_INCREMENT COMMENT '设置ID',
                    user_id INT NOT NULL COMMENT '用户ID',
                    `key` VARCHAR(120) NOT NULL COMMENT '设置键',
                    value TEXT NOT NULL COMMENT '设置值',
                    description TEXT COMMENT '设置描述',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_user_id (user_id),
                    UNIQUE KEY uk_user_key (user_id, `key`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户设置表';
            """,
        'ytx_system_settings': """
                CREATE TABLE IF NOT EXISTS ytx_system_settings (
                    `key` VARCHAR(120) PRIMARY KEY COMMENT '设置键',
                    value TEXT NOT NULL COMMENT '设置值',
                    description TEXT COMMENT '设置描述',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='系统设置表';
            """,
        'ytx_accounts': """
                CREATE TABLE IF NOT EXISTS ytx_accounts (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '账号ID',
                    owner_id BIGINT NOT NULL COMMENT '所属用户ID',
                    account_id VARCHAR(80) NOT NULL COMMENT '账号标识',
                    display_name VARCHAR(120) COMMENT '显示名称',
                    unb VARCHAR(64) COMMENT 'UNB标识',
                    cookie TEXT NOT NULL COMMENT 'Cookie信息',
                    login_method VARCHAR(20) NOT NULL COMMENT '登录方式',
                    status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT '账号状态',
                    username VARCHAR(120) COMMENT '登录用户名',
                    login_password TEXT COMMENT '登录密码',
                    remark VARCHAR(255) COMMENT '备注',
                    pause_duration INT DEFAULT 10 COMMENT '暂停时长(分钟)',
                    auto_confirm TINYINT(1) DEFAULT 0 COMMENT '自动确认发货',
                    show_browser TINYINT(1) DEFAULT 0 COMMENT '显示浏览器',
                    metadata JSON COMMENT '元数据',
                    last_login_at DATETIME COMMENT '最后登录时间',
                    last_refresh_at DATETIME COMMENT '最后刷新时间',
                    proxy_type VARCHAR(20) DEFAULT 'none' COMMENT '代理类型',
                    proxy_host VARCHAR(255) COMMENT '代理主机',
                    proxy_port INT COMMENT '代理端口',
                    proxy_user VARCHAR(120) COMMENT '代理用户名',
                    proxy_pass VARCHAR(255) COMMENT '代理密码',
                    message_expire_time INT DEFAULT 3600 COMMENT '相同消息等待时间(秒)',
                    reply_delay_seconds INT DEFAULT 0 COMMENT '自动回复延迟时间(秒)，0表示立即回复',
                    disable_reason VARCHAR(255) COMMENT '禁用原因',
                    scheduled_redelivery TINYINT(1) NOT NULL DEFAULT 0 COMMENT '定时补发货开关',
                    scheduled_rate TINYINT(1) NOT NULL DEFAULT 0 COMMENT '定时补评价开关',
                    auto_polish TINYINT(1) NOT NULL DEFAULT 0 COMMENT '商品自动擦亮开关',
                    confirm_before_send TINYINT(1) NOT NULL DEFAULT 0 COMMENT '发货成功再发卡券开关',
                    only_send_card TINYINT(1) NOT NULL DEFAULT 0 COMMENT '只发卡券不确认发货开关',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_owner_id (owner_id),
                    UNIQUE KEY uk_account_id (account_id),
                    INDEX idx_unb (unb),
                    INDEX idx_account_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='闲鱼账号表';
            """,
        'ytx_keyword_rules': """
                CREATE TABLE IF NOT EXISTS ytx_keyword_rules (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '规则ID',
                    owner_id BIGINT NOT NULL COMMENT '所属用户ID',
                    account_id BIGINT COMMENT '关联账号ID',
                    keyword VARCHAR(120) NOT NULL COMMENT '关键词',
                    reply_content TEXT COMMENT '回复内容',
                    reply_type VARCHAR(32) COMMENT '回复类型(text/image/external_contact)',
                    image_url VARCHAR(512) COMMENT '图片URL',
                    location_name VARCHAR(255) COMMENT '站外联系方式定位名称',
                    location_longitude VARCHAR(32) COMMENT '站外联系方式经度',
                    location_latitude VARCHAR(32) COMMENT '站外联系方式纬度',
                    location_title VARCHAR(128) COMMENT '站外联系方式位置标题',
                    location_subtitle VARCHAR(255) COMMENT '站外联系方式位置副标题',
                    item_id VARCHAR(64) COMMENT '商品ID',
                    priority INT DEFAULT 100 COMMENT '优先级',
                    is_active TINYINT(1) DEFAULT 1 COMMENT '是否启用',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_owner_id (owner_id),
                    INDEX idx_account_id (account_id),
                    INDEX idx_keyword (keyword),
                    INDEX idx_kw_account_item (account_id, item_id),
                    INDEX idx_kw_account_active (account_id, is_active)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='关键词规则表';
            """,
        'ytx_catalog_items': """
                CREATE TABLE IF NOT EXISTS ytx_catalog_items (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '商品ID',
                    owner_id BIGINT NOT NULL COMMENT '所属用户ID',
                    account_id BIGINT NOT NULL COMMENT '关联账号ID',
                    item_id VARCHAR(64) NOT NULL COMMENT '商品标识',
                    title VARCHAR(255) COMMENT '商品标题',
                    price VARCHAR(32) COMMENT '商品价格',
                    ai_prompt TEXT COMMENT '商品AI提示词',
                    is_polished TINYINT(1) DEFAULT 0 COMMENT '是否擦亮',
                    metadata JSON COMMENT '商品元数据',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_owner_id (owner_id),
                    INDEX idx_account_id (account_id),
                    INDEX idx_item_id (item_id),
                    INDEX idx_cat_account_item (account_id, item_id),
                    INDEX idx_cat_owner_created (owner_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商品目录表';
            """,
        'ytx_orders': """
                CREATE TABLE IF NOT EXISTS ytx_orders (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '订单ID',
                    owner_id BIGINT NOT NULL COMMENT '所属用户ID',
                    order_no VARCHAR(64) NOT NULL COMMENT '订单号',
                    status VARCHAR(32) NOT NULL COMMENT '订单状态',
                    buyer_nick VARCHAR(120) COMMENT '买家昵称',
                    buyer_id VARCHAR(64) COMMENT '买家ID',
                    chat_id VARCHAR(64) COMMENT '聊天会话ID',
                    item_id VARCHAR(64) COMMENT '商品ID',
                    spec_name VARCHAR(120) COMMENT '规格名称',
                    spec_value VARCHAR(120) COMMENT '规格值',
                    quantity INT DEFAULT 1 COMMENT '数量',
                    amount DECIMAL(12,2) COMMENT '金额',
                    currency VARCHAR(8) DEFAULT 'CNY' COMMENT '货币',
                    account_id VARCHAR(64) COMMENT '账号标识',
                    account_name VARCHAR(120) COMMENT '账号名称',
                    is_bargain TINYINT(1) DEFAULT 0 COMMENT '是否小刀',
                    receiver_name VARCHAR(120) COMMENT '收货人姓名',
                    receiver_phone VARCHAR(32) COMMENT '收货人手机号',
                    receiver_address VARCHAR(512) COMMENT '收货地址',
                    delivery_fail_reason VARCHAR(2000) COMMENT '发货失败原因',
                    card_only_delivered TINYINT(1) NOT NULL DEFAULT 0 COMMENT '仅发卡券流程是否已处理',
                    item_snapshot JSON COMMENT '商品快照',
                    metadata JSON COMMENT '元数据',
                    source VARCHAR(32) COMMENT '数据来源：fetch_xianyu-获取闲鱼订单按钮',
                    placed_at DATETIME COMMENT '下单时间',
                    synced_at DATETIME COMMENT '同步时间',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    UNIQUE KEY uk_order_account_no (account_id, order_no),
                    INDEX idx_owner_id (owner_id),
                    INDEX idx_order_no (order_no),
                    INDEX idx_account_id (account_id),
                    INDEX idx_order_created_at (created_at),
                    INDEX idx_order_placed_status (placed_at, status),
                    INDEX idx_order_created_status (created_at, status),
                    INDEX idx_order_owner_placed (owner_id, placed_at),
                    INDEX idx_order_owner_created (owner_id, created_at),
                    INDEX idx_order_owner_account_placed (owner_id, account_id, placed_at),
                    INDEX idx_order_owner_account_buyer_created (owner_id, account_id, buyer_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='订单表';
            """,
        'ytx_cards': """
                CREATE TABLE IF NOT EXISTS ytx_cards (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '卡券ID',
                    user_id BIGINT NOT NULL COMMENT '所属用户ID',
                    item_id VARCHAR(64) COMMENT '关联商品ID',
                    name VARCHAR(255) NOT NULL COMMENT '卡券名称',
                    type VARCHAR(50) NOT NULL COMMENT '卡券类型(api/text/data/image)',
                    description TEXT COMMENT '卡券描述',
                    enabled TINYINT(1) DEFAULT 1 COMMENT '是否启用',
                    delay_seconds INT DEFAULT 0 COMMENT '延迟秒数',
                    use_no_logistics_form TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否通过无需邮寄表单发货',
                    delivery_count INT DEFAULT 0 COMMENT '发货次数',
                    is_multi_spec TINYINT(1) DEFAULT 0 COMMENT '是否多规格',
                    spec_name VARCHAR(255) COMMENT '规格名称',
                    spec_value VARCHAR(255) COMMENT '规格值',
                    api_config TEXT COMMENT 'API配置(JSON)',
                    text_content TEXT COMMENT '文本内容',
                    data_content TEXT COMMENT '数据内容',
                    image_url VARCHAR(512) COMMENT '图片URL',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_user_id (user_id),
                    INDEX idx_item_id (item_id),
                    INDEX idx_card_user_item (user_id, item_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='卡券表';
            """,
        'ytx_default_replies': """
                CREATE TABLE IF NOT EXISTS ytx_default_replies (
                    id INT PRIMARY KEY AUTO_INCREMENT COMMENT '回复ID',
                    account_id VARCHAR(80) NOT NULL COMMENT '账号标识',
                    item_id VARCHAR(64) DEFAULT NULL COMMENT '商品ID(空为账号默认回复)',
                    enabled TINYINT(1) DEFAULT 0 COMMENT '是否启用',
                    reply_type VARCHAR(32) DEFAULT 'text' COMMENT '回复类型：text-文本，api-接口，external_contact-站外联系方式',
                    reply_content TEXT COMMENT '回复内容',
                    reply_image VARCHAR(512) COMMENT '回复图片URL',
                    api_url VARCHAR(1024) DEFAULT NULL COMMENT 'API地址(reply_type=api时POST此地址)',
                    api_timeout INT DEFAULT 80 COMMENT 'API请求超时时间(秒)',
                    location_name VARCHAR(255) DEFAULT NULL COMMENT '站外联系方式定位名称',
                    location_longitude VARCHAR(32) DEFAULT NULL COMMENT '站外联系方式经度',
                    location_latitude VARCHAR(32) DEFAULT NULL COMMENT '站外联系方式纬度',
                    location_title VARCHAR(128) DEFAULT NULL COMMENT '站外联系方式位置标题',
                    location_subtitle VARCHAR(255) DEFAULT NULL COMMENT '站外联系方式位置副标题',
                    reply_once TINYINT(1) DEFAULT 0 COMMENT '只回复一次',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_account_id (account_id),
                    INDEX idx_item_id (item_id),
                    UNIQUE KEY uk_account_item (account_id, item_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='默认回复表';
            """,
        'ytx_default_reply_records': """
                CREATE TABLE IF NOT EXISTS ytx_default_reply_records (
                    id INT PRIMARY KEY AUTO_INCREMENT COMMENT '记录ID',
                    account_id VARCHAR(80) NOT NULL COMMENT '账号标识',
                    item_id VARCHAR(64) DEFAULT NULL COMMENT '商品ID(空为账号默认回复)',
                    user_id VARCHAR(64) NOT NULL COMMENT '被回复用户ID',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    INDEX idx_account_id (account_id),
                    INDEX idx_user_id (user_id),
                    INDEX idx_account_item_user (account_id, item_id, user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='默认回复记录表';
            """,
        'ytx_ai_chat_messages': """
                CREATE TABLE IF NOT EXISTS ytx_ai_chat_messages (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '消息ID',
                    chat_id VARCHAR(64) NOT NULL COMMENT '聊天ID',
                    cookie_id VARCHAR(80) NOT NULL COMMENT '账号标识',
                    user_id VARCHAR(64) NOT NULL COMMENT '用户ID',
                    item_id VARCHAR(64) COMMENT '商品ID',
                    role VARCHAR(20) NOT NULL COMMENT '角色(user/assistant)',
                    content TEXT NOT NULL COMMENT '消息内容',
                    intent VARCHAR(20) COMMENT '意图(price/tech/default)',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    INDEX idx_chat_id (chat_id),
                    INDEX idx_cookie_id (cookie_id),
                    INDEX ix_ai_chat_messages_chat_cookie (chat_id, cookie_id),
                    INDEX ix_ai_chat_messages_intent (cookie_id, intent)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI聊天消息表';
            """,
        'ytx_risk_control_logs': """
                CREATE TABLE IF NOT EXISTS ytx_risk_control_logs (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '日志ID',
                    owner_id BIGINT COMMENT '所属用户ID',
                    account_id BIGINT COMMENT '关联账号ID',
                    account_identifier VARCHAR(80) COMMENT '账号标识',
                    event_type VARCHAR(64) DEFAULT 'slider_captcha' COMMENT '事件类型',
                    event_description TEXT COMMENT '事件描述',
                    processing_result TEXT COMMENT '处理结果',
                    processing_status VARCHAR(32) DEFAULT 'processing' COMMENT '处理状态',
                    captcha_engine VARCHAR(32) DEFAULT NULL COMMENT '验证通过引擎：playwright-主引擎/drissionpage-兜底引擎/real_mouse-真人鼠标引擎',
                    call_type VARCHAR(16) DEFAULT 'local' COMMENT '调用类型：local-本机/remote-远程(外部凭秘钥调用)',
                    call_user VARCHAR(128) DEFAULT NULL COMMENT '调用用户：仅远程调用记录(按秘钥查到的用户名)',
                    error_message TEXT COMMENT '错误信息',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_owner_id (owner_id),
                    INDEX idx_account_id (account_id),
                    INDEX idx_event_type (event_type),
                    INDEX idx_rcl_account_status (account_id, processing_status),
                    INDEX idx_rcl_identifier_status_created (account_identifier, processing_status, created_at),
                    INDEX idx_rcl_status_event (processing_status, event_type),
                    INDEX idx_rcl_owner_created (owner_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='风控日志表';
            """,
        'ytx_account_login_logs': """
                CREATE TABLE IF NOT EXISTS ytx_account_login_logs (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '日志ID',
                    owner_id BIGINT COMMENT '所属用户ID',
                    account_id BIGINT COMMENT '关联账号ID（ytx_accounts.id）',
                    account_identifier VARCHAR(80) COMMENT '业务账号ID',
                    username VARCHAR(255) COMMENT '登录用户名快照',
                    trigger_reason VARCHAR(128) COMMENT '触发本次登录的原因',
                    login_status VARCHAR(32) DEFAULT 'failed' COMMENT '登录状态：success/failed/skipped_cooldown/no_credentials',
                    failure_reason VARCHAR(64) COMMENT '失败大类：bad_credentials/baxia_punish_captcha/account_info_missing/exception/...',
                    error_message TEXT COMMENT '详细错误消息',
                    updated_cookie_names VARCHAR(500) DEFAULT NULL COMMENT '接口续期更新的Cookie字段名（逗号分隔）',
                    duration_ms INT COMMENT '整个登录流程耗时（毫秒）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    INDEX idx_all_owner_id (owner_id),
                    INDEX idx_all_account_id (account_id),
                    INDEX idx_all_login_status (login_status),
                    INDEX idx_all_identifier_status_created (account_identifier, login_status, created_at),
                    INDEX idx_all_owner_created (owner_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='账号登录日志表';
            """,
        'ytx_user_login_logs': """
                CREATE TABLE IF NOT EXISTS ytx_user_login_logs (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '日志ID',
                    owner_id BIGINT DEFAULT NULL COMMENT '登录用户ID（登录失败时为空）',
                    login_account VARCHAR(255) DEFAULT NULL COMMENT '登录账号（用户名或邮箱）',
                    login_method VARCHAR(32) NOT NULL COMMENT '登录方式：username_password/email_password/email_code',
                    success TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否登录成功：1-成功，0-失败',
                    client_ip VARCHAR(64) DEFAULT NULL COMMENT '客户端IP',
                    result_message VARCHAR(512) DEFAULT NULL COMMENT '结果说明（成功提示或失败原因）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    INDEX idx_ull_owner_id (owner_id),
                    INDEX idx_ull_login_method (login_method),
                    INDEX idx_ull_created_at (created_at),
                    INDEX idx_ull_owner_created (owner_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户登录日志表';
            """,
        'ytx_notification_channels': """
                CREATE TABLE IF NOT EXISTS ytx_notification_channels (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '渠道ID',
                    owner_id BIGINT NOT NULL COMMENT '所属用户ID',
                    name VARCHAR(120) NOT NULL COMMENT '渠道名称',
                    channel_type VARCHAR(32) NOT NULL COMMENT '渠道类型',
                    config JSON COMMENT '渠道配置',
                    enabled TINYINT(1) DEFAULT 1 COMMENT '是否启用',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_owner_id (owner_id),
                    INDEX idx_channel_type (channel_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='通知渠道表';
            """,
        'ytx_message_notifications': """
                CREATE TABLE IF NOT EXISTS ytx_message_notifications (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '通知ID',
                    owner_id BIGINT NOT NULL COMMENT '所属用户ID',
                    account_pk BIGINT NOT NULL COMMENT '关联账号ID',
                    account_identifier VARCHAR(80) NOT NULL COMMENT '账号标识',
                    channel_id BIGINT NOT NULL COMMENT '渠道ID',
                    enabled TINYINT(1) DEFAULT 1 COMMENT '是否启用',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_owner_id (owner_id),
                    INDEX idx_account_pk (account_pk),
                    INDEX idx_channel_id (channel_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='消息通知表';
            """,
        'ytx_message_filters': """
                CREATE TABLE IF NOT EXISTS ytx_message_filters (
                    id INT PRIMARY KEY AUTO_INCREMENT COMMENT '规则ID',
                    account_id VARCHAR(80) NOT NULL COMMENT '账号标识',
                    keyword VARCHAR(255) NOT NULL COMMENT '过滤关键词',
                    filter_type VARCHAR(20) NOT NULL COMMENT '过滤类型',
                    enabled TINYINT(1) DEFAULT 1 COMMENT '是否启用',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_account_id (account_id),
                    INDEX idx_keyword (keyword),
                    UNIQUE KEY uk_account_keyword_type (account_id, keyword, filter_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='消息过滤规则表';
            """,
        'ytx_goofish_crawl_jobs': """
                CREATE TABLE IF NOT EXISTS ytx_goofish_crawl_jobs (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT NOT NULL COMMENT '归属用户ID',
                    cookie_id VARCHAR(80) NOT NULL COMMENT '账号标识',
                    keyword VARCHAR(80) NOT NULL COMMENT '抓取关键词',
                    interval_seconds INT NOT NULL DEFAULT 900 COMMENT '执行间隔(秒)',
                    start_page INT NOT NULL DEFAULT 1 COMMENT '起始页码',
                    pages INT NOT NULL DEFAULT 1 COMMENT '抓取页数',
                    page_size INT NOT NULL DEFAULT 20 COMMENT '每页数量',
                    fetch_detail TINYINT(1) DEFAULT 1 COMMENT '是否抓取详情',
                    detail_limit INT NOT NULL DEFAULT 20 COMMENT '抓取详情数量上限',
                    enabled TINYINT(1) DEFAULT 1 COMMENT '是否启用',
                    last_run_at DATETIME COMMENT '最近一次执行时间',
                    last_error TEXT COMMENT '最近一次错误信息',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_owner_id (owner_id),
                    INDEX idx_cookie_id (cookie_id),
                    INDEX idx_enabled (enabled)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """,
        'ytx_goofish_crawl_items': """
                CREATE TABLE IF NOT EXISTS ytx_goofish_crawl_items (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    job_id BIGINT NOT NULL COMMENT '关联的抓取任务ID',
                    item_id VARCHAR(64) NOT NULL COMMENT '闲鱼商品ID',
                    title TEXT COMMENT '商品标题',
                    price VARCHAR(64) COMMENT '商品价格',
                    area VARCHAR(120) COMMENT '所在地区',
                    seller_name VARCHAR(120) COMMENT '卖家昵称',
                    item_url TEXT COMMENT '商品链接',
                    main_image VARCHAR(512) COMMENT '主图URL',
                    publish_time VARCHAR(64) COMMENT '发布时间',
                    want_count INT COMMENT '想要人数',
                    view_count INT COMMENT '浏览次数',
                    description TEXT COMMENT '商品描述',
                    detail_error VARCHAR(255) COMMENT '详情抓取错误信息',
                    raw_json JSON COMMENT '原始数据JSON',
                    fetched_at DATETIME NOT NULL COMMENT '抓取时间',
                    UNIQUE KEY uk_job_item (job_id, item_id),
                    INDEX idx_job_id (job_id),
                    INDEX idx_fetched_at (fetched_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """,
        'ytx_scheduled_redelivery_log': """
                CREATE TABLE IF NOT EXISTS `ytx_scheduled_redelivery_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `batch_id` VARCHAR(36) NOT NULL COMMENT '批次ID',
                    `account_id` VARCHAR(80) NOT NULL COMMENT '账号ID',
                    `order_no` VARCHAR(64) NOT NULL COMMENT '订单号',
                    `status` VARCHAR(20) NOT NULL COMMENT '状态',
                    `error_message` VARCHAR(500) DEFAULT NULL COMMENT '错误信息',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    INDEX `idx_batch_id` (`batch_id`),
                    INDEX `idx_account_id` (`account_id`),
                    INDEX `idx_created_at` (`created_at`),
                    INDEX `idx_srl_created_batch` (`created_at`, `batch_id`),
                    INDEX `idx_srl_batch_created_status` (`batch_id`, `created_at`, `status`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='定时补发货执行日志表';
            """,
        'ytx_confirm_receipt_messages': """
                CREATE TABLE IF NOT EXISTS ytx_confirm_receipt_messages (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    account_id VARCHAR(80) NOT NULL COMMENT '账号ID',
                    enabled TINYINT(1) DEFAULT 0 COMMENT '是否启用',
                    message_content TEXT COMMENT '消息文本内容',
                    message_image VARCHAR(512) COMMENT '消息图片URL',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    UNIQUE KEY uk_account_id (account_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='确认收货消息配置表';
            """,
        'ytx_auto_rate_configs': """
                CREATE TABLE IF NOT EXISTS ytx_auto_rate_configs (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    account_id VARCHAR(80) NOT NULL COMMENT '账号ID',
                    enabled TINYINT(1) DEFAULT 0 COMMENT '是否启用自动评价',
                    rate_type VARCHAR(20) DEFAULT 'text' COMMENT '评价类型',
                    text_content TEXT COMMENT '固定评价文字内容',
                    api_url VARCHAR(512) COMMENT 'API地址',
                    thanks_enabled TINYINT(1) DEFAULT 0 COMMENT '好评后自动发送消息开关',
                    thanks_content TEXT COMMENT '好评后发送的消息内容',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    UNIQUE KEY uk_account_id (account_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='自动评价配置表';
            """,
        'ytx_scheduled_rate_log': """
                CREATE TABLE IF NOT EXISTS `ytx_scheduled_rate_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `batch_id` VARCHAR(36) NOT NULL COMMENT '批次ID',
                    `account_id` VARCHAR(80) NOT NULL COMMENT '账号ID',
                    `order_no` VARCHAR(64) NOT NULL COMMENT '订单号',
                    `status` VARCHAR(20) NOT NULL COMMENT '状态',
                    `error_message` VARCHAR(500) DEFAULT NULL COMMENT '错误信息',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    INDEX `idx_batch_id` (`batch_id`),
                    INDEX `idx_account_id` (`account_id`),
                    INDEX `idx_created_at` (`created_at`),
                    INDEX `idx_srate_created_batch` (`created_at`, `batch_id`),
                    INDEX `idx_srate_batch_created_status` (`batch_id`, `created_at`, `status`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='定时补评价执行日志表';
            """,
        'ytx_scheduled_polish_log': """
                CREATE TABLE IF NOT EXISTS `ytx_scheduled_polish_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `batch_id` VARCHAR(36) NOT NULL COMMENT '批次ID',
                    `account_id` VARCHAR(80) NOT NULL COMMENT '账号ID',
                    `item_id` VARCHAR(64) NOT NULL COMMENT '商品ID',
                    `status` VARCHAR(20) NOT NULL COMMENT '状态',
                    `error_message` VARCHAR(500) DEFAULT NULL COMMENT '错误信息',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    INDEX `idx_batch_id` (`batch_id`),
                    INDEX `idx_account_id` (`account_id`),
                    INDEX `idx_created_at` (`created_at`),
                    INDEX `idx_spol_created_batch` (`created_at`, `batch_id`),
                    INDEX `idx_spol_batch_created_status` (`batch_id`, `created_at`, `status`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='定时擦亮执行日志表';
            """,
        'ytx_scheduled_login_renew_log': """
                CREATE TABLE IF NOT EXISTS `ytx_scheduled_login_renew_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `batch_id` VARCHAR(36) NOT NULL COMMENT '批次ID',
                    `account_id` VARCHAR(80) NOT NULL COMMENT '账号ID',
                    `status` VARCHAR(20) NOT NULL COMMENT '状态：success/token_refreshed/session_expired/failed',
                    `error_message` VARCHAR(500) DEFAULT NULL COMMENT '错误信息或处理说明',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    INDEX `idx_batch_id` (`batch_id`),
                    INDEX `idx_account_id` (`account_id`),
                    INDEX `idx_created_at` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='登录续期执行日志表';
            """,
        'ytx_scheduled_token_renewal_log': """
                CREATE TABLE IF NOT EXISTS `ytx_scheduled_token_renewal_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `batch_id` VARCHAR(36) NOT NULL COMMENT '批次ID',
                    `account_id` VARCHAR(80) NOT NULL COMMENT '账号ID',
                    `token_user_id` VARCHAR(128) NOT NULL COMMENT 'Token缓存用户ID（myid）',
                    `status` VARCHAR(20) NOT NULL COMMENT '状态：success/failed',
                    `renew_expire_at` DATETIME DEFAULT NULL COMMENT '续期Token到期时间',
                    `error_message` VARCHAR(500) DEFAULT NULL COMMENT '执行结果说明或错误信息',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    INDEX `idx_batch_id` (`batch_id`),
                    INDEX `idx_account_id` (`account_id`),
                    INDEX `idx_created_at` (`created_at`),
                    INDEX `idx_strl_created_batch` (`created_at`, `batch_id`),
                    INDEX `idx_strl_batch_created_status` (`batch_id`, `created_at`, `status`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Token续期执行日志表';
            """,
        'ytx_cookie_refresh_schedules': """
                CREATE TABLE IF NOT EXISTS `ytx_cookie_refresh_schedules` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `account_id` VARCHAR(80) NOT NULL COMMENT '账号ID',
                    `expire_at` DATETIME NOT NULL COMMENT '当前Cookie续期到期时间',
                    `last_refresh_at` DATETIME DEFAULT NULL COMMENT '最近一次续期成功时间',
                    `last_status` VARCHAR(20) DEFAULT NULL COMMENT '最近一次状态：initialized/success/failed',
                    `last_error_message` VARCHAR(500) DEFAULT NULL COMMENT '最近一次错误信息',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_account_id` (`account_id`),
                    INDEX `idx_expire_at` (`expire_at`),
                    INDEX `idx_created_at` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Cookie续期计划表';
            """,
        'ytx_scheduled_cookies_refresh_log': """
                CREATE TABLE IF NOT EXISTS `ytx_scheduled_cookies_refresh_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `batch_id` VARCHAR(36) NOT NULL COMMENT '批次ID',
                    `account_id` VARCHAR(80) NOT NULL COMMENT '账号ID',
                    `status` VARCHAR(20) NOT NULL COMMENT '状态：initialized/success/failed',
                    `updated_cookie_count` INT NOT NULL DEFAULT 0 COMMENT '本次增量更新的Cookie字段数量',
                    `next_expire_at` DATETIME DEFAULT NULL COMMENT '下次到期时间',
                    `error_message` VARCHAR(500) DEFAULT NULL COMMENT '错误信息或处理说明',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    INDEX `idx_batch_id` (`batch_id`),
                    INDEX `idx_account_id` (`account_id`),
                    INDEX `idx_status` (`status`),
                    INDEX `idx_created_at` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='COOKIES刷新日志表';
            """,
        'ytx_scheduled_api_cookie_renew_log': """
                CREATE TABLE IF NOT EXISTS `ytx_scheduled_api_cookie_renew_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `batch_id` VARCHAR(36) NOT NULL COMMENT '批次ID，标识一次定时任务执行',
                    `account_id` VARCHAR(80) NOT NULL COMMENT '账号ID',
                    `status` VARCHAR(30) NOT NULL COMMENT '状态：success/cookie_updated/browser_renewed/need_password_login/failed',
                    `updated_cookie_count` INT NOT NULL DEFAULT 0 COMMENT '本次更新的Cookie字段数量',
                    `updated_cookie_names` TEXT DEFAULT NULL COMMENT '本次更新的Cookie字段名列表（逗号分隔）',
                    `response_content` TEXT DEFAULT NULL COMMENT '接口返回内容（用于失败排查），最大裁剪到2000字符',
                    `error_message` VARCHAR(500) DEFAULT NULL COMMENT '错误信息或处理说明',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    INDEX `idx_batch_id` (`batch_id`),
                    INDEX `idx_account_id` (`account_id`),
                    INDEX `idx_status` (`status`),
                    INDEX `idx_created_at` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='接口续期Cookies执行日志表';
            """,
        'ytx_scheduled_tasks': """
                CREATE TABLE IF NOT EXISTS `ytx_scheduled_tasks` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `task_code` VARCHAR(50) NOT NULL COMMENT '任务代码',
                    `task_name` VARCHAR(100) NOT NULL COMMENT '任务名称',
                    `interval_seconds` INT NOT NULL DEFAULT 60 COMMENT '执行间隔(秒)',
                    `enabled` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否启用',
                    `description` TEXT COMMENT '任务描述',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uk_task_code` (`task_code`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='定时任务配置表';
            """,
        'ytx_card_item_relations': """
                CREATE TABLE IF NOT EXISTS ytx_card_item_relations (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    user_id BIGINT NOT NULL COMMENT '所属用户ID',
                    card_id BIGINT NOT NULL COMMENT '卡券ID',
                    item_id VARCHAR(64) NOT NULL COMMENT '商品ID',
                    source VARCHAR(20) DEFAULT 'own' COMMENT '卡券来源：own-自有',

                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_user_id (user_id),
                    INDEX idx_card_id (card_id),
                    INDEX idx_item_id (item_id),
                    INDEX idx_cir_user_item (user_id, item_id),
                    UNIQUE KEY uk_card_item (card_id, item_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='卡券商品关联表';
            """,
        'ytx_token_cache': """
                CREATE TABLE IF NOT EXISTS ytx_token_cache (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    user_id VARCHAR(128) NOT NULL COMMENT '用户ID（myid）',
                    token TEXT NOT NULL COMMENT 'IM Token',
                    device_id VARCHAR(128) NOT NULL COMMENT '设备ID',
                    expire_at DATETIME NOT NULL COMMENT '过期时间',
                    renew_expire_at DATETIME DEFAULT NULL COMMENT '续期Token过期时间',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    UNIQUE KEY uk_user_id (user_id),
                    INDEX idx_token_cache_expiries (expire_at, renew_expire_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Token缓存表';
            """,
        'ytx_product_materials': """
                CREATE TABLE IF NOT EXISTS ytx_product_materials (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    user_id BIGINT NOT NULL COMMENT '所属用户ID',
                    title VARCHAR(200) NOT NULL COMMENT '商品标题',
                    description TEXT NOT NULL COMMENT '商品描述',
                    price DECIMAL(12,2) NOT NULL COMMENT '价格',
                    original_price DECIMAL(12,2) DEFAULT NULL COMMENT '原价（划线价）',
                    category VARCHAR(100) DEFAULT NULL COMMENT '商品分类',
                    platform_category_id VARCHAR(64) DEFAULT NULL COMMENT '平台末级分类ID（catId）',
                    platform_category_name VARCHAR(100) DEFAULT NULL COMMENT '平台末级分类名称（catName）',
                    platform_channel_category_id VARCHAR(64) DEFAULT NULL COMMENT '平台频道分类ID（channelCatId）',
                    platform_channel_category_name VARCHAR(100) DEFAULT NULL COMMENT '平台频道分类名称（channelCatName）',
                    platform_leaf_id VARCHAR(64) DEFAULT NULL COMMENT '平台叶子分类ID（leafId）',
                    platform_tb_category_id VARCHAR(64) DEFAULT NULL COMMENT '淘宝分类ID（tbCatId）',
                    platform_category_path JSON DEFAULT NULL COMMENT '平台多级分类路径（各级ID和名称）',
                    platform_attributes JSON DEFAULT NULL COMMENT '平台属性标签列表（itemLabelExtList）',
                    category_source VARCHAR(20) NOT NULL DEFAULT 'manual' COMMENT '分类来源：manual-手动，recommendation-推荐',
                    category_confidence DECIMAL(8,6) DEFAULT NULL COMMENT '分类推荐置信度',
                    images JSON DEFAULT NULL COMMENT '图片URL列表（最多9张）',
                    videos JSON DEFAULT NULL COMMENT '视频素材列表（URL、文件ID、尺寸等）',
                    specifications JSON DEFAULT NULL COMMENT '商品规格列表',
                    sku_rows JSON DEFAULT NULL COMMENT '规格组合价格和库存列表',
                    quantity INT NOT NULL DEFAULT 1 COMMENT '发布数量',
                    delivery_method VARCHAR(20) DEFAULT 'express' COMMENT '发货方式：express-快递, pickup-自提',
                    shipping_method VARCHAR(20) DEFAULT 'free' COMMENT '运费方式：free/distance/fixed/template/none',
                    support_pickup TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否支持自提',
                    postage DECIMAL(8,2) DEFAULT 0 COMMENT '邮费，0表示包邮',
                    address VARCHAR(200) DEFAULT NULL COMMENT '宝贝所在地',
                    address_expected_text VARCHAR(200) DEFAULT NULL COMMENT '所在地选择时的期望文本',
                    brand VARCHAR(100) DEFAULT NULL COMMENT '品牌',
                    `condition` VARCHAR(20) DEFAULT '全新' COMMENT '成色',
                    remark VARCHAR(500) DEFAULT NULL COMMENT '备注（仅内部使用）',
                    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除（软删除）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_user_id (user_id),
                    INDEX idx_created_at (created_at),
                    INDEX idx_pm_user_created (user_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商品素材库表';
            """,
        'ytx_scheduled_red_flower_log': """
                CREATE TABLE IF NOT EXISTS `ytx_scheduled_red_flower_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `batch_id` VARCHAR(36) NOT NULL COMMENT '批次ID',
                    `account_id` VARCHAR(80) NOT NULL COMMENT '账号ID',
                    `order_no` VARCHAR(64) NOT NULL COMMENT '订单号',
                    `status` VARCHAR(20) NOT NULL COMMENT '状态：success/failed',
                    `error_message` VARCHAR(500) DEFAULT NULL COMMENT '错误信息',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    INDEX `idx_batch_id` (`batch_id`),
                    INDEX `idx_account_id` (`account_id`),
                    INDEX `idx_created_at` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='定时求小红花执行日志表';
            """,
        'ytx_scheduled_close_notice_log': """
                CREATE TABLE IF NOT EXISTS `ytx_scheduled_close_notice_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `batch_id` VARCHAR(36) NOT NULL COMMENT '批次ID',
                    `account_id` VARCHAR(80) NOT NULL COMMENT '账号ID',
                    `status` VARCHAR(20) NOT NULL COMMENT '状态：success/failed',
                    `error_message` VARCHAR(500) DEFAULT NULL COMMENT '错误信息',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    INDEX `idx_batch_id` (`batch_id`),
                    INDEX `idx_account_id` (`account_id`),
                    INDEX `idx_created_at` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='账号消息通知关闭执行日志表';
            """,
        'ytx_db_backup_log': """
                CREATE TABLE IF NOT EXISTS `ytx_db_backup_log` (
                    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键ID',
                    `status` VARCHAR(20) NOT NULL COMMENT '状态：success/failed',
                    `file_name` VARCHAR(255) DEFAULT NULL COMMENT '备份文件名',
                    `file_path` VARCHAR(500) DEFAULT NULL COMMENT '备份文件绝对路径',
                    `file_size` BIGINT DEFAULT NULL COMMENT '备份文件大小(字节)',
                    `table_count` INT DEFAULT NULL COMMENT '备份的数据表数量',
                    `total_rows` BIGINT DEFAULT NULL COMMENT '备份的数据总行数',
                    `duration_ms` INT DEFAULT NULL COMMENT '备份耗时(毫秒)',
                    `error_message` VARCHAR(1000) DEFAULT NULL COMMENT '错误信息',
                    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    PRIMARY KEY (`id`),
                    INDEX `idx_status` (`status`),
                    INDEX `idx_created_at` (`created_at`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='数据库备份日志表';
            """,
        'ytx_auto_reply_message_logs': """
                CREATE TABLE IF NOT EXISTS ytx_auto_reply_message_logs (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT DEFAULT NULL COMMENT '所属系统用户ID',
                    owner_username VARCHAR(120) DEFAULT NULL COMMENT '所属系统用户名',
                    account_pk BIGINT DEFAULT NULL COMMENT '账号主键ID',
                    account_id VARCHAR(80) NOT NULL COMMENT '闲鱼账号ID',
                    account_name VARCHAR(120) DEFAULT NULL COMMENT '闲鱼账号显示名称',
                    chat_id VARCHAR(128) NOT NULL COMMENT '聊天会话ID',
                    item_id VARCHAR(64) DEFAULT NULL COMMENT '商品ID',
                    item_title VARCHAR(255) DEFAULT NULL COMMENT '商品标题',
                    order_no VARCHAR(64) DEFAULT NULL COMMENT '订单号（自动发货等场景关联订单）',
                    source_message_id VARCHAR(128) DEFAULT NULL COMMENT '源消息ID',
                    sender_user_id VARCHAR(64) NOT NULL COMMENT '发送方闲鱼用户ID',
                    sender_user_name VARCHAR(120) DEFAULT NULL COMMENT '发送方昵称',
                    source_message TEXT COMMENT '收到的消息内容',
                    source_message_time DATETIME DEFAULT NULL COMMENT '收到消息时间',
                    process_status VARCHAR(20) NOT NULL DEFAULT 'processing' COMMENT '处理状态：processing/success/skipped/failed',
                    decision_reason VARCHAR(64) NOT NULL DEFAULT 'processing' COMMENT '决策原因',
                    reply_strategy VARCHAR(20) NOT NULL DEFAULT 'none' COMMENT '回复策略：keyword/ai/default/none',
                    reply_mode VARCHAR(20) NOT NULL DEFAULT 'none' COMMENT '回复模式：text/image/text_image/none',
                    matched_keyword VARCHAR(255) DEFAULT NULL COMMENT '命中的关键词',
                    matched_rule_type VARCHAR(32) DEFAULT NULL COMMENT '命中的规则类型',
                    default_reply_scope VARCHAR(20) DEFAULT NULL COMMENT '默认回复作用域：item/account',
                    default_reply_once TINYINT(1) NOT NULL DEFAULT 0 COMMENT '默认回复是否仅回复一次',
                    ai_model_name VARCHAR(120) DEFAULT NULL COMMENT 'AI模型名称',
                    ai_provider_name VARCHAR(80) DEFAULT NULL COMMENT 'AI服务商名称',
                    reply_text TEXT COMMENT '回复文本内容',
                    reply_image_url VARCHAR(1000) DEFAULT NULL COMMENT '回复图片URL',
                    reply_segments JSON DEFAULT NULL COMMENT '拆分后的回复分段',
                    error_message TEXT COMMENT '错误信息',
                    send_status VARCHAR(20) NOT NULL DEFAULT 'unknown' COMMENT '发送状态：success-发送成功/failed-发送失败/unknown-未知(无响应)/timeout-超时(无响应超过阈值)',
                    send_fail_reason TEXT COMMENT '发送失败原因（如被安全拦截的明文文案）',
                    raw_message_json JSON DEFAULT NULL COMMENT '原始消息JSON',
                    context_snapshot JSON DEFAULT NULL COMMENT '上下文快照',
                    send_result_json JSON DEFAULT NULL COMMENT '发送结果快照',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_owner_id (owner_id),
                    INDEX idx_account_pk (account_pk),
                    INDEX idx_account_id (account_id),
                    INDEX idx_chat_id (chat_id),
                    INDEX idx_item_id (item_id),
                    INDEX idx_order_no (order_no),
                    INDEX idx_source_message_id (source_message_id),
                    INDEX idx_sender_user_id (sender_user_id),
                    INDEX idx_process_status (process_status),
                    INDEX idx_decision_reason (decision_reason),
                    INDEX idx_created_at (created_at),
                    INDEX idx_arml_account_created (account_id, created_at),
                    INDEX idx_arml_account_status_created (account_id, process_status, created_at),
                    INDEX idx_arml_owner_created (owner_id, created_at),
                    INDEX idx_arml_owner_status_created (owner_id, process_status, created_at),
                    INDEX idx_arml_status_created (process_status, created_at),
                    INDEX idx_arml_status_strategy_created (process_status, reply_strategy, created_at),
                    INDEX idx_arml_strategy_created (reply_strategy, created_at),
                    INDEX idx_arml_order_strategy_id (order_no, reply_strategy, id),
                    INDEX idx_arml_strategy_order_id (reply_strategy, order_no, id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='自动回复消息日志表';
            """,
        'ytx_publish_addresses': """
                CREATE TABLE IF NOT EXISTS ytx_publish_addresses (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    name VARCHAR(120) NOT NULL COMMENT '地址名称',
                    search_keyword VARCHAR(200) NOT NULL COMMENT '地址搜索关键词',
                    expected_text VARCHAR(200) DEFAULT NULL COMMENT '期望命中的候选文本',
                    account_id VARCHAR(80) DEFAULT NULL COMMENT '限定使用的闲鱼账号ID，空表示全局通用',
                    weight INT NOT NULL DEFAULT 1 COMMENT '随机权重',
                    sort_order INT NOT NULL DEFAULT 100 COMMENT '排序值',
                    is_enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否启用',
                    use_count INT NOT NULL DEFAULT 0 COMMENT '使用次数',
                    last_used_at DATETIME DEFAULT NULL COMMENT '最后使用时间',
                    created_by BIGINT DEFAULT NULL COMMENT '创建人用户ID',
                    remark VARCHAR(500) DEFAULT NULL COMMENT '备注',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_account_id (account_id),
                    INDEX idx_pa_enabled_account (is_enabled, account_id),
                    INDEX idx_pa_sort_created (sort_order, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商品发布随机地址池表';
            """,
        'ytx_user_publish_addresses': """
                CREATE TABLE IF NOT EXISTS ytx_user_publish_addresses (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT NOT NULL COMMENT '归属用户ID',
                    address VARCHAR(200) NOT NULL COMMENT '地址文本（去重键）',
                    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除（软删除）',
                    use_count INT NOT NULL DEFAULT 0 COMMENT '使用次数',
                    last_used_at DATETIME DEFAULT NULL COMMENT '最后使用时间',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_upa_owner_deleted (owner_id, is_deleted),
                    INDEX idx_upa_owner_addr (owner_id, address)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='个人发布地址库表';
            """,
        'ytx_publish_logs': """
                CREATE TABLE IF NOT EXISTS ytx_publish_logs (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    user_id BIGINT NOT NULL COMMENT '操作用户ID',
                    account_id VARCHAR(80) NOT NULL COMMENT '闲鱼账号ID（cookie_id）',
                    title VARCHAR(200) NOT NULL COMMENT '商品标题',
                    description TEXT DEFAULT NULL COMMENT '商品描述',
                    price VARCHAR(20) DEFAULT NULL COMMENT '发布价格',
                    material_id BIGINT DEFAULT NULL COMMENT '关联的素材ID（批量发布时使用）',
                    batch_id VARCHAR(36) DEFAULT NULL COMMENT '批次ID（批量发布任务标识）',
                    status VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT '状态：pending/publishing/success/failed',
                    item_url VARCHAR(500) DEFAULT NULL COMMENT '发布成功后的商品链接',
                    item_id VARCHAR(64) DEFAULT NULL COMMENT '发布成功后的商品ID',
                    error_message VARCHAR(1000) DEFAULT NULL COMMENT '失败原因',
                    resolved_address_id BIGINT DEFAULT NULL COMMENT '本次发布命中的地址池ID',
                    resolved_address_text VARCHAR(200) DEFAULT NULL COMMENT '本次发布实际使用的地址搜索词',
                    address_source VARCHAR(20) DEFAULT NULL COMMENT '地址来源：material/account_pool/global_pool',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_user_id (user_id),
                    INDEX idx_account_id (account_id),
                    INDEX idx_batch_id (batch_id),
                    INDEX idx_status (status),
                    INDEX idx_created_at (created_at),
                    INDEX idx_publish_user_created (user_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商品发布日志表';
            """,
        'ytx_listing_monitor_categories': """
                CREATE TABLE IF NOT EXISTS ytx_listing_monitor_categories (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT NOT NULL COMMENT '归属用户ID',
                    name VARCHAR(100) NOT NULL COMMENT '分类名称',
                    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除（软删除）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_lmc_owner (owner_id),
                    INDEX idx_lmc_owner_deleted (owner_id, is_deleted)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商品监控分类表';
            """,
        'ytx_listing_monitor_tasks': """
                CREATE TABLE IF NOT EXISTS ytx_listing_monitor_tasks (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT DEFAULT NULL COMMENT '归属用户ID，用于多用户数据隔离',
                    category_id BIGINT DEFAULT NULL COMMENT '所属分类ID（NULL=未分类）',
                    monitor_type VARCHAR(20) NOT NULL DEFAULT 'listing' COMMENT '监控类型：listing-上新监控，price_drop-降价监控',
                    keyword VARCHAR(200) NOT NULL COMMENT '商品监控关键字',
                    price_min DECIMAL(12,2) DEFAULT NULL COMMENT '商品价格区间最低值',
                    price_max DECIMAL(12,2) DEFAULT NULL COMMENT '商品价格区间最高值',
                    publish_days INT DEFAULT NULL COMMENT '上新天数筛选（searchFilter 的 publishDays，单位天，NULL/0=不限）',
                    interval_minutes INT NOT NULL DEFAULT 5 COMMENT '任务执行间隔（分钟）',
                    collect_pages INT NOT NULL DEFAULT 1 COMMENT '每次采集页数',
                    proxy_url VARCHAR(255) DEFAULT NULL COMMENT '代理API地址（GET返回IP:PORT列表，取一个作HTTP代理；空=不使用代理）',
                    account_ids JSON DEFAULT NULL COMMENT '关联的闲鱼账号ID列表（JSON数组）',
                    order_account_ids JSON DEFAULT NULL COMMENT '下单账号ID列表（多选，私信与下单共用）',
                    dm_content VARCHAR(1000) DEFAULT NULL COMMENT '私信内容（配置下单账号后必填）',
                    dm_batch_size INT NOT NULL DEFAULT 5 COMMENT '每次定时私信任务最多处理条数',
                    order_batch_size INT NOT NULL DEFAULT 5 COMMENT '每次定时下单任务最多处理条数',
                    direct_order TINYINT(1) NOT NULL DEFAULT 0 COMMENT '采集后是否直接下单（开启则新采集商品立即用下单账号下单后再入库）',
                    is_enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否启用监控任务',
                    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除（软删除）',
                    last_run_at DATETIME DEFAULT NULL COMMENT '最近一次执行时间',
                    created_by BIGINT DEFAULT NULL COMMENT '创建人用户ID',
                    remark VARCHAR(500) DEFAULT NULL COMMENT '备注',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_lmt_owner_enabled (owner_id, is_enabled),
                    INDEX idx_lmt_owner_deleted (owner_id, is_deleted),
                    INDEX idx_lmt_created_at (created_at),
                    INDEX idx_lmt_category (category_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商品上新监控任务表';
            """,
        'ytx_listing_monitor_items': """
                CREATE TABLE IF NOT EXISTS ytx_listing_monitor_items (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    monitor_task_id BIGINT NOT NULL COMMENT '关联的商品监控任务ID',
                    owner_id BIGINT DEFAULT NULL COMMENT '归属用户ID',
                    item_id VARCHAR(64) NOT NULL COMMENT '闲鱼商品ID',
                    title VARCHAR(500) DEFAULT NULL COMMENT '商品标题',
                    price VARCHAR(32) DEFAULT NULL COMMENT '商品价格（展示文本）',
                    area VARCHAR(120) DEFAULT NULL COMMENT '商品所在地区',
                    pic_url VARCHAR(1000) DEFAULT NULL COMMENT '商品主图URL',
                    seller_id VARCHAR(120) DEFAULT NULL COMMENT '卖家ID（搜索返回，可能为加密串）',
                    seller_user_id VARCHAR(64) DEFAULT NULL COMMENT '卖家真实用户ID（商品详情接口补全）',
                    seller_nick VARCHAR(120) DEFAULT NULL COMMENT '卖家昵称',
                    seller_avatar VARCHAR(1000) DEFAULT NULL COMMENT '卖家头像URL',
                    want_count VARCHAR(32) DEFAULT NULL COMMENT '想要数（从营销标签解析的真实想要人数）',
                    tags VARCHAR(500) DEFAULT NULL COMMENT '商品营销标签（逗号分隔，如：4天内上新,235人想要）',
                    publish_time DATETIME DEFAULT NULL COMMENT '商品发布时间',
                    target_url VARCHAR(1000) DEFAULT NULL COMMENT '商品详情跳转URL',
                    raw_json TEXT DEFAULT NULL COMMENT '商品原始数据（搜索结果项JSON，兜底）',
                    detail_json MEDIUMTEXT DEFAULT NULL COMMENT '商品详情数据（详情接口返回JSON）',
                    seller_fill_status VARCHAR(20) DEFAULT NULL COMMENT '卖家ID补全结果：failed-明确失败不再补全（如跨境商品/已下架）',
                    seller_fill_fail_reason VARCHAR(500) DEFAULT NULL COMMENT '卖家ID补全失败原因（明确业务失败的原文）',
                    is_dm_sent TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已发起私信（已处理，避免重复发送）',
                    dm_account_id VARCHAR(80) DEFAULT NULL COMMENT '成功私信使用的账号ID（后续优先用该账号下单）',
                    dm_chat_id VARCHAR(80) DEFAULT NULL COMMENT '私信会话ID（create-chat 返回的 chat_id）',
                    dm_status VARCHAR(20) DEFAULT NULL COMMENT '私信发送结果：success/failed/unknown',
                    dm_fail_reason VARCHAR(500) DEFAULT NULL COMMENT '私信发送失败原因',
                    dm_attempts INT NOT NULL DEFAULT 0 COMMENT '私信发送尝试次数（失败重试用）',
                    is_ordered TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已下单成功',
                    order_id VARCHAR(64) DEFAULT NULL COMMENT '下单成功的订单ID（拍下）',
                    order_account_id VARCHAR(80) DEFAULT NULL COMMENT '下单成功使用的账号ID（发起私信时严格使用该账号）',
                    order_status VARCHAR(20) DEFAULT NULL COMMENT '下单结果：success/failed/duplicate',
                    order_fail_reason VARCHAR(500) DEFAULT NULL COMMENT '下单失败原因',
                    order_attempts INT NOT NULL DEFAULT 0 COMMENT '下单尝试次数（失败重试用）',
                    dm_sent_at DATETIME DEFAULT NULL COMMENT '实际私信成功/发起时间（用于按日统计私信数）',
                    ordered_at DATETIME DEFAULT NULL COMMENT '下单成功时间（用于按日统计下单数）',
                    last_seen_at DATETIME DEFAULT NULL COMMENT '最近一次采集到的时间',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    UNIQUE KEY uk_lmi_task_item (monitor_task_id, item_id),
                    INDEX idx_lmi_task (monitor_task_id),
                    INDEX idx_lmi_owner (owner_id),
                    INDEX idx_lmi_publish_time (publish_time),
                    INDEX idx_lmi_created (created_at),
                    INDEX idx_lmi_dm_send (order_status, is_dm_sent, ordered_at),
                    INDEX idx_lmi_order_pending (is_ordered, order_attempts),
                    INDEX idx_lmi_item_ordered (item_id, is_ordered),
                    INDEX idx_lmi_owner_publish (owner_id, publish_time),
                    INDEX idx_lmi_owner_order_pending (owner_id, is_ordered, created_at, order_attempts),
                    INDEX idx_lmi_owner_task_created (owner_id, monitor_task_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商品监控采集商品信息表';
            """,
        'ytx_listing_monitor_logs': """
                CREATE TABLE IF NOT EXISTS ytx_listing_monitor_logs (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    monitor_task_id BIGINT NOT NULL COMMENT '关联的商品监控任务ID',
                    owner_id BIGINT DEFAULT NULL COMMENT '归属用户ID',
                    monitor_type VARCHAR(20) DEFAULT NULL COMMENT '监控类型：listing-上新监控，price_drop-降价监控',
                    keyword VARCHAR(200) DEFAULT NULL COMMENT '监控关键字',
                    trigger_type VARCHAR(10) NOT NULL DEFAULT 'auto' COMMENT '触发方式：auto-定时自动，manual-手动',
                    account_id VARCHAR(80) DEFAULT NULL COMMENT '本次实际使用的主账号ID',
                    used_account_ids JSON DEFAULT NULL COMMENT '本次执行实际使用过的账号ID列表（可能多个）',
                    pages INT NOT NULL DEFAULT 0 COMMENT '本次采集页数',
                    fetched_count INT NOT NULL DEFAULT 0 COMMENT '本次获取的商品数',
                    inserted_count INT NOT NULL DEFAULT 0 COMMENT '本次新增的商品数',
                    updated_count INT NOT NULL DEFAULT 0 COMMENT '本次更新的商品数',
                    status VARCHAR(20) NOT NULL DEFAULT 'success' COMMENT '执行状态：success/failed/partial',
                    message VARCHAR(1000) DEFAULT NULL COMMENT '执行结果说明',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_lml_task (monitor_task_id),
                    INDEX idx_lml_owner (owner_id),
                    INDEX idx_lml_created_at (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商品监控执行日志表';
            """,
        'ytx_order_fallback_accounts': """
                CREATE TABLE IF NOT EXISTS ytx_order_fallback_accounts (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT NOT NULL COMMENT '归属用户ID',
                    category_id BIGINT DEFAULT NULL COMMENT '所属分类ID（NULL=未分类全局兜底）',
                    account_ids JSON DEFAULT NULL COMMENT '兜底下单账号ID列表（JSON数组，多选轮换使用）',
                    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除（软删除）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    UNIQUE KEY uk_ofa_owner_category (owner_id, category_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户级兜底下单账号配置表（按分类配置）';
            """,
        'ytx_collect_fallback_accounts': """
                CREATE TABLE IF NOT EXISTS ytx_collect_fallback_accounts (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT NOT NULL COMMENT '归属用户ID',
                    category_id BIGINT DEFAULT NULL COMMENT '所属分类ID（NULL=未分类全局兜底）',
                    account_ids JSON DEFAULT NULL COMMENT '兜底采集账号ID列表（JSON数组，多选轮换使用）',
                    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除（软删除）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    UNIQUE KEY uk_cfa_owner_category (owner_id, category_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户级兜底采集账号配置表（按分类配置）';
            """,
        'ytx_shared_scan_sessions': """
                CREATE TABLE IF NOT EXISTS ytx_shared_scan_sessions (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    session_id VARCHAR(36) NOT NULL UNIQUE COMMENT '会话唯一ID（UUID）',
                    owner_id BIGINT NOT NULL COMMENT '创建者用户ID',
                    owner_username VARCHAR(120) NOT NULL COMMENT '创建者用户名',
                    status VARCHAR(20) NOT NULL DEFAULT 'active' COMMENT '会话状态：active/closed',
                    expires_at DATETIME NOT NULL COMMENT '过期时间（默认72小时）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_session_id (session_id),
                    INDEX idx_owner_id (owner_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='共享扫码登录会话表';
            """,
        'ytx_delivery_block_rules': """
                CREATE TABLE IF NOT EXISTS ytx_delivery_block_rules (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    account_id VARCHAR(64) NOT NULL COMMENT '账号ID',
                    rule_code VARCHAR(50) NOT NULL COMMENT '规则编码',
                    enabled TINYINT(1) NOT NULL DEFAULT 0 COMMENT '规则开关',
                    priority INT NOT NULL DEFAULT 0 COMMENT '执行优先级（越小越先执行）',
                    block_reason VARCHAR(500) DEFAULT NULL COMMENT '禁止发货原因（发给买家的消息）',
                    auto_close_order TINYINT(1) NOT NULL DEFAULT 0 COMMENT '命中后主动关闭订单',
                    only_card_after_close TINYINT(1) NOT NULL DEFAULT 0 COMMENT '关闭订单后继续发货（只发卡券）',
                    excluded_item_ids JSON DEFAULT NULL COMMENT '该规则的排除商品列表（命中则跳过本规则）',
                    config JSON DEFAULT NULL COMMENT '规则专属参数',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_account_id (account_id),
                    UNIQUE KEY uk_account_rule (account_id, rule_code)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='禁止发货规则配置表';
            """,
        'ytx_shared_scan_workers': """
                CREATE TABLE IF NOT EXISTS ytx_shared_scan_workers (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    shared_session_id VARCHAR(36) NOT NULL COMMENT '关联的共享会话ID',
                    sub_session_id VARCHAR(36) NOT NULL UNIQUE COMMENT '兼职子会话唯一ID（UUID）',
                    xianyu_session_id VARCHAR(36) DEFAULT NULL COMMENT '关联的闲鱼QR登录会话ID',
                    status VARCHAR(20) NOT NULL DEFAULT 'qrcode_ready' COMMENT '状态：qrcode_ready/scanning/success/failed',
                    qr_code_url LONGTEXT DEFAULT NULL COMMENT '二维码图片base64 data URL',
                    account_id VARCHAR(80) DEFAULT NULL COMMENT '扫码成功后的闲鱼账号ID（unb）',
                    cookie_saved TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'Cookie是否已保存到账号表',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_shared_session_id (shared_session_id),
                    INDEX idx_sub_session_id (sub_session_id),
                    INDEX idx_account_id (account_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='共享扫码登录兼职工作者表';
            """,
        'ytx_personal_blacklist': """
                CREATE TABLE IF NOT EXISTS ytx_personal_blacklist (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT NOT NULL COMMENT '用户ID',
                    account_id VARCHAR(64) DEFAULT NULL COMMENT '账号ID',
                    buyer_id VARCHAR(64) NOT NULL COMMENT '买家ID',
                    buyer_nick VARCHAR(120) DEFAULT NULL COMMENT '买家昵称',
                    item_id VARCHAR(64) DEFAULT NULL COMMENT '商品ID',
                    reason VARCHAR(500) DEFAULT NULL COMMENT '拉黑原因',
                    is_enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否启用',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_pb_owner_id (owner_id),
                    INDEX idx_pb_owner_buyer (owner_id, buyer_id),
                    INDEX idx_pb_owner_account (owner_id, account_id),
                    INDEX idx_pb_owner_created (owner_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='个人黑名单表';
            """,
        'ytx_platform_blacklist': """
                CREATE TABLE IF NOT EXISTS ytx_platform_blacklist (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT NOT NULL COMMENT '拉黑用户（本系统用户ID）',
                    buyer_id VARCHAR(64) NOT NULL COMMENT '买家ID',
                    buyer_nick VARCHAR(120) DEFAULT NULL COMMENT '买家昵称',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX idx_plb_owner_id (owner_id),
                    INDEX idx_plb_owner_buyer (owner_id, buyer_id),
                    INDEX idx_plb_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='闲鱼黑名单表';
            """,
        'ytx_chat_quick_phrases': """
                CREATE TABLE IF NOT EXISTS ytx_chat_quick_phrases (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT NOT NULL COMMENT '归属用户（本系统用户ID）',
                    title VARCHAR(80) NOT NULL COMMENT '短语标题',
                    content TEXT NOT NULL COMMENT '短语内容（发送的文本）',
                    sort_order INT NOT NULL DEFAULT 0 COMMENT '排序值，越小越靠前',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX ix_ytx_chat_quick_phrases_owner_id (owner_id),
                    INDEX idx_chat_quick_phrase_owner_sort (owner_id, sort_order)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='在线聊天快捷短语';
            """,
        'ytx_ai_listing_configs': """
                CREATE TABLE IF NOT EXISTS ytx_ai_listing_configs (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT NOT NULL COMMENT '归属用户（本系统用户ID）',
                    name VARCHAR(80) NOT NULL COMMENT '配置名称',
                    provider_type VARCHAR(30) NOT NULL DEFAULT 'openai_compatible' COMMENT '服务商类型：openai_compatible 等',
                    text_base_url VARCHAR(300) NOT NULL COMMENT '文案接口地址',
                    text_api_key VARCHAR(500) NOT NULL COMMENT '文案接口密钥',
                    text_model VARCHAR(100) NOT NULL COMMENT '文案模型名称',
                    text_temperature DECIMAL(4,2) NOT NULL DEFAULT 0.70 COMMENT '文案生成温度',
                    text_max_tokens INT NOT NULL DEFAULT 2048 COMMENT '文案生成最大token数',
                    prompt_template TEXT DEFAULT NULL COMMENT '自定义提示词模板（为空则用内置模板）',
                    image_enabled TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否启用AI图片生成',
                    image_base_url VARCHAR(300) DEFAULT NULL COMMENT '图片接口地址',
                    image_api_key VARCHAR(500) DEFAULT NULL COMMENT '图片接口密钥',
                    image_model VARCHAR(100) DEFAULT NULL COMMENT '图片模型名称',
                    image_size VARCHAR(20) NOT NULL DEFAULT '1024x1024' COMMENT '图片尺寸，如 1024x1024',
                    image_count INT NOT NULL DEFAULT 1 COMMENT '每条素材生成图片数量（1~9）',
                    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除（软删除）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX ix_ytx_ai_listing_configs_owner_id (owner_id),
                    INDEX idx_alc_owner_deleted (owner_id, is_deleted)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI铺货配置表';
            """,
        'ytx_ai_listing_tasks': """
                CREATE TABLE IF NOT EXISTS ytx_ai_listing_tasks (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    owner_id BIGINT NOT NULL COMMENT '归属用户（本系统用户ID）',
                    task_id VARCHAR(36) NOT NULL COMMENT '任务ID（UUID）',
                    config_id BIGINT NOT NULL COMMENT '使用的AI铺货配置ID',
                    config_name VARCHAR(80) DEFAULT NULL COMMENT '配置名称快照',
                    keyword VARCHAR(200) NOT NULL COMMENT '生成主题/关键词',
                    total_count INT NOT NULL DEFAULT 0 COMMENT '计划生成条数',
                    success_count INT NOT NULL DEFAULT 0 COMMENT '已成功条数',
                    failed_count INT NOT NULL DEFAULT 0 COMMENT '已失败条数',
                    status VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT '状态：pending/running/success/partial/failed/canceled',
                    error_message VARCHAR(1000) DEFAULT NULL COMMENT '整体失败原因',
                    params JSON DEFAULT NULL COMMENT '提交参数快照（价格模式、素材默认值等）',
                    started_at DATETIME DEFAULT NULL COMMENT '开始执行时间',
                    finished_at DATETIME DEFAULT NULL COMMENT '执行结束时间',
                    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除（软删除）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    UNIQUE KEY uq_ytx_ai_listing_tasks_task_id (task_id),
                    INDEX ix_ytx_ai_listing_tasks_owner_id (owner_id),
                    INDEX idx_alt_owner_created (owner_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI铺货任务表';
            """,
        'ytx_ai_listing_task_items': """
                CREATE TABLE IF NOT EXISTS ytx_ai_listing_task_items (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '主键ID',
                    task_id VARCHAR(36) NOT NULL COMMENT '所属任务ID',
                    owner_id BIGINT NOT NULL COMMENT '归属用户（本系统用户ID）',
                    seq INT NOT NULL DEFAULT 0 COMMENT '序号（从1开始）',
                    status VARCHAR(20) NOT NULL DEFAULT 'pending' COMMENT '状态：pending/running/success/failed',
                    title VARCHAR(200) DEFAULT NULL COMMENT '生成的商品标题',
                    material_id BIGINT DEFAULT NULL COMMENT '入库后的素材ID',
                    image_count INT NOT NULL DEFAULT 0 COMMENT '本条素材图片数量',
                    error_message VARCHAR(1000) DEFAULT NULL COMMENT '失败原因',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                    INDEX ix_ytx_ai_listing_task_items_task_id (task_id),
                    INDEX idx_alti_task_status (task_id, status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI铺货任务明细表';
            """,
    }
    COLUMN_MIGRATIONS = {'ytx_keyword_rules': [('location_name', "VARCHAR(255) DEFAULT NULL COMMENT '站外联系方式定位名称'", 'image_url'),
                           ('location_longitude', "VARCHAR(32) DEFAULT NULL COMMENT '站外联系方式经度'", 'location_name'),
                           ('location_latitude',
                            "VARCHAR(32) DEFAULT NULL COMMENT '站外联系方式纬度'",
                            'location_longitude'),
                           ('location_title',
                            "VARCHAR(128) DEFAULT NULL COMMENT '站外联系方式位置标题'",
                            'location_latitude'),
                           ('location_subtitle',
                            "VARCHAR(255) DEFAULT NULL COMMENT '站外联系方式位置副标题'",
                            'location_title')],
     'ytx_token_cache': [('renew_expire_at', "DATETIME DEFAULT NULL COMMENT '续期Token过期时间'", 'expire_at')],
     'ytx_product_materials': [('platform_category_id',
                                "VARCHAR(64) DEFAULT NULL COMMENT '平台末级分类ID（catId）'",
                                'category'),
                               ('platform_category_name',
                                "VARCHAR(100) DEFAULT NULL COMMENT '平台末级分类名称（catName）'",
                                'platform_category_id'),
                               ('platform_channel_category_id',
                                "VARCHAR(64) DEFAULT NULL COMMENT '平台频道分类ID（channelCatId）'",
                                'platform_category_name'),
                               ('platform_channel_category_name',
                                "VARCHAR(100) DEFAULT NULL COMMENT '平台频道分类名称（channelCatName）'",
                                'platform_channel_category_id'),
                               ('platform_leaf_id',
                                "VARCHAR(64) DEFAULT NULL COMMENT '平台叶子分类ID（leafId）'",
                                'platform_channel_category_name'),
                               ('platform_tb_category_id',
                                "VARCHAR(64) DEFAULT NULL COMMENT '淘宝分类ID（tbCatId）'",
                                'platform_leaf_id'),
                               ('platform_category_path',
                                "JSON DEFAULT NULL COMMENT '平台多级分类路径（各级ID和名称）'",
                                'platform_tb_category_id'),
                               ('platform_attributes',
                                "JSON DEFAULT NULL COMMENT '平台属性标签列表（itemLabelExtList）'",
                                'platform_category_path'),
                               ('category_source',
                                "VARCHAR(20) NOT NULL DEFAULT 'manual' COMMENT "
                                "'分类来源：manual-手动，recommendation-推荐'",
                                'platform_attributes'),
                               ('category_confidence',
                                "DECIMAL(8,6) DEFAULT NULL COMMENT '分类推荐置信度'",
                                'category_source'),
                               ('videos', "JSON DEFAULT NULL COMMENT '视频素材列表（URL、文件ID、尺寸等）'", 'images'),
                               ('specifications', "JSON DEFAULT NULL COMMENT '商品规格列表'", 'videos'),
                               ('sku_rows', "JSON DEFAULT NULL COMMENT '规格组合价格和库存列表'", 'specifications'),
                               ('quantity', "INT NOT NULL DEFAULT 1 COMMENT '发布数量'", 'sku_rows'),
                               ('delivery_method',
                                "VARCHAR(20) DEFAULT 'express' COMMENT '发货方式：express-快递, pickup-自提'",
                                'quantity'),
                               ('shipping_method',
                                "VARCHAR(20) DEFAULT 'free' COMMENT '运费方式：free/distance/fixed/template/none'",
                                'delivery_method'),
                               ('support_pickup',
                                "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否支持自提'",
                                'shipping_method'),
                               ('postage', "DECIMAL(8,2) DEFAULT 0 COMMENT '邮费，0表示包邮'", 'support_pickup'),
                               ('address_expected_text',
                                "VARCHAR(200) DEFAULT NULL COMMENT '所在地选择时的期望文本'",
                                'address'),
                               ('is_deleted', "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除（软删除）'", 'remark')],
     'ytx_listing_monitor_tasks': [('monitor_type',
                                    "VARCHAR(20) NOT NULL DEFAULT 'listing' COMMENT "
                                    "'监控类型：listing-上新监控，price_drop-降价监控'",
                                    'owner_id'),
                                   ('category_id', "BIGINT DEFAULT NULL COMMENT '所属分类ID（NULL=未分类）'", 'owner_id'),
                                   ('collect_pages',
                                    "INT NOT NULL DEFAULT 1 COMMENT '每次采集页数'",
                                    'interval_minutes'),
                                   ('dm_content',
                                    "VARCHAR(1000) DEFAULT NULL COMMENT '私信内容（配置下单账号后必填）'",
                                    'account_ids'),
                                   ('order_account_ids',
                                    "JSON DEFAULT NULL COMMENT '下单账号ID列表（多选，私信与下单共用）'",
                                    'account_ids'),
                                   ('dm_batch_size',
                                    "INT NOT NULL DEFAULT 5 COMMENT '每次定时私信任务最多处理条数'",
                                    'dm_content'),
                                   ('order_batch_size',
                                    "INT NOT NULL DEFAULT 5 COMMENT '每次定时下单任务最多处理条数'",
                                    'dm_batch_size'),
                                   ('publish_days',
                                    "INT DEFAULT NULL COMMENT '上新天数筛选（searchFilter 的 publishDays，单位天，NULL/0=不限）'",
                                    'price_max'),
                                   ('proxy_url',
                                    'VARCHAR(255) DEFAULT NULL COMMENT '
                                    "'代理API地址（GET返回IP:PORT列表，取一个作HTTP代理；空=不使用代理）'",
                                    'collect_pages'),
                                   ('direct_order',
                                    "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '采集后是否直接下单（开启则新采集商品立即用下单账号下单后再入库）'",
                                    'order_batch_size')],
     'ytx_listing_monitor_logs': [('used_account_ids',
                                   "JSON DEFAULT NULL COMMENT '本次执行实际使用过的账号ID列表（可能多个）'",
                                   'account_id'),
                                  ('trigger_type',
                                   "VARCHAR(10) NOT NULL DEFAULT 'auto' COMMENT '触发方式：auto-定时自动，manual-手动'",
                                   'keyword')],
     'ytx_listing_monitor_items': [('is_dm_sent', "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已私信'", 'raw_json'),
                                   ('is_ordered', "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已下单'", 'is_dm_sent'),
                                   ('seller_user_id',
                                    "VARCHAR(64) DEFAULT NULL COMMENT '卖家真实用户ID（商品详情接口补全）'",
                                    'seller_id'),
                                   ('detail_json',
                                    "MEDIUMTEXT DEFAULT NULL COMMENT '商品详情数据（详情接口返回JSON）'",
                                    'raw_json'),
                                   ('order_id', "VARCHAR(64) DEFAULT NULL COMMENT '下单成功的订单ID（拍下）'", 'is_ordered'),
                                   ('order_account_id',
                                    "VARCHAR(80) DEFAULT NULL COMMENT '下单成功使用的账号ID（发起私信时严格使用该账号）'",
                                    'order_id'),
                                   ('dm_status',
                                    "VARCHAR(20) DEFAULT NULL COMMENT '私信发送结果：success/failed/unknown'",
                                    'is_dm_sent'),
                                   ('dm_fail_reason',
                                    "VARCHAR(500) DEFAULT NULL COMMENT '私信发送失败原因'",
                                    'dm_status'),
                                   ('dm_attempts',
                                    "INT NOT NULL DEFAULT 0 COMMENT '私信发送尝试次数（失败重试用）'",
                                    'dm_fail_reason'),
                                   ('order_status',
                                    "VARCHAR(20) DEFAULT NULL COMMENT '下单结果：success/failed/duplicate'",
                                    'order_id'),
                                   ('order_fail_reason',
                                    "VARCHAR(500) DEFAULT NULL COMMENT '下单失败原因'",
                                    'order_status'),
                                   ('order_attempts',
                                    "INT NOT NULL DEFAULT 0 COMMENT '下单尝试次数（失败重试用）'",
                                    'order_fail_reason'),
                                   ('dm_sent_at',
                                    "DATETIME DEFAULT NULL COMMENT '实际私信成功/发起时间（用于按日统计私信数）'",
                                    'dm_attempts'),
                                   ('ordered_at',
                                    "DATETIME DEFAULT NULL COMMENT '下单成功时间（用于按日统计下单数）'",
                                    'order_attempts'),
                                   ('dm_account_id',
                                    "VARCHAR(80) DEFAULT NULL COMMENT '成功私信使用的账号ID（后续优先用该账号下单）'",
                                    'is_dm_sent'),
                                   ('dm_chat_id',
                                    "VARCHAR(80) DEFAULT NULL COMMENT '私信会话ID（create-chat 返回的 chat_id）'",
                                    'dm_account_id'),
                                   ('seller_fill_status',
                                    "VARCHAR(20) DEFAULT NULL COMMENT '卖家ID补全结果：failed-明确失败不再补全（如跨境商品/已下架）'",
                                    'detail_json'),
                                   ('seller_fill_fail_reason',
                                    "VARCHAR(500) DEFAULT NULL COMMENT '卖家ID补全失败原因（明确业务失败的原文）'",
                                    'seller_fill_status'),
                                   ('seller_avatar',
                                    "VARCHAR(1000) DEFAULT NULL COMMENT '卖家头像URL'",
                                    'seller_nick'),
                                   ('tags',
                                    "VARCHAR(500) DEFAULT NULL COMMENT '商品营销标签（逗号分隔，如：4天内上新,235人想要）'",
                                    'want_count')],
     'ytx_order_fallback_accounts': [('category_id',
                                      "BIGINT DEFAULT NULL COMMENT '所属分类ID（NULL=未分类全局兜底）'",
                                      'owner_id'),
                                     ('is_deleted',
                                      "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除（软删除）'",
                                      'account_ids')],
     'ytx_collect_fallback_accounts': [('category_id',
                                        "BIGINT DEFAULT NULL COMMENT '所属分类ID（NULL=未分类全局兜底）'",
                                        'owner_id'),
                                       ('is_deleted',
                                        "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除（软删除）'",
                                        'account_ids')],
     'ytx_auto_reply_message_logs': [('send_status',
                                      "VARCHAR(20) NOT NULL DEFAULT 'unknown' COMMENT "
                                      "'发送状态：success-发送成功/failed-发送失败/unknown-未知(无响应)/timeout-超时(无响应超过阈值)'",
                                      'error_message'),
                                     ('send_fail_reason', "TEXT COMMENT '发送失败原因（如被安全拦截的明文文案）'", 'send_status'),
                                     ('order_no',
                                      "VARCHAR(64) DEFAULT NULL COMMENT '订单号（自动发货等场景关联订单）'",
                                      'item_title')],
     'ytx_risk_control_logs': [('captcha_engine',
                                'VARCHAR(32) DEFAULT NULL COMMENT '
                                "'验证通过引擎：playwright-主引擎/drissionpage-兜底引擎/real_mouse-真人鼠标引擎'",
                                'processing_status'),
                               ('call_type',
                                "VARCHAR(16) DEFAULT 'local' COMMENT '调用类型：local-本机/remote-远程(外部凭秘钥调用)'",
                                'captcha_engine'),
                               ('call_user',
                                "VARCHAR(128) DEFAULT NULL COMMENT '调用用户：仅远程调用记录(按秘钥查到的用户名)'",
                                'call_type')],
     'ytx_accounts': [('proxy_type', "VARCHAR(20) DEFAULT 'none' COMMENT '代理类型'", 'last_refresh_at'),
                      ('proxy_host', "VARCHAR(255) COMMENT '代理主机'", 'proxy_type'),
                      ('proxy_port', "INT COMMENT '代理端口'", 'proxy_host'),
                      ('proxy_user', "VARCHAR(120) COMMENT '代理用户名'", 'proxy_port'),
                      ('proxy_pass', "VARCHAR(255) COMMENT '代理密码'", 'proxy_user'),
                      ('message_expire_time', "INT DEFAULT 3600 COMMENT '相同消息等待时间(秒)'", 'proxy_pass'),
                      ('reply_delay_seconds',
                       "INT DEFAULT 0 COMMENT '自动回复延迟时间(秒)，0表示立即回复'",
                       'message_expire_time'),
                      ('disable_reason', "VARCHAR(255) COMMENT '禁用原因'", 'message_expire_time'),
                      ('scheduled_redelivery',
                       "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '定时补发货开关'",
                       'disable_reason'),
                      ('scheduled_rate',
                       "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '定时补评价开关'",
                       'scheduled_redelivery'),
                      ('auto_polish', "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '商品自动擦亮开关'", 'scheduled_rate'),
                      ('confirm_before_send',
                       "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '发货成功再发卡券开关'",
                       'auto_polish'),
                      ('send_before_confirm',
                       "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '卡券发送成功再确认发货开关'",
                       'confirm_before_send'),
                      ('only_send_card',
                       "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '只发卡券不确认发货开关'",
                       'send_before_confirm'),
                      ('auto_red_flower',
                       "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '自动求小红花开关'",
                       'send_before_confirm'),
                      ('delivery_disabled', "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '禁止发货开关'", 'auto_red_flower'),
                      ('delivery_disabled_reason',
                       "VARCHAR(500) DEFAULT NULL COMMENT '禁止发货原因'",
                       'delivery_disabled'),
                      ('auto_close_order',
                       "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '主动关闭订单开关'",
                       'delivery_disabled_reason'),
                      ('delivery_only_card_after_close',
                       "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '关闭订单后继续发货（只发卡券）'",
                       'auto_close_order'),
                      ('delivery_disabled_excluded_items',
                       "JSON DEFAULT NULL COMMENT '禁止发货排除商品列表（item_id 数组，命中后按正常流程发货）'",
                       'delivery_only_card_after_close'),
                      ('ai_reply_block_ordered_users',
                       "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '已下单用户禁止AI回复'",
                       'delivery_disabled_excluded_items'),
                      ('refund_cancel_enabled',
                       "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '退款订单注销开关'",
                       'ai_reply_block_ordered_users'),
                      ('refund_cancel_url',
                       "VARCHAR(255) DEFAULT NULL COMMENT '退款订单注销请求URL'",
                       'refund_cancel_enabled'),
                      ('refund_cancel_timeout', "INT DEFAULT 60 COMMENT '退款订单注销超时时间(秒)'", 'refund_cancel_url'),
                      ('agree_deliver_enabled',
                       "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '同意后发货开关'",
                       'refund_cancel_timeout'),
                      ('agree_deliver_notify_message',
                       "VARCHAR(2000) DEFAULT NULL COMMENT '同意后发货-通知用户信息'",
                       'agree_deliver_enabled'),
                      ('agree_deliver_pickup_url',
                       "VARCHAR(255) DEFAULT NULL COMMENT '同意后发货-提货URL'",
                       'agree_deliver_notify_message')],
     'ytx_orders': [('card_only_delivered',
                     "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '仅发卡券流程是否已处理'",
                     'delivery_fail_reason'),
                    ('is_bargain', "TINYINT(1) DEFAULT 0 COMMENT '是否小刀'", 'account_name'),
                    ('chat_id', "VARCHAR(64) COMMENT '聊天会话ID'", 'buyer_id'),
                    ('buyer_fish_nick', "VARCHAR(120) COMMENT '买家闲鱼昵称（明文）'", 'buyer_nick'),
                    ('receiver_name', "VARCHAR(120) COMMENT '收货人姓名'", 'is_bargain'),
                    ('receiver_phone', "VARCHAR(32) COMMENT '收货人手机号'", 'receiver_name'),
                    ('receiver_address', "VARCHAR(512) COMMENT '收货地址'", 'receiver_phone'),
                    ('is_rated', "TINYINT(1) DEFAULT 0 COMMENT '是否已评价'", 'receiver_address'),
                    ('delivery_method', "VARCHAR(32) COMMENT '发货方式'", 'is_rated'),
                    ('delivery_content', "VARCHAR(2000) COMMENT '发货内容'", 'delivery_method'),
                    ('delivery_fail_reason', "VARCHAR(2000) COMMENT '发货失败原因'", 'delivery_content'),
                    ('source', "VARCHAR(32) COMMENT '数据来源：fetch_xianyu-获取闲鱼订单按钮'", 'metadata'),
                    ('is_red_flower', "TINYINT(1) DEFAULT 0 COMMENT '是否已求小红花'", 'is_rated'),
                    ('is_thanks_sent', "TINYINT(1) DEFAULT 0 COMMENT '是否已发送好评后消息'", 'is_red_flower'),
                    ('is_unregistered', "TINYINT(1) DEFAULT 0 COMMENT '是否已请求注销接口'", 'is_red_flower'),
                    ('unregister_error_reason',
                     "VARCHAR(500) DEFAULT NULL COMMENT '注销接口错误原因'",
                     'is_unregistered'),
                    ('agree_deliver_agreed',
                     "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '同意后发货-买家是否已点击同意'",
                     'card_only_delivered'),
                    ('agree_deliver_agreed_at',
                     "DATETIME DEFAULT NULL COMMENT '同意后发货-买家点击同意时间'",
                     'agree_deliver_agreed')],
     'ytx_auto_rate_configs': [('thanks_enabled', "TINYINT(1) DEFAULT 0 COMMENT '好评后自动发送消息开关'", 'api_url'),
                               ('thanks_content', "TEXT COMMENT '好评后发送的消息内容'", 'thanks_enabled')],
     'ytx_cards': [('delivery_count', "INT DEFAULT 0 COMMENT '发货次数'", 'delay_seconds'),
                   ('use_no_logistics_form',
                    "TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否通过无需邮寄表单发货'",
                    'delay_seconds'),
                   ('image_urls', "TEXT COMMENT '多图片URL列表(JSON数组，最多3张)'", 'image_url')],
     'ytx_users': [('account_limit', "INT DEFAULT NULL COMMENT '可添加账号数量'", 'role'),
                   ('login_fail_count', "INT DEFAULT 0 COMMENT '登录失败次数'", 'last_login_at'),
                   ('login_locked_until', "DATETIME COMMENT '登录锁定截止时间'", 'login_fail_count'),
                   ('secret_key',
                    "VARCHAR(64) DEFAULT NULL UNIQUE COMMENT '业务接口密钥，32位随机字符，全局唯一'",
                    'login_locked_until'),
                   ('expire_at', "DATETIME DEFAULT NULL COMMENT '账号到期日（精确到秒，NULL=永不过期）'", 'secret_key')],
     'ytx_default_replies': [('item_id', "VARCHAR(64) DEFAULT NULL COMMENT '商品ID'", 'account_id'),
                             ('reply_image', "VARCHAR(512) COMMENT '回复图片URL'", 'reply_content'),
                             ('reply_type',
                              "VARCHAR(32) DEFAULT 'text' COMMENT '回复类型：text-文本，api-接口，external_contact-站外联系方式'",
                              'enabled'),
                             ('api_url',
                              "VARCHAR(1024) DEFAULT NULL COMMENT 'API地址(reply_type=api时POST此地址)'",
                              'reply_image'),
                             ('api_timeout', "INT DEFAULT 80 COMMENT 'API请求超时时间(秒)'", 'api_url'),
                             ('location_name', "VARCHAR(255) DEFAULT NULL COMMENT '站外联系方式定位名称'", 'api_timeout'),
                             ('location_longitude',
                              "VARCHAR(32) DEFAULT NULL COMMENT '站外联系方式经度'",
                              'location_name'),
                             ('location_latitude',
                              "VARCHAR(32) DEFAULT NULL COMMENT '站外联系方式纬度'",
                              'location_longitude'),
                             ('location_title',
                              "VARCHAR(128) DEFAULT NULL COMMENT '站外联系方式位置标题'",
                              'location_latitude'),
                             ('location_subtitle',
                              "VARCHAR(255) DEFAULT NULL COMMENT '站外联系方式位置副标题'",
                              'location_title')],
     'ytx_default_reply_records': [('item_id', "VARCHAR(64) DEFAULT NULL COMMENT '商品ID'", 'account_id')],
     'ytx_catalog_items': [('ai_prompt', "TEXT COMMENT '商品AI提示词'", 'price'),
                           ('is_polished', "TINYINT(1) DEFAULT 0 COMMENT '是否擦亮'", 'ai_prompt'),
                           ('updated_at', "DATETIME COMMENT '更新时间'", 'created_at')],
     'ytx_card_item_relations': [('source',
                                  "VARCHAR(20) DEFAULT 'own' COMMENT '卡券来源：own-自有'",
                                  'item_id'),
                                ],
     'ytx_publish_logs': [('resolved_address_id', "BIGINT DEFAULT NULL COMMENT '本次发布命中的地址池ID'", 'error_message'),
                          ('resolved_address_text',
                           "VARCHAR(200) DEFAULT NULL COMMENT '本次发布实际使用的地址搜索词'",
                           'resolved_address_id'),
                          ('address_source',
                           "VARCHAR(20) DEFAULT NULL COMMENT '地址来源：material/account_pool/global_pool'",
                           'resolved_address_text')],
     'ytx_account_login_logs': [('updated_cookie_names',
                                 "VARCHAR(500) DEFAULT NULL COMMENT '接口续期更新的Cookie字段名（逗号分隔）'",
                                 'error_message')]}

    async def init_all(self):
        """初始化所有数据库内容"""
        try:
            logger.info('=' * 50)
            logger.info('开始初始化数据库...')
            async with init_advisory_lock():
                with suppress_db_warnings():
                    await self.create_all_tables()
                    async with ddl_connection() as conn:
                        await ensure_auto_relist_schema(conn, get_beijing_now_naive())
                    await self.create_default_admin()
                    await self.init_system_settings()
                    await self.init_scheduled_tasks()
                    await self.init_publish_addresses()
                    await self.init_redis_platform_day()
                    await self.migrate_card_item_relations()
                    await self.migrate_delivery_block_rules()
            logger.info('数据库初始化完成')
            logger.info('=' * 50)
        except Exception as e:
            logger.error(f'数据库初始化失败: {e}')
            raise
    TABLES_TO_RENAME = {'scheduled_redelivery_log': 'ytx_scheduled_redelivery_log', 'scheduled_rate_log': 'ytx_scheduled_rate_log', 'scheduled_polish_log': 'ytx_scheduled_polish_log'}

    async def create_all_tables(self):
        """创建所有数据表"""
        logger.info('开始创建数据表...')
        async with ddl_connection() as conn:
            for table_name, ddl in self.TABLES_DDL.items():
                try:
                    await conn.execute(text(ddl))
                    logger.info(f'✓ 表 {table_name} 已就绪')
                except Exception as e:
                    logger.warning(f'✗ 表 {table_name} 创建失败: {describe_ddl_error(e)}')
        logger.info(f'数据表创建完成，共 {len(self.TABLES_DDL)} 张表')
        await self.migrate_columns()
        await self.migrate_indexes()

    async def migrate_columns(self):
        """检查并添加/修改字段"""
        logger.info('检查字段迁移...')
        async with ddl_connection() as conn:
            for table_name, columns in self.COLUMN_MIGRATIONS.items():
                for col_name, col_def, after_col in columns:
                    try:
                        check_sql = text(f"\n                            SELECT COUNT(*) FROM information_schema.COLUMNS \n                            WHERE TABLE_SCHEMA = DATABASE() \n                            AND TABLE_NAME = '{table_name}' \n                            AND COLUMN_NAME = '{col_name}'\n                        ")
                        result = await conn.execute(check_sql)
                        exists = result.scalar() > 0
                        if exists and table_name == 'ytx_default_replies' and (col_name == 'reply_type'):
                            length_result = await conn.execute(text("\n                                SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS\n                                WHERE TABLE_SCHEMA = DATABASE()\n                                AND TABLE_NAME = 'ytx_default_replies'\n                                AND COLUMN_NAME = 'reply_type'\n                            "))
                            current_length = length_result.scalar()
                            if current_length and current_length < 32:
                                await conn.execute(text("ALTER TABLE ytx_default_replies MODIFY COLUMN reply_type VARCHAR(32) DEFAULT 'text' COMMENT 'reply type'"))
                                logger.info('Expanded ytx_default_replies.reply_type to VARCHAR(32)')
                        if not exists:
                            try:
                                alter_sql = text(f'ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def} AFTER {after_col}')
                                await conn.execute(alter_sql)
                            except Exception:
                                alter_sql = text(f'ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}')
                                await conn.execute(alter_sql)
                            logger.info(f'✓ 表 {table_name} 添加字段 {col_name}')
                        else:
                            logger.debug(f'✓ 表 {table_name} 已有字段 {col_name}')
                    except Exception as e:
                        logger.warning(f'✗ 表 {table_name} 字段 {col_name} 迁移失败: {describe_ddl_error(e)}')
            try:
                check_account_limit = text("\n                    SELECT IS_NULLABLE, COLUMN_DEFAULT FROM information_schema.COLUMNS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_users'\n                    AND COLUMN_NAME = 'account_limit'\n                ")
                result = await conn.execute(check_account_limit)
                account_limit_row = result.fetchone()
                if account_limit_row and (account_limit_row.IS_NULLABLE != 'YES' or account_limit_row.COLUMN_DEFAULT is not None):
                    await conn.execute(text("ALTER TABLE ytx_users MODIFY COLUMN account_limit INT DEFAULT NULL COMMENT '可添加账号数量'"))
                    logger.info('✓ ytx_users: account_limit 字段已调整为允许为空且默认值为空')
            except Exception as e:
                logger.warning(f'✗ ytx_users account_limit 字段迁移失败: {e}')
            try:
                check_len = text("\n                    SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_token_cache'\n                    AND COLUMN_NAME = 'user_id'\n                ")
                result = await conn.execute(check_len)
                max_len = result.scalar()
                if max_len and max_len < 128:
                    await conn.execute(text("ALTER TABLE ytx_token_cache MODIFY COLUMN user_id VARCHAR(128) NOT NULL COMMENT '用户ID（myid）'"))
                    logger.info('✓ ytx_token_cache: user_id 字段长度已扩展为 VARCHAR(128)')
            except Exception as e:
                logger.warning(f'✗ ytx_token_cache user_id 字段迁移失败: {e}')
            try:
                check_status_len = text("\n                    SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_scheduled_api_cookie_renew_log'\n                    AND COLUMN_NAME = 'status'\n                ")
                result = await conn.execute(check_status_len)
                status_len = result.scalar()
                if status_len and status_len < 30:
                    await conn.execute(text("ALTER TABLE ytx_scheduled_api_cookie_renew_log MODIFY COLUMN `status` VARCHAR(30) NOT NULL COMMENT '状态：success/cookie_updated/browser_renewed/need_password_login/failed'"))
                    logger.info('✓ ytx_scheduled_api_cookie_renew_log: status 字段长度已扩展为 VARCHAR(30)')
            except Exception as e:
                logger.warning(f'✗ ytx_scheduled_api_cookie_renew_log status 字段迁移失败: {e}')
            for card_col in ('text_content', 'data_content'):
                try:
                    check_card_col = text("\n                        SELECT DATA_TYPE FROM information_schema.COLUMNS\n                        WHERE TABLE_SCHEMA = DATABASE()\n                        AND TABLE_NAME = 'ytx_cards'\n                        AND COLUMN_NAME = :col_name\n                    ")
                    result = await conn.execute(check_card_col, {'col_name': card_col})
                    data_type = result.scalar()
                    if data_type and data_type.lower() != 'longtext':
                        await conn.execute(text(f'ALTER TABLE ytx_cards MODIFY COLUMN {card_col} LONGTEXT NULL'))
                        logger.info(f'✓ ytx_cards: {card_col} 字段已升级为 LONGTEXT')
                except Exception as e:
                    logger.warning(f'✗ ytx_cards {card_col} 字段迁移失败: {e}')
            try:
                result = await conn.execute(text('\n                    UPDATE ytx_accounts\n                    SET auto_confirm = 0,\n                        confirm_before_send = 0,\n                        send_before_confirm = 0\n                    WHERE only_send_card = 1\n                      AND (\n                          auto_confirm <> 0\n                          OR confirm_before_send <> 0\n                          OR send_before_confirm <> 0\n                      )\n                '))
                if result.rowcount:
                    logger.info(f'✓ ytx_accounts: 已修复 {result.rowcount} 条只发卡券冲突配置')
            except Exception as e:
                logger.warning(f'✗ ytx_accounts 发货开关冲突配置修复失败: {e}')

    async def migrate_indexes(self):
        """检查并迁移索引（如更新 UNIQUE KEY 等）"""
        logger.info('检查索引迁移...')
        async with ddl_connection() as conn:
            try:
                result = await conn.execute(text("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ytx_card_item_relations' AND INDEX_NAME = 'uk_card_item'"))
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_card_item_relations ADD UNIQUE KEY uk_card_item (card_id, item_id)'))
            except Exception as e:
                logger.warning(f'✗ 卡券商品唯一索引检查失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_token_cache'\n                    AND INDEX_NAME = 'idx_token_cache_expiries'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_token_cache ADD INDEX idx_token_cache_expiries (expire_at, renew_expire_at)'))
                    logger.info('✓ ytx_token_cache: 创建 idx_token_cache_expiries 索引')
            except Exception as e:
                logger.warning(f'✗ ytx_token_cache idx_token_cache_expiries 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_users'\n                    AND INDEX_NAME = 'idx_user_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_users ADD INDEX idx_user_created (created_at)'))
                    logger.info('✓ ytx_users: 创建 idx_user_created 索引')
            except Exception as e:
                logger.warning(f'✗ ytx_users idx_user_created 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_accounts'\n                    AND INDEX_NAME = 'idx_account_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_accounts ADD INDEX idx_account_created (created_at)'))
                    logger.info('✓ ytx_accounts: 创建 idx_account_created 索引')
            except Exception as e:
                logger.warning(f'✗ ytx_accounts idx_account_created 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_keyword_rules'\n                    AND INDEX_NAME = 'idx_kw_account_item'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_keyword_rules ADD INDEX idx_kw_account_item (account_id, item_id)'))
                    logger.info('✓ ytx_keyword_rules: 创建 idx_kw_account_item 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_keyword_rules 复合索引创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_keyword_rules'\n                    AND INDEX_NAME = 'idx_kw_account_active'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_keyword_rules ADD INDEX idx_kw_account_active (account_id, is_active)'))
                    logger.info('✓ ytx_keyword_rules: 创建 idx_kw_account_active 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_keyword_rules idx_kw_account_active 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_risk_control_logs'\n                    AND INDEX_NAME = 'idx_rcl_status_event'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_risk_control_logs ADD INDEX idx_rcl_status_event (processing_status, event_type)'))
                    logger.info('✓ ytx_risk_control_logs: 创建 idx_rcl_status_event 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_risk_control_logs idx_rcl_status_event 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_catalog_items'\n                    AND INDEX_NAME = 'idx_cat_account_item'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_catalog_items ADD INDEX idx_cat_account_item (account_id, item_id)'))
                    logger.info('✓ ytx_catalog_items: 创建 idx_cat_account_item 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_catalog_items 复合索引创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_cards'\n                    AND INDEX_NAME = 'idx_card_user_item'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_cards ADD INDEX idx_card_user_item (user_id, item_id)'))
                    logger.info('✓ ytx_cards: 创建 idx_card_user_item 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_cards 复合索引创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_catalog_items'\n                    AND INDEX_NAME = 'idx_cat_owner_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_catalog_items ADD INDEX idx_cat_owner_created (owner_id, created_at)'))
                    logger.info('✓ ytx_catalog_items: 创建 idx_cat_owner_created 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_catalog_items idx_cat_owner_created 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_catalog_items'\n                    AND INDEX_NAME = 'uk_cat_account_item'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    dup_check = text('\n                        SELECT COUNT(*) FROM (\n                            SELECT account_id, item_id\n                            FROM ytx_catalog_items\n                            GROUP BY account_id, item_id\n                            HAVING COUNT(*) > 1\n                        ) AS dup\n                    ')
                    dup_result = await conn.execute(dup_check)
                    dup_groups = dup_result.scalar() or 0
                    if dup_groups > 0:
                        logger.warning(f'✗ ytx_catalog_items 存在 {dup_groups} 组 (account_id, item_id) 重复数据，为遵守禁止删除数据规范，暂不创建 uk_cat_account_item 唯一约束。请人工合并重复商品后，重启服务自动补建（当前由 Redis 账号锁兜底防并发）')
                    else:
                        await conn.execute(text('ALTER TABLE ytx_catalog_items ADD UNIQUE KEY uk_cat_account_item (account_id, item_id)'))
                        logger.info('✓ ytx_catalog_items: 创建 uk_cat_account_item 唯一约束')
            except Exception as e:
                logger.warning(f'✗ ytx_catalog_items uk_cat_account_item 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_card_item_relations'\n                    AND INDEX_NAME = 'idx_cir_user_item'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_card_item_relations ADD INDEX idx_cir_user_item (user_id, item_id)'))
                    logger.info('✓ ytx_card_item_relations: 创建 idx_cir_user_item 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_card_item_relations idx_cir_user_item 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_card_item_relations'\n                    AND INDEX_NAME = 'idx_cir_item_card'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_card_item_relations ADD INDEX idx_cir_item_card (item_id, card_id)'))
                    logger.info('✓ ytx_card_item_relations: 创建 idx_cir_item_card 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_card_item_relations idx_cir_item_card 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_cards'\n                    AND INDEX_NAME = 'idx_cards_user_id_desc'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_cards ADD INDEX idx_cards_user_id_desc (user_id, id)'))
                    logger.info('✓ ytx_cards: 创建 idx_cards_user_id_desc 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_cards idx_cards_user_id_desc 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_cards'\n                    AND INDEX_NAME = 'idx_cards_user_enabled'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_cards ADD INDEX idx_cards_user_enabled (user_id, enabled)'))
                    logger.info('✓ ytx_cards: 创建 idx_cards_user_enabled 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_cards idx_cards_user_enabled 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_risk_control_logs'\n                    AND INDEX_NAME = 'idx_rcl_account_status'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_risk_control_logs ADD INDEX idx_rcl_account_status (account_id, processing_status)'))
                    logger.info('✓ ytx_risk_control_logs: 创建 idx_rcl_account_status 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_risk_control_logs 复合索引创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_risk_control_logs'\n                    AND INDEX_NAME = 'idx_rcl_identifier_status_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_risk_control_logs ADD INDEX idx_rcl_identifier_status_created (account_identifier, processing_status, created_at)'))
                    logger.info('✓ ytx_risk_control_logs: 创建 idx_rcl_identifier_status_created 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_risk_control_logs idx_rcl_identifier_status_created 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_scheduled_redelivery_log'\n                    AND INDEX_NAME = 'idx_srl_created_batch'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_scheduled_redelivery_log ADD INDEX idx_srl_created_batch (created_at, batch_id)'))
                    logger.info('✓ ytx_scheduled_redelivery_log: 创建 idx_srl_created_batch 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_scheduled_redelivery_log idx_srl_created_batch 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_scheduled_redelivery_log'\n                    AND INDEX_NAME = 'idx_srl_batch_created_status'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_scheduled_redelivery_log ADD INDEX idx_srl_batch_created_status (batch_id, created_at, status)'))
                    logger.info('✓ ytx_scheduled_redelivery_log: 创建 idx_srl_batch_created_status 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_scheduled_redelivery_log idx_srl_batch_created_status 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_scheduled_rate_log'\n                    AND INDEX_NAME = 'idx_srate_created_batch'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_scheduled_rate_log ADD INDEX idx_srate_created_batch (created_at, batch_id)'))
                    logger.info('✓ ytx_scheduled_rate_log: 创建 idx_srate_created_batch 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_scheduled_rate_log idx_srate_created_batch 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_scheduled_rate_log'\n                    AND INDEX_NAME = 'idx_srate_batch_created_status'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_scheduled_rate_log ADD INDEX idx_srate_batch_created_status (batch_id, created_at, status)'))
                    logger.info('✓ ytx_scheduled_rate_log: 创建 idx_srate_batch_created_status 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_scheduled_rate_log idx_srate_batch_created_status 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_scheduled_polish_log'\n                    AND INDEX_NAME = 'idx_spol_created_batch'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_scheduled_polish_log ADD INDEX idx_spol_created_batch (created_at, batch_id)'))
                    logger.info('✓ ytx_scheduled_polish_log: 创建 idx_spol_created_batch 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_scheduled_polish_log idx_spol_created_batch 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_scheduled_polish_log'\n                    AND INDEX_NAME = 'idx_spol_batch_created_status'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_scheduled_polish_log ADD INDEX idx_spol_batch_created_status (batch_id, created_at, status)'))
                    logger.info('✓ ytx_scheduled_polish_log: 创建 idx_spol_batch_created_status 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_scheduled_polish_log idx_spol_batch_created_status 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_scheduled_red_flower_log'\n                    AND INDEX_NAME = 'idx_srf_created_batch'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_scheduled_red_flower_log ADD INDEX idx_srf_created_batch (created_at, batch_id)'))
                    logger.info('✓ ytx_scheduled_red_flower_log: 创建 idx_srf_created_batch 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_scheduled_red_flower_log idx_srf_created_batch 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_scheduled_red_flower_log'\n                    AND INDEX_NAME = 'idx_srf_batch_created_status'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_scheduled_red_flower_log ADD INDEX idx_srf_batch_created_status (batch_id, created_at, status)'))
                    logger.info('✓ ytx_scheduled_red_flower_log: 创建 idx_srf_batch_created_status 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_scheduled_red_flower_log idx_srf_batch_created_status 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_orders'\n                    AND INDEX_NAME = 'idx_order_owner_account_placed'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_orders ADD INDEX idx_order_owner_account_placed (owner_id, account_id, placed_at)'))
                    logger.info('✓ ytx_orders: 创建 idx_order_owner_account_placed 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_orders idx_order_owner_account_placed 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_orders'\n                    AND INDEX_NAME = 'idx_order_owner_placed'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_orders ADD INDEX idx_order_owner_placed (owner_id, placed_at)'))
                    logger.info('✓ ytx_orders: 创建 idx_order_owner_placed 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_orders idx_order_owner_placed 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_orders'\n                    AND INDEX_NAME = 'idx_order_owner_account_buyer_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_orders ADD INDEX idx_order_owner_account_buyer_created (owner_id, account_id, buyer_id, created_at)'))
                    logger.info('✓ ytx_orders: 创建 idx_order_owner_account_buyer_created 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_orders idx_order_owner_account_buyer_created 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_orders'\n                    AND INDEX_NAME = 'idx_order_created_at'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_orders ADD INDEX idx_order_created_at (created_at)'))
                    logger.info('✓ ytx_orders: 创建 idx_order_created_at 索引')
            except Exception as e:
                logger.warning(f'✗ ytx_orders idx_order_created_at 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_orders'\n                    AND INDEX_NAME = 'idx_order_placed_status'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_orders ADD INDEX idx_order_placed_status (placed_at, status)'))
                    logger.info('✓ ytx_orders: 创建 idx_order_placed_status 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_orders idx_order_placed_status 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_orders'\n                    AND INDEX_NAME = 'idx_order_created_status'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_orders ADD INDEX idx_order_created_status (created_at, status)'))
                    logger.info('✓ ytx_orders: 创建 idx_order_created_status 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_orders idx_order_created_status 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_orders'\n                    AND INDEX_NAME = 'idx_order_owner_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_orders ADD INDEX idx_order_owner_created (owner_id, created_at)'))
                    logger.info('✓ ytx_orders: 创建 idx_order_owner_created 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_orders idx_order_owner_created 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_product_materials'\n                    AND INDEX_NAME = 'idx_pm_user_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_product_materials ADD INDEX idx_pm_user_created (user_id, created_at)'))
                    logger.info('✓ ytx_product_materials: 创建 idx_pm_user_created 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_product_materials idx_pm_user_created 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_product_materials'\n                    AND INDEX_NAME = 'idx_pm_platform_category'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_product_materials ADD INDEX idx_pm_platform_category (platform_category_id)'))
                    logger.info('✓ ytx_product_materials: 创建 idx_pm_platform_category 索引')
            except Exception as e:
                logger.warning(f'✗ ytx_product_materials idx_pm_platform_category 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_publish_logs'\n                    AND INDEX_NAME = 'idx_publish_user_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_publish_logs ADD INDEX idx_publish_user_created (user_id, created_at)'))
                    logger.info('✓ ytx_publish_logs: 创建 idx_publish_user_created 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_publish_logs idx_publish_user_created 创建失败: {e}')
            auto_reply_log_table_exists = False
            try:
                table_check = text("\n                    SELECT COUNT(*) FROM information_schema.TABLES\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_auto_reply_message_logs'\n                ")
                table_result = await conn.execute(table_check)
                auto_reply_log_table_exists = table_result.scalar() > 0
            except Exception:
                auto_reply_log_table_exists = False
            if auto_reply_log_table_exists:
                try:
                    check = text("\n                        SELECT COUNT(*) FROM information_schema.STATISTICS\n                        WHERE TABLE_SCHEMA = DATABASE()\n                        AND TABLE_NAME = 'ytx_auto_reply_message_logs'\n                        AND INDEX_NAME = 'idx_arml_account_status_created'\n                    ")
                    result = await conn.execute(check)
                    if result.scalar() == 0:
                        await conn.execute(text('ALTER TABLE ytx_auto_reply_message_logs ADD INDEX idx_arml_account_status_created (account_id, process_status, created_at)'))
                        logger.info('✓ ytx_auto_reply_message_logs: 创建 idx_arml_account_status_created 复合索引')
                except Exception as e:
                    logger.warning(f'✗ ytx_auto_reply_message_logs idx_arml_account_status_created 创建失败: {e}')
                try:
                    check = text("\n                        SELECT COUNT(*) FROM information_schema.STATISTICS\n                        WHERE TABLE_SCHEMA = DATABASE()\n                        AND TABLE_NAME = 'ytx_auto_reply_message_logs'\n                        AND INDEX_NAME = 'idx_arml_owner_status_created'\n                    ")
                    result = await conn.execute(check)
                    if result.scalar() == 0:
                        await conn.execute(text('ALTER TABLE ytx_auto_reply_message_logs ADD INDEX idx_arml_owner_status_created (owner_id, process_status, created_at)'))
                        logger.info('✓ ytx_auto_reply_message_logs: 创建 idx_arml_owner_status_created 复合索引')
                except Exception as e:
                    logger.warning(f'✗ ytx_auto_reply_message_logs idx_arml_owner_status_created 创建失败: {e}')
                try:
                    check = text("\n                        SELECT COUNT(*) FROM information_schema.STATISTICS\n                        WHERE TABLE_SCHEMA = DATABASE()\n                        AND TABLE_NAME = 'ytx_auto_reply_message_logs'\n                        AND INDEX_NAME = 'idx_arml_status_strategy_created'\n                    ")
                    result = await conn.execute(check)
                    if result.scalar() == 0:
                        await conn.execute(text('ALTER TABLE ytx_auto_reply_message_logs ADD INDEX idx_arml_status_strategy_created (process_status, reply_strategy, created_at)'))
                        logger.info('✓ ytx_auto_reply_message_logs: 创建 idx_arml_status_strategy_created 复合索引')
                except Exception as e:
                    logger.warning(f'✗ ytx_auto_reply_message_logs idx_arml_status_strategy_created 创建失败: {e}')
                try:
                    check = text("\n                        SELECT COUNT(*) FROM information_schema.STATISTICS\n                        WHERE TABLE_SCHEMA = DATABASE()\n                        AND TABLE_NAME = 'ytx_auto_reply_message_logs'\n                        AND INDEX_NAME = 'idx_arml_status_created'\n                    ")
                    result = await conn.execute(check)
                    if result.scalar() == 0:
                        await conn.execute(text('ALTER TABLE ytx_auto_reply_message_logs ADD INDEX idx_arml_status_created (process_status, created_at)'))
                        logger.info('✓ ytx_auto_reply_message_logs: 创建 idx_arml_status_created 复合索引')
                except Exception as e:
                    logger.warning(f'✗ ytx_auto_reply_message_logs idx_arml_status_created 创建失败: {e}')
                try:
                    check = text("\n                        SELECT COUNT(*) FROM information_schema.STATISTICS\n                        WHERE TABLE_SCHEMA = DATABASE()\n                        AND TABLE_NAME = 'ytx_auto_reply_message_logs'\n                        AND INDEX_NAME = 'idx_order_no'\n                    ")
                    result = await conn.execute(check)
                    if result.scalar() == 0:
                        await conn.execute(text('ALTER TABLE ytx_auto_reply_message_logs ADD INDEX idx_order_no (order_no)'))
                        logger.info('✓ ytx_auto_reply_message_logs: 创建 idx_order_no 索引')
                except Exception as e:
                    logger.warning(f'✗ ytx_auto_reply_message_logs idx_order_no 创建失败: {e}')
                try:
                    check = text("\n                        SELECT COUNT(*) FROM information_schema.STATISTICS\n                        WHERE TABLE_SCHEMA = DATABASE()\n                        AND TABLE_NAME = 'ytx_auto_reply_message_logs'\n                        AND INDEX_NAME = 'idx_arml_order_strategy_id'\n                    ")
                    result = await conn.execute(check)
                    if result.scalar() == 0:
                        await conn.execute(text('ALTER TABLE ytx_auto_reply_message_logs ADD INDEX idx_arml_order_strategy_id (order_no, reply_strategy, id)'))
                        logger.info('✓ ytx_auto_reply_message_logs: 创建 idx_arml_order_strategy_id 复合索引')
                except Exception as e:
                    logger.warning(f'✗ ytx_auto_reply_message_logs idx_arml_order_strategy_id 创建失败: {e}')
                try:
                    check = text("\n                        SELECT COUNT(*) FROM information_schema.STATISTICS\n                        WHERE TABLE_SCHEMA = DATABASE()\n                        AND TABLE_NAME = 'ytx_auto_reply_message_logs'\n                        AND INDEX_NAME = 'idx_arml_strategy_order_id'\n                    ")
                    result = await conn.execute(check)
                    if result.scalar() == 0:
                        await conn.execute(text('ALTER TABLE ytx_auto_reply_message_logs ADD INDEX idx_arml_strategy_order_id (reply_strategy, order_no, id)'))
                        logger.info('✓ ytx_auto_reply_message_logs: 创建 idx_arml_strategy_order_id 复合索引')
                except Exception as e:
                    logger.warning(f'✗ ytx_auto_reply_message_logs idx_arml_strategy_order_id 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_personal_blacklist'\n                    AND INDEX_NAME = 'idx_pb_owner_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_personal_blacklist ADD INDEX idx_pb_owner_created (owner_id, created_at)'))
                    logger.info('✓ ytx_personal_blacklist: 创建 idx_pb_owner_created 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_personal_blacklist idx_pb_owner_created 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_platform_blacklist'\n                    AND INDEX_NAME = 'idx_plb_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_platform_blacklist ADD INDEX idx_plb_created (created_at)'))
                    logger.info('✓ ytx_platform_blacklist: 创建 idx_plb_created 索引')
            except Exception as e:
                logger.warning(f'✗ ytx_platform_blacklist idx_plb_created 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_listing_monitor_items'\n                    AND INDEX_NAME = 'idx_lmi_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_listing_monitor_items ADD INDEX idx_lmi_created (created_at)'))
                    logger.info('✓ ytx_listing_monitor_items: 创建 idx_lmi_created 索引')
            except Exception as e:
                logger.warning(f'✗ ytx_listing_monitor_items idx_lmi_created 创建失败: {e}')
            lmi_query_indexes = [('idx_lmi_dm_send', '(order_status, is_dm_sent, ordered_at)'), ('idx_lmi_order_pending', '(is_ordered, order_attempts)'), ('idx_lmi_item_ordered', '(item_id, is_ordered)'), ('idx_lmi_owner_publish', '(owner_id, publish_time)'), ('idx_lmi_owner_order_pending', '(owner_id, is_ordered, created_at, order_attempts)'), ('idx_lmi_owner_task_created', '(owner_id, monitor_task_id, created_at)')]
            for idx_name, idx_cols in lmi_query_indexes:
                try:
                    check = text("\n                        SELECT COUNT(*) FROM information_schema.STATISTICS\n                        WHERE TABLE_SCHEMA = DATABASE()\n                        AND TABLE_NAME = 'ytx_listing_monitor_items'\n                        AND INDEX_NAME = :idx_name\n                    ")
                    result = await conn.execute(check, {'idx_name': idx_name})
                    if result.scalar() == 0:
                        await conn.execute(text(f'ALTER TABLE ytx_listing_monitor_items ADD INDEX {idx_name} {idx_cols}'))
                        logger.info(f'✓ ytx_listing_monitor_items: 创建 {idx_name} 索引')
                except Exception as e:
                    logger.warning(f'✗ ytx_listing_monitor_items {idx_name} 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_listing_monitor_tasks'\n                    AND INDEX_NAME = 'idx_lmt_category'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_listing_monitor_tasks ADD INDEX idx_lmt_category (category_id)'))
                    logger.info('✓ ytx_listing_monitor_tasks: 创建 idx_lmt_category 索引')
            except Exception as e:
                logger.warning(f'✗ ytx_listing_monitor_tasks idx_lmt_category 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_risk_control_logs'\n                    AND INDEX_NAME = 'idx_rcl_owner_created'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_risk_control_logs ADD INDEX idx_rcl_owner_created (owner_id, created_at)'))
                    logger.info('✓ ytx_risk_control_logs: 创建 idx_rcl_owner_created 复合索引')
            except Exception as e:
                logger.warning(f'✗ ytx_risk_control_logs idx_rcl_owner_created 创建失败: {e}')
            try:
                check_old = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_order_fallback_accounts'\n                    AND INDEX_NAME = 'uk_ofa_owner'\n                ")
                result = await conn.execute(check_old)
                if result.scalar() > 0:
                    await conn.execute(text('ALTER TABLE ytx_order_fallback_accounts DROP INDEX uk_ofa_owner'))
                    logger.info('✓ ytx_order_fallback_accounts: 删除旧唯一键 uk_ofa_owner')
                check_new = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_order_fallback_accounts'\n                    AND INDEX_NAME = 'uk_ofa_owner_category'\n                ")
                result = await conn.execute(check_new)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_order_fallback_accounts ADD UNIQUE KEY uk_ofa_owner_category (owner_id, category_id)'))
                    logger.info('✓ ytx_order_fallback_accounts: 创建新唯一键 uk_ofa_owner_category')
            except Exception as e:
                logger.warning(f'✗ ytx_order_fallback_accounts 唯一键迁移失败: {e}')
            try:
                check_old = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_collect_fallback_accounts'\n                    AND INDEX_NAME = 'uk_cfa_owner'\n                ")
                result = await conn.execute(check_old)
                if result.scalar() > 0:
                    await conn.execute(text('ALTER TABLE ytx_collect_fallback_accounts DROP INDEX uk_cfa_owner'))
                    logger.info('✓ ytx_collect_fallback_accounts: 删除旧唯一键 uk_cfa_owner')
                check_new = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_collect_fallback_accounts'\n                    AND INDEX_NAME = 'uk_cfa_owner_category'\n                ")
                result = await conn.execute(check_new)
                if result.scalar() == 0:
                    await conn.execute(text('ALTER TABLE ytx_collect_fallback_accounts ADD UNIQUE KEY uk_cfa_owner_category (owner_id, category_id)'))
                    logger.info('✓ ytx_collect_fallback_accounts: 创建新唯一键 uk_cfa_owner_category')
            except Exception as e:
                logger.warning(f'✗ ytx_collect_fallback_accounts 唯一键迁移失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_orders'\n                    AND INDEX_NAME = 'uk_order_account_no'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    dup_check = text('\n                        SELECT COUNT(*) FROM (\n                            SELECT account_id, order_no\n                            FROM ytx_orders\n                            GROUP BY account_id, order_no\n                            HAVING COUNT(*) > 1\n                        ) AS dup\n                    ')
                    dup_result = await conn.execute(dup_check)
                    dup_groups = dup_result.scalar() or 0
                    if dup_groups > 0:
                        logger.warning(f'✗ ytx_orders 存在 {dup_groups} 组 (account_id, order_no) 重复数据，为遵守禁止删除数据规范，暂不创建 uk_order_account_no 唯一约束。请人工合并重复订单后，重启服务自动补建（当前由 Redis 账号锁兜底防并发）')
                    else:
                        await conn.execute(text('ALTER TABLE ytx_orders ADD UNIQUE KEY uk_order_account_no (account_id, order_no)'))
                        logger.info('✓ ytx_orders: 创建 uk_order_account_no 唯一约束')
            except Exception as e:
                logger.warning(f'✗ ytx_orders uk_order_account_no 创建失败: {e}')
            try:
                check = text("\n                    SELECT COUNT(*) FROM information_schema.STATISTICS\n                    WHERE TABLE_SCHEMA = DATABASE()\n                    AND TABLE_NAME = 'ytx_accounts'\n                    AND INDEX_NAME = 'uk_account_id'\n                ")
                result = await conn.execute(check)
                if result.scalar() == 0:
                    dup_check = text('\n                        SELECT COUNT(*) FROM (\n                            SELECT account_id\n                            FROM ytx_accounts\n                            GROUP BY account_id\n                            HAVING COUNT(*) > 1\n                        ) AS dup\n                    ')
                    dup_result = await conn.execute(dup_check)
                    dup_groups = dup_result.scalar() or 0
                    if dup_groups > 0:
                        logger.warning(f'✗ ytx_accounts 存在 {dup_groups} 组 account_id 重复数据，为遵守禁止删除数据规范，暂不创建 uk_account_id 唯一约束。请人工合并/处理重复账号后，重启服务自动补建')
                    else:
                        await conn.execute(text('ALTER TABLE ytx_accounts ADD UNIQUE KEY uk_account_id (account_id)'))
                        logger.info('✓ ytx_accounts: 创建 uk_account_id 全局唯一约束')
            except Exception as e:
                logger.warning(f'✗ ytx_accounts uk_account_id 创建失败: {e}')

    async def create_default_admin(self):
        """创建默认管理员用户 (admin/admin123)"""
        logger.info('检查默认管理员用户...')
        try:
            async with async_session_maker() as session:
                result = await session.execute(text("SELECT id FROM ytx_users WHERE username = 'admin' LIMIT 1"))
                existing = result.fetchone()
                if existing:
                    logger.info('✓ 管理员用户已存在，跳过创建')
                    return
                password_hash = get_password_hash('admin123')
                await session.execute(text("\n                        INSERT INTO ytx_users (username, email, password_hash, status, role, created_at, updated_at)\n                        VALUES ('admin', 'admin@example.com', :password_hash, 'ACTIVE', 'ADMIN', NOW(), NOW())\n                    "), {'password_hash': password_hash})
                await session.commit()
                logger.info('✓ 默认管理员用户创建成功')
                logger.info('  用户名: admin')
                logger.info('  密码: admin123')
        except IntegrityError:
            logger.info('✓ 管理员用户已存在，跳过创建')
        except Exception as e:
            logger.error(f'✗ 创建管理员用户失败: {e}')

    async def init_system_settings(self):
        """初始化系统设置"""
        logger.info('初始化系统设置...')
        try:
            async with async_session_maker() as session:
                for key, value, description in self.DEFAULT_SETTINGS:
                    try:
                        await session.execute(text('\n                                INSERT IGNORE INTO ytx_system_settings (`key`, value, description, updated_at)\n                                VALUES (:key, :value, :description, NOW())\n                            '), {'key': key, 'value': value, 'description': description})
                    except Exception as e:
                        logger.warning(f'设置 {key} 插入失败: {e}')
                await session.commit()
                logger.info(f'✓ 系统设置初始化完成，共 {len(self.DEFAULT_SETTINGS)} 项')
        except Exception as e:
            logger.error(f'✗ 初始化系统设置失败: {e}')

    async def init_scheduled_tasks(self):
        """初始化定时任务配置"""
        logger.info('初始化定时任务配置...')
        try:
            async with async_session_maker() as session:
                for task_code, task_name, interval_seconds, enabled, description in self.DEFAULT_SCHEDULED_TASKS:
                    try:
                        await session.execute(text('\n                                INSERT IGNORE INTO ytx_scheduled_tasks \n                                (task_code, task_name, interval_seconds, enabled, description, created_at, updated_at)\n                                VALUES (:task_code, :task_name, :interval_seconds, :enabled, :description, NOW(), NOW())\n                            '), {'task_code': task_code, 'task_name': task_name, 'interval_seconds': interval_seconds, 'enabled': enabled, 'description': description})
                    except Exception as e:
                        logger.warning(f'定时任务 {task_code} 插入失败: {e}')
                await session.commit()
                logger.info(f'✓ 定时任务配置初始化完成，共 {len(self.DEFAULT_SCHEDULED_TASKS)} 项')
        except Exception as e:
            logger.error(f'✗ 初始化定时任务配置失败: {e}')

    async def init_publish_addresses(self):
        """初始化随机地址默认数据"""
        logger.info('初始化随机地址默认数据...')
        try:
            default_addresses = build_default_publish_addresses()
            async with async_session_maker() as session:
                removed_conditions = []
                removed_params: dict[str, str] = {}
                for index, prefix in enumerate(REMOVED_PUBLISH_ADDRESS_PREFIXES):
                    param_key = f'removed_prefix_{index}'
                    removed_conditions.append(f'search_keyword LIKE :{param_key}')
                    removed_params[param_key] = f'{prefix}%'
                if removed_conditions:
                    await session.execute(text(f"\n                                UPDATE ytx_publish_addresses\n                                SET is_enabled = 0,\n                                    updated_at = NOW()\n                                WHERE account_id IS NULL\n                                  AND remark = '系统初始化默认地址'\n                                  AND ({' OR '.join(removed_conditions)})\n                            "), removed_params)
                existing_result = await session.execute(text('\n                        SELECT search_keyword\n                        FROM ytx_publish_addresses\n                        WHERE account_id IS NULL\n                    '))
                existing_keywords = {str(search_keyword).strip() for search_keyword in existing_result.scalars().all() if search_keyword and str(search_keyword).strip()}
                insert_rows = [{'name': address, 'search_keyword': address, 'sort_order': index, 'remark': '系统初始化默认地址'} for index, address in enumerate(default_addresses, start=1) if address not in existing_keywords]
                if insert_rows:
                    await session.execute(text('\n                            INSERT INTO ytx_publish_addresses (\n                                name,\n                                search_keyword,\n                                expected_text,\n                                account_id,\n                                weight,\n                                sort_order,\n                                is_enabled,\n                                use_count,\n                                last_used_at,\n                                created_by,\n                                remark,\n                                created_at,\n                                updated_at\n                            ) VALUES (\n                                :name,\n                                :search_keyword,\n                                NULL,\n                                NULL,\n                                1,\n                                :sort_order,\n                                1,\n                                0,\n                                NULL,\n                                NULL,\n                                :remark,\n                                NOW(),\n                                NOW()\n                            )\n                        '), insert_rows)
                    await session.commit()
                logger.info(f'✓ 随机地址默认数据初始化完成，默认 {len(default_addresses)} 条，本次新增 {len(insert_rows)} 条')
        except Exception as e:
            logger.warning(f'✗ 初始化随机地址默认数据失败（不影响系统运行）: {e}')

    async def init_redis_platform_day(self):
        """初始化Redis中的平台日"""
        logger.info('初始化Redis平台日...')
        try:
            from common.db.redis_client import get_redis_client
            redis_client = await get_redis_client()
            platform_day_key = 'ytx:platform:day'
            existing_day = await redis_client.get(platform_day_key)
            if existing_day:
                logger.info(f'✓ Redis平台日已存在: {existing_day}')
            else:
                current_day = get_beijing_now_naive().strftime('%Y-%m-%d')
                await redis_client.set(platform_day_key, current_day)
                logger.info(f'✓ Redis平台日初始化完成: {current_day}')
        except Exception as e:
            logger.warning(f'✗ 初始化Redis平台日失败（不影响系统运行）: {e}')

    async def migrate_card_item_relations(self):
        """
        迁移卡券商品关联数据（仅在关联表为空时执行一次）
        
        将 ytx_cards 表中 item_id 不为空的记录迁移到 ytx_card_item_relations 关联表。
        关联表已有数据时跳过，避免重复迁移。
        """
        try:
            async with async_session_maker() as session:
                existing_result = await session.execute(text('SELECT COUNT(*) FROM ytx_card_item_relations'))
                existing = existing_result.scalar()
                if existing > 0:
                    logger.debug(f'✓ 卡券商品关联表已有 {existing} 条数据，跳过迁移')
                    return
                count_result = await session.execute(text("\n                        SELECT COUNT(*) FROM ytx_cards \n                        WHERE item_id IS NOT NULL AND item_id != ''\n                    "))
                total = count_result.scalar()
                if total == 0:
                    logger.info('✓ 无需迁移卡券商品关联数据')
                    return
                migrate_result = await session.execute(text("\n                        INSERT IGNORE INTO ytx_card_item_relations (user_id, card_id, item_id, created_at, updated_at)\n                        SELECT user_id, id, item_id, NOW(), NOW()\n                        FROM ytx_cards\n                        WHERE item_id IS NOT NULL AND item_id != ''\n                    "))
                await session.commit()
                migrated = migrate_result.rowcount
                logger.info(f'✓ 卡券商品关联数据首次迁移完成：源数据 {total} 条，本次迁移 {migrated} 条')
        except Exception as e:
            logger.warning(f'✗ 卡券商品关联数据迁移失败（不影响系统运行）: {e}')

    async def migrate_delivery_block_rules(self):
        """迁移旧禁止发货设置到规则配置表

        对于已开启 delivery_disabled=1 但 ytx_delivery_block_rules 表中无记录的账号，
        自动将旧配置迁移为一条 buyer_credit_zero 规则。
        迁移后旧字段保留不删除，但不再使用。
        """
        try:
            async with async_session_maker() as session:
                result = await session.execute(text('\n                        SELECT a.account_id, a.delivery_disabled_reason,\n                               a.auto_close_order, a.delivery_only_card_after_close,\n                               a.delivery_disabled_excluded_items\n                        FROM ytx_accounts a\n                        WHERE a.delivery_disabled = 1\n                        AND NOT EXISTS (\n                            SELECT 1 FROM ytx_delivery_block_rules r\n                            WHERE r.account_id = a.account_id\n                        )\n                    '))
                rows = result.fetchall()
                if not rows:
                    logger.info('✓ 无需迁移禁止发货规则数据')
                    return
                migrated_count = 0
                for row in rows:
                    account_id = row[0]
                    reason = row[1]
                    auto_close = row[2]
                    only_card = row[3]
                    excluded_items = row[4]
                    excluded_json = None
                    if excluded_items:
                        if isinstance(excluded_items, str):
                            excluded_json = excluded_items
                        elif isinstance(excluded_items, list):
                            import json as _json
                            excluded_json = _json.dumps(excluded_items, ensure_ascii=False)
                    try:
                        await session.execute(text('\n                                INSERT IGNORE INTO ytx_delivery_block_rules\n                                (account_id, rule_code, enabled, priority, block_reason,\n                                 auto_close_order, only_card_after_close, excluded_item_ids, config)\n                                VALUES\n                                (:account_id, \'buyer_credit_zero\', 1, 10, :block_reason,\n                                 :auto_close, :only_card, :excluded_ids, \'{"threshold": 0}\')\n                            '), {'account_id': account_id, 'block_reason': reason, 'auto_close': 1 if auto_close else 0, 'only_card': 1 if only_card else 0, 'excluded_ids': excluded_json})
                        migrated_count += 1
                    except Exception as row_err:
                        logger.warning(f'✗ 迁移账号 {account_id} 禁止发货规则失败: {row_err}')
                await session.commit()
                logger.info(f'✓ 禁止发货规则迁移完成：共 {len(rows)} 个账号，成功迁移 {migrated_count} 条')
        except Exception as e:
            logger.warning(f'✗ 禁止发货规则迁移失败（不影响系统运行）: {e}')

async def init_database():
    """初始化数据库（供外部调用）"""
    initializer = DatabaseInitializer()
    await initializer.init_all()
if __name__ == '__main__':
    import asyncio
    asyncio.run(init_database())
