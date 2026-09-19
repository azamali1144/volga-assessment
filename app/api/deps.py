"""
Route-level dependencies: auth + rate limiting, applied via `Depends()` so no
route can accidentally be added without them (see app/main.py's `require_api_key`
usage on every route).
"""
from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from app.core.config import settings


async def require_api_key(request: Request, x_api_key: str = Header(default="")) -> str:
    if x_api_key not in settings.API_KEYS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Send it as the X-API-Key header.",
        )
    if not request.app.state.rate_limiter.allow(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({settings.RATE_LIMIT_PER_MINUTE}/min). Try again shortly.",
        )
    return x_api_key
