from pathlib import Path

import pytest

from radar.models import CATEGORIES, EvalResult
from radar.prefilter import interest_categories, load_candidate_settings

INTERESTS = load_candidate_settings(Path("config")).interests

TITLES = {
    "coding-tools": "Claude Code now reviews pull requests automatically",
    "agent-extensions": "How to write Agent Skills and subagents for your team",
    "mcp-tools": "A Model Context Protocol server for Postgres",
    "agent-design": "Building a verification loop inside an agent harness",
    "context-memory": "Context engineering: keeping long sessions under the window",
    "genui": "Generative UI: letting the model render components",
    "dev-workflow": "How we ship features with spec-driven development",
    "models-api": "GPT-6.2 arrives with a cheaper Responses API tier",
    "local-models": "Running Qwen locally with llama.cpp on a laptop",
    "ai-services": "Introducing a new AI meeting notes app",
    "agent-workspace": "Orca runs parallel agents in separate worktrees",
    "usage-patterns": "My LLM wiki: connecting Obsidian to Claude",
}


def test_twelve_categories_with_fallback():
    assert len(CATEGORIES) == 13 and CATEGORIES[-1] == "기타"
    assert [c.id for c in INTERESTS.categories] == list(TITLES)
    assert [c.name for c in INTERESTS.categories] == list(CATEGORIES[:-1])


@pytest.mark.parametrize("expected,title", TITLES.items())
def test_representative_title_matches_its_category(expected, title):
    assert expected in interest_categories(title, INTERESTS)


def test_unrelated_title_matches_nothing():
    assert interest_categories("Football scores for the weekend", INTERESTS) == []


def test_unknown_category_becomes_fallback():
    payload = dict(
        scores=dict(practical=4, impact=4, novelty=4, trust=4, interest=4),
        tags=[],
        title_ko="제목",
        what=["바뀌었다."],
        why=["쓸 수 있다."],
        how=[],
        availability="available",
        access={"type": "open"},
        claims=[],
    )
    payload["category"] = "tool"
    assert EvalResult.model_validate(payload).category == "기타"
    payload["category"] = "MCP·도구 연결"
    assert EvalResult.model_validate(payload).category == "MCP·도구 연결"


def test_not_worth_briefing_is_a_rejected_analysis_without_a_card():
    from datetime import datetime, timezone

    from radar.analysis import analyze
    from radar.models import Usage, Verification
    from radar.verify import VerifiedEvaluation

    payload = dict(
        scores=dict(practical=2, impact=2, novelty=1, trust=4, interest=2),
        worth_briefing=False,
        skip_reason="특정 분야 응용 연구",
        category="기타",
        tags=[],
        title_ko="제목",
        what=["바뀌었다."],
        why=["쓸 수 있다."],
        how=[],
        availability="available",
        access={"type": "open"},
        claims=[],
    )

    class Event:
        event_id = "e"
        revision = 1

    checked = VerifiedEvaluation(
        EvalResult.model_validate(payload),
        Verification(
            link_ok=True,
            level="verified",
            claims=[],
            quote_found_rate=1,
            supported_rate=1,
        ),
        [],
        None,
    )
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    record = analyze(Event, "v", checked, Usage(input_tokens=0, output_tokens=0), now)
    assert (record.verdict, record.rejection_reason) == (
        "rejected",
        "not_worth_briefing",
    )
    stored = {
        k: v for k, v in payload.items() if k not in {"worth_briefing", "skip_reason"}
    }
    assert EvalResult.model_validate(stored).worth_briefing is True
