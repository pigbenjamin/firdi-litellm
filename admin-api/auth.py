import os
from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer()


def verify_admin_key(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> None:
    expected = os.getenv("ADMIN_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured")
    if credentials.credentials != expected:
        raise HTTPException(status_code=401, detail="Invalid admin API key")
