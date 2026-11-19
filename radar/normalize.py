import re
import unicodedata
from functools import lru_cache
from urllib.parse import unquote_plus, urlsplit, urlunsplit

TRACKING_KEYS = {"fbclid", "gclid"}
ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Za-z]{2})?/\d{7})"
TITLE_TOKEN = re.compile(r"(?<!\w)v?\d+(?:\.\d+)+(?:[-+][a-z0-9.]+)*|[^\W_]+")


def canonical_url(value) -> str:
    parts = urlsplit(str(value))
    path = parts.path or "/"
    if parts.hostname == "arxiv.org" and path.startswith("/abs/"):
        path = re.sub(r"v\d+$", "", path)
    query = [
        pair
        for pair in parts.query.split("&")
        if pair
        and not unquote_plus(pair.partition("=")[0]).lower().startswith("utm_")
        and unquote_plus(pair.partition("=")[0]).lower() not in TRACKING_KEYS
    ]
    return urlunsplit(parts._replace(path=path, query="&".join(query), fragment=""))


def release_key(url):
    parts = urlsplit(url)
    if parts.hostname != "github.com":
        return None
    match = re.fullmatch(r"/([^/]+/[^/]+)/releases/tag/([^/]+)/?", parts.path)
    return (match[1].lower(), match[2]) if match else None


def external_key(url):
    parts = urlsplit(url)
    prefix = {"arxiv.org": "/abs/", "huggingface.co": "/papers/"}.get(parts.hostname)
    if prefix and parts.path.startswith(prefix):
        match = re.fullmatch(f"({ARXIV_ID})(?:v\\d+)?/?", parts.path[len(prefix) :])
        if match:
            return "arxiv", match[1].lower()
    if parts.hostname == "github.com":
        release = release_key(url)
        if release:
            return "github_release", release
        if re.fullmatch(r"/[^/]+/[^/]+/?", parts.path):
            return "github_repo", parts.path.strip("/").lower()
    return None


@lru_cache(maxsize=8192)
def title_tokens(title):
    return tuple(TITLE_TOKEN.findall(unicodedata.normalize("NFKC", title).casefold()))
