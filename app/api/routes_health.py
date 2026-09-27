from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(prefix="/v1", tags=["health"])


@router.get("/health", summary="Liveness check")
def health() -> dict:
    s = get_settings()
    return {"status": "ok", "service": s.app_name, "version": s.app_version, "env": s.app_env}
