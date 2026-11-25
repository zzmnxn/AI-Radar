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
    for event in explore:
        event.concept_tags = ["benchmark harness"]
    plan = select(
        interest + impact + explore + interest,
        a | b | c,
        "30",
        emerging_terms=["benchmark harness"],
    )
    assert len({row.event_id for row in plan.selected}) == 15
    assert Counter(row.pool for row in plan.selected) == {
        "interest": 6,
        "impact": 5,
        "explore": 4,
    }
    explored = [row for row in plan.selected if row.pool == "explore"]
    assert all("emerging:benchmark harness" in row.reasons for row in explored)


def test_explore_needs_a_new_trend_concept_or_a_strong_prescore():
    explore, c = sample(
        3, label="explore", title="Anthropic releases new benchmark", kind="aggregator"
    )
    assert select(explore, c, "30").selected == []


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
    old.title = "OpenAI provides a new model in the Responses API"
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


def test_hn_discussion_without_link_is_not_a_candidate():
    cfg = load_merge_config(Path("config"))
    hn = "https://news.ycombinator.com/item?id="
    rows = [
        ("1", "OpenAI researchers resign after board fight", None),
        ("2", "Show HN: OpenAI agent that edits spreadsheets", None),
        ("3", "Launch HN: Claude code review bot for teams", None),
        ("4", "OpenAI ships GPT audio streaming API", "https://example.org/gpt"),
    ]
    items = [
        item(
            f"hn{id}",
            f"{hn}{id}",
            title=title,
            org="hn",
            kind="aggregator",
            link_target_url=target,
            published_at=NOW,
        )
        for id, title, target in rows
    ]
    events = group(items, [], cfg, now=NOW).new
    selected = select(events, {"hn": source("hn", "hackernews")}, "10").selected
    titles = {
        event.title
        for event in events
        if event.event_id in {candidate.event_id for candidate in selected}
    }
    assert titles == {title for _, title, _ in rows[1:]}


def _titled(rows):
    cfg = load_merge_config(Path("config"))
    items = [
        item(
            f"t{i}",
            f"https://example.org/t/{i}",
            title=title,
            org=org,
            published_at=NOW,
        )
        for i, (title, org) in enumerate(rows)
    ]
    events = group(items, [], cfg, now=NOW).new
    sources = {org: source(org, "rss") for _, org in rows}
    return events, sources


def test_off_topic_news_is_excluded_before_evaluation_with_reasons():
    rows = [
        ("Three senior researchers resign from OpenAI", "a"),
        ("Anthropic raises funding at a higher valuation", "b"),
        ("OpenAI partners with a bank for a webinar series", "c"),
        ("Claude tops the LLM leaderboard ranking", "d"),
        ("OpenAI sued over GPT training data in court", "e"),
        ("Inside the Codex sandbox for AI agents", "f"),
    ]
    events, sources = _titled(rows)
    plan = select(events, sources, "10")
    titles = {c.event.title for c in plan.selected}
    assert titles == {rows[5][0]}
    assert plan.excluded == {
        "personnel": 1,
        "funding": 1,
        "promo": 1,
        "ranking": 1,
        "lawsuit": 1,
    }


def test_research_sources_need_a_signal_beyond_being_ai():
    rows = [
        ("Improving hyper-connections for LLM training", "arxiv-cs-cl"),
        ("Verification loops for LLM agent harness design", "arxiv-cs-cl"),
    ]
    events, sources = _titled(rows)
    plan = select(events, sources, "10")
    assert {c.event.title for c in plan.selected} == {rows[1][0]}
    assert plan.excluded == {"no_signal": 1}


def test_ai_source_needs_an_interest_match_without_ai_words():
    rows = [
        ("Inside the Codex sandbox", "openai-news"),
        ("A message to our customers in Chile", "openai-news"),
    ]
    events, sources = _titled(rows)
    assert {c.event.title for c in select(events, sources, "10").selected} == {
        rows[0][0]
    }


def test_mcp_registry_entries_need_a_repository_to_be_judged():
    cfg = load_merge_config(Path("config"))
    title = "io.github.example/agent-mcp-server 1.0.0"
    items = [
        item(
            "with-repo",
            "https://registry.modelcontextprotocol.io/v0.1/servers/a/versions/1.0.0",
            title=title,
            org="mcp-registry",
            kind="product",
            published_at=NOW,
            link_target_url="https://github.com/example/agent-mcp-server",
        ),
        item(
            "no-repo",
            "https://registry.modelcontextprotocol.io/v0.1/servers/b/versions/1.0.0",
            title=title.replace("example", "other"),
            org="mcp-registry",
            kind="product",
            published_at=NOW,
        ),
    ]
    events = group(items, [], cfg, now=NOW).new
    sources = {"mcp-registry": source("mcp-registry", "mcp_registry")}
    plan = select(events, sources, "10")
    assert [c.event.primary_item_id for c in plan.selected] == ["with-repo"]
    assert plan.excluded == {"no_repository": 1}


def test_source_cap_keeps_only_the_newest_release():
    cfg = load_merge_config(Path("config"))
    items = [
        item(
            f"r{i}",
            f"https://github.com/anthropics/claude-code/releases/tag/v2.1.{i}",
            title=f"Claude Code v2.1.{i}",
            org="claude-code-releases",
            published_at=NOW - timedelta(hours=10 - i),
        )
        for i in range(3)
    ]
    events = group(items, [], cfg, now=NOW).new
    feed = source("claude-code-releases", "rss").model_copy(
        update={"max_candidates": 1}
    )
    plan = select(events, {"claude-code-releases": feed}, "10")
    assert [c.event.title for c in plan.selected] == ["Claude Code v2.1.2"]
    assert plan.excluded == {"source_cap": 2}


def test_description_rescues_opaque_release_title():
    events, sources = sample(1, title="0.171.0")
    assert not select(events, sources, "5").selected
    plan = select(
        events,
        sources,
        "5",
        descriptions={events[0].event_id: "Codex CLI OpenAI model update"},
    )
    assert len(plan.selected) == 1


def test_curated_ai_feed_can_discover_unfamiliar_workflow():
    events, sources = sample(1, title="Nacre Loom", kind="aggregator")
    sources = {
        k: s.model_copy(update={"editorial_ai": True}) for k, s in sources.items()
    }
    plan = select(events, sources, "5")
    assert [r.pool for r in plan.selected] == ["explore"]


def test_screen_drops_discourse_and_lifts_the_count_cap():
    events, sources = sample(
        24, title="Agent harness for coding agents", kind="aggregator"
    )
    for number, event in enumerate(events):
        event.title = f"Agent harness {number} for coding agents"
    drop = events[0].event_id

    def screen(rows):
        return {
            event_id: "discourse" for event_id, _ in rows if event_id == drop
        }, set()

    plan = select(events, sources, "40", screen=screen)
    ids = {row.event_id for row in plan.selected}
    assert drop not in ids
    assert len(ids) == 23  # no 15-slot cap once screened
    assert plan.excluded["screened_out"] == 1
    assert plan.screen_drops == {drop: "discourse"}


def test_screen_failure_keeps_the_old_selection():
    events, sources = sample(24, kind="aggregator", title="Agent harness tool")
    assert len(select(events, sources, "20", screen=lambda rows: None).selected) == 15
    plain, sources = sample(
        3, title="Anthropic releases new benchmark", kind="aggregator"
    )
    assert select(plain, sources, "30", screen=lambda rows: None).selected == []


def test_official_events_skip_the_screen():
    events, sources = sample(3)
    seen = []

    def screen(rows):
        seen.extend(event_id for event_id, _ in rows)
        return {event_id: "drop" for event_id, _ in rows}, set()

    plan = select(events, sources, "10", screen=screen)
    assert not seen
    assert len(plan.selected) == 3


def test_must_read_outranks_the_keyword_score():
    events, sources = sample(6, kind="aggregator", title="Agent harness tool")
    last = events[-1].event_id
    plan = select(events, sources, "2", screen=lambda rows: ({}, {last}))
    assert last in {row.event_id for row in plan.selected}
