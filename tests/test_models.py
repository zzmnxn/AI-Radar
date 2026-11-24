import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from radar.models import AnalysisRecord, Card, Claim, RawItem, SCHEMA_VERSION, SourceRef


def test_raw_item_round_trip():
    item = RawItem(
        item_id="item",
        source_id="official-news",
        source_class="official",
        url="https://example.org/article",
        canonical_url="https://example.org/article",
        title="Example",
        published_at="2026-11-16T07:00:00Z",
        source_updated_at="2026-11-17T07:00:00Z",
        first_seen_at="2026-11-18T07:00:00Z",
    )
    assert RawItem.model_validate_json(item.model_dump_json()) == item
    with pytest.raises(ValidationError, match="source_class"):
        RawItem.model_validate({**item.model_dump(), "source_class": "invalid"})


def test_card_schema():
    card = {
        "card_id": "card_example",
        "event_id": "event",
        "analysis_id": "analysis",
        "revision": 1,
        "title_ko": "제목",
        "category": "tool",
        "tags": [],
        "sources": [],
        "what": ["공개했다."],
        "why": ["활용할 수 있다."],
        "how": [],
        "availability": "beta",
        "access": {"type": "signup"},
        "verification_summary": {"level": "verified", "claims": []},
        "score": 80,
        "pool": "explore",
    }
    assert Card.model_validate(card).how == []
    with pytest.raises(ValidationError, match="what"):
        Card.model_validate({**card, "what": []})


def test_storage_contract_has_no_raw_body():
    assert "snippet" not in SourceRef.model_fields
    evidence = AnalysisRecord.model_json_schema()["$defs"]["EvidenceMeta"]
    assert evidence["additionalProperties"] is False
    assert set(evidence["properties"]) == {
        "url",
        "final_url",
        "http_status",
        "content_type",
        "fetched_at",
        "text_sha256",
        "chars",
        "truncated",
    }


def test_quote_limit():
    claim = dict(role="what", text="주장", evidence_url="https://example.org/post")
    assert Claim(**claim, evidence_quote="가" * 200).evidence_quote == "가" * 200
    with pytest.raises(ValidationError, match="evidence_quote"):
        Claim(**claim, evidence_quote="가" * 201)


def test_export_schema_cli(tmp_path):
    subprocess.run(
        [sys.executable, "-m", "radar", "export-schema", "--output", str(tmp_path)],
        check=True,
    )
    schema = json.loads((tmp_path / "manifest.schema.json").read_text())
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert (tmp_path / "card.schema.json").is_file()


def test_fact_only_card_renders_no_why():
    # Verify against the published card contract, including older snapshots.
    from pathlib import Path

    card = next(
        card
        for p in sorted(Path("data/briefings").glob("*.json"))
        for card in json.loads(p.read_text()).get("cards", [])
    )
    assert Card.model_validate({**card, "why": []}).why == []
