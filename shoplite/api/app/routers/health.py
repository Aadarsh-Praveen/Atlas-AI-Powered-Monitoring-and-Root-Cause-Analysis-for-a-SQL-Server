"""GET /health: is the API up, and can it reach SQL Server?

Used by you in Phase 1, by deployment checks in Phase 2, and by Azure
availability tests and alerts in Phase 5. A health check must always answer,
so any database failure becomes a 503 "unavailable" response, never a 500.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine
from app.schemas import HealthResponse

logger = logging.getLogger("shoplite.health")

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "Database unreachable"}},
    summary="Check the API and its database connection",
)
def health():
    version = get_settings().app_version
    try:
        # Goes through the normal pool and login, so it tests the same path
        # real requests use, not a special side door.
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1;")).scalar_one()
    except Exception:
        # Pool timeout, login failure, network error, query timeout: all mean
        # "not healthy right now". Log the real cause (with the request ID).
        logger.warning("Health check failed: database unreachable", exc_info=True)
        body = HealthResponse(status="unavailable", version=version, database="unreachable")
        return JSONResponse(status_code=503, content=body.model_dump())

    return HealthResponse(status="ok", version=version, database="ok")
