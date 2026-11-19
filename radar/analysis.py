from datetime import timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from .models import AnalysisRecord
from .storage import upsert_jsonl

KST = ZoneInfo("Asia/Seoul")
WEIGHTS = {"practical": 30, "impact": 25, "novelty": 20, "trust": 15, "interest": 10}


def analysis_id(event, version):
    return sha256(f"{event.event_id}|{event.revision}|{version}".encode()).hexdigest()


def weighted_total(scores):
    return sum(getattr(scores, key) * weight for key, weight in WEIGHTS.items()) / 5


def analyze(event, version, checked, usage, now):
    return AnalysisRecord(
        analysis_id=analysis_id(event, version),
        event_id=event.event_id,
        revision=event.revision,
        analyzer_version=version,
        evidence=[doc.evidence for doc in checked.docs],
        eval=checked.evaluation,
        verification=checked.verification,
        verdict=(
            "rejected"
            if checked.rejection_reason
            else "partial"
            if checked.verification.level == "partial"
            else "accepted"
        ),
        rejection_reason=checked.rejection_reason,
        usage=usage,
        created_at=now,
    )


def save_analysis(data, record):
    month = record.created_at.astimezone(KST).strftime("%Y-%m")
    upsert_jsonl(
        data / "state/analyses" / f"{month}.jsonl",
        [record.model_dump(mode="json")],
        "analysis_id",
    )


def recent_analyses(data, now, events, version):
    wanted = {analysis_id(event, version) for event in events}
    months = {
        (now.astimezone(KST) - timedelta(days=offset)).strftime("%Y-%m")
        for offset in (0, 3)
    }
    records = {}
    for month in sorted(months):
        path = data / "state/analyses" / f"{month}.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = AnalysisRecord.model_validate_json(line)
                    if record.analysis_id in wanted:
                        records[record.event_id] = record
    return records
