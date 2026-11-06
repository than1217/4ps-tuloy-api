from fastapi import APIRouter
from fastapi.responses import JSONResponse

import database

router = APIRouter(tags=["Operations"])


@router.get("/healthz", include_in_schema=False)
async def liveness():
    """Process is up. No dependencies are touched."""
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readiness():
    """Ready to serve: tunnel (if configured) and a read-only SELECT 1 succeed."""
    checks = database.ping()
    ready = checks["database"] == "up"
    return JSONResponse({"status": "ready" if ready else "not_ready", "checks": checks},
                        status_code=200 if ready else 503)
