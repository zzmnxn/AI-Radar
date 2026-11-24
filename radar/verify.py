from dataclasses import dataclass
from typing import Literal

from .enrich import SourceDoc, claim_window
from .http import FetchError
from .models import EvalResult, Verification

VERSION = "w06-v2"
Support = Literal["supported", "partial", "unsupported", "nei"]
JudgeMode = Literal["normal", "strict"]


@dataclass(frozen=True)
class VerifiedEvaluation:
    evaluation: EvalResult
    verification: Verification
    docs: list[SourceDoc]
    rejection_reason: str | None


def _live(doc, check_link):
    try:
        return check_link(str(doc.evidence.url))
    except FetchError:
        return False


def verify(evaluation, docs, judge, check_link, *, mode: JudgeMode = "normal"):
    live = [doc for doc in docs if _live(doc, check_link)]
    link_ok = bool(live) and live[0] is docs[0]
    sections = {role: [] for role in ("what", "why", "how")}
    targets = {
        (role, index): sentence
        for role in sections
        for index, sentence in enumerate(getattr(evaluation, role))
    }
    targets.update(
        {
            ("availability", None): evaluation.availability,
            ("access", None): evaluation.access.type,
        }
    )
    checks = []
    rows = []
    pending = []
    used = set()
    uncertain = set()
    for claim in evaluation.claims:
        key = (claim.role, claim.sentence_index)
        doc = next(
            (doc for doc in live if doc.evidence.url == claim.evidence_url), None
        )
        bound = key in targets and targets[key] == claim.text
        quote, window, numbers_match = (
            claim_window(claim, doc) if doc and bound else ("", "", False)
        )
        checks.append(
            dict(
                role=claim.role,
                sentence_index=claim.sentence_index,
                quote_found=bool(quote),
                numbers_match=numbers_match,
                support="not_checked",
            )
        )
        if quote and not numbers_match:
            uncertain.add(key)
        if link_ok and numbers_match and key not in used:
            used.add(key)
            pending.append((len(checks) - 1, key, claim, quote))
            rows.append(
                dict(role=claim.role, claim=claim.text, quote=quote, context=window)
            )

    accepted = {}
    if rows:
        verdicts = judge(rows)
        if verdicts is None or len(verdicts) != len(rows):
            return None
        for (index, key, claim, quote), support in zip(pending, verdicts, strict=True):
            checks[index]["support"] = support
            if support == "supported":
                accepted[key] = claim.model_copy(update={"evidence_quote": quote})
            elif support in {"partial", "nei"}:
                uncertain.add(key)

    claims = []
    for key, sentence in targets.items():
        if key not in accepted:
            continue
        role, _ = key
        claim = accepted[key]
        if role in sections:
            claim = claim.model_copy(update={"sentence_index": len(sections[role])})
            sections[role].append(sentence)
        claims.append(claim)
    uncertain.difference_update(accepted)
    if not sections["why"]:
        uncertain.add(("why", None))
    level = "partial" if uncertain else "verified"
    reason = None
    if not link_ok:
        reason = "primary_link_failed"
    elif len(sections["what"]) != len(evaluation.what):
        reason = "what_unsupported"
    elif mode == "strict" and uncertain:
        reason = "partial_verification"
    for role in ("what", "why"):
        if not sections[role]:
            sections[role] = ["확인된 근거가 없습니다."]
    has_availability = ("availability", None) in accepted
    has_access = ("access", None) in accepted
    safe = evaluation.model_copy(
        update={
            **sections,
            "claims": claims,
            "availability": evaluation.availability if has_availability else "unknown",
            "access": evaluation.access.model_copy(
                update={
                    "type": evaluation.access.type if has_access else "unknown",
                    "note_ko": None,
                }
            ),
            "uncertainty": "일부 내용은 근거가 충분하지 않아 제외했습니다."
            if uncertain
            else None,
        }
    )
    count = len(checks)
    verification = Verification(
        link_ok=link_ok,
        level="partial" if reason else level,
        claims=checks,
        quote_found_rate=sum(c["quote_found"] for c in checks) / count if count else 0,
        supported_rate=sum(c["support"] == "supported" for c in checks) / count
        if count
        else 0,
    )
    return VerifiedEvaluation(safe, verification, live, reason)
