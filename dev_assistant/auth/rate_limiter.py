# Quick note: one-line comment added as requested.
"""Per-user daily rate limiting for dev_assistant APIs."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from dev_assistant.auth.api_key_auth import get_current_user
from dev_assistant.db.database import User, get_session


PLAN_LIMITS = {
    "free": {"requests_per_day": 5, "max_files_per_run": 5},
    "pro": {"requests_per_day": 100, "max_files_per_run": 50},
    "enterprise": {"requests_per_day": 1000, "max_files_per_run": 200},
}


async def check_rate_limit(user: User, db: AsyncSession) -> dict[str, int]:
    """Reset and increment the daily usage counter for a user."""

    today = date.today()
    if user.last_reset_date != today:
        user.api_calls_today = 0
        user.last_reset_date = today

    plan = PLAN_LIMITS.get(user.plan, PLAN_LIMITS["free"])
    limit = int(plan["requests_per_day"])
    if user.api_calls_today >= limit:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Daily limit reached. Upgrade to Pro.")

    user.api_calls_today += 1
    user.api_calls_total += 1
    await db.commit()

    reset_time = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    remaining = max(limit - user.api_calls_today, 0)
    return {
        "limit": limit,
        "remaining": remaining,
        "reset": int(reset_time.timestamp()),
    }


async def rate_limit_check(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> User:
    """Dependency that enforces rate limits and stores header metadata."""

    rate_state = await check_rate_limit(user, db)
    request.state.rate_limit = rate_state
    request.state.auth_user = user
    return user
