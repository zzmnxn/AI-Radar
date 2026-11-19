from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from .models import Contract, Count, EmergingItem, EventRecord
from .storage import write_json_atomic

KST = ZoneInfo("Asia/Seoul")


class TrendRow(Contract):
    date: date
    tag: str
    kind: Literal["taxonomy", "candidate"]
    events: Count
    independent_orgs: Count
    active_sources: Count
    # Retain evidence for unique event and org counts across discovery days.
    event_ids: list[str] = Field(default_factory=list)
    orgs: list[str] = Field(default_factory=list)


class TrendDay(Contract):
    date: date
    active_sources: Count
    rows: list[TrendRow]


def rebuild_day(
    day: date,
    events_state: Iterable[EventRecord],
    active_sources: int,
    *,
    taxonomy_tags: Iterable[str] = (),
) -> list[TrendRow]:
    known = set(taxonomy_tags)
    event_ids = defaultdict(set)
    orgs = defaultdict(set)
    for event in events_state:
        member_days = [
            (member.ref.first_seen_at.astimezone(KST).date(), member.ref.org)
            for member in event.members
        ]
        if (
            event.first_seen_at.astimezone(KST).date() != day
            and all(
                revision.revised_at.astimezone(KST).date() != day
                for revision in event.revision_history
            )
            and all(seen != day for seen, _ in member_days)
        ):
            continue
        for tag in event.rule_tags:
            event_ids[tag].add(event.event_id)
            orgs[tag].update(org for seen, org in member_days if seen <= day)
    return [
        TrendRow(
            date=day,
            tag=tag,
            kind="taxonomy" if tag in known else "candidate",
            events=len(ids),
            independent_orgs=len(orgs[tag]),
            active_sources=active_sources,
            event_ids=sorted(ids),
            orgs=sorted(orgs[tag]),
        )
        for tag, ids in sorted(event_ids.items())
    ]


def emerging(history: Iterable[TrendDay], today: date) -> list[EmergingItem]:
    recent = Counter()
    baseline = Counter()
    recent_exposure = baseline_exposure = baseline_days = 0
    first_seen = {}
    known = set()
    discovery_ids = defaultdict(set)
    discovery_orgs = defaultdict(set)
    for day in sorted(history, key=lambda day: day.date):
        age = (today - day.date).days
        if age < 0:
            continue
        if age < 7:
            recent_exposure += day.active_sources
        elif age < 97 and day.active_sources:
            baseline_exposure += day.active_sources
            baseline_days += 1
        for row in day.rows:
            first_seen.setdefault(row.tag, day.date)
            if row.kind == "taxonomy":
                known.add(row.tag)
            if age < 14:
                discovery_ids[row.tag].update(row.event_ids)
                discovery_orgs[row.tag].update(row.orgs)
            if day.active_sources:
                if age < 7:
                    recent[row.tag] += row.events
                elif age < 97:
                    baseline[row.tag] += row.events

    ranked = []
    discoveries = set()
    for tag, ids in discovery_ids.items():
        org_count = len(discovery_orgs[tag])
        if (
            tag not in known
            and (today - first_seen[tag]).days < 14
            and len(ids) >= 2
            and org_count >= 2
        ):
            discoveries.add(tag)
            ranked.append(
                (
                    0,
                    -len(ids),
                    -org_count,
                    tag,
                    EmergingItem(
                        tag=tag,
                        kind="discovery",
                        reason_ko=f"최근 14일 동안 독립 조직 {org_count}곳의 사건 {len(ids)}건에서 처음 나타났습니다.",
                    ),
                )
            )

    if baseline_days >= 30 and recent_exposure:
        for tag, count in recent.items():
            prior = baseline[tag]
            if (
                tag in discoveries
                or count < 3
                or count * baseline_exposure < 2 * prior * recent_exposure
            ):
                continue
            reason = (
                f"최근 7일 사건 {count}건으로, 소스 수를 보정한 발생률이 기준 기간의 2배 이상입니다."
                if prior
                else f"기준 기간에는 사건이 없었고 최근 7일에 {count}건이 발생했습니다."
            )
            ranked.append(
                (
                    1,
                    -count,
                    0,
                    tag,
                    EmergingItem(
                        tag=tag,
                        kind="watch",
                        reason_ko=reason,
                    ),
                )
            )
    return [entry[-1] for entry in sorted(ranked)[:3]]


def record_trends(
    directory: Path,
    today: date,
    events: Iterable[EventRecord],
    active_sources: int,
    taxonomy_tags: Iterable[str],
) -> list[EmergingItem]:
    history = {}
    for path in sorted(directory.glob("*.json")):
        day = TrendDay.model_validate_json(path.read_bytes())
        history[day.date] = day
    yesterday = today - timedelta(days=1)
    prior = history.get(yesterday)
    for day, sources in (
        (yesterday, prior.active_sources if prior else 0),
        (today, active_sources),
    ):
        snapshot = TrendDay(
            date=day,
            active_sources=sources,
            rows=rebuild_day(day, events, sources, taxonomy_tags=taxonomy_tags),
        )
        write_json_atomic(directory / f"{day}.json", snapshot.model_dump(mode="json"))
        history[day] = snapshot
    return emerging(history.values(), today)
