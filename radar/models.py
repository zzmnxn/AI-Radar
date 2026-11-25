from datetime import date
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    RootModel,
)

from .config import DEADLINE_KST, SCHEMA_VERSION

SourceClass = Literal["official", "community", "research", "product", "aggregator"]
Availability = Literal[
    "available", "beta", "preview", "research", "announced", "unknown"
]
AccessType = Literal[
    "open",
    "signup",
    "waitlist",
    "paid",
    "enterprise",
    "region_limited",
    "api_only",
    "unknown",
]
ClaimRole = Literal["what", "why", "how", "availability", "access"]
Pool = Literal["interest", "explore", "impact"]
Count = Annotated[int, Field(ge=0)]
Revision = Annotated[int, Field(ge=1)]
Rating = Annotated[int, Field(ge=0, le=5, description="정수 0~5")]
# Interest categories, in config/interests.yaml order, plus the fallback.
CATEGORIES = (
    "AI 코딩 도구",
    "에이전트 확장",
    "MCP·도구 연결",
    "에이전트 설계·스택",
    "컨텍스트·메모리",
    "GenUI·AI 인터페이스",
    "AI 개발 워크플로",
    "새 모델·API",
    "로컬·오픈 모델",
    "새 AI 서비스",
    "에이전트 작업 환경",
    "AI 활용 패턴",
    "기타",
)
Category = Annotated[
    Literal[CATEGORIES],
    BeforeValidator(lambda value: value if value in CATEGORIES else "기타"),
]
Rate = Annotated[float, Field(ge=0, le=1)]
Quote = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        description="원문에서 그대로 복사한 연속 구간, 20~200자",
    ),
]
Sentence = Annotated[
    str, Field(min_length=1, description="한국어 한 문장, 약 60자 이내")
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceMetrics(Contract):
    hn_points: Count | None = None


class RawMetrics(SourceMetrics):
    gh_stars: Count | None = None


class ItemMetadata(Contract):
    item_id: str
    source_id: str
    source_class: SourceClass
    url: HttpUrl
    canonical_url: HttpUrl
    link_target_url: HttpUrl | None = None
    title: str
    published_at: AwareDatetime | None = None
    source_updated_at: AwareDatetime | None = None
    first_seen_at: AwareDatetime


class RawItem(ItemMetadata):
    snippet: str = Field(default="", max_length=300)
    source_tags: list[str] = Field(default_factory=list)
    metrics: RawMetrics = Field(default_factory=RawMetrics)


class SourceRef(ItemMetadata):
    org: str
    snippet_sha256: str
    source_tags: list[str] = Field(default_factory=list)
    metrics: SourceMetrics = Field(default_factory=SourceMetrics)


class EventMember(Contract):
    ref: SourceRef
    role: Literal["primary", "official", "coverage", "discussion"]
    merge_reason: str


class EventRevision(Contract):
    revision: Revision
    revised_at: AwareDatetime
    reason: str


class EventRecord(Contract):
    event_id: str
    revision: Revision
    fingerprint: str
    primary_item_id: str
    title: str
    members: list[EventMember] = Field(min_length=1)
    related_event_ids: list[str] = Field(default_factory=list)
    rule_tags: list[str] = Field(default_factory=list)
    # New technical concepts named by the daily LLM extraction (lowercase).
    concept_tags: list[str] = Field(default_factory=list)
    taxonomy_version: str
    first_seen_at: AwareDatetime
    revision_history: list[EventRevision] = Field(min_length=1)
    last_activity_at: AwareDatetime
    analysis_status: Literal["none", "queued", "deferred", "done", "failed", "expired"]


class EvidenceMeta(Contract):
    url: HttpUrl
    final_url: HttpUrl
    http_status: Annotated[int, Field(ge=100, le=599)]
    content_type: str
    fetched_at: AwareDatetime
    text_sha256: str
    chars: Count
    truncated: bool


class Scores(Contract):
    practical: Rating
    impact: Rating
    novelty: Rating
    trust: Rating
    interest: Rating


class Access(Contract):
    type: AccessType
    note_ko: str | None = None


class Claim(Contract):
    role: ClaimRole
    sentence_index: Count | None = None
    text: str
    evidence_url: HttpUrl
    evidence_quote: Quote


class EvalResult(Contract):
    scores: Scores
    # Exit for items a practitioner would not act on; defaults keep stored rows valid.
    worth_briefing: bool = True
    skip_reason: str = Field(
        default="",
        max_length=80,
        description="worth_briefing이 false일 때 한 구절 사유",
    )
    category: Category
    tags: list[str]
    title_ko: str
    what: list[Sentence] = Field(min_length=1, max_length=2)
    why: list[Sentence] = Field(min_length=1, max_length=2)
    how: list[Sentence] = Field(max_length=3)
    availability: Availability
    access: Access
    claims: list[Claim]
    uncertainty: str | None = None


class ClaimCheck(Contract):
    role: ClaimRole
    sentence_index: Count | None = None
    quote_found: bool
    numbers_match: bool
    support: Literal["supported", "partial", "unsupported", "nei", "not_checked"]


class Verification(Contract):
    link_ok: bool
    level: Literal["verified", "partial"]
    claims: list[ClaimCheck]
    quote_found_rate: Rate
    supported_rate: Rate


class Usage(Contract):
    input_tokens: Count
    output_tokens: Count


class AnalysisRecord(Contract):
    analysis_id: str
    event_id: str
    revision: Revision
    analyzer_version: str
    evidence: list[EvidenceMeta]
    eval: EvalResult
    verification: Verification
    verdict: Literal["accepted", "partial", "rejected"]
    rejection_reason: str | None = None
    usage: Usage
    created_at: AwareDatetime


class CardSource(Contract):
    source_id: str
    source_class: SourceClass
    org: str
    url: HttpUrl
    title: str


class VerificationSummary(Contract):
    level: Literal["verified", "partial"]
    claims: list[Claim]
    uncertainty: str | None = None


class Card(Contract):
    card_id: str
    event_id: str
    revision: Revision
    analysis_id: str
    update_of: str | None = None
    title_ko: str
    category: str
    tags: list[str]
    published_at: AwareDatetime | None = None
    sources: list[CardSource]
    what: list[Sentence] = Field(min_length=1)
    why: list[Sentence]
    how: list[Sentence] = Field(max_length=3)
    availability: Availability
    access: Access
    verification_summary: VerificationSummary
    score: Annotated[float, Field(ge=0, le=100)]
    pool: Pool


class BriefingCounts(Contract):
    raw: Count
    events: Count
    candidates: Count
    analyzed: Count
    accepted: Count
    selected: Count
    deferred: Count


class EmergingSource(Contract):
    event_id: str
    title: str
    org: str
    url: HttpUrl
    published_at: AwareDatetime | None = None


class EmergingItem(Contract):
    tag: str
    kind: Literal["discovery", "watch"]
    reason_ko: str
    sources: list[EmergingSource] = Field(default_factory=list)


class Briefing(Contract):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    date: date
    run_id: str
    generated_at: AwareDatetime
    counts: BriefingCounts
    headline_ko: str
    cards: list[Card] = Field(max_length=5)
    emerging: list[EmergingItem] = Field(max_length=3)


class IndexEntry(Contract):
    path: str
    headline_ko: str
    card_count: Annotated[int, Field(ge=0, le=5)]
    categories: list[str]
    tags: list[str]
    titles: list[str] = Field(default_factory=list)


class YearIndex(RootModel[dict[str, IndexEntry]]):
    pass


class PublishedSnapshot(Contract):
    date: date
    run_id: str
    generated_at: AwareDatetime
    files: dict[str, Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]]


class SourceCounts(Contract):
    active: Count
    complete: Count
    partial: Count
    failed: Count


class LastRun(Contract):
    run_id: str
    date: date
    started_at: AwareDatetime
    finished_at: AwareDatetime
    outcome: Literal["ok", "failed", "skipped"]
    failed_stage: str | None = None
    message: str | None = None
    sources: SourceCounts


class Manifest(Contract):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    deadline_kst: Literal[DEADLINE_KST] = DEADLINE_KST
    published: PublishedSnapshot | None = None
    last_success_at: AwareDatetime | None = None
    last_run: LastRun
