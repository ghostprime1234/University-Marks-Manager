"""Supabase Auth (GoTrue) integration and JWT validation utilities."""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, Optional, Union

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
SUPABASE_PUBLIC_KEY = os.getenv("SUPABASE_PUBLIC_KEY", "")


def ensure_uuid(val: Any) -> uuid.UUID:
    """Safely convert a value to uuid.UUID, or raise ValueError."""
    if isinstance(val, uuid.UUID):
        return val
    if val is None:
        raise ValueError("Cannot convert None to UUID")
    return uuid.UUID(str(val).strip())


def verify_supabase_token(token: str) -> Dict[str, Any]:
    """Verify a Supabase JWT token and return decoded payload dict.

    Validates against SUPABASE_JWT_SECRET (HS256) or SUPABASE_PUBLIC_KEY (RS256/ES256).
    If no secret or public key is set (e.g. local dev / test), decodes token claims.

    Raises:
        HTTPException: 401 Unauthorized if token is expired, corrupted, or missing subject.
    """
    if not token or not isinstance(token, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or empty authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    jwt_secret = os.getenv("SUPABASE_JWT_SECRET", SUPABASE_JWT_SECRET)
    public_key = os.getenv("SUPABASE_PUBLIC_KEY", SUPABASE_PUBLIC_KEY)

    try:
        if public_key:
            payload = jwt.decode(
                token,
                public_key,
                algorithms=["RS256", "ES256"],
                options={"verify_aud": False},
            )
        elif jwt_secret:
            payload = jwt.decode(
                token,
                jwt_secret,
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
        else:
            # Dev/test fallback when no secret is configured
            payload = jwt.decode(
                token,
                options={"verify_signature": False, "verify_aud": False},
            )
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload missing user identifier ('sub')",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        ensure_uuid(sub)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid UUID in token subject: {sub}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    return payload


def extract_token_from_request(request: Request) -> Optional[str]:
    """Extract Bearer JWT token from Authorization header, cookies, or session."""
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token:
            return token

    # Check common cookie names used for Supabase auth
    for cookie_key in ("sb_access_token", "sb-access-token", "access_token"):
        cookie_token = request.cookies.get(cookie_key)
        if cookie_token:
            return cookie_token

    # Check Starlette session
    session_token = request.session.get("access_token") if hasattr(request, "session") else None
    if session_token:
        return session_token

    return None


async def get_current_user_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Dict[str, Any]:
    """FastAPI dependency to extract and validate the authenticated user's token.

    Supports Bearer header credentials, request cookies, and existing server session.
    """
    # Check if already validated in request.state (e.g. by auth middleware)
    if hasattr(request.state, "user") and request.state.user:
        return request.state.user

    token = None
    if isinstance(credentials, HTTPAuthorizationCredentials) and credentials.credentials:
        token = credentials.credentials
    else:
        token = extract_token_from_request(request)

    if token:
        payload = verify_supabase_token(token)
        request.state.user = payload
        request.state.user_id = ensure_uuid(payload["sub"])
        return payload

    # Fallback for server-rendered web sessions when request.session has user_id
    if hasattr(request, "session") and request.session.get("user_id"):
        try:
            user_uuid = ensure_uuid(request.session["user_id"])
            user_data = {
                "sub": str(user_uuid),
                "email": request.session.get("email", ""),
                "user_metadata": {"username": request.session.get("username", "")},
                "role": "authenticated",
            }
            request.state.user = user_data
            request.state.user_id = user_uuid
            return user_data
        except (ValueError, TypeError):
            pass

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated. Bearer token or active session required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user_id(
    token_data: Dict[str, Any] = Depends(get_current_user_token),
) -> uuid.UUID:
    """FastAPI dependency to obtain the authenticated user's UUID."""
    return ensure_uuid(token_data["sub"])


async def get_optional_user_id(
    request: Request,
) -> Optional[uuid.UUID]:
    """FastAPI dependency to optionally obtain user's UUID if authenticated, else None."""
    try:
        token_data = await get_current_user_token(request, credentials=None)
        return ensure_uuid(token_data["sub"])
    except (HTTPException, Exception):
        return None
