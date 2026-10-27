"""Website Health Monitor: a small REST API that checks whether websites are up.

It also serves a simple web page at "/" (app/static/index.html) where users
type a URL and see the result. That page calls the POST /check endpoint below.

Security note (SSRF protection)
-------------------------------
An API that fetches any URL a user sends is a classic "Server-Side Request
Forgery" (SSRF) risk: an attacker could ask it to fetch http://localhost or the
cloud metadata service (http://169.254.169.254) and read internal data.

We defend against this in four steps:
1. Only accept http:// and https:// URLs.
2. Resolve the hostname to IP addresses ourselves and reject the request if
   ANY address is not a public internet address.
3. Connect to the exact IP we checked ("IP pinning"), so a DNS server cannot
   return a safe IP to us and a private IP a moment later (DNS rebinding).
4. Follow redirects manually, repeating steps 1-3 for every hop.
"""

import asyncio
import ipaddress
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

APP_NAME = "Website Health Monitor"
APP_VERSION = "0.1.0"
STATIC_DIR = Path(__file__).parent / "static"

REQUEST_TIMEOUT_SECONDS = 5.0
MAX_REDIRECTS = 5
ALLOWED_SCHEMES = {"http", "https"}

# Hostnames that are never public, even before we look up their IP address.
BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}
BLOCKED_HOSTNAME_SUFFIXES = (".localhost", ".local", ".internal")


class UnsafeURLError(Exception):
    """Raised when a URL points to a destination we refuse to contact."""


# ---------------------------------------------------------------------------
# Request and response models (Pydantic validates the JSON for us)
# ---------------------------------------------------------------------------


def parse_http_url(url: str) -> httpx.URL:
    """Parse a URL and make sure it is a well-formed http(s) URL."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise ValueError(f"Invalid URL: {exc}") from exc

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError("Only http:// and https:// URLs are allowed")
    if not parsed.host:
        raise ValueError("URL must include a hostname")
    if parsed.userinfo:
        raise ValueError("URLs containing a username or password are not allowed")
    return parsed


class CheckRequest(BaseModel):
    url: str = Field(max_length=2048, examples=["https://example.com"])

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: str) -> str:
        value = value.strip()
        parse_http_url(value)  # raises ValueError -> FastAPI returns HTTP 422
        return value


class CheckResult(BaseModel):
    url: str
    status: Literal["online", "offline"]
    status_code: int | None = None
    response_time_ms: float
    error: str | None = None


# ---------------------------------------------------------------------------
# SSRF protection helpers
# ---------------------------------------------------------------------------


def is_public_ip(address: str) -> bool:
    """Return True only for normal, publicly routable internet addresses.

    `is_global` is False for loopback (127.0.0.1, ::1), private ranges
    (10.x, 172.16-31.x, 192.168.x), link-local (169.254.x, where cloud
    metadata services live), carrier-grade NAT, reserved ranges and more.
    """
    ip = ipaddress.ip_address(address)
    # "::ffff:127.0.0.1" is IPv4 127.0.0.1 written in IPv6 form - unwrap it.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def resolve_host(host: str, port: int) -> list[str]:
    """Look up all IP addresses for a hostname using the system DNS resolver.

    Tests replace this function so they never need real DNS.
    """
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({result[4][0] for result in results})


async def resolve_public_ip(url: httpx.URL) -> str:
    """Return a safe IP address to connect to, or raise UnsafeURLError."""
    host = url.raw_host.decode("ascii").lower()  # IDNA-encoded, e.g. "xn--..."
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_HOSTNAME_SUFFIXES):
        raise UnsafeURLError(f"Destination '{host}' is not allowed")

    port = url.port or (443 if url.scheme == "https" else 80)
    addresses = await asyncio.wait_for(resolve_host(host, port), timeout=REQUEST_TIMEOUT_SECONDS)
    if not addresses:
        raise socket.gaierror(f"No addresses found for '{host}'")

    for address in addresses:
        if not is_public_ip(address):
            raise UnsafeURLError(
                f"Destination '{host}' resolves to a non-public address ({address})"
            )
    return addresses[0]


def build_pinned_request(client: httpx.AsyncClient, url: httpx.URL, ip: str) -> httpx.Request:
    """Build a GET request that connects to `ip` but still talks to `url`'s host.

    - The URL's host is swapped for the IP so no second DNS lookup happens.
    - The Host header tells the web server which website we want.
    - For HTTPS, `sni_hostname` makes TLS use (and verify the certificate
      for) the real hostname instead of the IP address.
    """
    pinned_url = url.copy_with(host=ip)
    headers = {"Host": url.netloc.decode("ascii")}
    extensions = {"sni_hostname": url.raw_host.decode("ascii")} if url.scheme == "https" else {}
    return client.build_request("GET", pinned_url, headers=headers, extensions=extensions)


# ---------------------------------------------------------------------------
# The website check itself
# ---------------------------------------------------------------------------


async def check_website(url: str, client: httpx.AsyncClient) -> CheckResult:
    """Check one website, following up to MAX_REDIRECTS redirects safely."""
    start = time.perf_counter()

    def result(**fields) -> CheckResult:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        return CheckResult(url=url, response_time_ms=elapsed_ms, **fields)

    current_url = parse_http_url(url)
    for hop in range(MAX_REDIRECTS + 1):
        try:
            ip = await resolve_public_ip(current_url)
            request = build_pinned_request(client, current_url, ip)
            # stream=True + aclose(): read the status and headers only,
            # without downloading a potentially huge page body.
            response = await client.send(request, stream=True)
            await response.aclose()
        except UnsafeURLError as exc:
            if hop == 0:
                raise  # the user's own URL is unsafe -> HTTP 400 in the endpoint
            return result(status="offline", error=f"Redirect blocked: {exc}")
        except (TimeoutError, httpx.TimeoutException):
            return result(
                status="offline",
                error=f"Request timed out after {REQUEST_TIMEOUT_SECONDS:g} seconds",
            )
        except socket.gaierror:
            return result(status="offline", error=f"DNS lookup failed for '{current_url.host}'")
        except httpx.ConnectError as exc:
            return result(status="offline", error=f"Connection failed: {exc}")
        except httpx.HTTPError as exc:
            return result(status="offline", error=f"Request failed: {exc}")

        if not response.is_redirect:
            # Any HTTP answer below 500 means the server is up and responding
            # (a 404 is "page not found", but the website itself is online).
            status = "online" if response.status_code < 500 else "offline"
            error = None if status == "online" else f"Server error (HTTP {response.status_code})"
            return result(status=status, status_code=response.status_code, error=error)

        # Redirect: work out the next URL (Location may be relative) and loop.
        try:
            current_url = parse_http_url(str(current_url.join(response.headers["location"])))
        except ValueError as exc:
            return result(
                status="offline",
                status_code=response.status_code,
                error=f"Redirected to an invalid URL: {exc}",
            )

    return result(status="offline", error=f"Too many redirects (more than {MAX_REDIRECTS})")


# ---------------------------------------------------------------------------
# FastAPI application and routes
# ---------------------------------------------------------------------------

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="Check whether public websites are online.",
)


async def get_http_client() -> AsyncIterator[httpx.AsyncClient]:
    """Provide an HTTP client to the /check route (tests swap in a fake one)."""
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,  # we follow redirects ourselves, safely
        trust_env=False,  # ignore proxy environment variables
        headers={"User-Agent": f"WebsiteHealthMonitor/{APP_VERSION}"},
    ) as client:
        yield client


@app.get("/", include_in_schema=False)
async def home_page() -> FileResponse:
    """Serve the web page where users type a URL and see the result."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api")
async def api_info() -> dict[str, str]:
    return {"message": f"Welcome to the {APP_NAME} API! Visit /docs to try it out."}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/check")
async def check(
    payload: CheckRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> CheckResult:
    try:
        return await check_website(payload.url, client)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
