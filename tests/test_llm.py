import json
from datetime import datetime, timezone
from decimal import Decimal

from radar.demo import demo_inputs
from radar.llm import ModelConfig, OpenAIEvaluator
from radar.models import RawMetrics

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)


def test_response_contract_and_persistent_budget(tmp_path):
    results, fetch, evaluate = demo_inputs(NOW)
    item = results[0].items[0]
    doc = fetch(item, now=NOW)
    evaluation = evaluate(item, doc)
    calls = []

    def post(payload, key):
        calls.append(payload)
        response = (
            json.dumps({"verdicts": ["supported"]})
            if payload["text"]["format"]["name"] == "radar_judgment"
            else evaluation.model_dump_json()
        )
        return {
            "status": "completed",
            "usage": {"input_tokens": 500, "output_tokens": 200},
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": response}],
                }
            ],
        }

    config = ModelConfig(
        model="configured-model",
        daily_budget_usd=Decimal("1"),
        max_calls=2,
        input_usd_per_million=Decimal("1"),
        output_usd_per_million=Decimal("1"),
    )
    adapter = OpenAIEvaluator(config, "fake-key", tmp_path, NOW, post=post)
    assert adapter(item, doc) == evaluation
    schema = calls[0]["text"]["format"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert calls[0]["store"] is False
    assert "fake-key" not in str(calls[0])
    assert adapter.judge(
        [{"role": "what", "claim": "x", "quote": "x", "context": "x"}]
    ) == ["supported"]
    assert adapter.last_usage.output_tokens == 400
    state = json.loads(adapter.path.read_text())
    assert state["calls"] == 2
    assert Decimal(state["estimated_usd"]) == Decimal("0.0014")
    retry = OpenAIEvaluator(config, "fake-key", tmp_path, NOW, post=post)
    assert retry(item, doc) is None
    assert len(calls) == 2


def test_zero_budget_skips_transport(tmp_path):
    results, fetch, _ = demo_inputs(NOW)
    item = results[0].items[0]
    config = ModelConfig(
        model="configured-model",
        daily_budget_usd=Decimal("0"),
        input_usd_per_million=Decimal("1"),
        output_usd_per_million=Decimal("1"),
    )

    def unexpected(*args):
        raise AssertionError("zero-budget request")

    assert (
        OpenAIEvaluator(config, "fake-key", tmp_path, NOW, post=unexpected)(
            item, fetch(item, now=NOW)
        )
        is None
    )


def _adapter(tmp_path, post):
    config = ModelConfig(
        model="configured-model",
        daily_budget_usd=Decimal("1"),
        input_usd_per_million=Decimal("1"),
        output_usd_per_million=Decimal("1"),
    )
    return OpenAIEvaluator(config, "fake-key", tmp_path, NOW, post=post)


def _respond(text):
    return lambda payload, key: {
        "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 10},
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ],
    }


def test_validation_failure_records_field_paths_without_source_text(tmp_path):
    results, fetch, evaluate = demo_inputs(NOW)
    item = results[0].items[0]
    doc = fetch(item, now=NOW)
    bad = evaluate(item, doc).model_dump(mode="json")
    bad["scores"]["practical"] = 7
    bad["what"] = ["하나", "둘", "셋"]
    bad["claims"][0]["evidence_quote"] = ""
    bad["title_ko"] = "SECRET-SOURCE"
    adapter = _adapter(tmp_path, _respond(json.dumps(bad, ensure_ascii=False)))
    assert adapter(item, doc) is None
    assert adapter.last_failure == "schema_error"
    error = adapter.last_error
    assert "scores.practical:less_than_equal" in error
    assert "what:too_long" in error
    assert "claims.0.evidence_quote:string_too_short" in error
    assert "SECRET-SOURCE" not in error


def test_auth_error_is_not_schema_error(tmp_path):
    from radar.llm import ModelError

    def unauthorized(payload, key):
        raise ModelError("HTTP 401", "api_error")

    results, fetch, _ = demo_inputs(NOW)
    item = results[0].items[0]
    adapter = _adapter(tmp_path, unauthorized)
    assert adapter(item, fetch(item, now=NOW)) is None
    assert adapter.last_failure == "api_error"
    assert adapter.last_error == "HTTP 401"


def test_http_status_classification():
    from radar.llm import _status_failure

    assert _status_failure(401) == "api_error"
    assert _status_failure(403) == "api_error"
    assert _status_failure(429) == "api_error"
    assert _status_failure(500) == "api_error"
    assert _status_failure(400) == "schema_error"


def test_schema_keeps_supported_limits_and_describes_the_rest(tmp_path):
    adapter = _adapter(tmp_path, _respond("{}"))
    schema = adapter.schema
    defs = schema.get("$defs", {})
    scores = defs["Scores"]["properties"]
    assert scores["practical"]["minimum"] == 0
    assert scores["practical"]["maximum"] == 5
    assert schema["properties"]["what"]["maxItems"] == 2
    assert schema["properties"]["why"]["maxItems"] == 2
    assert schema["properties"]["how"]["maxItems"] == 3
    quote = defs["Claim"]["properties"]["evidence_quote"]
    assert "maxLength" not in quote
    assert "200" in quote["description"]


def test_overlong_quote_is_trimmed_to_a_verbatim_prefix(tmp_path):
    results, fetch, evaluate = demo_inputs(NOW)
    item = results[0].items[0]
    doc = fetch(item, now=NOW)
    answer = evaluate(item, doc).model_dump(mode="json")
    long_quote = "word " * 60
    answer["claims"][0]["evidence_quote"] = long_quote
    adapter = _adapter(tmp_path, _respond(json.dumps(answer, ensure_ascii=False)))
    result = adapter(item, doc)
    quote = result.claims[0].evidence_quote
    assert len(quote) <= 200
    assert long_quote.startswith(quote)
    assert not quote.endswith(" ")


def test_candidate_budget_fits_target_at_default_budget(tmp_path):
    config = ModelConfig(
        model="configured-model",
        daily_budget_usd=Decimal("0.25"),
        input_usd_per_million=Decimal("0.75"),
        output_usd_per_million=Decimal("4.50"),
    )
    adapter = OpenAIEvaluator(config, "fake-key", tmp_path, NOW, post=None)
    remaining, estimate, headline, _ = adapter.candidate_budget()
    # Observed: about 2,000 input and 800 output tokens per call, two calls per item.
    observed = adapter._cost(2 * 2000, 2 * 800)
    assert estimate >= observed
    assert (remaining - headline) // estimate >= 15


def test_evaluation_input_carries_source_metadata_and_exit_fields(tmp_path):
    results, fetch, evaluate = demo_inputs(NOW)
    item = results[0].items[0].model_copy(update={"metrics": RawMetrics(hn_points=120)})
    doc = fetch(item, now=NOW)
    sent = []
    body = evaluate(item, doc).model_dump(mode="json")
    body.update(worth_briefing=False, skip_reason="특정 산업 연구")

    def post(payload, key):
        sent.append(payload)
        return _respond(json.dumps(body))(payload, key)

    result = _adapter(tmp_path, post)(item, doc)
    assert result.worth_briefing is False and result.skip_reason == "특정 산업 연구"
    message = json.loads(sent[0]["input"])
    assert message["source"] == item.source_id
    assert message["source_class"] == item.source_class
    assert message["hn_points"] == 120
    schema = sent[0]["text"]["format"]["schema"]
    assert {"worth_briefing", "skip_reason"} <= set(schema["required"])


def test_full_size_article_is_not_blocked_by_the_input_limit(tmp_path):
    from radar.llm import MAX_ARTICLE_BYTES

    results, fetch, evaluate = demo_inputs(NOW)
    item = results[0].items[0]
    doc = fetch(item, now=NOW)
    doc = type(doc)("가" * (MAX_ARTICLE_BYTES // 3), doc.evidence)
    body = evaluate(item, fetch(item, now=NOW)).model_dump_json()
    sent = []

    def post(payload, key):
        sent.append(payload)
        return _respond(body)(payload, key)

    adapter = _adapter(tmp_path, post)
    adapter(item, doc)
    assert sent, (adapter.last_failure, adapter.last_error)


def test_concepts_extracts_names_for_numbered_titles_in_one_call(tmp_path):
    sent = []
    rows = [
        ("e1", "Context engineering for agents"),
        ("e2", "Weekly news"),
        ("e3", "Context engineering in practice"),
    ]
    body = json.dumps(
        {"concepts": [{"name": "Context Engineering", "items": [1, 3, 99]}]}
    )

    def post(payload, key):
        sent.append(payload)
        return _respond(body)(payload, key)

    found = _adapter(tmp_path, post).concepts(rows)
    assert len(sent) == 1
    assert "1. Context engineering for agents" in sent[0]["input"]
    assert found == {"e1": ["context engineering"], "e3": ["context engineering"]}


def test_concepts_failure_returns_nothing(tmp_path):
    def post(payload, key):
        return _respond("not json")(payload, key)

    assert _adapter(tmp_path, post).concepts([("e1", "A title")]) == {}


def test_concepts_split_a_busy_day_into_capped_batches(tmp_path):
    from radar.llm import CONCEPT_BATCHES, CONCEPT_TITLES

    sent = []
    rows = [(f"e{i}", f"Title {i}") for i in range(CONCEPT_TITLES * 6)]
    body = json.dumps({"concepts": [{"name": "Agent Memory", "items": [1]}]})

    def post(payload, key):
        sent.append(payload)
        return _respond(body)(payload, key)

    found = _adapter(tmp_path, post).concepts(rows)
    assert len(sent) == CONCEPT_BATCHES
    assert f"1. Title {CONCEPT_TITLES}" in sent[1]["input"]
    assert set(found) == {f"e{CONCEPT_TITLES * n}" for n in range(CONCEPT_BATCHES)}


def test_headline_is_capped_at_30_characters():
    from pathlib import Path

    import pytest
    from pydantic import ValidationError

    from radar.llm import Headline

    assert Headline(headline_ko="가" * 30).headline_ko == "가" * 30
    with pytest.raises(ValidationError):
        Headline(headline_ko="가" * 31)
    assert "30자" in Path("prompts/headline.md").read_text(encoding="utf-8")


def test_screen_returns_reasons_and_none_on_failure(tmp_path):
    rows = [("e1", "Climate harm of AI"), ("e2", "Agent harness"), ("e3", "Other")]
    body = json.dumps(
        {"drop": [{"number": 1, "reason_ko": "담론"}, {"number": 9, "reason_ko": "x"}]}
    )
    found = _adapter(tmp_path, _respond(body)).screen(rows)
    assert found == {"e1": "담론"}

    def bad(payload, key):
        return _respond("not json")(payload, key)

    assert _adapter(tmp_path, bad).screen(rows) is None
