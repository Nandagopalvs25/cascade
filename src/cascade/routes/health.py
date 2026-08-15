from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def healthz():
    return {"status": "ok"}
