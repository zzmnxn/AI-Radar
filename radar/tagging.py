import re
import unicodedata

from pydantic import Field

from .models import Contract, EventRecord
from .normalize import title_tokens

WORD = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)
CAMEL_CASE = re.compile(r"[a-z][A-Z]")


class Taxonomy(Contract):
    version: str = "1"
    entities: dict[str, list[str]] = Field(default_factory=dict)
    source_tag_map: dict[str, str] = Field(default_factory=dict)
    stop_words: list[str] = Field(default_factory=list)


def rule_tags(event: EventRecord, taxonomy: Taxonomy) -> list[str]:
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
    known = set().union(*aliases.values())
    stops = set(taxonomy.stop_words) | known
    candidates = set()
    for title in {event.title, *(member.ref.title for member in event.members)}:
        phrase = " " + " ".join(title_tokens(title)) + " "
        tags.update(
            name
            for name, terms in aliases.items()
            if any(f" {term} " in phrase for term in terms)
        )
        words = WORD.findall(unicodedata.normalize("NFKC", title))
        normalized = [word.casefold() for word in words]
        for index, word in enumerate(normalized):
            if not any(char.isalpha() for char in word) or word in stops:
                continue
            original = words[index]
            if (
                "-" in word
                or CAMEL_CASE.search(original)
                or (len(original) >= 3 and original.isupper())
            ):
                candidates.add(word)
            for width in (2, 3):
                window = normalized[index : index + width]
                if len(window) == width and all(
                    term not in stops and any(char.isalpha() for char in term)
                    for term in window
                ):
                    candidates.add(" ".join(window))
    return sorted(tags | (candidates - known))
