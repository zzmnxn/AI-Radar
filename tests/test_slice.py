import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import HttpUrl

from radar.assemble import briefing_path
from radar.collect import CollectResult, RawItem
from radar.enrich import SourceDoc
from radar.models import Briefing, EvalResult, EvidenceMeta
from radar.slice import run_slice, validate_snapshot
from test_collectors import source

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
URL = "https://example.org/article"
QUOTE = "The update makes the cache size configurable."
WHY = "This helps operators keep memory use within limits."


def test_slice_publishes_only_verified_sentences_and_preserves_on_failure(tmp_path):
    item = RawItem(
        item_id="a",
        source_id="rss",
        source_class="official",
        title="Configurable NovaMesh cache",
        url=URL,
        canonical_url=URL,
        published_at=NOW,
        first_seen_at=NOW,
    )
    result = CollectResult(source("rss", "rss"), "complete", [item])
    doc = SourceDoc(
        QUOTE + " " + WHY,
        EvidenceMeta(
            url=URL,
            final_url=URL,
            http_status=200,
            content_type="text/plain",
            fetched_at=NOW,
            text_sha256="x",
            chars=len(QUOTE) + len(WHY) + 1,
            truncated=False,
        ),
    )
    evaluation = EvalResult(
        scores=dict(practical=4, impact=4, novelty=4, trust=4, interest=4),
        category="tool",
        tags=[],
        title_ko="캐시 크기 설정",
        what=["캐시 크기를 설정할 수 있다."],
        why=["메모리 사용량을 관리할 수 있다."],
        how=["출처에 없는 명령어를 실행한다."],
        availability="available",
        access={"type": "open"},
        claims=[
            dict(
                role=role,
                sentence_index=0,
                text=text,
                evidence_url=URL,
                evidence_quote=quote,
            )
            for role, text, quote in [
                ("what", "캐시 크기를 설정할 수 있다.", QUOTE),
                ("why", "메모리 사용량을 관리할 수 있다.", WHY),
            ]
        ],
    )
    other = item.model_copy(
        update={
            "item_id": "b",
            "url": HttpUrl("https://other.org/novamesh"),
            "source_id": "other",
            "title": "NovaMesh documentation",
            "published_at": NOW - timedelta(days=3),
            "canonical_url": HttpUrl("https://other.org/novamesh"),
        }
    )
    manifest = run_slice(
        [result, CollectResult(source("other", "rss"), "complete", [other])],
        NOW,
        tmp_path,
        fetch=lambda item, *, now: doc,
        evaluate=lambda item, doc: evaluation,
        analyzer_version="fixture",
        limit=1,
    )
    assert manifest.last_run.outcome == "ok"
    path = briefing_path(manifest.published.date)
    briefing = Briefing.model_validate_json((tmp_path / path).read_bytes())
    (card,) = briefing.cards
    assert (briefing.emerging[0].tag, briefing.emerging[0].kind) == (
        "novamesh",
        "discovery",
    )
    assert card.how == []
    assert card.availability == "unknown"
    assert card.access.type == "unknown"
    assert card.verification_summary.claims == evaluation.claims
    assert QUOTE in briefing.model_dump_json()

    failure = run_slice(
        [CollectResult(result.source, "failed")],
        NOW + timedelta(days=1),
        tmp_path,
        fetch=lambda item, *, now: doc,
        evaluate=lambda item, doc: evaluation,
        analyzer_version="fixture",
        limit=3,
    )
    assert failure.last_run.outcome == "failed"
    assert failure.published == manifest.published
    assert failure.last_success_at == manifest.last_success_at
    assert (tmp_path / path).is_file()

    changed = item.model_copy(
        update={"title": "Configurable cache size", "snippet": "New option"}
    )
    revised = run_slice(
        [CollectResult(result.source, "complete", [changed])],
        NOW + timedelta(days=2),
        tmp_path,
        fetch=lambda item, *, now: doc,
        evaluate=lambda item, doc: evaluation,
        analyzer_version="fixture",
        limit=3,
    )
    revised_path = briefing_path(revised.published.date)
    (revised_card,) = Briefing.model_validate_json(
        (tmp_path / revised_path).read_bytes()
    ).cards
    assert revised_card.event_id == card.event_id
    assert revised_card.revision == 2
    assert revised_card.analysis_id != card.analysis_id


def test_validate_snapshot_rejects_modified_data(tmp_path):
    path = tmp_path / "briefings" / "2026-11-18.json"
    path.parent.mkdir()
    path.write_text("{}")
    manifest = {
        "published": {
            "date": "2026-11-18",
            "run_id": "run",
            "generated_at": NOW.isoformat(),
            "files": {"briefings/2026-11-18.json": "0" * 64},
        },
        "last_run": {
            "run_id": "run",
            "date": "2026-11-18",
            "started_at": NOW.isoformat(),
            "finished_at": NOW.isoformat(),
            "outcome": "ok",
            "sources": {"active": 1, "complete": 1, "partial": 0, "failed": 0},
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_snapshot(tmp_path)
