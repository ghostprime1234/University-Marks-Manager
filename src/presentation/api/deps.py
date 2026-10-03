"""Dependency helpers for API layer."""
from __future__ import annotations

from typing import Generator

from sqlmodel import Session

from src.infrastructure.db.engine import engine
from src.core.auth import (
    ensure_uuid,
    get_current_user_id,
    get_current_user_token,
    get_optional_user_id,
    verify_supabase_token,
)


def get_session() -> Generator[Session, None, None]:  # FastAPI dependency
    """Yield a database session for a single FastAPI request."""
    with Session(engine) as session:
        yield session


__all__ = [
    "get_session",
    "get_current_user_token",
    "get_current_user_id",
    "get_optional_user_id",
    "ensure_uuid",
    "verify_supabase_token",
]
