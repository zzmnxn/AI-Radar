import pytest

from radar.http import FetchError, Response, check_link, safe_get


def test_github_token_stays_on_https_api_origin(monkeypatch):
    sent = []
    monkeypatch.setattr("radar.http._addresses", lambda url: [object()])

    def exchange(
        method, url, addresses, read_body, max_bytes, accept, github_token=None
    ):
        sent.append(github_token)
        if len(sent) == 1:
            return Response(
                url, 302, {"location": "https://example.org/redirect"}, None
            )
        return Response(url, 200, {}, b"{}")

    monkeypatch.setattr("radar.http._exchange", exchange)
    safe_get("https://api.github.com/search/repositories", github_token="test-token")
    safe_get("http://api.github.com/", github_token="test-token")
    assert sent == ["test-token", None, None]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "https://169.254.169.254/",
        "https://[::1]/",
        "https://[ff02::1]/",
        "http://100.64.0.1/",
    ],
)
def test_non_public_ip_blocked(url):
    with pytest.raises(FetchError, match="blocked_address"):
        safe_get(url)


def test_redirect_revalidates_address(monkeypatch):
    monkeypatch.setattr(
        "radar.http._exchange",
        lambda *args: Response(
            status=302,
            headers={"location": "http://127.0.0.1/"},
            body=None,
            final_url="https://93.184.215.14/",
        ),
    )
    with pytest.raises(FetchError, match="blocked_address"):
        safe_get("https://93.184.215.14/")


def test_link_check_falls_back_to_header_only_get(monkeypatch):
    requests = []

    def exchange(
        method, url, addresses, read_body, max_bytes, accept, github_token=None
    ):
        requests.append((method, read_body))
        return Response(
            final_url=url,
            status=405 if method == "HEAD" else 200,
            headers={},
            body=None,
        )

    monkeypatch.setattr("radar.http._exchange", exchange)
    assert check_link("https://93.184.215.14/")
    assert requests == [("HEAD", False), ("GET", False)]


def test_dns_checks_every_answer(monkeypatch):
    monkeypatch.setattr(
        "radar.http.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.215.14", 443)),
            (2, 1, 6, "", ("10.0.0.1", 443)),
        ],
    )
    with pytest.raises(FetchError, match="blocked_address"):
        safe_get("https://example.org/")


@pytest.mark.parametrize(
    ("headers", "body", "error"),
    [
        (b"Content-Type: text/plain", b"123456789", "too_large"),
        (b"Content-Type: application/pdf", b"pdf", "bad_content_type"),
        (
            b"Content-Type: text/plain\r\nContent-Encoding: gzip",
            b"zip",
            "unsupported_encoding",
        ),
    ],
)
def test_transport_rejects_unsupported_or_oversized_bodies(
    monkeypatch, headers, body, error
):
    import socket

    client, server = socket.socketpair()
    monkeypatch.setattr("radar.http._connect", lambda addresses: client)
    with client, server:
        server.sendall(
            b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + headers
            + b"\r\n\r\n"
            + body
        )
        with pytest.raises(FetchError, match=error):
            safe_get("http://93.184.215.14/", max_bytes=8)
