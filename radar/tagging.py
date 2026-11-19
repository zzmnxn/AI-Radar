from pydantic import Field

from .models import Contract, EventRecord
from .normalize import title_tokens


class Taxonomy(Contract):
    version: str = "1"
    entities: dict[str, list[str]] = Field(default_factory=dict)
    source_tag_map: dict[str, str] = Field(default_factory=dict)
    stop_words: list[str] = Field(default_factory=list)


def rule_tags(event: EventRecord, taxonomy: Taxonomy) -> list[str]:
    """Taxonomy tags only. New concepts come from LLM extraction, not title n-grams."""
    tags = {
        taxonomy.source_tag_map[tag.casefold()]
        for member in event.members
        for tag in member.ref.source_tags
        if tag.casefold() in taxonomy.source_tag_map
    }
    aliases = {
        name: {" ".join(title_tokens(alias)) for alias in [name, *terms]} - {""}
        for name, terms in taxonomy.entities.items()
    }
    for title in {event.title, *(member.ref.title for member in event.members)}:
        phrase = " " + " ".join(title_tokens(title)) + " "
        tags.update(
            name
            for name, terms in aliases.items()
            if any(f" {term} " in phrase for term in terms)
        )
    return sorted(tags)
