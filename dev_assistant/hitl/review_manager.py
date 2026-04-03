# Quick note: one-line comment added as requested.
"""Human-in-the-loop review coordination for plan and file approval."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


ReviewAction = Literal["approve", "edit", "skip", "reject_all"]
ALLOWED_ACTIONS: set[str] = {"approve", "edit", "skip", "reject_all"}


@dataclass(slots=True)
class ReviewRequest:
    """A pending review request that blocks generation until resolved."""

    review_id: str
    review_type: str
    content: str
    file_path: str | None
    timestamp: float


@dataclass(slots=True)
class ReviewResult:
    """A user's decision for a specific review request."""

    review_id: str
    action: ReviewAction
    edited_content: str | None
    comment: str | None


class ReviewManager:
    """Stores pending reviews and coordinates pause/resume via asyncio events."""

    def __init__(self, timeout_seconds: int = 600, log_file: str = "dev_assistant_reviews.log") -> None:
        self.pending_reviews: dict[str, ReviewRequest] = {}
        self.review_results: dict[str, ReviewResult] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._timeout_seconds = timeout_seconds

        self._logger = logging.getLogger("dev_assistant.hitl.review_manager")
        if not self._logger.handlers:
            handler = logging.FileHandler(Path(log_file), encoding="utf-8")
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)
        self._logger.setLevel(logging.INFO)

    async def request_review(self, review_type: str, content: str, file_path: str | None = None) -> ReviewResult:
        """Create and await a review request, timing out to auto-approve."""

        review_id = str(uuid.uuid4())
        request = ReviewRequest(
            review_id=review_id,
            review_type=review_type,
            content=content,
            file_path=file_path,
            timestamp=time.time(),
        )
        self.pending_reviews[review_id] = request
        self._events[review_id] = asyncio.Event()

        try:
            await asyncio.wait_for(self._events[review_id].wait(), timeout=self._timeout_seconds)
        except asyncio.TimeoutError:
            self._logger.warning(
                "Review timed out; auto-approving review_id=%s review_type=%s file_path=%s",
                review_id,
                review_type,
                file_path,
            )
            self.submit_review(review_id=review_id, action="approve", comment="Auto-approved after timeout")

        result = self.review_results[review_id]
        self.pending_reviews.pop(review_id, None)
        self._events.pop(review_id, None)

        self._logger.info(
            "Review resolved %s",
            asdict(result),
        )
        return result

    def submit_review(
        self,
        review_id: str,
        action: str,
        edited_content: str | None = None,
        comment: str | None = None,
    ) -> None:
        """Submit a review result and resume any waiter for this review id."""

        if review_id not in self.pending_reviews:
            raise ValueError(f"Unknown review_id: {review_id}")
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"Invalid action: {action}")

        normalized_content = edited_content if action == "edit" else None
        result = ReviewResult(
            review_id=review_id,
            action=action,  # type: ignore[arg-type]
            edited_content=normalized_content,
            comment=comment,
        )
        self.review_results[review_id] = result

        event = self._events.get(review_id)
        if event is not None:
            event.set()

    def get_pending(self) -> list[ReviewRequest]:
        """Return all currently unresolved review requests."""

        return list(self.pending_reviews.values())
