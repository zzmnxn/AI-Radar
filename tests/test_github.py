import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from radar.collect import collect_source
from radar.http import Response
from test_catalogs import configured
from test_collectors import NOW


def reply(url, total=1, *, status=200, headers=None, incomplete=False):
    return Response(
        url,
        status,
        headers or {},
        json.dumps(
            {
                "total_count": total,
                "incomplete_results": incomplete,
                "items": [
                    {
                        "id": 10,
                        "html_url": "https://github.com/octocat/demo",
                        "full_name": "octocat/demo",
                        "created_at": "2026-10-01T00:00:00Z",
                        "pushed_at": "2026-11-17T12:00:00Z",
                        "stargazers_count": 230,
                    }
                ],
            }
        ).encode()
        if status == 200
        else None,
    )


def test_search_splits_large_ranges_and_deduplicates():
    github = configured(
        "github_search", "https://api.github.com/search/repositories?q=topic%3Aai"
    )
    queries = []

    def fetch(url, **options):
        query = parse_qs(urlsplit(url).query)["q"][0]
        queries.append(query)
        return reply(url, 1001 if len(queries) == 2 else 1)

    result = collect_source(github, NOW, since=NOW - timedelta(days=1), fetch=fetch)
    assert result.completeness == "complete"
    assert len(queries) == 4
    assert "created:" in queries[0] and all("pushed:" in query for query in queries[1:])
    assert queries[2] != queries[3]
    (item,) = result.items
    assert item.metrics.gh_stars == 230
    assert item.source_updated_at == NOW - timedelta(hours=12)


def test_search_rate_limit_keeps_collected_rows_and_stops():
    github = configured(
        "github_search", "https://api.github.com/search/repositories?q=topic%3Aai"
    )
    responses = iter(
        [
            reply(str(github.url), incomplete=True),
            reply(str(github.url), status=403, headers={"retry-after": "60"}),
        ]
    )
    result = collect_source(
        github,
        NOW,
        since=NOW - timedelta(days=1),
        fetch=lambda *a, **k: next(responses),
    )
    assert result.completeness == "partial"
    assert len(result.items) == 1
    assert result.partial_reasons == ["incomplete_results", "rate_limited"]


def test_search_keeps_first_page_if_limited_during_split():
    github = configured(
        "github_search", "https://api.github.com/search/repositories?q=topic%3Aai"
    )
    responses = iter(
        [
            reply(str(github.url), total=1001, headers={"x-ratelimit-remaining": "0"}),
        ]
    )
    result = collect_source(
        github,
        NOW,
        since=NOW - timedelta(days=1),
        fetch=lambda *a, **k: next(responses),
    )
    assert result.completeness == "partial"
    assert result.requests == 1
    assert result.partial_reasons == ["rate_limited"]
    assert len(result.items) == 1


def test_star_history_unavailable_returns_none():
    from radar.github import star_growth_7d

    assert (
        star_growth_7d(
            "octocat/demo", now=NOW, fetch=lambda url, **kw: reply(url, status=404)
        )
        is None
    )


def test_star_history_sums_seven_closed_daily_buckets():
    from radar.github import star_growth_7d

    week = int((NOW - timedelta(days=3)).timestamp())
    body = json.dumps(
        [
            {"week": week, "days": [10, 20, 30, 100, 0, 0, 0]},
            {"week": week - 7 * 86400, "days": [100, 100, 100, 1, 2, 3, 4]},
        ]
    ).encode()
    assert (
        star_growth_7d(
            "octocat/demo",
            now=NOW,
            fetch=lambda url, **kw: Response(url, 200, {}, body),
        )
        == 70
    )
