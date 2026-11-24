from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import Field, model_validator

from .collect import Source

from .models import Contract, EventRecord, Pool
from .normalize import canonical_url, external_key, hn_discussion_only, title_tokens

from .queue import QUEUE as QUEUE
from .queue import RETRY_WINDOW as RETRY_WINDOW
from .queue import QueueEntry as QueueEntry
from .verify import JudgeMode

POOLS: tuple[Pool, ...] = ("interest", "impact", "explore")
NOISE = ("ask hn hiring", "ask hn who is hiring", "ask hn who wants to be hired")
FRESH = timedelta(hours=48)


class InterestCategory(Contract):
    id: str
    name: str
    weight: int = Field(gt=0)
    aliases: list[str] = Field(min_length=1)


class Interests(Contract):
    categories: list[InterestCategory] = Field(min_length=1)
    threshold: int = Field(gt=0)
    ai_tags: set[str]
    ai_terms: list[str]
    ai_sources: set[str] = Field(default_factory=set)


class Pipeline(Contract):
    min: int = Field(ge=1)
    target: int = Field(ge=1)
    max: int = Field(ge=1)
    pool_percent: dict[Pool, int]
    hn_min_points: int = Field(ge=1)
    impact_top: int = Field(ge=1)
    judge_mode: JudgeMode = "normal"
    explore_min_score: float = Field(default=70, ge=0, le=100)
    explore_min_prescore: int = Field(default=0, ge=0)
    min_card_score: float = Field(default=0, ge=0, le=100)
    exclude: dict[str, list[str]] = Field(default_factory=dict)
    keep: list[str] = Field(default_factory=list)
    signal_sources: set[str] = Field(default_factory=set)

    @model_validator(mode="after")
    def check_limits(self):
        if not self.min <= self.target <= self.max:
            raise ValueError("Candidate limits must satisfy min <= target <= max")
        if (
            set(self.pool_percent) != set(POOLS)
            or sum(self.pool_percent.values()) != 100
            or min(self.pool_percent.values()) <= 0
        ):
            raise ValueError(
                "Candidate pool percentages must be positive and total 100"
            )
        return self


@dataclass(frozen=True)
class CandidateSettings:
    interests: Interests
    pipeline: Pipeline


@dataclass(frozen=True)
class Candidate:
    event: EventRecord
    pool: Pool
    pool_tags: tuple[Pool, ...]
    prescore: int
    reasons: tuple[str, ...]
    retry: bool

    @property
    def event_id(self):
        return self.event.event_id

    @property
    def revision(self):
        return self.event.revision


@dataclass(frozen=True)
class CandidatePlan:
    selected: list[Candidate]
    deferred: list[Candidate]
    excluded: dict[str, int] = field(default_factory=dict)


def load_candidate_settings(directory: Path) -> CandidateSettings:
    return CandidateSettings(
        Interests.model_validate(
            yaml.safe_load((directory / "interests.yaml").read_text(encoding="utf-8"))
        ),
        Pipeline.model_validate(
            yaml.safe_load((directory / "pipeline.yaml").read_text(encoding="utf-8"))
        ),
    )


def primary_ref(event):
    return next(member.ref for member in event.members if member.role == "primary")


def freshness(event):
    ref = primary_ref(event)
    return (
        event.revision_history[-1].revised_at
        if event.revision > 1
        else ref.published_at or ref.first_seen_at
    )


def _phrases(terms):
    return [f" {' '.join(title_tokens(term))} " for term in terms]


def exclusion_reason(title: str, pipeline: Pipeline) -> str | None:
    """The first exclude reason whose phrase is in the title, unless a keep phrase is."""
    phrase = f" {' '.join(title_tokens(title))} "
    if any(term in phrase for term in _phrases(pipeline.keep)):
        return None
    for reason, terms in pipeline.exclude.items():
        if any(term in phrase for term in _phrases(terms)):
            return reason
    return None


def interest_categories(title: str, interests: Interests) -> list[str]:
    """Category ids whose aliases appear as whole words in the title."""
    phrase = f" {' '.join(title_tokens(title))} "
    return [
        category.id
        for category in interests.categories
        if any(
            f" {' '.join(title_tokens(alias))} " in phrase for alias in category.aliases
        )
    ]


def _eligible(events, sources, now, settings, queue, shown, excluded, descriptions):
    interests, pipeline = settings.interests, settings.pipeline
    latest = {}
    for event in events:
        prior = latest.get(event.event_id)
        if prior is None or prior.revision <= event.revision:
            latest[event.event_id] = event
    # AI tag names count as title words too, so the gate does not depend on taxonomy.
    ai_phrases = _phrases([*interests.ai_terms, *interests.ai_tags])
    for event in latest.values():
        ref = primary_ref(event)
        entry = queue.get(event.event_id)
        same_revision = entry is not None and entry.revision == event.revision
        if (
            ref.source_id not in sources
            or event.analysis_status in {"done", "failed", "expired"}
            or (event.event_id, event.revision) in shown
            or (entry and entry.revision > event.revision)
            or (same_revision and entry.status in {"done", "failed", "expired"})
            or (
                same_revision
                and entry.next_attempt_after
                and entry.next_attempt_after > now
            )
        ):
            continue
        retry = bool(
            entry
            and entry.status in {"queued", "deferred"}
            and entry.revision <= event.revision
        )
        age = now - freshness(event)
        if (
            not timedelta(0)
            <= age
            <= (RETRY_WINDOW if retry and same_revision else FRESH)
        ):
            continue
        title = " ".join(title_tokens(event.title))
        if any(title == prefix or title.startswith(prefix + " ") for prefix in NOISE):
            continue
        if hn_discussion_only(ref):
            continue
        if sources[ref.source_id].type == "mcp_registry" and not ref.link_target_url:
            # Registry entries without a repository have no readable text to judge.
            excluded["no_repository"] += 1
            continue
        if reason := exclusion_reason(event.title, pipeline):
            excluded[reason] += 1
            continue
        tags = set(event.rule_tags)
        context = event.title + " " + descriptions.get(event.event_id, "")
        context_tokens = " " + " ".join(title_tokens(context)) + " "
        if (
            not sources[ref.source_id].editorial_ai
            and not tags.intersection(interests.ai_tags)
            and not any(phrase in context_tokens for phrase in ai_phrases)
            and not (
                any(m.ref.source_id in interests.ai_sources for m in event.members)
                and interest_categories(context, interests)
            )
        ):
            continue
        if (
            all(m.ref.source_id in pipeline.signal_sources for m in event.members)
            and not interest_categories(context, interests)
            and len({m.ref.org for m in event.members}) < 2
            and max((m.ref.metrics.hn_points or 0 for m in event.members), default=0)
            < pipeline.hn_min_points
        ):
            excluded["no_signal"] += 1
            continue
        yield event, sources[ref.source_id], tags, retry


def _impact_signals(eligible, pipeline, growth, current_stars):
    points = {}
    repositories = []
    for event, _, _, _ in eligible:
        count = max((m.ref.metrics.hn_points or 0 for m in event.members), default=0)
        if count >= pipeline.hn_min_points:
            points[event.event_id] = count
        if growth and (key := external_key(str(primary_ref(event).canonical_url))):
            kind, repo = key
            if kind == "github_repo":
                repositories.append((repo, event.event_id))
    activity = sorted(points, key=lambda key: (-points[key], key))[
        : pipeline.impact_top
    ]
    gains = {}
    repos = {repo for repo, _ in repositories}
    shortlist = sorted(repos, key=lambda repo: (-current_stars.get(repo, 0), repo))[:20]
    for repo in shortlist:
        if (value := growth(repo)) is not None and value > 0:
            gains[repo] = value
    top_repos = set(
        sorted(gains, key=lambda repo: (-gains[repo], repo))[: pipeline.impact_top]
    )
    return (
        {event_id: points[event_id] for event_id in activity},
        {event_id: gains[repo] for repo, event_id in repositories if repo in top_repos},
    )


def github_stars(results):
    stars = {}
    for result in results:
        for item in result.items:
            if item.metrics.gh_stars is None:
                continue
            key = external_key(str(item.canonical_url))
            if key and key[0] == "github_repo":
                stars[key[1]] = item.metrics.gh_stars
    return stars


def _allocate(ranked, n, percentages, cached, paid_limit):
    quotas = {pool: n * percentages[pool] // 100 for pool in POOLS}
    leftovers = sorted(POOLS, key=lambda pool: -(n * percentages[pool] % 100))
    for pool in leftovers[: n - sum(quotas.values())]:
        quotas[pool] += 1
    selected = []
    used = set()
    explored = set()
    counts = Counter()
    paid = 0

    def available(candidate, pool):
        return (
            candidate.event_id not in used
            and (candidate.event_id in cached or paid < paid_limit)
            and pool in candidate.pool_tags
            and (
                pool != "explore"
                or primary_ref(candidate.event).source_id not in explored
            )
        )

    def choose(candidate, pool):
        nonlocal paid
        paid += candidate.event_id not in cached
        selected.append(replace(candidate, pool=pool))
        used.add(candidate.event_id)
        counts[pool] += 1
        if pool == "explore":
            explored.add(primary_ref(candidate.event).source_id)

    if n == 0:
        return selected
    fresh = [c for c in ranked if not c.retry or c.event_id in cached]
    retry_slots = n // 3
    if not fresh and paid_limit:
        # A run with only waiting work must be able to advance one retry.
        retry_slots = max(1, retry_slots)
    for candidate in (c for c in ranked if c.retry and c.event_id not in cached):
        if len(selected) >= retry_slots:
            break
        for pool in sorted(
            candidate.pool_tags, key=lambda pool: -quotas[pool] + counts[pool]
        ):
            if available(candidate, pool):
                choose(candidate, pool)
                break

    for minimum in (1, 2):
        for pool in POOLS:
            if len(selected) == n or counts[pool] >= minimum:
                continue
            options = [c for c in fresh if available(c, pool)]
            if options:
                choose(min(options, key=lambda c: len(c.pool_tags)), pool)
    for pool in ("interest", "explore", "impact"):
        for candidate in fresh:
            if len(selected) == n or counts[pool] >= quotas[pool]:
                break
            if available(candidate, pool):
                choose(candidate, pool)
    for candidate in fresh:
        if len(selected) == n:
            break
        for pool in candidate.pool_tags:
            if available(candidate, pool):
                choose(candidate, pool)
                break
    return selected


def build_candidates(
    events: Iterable[EventRecord],
    sources: Mapping[str, Source],
    now: datetime,
    settings: CandidateSettings,
    *,
    budget_left: Decimal,
    estimate: Decimal,
    headline_reserve: Decimal = Decimal(0),
    call_slots: int = 50,
    queue: Mapping[str, QueueEntry] | None = None,
    shown: set[tuple[str, int]] | None = None,
    emerging_terms: Iterable[str] = (),
    growth: Callable[[str], int | None] | None = None,
    current_stars: Mapping[str, int] | None = None,
    descriptions: Mapping[str, str] | None = None,
    cached: set[str] | None = None,
) -> CandidatePlan:
    interests, pipeline = settings.interests, settings.pipeline
    if estimate <= 0:
        raise ValueError("The evaluation cost estimate must be positive")
    affordable = max(
        0, min(call_slots, int((budget_left - headline_reserve) // estimate))
    )
    excluded = Counter()
    descriptions = descriptions or {}
    eligible = list(
        _eligible(
            events,
            sources,
            now,
            settings,
            queue or {},
            shown or set(),
            excluded,
            descriptions,
        )
    )
    points, gains = _impact_signals(
        eligible, pipeline, growth if affordable else None, current_stars or {}
    )
    ranked = []
    emerging = set(emerging_terms)
    for event, source, tags, retry in eligible:
        weights = {category.id: category.weight for category in interests.categories}
        context = event.title + " " + descriptions.get(event.event_id, "")
        matches = set(interest_categories(context, interests))
        interest = sum(weights[match] for match in matches)
        independent = min(
            len({member.ref.org for member in event.members}),
            len(
                {
                    canonical_url(
                        member.ref.link_target_url or member.ref.canonical_url
                    )
                    for member in event.members
                }
            ),
        )
        official = any(
            member.role in {"primary", "official"}
            and member.ref.source_class == "official"
            for member in event.members
        )
        hn = points.get(event.event_id, 0)
        stars = gains.get(event.event_id, 0)
        trending = (tags | set(event.concept_tags)) & emerging
        pools = []
        reasons = []
        if interest >= interests.threshold:
            pools.append("interest")
            reasons.extend(f"interest:{tag}" for tag in sorted(matches))
        else:
            if trending or not matches or source.discovery_mode == "open":
                pools.append("explore")
                reasons.extend(f"emerging:{tag}" for tag in sorted(trending))
                reasons.append(
                    "open_source"
                    if source.discovery_mode == "open"
                    else "outside_interests"
                )
        if official or independent >= 3 or hn or stars:
            pools.append("impact")
            if official:
                reasons.append("official")
            if independent >= 3:
                reasons.append(f"distinct_documents:{independent}")
            if hn:
                reasons.append(f"hn_points:{hn}")
            if stars:
                reasons.append(f"star_growth_7d:{stars}")
        score = interest * 10 + source.trust + 5 * bool(trending) + 5 * official
        score += min(independent, 5) + min(hn // 25, 10) + min(stars, 10)
        if (
            "explore" in pools
            and not trending
            and not source.editorial_ai
            and score < pipeline.explore_min_prescore
        ):
            # Outside-interest items need a new-trend concept or a strong signal.
            pools.remove("explore")
        if pools:
            ranked.append(
                Candidate(event, pools[0], tuple(pools), score, tuple(reasons), retry)
            )

    ranked.sort(
        key=lambda c: (-c.prescore, -freshness(c.event).timestamp(), c.event_id)
    )
    per_source = Counter()
    capped = []
    for candidate in ranked:
        source = sources[primary_ref(candidate.event).source_id]
        if source.max_candidates and per_source[source.id] >= source.max_candidates:
            excluded["source_cap"] += 1
            continue
        per_source[source.id] += 1
        capped.append(candidate)
    ranked = capped
    target = min(pipeline.target, pipeline.max, len(ranked))
    cached = cached or set()
    free = sum(candidate.event_id in cached for candidate in ranked)
    n = min(target, affordable + free)
    selected = _allocate(ranked, n, pipeline.pool_percent, cached, affordable)
    if affordable:
        return CandidatePlan(selected, [], dict(excluded))
    new = [c for c in ranked if not c.retry and c.event_id not in cached]
    waiting = sum(entry.status == "deferred" for entry in (queue or {}).values())
    return CandidatePlan(selected, new[: max(0, target - waiting)], dict(excluded))
