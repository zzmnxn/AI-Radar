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


def test_release_feed_prefixes_titles_skips_prereleases_and_dates_by_update():
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>a</id><title>Release v0.69.0</title>
        <link rel="alternate" href="https://github.com/o/r/releases/tag/v0.69.0"/>
        <updated>2026-11-17T10:00:00Z</updated></entry>
      <entry><id>b</id><title>v0.70.0-preview.0</title>
        <link rel="alternate" href="https://github.com/o/r/releases/tag/v0.70.0-preview.0"/>
        <updated>2026-11-17T11:00:00Z</updated></entry>
      <entry><id>c</id><title>0.172.0-alpha.1</title>
        <link rel="alternate" href="https://github.com/o/r/releases/tag/rust-v0.172.0-alpha.1"/>
        <updated>2026-11-17T12:00:00Z</updated></entry>
    </feed>"""
    feed = source("releases", "rss").model_copy(
        update=dict(
            title_prefix="Gemini CLI",
            skip_prereleases=True,
            published_from_updated=True,
        )
    )
    (result,) = collect([feed], NOW, fetch=lambda *a, **k: body)
    (item,) = result.items
    assert item.title == "Gemini CLI v0.69.0"
    assert item.published_at == NOW - timedelta(hours=14)
    assert result.completeness == "complete"


def test_tldr_articles_keep_issue_credit_and_ignore_sponsors():
    archive = b'<a href="/ai/2026-11-17">issue</a>'
    issue = b'<article><a href="https://code.org/new"><h3>Tool</h3></a>Agents in a terminal</article><article><a href="https://ad.org"><h3>Offer (Sponsor)</h3></a></article>'
    src = source("tldr-ai", "tldr").model_copy(
        update={"url": "https://tldr.tech/ai/archives"}
    )
    (result,) = collect(
        [src],
        NOW,
        fetch=lambda url, **k: archive if url.endswith("archives") else issue,
    )
    assert result.completeness == "complete"
    (item,) = result.items
    assert str(item.url) == "https://tldr.tech/ai/2026-11-17"
    assert str(item.link_target_url) == "https://code.org/new"
    assert "Agents in a terminal" in item.snippet


def test_linked_project_and_authored_post_are_distinct():
    body = b"""<rss><channel><item><guid>original</guid><title>Trying a new tool</title>
    <link>https://latent.space/p/trial</link><description><![CDATA[
    <a href="https://github.com/person/tool">Tool repository</a>
    <a href="https://github.com/person/tool?utm_source=post">Tool repository</a>
    ]]></description></item></channel></rss>"""
    (result,) = collect(
        [source("latent-space", "rss")], NOW, fetch=lambda *a, **k: body
    )
    assert len(result.items) == 2
    original, referral = result.items
    assert original.link_target_url is None
    assert original.url == referral.url
    assert str(referral.canonical_url) == "https://github.com/person/tool"


INDEX = b"""<html><body>
<article><h3><a href="/research/components-of-agent-memory">The components of agent memory</a></h3>
<time>Nov 6, 2026</time></article>
<article><h3><a href="https://www.anthropic.com/engineering/new-harness">  A new harness </a></h3>
<time>Nov 17, 2026</time></article>
<article><h3><a href="/engineering/no-date">No date here</a></h3></article>
</body></html>"""


def test_article_index_page_reads_cards_with_dates():
    from radar.collect import collect_source

    src = source("anthropic-eng", "html_index")
    result = collect_source(
        src,
        NOW,
        since=NOW - timedelta(days=30),
        fetch=lambda url, **kwargs: INDEX,
    )
    assert result.completeness == "complete"
    titles = {item.title: item.url.unicode_string() for item in result.items}
    assert (
        titles["A new harness"] == "https://www.anthropic.com/engineering/new-harness"
    )
    assert titles["The components of agent memory"].endswith(
        "/research/components-of-agent-memory"
    )
    assert "No date here" not in titles


def test_article_index_structure_change_is_a_failure_not_empty():
    from radar.collect import collect_source

    src = source("anthropic-eng", "html_index")
    result = collect_source(
        src, NOW, fetch=lambda url, **kwargs: b"<html><body>redesign</body></html>"
    )
    assert result.completeness == "failed"
