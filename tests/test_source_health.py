import json
import sys
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from radar.__main__ import main
from radar.health import collect_with_health
from radar.http import FetchError
from radar.storage import write_json_atomic

from test_collectors import FEED, HN, NOW, source


def test_collection_dry_run_preserves_health(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["radar", "collect", "--dry-run", "--data", str(tmp_path)]
    )
    monkeypatch.setattr("radar.__main__.load_sources", lambda _: [source("rss", "rss")])
    monkeypatch.setattr("radar.collect._fetch", lambda *a, **k: FEED)
    main()
    assert "rss" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_unranged_partial_gap_is_closed_on_complete(tmp_path):
    path = tmp_path / "state/source_health.json"
    rss = source("rss", "rss")
    collect_with_health([rss], NOW, path, fetch=lambda *a, **k: FEED)
    tomorrow = NOW + timedelta(days=1)
    broken = FEED.replace(b"https://example.org/undated", b"javascript:invalid")
    collect_with_health([rss], tomorrow, path, fetch=lambda *a, **k: broken)
    row = json.loads(path.read_text())["rss"]
    assert row["last_complete_at"] == "2026-11-18T00:00:00Z"
    assert row["health"] == "degraded"
    assert row["gaps"][0]["status"] == "open"
    assert row["gaps"][0]["from"] == "2026-11-17T00:00:00Z"

    collect_with_health(
        [rss], tomorrow + timedelta(days=1), path, fetch=lambda *a, **k: FEED
    )
    row = json.loads(path.read_text())["rss"]
    assert row["last_complete_at"] == "2026-11-20T00:00:00Z"
    assert row["gaps"][0]["status"] == "unrecoverable"


def test_hn_recovers_gap_before_current_range_and_preserves_results(tmp_path):
    path = tmp_path / "source_health.json"
    hn = source("hn", "hackernews").model_copy(
        update={
            "supports_range": True,
            "url": "https://example.org/hn?numericFilters=points%3E%3D5",
        }
    )
    requests = []

    def fetch(url, **kwargs):
        params = parse_qs(urlsplit(url).query)
        requests.append(params["numericFilters"][0])
        return HN

    collect_with_health([hn], NOW, path, fetch=fetch)
    tomorrow = NOW + timedelta(days=1)
    collect_with_health(
        [hn],
        tomorrow,
        path,
        fetch=lambda *a, **k: HN.replace(b'"nbPages":1', b'"nbPages":2'),
    )
    (result,) = collect_with_health(
        [hn], tomorrow + timedelta(days=1), path, fetch=fetch
    )
    assert len(result.items) == 1
    assert result.requests == 2
    assert requests == [
        f"points>=5,created_at_i>={int((NOW - timedelta(days=3)).timestamp())},created_at_i<={int(NOW.timestamp())}",
        f"points>=5,created_at_i>={int((NOW - timedelta(days=1)).timestamp())},created_at_i<={int(tomorrow.timestamp())}",
        f"points>=5,created_at_i>={int(tomorrow.timestamp())},created_at_i<={int((tomorrow + timedelta(days=1)).timestamp())}",
    ]
    row = json.loads(path.read_text())["hn"]
    assert row["gaps"][0]["status"] == "recovered"
    assert row["last_complete_at"] == "2026-11-20T00:00:00Z"


def test_expired_gap_is_closed_and_since_is_capped(tmp_path):
    path = tmp_path / "source_health.json"
    hn = source("hn", "hackernews").model_copy(update={"supports_range": True})
    write_json_atomic(
        path,
        {
            "hn": {
                "source_id": "hn",
                "last_complete_at": "2026-11-01T00:00:00Z",
                "gaps": [
                    {
                        "from": "2026-11-01T00:00:00Z",
                        "to": "2026-11-02T00:00:00Z",
                        "reason": "pagination_stopped",
                        "status": "open",
                    }
                ],
            }
        },
    )
    requests = []

    def fetch(url, **kwargs):
        requests.append(parse_qs(urlsplit(url).query)["numericFilters"][0])
        return HN

    collect_with_health([hn], NOW, path, fetch=fetch)
    assert len(requests) == 1
    assert requests[0].startswith(
        f"created_at_i>={int((NOW - timedelta(days=7)).timestamp())},"
    )
    assert json.loads(path.read_text())["hn"]["gaps"][0]["status"] == "lookback_capped"


def test_source_failures_are_isolated_and_staleness_uses_original_date(tmp_path):
    path = tmp_path / "source_health.json"
    rss = source("rss", "rss")
    broken = source("broken", "rss")

    def fetch(url, **kwargs):
        if url.endswith("/broken"):
            raise FetchError("timeout")
        return FEED

    for days in (0, 1, 4):
        collect_with_health(
            [rss, broken], NOW + timedelta(days=days), path, fetch=fetch
        )

    state = json.loads(path.read_text())
    assert state["broken"]["health"] == "failing"
    assert state["broken"]["consecutive_failures"] == 3
    assert state["rss"]["health"] == "stale"
    assert state["rss"]["last_item_at"] == "2026-11-17T12:00:00Z"


def test_github_rate_limit_during_gap_defers_the_current_window(tmp_path):
    from test_catalogs import configured
    from test_github import reply

    path = tmp_path / "source_health.json"
    github = configured(
        "github_search", "https://api.github.com/search/repositories?q=topic%3Aai"
    )

    def limited(url, **opts):
        return reply(url, status=429)

    collect_with_health([github], NOW, path, fetch=limited)
    (result,) = collect_with_health(
        [github], NOW + timedelta(days=1), path, fetch=limited
    )
    assert result.requests == 1
    assert "rate_limited" in result.partial_reasons
    row = json.loads(path.read_text())["github_search"]
    assert row["last_complete_at"] is None
    assert [(gap["to"], gap["status"]) for gap in row["gaps"]] == [
        ("2026-11-18T00:00:00Z", "open"),
        ("2026-11-19T00:00:00Z", "open"),
    ]
