from datetime import datetime, timedelta, timezone

from radar.collect import Source, collect, select_candidates

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
FEED = b"""<rss version="2.0"><channel>
<item><guid>001</guid><title>AI release</title>
<link>https://example.org/release?utm_source=feed</link>
<pubDate>Tue, 17 Nov 2026 12:00:00 GMT</pubDate></item>
<item><guid>002</guid><title>Undated AI release</title>
<link>https://example.org/undated</link></item>
</channel></rss>"""
HN = b"""{"nbPages":1,"hits":[{"objectID":"456","title":"Release discussion",
"url":"https://example.org/release","created_at":"2026-11-17T18:00:00Z","points":42}]}"""


def source(id, kind):
    return Source(
        id=id,
        name=id,
        org=id,
        type=kind,
        url=f"https://example.org/{id}",
        source_class="aggregator" if kind == "hackernews" else "official",
        discovery_mode="open" if kind == "hackernews" else "keyword",
        supports_range=False,
        trust=4,
        status="candidate",
        expected_cadence_days=1,
        auth="none",
        terms_note="test",
    )


def test_collect_isolates_failed_feed_and_preserves_dates():
    def fetch(url, **kwargs):
        return {
            "rss": FEED,
            "hn": HN,
            "bad": b"<invalid",
        }[url.rsplit("/", 1)[-1]]

    results = collect(
        [source("rss", "rss"), source("hn", "hackernews"), source("bad", "rss")],
        NOW,
        fetch=fetch,
    )
    assert [result.completeness for result in results] == [
        "complete",
        "complete",
        "failed",
    ]
    rss, hn, _ = results
    assert rss.items[0].published_at == NOW - timedelta(hours=12)
    assert rss.items[0].source_updated_at is None
    assert rss.items[1].published_at is None
    assert rss.items[1].first_seen_at == NOW
    assert hn.items[0].metrics.hn_points == 42
    assert str(hn.items[0].link_target_url) == "https://example.org/release"

    selected = select_candidates(results, NOW, limit=5)
    assert len(selected) == 2
    assert {item.source_id for item in selected} == {"rss"}


def test_partial_hackernews_is_reported():
    payload = HN.replace(b'"nbPages":1', b'"nbPages":2')
    (result,) = collect(
        [source("hn", "hackernews")], NOW, fetch=lambda *a, **k: payload
    )
    assert result.completeness == "partial"
    assert len(result.items) == 1


def test_atom_updated_does_not_replace_missing_published_date():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://example.org/posts/">
      <entry><id>one</id><title>Update</title><link rel="alternate" href="one"/>
        <category term="cs.AI"/><category term="cs.AI"/><category/>
        <updated>2026-11-17T10:00:00Z</updated></entry>
    </feed>"""
    (result,) = collect([source("atom", "rss")], NOW, fetch=lambda *a, **k: body)
    (item,) = result.items
    assert item.published_at is None
    assert item.source_updated_at == NOW - timedelta(hours=14)
    assert str(item.url) == "https://example.org/posts/one"
    assert item.source_tags == ["cs.AI"]
