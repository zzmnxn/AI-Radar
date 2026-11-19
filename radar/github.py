import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .http import FetchError, safe_get

API = "https://api.github.com"
JSON_TYPES = {"application/json", "application/vnd.github+json"}
DAY = 86400


def search_rows(url, since, until, fetch, reasons, token):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "api.github.com"
        or parts.path != "/search/repositories"
    ):
        raise ValueError("Unexpected GitHub Search endpoint")
    query = dict(parse_qsl(parts.query))["q"]
    rows = {}
    remaining = 25 if token else 9

    def page(qualifier, start, end, number):
        nonlocal remaining
        if remaining <= 0:
            reasons.add("rate_limited")
            return None
        bounds = "..".join(
            datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for value in (start, end)
        )
        params = {"q": f"{query} {qualifier}:{bounds}", "per_page": 100, "page": number}
        remaining -= 1
        response = fetch(
            urlunsplit(parts._replace(query=urlencode(params))),
            accept=JSON_TYPES,
            github_token=token,
        )
        quota = response.headers.get("x-ratelimit-remaining")
        if response.status == 429 or (
            response.status == 403
            and (quota == "0" or "retry-after" in response.headers)
        ):
            remaining = 0
            reasons.add("rate_limited")
            return None
        if quota is not None and quota.isdecimal():
            remaining = min(remaining, int(quota))
        if response.body is None or response.status != 200:
            raise FetchError("http_error", response.status)
        return json.loads(response.body)

    def retain(payload):
        if payload["incomplete_results"]:
            reasons.add("incomplete_results")
        for repo in payload["items"]:
            rows[repo["id"]] = dict(
                id=str(repo["id"]),
                url=repo["html_url"],
                title=repo["full_name"],
                published=repo["created_at"],
                updated=repo["pushed_at"],
                stars=repo["stargazers_count"],
            )

    def window(qualifier, start, end, depth):
        payload = page(qualifier, start, end, 1)
        if payload is None:
            return
        retain(payload)
        count = payload["total_count"]
        if count > 1000 and depth < 3 and start < end:
            midpoint = (start + end) // 2
            window(qualifier, start, midpoint, depth + 1)
            if "rate_limited" not in reasons:
                window(qualifier, midpoint + 1, end, depth + 1)
            return
        if count > 1000:
            reasons.add("result_cap")
        for number in range(1, max(1, (min(count, 1000) + 99) // 100) + 1):
            if number > 1:
                payload = page(qualifier, start, end, number)
                if payload is None:
                    break
                retain(payload)
            if len(payload["items"]) < min(100, count - (number - 1) * 100):
                reasons.add("incomplete_results")

    try:
        for qualifier in ("created", "pushed"):
            window(qualifier, int(since.timestamp()), int(until.timestamp()), 0)
            if "rate_limited" in reasons:
                break
    except (FetchError, ValueError, TypeError, KeyError):
        if not rows:
            raise
        reasons.add("pagination_stopped")
    return rows.values()


def star_growth_7d(repo, *, now=None, token=None, fetch=safe_get):
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo):
        return None
    now = now or datetime.now(timezone.utc)
    end = int(now.timestamp())
    try:
        response = fetch(
            f"{API}/repos/{quote(repo, safe='/')}/stargazers/history?per_page=3",
            accept=JSON_TYPES,
            github_token=token,
        )
        if response.status != 200 or response.body is None:
            return None
        history = json.loads(response.body)
        buckets = {}
        for entry in history:
            start, days = entry["week"], entry["days"]
            if type(start) is not int or not isinstance(days, list) or len(days) != 7:
                return None
            for index, count in enumerate(days):
                if type(count) is not int or count < 0:
                    return None
                day = start + index * DAY
                if end - 8 * DAY < day <= end - DAY:
                    buckets[day] = count
        if len(buckets) != 7 or max(buckets) - min(buckets) != 6 * DAY:
            return None
        return sum(buckets.values())
    except (FetchError, ValueError, TypeError, KeyError):
        return None
