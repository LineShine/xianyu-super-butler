"""
数据库会话配置

提供异步数据库连接和会话管理
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from loguru import logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from common.core.config import get_settings

settings = get_settings()


def _patch_asyncmy_ping() -> None:
    """兼容 SQLAlchemy 2.0.41 与 asyncmy 的 pool_pre_ping 调用签名。

    SQLAlchemy 的 MySQL 通用方言会无参调用适配层 ``ping()``，但该版本的
    ``AsyncAdapt_asyncmy_connection.ping`` 要求必须传入 ``reconnect``。
    仅为适配层补充默认值，不修改 asyncmy 驱动本身。
    """
    try:
        import inspect
        from sqlalchemy.dialects.mysql.asyncmy import AsyncAdapt_asyncmy_connection

        original_ping = AsyncAdapt_asyncmy_connection.ping

        # 如果已经 patch 过，跳过
        if getattr(original_ping, "_compat_patched", False):
            return

        try:
            reconnect = inspect.signature(original_ping).parameters.get("reconnect")
            if reconnect is None or reconnect.default is not inspect.Parameter.empty:
                return
        except (ValueError, TypeError):
            return

        def patched_ping(self, reconnect=False):
            return original_ping(self, reconnect)

        patched_ping._compat_patched = True
        AsyncAdapt_asyncmy_connection.ping = patched_ping

    except (ImportError, AttributeError):
        pass


# 在引擎创建前执行 patch
_patch_asyncmy_ping()


def _compile_sql_with_params(statement, parameters):
    """
    记录SQL模板，始终隐藏绑定参数
    
    Args:
        statement: SQL语句
        parameters: 参数字典或元组
    
    Returns:
        不包含绑定参数的SQL模板
    """
    return str(statement) + " [bound parameters hidden]"


async_engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    hide_parameters=True,  # 关闭SQL输出
    echo_pool=False,  # 不输出连接池日志
    pool_pre_ping=settings.db_pool_pre_ping,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_recycle=settings.db_pool_recycle,
    pool_use_lifo=settings.db_pool_use_lifo,
    connect_args={"connect_timeout": settings.db_connect_timeout},
)


# 监听SQL执行事件，输出完整的SQL（带参数）
# 仅在 settings.sql_echo 为 True 时注册钩子：
# - 开启时通过 loguru 输出，控制台与文件日志均可见（Docker 环境亦可见）；
# - 关闭时不注册钩子，不产生任何字符串拼接开销（适合高并发生产环境）。
def _register_sql_echo() -> None:
    @event.listens_for(async_engine.sync_engine, "before_cursor_execute")
    def receive_before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        """在SQL执行前触发，打印隐藏参数的SQL模板。"""
        compiled_sql = _compile_sql_with_params(statement, parameters)
        logger.opt(depth=1).info(f"[SQL]\n{'='*60}\n{compiled_sql}\n{'='*60}")


if settings.sql_echo:
    _register_sql_echo()


async_session_maker = async_sessionmaker(
    async_engine,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an AsyncSession."""
    async with async_session_maker() as session:
        yield session

