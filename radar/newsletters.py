"""Read dated TLDR AI issues and keep editorial article links."""

import re
from datetime import date, datetime, time, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .normalize import canonical_url, external_key


class IssueLinks(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.issues = {}

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        url = urlsplit(urljoin(self.base, dict(attrs).get("href") or ""))
        match = re.fullmatch(r"/ai/(\d{4}-\d{2}-\d{2})/?", url.path)
        if url.hostname == urlsplit(self.base).hostname and match:
            self.issues[date.fromisoformat(match[1])] = urljoin(self.base, url.path)


class Articles(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = self.heading = False
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "article":
            self.active = True
            self.target = None
            self.title, self.body = [], []
        elif self.active and tag == "a" and not self.target:
            self.target = unescape(dict(attrs).get("href") or "")
        elif self.active and tag == "h3":
            self.heading = True

    def handle_data(self, text):
        if self.active:
            (self.title if self.heading else self.body).append(text)

    def handle_endtag(self, tag):
        if tag == "h3":
            self.heading = False
        if tag == "article" and self.active:
            self.active = False
            title = " ".join(self.title).strip()
            if not title or urlsplit(self.target or "").scheme not in {"http", "https"}:
                raise ValueError("Malformed TLDR article")
            if (
                "(sponsor" not in title.casefold()
                and "utm_medium=paid" not in self.target.casefold()
            ):
                self.rows.append((title, self.target, " ".join(self.body)))


def newsletter_rows(url, start, end, fetch):
    def read(parser, page):
        parser.feed(fetch(page, accept={"text/html"}).decode("utf-8"))
        parser.close()
        return parser

    issues = read(IssueLinks(url), url).issues
    if not issues:
        raise ValueError("TLDR archive structure changed")
    for day, issue in sorted(issues.items()):
        if not start.date() <= day <= end.date():
            continue
        articles = read(Articles(), issue)
        if not articles.rows:
            raise ValueError("No editorial articles in TLDR issue")
        for title, target, body in articles.rows:
            yield dict(
                id=f"{issue}|{canonical_url(target)}",
                title=title,
                target=target,
                url=issue,
                published=datetime.combine(day, time(), timezone.utc),
                snippet=body,
            )


class ProjectLinks(HTMLParser):
    """Explicit repository and release links inside an editor's own feed entry."""

    def __init__(self):
        super().__init__()
        self.targets = {}
        self.href = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, text):
        if self.href:
            self.label.append(text)

    def handle_endtag(self, tag):
        if tag != "a" or not self.href:
            return
        url = canonical_url(self.href)
        key = external_key(url)
        if (
            urlsplit(url).scheme in {"http", "https"}
            and key
            and key[0] in {"github_repo", "github_release", "arxiv"}
        ):
            self.targets.setdefault(
                url, " ".join(self.label).strip() or urlsplit(url).path
            )
        self.href = None


def editorial_rows(rows, source_id):
    for row in rows:
        content = row.pop("content", "") or row.get("snippet") or ""
        yield row
        if source_id not in {"simon-willison", "latent-space", "geeknews"}:
            continue
        links = ProjectLinks()
        links.feed(content)
        links.close()
        for target, title in links.targets.items():
            if canonical_url(row.get("url") or "") != target:
                yield {
                    **row,
                    "id": f"{row.get('id') or row['url']}|{target}",
                    "target": target,
                    "title": title,
                    "canonical": target,
                    "snippet": title,
                }
