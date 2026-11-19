import json
from datetime import datetime, timezone
from decimal import Decimal

from radar.demo import demo_inputs
from radar.llm import ModelConfig, OpenAIEvaluator

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
