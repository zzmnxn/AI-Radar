from .models import Briefing, IndexEntry


def briefing_path(day):
    return f"briefings/{day.isoformat()}.json"


def assemble(*, day, run_id, now, counts, cards, emerging, headline=None):
    title = "주목할 변화 적음"
    if cards:
        title = (headline(cards) if headline else None) or "오늘의 AI 브리핑"
    return Briefing(
        date=day,
        run_id=run_id,
        generated_at=now,
        counts={**counts, "selected": len(cards)},
        headline_ko=title,
        cards=cards,
        emerging=list(emerging),
    )


def update_index(index, briefing):
    entry = IndexEntry(
        path=briefing_path(briefing.date),
        headline_ko=briefing.headline_ko,
        card_count=len(briefing.cards),
        titles=[card.title_ko for card in briefing.cards],
        categories=sorted({card.category for card in briefing.cards}),
        tags=sorted({tag for card in briefing.cards for tag in card.tags}),
    )
    return {**index, briefing.date.isoformat(): entry}
