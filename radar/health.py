from datetime import timedelta
from fcntl import LOCK_EX, flock
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, TypeAdapter

from .collect import CollectResult, Completeness, collect_source
from .models import Contract, Count
from .storage import write_json_atomic


class Gap(Contract):
    start: AwareDatetime = Field(alias="from")
    to: AwareDatetime
    reason: str
    status: Literal["open", "recovered", "unrecoverable", "lookback_capped"] = "open"


class SourceHealth(Contract):
    source_id: str
    last_attempt_at: AwareDatetime | None = None
    last_success_at: AwareDatetime | None = None
    last_complete_at: AwareDatetime | None = None
    gaps: list[Gap] = Field(default_factory=list)
    consecutive_failures: Count = 0
    last_item_at: AwareDatetime | None = None
    last_completeness: Completeness | None = None
    health: Literal["healthy", "degraded", "failing", "stale"] = "healthy"


STATE = TypeAdapter(dict[str, SourceHealth])
LOOKBACK = timedelta(days=7)
OVERLAP = timedelta(days=1)
INITIAL = timedelta(days=3)


def collect_with_health(
    sources, now, path: Path, *, fetch=None, dry_run=False, before_save=None
):
    def execute():
        state = STATE.validate_json(path.read_bytes()) if path.exists() else {}
        results = []
        for source in sources:
            row = state.setdefault(source.id, SourceHealth(source_id=source.id))
            if row.last_attempt_at and row.last_attempt_at > now:
                raise ValueError("Collection time predates the saved source state")
            results.append(_collect_source(source, now, row, fetch))
        if not dry_run:
            if before_save:
                before_save(results)
            write_json_atomic(
                path, STATE.dump_python(state, mode="json", by_alias=True)
            )
        return results

    if dry_run:
        return execute()
    staging = path.parent / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    with (staging / "collect.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        return execute()


def _collect_source(source, now, row, fetch):
    floor = now - LOOKBACK
    since = (
        max(floor, row.last_complete_at - OVERLAP)
        if row.last_complete_at
        else now - INITIAL
    )
    results = []
    for gap in sorted(row.gaps, key=lambda entry: entry.start):
        if gap.status != "open":
            continue
        if gap.to < floor:
            gap.status = "lookback_capped"
        elif source.supports_range:
            retry = collect_source(
                source, now, since=max(floor, gap.start), until=gap.to, fetch=fetch
            )
            results.append(retry)
            # Every part of this interval was attempted above, even if the gap remains open.
            since = max(since, gap.to)
            if retry.completeness == "complete":
                gap.status = "lookback_capped" if gap.start < floor else "recovered"
            if "rate_limited" in retry.partial_reasons:
                break

    if results and "rate_limited" in results[-1].partial_reasons:
        current = CollectResult(source, "partial", partial_reasons=["rate_limited"])
    else:
        current = collect_source(source, now, since=since, fetch=fetch)
    results.append(current)
    if current.completeness != "complete":
        row.gaps.append(
            Gap(
                **{"from": since},
                to=now,
                reason=",".join(current.partial_reasons) or current.error or "failed",
            )
        )
    elif not source.supports_range:
        for gap in row.gaps:
            if gap.status == "open":
                gap.status = "unrecoverable"

    open_gaps = [gap for gap in row.gaps if gap.status == "open"]
    if len(open_gaps) > 5:
        ordered = sorted(open_gaps, key=lambda gap: gap.start)
        oldest = ordered[:-4]
        open_gaps = ordered[-5:]
        open_gaps[0].start = oldest[0].start
        open_gaps[0].to = max(gap.to for gap in oldest)
    closed_gaps = [gap for gap in row.gaps if gap.status != "open"]
    retained_closed = max(0, 5 - len(open_gaps))
    row.gaps = closed_gaps[len(closed_gaps) - retained_closed :] + open_gaps

    items = {item.item_id: item for result in results for item in result.items}
    reasons = {reason for result in results for reason in result.partial_reasons}
    completeness = current.completeness
    if completeness != "failed" and open_gaps:
        completeness = "partial"
        reasons.add("unresolved_gap")
    if completeness == "failed" and items:
        completeness = "partial"
        reasons.add("incomplete_results")

    row.last_attempt_at = now
    row.last_completeness = completeness
    if current.completeness == "failed":
        row.consecutive_failures += 1
    elif "rate_limited" not in current.partial_reasons or any(
        result.items for result in results
    ):
        row.consecutive_failures = 0
        row.last_success_at = now
    if completeness == "complete":
        row.last_complete_at = now
    dates = [result.last_item_at for result in results if result.last_item_at]
    if row.last_item_at:
        dates.append(row.last_item_at)
    row.last_item_at = max(dates, default=None)
    if row.consecutive_failures >= 3:
        row.health = "failing"
    elif completeness != "complete":
        row.health = "degraded"
    elif row.last_item_at is None or now - row.last_item_at >= timedelta(
        days=3 * source.expected_cadence_days
    ):
        row.health = "stale"
    else:
        row.health = "healthy"

    return CollectResult(
        source,
        completeness,
        list(items.values()),
        error=current.error,
        partial_reasons=sorted(reasons),
        last_item_at=row.last_item_at,
        requests=sum(result.requests for result in results),
        elapsed_ms=sum(result.elapsed_ms for result in results),
    )
