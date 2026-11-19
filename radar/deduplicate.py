import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from difflib import SequenceMatcher
from fcntl import LOCK_EX, flock
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from .models import EmergingItem, EventMember, EventRecord, EventRevision, SourceRef
from .normalize import canonical_url, external_key, release_key, title_tokens
from .storage import upsert_jsonl
from .tagging import Taxonomy, rule_tags
from .trends import KST, record_trends

WINDOW = timedelta(hours=48)


class MergeConfig(Taxonomy):
    title_mode: Literal["merge", "related_only"] = "related_only"
    never_merge: list[tuple[str, str]] = Field(default_factory=list)
    force_merge: list[tuple[str, str]] = Field(default_factory=list)


def load_merge_config(directory: Path) -> MergeConfig:
    overrides = yaml.safe_load(
        (directory / "merge_overrides.yaml").read_text(encoding="utf-8")
    )
    taxonomy = yaml.safe_load((directory / "taxonomy.yaml").read_text(encoding="utf-8"))
    return MergeConfig.model_validate(overrides | taxonomy)


@dataclass
class GroupResult:
    new: list[EventRecord] = field(default_factory=list)
    revised: list[EventRecord] = field(default_factory=list)
    touched: list[EventRecord] = field(default_factory=list)
    merge_log: list[tuple[str, str, str]] = field(default_factory=list)
    observed: list[EventRecord] = field(default_factory=list)
    emerging: list[EmergingItem] = field(default_factory=list)


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _date(ref):
    return ref.published_at or ref.first_seen_at


def _source_ref(item, org, previous):
    fields = set(SourceRef.model_fields) - {"org", "snippet_sha256", "metrics"}
    data = item.model_dump(include=fields)
    if previous:
        data["first_seen_at"] = min(previous.first_seen_at, item.first_seen_at)
        data["published_at"] = previous.published_at or item.published_at
        if previous.source_updated_at and (
            not item.source_updated_at
            or previous.source_updated_at > item.source_updated_at
        ):
            data["source_updated_at"] = previous.source_updated_at
            data["title"] = previous.title
            data["source_tags"] = previous.source_tags
    return SourceRef(
        **data,
        org=org,
        snippet_sha256=(
            previous.snippet_sha256
            if previous and data["source_updated_at"] != item.source_updated_at
            else _hash(item.snippet)
        ),
        metrics={"hn_points": item.metrics.hn_points},
    )


def _fingerprint(event):
    entries = sorted(
        (member.ref.item_id, title_tokens(member.ref.title), member.ref.snippet_sha256)
        for member in event.members
        if member.role in {"primary", "official"}
    )
    return _hash(json.dumps(entries, ensure_ascii=False, separators=(",", ":")))


def _pairs(pairs):
    result = defaultdict(set)
    for left, right in pairs:
        left, right = canonical_url(left), canonical_url(right)
        result[left].add(right)
        result[right].add(left)
    return result


def _entities(tokens, aliases):
    text = " " + " ".join(tokens) + " "
    return {
        key for key, terms in aliases.items() if any(term in text for term in terms)
    }


def _numbers(tokens):
    return {token for token in tokens if any(char.isdigit() for char in token)}


def _title_score(left, right):
    first, second = sorted((" ".join(sorted(left)), " ".join(sorted(right))))
    matcher = SequenceMatcher(None, first, second, autojunk=False)
    return (
        matcher.ratio()
        if matcher.real_quick_ratio() >= 0.85 and matcher.quick_ratio() >= 0.85
        else 0
    )


def _title_guard(left, right, left_tokens, right_tokens, aliases):
    if min(len(left_tokens), len(right_tokens)) < 4:
        return False
    if left.source_id == right.source_id or _numbers(left_tokens) != _numbers(
        right_tokens
    ):
        return False
    left_release = release_key(str(left.canonical_url))
    right_release = release_key(str(right.canonical_url))
    if left_release and right_release and left_release != right_release:
        return False
    entities = _entities(left_tokens, aliases)
    return bool(entities and entities == _entities(right_tokens, aliases))


def _keys(ref):
    url = canonical_url(ref.canonical_url)
    yield "id", ref.item_id
    yield "url", url
    if external := external_key(url):
        yield "external", external
    if ref.source_class == "aggregator" and ref.link_target_url:
        yield "target", canonical_url(ref.link_target_url)


def group(items, current_events, cfg: MergeConfig, *, now, orgs=None, trust=None):
    orgs, trust = orgs or {}, trust or {}
    prior = {event.event_id: event for event in current_events}
    events = prior.copy()
    index = defaultdict(set)
    dates = defaultdict(set)
    positives, negatives = _pairs(cfg.force_merge), _pairs(cfg.never_merge)
    aliases = {
        name: [
            " " + " ".join(tokens) + " "
            for term in terms
            if (tokens := title_tokens(term))
        ]
        for name, terms in cfg.entities.items()
    }
    result = GroupResult()
    observed = set()

    def index_event(event):
        for member in event.members:
            for key in _keys(member.ref):
                index[key].add(event.event_id)
            dates[_date(member.ref).astimezone(timezone.utc).date()].add(event.event_id)

    for event in events.values():
        index_event(event)

    ordered = sorted(
        items,
        key=lambda item: (
            item.source_class == "aggregator",
            item.source_class != "official",
            -trust.get(item.source_id, 0),
            _date(item),
            item.item_id,
        ),
    )
    for item in ordered:
        url = canonical_url(item.canonical_url)
        blocked = {
            event_id
            for other in negatives.get(url, ())
            for event_id in index["url", other]
        }
        forced = {
            event_id
            for other in positives.get(url, ())
            for event_id in index["url", other]
        }
        exact = [
            ("item_id", index["id", item.item_id]),
            ("force_merge", forced),
            ("canonical_url", index["url", url]),
        ]
        if external := external_key(url):
            exact.append(("external_id", index["external", external]))
        discussion = index["target", url].copy()
        if item.source_class == "aggregator" and item.link_target_url:
            target = canonical_url(item.link_target_url)
            discussion.update(index["url", target], index["target", target])
        exact.append(("discussion_target", discussion))
        match, reason = None, None
        for label, choices in exact:
            candidates = choices if label == "item_id" else choices - blocked
            if candidates:
                match = min(
                    candidates, key=lambda key: (events[key].first_seen_at, key)
                )
                reason = label
                break

        related = set()
        if match is None:
            date = _date(item)
            day = date.astimezone(timezone.utc).date()
            nearby = set().union(
                *(dates[day + timedelta(days=offset)] for offset in range(-2, 3))
            )
            tokens = title_tokens(item.title)
            likely = []
            for key in nearby:
                for member in events[key].members:
                    ref = member.ref
                    if abs(date - _date(ref)) > WINDOW:
                        continue
                    other = title_tokens(ref.title)
                    score = _title_score(tokens, other)
                    if score >= 0.85:
                        related.add(key)
                    if (
                        key not in blocked
                        and cfg.title_mode == "merge"
                        and score >= 0.92
                        and _title_guard(item, ref, tokens, other, aliases)
                    ):
                        likely.append((-score, events[key].first_seen_at, key))
            if likely:
                match, reason = min(likely)[2], "title"

        event = events.get(match)
        org = orgs.get(item.source_id, item.source_id)
        if event is None:
            event_id = _hash(url)
            if event_id in events:
                event_id = _hash(f"{url}|{item.item_id}")
            event = EventRecord(
                event_id=event_id,
                revision=1,
                fingerprint="",
                primary_item_id=item.item_id,
                title=item.title,
                members=[
                    EventMember(
                        ref=_source_ref(item, org, None),
                        role="primary",
                        merge_reason="first_seen",
                    )
                ],
                taxonomy_version=cfg.version,
                first_seen_at=item.first_seen_at,
                revision_history=[
                    EventRevision(revision=1, revised_at=now, reason="first_seen")
                ],
                last_activity_at=now,
                analysis_status="none",
            )
        else:
            event_id = event.event_id
            members = list(event.members)
            previous = next(
                (member for member in members if member.ref.item_id == item.item_id),
                None,
            )
            ref = _source_ref(item, org, previous.ref if previous else None)
            changes = {}
            if previous:
                if ref != previous.ref:
                    members[members.index(previous)] = previous.model_copy(
                        update={"ref": ref}
                    )
                    changes["members"] = members
                    if previous.role == "primary":
                        changes["title"] = ref.title
            else:
                primary = next(member for member in members if member.role == "primary")
                role = {"official": "official", "aggregator": "discussion"}.get(
                    item.source_class, "coverage"
                )
                if (
                    item.source_class != "aggregator"
                    and primary.ref.source_class == "aggregator"
                ):
                    members[members.index(primary)] = primary.model_copy(
                        update={"role": "discussion"}
                    )
                    changes.update(primary_item_id=item.item_id, title=ref.title)
                    role = "primary"
                members.append(EventMember(ref=ref, role=role, merge_reason=reason))
                changes.update(
                    members=members, last_activity_at=max(event.last_activity_at, now)
                )
                result.merge_log.append((item.item_id, event_id, reason))
            event = event.model_copy(update=changes)

        related.discard(event_id)
        if related:
            event = event.model_copy(
                update={
                    "related_event_ids": sorted(set(event.related_event_ids) | related)
                }
            )
        events[event_id] = event
        index_event(event)
        observed.add(event_id)

    for event in events.values():
        for key in event.related_event_ids:
            other = events.get(key)
            if other and event.event_id not in other.related_event_ids:
                events[key] = other.model_copy(
                    update={
                        "related_event_ids": sorted(
                            [*other.related_event_ids, event.event_id]
                        )
                    }
                )

    for key, event in sorted(events.items()):
        original = prior.get(key)
        event = event.model_copy(
            update={"rule_tags": rule_tags(event, cfg), "taxonomy_version": cfg.version}
        )
        if key in observed:
            fingerprint = _fingerprint(event)
            event = event.model_copy(update={"fingerprint": fingerprint})
            if original and original.fingerprint != fingerprint:
                revision = original.revision + 1
                event = event.model_copy(
                    update={
                        "revision": revision,
                        "revision_history": original.revision_history
                        + [
                            EventRevision(
                                revision=revision,
                                revised_at=max(original.last_activity_at, now),
                                reason="primary_or_official_changed",
                            )
                        ],
                        "last_activity_at": max(event.last_activity_at, now),
                        "analysis_status": "none",
                    }
                )
                result.revised.append(event)
            result.observed.append(event)
        if original is None:
            result.new.append(event)
        elif event.revision == original.revision and event != original:
            result.touched.append(event)
    return result


def load_events(data: Path) -> list[EventRecord]:
    current = []
    for path in sorted((data / "state/events").glob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            current.extend(
                EventRecord.model_validate_json(line) for line in stream if line.strip()
            )
    return current


def record_events(
    items,
    data: Path,
    cfg: MergeConfig,
    *,
    now,
    orgs=None,
    trust=None,
    active_sources=None,
    before_trends=None,
):
    directory = data / "state/events"
    staging = data / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    with (staging / "events.lock").open("a") as lock:
        flock(lock, LOCK_EX)
        current = load_events(data)
        result = group(items, current, cfg, now=now, orgs=orgs, trust=trust)
        shards = defaultdict(list)
        for event in result.new + result.revised + result.touched:
            month = event.first_seen_at.astimezone(KST).strftime("%Y-%m")
            shards[month].append(event.model_dump(mode="json"))
        for month, rows in sorted(shards.items()):
            upsert_jsonl(directory / f"{month}.jsonl", rows, "event_id")
        if active_sources is not None:
            if before_trends:
                before_trends()
            events = {event.event_id: event for event in current}
            events.update(
                (event.event_id, event)
                for event in result.new + result.revised + result.touched
            )
            result.emerging = record_trends(
                data / "state/trends",
                now.astimezone(KST).date(),
                events.values(),
                active_sources,
                set(cfg.entities) | set(cfg.source_tag_map.values()),
            )
        return result
