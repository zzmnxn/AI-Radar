from collections import Counter
from datetime import timedelta

from .assemble import briefing_path
from .models import Briefing

SHOWN_DAYS = 120


def recent_shown(data, day, published):
    rows = []
    if published is None:
        return rows
    indexed = any(name.startswith("index/") for name in published.files)
    for offset in range(1, SHOWN_DAYS + 1):
        date = day - timedelta(days=offset)
        name = briefing_path(date)
        path = data / name
        if date <= published.date and (
            name in published.files if indexed else path.is_file()
        ):
            briefing = Briefing.model_validate_json(path.read_bytes())
            rows.extend(shown_rows(briefing))
    return rows


def shown_rows(briefing):
    return [
        dict(
            date=briefing.date.isoformat(),
            event_id=card.event_id,
            revision=card.revision,
            card_id=card.card_id,
        )
        for card in briefing.cards
    ]


def _pick(ranked, reserved=None):
    selected = []
    seen = set()
    sources = Counter()
    orgs = Counter()
    ordered = [reserved, *ranked] if reserved is not None else ranked
    for card in ordered:
        source_ids = {source.source_id for source in card.sources}
        organizations = {source.org.casefold().strip() for source in card.sources}
        if (
            card.event_id in seen
            or any(sources[key] >= 2 for key in source_ids)
            or any(orgs[key] >= 2 for key in organizations)
        ):
            continue
        selected.append(card)
        seen.add(card.event_id)
        sources.update(source_ids)
        orgs.update(organizations)
        if len(selected) == 5:
            break
    return selected


def _sort_key(card):
    return -card.score, card.event_id, -card.revision, card.card_id


def select(cards, shown, *, explore_min_score):
    seen = {(row["event_id"], row["revision"]) for row in shown}
    previous = {}
    for row in sorted(shown, key=lambda row: (row["revision"], row["date"])):
        previous[row["event_id"]] = row
    ranked = []
    for card in cards:
        if (card.event_id, card.revision) in seen:
            continue
        prior = previous.get(card.event_id)
        if prior:
            if prior["revision"] > card.revision:
                continue
            card = card.model_copy(update={"update_of": prior["card_id"]})
        ranked.append(card)
    ranked.sort(key=_sort_key)
    selected = _pick(ranked)
    explore = next(
        (
            card
            for card in ranked
            if card.pool == "explore" and card.score >= explore_min_score
        ),
        None,
    )
    if explore and not any(
        card.pool == "explore" and card.score >= explore_min_score for card in selected
    ):
        selected = _pick(ranked, reserved=explore)
    return sorted(selected, key=_sort_key)
