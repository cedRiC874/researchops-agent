from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .application import actor_hash


class BearerTokenAuth:
    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token
        self._scheme = HTTPBearer(auto_error=False)

    async def __call__(self, request: Request) -> str:
        credentials: HTTPAuthorizationCredentials | None = await self._scheme(request)
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials, self._expected_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_code": "authentication_required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return actor_hash(credentials.credentials)
