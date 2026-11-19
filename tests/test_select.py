import json
from datetime import timedelta

from radar.assemble import briefing_path
from radar.demo import demo_inputs
from radar.models import Briefing, Card
from radar.select import select
from radar.slice import run_slice
from test_collectors import NOW


def card(key, score, org, source, *, pool="impact", revision=1):
    return Card(
        card_id=f"{key}-{revision}",
        event_id=key,
        revision=revision,
        analysis_id=f"analysis-{key}-{revision}",
        title_ko=key,
        category="도구",
        tags=["테스트"],
        sources=[
            dict(
                source_id=source,
                source_class="official",
                org=org,
                url="https://example.org/article",
                title=key,
            )
        ],
        what=["변경 사항"],
        why=["변경 영향"],
        how=[],
        availability="unknown",
        access={"type": "unknown"},
        verification_summary={"level": "verified", "claims": []},
        score=score,
        pool=pool,
    )


def test_diversity_and_explore_reservation():
    cards = [
        card("a", 100, "org", "src"),
        card("b", 99, "org", "src2"),
        card("c", 98, "org", "src3"),
        card("d", 97, "other", "src"),
        card("e", 96, "more", "src"),
        card("f", 95, "new", "new"),
        card("g", 94, "last", "last"),
        card("x", 75, "org", "src", pool="explore"),
        card("x", 74, "org", "src", pool="explore", revision=2),
    ]
    selected = select(cards, [], explore_min_score=70)
    assert [row.event_id for row in selected] == ["a", "f", "g", "x"]
    assert len({row.event_id for row in selected}) == len(selected)


def test_next_day_excludes_same_revision_and_links_previous(tmp_path):
    results, fetch, evaluate = demo_inputs(NOW)

    def run(now):
        return run_slice(
            results,
            now,
            tmp_path,
            fetch=fetch,
            evaluate=evaluate,
            analyzer_version="fixture",
            limit=1,
        )

    first = run(NOW)
    path = briefing_path(first.published.date)
    first_bytes = (tmp_path / path).read_bytes()
    (original,) = Briefing.model_validate_json(first_bytes).cards
    assert run(NOW + timedelta(hours=1)).last_run.outcome == "skipped"
    assert (tmp_path / path).read_bytes() == first_bytes
    empty = run(NOW + timedelta(days=1))
    payload = Briefing.model_validate_json(
        (tmp_path / briefing_path(empty.published.date)).read_bytes()
    )
    assert payload.cards == []
    assert payload.headline_ko == "주목할 변화 적음"
    results[0].items[0].snippet = "내용 변경"
    latest = run(NOW + timedelta(days=2))
    (updated,) = Briefing.model_validate_json(
        (tmp_path / briefing_path(latest.published.date)).read_bytes()
    ).cards
    assert updated.update_of == original.card_id
    assert updated.revision == original.revision + 1
    index = json.loads((tmp_path / "index" / f"{NOW.year}.json").read_bytes())
    assert list(index) == [
        (NOW + timedelta(days=offset)).date().isoformat() for offset in range(3)
    ]
    assert index[payload.date.isoformat()]["card_count"] == 0


def test_120_day_history_reads_only_published_dates(tmp_path):
    from radar.select import recent_shown

    results, fetch, evaluate = demo_inputs(NOW)
    first = run_slice(
        results,
        NOW,
        tmp_path,
        fetch=fetch,
        evaluate=evaluate,
        analyzer_version="fixture",
        limit=1,
    ).published
    original = tmp_path / briefing_path(first.date)
    orphan_day = first.date + timedelta(days=1)
    (tmp_path / briefing_path(orphan_day)).write_bytes(original.read_bytes())
    rows = recent_shown(tmp_path, first.date + timedelta(days=120), first)
    assert [row["date"] for row in rows] == [first.date.isoformat()]
    assert not recent_shown(tmp_path, first.date + timedelta(days=121), first)


def test_two_cards_and_archive_rollback_after_manifest_write_failure(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    import pytest
    from pydantic import HttpUrl

    from radar import slice as pipeline

    results, fetch, evaluate = demo_inputs(NOW)
    original = results[0].items[0]
    second_url = HttpUrl("https://example.org/another-item")
    second = original.model_copy(
        update={
            "item_id": "second",
            "title": "별도의 독립 검색 도구",
            "url": second_url,
            "canonical_url": second_url,
        }
    )
    results[0].items.append(second)

    def evidence(item, *, now):
        doc = fetch(item, now=now)
        return replace(
            doc,
            evidence=doc.evidence.model_copy(
                update={"url": item.url, "final_url": item.url}
            ),
        )

    def evaluation(item, doc):
        result = evaluate(item, doc).model_copy(deep=True)
        for claim in result.claims:
            claim.evidence_url = item.url
        return result

    def run(now):
        return run_slice(
            results,
            now,
            tmp_path,
            fetch=evidence,
            evaluate=evaluation,
            analyzer_version="fixture",
            limit=5,
        )

    first = run(NOW)
    payload = Briefing.model_validate_json(
        (tmp_path / briefing_path(first.published.date)).read_bytes()
    )
    assert len(payload.cards) == payload.counts.selected == 2
    assert all(card.analysis_id for card in payload.cards)
    names = [*first.published.files, "state/shown.json", "manifest.json"]
    before = {name: (tmp_path / name).read_bytes() for name in names}
    original_write = pipeline.write_json_atomic

    def fail_manifest(path, value):
        if path.name == "manifest.json":
            raise OSError("interrupted manifest write")
        original_write(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "write_json_atomic", fail_manifest)
        with pytest.raises(OSError, match="interrupted manifest write"):
            run(NOW + timedelta(days=1))
    assert pipeline.validate_snapshot(tmp_path) == first
    assert {name: (tmp_path / name).read_bytes() for name in names} == before
    assert not (
        tmp_path / briefing_path(first.published.date + timedelta(days=1))
    ).exists()


def test_archive_backfills_titles_in_older_years(tmp_path):
    from hashlib import sha256

    from radar.storage import write_json_atomic

    earlier = NOW.replace(year=2025, month=12, day=24)
    results, fetch, evaluate = demo_inputs(earlier)
    first = run_slice(
        results,
        earlier,
        tmp_path,
        fetch=fetch,
        evaluate=evaluate,
        analyzer_version="fixture",
        limit=1,
    )
    index_path = "index/2025.json"
    path = tmp_path / index_path
    index = json.loads(path.read_bytes())
    index[earlier.date().isoformat()].pop("titles")
    write_json_atomic(path, index)
    first.published.files[index_path] = sha256(path.read_bytes()).hexdigest()
    write_json_atomic(tmp_path / "manifest.json", first.model_dump(mode="json"))

    results, fetch, evaluate = demo_inputs(NOW)
    results[0].items[0].snippet = "변경된 문서 검색"
    published = run_slice(
        results,
        NOW,
        tmp_path,
        fetch=fetch,
        evaluate=evaluate,
        analyzer_version="fixture",
        limit=1,
    )
    assert index_path in published.published.files
    row = json.loads(path.read_bytes())[earlier.date().isoformat()]
    assert row["titles"] == ["테스트용 · 문서 검색 결과에 원문 링크 추가"]
