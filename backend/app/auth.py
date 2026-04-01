import time
from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
import httpx
from app.config import settings

security = HTTPBearer(auto_error=False)

JWKS_URL = f"https://login.microsoftonline.com/{settings.azure_tenant_id}/discovery/v2.0/keys"
ISSUER = f"https://login.microsoftonline.com/{settings.azure_tenant_id}/v2.0"


# JWKS cache with TTL (refresh every 6 hours instead of caching forever)
_jwks_cache: dict | None = None
_jwks_cache_time: float = 0
_JWKS_TTL = 6 * 3600


def _get_jwks() -> dict:
    global _jwks_cache, _jwks_cache_time
    now = time.monotonic()
    if _jwks_cache is None or (now - _jwks_cache_time) > _JWKS_TTL:
        resp = httpx.get(JWKS_URL, timeout=10)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_cache_time = now
    return _jwks_cache


def _decode_token(token: str) -> dict:
    jwks = _get_jwks()
    unverified_header = jwt.get_unverified_header(token)
    key = None
    for k in jwks.get("keys", []):
        if k["kid"] == unverified_header.get("kid"):
            key = k
            break
    if key is None:
        raise JWTError("Matching key not found")

    return jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=settings.azure_client_id,
        issuer=ISSUER,
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict | None:
    if not settings.auth_enabled:
        return {"sub": "local-dev", "name": "Developer"}

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
        )

    try:
        payload = _decode_token(credentials.credentials)
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


async def verify_ws_token(websocket: WebSocket) -> dict | None:
    """Verify authentication for WebSocket connections via query parameter.

    Returns user dict on success, None if auth fails.
    When auth is disabled, returns a local-dev user.
    """
    if not settings.auth_enabled:
        return {"sub": "local-dev", "name": "Developer"}

    token = websocket.query_params.get("token")
    if not token:
        return None

    try:
        return _decode_token(token)
    except JWTError:
        return None
