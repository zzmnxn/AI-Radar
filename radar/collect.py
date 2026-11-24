import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic
from typing import Literal
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)
from xml.etree.ElementTree import ParseError

import yaml
from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from pydantic import AwareDatetime, Field, HttpUrl, TypeAdapter, ValidationError

from .catalogs import arxiv_fetch, arxiv_rows, hf_rows, mcp_rows
from .enrich import ArticleParser
from .github import search_rows
from .http import FetchError, safe_get
from .models import Contract, Rating, RawItem, SourceClass
from .newsletters import editorial_rows, newsletter_rows
from .normalize import canonical_url

DATE = TypeAdapter(AwareDatetime)
URL = TypeAdapter(HttpUrl)
Completeness = Literal["complete", "partial", "failed"]
FEED_TYPES = frozenset(
    {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}
)


class Source(Contract):
    id: str
    name: str
    org: str
    type: Literal[
        "rss",
        "tldr",
        "hackernews",
        "github_search",
        "arxiv",
        "hf_papers",
        "mcp_registry",
    ]
    url: HttpUrl
    source_class: SourceClass
    discovery_mode: Literal["keyword", "open"]
    supports_range: bool
    trust: Rating = Field(ge=1)
    status: Literal["candidate", "active", "paused", "retired"]
    expected_cadence_days: int = Field(gt=0)
    auth: Literal["none", "env:GITHUB_TOKEN"]
    terms_note: str
    # Release feeds title entries with a bare version; prefix the tool name.
    title_prefix: str | None = None
    editorial_ai: bool = False
    skip_prereleases: bool = False
    # GitHub release feeds have no <published>; their <updated> is the release time.
    published_from_updated: bool = False
    # At most this many candidates per run come from the source (newest first).
    max_candidates: int | None = Field(default=None, ge=1)


PRERELEASE = re.compile(
    r"[-.](?:alpha|beta|rc|pre|preview|nightly|canary|dev)\b", re.IGNORECASE
)


def _release_title(source, title):
    if not title or not source.title_prefix:
        return title
    title = re.sub(r"^release\s+", "", title.strip(), flags=re.IGNORECASE)
    if title.casefold().startswith(source.title_prefix.casefold()):
        return title
    return f"{source.title_prefix} {title}"


@dataclass
class CollectResult:
    source: Source
    completeness: Completeness
    items: list[RawItem] = field(default_factory=list)
    error: str | None = None
    partial_reasons: list[str] = field(default_factory=list)
    last_item_at: datetime | None = None
    requests: int = 0
    elapsed_ms: int = 0


def load_sources(path: Path) -> list[Source]:
    sources = TypeAdapter(list[Source]).validate_python(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    if len({source.id for source in sources}) != len(sources):
        raise ValueError("Source IDs must be unique")
    return [source for source in sources if source.status in {"candidate", "active"}]


def _date(value):
    if not value:
        return None
    try:
        return DATE.validate_python(value)
    except ValidationError:
        try:
            date = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
        return date if date.tzinfo is not None else date.replace(tzinfo=timezone.utc)


def _item(
    source,
    now,
    *,
    id,
    url,
    title,
    published=None,
    updated=None,
    target=None,
    points=None,
    stars=None,
    snippet=None,
    canonical=None,
    source_tags=(),
):
    if not title or not url:
        return None
    url = URL.validate_python(url)
    canonical = canonical_url(canonical or (target if source.type == "tldr" else url))
    excerpt = ""
    if isinstance(snippet, str) and snippet:
        parser = ArticleParser()
        parser.feed(snippet)
        parser.close()
        excerpt = " ".join("".join(parser.page).split())[:300]
    key = hashlib.sha256(f"{source.id}|{id or canonical}".encode()).hexdigest()
    return RawItem(
        item_id=key,
        source_id=source.id,
        source_class=source.source_class,
        url=url,
        canonical_url=canonical,
        link_target_url=target,
        title=" ".join(title.split()),
        published_at=_date(published),
        source_updated_at=_date(updated),
        first_seen_at=now,
        metrics={"hn_points": points, "gh_stars": stars},
        snippet=excerpt,
        source_tags=sorted({tag.strip() for tag in source_tags if tag and tag.strip()}),
    )


def _feed_rows(root, base_url):
    if root.tag == "{http://www.w3.org/2005/Atom}feed":
        namespace = "{http://www.w3.org/2005/Atom}"
        xml_base = "{http://www.w3.org/XML/1998/namespace}base"
        for entry in root.findall(f"{namespace}entry"):
            link = next(
                (
                    node
                    for node in entry.findall(f"{namespace}link")
                    if node.get("rel", "alternate") == "alternate" and node.get("href")
                ),
                None,
            )
            entry_base = urljoin(
                urljoin(base_url, root.get(xml_base, "")), entry.get(xml_base, "")
            )
            yield dict(
                id=entry.findtext(f"{namespace}id"),
                url=urljoin(
                    urljoin(entry_base, link.get(xml_base, "")), link.get("href")
                )
                if link is not None
                else None,
                title=entry.findtext(f"{namespace}title"),
                published=entry.findtext(f"{namespace}published"),
                updated=entry.findtext(f"{namespace}updated"),
                snippet=entry.findtext(f"{namespace}summary")
                or entry.findtext(f"{namespace}content"),
                target=next(
                    (
                        urljoin(entry_base, n.get("href"))
                        for n in entry.findall(f"{namespace}link")
                        if n.get("rel") == "related" and n.get("href")
                    ),
                    None,
                ),
                source_tags=[
                    node.get("term") for node in entry.findall(f"{namespace}category")
                ],
            )
    elif root.tag == "rss" and root.find("channel") is not None:
        for entry in root.findall("channel/item"):
            link = entry.findtext("link")
            yield dict(
                id=entry.findtext("guid"),
                url=urljoin(base_url, link) if link else None,
                title=entry.findtext("title"),
                published=entry.findtext("pubDate"),
                snippet=entry.findtext("description"),
                content=entry.findtext(
                    "{http://purl.org/rss/1.0/modules/content/}encoded"
                ),
                source_tags=[node.text for node in entry.findall("category")],
            )
    else:
        raise ValueError("Unsupported feed format")


def _hn_rows(payload):
    for hit in payload["hits"]:
        id = hit["objectID"]
        if not isinstance(id, str) or not id.isdecimal():
            continue
        yield dict(
            id=id,
            url=f"https://news.ycombinator.com/item?id={id}",
            target=hit.get("url") or None,
            title=hit.get("title"),
            published=hit.get("created_at"),
            points=hit.get("points"),
        )


def _fetch(url, **kwargs):
    response = safe_get(url, **kwargs)
    if response.body is None:
        raise FetchError("http_error", response.status)
    return response.body


def collect_source(source, now, *, since=None, until=None, fetch=None):
    request = fetch or (safe_get if source.type == "github_search" else _fetch)
    requests = 0

    def fetch_page(url, **options):
        nonlocal requests
        requests += 1
        if fetch is None and source.type == "arxiv":
            return arxiv_fetch(request, url, **options)
        return request(url, **options)

    url = str(source.url)
    reasons = set()
    started = monotonic()
    last_item_at = None
    end = until or now
    start = since or end - timedelta(days=3)
    try:
        if source.type == "rss":
            root = fromstring(fetch_page(url, accept=FEED_TYPES), forbid_dtd=True)
            rows = editorial_rows(_feed_rows(root, url), source.id)
        elif source.type == "tldr":
            rows = newsletter_rows(url, start, end, fetch_page)
        elif source.type == "hackernews":
            if source.supports_range and since is not None:
                parts = urlsplit(url)
                params = dict(parse_qsl(parts.query))
                filters = [
                    params.get("numericFilters"),
                    f"created_at_i>={int(since.timestamp())}",
                    f"created_at_i<={int(end.timestamp())}",
                ]
                params["numericFilters"] = ",".join(value for value in filters if value)
                url = urlunsplit(parts._replace(query=urlencode(params)))
            payload = json.loads(fetch_page(url, accept={"application/json"}))
            rows = _hn_rows(payload)
            if payload.get("nbHits", 0) > 1000:
                reasons.add("result_cap")
            if payload["nbPages"] > 1:
                reasons.add("pagination_stopped")
        elif source.type == "github_search":
            token = (
                os.environ.get("GITHUB_TOKEN")
                if source.auth == "env:GITHUB_TOKEN"
                else None
            )
            rows = search_rows(url, start, end, fetch_page, reasons, token)
        elif source.type == "arxiv":
            rows = arxiv_rows(url, start, end, fetch_page, reasons, _feed_rows)
        elif source.type == "hf_papers":
            rows = hf_rows(url, start, end, fetch_page, reasons)
        else:
            rows = mcp_rows(url, start, fetch_page, reasons)
        items = []
        for row in rows:
            if source.skip_prereleases and PRERELEASE.search(row.get("title") or ""):
                continue
            row["title"] = _release_title(source, row.get("title"))
            if source.published_from_updated and not row.get("published"):
                row["published"] = row.get("updated")
            try:
                item = _item(source, now, **row)
            except (ValidationError, AttributeError, ValueError):
                item = None
            if item is None:
                reasons.add("incomplete_results")
                continue
            published = item.published_at
            if published and published <= end:
                last_item_at = max(last_item_at or published, published)
            updated = item.source_updated_at
            recent_update = (
                source.type in {"github_search", "mcp_registry"}
                and updated is not None
                and start <= updated <= end
            )
            if (
                since is None
                or published is None
                or since <= published <= end
                or recent_update
            ):
                items.append(item)
        return CollectResult(
            source,
            "partial" if reasons else "complete",
            items,
            partial_reasons=sorted(reasons),
            last_item_at=last_item_at,
            requests=requests,
            elapsed_ms=round((monotonic() - started) * 1000),
        )
    except (
        FetchError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        ParseError,
        DefusedXmlException,
    ) as exc:
        error = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
        return CollectResult(
            source,
            "failed",
            error=error,
            requests=requests,
            elapsed_ms=round((monotonic() - started) * 1000),
        )


def collect(sources, now: datetime, *, fetch=None) -> list[CollectResult]:
    return [
        collect_source(
            source,
            now,
            since=now - timedelta(days=3) if source.supports_range else None,
            fetch=fetch,
        )
        for source in sources
    ]


def select_candidates(
    results: list[CollectResult], now: datetime, *, limit: int
) -> list[RawItem]:
    cutoff = now - timedelta(hours=48)
    seen_urls = set()
    seen_items = set()
    selected = []
    for result in sorted(
        results,
        key=lambda result: (
            -result.source.trust,
            result.source.source_class != "official",
            result.source.id,
        ),
    ):
        for item in result.items:
            if not cutoff <= (item.published_at or item.first_seen_at) <= now:
                continue
            urls = {str(item.canonical_url)}
            if item.link_target_url:
                urls.add(canonical_url(item.link_target_url))
            if item.item_id in seen_items or seen_urls.intersection(urls):
                continue
            seen_urls.update(urls)
            seen_items.add(item.item_id)
            selected.append(item)
    return sorted(
        selected, key=lambda item: item.published_at or item.first_seen_at, reverse=True
    )[:limit]
