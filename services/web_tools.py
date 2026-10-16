import asyncio
import ipaddress
import json
import logging
import re
import socket
import urllib.parse

import httpcore
from httpcore._backends.sync import SyncBackend
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _web_search_inner(query: str, max_results: int = 5) -> str:
    """
    Performs a live web search for the given query and returns a list of results
    with titles, URLs, and snippets in JSON format.
    """
    if not query or not query.strip():
        return json.dumps({"error": "Search query cannot be empty."})

    cleaned_query = query.strip()
    max_results = max(1, min(int(max_results), 10))

    # Strategy 1: DuckDuckGo HTML Endpoint (Fast, reliable, zero-auth)
    try:
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        data = {"q": cleaned_query}
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            response = client.post(url, data=data, headers=headers)

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            results = []
            result_divs = soup.select(".result")

            for div in result_divs:
                if len(results) >= max_results:
                    break

                title_elem = div.select_one(".result__title a") or div.select_one(".result__a")
                snippet_elem = div.select_one(".result__snippet")
                url_elem = div.select_one(".result__url")

                if not title_elem:
                    continue

                raw_href = title_elem.get("href", "")
                target_url = raw_href
                if "uddg=" in raw_href:
                    parsed = urllib.parse.urlparse(raw_href)
                    qs = urllib.parse.parse_qs(parsed.query)
                    if "uddg" in qs:
                        target_url = qs["uddg"][0]
                elif raw_href.startswith("//"):
                    target_url = "https:" + raw_href
                elif not raw_href.startswith("http") and url_elem:
                    url_text = url_elem.get_text(strip=True)
                    if url_text:
                        target_url = f"https://{url_text}"

                title = title_elem.get_text(strip=True)
                snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

                if title and target_url:
                    results.append(
                        {
                            "title": title,
                            "url": target_url,
                            "snippet": snippet,
                        }
                    )

            if results:
                return json.dumps(
                    {
                        "query": cleaned_query,
                        "results_count": len(results),
                        "results": results,
                    },
                    indent=2,
                )

    except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError) as e:
        logger.warning(f"[WEB_SEARCH] DuckDuckGo HTML endpoint failed: {e}. Attempting fallback...")
    except Exception as e:  # noqa: BLE001 - HTML search endpoint fallback to DDGS
        logger.warning(f"[WEB_SEARCH] DuckDuckGo HTML endpoint failed: {e}. Attempting fallback...")

    # Strategy 2: ddgs library fallback
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        ddgs_results = []
        with DDGS() as ddgs:
            for r in ddgs.text(cleaned_query, max_results=max_results):
                ddgs_results.append(
                    {
                        "title": r.get("title", ""),
                        "url": r.get("href", "") or r.get("link", "") or r.get("url", ""),
                        "snippet": r.get("body", "") or r.get("snippet", ""),
                    }
                )

        if ddgs_results:
            return json.dumps(
                {
                    "query": cleaned_query,
                    "results_count": len(ddgs_results),
                    "results": ddgs_results,
                },
                indent=2,
            )
    except (ImportError, AttributeError) as e:
        logger.error(f"[WEB_SEARCH] DDGS import or call failed: {e}")
    except Exception as e:  # noqa: BLE001 - DDGS search fallback boundary
        logger.error(f"[WEB_SEARCH] DDGS fallback failed: {e}")

    return json.dumps(
        {
            "error": f"Unable to fetch web search results for query: '{cleaned_query}'.",
            "results": [],
        }
    )


def web_search(query: str, max_results: int = 5) -> str:
    from core.security_utils import wrap_untrusted_content
    result = _web_search_inner(query, max_results)
    return wrap_untrusted_content(result)


# Security constraints for SSRF & outbound HTTP defense
MAX_URL_LENGTH = 2048
MAX_QUERY_LENGTH = 1024
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB response body cap


class PinnedIPBackend(SyncBackend):
    """
    Custom NetworkBackend that routes TCP connections directly to a pre-verified IP,
    defeating DNS-rebinding Time-Of-Check to Time-Of-Use (TOCTOU) exploits while
    preserving the original Host header and TLS SNI extension.
    """

    def __init__(self, pinned_ip: str):
        super().__init__()
        self._pinned_ip = pinned_ip

    def connect_tcp(self, host: str, port: int, *args, **kwargs):
        return super().connect_tcp(self._pinned_ip, port, *args, **kwargs)


class PinnedIPTransport(httpx.HTTPTransport):
    """
    HTTPTransport configured to use PinnedIPBackend for connection establishment.
    """

    def __init__(self, pinned_ip: str, **kwargs):
        super().__init__(**kwargs)
        if not (hasattr(self, "_pool") and hasattr(self._pool, "_network_backend")):
            raise RuntimeError(
                "PinnedIPTransport cannot bind PinnedIPBackend: httpx HTTPTransport internal "
                "_pool._network_backend attribute is missing. Refusing to proceed without IP pinning."
            )
        self._pool._network_backend = PinnedIPBackend(pinned_ip)

BLOCKED_HOSTNAMES = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
}

BLOCKED_DOMAIN_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
)

SUSPICIOUS_MUTATING_ACTIONS = {
    "edit",
    "save",
    "delete",
    "post",
    "submit",
    "create",
    "write",
    "destroy",
    "upload",
}

MUTATING_PARAM_KEYS = {
    "action",
    "act",
    "do",
    "cmd",
    "exec",
    "operation",
}


def is_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    Returns True if the IP address belongs to any restricted range:
    loopback, private, link-local, multicast, reserved, unspecified, or CGNAT.
    """
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True

    if getattr(ip, "ipv4_mapped", None) and is_ip_blocked(ip.ipv4_mapped):
        return True

    # Carrier Grade NAT (RFC 6598: 100.64.0.0/10)
    return isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10")


def validate_url_security(url: str) -> tuple[bool, str, str | None, str | None]:
    """
    Validates a URL against SSRF, dangerous mutating query parameters, and size limits.
    Returns (is_safe, cleaned_url, failure_reason, resolved_ip).
    """
    if not url or not isinstance(url, str) or not url.strip():
        return False, "", "URL cannot be empty.", None

    try:
        raw_parsed = urllib.parse.urlsplit(url.strip())
    except (ValueError, TypeError) as e:
        return False, url.strip(), f"Malformed URL: {e}", None

    if not raw_parsed.scheme:
        cleaned_url = f"https://{url.strip()}"
    else:
        cleaned_url = url.strip()

    if len(cleaned_url) > MAX_URL_LENGTH:
        return (
            False,
            cleaned_url,
            f"URL length ({len(cleaned_url)}) exceeds maximum permitted limit of {MAX_URL_LENGTH} characters.",
            None,
        )

    try:
        parsed = urllib.parse.urlsplit(cleaned_url)
    except (ValueError, TypeError) as e:
        return False, cleaned_url, f"Malformed URL: {e}", None

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, cleaned_url, f"Scheme '{scheme}' is not allowed. Only HTTP and HTTPS are permitted.", None

    if not parsed.netloc:
        return False, cleaned_url, "URL is missing a valid host.", None

    if parsed.username or parsed.password:
        return False, cleaned_url, "Embedded credentials (userinfo) in URLs are not permitted.", None

    # Validate query string
    if parsed.query:
        if len(parsed.query) > MAX_QUERY_LENGTH:
            return (
                False,
                cleaned_url,
                f"Query string length ({len(parsed.query)}) exceeds maximum permitted limit of {MAX_QUERY_LENGTH} characters.",
                None,
            )

        query_params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        for key, values in query_params.items():
            key_lower = key.lower()
            if key_lower in MUTATING_PARAM_KEYS:
                for val in values:
                    if val.lower().strip() in SUSPICIOUS_MUTATING_ACTIONS:
                        return (
                            False,
                            cleaned_url,
                            f"Blocked mutating parameter '{key}={val}' in query string.",
                            None,
                        )
            for val in values:
                if len(val) > 512:
                    return (
                        False,
                        cleaned_url,
                        f"Query parameter value for '{key}' exceeds maximum permitted limit (512 characters).",
                        None,
                    )

    # Validate hostname
    hostname = parsed.hostname
    if not hostname:
        return False, cleaned_url, "URL hostname could not be determined.", None

    hostname_lower = hostname.lower().strip(".")
    if hostname_lower in BLOCKED_HOSTNAMES:
        return False, cleaned_url, f"Access to '{hostname}' is blocked (loopback/unspecified host).", None

    for suffix in BLOCKED_DOMAIN_SUFFIXES:
        if hostname_lower == suffix[1:] or hostname_lower.endswith(suffix):
            return False, cleaned_url, f"Access to internal domain '{hostname}' is blocked.", None

    # Check if hostname is an IP literal
    try:
        ip = ipaddress.ip_address(hostname_lower)
        if is_ip_blocked(ip):
            return False, cleaned_url, f"Direct access to restricted IP address '{ip}' is blocked.", None
        return True, cleaned_url, None, str(ip)
    except ValueError:
        pass

    # Resolve hostname via DNS and check all resolved IP addresses
    primary_ip = None
    try:
        addr_info = socket.getaddrinfo(hostname_lower, None, proto=socket.IPPROTO_TCP)
        if not addr_info:
            return False, cleaned_url, f"Could not resolve host '{hostname_lower}'.", None
        for entry in addr_info:
            sockaddr = entry[4]
            ip_str = sockaddr[0]
            ip = ipaddress.ip_address(ip_str)
            if is_ip_blocked(ip):
                return False, cleaned_url, f"Host '{hostname_lower}' resolved to restricted IP address '{ip}'.", None
            if primary_ip is None:
                primary_ip = ip_str
    except socket.gaierror as e:
        return False, cleaned_url, f"DNS resolution failed for '{hostname_lower}': {e}", None
    except (OSError, ValueError) as e:
        return False, cleaned_url, f"Host validation error for '{hostname_lower}': {e}", None

    return True, cleaned_url, None, primary_ip


def _fetch_web_page_inner(url: str, max_chars: int = 4000) -> str:
    """
    Fetches the content of a web page and extracts clean, readable text/markdown.
    Strips scripts, styles, navigation, headers, footers, and ads.
    Guarded with SSRF protection, IP pinning against DNS rebinding, redirect security validation,
    a 2 MB streaming body cap, and Content-Type filtering.
    """
    if not url or not isinstance(url, str) or not url.strip():
        return json.dumps({"error": "URL cannot be empty."})

    is_safe, cleaned_url, reason, resolved_ip = validate_url_security(url)
    if not is_safe:
        logger.warning(f"[FETCH_WEB_PAGE] Blocked unsafe URL '{url}': {reason}")
        return json.dumps(
            {
                "error": f"Security violation: {reason}",
                "url": url.strip() if isinstance(url, str) else "",
            }
        )

    max_chars = max(500, min(int(max_chars), 15000))

    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        current_url = cleaned_url
        current_ip = resolved_ip
        redirect_count = 0

        while True:
            transport = PinnedIPTransport(current_ip) if current_ip else None
            with httpx.Client(transport=transport, timeout=12.0, follow_redirects=False) as client:
                with client.stream("GET", current_url, headers=headers) as response:
                    # Check for redirect (3xx status code with Location header)
                    if (300 <= response.status_code < 400) and response.headers.get("location"):
                        redirect_count += 1
                        if redirect_count > MAX_REDIRECTS:
                            return json.dumps(
                                {
                                    "error": f"Security violation: Exceeded maximum allowed redirects ({MAX_REDIRECTS}).",
                                    "url": cleaned_url,
                                }
                            )
                        loc = response.headers.get("location", "")
                        next_url = urllib.parse.urljoin(current_url, loc)
                        is_safe_redirect, validated_next, redirect_reason, next_ip = validate_url_security(next_url)
                        if not is_safe_redirect:
                            logger.warning(
                                f"[FETCH_WEB_PAGE] Blocked redirect to unsafe URL '{next_url}': {redirect_reason}"
                            )
                            return json.dumps(
                                {
                                    "error": f"Security violation on redirect: {redirect_reason}",
                                    "url": next_url,
                                }
                            )
                        current_url = validated_next
                        current_ip = next_ip
                        continue

                    if response.status_code != 200:
                        return json.dumps(
                            {
                                "error": f"Failed to fetch URL. HTTP status code: {response.status_code}",
                                "url": current_url,
                            }
                        )

                    # Validate Content-Type
                    content_type = response.headers.get("content-type", "").lower()
                    mime_type = content_type.split(";")[0].strip() if content_type else ""
                    if mime_type:
                        is_allowed_mime = (
                            mime_type.startswith("text/")
                            or mime_type in {
                                "application/json",
                                "application/xhtml+xml",
                                "application/xml",
                                "application/rss+xml",
                                "application/atom+xml",
                            }
                            or mime_type.endswith("+xml")
                            or mime_type.endswith("+json")
                        )
                        if not is_allowed_mime:
                            return json.dumps(
                                {
                                    "error": f"Unsupported or dangerous Content-Type '{mime_type}'. Only text, HTML, and JSON responses are allowed.",
                                    "url": current_url,
                                }
                            )

                    # Read streamed content with byte limit
                    content_chunks = []
                    total_bytes = 0
                    for chunk in response.iter_bytes(chunk_size=65536):
                        total_bytes += len(chunk)
                        if total_bytes > MAX_BODY_BYTES:
                            return json.dumps(
                                {
                                    "error": f"Response body exceeded maximum permitted limit of {MAX_BODY_BYTES} bytes (2 MB).",
                                    "url": current_url,
                                }
                            )
                        content_chunks.append(chunk)

                    raw_bytes = b"".join(content_chunks)
                    encoding = response.encoding or "utf-8"
                    body_text = raw_bytes.decode(encoding, errors="replace")
                    break

        soup = BeautifulSoup(body_text, "html.parser")

        # Extract title
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        elif soup.find("h1"):
            title = soup.find("h1").get_text(strip=True)

        # Remove noisy and non-content elements
        for tag in soup(
            [
                "script",
                "style",
                "nav",
                "footer",
                "header",
                "aside",
                "noscript",
                "svg",
                "iframe",
                "form",
                "button",
                "meta",
                "link",
            ]
        ):
            tag.decompose()

        # Extract text elements with paragraph separation
        main_content = soup.find("main") or soup.find("article") or soup.find("body") or soup
        text_lines = []
        for element in main_content.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr"]):
            line = element.get_text(separator=" ", strip=True)
            if line and len(line) > 10:
                text_lines.append(line)

        if not text_lines:
            raw_text = soup.get_text(separator="\n", strip=True)
            text_lines = [line.strip() for line in raw_text.splitlines() if len(line.strip()) > 15]

        cleaned_text = "\n\n".join(text_lines)
        cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text).strip()

        is_truncated = False
        if len(cleaned_text) > max_chars:
            cleaned_text = cleaned_text[:max_chars] + "\n... [Content Truncated]"
            is_truncated = True

        return json.dumps(
            {
                "url": current_url,
                "title": title,
                "content": cleaned_text if cleaned_text else "(No readable text found on page)",
                "content_length": len(cleaned_text),
                "truncated": is_truncated,
            },
            indent=2,
        )

    except httpx.TimeoutException:
        return json.dumps({"error": f"Request to '{cleaned_url}' timed out.", "url": cleaned_url})
    except httpx.HTTPStatusError as e:
        logger.error(f"[FETCH_WEB_PAGE] HTTP error fetching '{cleaned_url}': {e}")
        return json.dumps({"error": f"HTTP error fetching web page: {e}", "url": cleaned_url})
    except httpx.RequestError as e:
        logger.error(f"[FETCH_WEB_PAGE] Network error fetching '{cleaned_url}': {e}")
        return json.dumps({"error": f"Network error fetching web page: {e}", "url": cleaned_url})
    except Exception as e:  # noqa: BLE001 - Web page scraping error boundary returns error JSON
        logger.error(f"[FETCH_WEB_PAGE] Error fetching '{cleaned_url}': {e}")
        return json.dumps({"error": f"Error fetching web page: {e!s}", "url": cleaned_url})


def fetch_web_page(url: str, max_chars: int = 4000) -> str:
    from core.security_utils import wrap_untrusted_content
    result = _fetch_web_page_inner(url, max_chars)
    return wrap_untrusted_content(result)


async def async_web_search(query: str, max_results: int = 5) -> str:
    """Asynchronous wrapper for web_search that executes in a background thread."""
    return await asyncio.to_thread(web_search, query=query, max_results=max_results)


async def async_fetch_web_page(url: str, max_chars: int = 4000) -> str:
    """Asynchronous wrapper for fetch_web_page that executes in a background thread."""
    return await asyncio.to_thread(fetch_web_page, url=url, max_chars=max_chars)
