"""
用户设置 API 路由

提供用户个人设置的管理功能
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from common.models.user import User
from common.models.user_setting import UserSetting
from common.utils.product_settings import is_removed_setting
from common.schemas.common import ApiResponse
from common.utils.image_utils import image_manager
from common.services.remote_location_message_api import (
    RemoteLocationMessageTestResult,
    test_remote_location_message_interface,
)

from common.utils.time_utils import safe_isoformat
router = APIRouter(tags=["用户设置"])

# 位置聊天远程接口配置键，与前端保持一致。
LOCATION_CHAT_REMOTE_URL_KEY = "location_chat.remote_url"
LOCATION_CHAT_REMOTE_SECRET_KEY = "location_chat.remote_secret_key"

# 单用户串行测试，并限制短时间内重复请求，避免误操作放大远程调用量。
LOCATION_API_TEST_INTERVAL_SECONDS = 5.0
_location_api_test_active_users: set[int] = set()
_location_api_test_last_started: dict[int, float] = {}


class UserSettingUpdate(BaseModel):
    """用户设置更新请求"""
    value: str
    description: Optional[str] = None


class LocationChatApiTestRequest(BaseModel):
    """位置聊天远程接口连通性测试请求。"""

    url: str
    secret_key: str


@router.post("/external-api/location-chat/test", response_model=ApiResponse)
async def test_location_chat_external_api(
    payload: LocationChatApiTestRequest,
    current_user: User = Depends(deps.get_current_active_user),
) -> ApiResponse:
    """测试位置消息远程接口，不写入配置且不返回秘钥或报文内容。"""
    user_id = int(current_user.id)
    now = time.monotonic()
    if user_id in _location_api_test_active_users:
        return ApiResponse(success=False, message="位置聊天远程接口测试正在进行，请稍后再试")
    previous = _location_api_test_last_started.get(user_id)
    if previous is not None and now - previous < LOCATION_API_TEST_INTERVAL_SECONDS:
        return ApiResponse(success=False, message="位置聊天远程接口测试过于频繁，请稍后再试")

    _location_api_test_active_users.add(user_id)
    _location_api_test_last_started[user_id] = now
    try:
        result: RemoteLocationMessageTestResult = await test_remote_location_message_interface(
            payload.url,
            payload.secret_key,
        )
        metadata = {
            "status_code": result.status_code,
            "duration_ms": result.duration_ms,
            "protocol_version": result.protocol_version,
        }
        # The settings test only verifies that the configured endpoint is reachable.
        # A 200 response is sufficient even when the returned test envelope differs
        # from this application's sample LWP card.
        if result.status_code == 200:
            return ApiResponse(
                success=True,
                message=result.message if result.success else "测试成功",
                data=metadata,
            )
        return ApiResponse(success=False, message=result.message, data=metadata)
    except Exception:  # noqa: BLE001
        # 远程服务的异常内容可能包含请求信息，统一返回安全的中文提示。
        return ApiResponse(success=False, message="位置聊天远程接口测试失败，请检查URL和秘钥")
    finally:
        _location_api_test_active_users.discard(user_id)


@router.get("")
async def get_user_settings(
    current_user: User = Depends(deps.get_current_active_user),
    session: AsyncSession = Depends(deps.get_db_session),
) -> Dict[str, Any]:
    """获取当前用户的所有设置"""
    stmt = select(UserSetting).where(UserSetting.user_id == current_user.id)
    result = await session.execute(stmt)
    settings = result.scalars().all()
    
    return {
        setting.key: {
            "value": setting.value,
            "description": setting.description,
            "updated_at": safe_isoformat(setting.updated_at),
        }
        for setting in settings if not is_removed_setting(setting.key)
    }


@router.get("/{key}")
async def get_user_setting(
    key: str,
    current_user: User = Depends(deps.get_current_active_user),
    session: AsyncSession = Depends(deps.get_db_session),
) -> Dict[str, Any]:
    """获取用户特定设置，不存在时返回空值而非404"""
    stmt = select(UserSetting).where(
        UserSetting.user_id == current_user.id,
        UserSetting.key == key,
    )
    result = await session.execute(stmt)
    setting = result.scalar_one_or_none()
    
    if not setting:
        return {
            "success": True,
            "key": key,
            "value": "",
            "description": "",
        }
    
    return {
        "success": True,
        "key": setting.key,
        "value": setting.value,
        "description": setting.description,
    }


@router.put("/{key}", response_model=ApiResponse)
async def update_user_setting(
    key: str,
    payload: UserSettingUpdate,
    current_user: User = Depends(deps.get_current_active_user),
    session: AsyncSession = Depends(deps.get_db_session),
) -> ApiResponse:
    """更新用户设置"""
    if is_removed_setting(key):
        raise HTTPException(status_code=404, detail="此功能不属于鱼太咸")
    stmt = select(UserSetting).where(
        UserSetting.user_id == current_user.id,
        UserSetting.key == key,
    )
    result = await session.execute(stmt)
    setting = result.scalar_one_or_none()
    
    if setting:
        # 更新现有设置
        setting.value = payload.value
        if payload.description is not None:
            setting.description = payload.description
    else:
        # 创建新设置
        setting = UserSetting(
            user_id=current_user.id,
            key=key,
            value=payload.value,
            description=payload.description,
        )
        session.add(setting)
    
    await session.commit()


    return ApiResponse(success=True, message="设置已保存")


@router.delete("/{key}", response_model=ApiResponse)
async def delete_user_setting(
    key: str,
    current_user: User = Depends(deps.get_current_active_user),
    session: AsyncSession = Depends(deps.get_db_session),
) -> ApiResponse:
    """删除用户设置"""
    stmt = delete(UserSetting).where(
        UserSetting.user_id == current_user.id,
        UserSetting.key == key,
    )
    result = await session.execute(stmt)
    await session.commit()


    return ApiResponse(success=True, message="设置已删除")




