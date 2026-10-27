"""Automated tests for the Website Health Monitor API.

No test touches the real internet:
- `fake_dns` replaces DNS lookups with a small dictionary.
- `mock_website` replaces real HTTP requests with an httpx.MockTransport,
  which calls our own Python function instead of opening a network connection.
"""

import ipaddress
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app, get_http_client

PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "151.101.1.69"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Replace DNS with a dictionary. Tests can add entries to it."""
    records = {
        "example.com": [PUBLIC_IP],
        "www.example.com": [OTHER_PUBLIC_IP],
    }

    async def fake_resolve_host(host: str, port: int) -> list[str]:
        try:
            ipaddress.ip_address(host)
            return [host]  # IP addresses "resolve" to themselves
        except ValueError:
            pass
        if host not in records:
            raise socket.gaierror(f"Name or service not known: {host}")
        return records[host]

    monkeypatch.setattr(main, "resolve_host", fake_resolve_host)
    return records


@pytest.fixture
def mock_website():
    """Return a function that builds a TestClient whose outgoing requests
    are answered by `handler` instead of the real internet."""

    def _make_client(handler) -> TestClient:
        async def override_http_client():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                yield client

        app.dependency_overrides[get_http_client] = override_http_client
        return TestClient(app)

    yield _make_client
    app.dependency_overrides.clear()


@pytest.fixture
def client(mock_website):
    """A client for tests that must never make an outgoing request."""

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected outgoing request to {request.url}")

    return mock_website(fail_if_called)


# --- Basic endpoints --------------------------------------------------------


def test_home_page_serves_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Website Health Monitor" in response.text
    assert 'fetch("/check"' in response.text  # the page calls our API


def test_api_returns_welcome_message(client):
    response = client.get("/api")
    assert response.status_code == 200
    assert "Welcome" in response.json()["message"]


def test_health_returns_healthy(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


# --- Successful checks ------------------------------------------------------


def test_check_online_website(mock_website):
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200)

    response = mock_website(handler).post("/check", json={"url": "https://example.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "https://example.com"
    assert body["status"] == "online"
    assert body["status_code"] == 200
    assert body["response_time_ms"] >= 0
    assert body["error"] is None

    # The request went to the IP we checked, with the real hostname in Host.
    assert seen_requests[0].url.host == PUBLIC_IP
    assert seen_requests[0].headers["host"] == "example.com"


def test_check_404_still_counts_as_online(mock_website):
    client = mock_website(lambda request: httpx.Response(404))
    body = client.post("/check", json={"url": "https://example.com/missing"}).json()
    assert body["status"] == "online"
    assert body["status_code"] == 404


def test_check_follows_safe_redirect(mock_website):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return httpx.Response(301, headers={"Location": "https://www.example.com/"})
        return httpx.Response(200)

    body = mock_website(handler).post("/check", json={"url": "http://example.com"}).json()
    assert body["status"] == "online"
    assert body["status_code"] == 200


# --- Failures ---------------------------------------------------------------


def test_check_server_error_is_offline(mock_website):
    client = mock_website(lambda request: httpx.Response(503))
    body = client.post("/check", json={"url": "https://example.com"}).json()
    assert body["status"] == "offline"
    assert body["status_code"] == 503
    assert "503" in body["error"]


def test_check_timeout(mock_website):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    body = mock_website(handler).post("/check", json={"url": "https://example.com"}).json()
    assert body["status"] == "offline"
    assert body["status_code"] is None
    assert "timed out after 5 seconds" in body["error"]


def test_check_connection_error(mock_website):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    body = mock_website(handler).post("/check", json={"url": "https://example.com"}).json()
    assert body["status"] == "offline"
    assert "Connection failed" in body["error"]


def test_check_dns_failure(client):
    response = client.post("/check", json={"url": "https://does-not-exist.example"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "offline"
    assert "DNS lookup failed" in body["error"]


def test_check_too_many_redirects(mock_website):
    client = mock_website(lambda request: httpx.Response(302, headers={"Location": "/again"}))
    body = client.post("/check", json={"url": "https://example.com"}).json()
    assert body["status"] == "offline"
    assert "Too many redirects" in body["error"]


# --- Invalid input ----------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "not-a-url",
        "example.com",  # missing http:// or https://
        "ftp://example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://",
        "https://user:password@example.com",
    ],
)
def test_check_rejects_invalid_urls(client, bad_url):
    response = client.post("/check", json={"url": bad_url})
    assert response.status_code == 422


def test_check_requires_url_field(client):
    assert client.post("/check", json={}).status_code == 422


# --- Security: non-public destinations --------------------------------------


@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://10.0.0.5",
        "http://172.16.0.1",
        "http://192.168.1.1",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata service
        "http://metadata.google.internal",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0",
    ],
)
def test_check_blocks_non_public_destinations(client, blocked_url):
    response = client.post("/check", json={"url": blocked_url})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "not allowed" in detail or "non-public" in detail


def test_check_blocks_hostname_resolving_to_private_ip(client, fake_dns):
    fake_dns["sneaky.example.com"] = ["10.1.2.3"]
    response = client.post("/check", json={"url": "https://sneaky.example.com"})
    assert response.status_code == 400
    assert "non-public" in response.json()["detail"]


def test_check_blocks_redirect_to_private_address(mock_website):
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    body = mock_website(handler).post("/check", json={"url": "https://example.com"}).json()
    assert body["status"] == "offline"
    assert "Redirect blocked" in body["error"]
    assert len(seen_requests) == 1  # the metadata address was never contacted
