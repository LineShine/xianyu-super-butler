"""鱼太咸原生服务状态和重启入口。"""
from __future__ import annotations
import asyncio
import aiohttp
from fastapi import APIRouter, Depends
from loguru import logger
from app.api.deps import get_current_admin_user
from app.core.config import get_settings
from app.core.http_client import get_http_client
from common.models.user import User
from common.schemas.common import ApiResponse
from common.utils.service_restart import SERVICE_META, detect_runtime, restart_service
router = APIRouter(prefix='/system-control', tags=['系统管理'])
settings = get_settings()
_HEALTH_PROBE_TIMEOUT = 30.0
_SERVICE_URL_MAP = {'websocket': lambda : settings.websocket_service_url, 'scheduler': lambda : settings.scheduler_service_url}

def _health_url(service_key: str) -> str:
    """
    构建各服务的健康检查 URL

    - backend-web：本机 127.0.0.1:<port>/api/v1/health/ping
    - websocket / scheduler：使用服务间通信地址 + /health（dev 为 localhost，docker 为容器名）
    """
    if service_key == 'backend-web':
        return f'http://127.0.0.1:{settings.service_port}/api/v1/health/ping'
    base = _SERVICE_URL_MAP[service_key]().rstrip('/')
    return f'{base}/health'

@router.get('/status')
async def get_services_status(_admin: User=Depends(get_current_admin_user)) -> ApiResponse:
    """
    查询三个服务的在线状态

    统一通过调用各服务的健康检查接口判断（而非仅检测端口占用），
    能真实反映 HTTP 服务是否可响应：
    - backend-web：/api/v1/health/ping
    - websocket / scheduler：/health
    三个探测并行执行，单个超时 15 秒且不重试。
    """
    runtime = detect_runtime()
    keys = list(SERVICE_META.keys())
    results = await asyncio.gather(*[_probe_health(_health_url(k)) for k in keys])
    services = []
    for (key, online) in zip(keys, results):
        meta = SERVICE_META[key]
        services.append({'key': key, 'label': meta['label'], 'port': meta['port'], 'online': online})
    return ApiResponse(success=True, message='查询成功', data={'runtime': runtime, 'services': services})

async def _probe_health(url: str) -> bool:
    """
    调用健康检查接口判断服务是否在线（单次请求，短超时，无重试）

    Args:
        url: 健康检查完整 URL

    Returns:
        True 表示服务 HTTP 可响应（HTTP 状态码 < 400）
    """
    timeout = aiohttp.ClientTimeout(total=_HEALTH_PROBE_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                return resp.status < 400
    except Exception:
        return False

@router.post('/restart/{service_key}')
async def restart(service_key: str, _admin: User=Depends(get_current_admin_user)) -> ApiResponse:
    """
    重启指定服务

    Args:
        service_key: backend-web / websocket / scheduler

    Returns:
        统一响应；成功表示「已触发重启」，服务会在数秒内恢复。
    """
    if service_key not in SERVICE_META:
        return ApiResponse(success=False, message=f'未知服务：{service_key}', data=None)
    label = SERVICE_META[service_key]['label']
    runtime = detect_runtime()
    if service_key == 'backend-web':
        result = restart_service('backend-web')
        return ApiResponse(success=bool(result.get('success')), message=result.get('message') or '', data={'mode': result.get('mode')})
    result = restart_service(service_key)
    return ApiResponse(success=bool(result.get('success')), message=result.get('message') or '', data={'mode': result.get('mode')})
