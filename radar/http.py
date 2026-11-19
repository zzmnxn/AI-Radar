import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

FEED_BYTES = 5 * 1024 * 1024
ARTICLE_BYTES = 2 * 1024 * 1024
CONTENT_TYPES = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/xml",
        "application/json",
        "application/xhtml+xml",
        "text/html",
        "text/xml",
        "text/plain",
    }
)
REDIRECTS = {301, 302, 303, 307, 308}


class FetchError(Exception):
    def __init__(self, kind: str, status: int | None = None):
        self.kind = kind
        self.status = status
        super().__init__(kind if status is None else f"{kind} ({status})")


@dataclass(frozen=True)
class Response:
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes | None
    redirects: tuple[str, ...] = ()


def _addresses(url):
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise FetchError("bad_port") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or any(ord(char) <= 32 for char in url)
    ):
        raise FetchError("bad_scheme")
    if port not in {None, 80, 443}:
        raise FetchError("bad_port")
    port = port or (443 if parts.scheme == "https" else 80)
    try:
        answers = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError("dns_error") from exc
    if not answers:
        raise FetchError("dns_error")
    for _, _, _, _, address in answers:
        ip = ipaddress.ip_address(address[0])
        if isinstance(ip, ipaddress.IPv6Address):
            ip = ip.ipv4_mapped or ip
        if (
            not ip.is_global
            or ip.is_multicast
            or ip.is_reserved
            or getattr(ip, "is_site_local", False)
        ):
            raise FetchError("blocked_address")
    return answers


def _connect(addresses):
    for family, socktype, proto, _, address in addresses:
        connection = socket.socket(family, socktype, proto)
        try:
            connection.settimeout(5)
            connection.connect(address)
            return connection
        except OSError as exc:
            connection.close()
            last_error = exc
    raise last_error


def _exchange(method, url, addresses, read_body, max_bytes, accept, github_token=None):
    parts = urlsplit(url)
    host = parts.hostname.encode("idna").decode("ascii")
    connection = http.client.HTTPConnection(
        host, parts.port or (443 if parts.scheme == "https" else 80)
    )
    # Connect to the validated address without a second DNS lookup. TLS verifies
    # the original hostname and sends it as SNI.
    sock = _connect(addresses)
    try:
        if parts.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock.settimeout(15)
        connection.sock = sock
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        headers = {
            "Accept": ", ".join(sorted(accept)),
            "Accept-Encoding": "identity",
            "User-Agent": "AI-Radar/0.1",
            "Connection": "close",
        }
        if (
            parts.scheme == "https"
            and host == "api.github.com"
            and parts.port in {443, None}
            and parts.path.endswith("/stargazers/history")
        ):
            headers["X-GitHub-Api-Version"] = "2026-03-10"
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"
        connection.request(method, target, headers=headers)
        with connection.getresponse() as response:
            headers = {name.lower(): value for name, value in response.getheaders()}
            body = None
            if read_body and 200 <= response.status < 300:
                content_type = (
                    headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                if content_type not in accept:
                    raise FetchError("bad_content_type")
                if headers.get("content-encoding", "identity").lower() != "identity":
                    raise FetchError("unsupported_encoding")
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise FetchError("too_large")
            return Response(url, response.status, headers, body)
    finally:
        connection.close()
        sock.close()


def safe_request(
    method,
    url,
    *,
    read_body,
    max_bytes=FEED_BYTES,
    accept=CONTENT_TYPES,
    github_token=None,
):
    if method not in {"GET", "HEAD"}:
        raise ValueError("Only GET and HEAD are supported")
    redirects = []
    for hop in range(6):
        addresses = _addresses(url)
        origin = urlsplit(url)
        if (
            origin.scheme != "https"
            or origin.hostname != "api.github.com"
            or origin.port not in {443, None}
        ):
            github_token = None
        try:
            response = _exchange(
                method, url, addresses, read_body, max_bytes, accept, github_token
            )
        except TimeoutError as exc:
            raise FetchError("timeout") from exc
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise FetchError("http_error") from exc
        location = response.headers.get("location")
        if response.status not in REDIRECTS or not location:
            return Response(
                response.final_url,
                response.status,
                response.headers,
                response.body,
                tuple(redirects),
            )
        if hop == 5:
            raise FetchError("too_many_redirects")
        url = urljoin(url, location)
        redirects.append(url)


def safe_get(url, **kwargs):
    return safe_request("GET", url, read_body=True, **kwargs)


def check_link(url):
    result = safe_request("HEAD", url, read_body=False)
    if result.status in {403, 405, 501}:
        result = safe_request("GET", url, read_body=False)
    return 200 <= result.status < 300
