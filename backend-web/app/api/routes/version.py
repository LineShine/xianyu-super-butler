from fastapi import APIRouter
from app.services.version_service import get_current_version
from common.schemas.common import ApiResponse
router = APIRouter(prefix="/version", tags=["版本信息"])
@router.get("/current", response_model=ApiResponse)
async def current_version():
    return ApiResponse(success=True, data={"version": get_current_version(), "name": "鱼太咸"})
