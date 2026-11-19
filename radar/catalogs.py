import json
import re
from datetime import timedelta, timezone
from threading import Lock
from time import monotonic, sleep
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from defusedxml.ElementTree import fromstring

from .http import FetchError

ARXIV_LOCK = Lock()
_last_arxiv_request = float("-inf")
OFFICIAL_MCP = "io.modelcontextprotocol.registry/official"
JSON_TYPES = {"application/json"}


def arxiv_fetch(fetch, url, **options):
    global _last_arxiv_request
    with ARXIV_LOCK:
        sleep(max(0, _last_arxiv_request + 3 - monotonic()))
        _last_arxiv_request = monotonic()
        return fetch(url, **options)


def arxiv_rows(url, since, until, fetch, reasons, feed_rows):
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    after = since.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    ceiling = (
        until + timedelta(minutes=1) if until.second or until.microsecond else until
    )
    before = ceiling.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    query = params["search_query"]
    params.update(
        search_query=f"({query}) AND submittedDate:[{after} TO {before}]",
        max_results=500,
        sortBy="submittedDate",
        sortOrder="descending",
    )
    body = fetch(
        urlunsplit(parts._replace(query=urlencode(params))),
        accept={"application/atom+xml", "application/xml", "text/xml"},
    )
    root = fromstring(body, forbid_dtd=True)
    total = int(
        root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults") or ""
    )
    rows = list(feed_rows(root, url))
    if total > len(rows):
        reasons.add("pagination_stopped")
    for row in rows:
        row["id"] = re.sub(r"v\d+$", "", row["id"] or "")
    return rows


def hf_rows(url, since, until, fetch, reasons):
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    params.update(limit=100, sort="publishedAt")
    first = since.astimezone(timezone.utc).date()
    last = until.astimezone(timezone.utc).date()
    rows = {}
    error = None
    for offset in range((last - first).days + 1):
        params["date"] = (first + timedelta(days=offset)).isoformat()
        for page in range(3):
            params["p"] = page
            try:
                payload = json.loads(
                    fetch(
                        urlunsplit(parts._replace(query=urlencode(params))),
                        accept=JSON_TYPES,
                    )
                )
                if not isinstance(payload, list):
                    raise ValueError("Unexpected Daily Papers response")
                for hit in payload:
                    paper = hit["paper"]
                    identifier = quote(paper["id"], safe="/")
                    rows[paper["id"]] = dict(
                        id=paper["id"],
                        title=paper["title"],
                        url=f"https://huggingface.co/papers/{identifier}",
                        target=f"https://arxiv.org/abs/{identifier}",
                        published=paper.get("publishedAt"),
                    )
            except (FetchError, ValueError, TypeError, KeyError) as exc:
                error = error or exc
                reasons.add("incomplete_results")
                break
            if len(payload) < 100:
                break
        else:
            reasons.add("pagination_stopped")
    if error and not rows:
        raise error
    return rows.values()


def mcp_rows(url, since, fetch, reasons):
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    params.update(
        limit=100,
        version="latest",
        updated_since=since.astimezone(timezone.utc).isoformat(),
    )
    cursors = set()
    rows = []
    for _ in range(10):
        try:
            payload = json.loads(
                fetch(
                    urlunsplit(parts._replace(query=urlencode(params))),
                    accept=JSON_TYPES,
                )
            )
            for hit in payload["servers"]:
                data = hit["server"]
                meta = hit["_meta"][OFFICIAL_MCP]
                if meta["status"] != "active":
                    continue
                name, version = data["name"], data["version"]
                repository = data.get("repository") or {}
                rows.append(
                    dict(
                        id=f"{name}@{version}",
                        title=f"{name} {version}",
                        url=f"https://registry.modelcontextprotocol.io/v0.1/servers/{quote(name, safe='')}/versions/{quote(version, safe='')}",
                        published=meta.get("publishedAt"),
                        updated=meta.get("updatedAt"),
                        target=repository.get("url"),
                    )
                )
            cursor = payload["metadata"].get("nextCursor")
        except (FetchError, ValueError, TypeError, KeyError, AttributeError):
            if not rows:
                raise
            break
        if not cursor:
            return rows
        if cursor in cursors:
            break
        cursors.add(cursor)
        params["cursor"] = cursor
    reasons.add("pagination_stopped")
    return rows
