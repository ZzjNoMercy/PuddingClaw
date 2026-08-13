"""Public, non-secret description of the active deployment profile."""

from fastapi import APIRouter

from extensions import runtime_profile


router = APIRouter(prefix="/runtime-profile", tags=["runtime"])


@router.get("")
async def get_runtime_profile() -> dict[str, object]:
    return runtime_profile()
