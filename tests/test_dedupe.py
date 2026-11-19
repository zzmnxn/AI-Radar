from datetime import timedelta
from pathlib import Path

from radar.models import RawItem
from test_collectors import NOW


def item(
    id,
    url,
    *,
    title="OpenAI GPT adds configurable audio streaming",
    org=None,
    kind="official",
    **extra,
):
    return RawItem(
        item_id=id,
        url=url,
        canonical_url=url,
        title=title,
        source_id=org or id,
        source_class=kind,
        first_seen_at=NOW,
        **extra,
    )


def config(**updates):
    from radar.deduplicate import MergeConfig

    return MergeConfig(entities={"openai": ["OpenAI"], "gpt": ["GPT"]}, **updates)


def test_url_and_hn_discussion_group_under_official_source():
    from radar.deduplicate import group

    url = "https://example.org/post"
    official = item("official", url)
    coverage = item("coverage", url + "?utm_source=feed", kind="community")
    hn = item(
        "hn",
        "https://news.ycombinator.com/item?id=3",
        kind="aggregator",
        link_target_url=url,
    )
    result = group([hn, coverage, official], [], config(), now=NOW)
    (event,) = result.new
    assert event.primary_item_id == "official"
    assert {member.role for member in event.members} == {
        "primary",
        "coverage",
        "discussion",
    }


def test_arxiv_external_id_matches_hf_paper():
    from radar.deduplicate import group

    arxiv = item("a", "https://arxiv.org/abs/2611.00001v2", kind="research")
    hf = item("b", "https://huggingface.co/papers/2611.00001", kind="research")
    (event,) = group([arxiv, hf], [], config(), now=NOW).new
    assert len(event.members) == 2


def test_title_merges_require_all_guards():
    from radar.deduplicate import group

    url = "https://github.com/org/a/releases/tag/v1.2"
    a = item("a", url, title="OpenAI GPT audio streaming release v1.2")
    b = item("b", "https://github.com/org/b/releases/tag/v1.2", title=a.title)
    c = item(
        "c",
        "https://example.org/updates",
        title="OpenAI GPT audio streaming release v1.3",
    )
    d = item("d", "https://example.org/pricing", title=a.title + " pricing")
    result = group([a, b, c, d], [], config(title_mode="merge"), now=NOW)
    assert len(result.new) == 4
    assert any(event.related_event_ids for event in result.new)


def test_different_semver_prereleases_remain_separate():
    from radar.deduplicate import group

    title = "OpenAI GPT introduces configurable low latency audio streaming v1.2"
    a = item("a", "https://example.org/beta", title=title + "-beta")
    b = item("b", "https://other.org/rc", title=title + "-rc")
    assert len(group([a, b], [], config(title_mode="merge"), now=NOW).new) == 2


def test_title_is_related_by_default_and_merge_mode_adds_official_revision():
    from radar.deduplicate import group

    a = item("a", "https://example.org/original")
    b = item("b", "https://other.org/official")
    (original,) = group([a], [], config(), now=NOW).new
    original.analysis_status = "done"
    related = group([b], [original], config(), now=NOW + timedelta(hours=3))
    (linked,) = related.new
    assert linked.related_event_ids == [original.event_id]
    assert related.touched[0].related_event_ids == [linked.event_id]

    (revised,) = group(
        [b], [original], config(title_mode="merge"), now=NOW + timedelta(hours=3)
    ).revised
    assert revised.event_id == original.event_id
    assert revised.revision == 2
    assert revised.analysis_status == "none"


def test_timestamp_only_update_preserves_revision_and_store_is_idempotent(tmp_path):
    from radar.deduplicate import record_events

    a = item("a", "https://example.org/original", snippet="private feed preview")
    initial = record_events([a], tmp_path, config(), now=NOW).new[0]
    update = a.model_copy(
        update={
            "source_updated_at": NOW + timedelta(hours=2),
            "first_seen_at": NOW + timedelta(hours=2),
        }
    )
    result = record_events([update], tmp_path, config(), now=NOW + timedelta(hours=2))
    (refreshed,) = result.touched
    assert refreshed.revision == initial.revision
    assert refreshed.members[0].ref.first_seen_at == NOW
    (path,) = (tmp_path / "state/events").glob("*.jsonl")
    first = path.read_bytes()
    assert b"private feed preview" not in first
    assert len(first.splitlines()) == 1
    assert (
        record_events(
            [update], tmp_path, config(), now=NOW + timedelta(hours=3)
        ).touched
        == []
    )
    assert path.read_bytes() == first


def test_configured_overrides_win_over_similarity():
    from radar.deduplicate import group

    a = item("a", "https://example.org/a")
    b = item("b", "https://example.org/b", title="Unrelated")
    pair = (str(a.url), str(b.url))
    (original,) = group([a], [], config(), now=NOW).new
    assert len(group([b], [original], config(force_merge=[pair]), now=NOW).revised) == 1
    b = b.model_copy(update={"title": a.title})
    assert (
        len(
            group(
                [b], [original], config(title_mode="merge", never_merge=[pair]), now=NOW
            ).new
        )
        == 1
    )


def test_real_pipeline_files_have_reviewed_safe_defaults():
    from radar.deduplicate import load_merge_config

    assert load_merge_config(Path("config")).title_mode == "related_only"


def test_collect_record_saves_events_without_model_or_publication(
    tmp_path, monkeypatch
):
    import sys
    from radar.__main__ import main
    from test_collectors import FEED, source

    monkeypatch.setattr(
        sys, "argv", ["radar", "collect", "--record", "--data", str(tmp_path)]
    )
    monkeypatch.setattr("radar.__main__.load_sources", lambda _: [source("rss", "rss")])
    monkeypatch.setattr("radar.collect._fetch", lambda *args, **options: FEED)
    main()
    assert any((tmp_path / "state/events").glob("*.jsonl"))
    assert (tmp_path / "state/source_health.json").is_file()
    assert any((tmp_path / "state/trends").glob("*.json"))
    assert not (tmp_path / "manifest.json").exists()
