import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlsplit

from .http import ARTICLE_BYTES, FetchError, safe_get
from .models import Claim, EvidenceMeta, RawItem
from .normalize import ARXIV_ID, external_key, hn_discussion_only

ARTICLE_TYPES = frozenset({"application/xhtml+xml", "text/html", "text/plain"})
MAX_CHARS = 60_000
JSON_TYPES = frozenset({"application/json"})
# GitHub echoes the Accept header back as the content type, so ask for one type only.
README_TYPES = frozenset({"application/vnd.github.raw+json"})


class ArticleParser(HTMLParser):
    skip = {"script", "style", "nav", "footer", "template", "noscript", "svg", "head"}
    sections = {"article", "main"}
    blocks = {"p", "div", "li", "br", "h1", "h2", "h3", "section", "pre", "blockquote"}

    def __init__(self):
        super().__init__()
        self.excluded: list[str] = []
        self.section_depth = 0
        self.main: list[str] = []
        self.page: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.skip:
            self.excluded.append(tag)
        if self.excluded:
            return
        if tag in self.sections:
            self.section_depth += 1
        if tag in self.blocks:
            self.handle_data(" ")

    def handle_endtag(self, tag):
        if self.excluded:
            if tag == self.excluded[-1]:
                self.excluded.pop()
            return
        if tag in self.sections:
            self.handle_data(" ")
            self.section_depth = max(0, self.section_depth - 1)
        if tag in self.blocks:
            self.handle_data(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.excluded:
            self.page.append(data)
            if self.section_depth:
                self.main.append(data)


@dataclass(frozen=True)
class SourceDoc:
    text: str
    evidence: EvidenceMeta


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


NUMBER_PATTERN = re.compile(r"[0-9]+(?:[.,][0-9]+)*")
NUMBER_WORDS = {
    word: str(value)
    for value, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
} | {
    "hundred": "100",
    "thousand": "1000",
    "million": "1000000",
    "billion": "1000000000",
}
NUMBER_WORD_PATTERN = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", re.I)


def _numbers(text: str) -> set[str]:
    """Digit tokens plus English number words read as digits ("Three" -> "3")."""
    words = NUMBER_WORD_PATTERN.sub(lambda m: NUMBER_WORDS[m[1].lower()], text)
    return set(NUMBER_PATTERN.findall(words))


def enrich(item: RawItem, *, now: datetime, fetch=safe_get) -> SourceDoc:
    original = str(item.link_target_url or item.url)
    url = original
    accept = ARTICLE_TYPES
    options = {}
    json_field, json_html = None, False
    parts = urlsplit(original)
    key = external_key(original)
    if key and key[0] == "github_release":
        # Release notes from the API; the release page is mostly GitHub chrome.
        repo, _, tag = parts.path.strip("/").partition("/releases/tag/")
        url = (
            f"https://api.github.com/repos/{quote(repo, safe='/')}"
            f"/releases/tags/{quote(tag, safe='')}"
        )
        accept = JSON_TYPES
        json_field = "body"
        options["github_token"] = os.environ.get("GITHUB_TOKEN")
    elif key and key[0] == "github_repo":
        path = quote(parts.path.strip("/"), safe="/")
        url = f"https://api.github.com/repos/{path}/readme"
        accept = README_TYPES
        options["github_token"] = os.environ.get("GITHUB_TOKEN")
    elif parts.hostname == "arxiv.org":
        match = re.fullmatch(
            rf"/(?:abs|pdf)/({ARXIV_ID}(?:v\d+)?)(?:\.pdf)?/?", parts.path
        )
        if match:
            url = f"https://arxiv.org/abs/{match[1]}"
    elif parts.hostname == "news.ycombinator.com" and item.link_target_url is None:
        # Use the post body only; the HN page itself is mostly comments.
        hn_id = parse_qs(parts.query).get("id", [""])[0]
        if hn_discussion_only(item) or not hn_id.isdecimal():
            raise FetchError("discussion_only")
        url = f"https://hn.algolia.com/api/v1/items/{hn_id}"
        accept = JSON_TYPES
        json_field, json_html = "text", True
    response = fetch(url, max_bytes=ARTICLE_BYTES, accept=accept, **options)
    if response.body is None or not 200 <= response.status < 300:
        raise FetchError("http_error", response.status)

    headers = Message()
    headers["content-type"] = response.headers.get(
        "content-type", "application/octet-stream"
    )
    if headers.get_content_type() not in accept:
        raise FetchError("bad_content_type")
    charset = headers.get_content_charset() or "utf-8"
    try:
        text = response.body.decode(charset, errors="replace")
    except LookupError as exc:
        raise FetchError("unsupported_charset") from exc

    if json_field:
        try:
            text = json.loads(text).get(json_field) or ""
        except (ValueError, AttributeError) as exc:
            raise FetchError("bad_content_type") from exc
    if json_html or (
        not json_field
        and headers.get_content_type()
        in {
            "text/html",
            "application/xhtml+xml",
        }
    ):
        parser = ArticleParser()
        parser.feed(text)
        parser.close()
        text = "".join(parser.main) or "".join(parser.page)
    text = _normalized(text)
    if not text:
        raise FetchError("empty_article")
    excerpt = text[:MAX_CHARS]
    return SourceDoc(
        text=excerpt,
        evidence=EvidenceMeta(
            url=url if parts.hostname == "arxiv.org" else original,
            final_url=response.final_url,
            http_status=response.status,
            content_type=headers.get_content_type(),
            fetched_at=now,
            text_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
            chars=len(excerpt),
            truncated=len(text) > MAX_CHARS,
        ),
    )


def _quote_spans(quote, text):
    exact = list(re.finditer(re.escape(quote), text))
    if exact:
        yield from ((match.start(), match.end(), 1.0) for match in exact)
        return

    # A 95% match can contain at most this many insertions or deletions.
    delta = len(quote) * 2 // 19
    width = max(1, len(quote) // (delta + 1))
    starts = set()
    for offset in range(0, len(quote) - width + 1, width):
        seed = quote[offset : offset + width]
        for match in re.finditer(f"(?={re.escape(seed)})", text):
            start = match.start() - offset
            starts.update(
                range(max(0, start - delta), min(len(text), start + delta + 1))
            )

    masks = {}
    for index, char in enumerate(quote):
        masks[char] = masks.get(char, 0) | (1 << index)
    min_length = max(20, (19 * len(quote) + 20) // 21)
    max_length = min(200, 21 * len(quote) // 19)
    for start in sorted(starts):
        best_ratio, best_length = 0.95, 0
        # Track the longest common subsequence as the excerpt grows. Its
        # normalized length gives the similarity used by the indel ratio.
        row = 0
        for length, char in enumerate(text[start : start + max_length], 1):
            merged = row | masks.get(char, 0)
            row = merged & ~(merged - ((row << 1) | 1))
            if length >= min_length:
                ratio = 2 * row.bit_count() / (length + len(quote))
                if ratio >= best_ratio:
                    best_ratio, best_length = ratio, length
        if best_length:
            yield start, start + best_length, best_ratio


def claim_window(claim: Claim, doc: SourceDoc) -> tuple[str, str, bool]:
    quote = _normalized(claim.evidence_quote)
    if (
        claim.evidence_url not in {doc.evidence.url, doc.evidence.final_url}
        or len(quote) < 20
    ):
        return "", "", False
    text = _normalized(doc.text)
    numbers = _numbers(_normalized(claim.text))
    fallback = "", "", False
    spans = _quote_spans(quote, text)
    for start, end, best in spans:
        found = text[start:end]
        window = text[max(0, start - 300) : end + 300]
        if _numbers(window).issuperset(numbers):
            # Among nearby fuzzy starts, retain the closest original excerpt.
            for later, stop, ratio in spans:
                if best == 1.0 or later > start + len(quote) // 8:
                    break
                candidate = text[later:stop]
                context = text[max(0, later - 300) : stop + 300]
                if ratio > best and _numbers(context).issuperset(numbers):
                    found, window, best = candidate, context, ratio
            return found, window, True
        fallback = found, window, False
    return fallback


def claim_match(claim: Claim, doc: SourceDoc) -> tuple[bool, bool]:
    quote, _, numbers_match = claim_window(claim, doc)
    return bool(quote), numbers_match


def verified_claims(claims: list[Claim], doc: SourceDoc) -> list[Claim]:
    return [claim for claim in claims if claim_match(claim, doc)[1]]
