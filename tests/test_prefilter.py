from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from test_collectors import NOW, source
from test_dedupe import item

from radar.assemble import briefing_path
from radar.deduplicate import group, load_merge_config


def sample(
    count, *, age=0, label="sample", title="OpenAI model update", kind="official"
):
    cfg = load_merge_config(Path("config"))
    items = [
        item(
            f"{label}{i}",
            f"https://example.org/{label}/{i}",
            title=title,
            kind=kind,
            published_at=NOW - timedelta(hours=age),
        )
        for i in range(count)
    ]
    sources = {
        entry.source_id: source(
            entry.source_id, "hackernews" if kind == "aggregator" else "rss"
        )
        for entry in items
    }
    return group(items, [], cfg, now=NOW).new, sources


def select(events, sources, budget, **kwargs):
    from radar.prefilter import build_candidates, load_candidate_settings

    return build_candidates(
        events,
        sources,
        NOW,
        load_candidate_settings(Path("config")),
        budget_left=Decimal(budget),
        estimate=Decimal("1"),
        **kwargs,
    )


def test_budget_sizes_and_leftover_pool_slots():
    events, sources = sample(24)
    assert len(select(events, sources, "20").selected) == 15
    assert len(select(events, sources, "5").selected) == 5
    assert len(select(events[:5], sources, "20").selected) == 5


def test_budget_zero_defers_at_most_target():
    events, sources = sample(24)
    plan = select(events, sources, "0")
    assert not plan.selected
    assert len(plan.deferred) == 15


def test_pool_mix_and_no_duplicate_assignment():
    interest, a = sample(10, label="interest")
    impact, b = sample(10, label="impact", title="Anthropic launches new Reasoning API")
    explore, c = sample(
        10, label="explore", title="Anthropic releases new benchmark", kind="aggregator"
    )
    plan = select(interest + impact + explore + interest, a | b | c, "30")
    assert len({row.event_id for row in plan.selected}) == 15
    assert Counter(row.pool for row in plan.selected) == {
        "interest": 6,
        "impact": 5,
        "explore": 4,
    }


def test_deferred_bypasses_48h_with_slot_cap():
    from radar.prefilter import QueueEntry

    old, a = sample(5, age=60, label="old")
    fresh, b = sample(10, label="fresh")
    queued = {
        event.event_id: QueueEntry(
            revision=event.revision, status="deferred", first_queued_at=NOW
        )
        for event in old[:4]
    }
    plan = select(old + fresh, a | b, "9", queue=queued)
    selected_ids = [row.event_id for row in plan.selected]
    assert len(selected_ids) == 9
    assert set(selected_ids[:3]) <= set(queued)
    assert len(set(selected_ids) & set(queued)) == 3
    assert old[-1].event_id not in selected_ids
    assert len(selected_ids) == len(set(selected_ids))


def test_shown_analyzed_and_open_non_ai_are_excluded():
    events, sources = sample(3)
    irrelevant, extra = sample(
        2, label="sports", title="Show HN: Football scores", kind="aggregator"
    )
    events[1].analysis_status = "done"
    selected = select(
        events + irrelevant,
        sources | extra,
        "10",
        shown={(events[0].event_id, events[0].revision)},
    ).selected
    assert [candidate.event_id for candidate in selected] == [events[2].event_id]


def test_growth_enriches_at_most_twenty_repositories():
    cfg = load_merge_config(Path("config"))
    entries = [
        item(
            f"repo{i}",
            f"https://github.com/org/repo{i}",
            title="OpenAI inference repository",
            kind="community",
        )
        for i in range(24)
    ]
    events = group(entries, [], cfg, now=NOW).new
    sources = {entry.source_id: source(entry.source_id, "rss") for entry in entries}
    calls = []

    def growth(repo):
        calls.append(repo)
        return 7 if repo == "org/repo23" else None

    plan = select(
        events,
        sources,
        "30",
        growth=growth,
        current_stars={"org/repo23": 100},
    )
    assert len(calls) == 20
    assert calls[0] == "org/repo23"
    assert [
        reason
        for row in plan.selected
        for reason in row.reasons
        if reason.startswith("star_growth")
    ] == ["star_growth_7d:7"]


def test_zero_budget_run_writes_queue_and_keeps_transport_idle(tmp_path):
    from radar.demo import demo_inputs
    from radar.llm import ModelConfig, OpenAIEvaluator
    from radar.prefilter import QUEUE, load_candidate_settings
    from radar.slice import run_slice

    results, _, _ = demo_inputs(NOW)
    results[0].items[0].title = "OpenAI provides an AI model"
    model_config = ModelConfig(
        model="fixture",
        daily_budget_usd=0,
        input_usd_per_million=1,
        output_usd_per_million=1,
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("zero-budget run fetched an article or called the model")

    budget = OpenAIEvaluator(model_config, "fixture", tmp_path, NOW, post=unexpected)
    manifest = run_slice(
        results,
        NOW,
        tmp_path,
        evaluate=budget,
        analyzer_version=budget.version,
        fetch=unexpected,
        limit=25,
        candidate_settings=load_candidate_settings(Path("config")),
    )
    assert manifest.last_run.failed_stage == "budget"
    (entry,) = QUEUE.validate_json(
        (tmp_path / "state/analysis_queue.json").read_bytes()
    ).values()
    assert (entry.status, entry.reason) == ("deferred", "budget")
    assert not (tmp_path / "state/llm-budget.json").exists()


def test_deferred_revisions_are_replaced_and_expired(tmp_path):
    from radar.prefilter import QUEUE, QueueEntry
    from radar.slice import _recent_queue

    events, _ = sample(2, age=75)
    revised, expired = events
    revised.revision = 2
    queue_path = tmp_path / "analysis_queue.json"
    queue_path.write_bytes(
        QUEUE.dump_json(
            {
                event.event_id: QueueEntry(
                    revision=1,
                    status="deferred",
                    first_queued_at=NOW - timedelta(days=2),
                )
                for event in events
            }
        )
    )
    queue = _recent_queue(queue_path, events, NOW)
    assert queue[expired.event_id].status == "expired"
    assert queue[revised.event_id].revision == 2


def test_repeated_zero_budget_does_not_grow_the_waiting_list():
    from radar.prefilter import QueueEntry

    events, sources = sample(24)
    plan = select(events, sources, "0")
    queued = {
        candidate.event_id: QueueEntry(
            revision=candidate.revision, status="deferred", first_queued_at=NOW
        )
        for candidate in plan.deferred
    }
    second = select(events, sources, "0", queue=queued)
    assert not second.selected
    assert not second.deferred


def test_run_retries_a_sixty_hour_event_after_budget_recovers(tmp_path):
    import json

    from pydantic import HttpUrl

    from radar.demo import demo_inputs
    from radar.http import FetchError
    from radar.llm import ModelConfig, OpenAIEvaluator
    from radar.models import Briefing
    from radar.prefilter import QUEUE, load_candidate_settings
    from radar.slice import run_slice

    results, demo_fetch, demo_evaluate = demo_inputs(NOW)
    old = results[0].items[0]
    old.title = "OpenAI provides an AI model"
    evaluation = demo_evaluate(old, demo_fetch(old, now=NOW))
    settings = load_candidate_settings(Path("config"))
    transport = []

    def post(payload, key):
        response = evaluation.model_dump_json()
        if payload["text"]["format"]["name"] == "radar_judgment":
            response = json.dumps({"verdicts": ["supported", "supported"]})
        elif payload["text"]["format"]["name"] == "radar_headline":
            response = json.dumps({"headline_ko": "문서 검색 개선"})
        else:
            transport.append(json.loads(payload["input"])["title"])
        return {
            "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 100},
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": response}],
                }
            ],
        }

    def fetch(entry, *, now):
        if entry.item_id != old.item_id:
            raise FetchError("fixture")
        return demo_fetch(entry, now=now)

    config = ModelConfig(
        model="fixture",
        daily_budget_usd=0,
        input_usd_per_million=1,
        output_usd_per_million=1,
    )
    first = OpenAIEvaluator(config, "fixture", tmp_path, NOW, post=post)
    run_slice(
        results,
        NOW,
        tmp_path,
        evaluate=first,
        analyzer_version=first.version,
        fetch=fetch,
        limit=25,
        candidate_settings=settings,
        link_check=lambda url: True,
    )
    assert not transport

    retry_at = NOW + timedelta(hours=60)
    results[0].items = [
        old.model_copy(
            update={
                "item_id": f"fresh-{i}",
                "url": HttpUrl(f"https://example.org/fresh-{i}"),
                "canonical_url": HttpUrl(f"https://example.org/fresh-{i}"),
                "published_at": retry_at,
                "first_seen_at": retry_at,
            }
        )
        for i in range(2)
    ]
    funded = config.model_copy(update={"daily_budget_usd": Decimal(1)})
    second = OpenAIEvaluator(funded, "fixture", tmp_path, retry_at, post=post)
    manifest = run_slice(
        results,
        retry_at,
        tmp_path,
        evaluate=second,
        analyzer_version=second.version,
        fetch=fetch,
        limit=25,
        candidate_settings=settings,
        link_check=lambda url: True,
    )
    assert transport == [old.title]
    path = briefing_path(manifest.published.date)
    (card,) = Briefing.model_validate_json((tmp_path / path).read_bytes()).cards
    assert card.pool == "interest"
    queue = QUEUE.validate_json((tmp_path / "state/analysis_queue.json").read_bytes())
    assert queue[card.event_id].status == "done"
