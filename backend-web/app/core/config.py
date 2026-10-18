"""
Backend-Web服务配置模块

功能：
1. 继承common配置基类
2. 添加Backend-Web服务特定配置
3. 从.env文件读取配置
"""
from __future__ import annotations
import json
from functools import lru_cache
from typing import List
from pydantic import Field, computed_field
from common.core.config import BaseConfig

class BackendWebConfig(BaseConfig):
    """
    Backend-Web服务配置类
    
    包含Backend-Web服务特定配置：
    - 服务端口
    - JWT配置
    - CORS配置
    - 服务间通信URL
    """
    project_name: str = Field(default='鱼太咸 Web 服务')
    version: str = Field(default='0.1.0')
    api_v1_prefix: str = Field(default='/api/v1')
    service_port: int = Field(default=8089, alias='BACKEND_WEB_PORT')
    jwt_secret_key: str = Field(default='change-me', repr=False)
    jwt_algorithm: str = Field(default='HS256')
    access_token_expire_minutes: int = Field(default=1440)
    refresh_token_expire_minutes: int = Field(default=60 * 24 * 7)
    cors_origins_raw: str = Field(default='*', alias='CORS_ORIGINS')
    websocket_service_url: str = Field(default='http://127.0.0.1:8090', alias='WEBSOCKET_SERVICE_URL')
    scheduler_service_url: str = Field(default='http://127.0.0.1:8091', alias='SCHEDULER_SERVICE_URL')
    static_dir: str = Field(default='static', alias='STATIC_DIR')
    frontend_public_url: str = Field(default='', alias='FRONTEND_PUBLIC_URL')
    backend_web_public_url: str = Field(default='http://127.0.0.1:8089', alias='BACKEND_WEB_PUBLIC_URL')
    auto_start_crawl_jobs: bool = Field(default=True, alias='AUTO_START_CRAWL_JOBS')

    @computed_field(return_type=list[str])
    @property
    def cors_origins(self) -> List[str]:
        """解析CORS配置"""
        raw_value = self.cors_origins_raw.strip()
        if not raw_value:
            return ['*']
        if raw_value == '*':
            return ['*']
        try:
            parsed = json.loads(raw_value)
            if isinstance(parsed, list):
                return [str(origin).strip() for origin in parsed if str(origin).strip()]
        except json.JSONDecodeError:
            pass
        return [origin.strip() for origin in raw_value.split(',') if origin.strip()]

@lru_cache
def get_settings() -> BackendWebConfig:
    """返回缓存的配置实例"""
    return BackendWebConfig()
