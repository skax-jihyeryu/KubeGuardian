"""헬스 체크. Deployment의 liveness·readiness 프로브가 호출한다."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
