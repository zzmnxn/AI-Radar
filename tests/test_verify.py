from dataclasses import replace

import pytest

from radar.assemble import briefing_path
from radar.demo import demo_inputs
from radar.models import Briefing, Claim
from radar.verify import verify
from test_collectors import NOW


def sample():
    results, fetch, evaluate = demo_inputs(NOW)
    item = results[0].items[0]
    doc = fetch(item, now=NOW)
    return doc, evaluate(item, doc).model_copy(deep=True)


def test_fuzzy_quote_and_numbers_near_match():
    doc, result = sample()
    quote = "The tool currently supports version 3.2 in a paid beta."
    doc = replace(doc, text="9.2 " + "padding " * 55 + quote + " Upgrade instructions.")
    result.what = ["3.2 버전 지원", "9.2 버전 지원"]
    result.claims = [
        Claim(
            role="what",
            sentence_index=index,
            text=sentence,
            evidence_url=doc.evidence.url,
            evidence_quote=quote.replace("currently", "curently"),
        )
        for index, sentence in enumerate(result.what)
    ]
    calls = []

    def judge(rows):
        calls.extend(rows)
        return ["supported"]

    checked = verify(result, [doc], judge, lambda url: True)
    assert len(calls) == 1
    assert calls[0]["quote"] == quote
    assert "9.2" not in calls[0]["context"]
    assert checked.rejection_reason == "what_unsupported"
    assert [check.numbers_match for check in checked.verification.claims] == [
        True,
        False,
    ]


@pytest.mark.parametrize("support", ["partial", "nei", "unsupported"])
def test_why_all_removed_rejected(support):
    doc, result = sample()
    outcome = verify(
        result, [doc], lambda rows: ["supported", support], lambda url: True
    )
    assert outcome.rejection_reason == "why_unsupported"
    assert outcome.verification.claims[1].support == support


def test_how_empty_valid_and_failed_access_unknown():
    doc, result = sample()
    quote = "The service is available in a paid beta."
    doc = replace(doc, text=f"{doc.text} {quote}")
    result.access.type = "open"
    result.how = ["무료 플랜을 사용한다.", "설명에 없는 명령을 실행한다."]
    for role, index, sentence, evidence in [
        ("how", 0, result.how[0], quote),
        ("how", 1, result.how[1], "This fabricated quote does not exist."),
        ("access", None, "open", quote),
    ]:
        result.claims.append(
            Claim(
                role=role,
                sentence_index=index,
                text=sentence,
                evidence_url=doc.evidence.url,
                evidence_quote=evidence,
            )
        )

    def judge(rows):
        assert [row["role"] for row in rows] == ["what", "why", "how", "access"]
        return ["supported", "supported", "partial", "unsupported"]

    outcome = verify(result, [doc], judge, lambda url: True)
    assert outcome.rejection_reason is None
    assert outcome.evaluation.how == []
    assert outcome.evaluation.access.type == "unknown"
    assert outcome.verification.level == "partial"
    assert outcome.evaluation.uncertainty
    assert "무료" not in outcome.evaluation.uncertainty
    assert (
        verify(result, [doc], judge, lambda url: True, mode="strict").rejection_reason
        == "partial_verification"
    )


def test_primary_link_failure_rejects_without_judgment():
    doc, result = sample()

    def unexpected(rows):
        pytest.fail("judged a card with a broken primary link")

    checked = verify(result, [doc], unexpected, lambda url: False)
    assert checked.rejection_reason == "primary_link_failed"
    assert checked.verification.link_ok is False


def test_missing_judgment_stays_unverified():
    doc, result = sample()
    assert verify(result, [doc], lambda rows: None, lambda url: True) is None


def test_finalize_schema_invalid_rejected(tmp_path):
    from radar.slice import run_slice

    results, fetch, evaluate = demo_inputs(NOW)

    def invalid(item, doc):
        result = evaluate(item, doc).model_copy(deep=True)
        result.scores = result.scores.model_copy(update={"practical": 99})
        return result

    manifest = run_slice(
        results,
        NOW,
        tmp_path,
        fetch=fetch,
        evaluate=invalid,
        analyzer_version="fixture",
        limit=1,
    )
    assert manifest.last_run.outcome == "ok"
    payload = Briefing.model_validate_json(
        (tmp_path / briefing_path(manifest.published.date)).read_bytes()
    )
    assert payload.cards == []
    assert "schema_invalid=1" in manifest.last_run.message


def test_failed_source_link_disables_its_claims():
    from pydantic import HttpUrl

    doc, result = sample()
    source_url = HttpUrl("https://second.example/article")
    corroboration = replace(
        doc,
        evidence=doc.evidence.model_copy(
            update={"url": source_url, "final_url": source_url}
        ),
    )
    result.claims[1].evidence_url = source_url
    seen = []

    def check(url):
        seen.append(url)
        return url != str(source_url)

    outcome = verify(result, [doc, corroboration], lambda rows: ["supported"], check)
    assert seen == [str(doc.evidence.url), str(source_url)]
    assert outcome.rejection_reason == "why_unsupported"
    assert outcome.docs == [doc]
