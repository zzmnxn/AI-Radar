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
        assert "application/vnd.github.raw+json" in accept
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
