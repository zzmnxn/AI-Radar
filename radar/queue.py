from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, TypeAdapter

from .http import FetchError
from .models import Contract, Count, Revision

RETRY_WINDOW = timedelta(hours=72)
BACKOFF = (timedelta(minutes=15), timedelta(hours=1))
Failure = Literal[
    "budget", "api_error", "fetch_transient", "fetch_permanent", "schema_error"
]


class QueueEntry(Contract):
    revision: Revision
    status: Literal["queued", "deferred", "done", "failed", "expired"]
    first_queued_at: AwareDatetime
    reason: Failure | None = None
    attempts: Count = 0
    invalid_attempts: Count = 0
    next_attempt_after: AwareDatetime | None = None


QUEUE = TypeAdapter(dict[str, QueueEntry])


def transition(previous, revision, now, reason: Failure | None):
    base = (
        previous
        if previous and previous.revision == revision
        else QueueEntry(revision=revision, status="queued", first_queued_at=now)
    )
    if reason is None:
        changes = {"status": "done", "reason": None, "next_attempt_after": None}
    elif reason == "budget":
        changes = {"status": "deferred", "reason": reason, "next_attempt_after": None}
    else:
        attempts = base.attempts + 1
        invalid = base.invalid_attempts + (
            reason in {"fetch_permanent", "schema_error"}
        )
        terminal = attempts >= 3 or invalid >= 2
        changes = {
            "status": "failed" if terminal else "deferred",
            "reason": reason,
            "attempts": attempts,
            "invalid_attempts": invalid,
            "next_attempt_after": None if terminal else now + BACKOFF[attempts - 1],
        }
    return base.model_copy(update=changes)


def fetch_failure(exc: FetchError) -> Failure:
    if (
        exc.status in {408, 429}
        or (exc.status is not None and 500 <= exc.status < 600)
        or (exc.status is None and exc.kind in {"timeout", "dns_error", "http_error"})
    ):
        return "fetch_transient"
    return "fetch_permanent"
