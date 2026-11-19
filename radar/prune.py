from calendar import monthrange
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from .models import AnalysisRecord, Contract, EventRecord
from .storage import write_json_atomic, write_jsonl_atomic

KST = ZoneInfo("Asia/Seoul")
RETENTION_DAYS = 120


class Shown(Contract):
    date: str
    event_id: str
    revision: int
    card_id: str


SHOWN = TypeAdapter(list[Shown])


def _prune_shards(directory, model, keep):
    for path in sorted(directory.glob("*.jsonl")):
        rows = [
            model.model_validate_json(line)
            for line in path.read_bytes().splitlines()
            if line.strip()
        ]
        retained = [row for row in rows if keep(row)]
        if not retained:
            path.unlink()
        elif len(rows) != len(retained):
            write_jsonl_atomic(path, (row.model_dump(mode="json") for row in retained))


def prune(data, day, *, protected_events=()):
    cutoff = day - timedelta(days=RETENTION_DAYS)
    active = set()

    def keep_event(event):
        keep = (
            event.event_id in protected_events
            or event.last_activity_at.astimezone(KST).date() >= cutoff
        )
        if keep:
            active.add(event.event_id)
        return keep

    _prune_shards(data / "state/events", EventRecord, keep_event)
    _prune_shards(
        data / "state/analyses", AnalysisRecord, lambda row: row.event_id in active
    )
    path = data / "state/shown.json"
    if path.is_file():
        rows = SHOWN.validate_json(path.read_bytes())
        retained = [row for row in rows if row.date >= cutoff.isoformat()]
        if retained != rows:
            write_json_atomic(path, SHOWN.dump_python(retained, mode="json"))

    year, month_offset = divmod(max(12, day.year * 12 + day.month - 7), 12)
    month = month_offset + 1
    oldest = date(year, month, min(day.day, monthrange(year, month)[1]))
    for path in (data / "state/trends").glob("????-??-??.json"):
        if date.fromisoformat(path.stem) < oldest:
            path.unlink()
