from datetime import timedelta

import pytest

from radar.demo import demo_inputs
from radar.deduplicate import load_events
from radar.prune import prune
from radar.slice import run_slice, validate_snapshot
from test_collectors import NOW


def preview(data, now, *, revised=False):
    results, fetch, evaluate = demo_inputs(now)
    if revised:
        results[0].items[0].snippet = "새 업데이트"
    return run_slice(
        results,
        now,
        data,
        fetch=fetch,
        evaluate=evaluate,
        analyzer_version="fixture",
        limit=1,
    )


def test_keeps_active_rows_in_old_shards(tmp_path):
    first = NOW - timedelta(days=130)
    preview(tmp_path, first)
    preview(tmp_path, NOW, revised=True)
    (event,) = load_events(tmp_path)
    assert event.first_seen_at == first
    old_shard = tmp_path / "state/events" / f"{first:%Y-%m}.jsonl"
    assert old_shard.is_file()
    prune(tmp_path, NOW.date())
    assert old_shard.is_file()
    assert load_events(tmp_path) == [event]
    validate_snapshot(tmp_path, references=True)


def test_validate_window(tmp_path):
    from radar.models import Manifest

    previous = NOW - timedelta(days=121)
    preview(tmp_path, previous)
    preview(tmp_path, NOW, revised=True)
    data = tmp_path / "state/analyses"
    old = data / f"{previous:%Y-%m}.jsonl"
    old.unlink()
    validate_snapshot(tmp_path, references=True)
    last = Manifest.model_validate_json((tmp_path / "manifest.json").read_bytes())
    assert last.published.date == NOW.date()
    for path in data.glob("*.jsonl"):
        path.unlink()
    with pytest.raises(ValueError, match="Broken briefing reference"):
        validate_snapshot(tmp_path, references=True)


def test_prune_removes_empty_inactive_shards(tmp_path):
    old = NOW - timedelta(days=122)
    preview(tmp_path, old)
    prune(tmp_path, NOW.date())
    assert not list((tmp_path / "state/events").glob("*.jsonl"))
    assert not list((tmp_path / "state/analyses").glob("*.jsonl"))
