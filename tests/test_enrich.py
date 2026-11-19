from datetime import datetime, timezone

from radar.collect import RawItem
from radar.http import Response
from radar.models import Claim
from radar.enrich import enrich, verified_claims

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
URL = "https://example.org/release"
QUOTE = "The update adds an explicit cache limit and a configuration guide."


def test_enrich_and_verify_only_article_text():
    item = RawItem(
        item_id="a",
        source_id="rss",
        source_class="official",
        title="A new feature",
        url=URL,
        canonical_url=URL,
        first_seen_at=NOW,
    )
    html = f"<nav>Site navigation</nav><article><script>Script text is not an article claim.</script><p>{QUOTE}</p></article>"
    doc = enrich(
        item,
        now=NOW,
        fetch=lambda *a, **k: Response(
            URL,
            200,
            {"content-type": "text/html; charset=utf-8"},
            html.encode(),
        ),
    )
    assert doc.text == QUOTE
    assert "text" not in doc.evidence.model_dump()

    def claim(url, quote):
        return Claim(
            role="what",
            sentence_index=0,
            text="캐시 한도를 설정할 수 있다.",
            evidence_url=url,
            evidence_quote=quote,
        )

    good = claim(URL, QUOTE)
    assert verified_claims(
        [
            good,
            claim("https://example.org/other", QUOTE),
            claim(URL, "Script text is not an article claim."),
        ],
        doc,
    ) == [good]


def test_github_uses_readme_api_with_bounded_text():
    from radar.http import ARTICLE_BYTES

    url = "https://github.com/example/project"
    api = "https://api.github.com/repos/example/project/readme"
    item = RawItem(
        item_id="repo",
        source_id="github",
        source_class="community",
        title="Repository",
        url=url,
        canonical_url=url,
        first_seen_at=NOW,
    )

    def fetch(endpoint, *, max_bytes, accept, github_token):
        assert endpoint == api
        assert max_bytes == ARTICLE_BYTES
        assert accept == {"application/vnd.github.raw+json"}
        return Response(
            api, 200, {"content-type": "application/vnd.github.raw+json"}, b"a" * 60_001
        )

    doc = enrich(item, now=NOW, fetch=fetch)
    assert str(doc.evidence.url) == url
    assert str(doc.evidence.final_url) == api
    assert doc.evidence.chars == len(doc.text) == 60_000
    assert doc.evidence.truncated
    assert "text" not in doc.evidence.model_dump()


def test_arxiv_pdf_link_uses_abstract_and_other_pdfs_are_rejected():
    import pytest

    from radar.http import FetchError

    url = "https://arxiv.org/pdf/2611.00123v2.pdf"
    abstract = "https://arxiv.org/abs/2611.00123v2"
    item = RawItem(
        item_id="paper",
        source_id="research",
        source_class="research",
        title="Paper",
        url=url,
        canonical_url=url,
        first_seen_at=NOW,
    )

    def fetch(endpoint, **kwargs):
        assert endpoint == abstract
        return Response(
            endpoint,
            200,
            {"content-type": "text/html"},
            b"<article>Abstract.</article>",
        )

    assert str(enrich(item, now=NOW, fetch=fetch).evidence.url) == abstract
    with pytest.raises(FetchError, match="bad_content_type"):
        enrich(
            item,
            now=NOW,
            fetch=lambda endpoint, **kw: Response(
                endpoint, 200, {"content-type": "application/pdf"}, b"%PDF"
            ),
        )


def _doc(text):
    from radar.enrich import SourceDoc
    from radar.models import EvidenceMeta

    return SourceDoc(
        text,
        EvidenceMeta(
            url=URL,
            final_url=URL,
            http_status=200,
            content_type="text/html",
            fetched_at=NOW,
            text_sha256="0" * 64,
            chars=len(text),
            truncated=False,
        ),
    )


def _numeric_claim(text, quote):
    return Claim(
        role="what",
        sentence_index=0,
        text=text,
        evidence_url=URL,
        evidence_quote=quote,
    )


def test_number_words_match_digits_but_not_other_numbers():
    quote = "Three senior researchers left the lab to start a new company."
    doc = _doc(quote)
    three = _numeric_claim("선임 연구원 3명이 떠났다.", quote)
    thirty = _numeric_claim("선임 연구원 30명이 떠났다.", quote)
    assert verified_claims([three, thirty], doc) == [three]


def test_number_words_keep_decimal_and_percent_behaviour():
    quote = "Latency dropped by 12.5% while accuracy stayed at twenty points."
    doc = _doc(quote)
    good = _numeric_claim("지연이 12.5% 줄었고 정확도는 20점이다.", quote)
    bad = _numeric_claim("지연이 12% 줄었다.", quote)
    assert verified_claims([good, bad], doc) == [good]


def _hn_item(title):
    return RawItem(
        item_id="hn",
        source_id="hackernews-show",
        source_class="aggregator",
        title=title,
        url="https://news.ycombinator.com/item?id=42",
        canonical_url="https://news.ycombinator.com/item?id=42",
        first_seen_at=NOW,
    )


def test_hn_self_post_uses_only_the_post_body():
    import json

    import pytest

    from radar.http import FetchError

    seen = []

    def fetch(url, **kwargs):
        seen.append(url)
        body = {
            "text": "<p>We built an agent that edits spreadsheets.</p>",
            "children": [{"text": "A comment that must not be evidence."}],
        }
        return Response(
            url, 200, {"content-type": "application/json"}, json.dumps(body).encode()
        )

    doc = enrich(_hn_item("Show HN: Sheet agent"), now=NOW, fetch=fetch)
    assert seen == ["https://hn.algolia.com/api/v1/items/42"]
    assert doc.text == "We built an agent that edits spreadsheets."
    assert str(doc.evidence.url) == "https://news.ycombinator.com/item?id=42"

    with pytest.raises(FetchError, match="discussion_only"):
        enrich(_hn_item("OpenAI researchers resign"), now=NOW, fetch=fetch)


def test_github_release_uses_api_release_notes():
    import json

    seen = []

    def fetch(url, **kwargs):
        seen.append((url, kwargs["accept"]))
        body = {"body": "## New Features\n- Added WebRTC voice calls in the TUI."}
        return Response(
            url, 200, {"content-type": "application/json"}, json.dumps(body).encode()
        )

    page = "https://github.com/openai/codex/releases/tag/rust-v0.170.0"
    release = RawItem(
        item_id="r",
        source_id="codex-releases",
        source_class="official",
        url=page,
        canonical_url=page,
        title="Codex 0.170.0",
        first_seen_at=NOW,
    )
    doc = enrich(release, now=NOW, fetch=fetch)
    assert seen == [
        (
            "https://api.github.com/repos/openai/codex/releases/tags/rust-v0.170.0",
            {"application/json"},
        )
    ]
    assert doc.text == "## New Features - Added WebRTC voice calls in the TUI."
    assert str(doc.evidence.url) == page
