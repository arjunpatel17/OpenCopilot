from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
import httpx
from functools import lru_cache
from app.config import settings

security = HTTPBearer(auto_error=False)

JWKS_URL = f"https://login.microsoftonline.com/{settings.azure_tenant_id}/discovery/v2.0/keys"
ISSUER = f"https://login.microsoftonline.com/{settings.azure_tenant_id}/v2.0"


@lru_cache(maxsize=1)
def _get_jwks() -> dict:
    resp = httpx.get(JWKS_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()


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
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        )
