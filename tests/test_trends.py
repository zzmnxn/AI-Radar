from datetime import timedelta
from pathlib import Path

from test_collectors import NOW
from test_dedupe import item

from radar.deduplicate import load_merge_config, record_events
from radar.models import EventRecord

DAY = (NOW + timedelta(hours=9)).date()


def test_counts_all_events_and_rebuilds_from_saved_state(tmp_path):
    from radar.trends import rebuild_day

    cfg = load_merge_config(Path("config"))
    original = item("a", "https://example.org/a", title="OpenAI introduces NovaMesh")
    coverage = [
        item(
            f"b{i}",
            "https://example.org/a",
            title="NovaMesh coverage",
            kind="community",
            source_tags=["cs.AI"],
        )
        for i in range(10)
    ]
    record_events([original, *coverage], tmp_path, cfg, now=NOW, active_sources=11)
    (state,) = (tmp_path / "state/events").glob("*.jsonl")
    (event,) = [
        EventRecord.model_validate_json(line) for line in state.read_text().splitlines()
    ]
    assert event.analysis_status == "none"
    assert {"openai", "novamesh", "artificial-intelligence"} <= set(event.rule_tags)
    rows = {
        row.tag: row
        for row in rebuild_day(DAY, [event], 11, taxonomy_tags=cfg.entities)
    }
    assert (rows["openai"].events, rows["openai"].independent_orgs) == (1, 11)
    assert rows["openai"].kind == "taxonomy"

    path = tmp_path / "state/trends" / f"{DAY}.json"
    original_bytes = path.read_bytes()
    record_events([original, *coverage], tmp_path, cfg, now=NOW, active_sources=11)
    assert path.read_bytes() == original_bytes


def test_rebuild_counts_revision_and_new_members_on_kst_day(tmp_path):
    from radar.trends import rebuild_day

    cfg = load_merge_config(Path("config"))
    before = NOW.replace(hour=14, minute=59)
    after = before + timedelta(minutes=1)
    original = item("a", "https://example.org/a", title="OpenAI NovaMesh").model_copy(
        update={"first_seen_at": before}
    )
    update = original.model_copy(update={"snippet": "updated content"})
    other = item("b", "https://example.org/a", kind="community").model_copy(
        update={"first_seen_at": after}
    )
    record_events([original], tmp_path, cfg, now=before, active_sources=1)
    record_events([update, other], tmp_path, cfg, now=after, active_sources=2)
    (state,) = (tmp_path / "state/events").glob("*.jsonl")
    event = EventRecord.model_validate_json(state.read_bytes())
    yesterday = before.date()
    today = yesterday + timedelta(days=1)
    for day, expected in [(yesterday, 1), (today, 2)]:
        row = next(
            row for row in rebuild_day(day, [event], expected) if row.tag == "openai"
        )
        assert (row.events, row.independent_orgs) == (1, expected)


def test_discovery_requires_two_distinct_events_and_orgs(tmp_path):
    cfg = load_merge_config(Path("config"))
    a = item("a", "https://example.org/a", title="NovaMesh")
    coverage = item("b", str(a.url), title="NovaMesh", kind="community")
    c = item("c", "https://other.org/c", title="NovaMesh")
    result = record_events([a, coverage], tmp_path, cfg, now=NOW, active_sources=3)
    assert not result.emerging
    result = record_events([c], tmp_path, cfg, now=NOW, active_sources=3)
    assert [(row.tag, row.kind) for row in result.emerging] == [
        ("novamesh", "discovery")
    ]


def test_watch_requires_history_and_source_normalized_growth():
    from radar.trends import TrendDay, TrendRow, emerging

    def day(offset, sources, events):
        date = DAY - timedelta(days=offset)
        return TrendDay(
            date=date,
            active_sources=sources,
            rows=[
                TrendRow(
                    date=date,
                    tag="openai",
                    kind="taxonomy",
                    events=events,
                    independent_orgs=1,
                    active_sources=sources,
                )
            ],
        )

    baseline = [day(offset, 1, 1) for offset in range(7, 37)]
    recent = [day(offset, 2, 2) for offset in range(7)]
    assert not emerging(recent, DAY)
    assert not emerging(baseline + recent, DAY)
    growing = [day(offset, 2, 4) for offset in range(7)]
    assert [(row.tag, row.kind) for row in emerging(baseline + growing, DAY)] == [
        ("openai", "watch")
    ]


def test_rebuilding_replaces_tags_that_disappeared(tmp_path):
    from radar.trends import TrendDay

    cfg = load_merge_config(Path("config"))
    original = item("a", "https://example.org/a", title="OpenAI NovaMesh")
    record_events([original], tmp_path, cfg, now=NOW, active_sources=1)
    changed = original.model_copy(update={"title": "OpenAI DeltaMesh"})
    record_events([changed], tmp_path, cfg, now=NOW, active_sources=1)
    path = tmp_path / "state/trends" / f"{DAY}.json"
    tags = {row.tag for row in TrendDay.model_validate_json(path.read_bytes()).rows}
    assert tags == {"openai", "deltamesh"}
