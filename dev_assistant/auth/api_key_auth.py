# Quick note: one-line comment added as requested.
"""API key authentication helpers for dev_assistant."""

from __future__ import annotations

import hashlib
import os
import secrets
import string
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from dev_assistant.db.database import ApiKey, User, get_session


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _random_key() -> str:
    alphabet = string.ascii_letters + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(32))
    return f"dask_{suffix}"


def generate_api_key() -> tuple[str, str]:
    """Return a raw API key and its bcrypt hash."""

    raw_key = _random_key()
    hashed_key = pwd_context.hash(raw_key)
    return raw_key, hashed_key


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return pwd_context.verify(password, hashed_password)


async def create_api_key_for_user(session: AsyncSession, user: User, name: str = "default", scopes: list[str] | None = None) -> tuple[ApiKey, str]:
    """Create an API key record and return it with the raw key."""

    raw_key, secret_hash = generate_api_key()
    key_hash = _sha256_hex(raw_key)
    api_key = ApiKey(
        user_id=user.id,
        key_hash=key_hash,
        secret_hash=secret_hash,
        name=name,
        scopes=scopes or ["generate", "models", "usage"],
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return api_key, raw_key


async def verify_api_key(key: str, db: AsyncSession) -> User | None:
    """Return the user for a valid API key or None."""

    if not key.startswith("dask_"):
        return None

    lookup_hash = _sha256_hex(key)
    result = await db.execute(select(ApiKey).where(ApiKey.key_hash == lookup_hash, ApiKey.is_active.is_(True)))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        return None

    # Verify the raw key matches the stored bcrypt hash for defense in depth.
    if not pwd_context.verify(key, api_key.secret_hash):
        return None

    api_key.last_used_at = datetime.now(timezone.utc)
    await db.commit()

    user_result = await db.execute(select(User).where(User.id == api_key.user_id, User.is_active.is_(True)))
    return user_result.scalar_one_or_none()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)] = None,
    db: AsyncSession = Depends(get_session),
) -> User:
    """FastAPI dependency that authenticates Bearer API keys."""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer API key")

    user = await verify_api_key(credentials.credentials, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return user
