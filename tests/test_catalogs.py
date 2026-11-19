import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from radar.collect import collect_source
from test_collectors import NOW, source


def configured(kind, url):
    original = source(kind, "rss")
    return type(original).model_validate(
        original.model_dump() | {"type": kind, "url": url, "supports_range": True}
    )


def test_arxiv_submission_range_and_truncation():
    arxiv = configured(
        "arxiv", "https://export.arxiv.org/api/query?search_query=cat%3Acs.CL"
    )

    def fetch(url, **options):
        params = parse_qs(urlsplit(url).query)
        assert params["search_query"] == [
            "(cat:cs.CL) AND submittedDate:[202611170000 TO 202611180000]"
        ]
        return b"""<feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:os="http://a9.com/-/spec/opensearch/1.1/">
          <os:totalResults>2</os:totalResults>
          <entry><id>https://arxiv.org/abs/2611.00001v2</id><title>Example</title>
          <link href="https://arxiv.org/abs/2611.00001v2" rel="alternate"/>
          <published>2026-11-17T01:00:00Z</published><updated>2026-11-17T02:00:00Z</updated>
          </entry></feed>"""

    result = collect_source(arxiv, NOW, since=NOW - timedelta(days=1), fetch=fetch)
    assert result.completeness == "partial"
    assert result.partial_reasons == ["pagination_stopped"]
    (item,) = result.items
    assert str(item.canonical_url) == "https://arxiv.org/abs/2611.00001"
    assert item.source_updated_at - item.published_at == timedelta(hours=1)


def test_hf_preserves_the_paper_publication_date():
    hf = configured("hf_papers", "https://huggingface.co/api/daily_papers")
    dates = []

    def fetch(url, **options):
        date = parse_qs(urlsplit(url).query)["date"][0]
        dates.append(date)
        return json.dumps(
            [
                {
                    "publishedAt": "2026-11-17T19:00:00Z",
                    "paper": {
                        "id": "2611.00001",
                        "title": "Example",
                        "publishedAt": "2026-11-16T12:00:00Z",
                    },
                }
            ]
            if date == "2026-11-17"
            else []
        ).encode()

    result = collect_source(
        hf,
        NOW,
        since=NOW - timedelta(days=2),
        fetch=fetch,
    )
    assert dates == ["2026-11-16", "2026-11-17", "2026-11-18"]
    (item,) = result.items
    assert item.published_at == NOW - timedelta(days=1, hours=12)
    assert str(item.link_target_url) == "https://arxiv.org/abs/2611.00001"


def test_mcp_follows_cursor_and_uses_distinct_dates():
    mcp = configured(
        "mcp_registry", "https://registry.modelcontextprotocol.io/v0.1/servers"
    )
    calls = []

    def fetch(url, **options):
        params = parse_qs(urlsplit(url).query)
        assert "include_deleted" not in params
        calls.append(params)
        return json.dumps(
            {
                "metadata": {"nextCursor": "server:1"} if len(calls) == 1 else {},
                "servers": []
                if len(calls) == 1
                else [
                    {
                        "server": {"name": "com.example/demo", "version": "1.0.0"},
                        "_meta": {
                            "io.modelcontextprotocol.registry/official": {
                                "status": "active",
                                "publishedAt": "2026-11-14T12:00:00Z",
                                "updatedAt": "2026-11-17T12:00:00Z",
                            }
                        },
                    }
                ],
            }
        ).encode()

    result = collect_source(mcp, NOW, since=NOW - timedelta(days=1), fetch=fetch)
    assert result.completeness == "complete"
    assert calls[0]["updated_since"] == calls[1]["updated_since"]
    assert calls[1]["cursor"] == ["server:1"]
    (item,) = result.items
    assert item.source_updated_at - item.published_at == timedelta(days=3)
    assert result.requests == 2
