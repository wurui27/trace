from fastapi import APIRouter

router = APIRouter(prefix="/v1")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readiness")
async def readiness() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "state": "healthy",
        "capabilities": [],
    }
