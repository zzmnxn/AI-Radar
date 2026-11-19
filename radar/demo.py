from hashlib import sha256

from .collect import CollectResult, Source
from .enrich import SourceDoc
from .models import EvalResult, EvidenceMeta, RawItem


def demo_inputs(now):
    url = "https://example.org/radar-demo"
    title = "테스트용 · 문서 검색 결과에 원문 링크 추가"
    what = "검색 결과에서 근거가 된 문단을 바로 열 수 있다."
    why = "답변을 검토할 때 근거가 나온 위치를 빠르게 찾을 수 있다."
    first = "The demo links each search result to the original source paragraph."
    second = "Reviewers can quickly locate the evidence for each answer."
    text = f"{first} {second}"
    source = Source(
        id="demo",
        name="테스트용 소스",
        org="AI Radar 데모",
        type="rss",
        url=url,
        source_class="official",
        discovery_mode="keyword",
        supports_range=False,
        trust=5,
        status="candidate",
        expected_cadence_days=1,
        auth="none",
        terms_note="Synthetic preview data.",
    )
    item = RawItem(
        item_id=sha256(url.encode()).hexdigest(),
        source_id=source.id,
        source_class=source.source_class,
        url=url,
        canonical_url=url,
        title=title,
        published_at=now,
        first_seen_at=now,
    )
    doc = SourceDoc(
        text=text,
        evidence=EvidenceMeta(
            url=url,
            final_url=url,
            http_status=200,
            content_type="text/plain",
            fetched_at=now,
            text_sha256=sha256(text.encode()).hexdigest(),
            chars=len(text),
            truncated=False,
        ),
    )
    evaluation = EvalResult(
        scores=dict(practical=4, impact=4, novelty=3, trust=4, interest=4),
        title_ko=title,
        category="새 AI 서비스",
        tags=["문서 검색", "출처"],
        what=[what],
        why=[why],
        how=[],
        availability="unknown",
        access={"type": "unknown"},
        claims=[
            dict(
                role=role,
                sentence_index=0,
                text=sentence,
                evidence_url=url,
                evidence_quote=quote,
            )
            for role, sentence, quote in [("what", what, first), ("why", why, second)]
        ],
    )
    return (
        [CollectResult(source, "complete", [item])],
        lambda item, *, now: doc,
        lambda item, doc: evaluation,
    )
