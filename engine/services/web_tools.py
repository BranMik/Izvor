import asyncio
import contextvars
import inspect
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import urllib.parse
from typing import Any

from httpcore._backends.auto import AutoBackend
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_DDGS_ENGINE_LOCK = threading.Lock()

_turn_web_fetch_count: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "_turn_web_fetch_count", default=None
)


def reset_turn_web_budget() -> None:
    """Resets the per-turn web fetch count."""
    _turn_web_fetch_count.set([0])


def get_turn_web_fetch_count() -> int:
    """Returns current number of web fetches executed in this turn."""
    holder = _turn_web_fetch_count.get()
    return holder[0] if isinstance(holder, list) else 0


def check_and_increment_web_budget() -> None:
    """
    Verifies that the turn has not exceeded its outbound web request budget.
    Increments the budget counter or raises a PermissionError if exceeded.
    Thread-safe across asyncio.to_thread and thread pools using a shared mutable container.
    """
    sec_limits = get_web_security_limits()
    max_fetches = sec_limits.get("max_fetches_per_turn", 5)
    holder = _turn_web_fetch_count.get()
    if not isinstance(holder, list):
        holder = [0]
        _turn_web_fetch_count.set(holder)
    if holder[0] >= max_fetches:
        raise PermissionError(
            f"Web fetch budget exceeded: limit of {max_fetches} requests per turn reached."
        )
    holder[0] += 1



def _get_web_settings():
    try:
        from config_store.system_settings import system_settings_manager
        return system_settings_manager
    except Exception:  # noqa: BLE001
        return None


def get_web_user_agent() -> str:
    mgr = _get_web_settings()
    return mgr.get_web_fetch_user_agent() if mgr else USER_AGENT


def get_web_search_timeout() -> float:
    mgr = _get_web_settings()
    return mgr.get_web_search_timeout_seconds() if mgr else 10.0


def get_web_fetch_timeout() -> float:
    mgr = _get_web_settings()
    return mgr.get_web_fetch_timeout_seconds() if mgr else 12.0


def get_web_security_limits() -> dict[str, int]:
    mgr = _get_web_settings()
    return mgr.get_web_security() if mgr else {
        "max_url_length": 2048,
        "max_query_length": 1024,
        "max_redirects": 5,
        "max_body_bytes": 2097152,
        "max_fetches_per_turn": 5,
    }


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def _web_search_inner(query: str, max_results: int = 5) -> str:
    """
    Performs a live web search for the given query via ddgs (dual-layout Yahoo first,
    then non-blocked auto fallback) and returns JSON results.
    """
    if not query or not query.strip():
        return json.dumps({"error": "Search query cannot be empty."})

    cleaned_query = query.strip()
    max_results = max(1, min(int(max_results), 10))

    try:
        check_and_increment_web_budget()
    except PermissionError as pe:
        return json.dumps({"error": str(pe), "results": []})

    try:
        def _configure_ddgs_engines() -> None:
            """Tunes ddgs Yahoo xpath for dual card layouts and strips blocked engines from auto fallback."""
            try:
                from ddgs.engines import ENGINES

                text_engines = ENGINES.get("text", {})
                for key in list(text_engines.keys()):
                    if "duck" in key.lower():
                        text_engines.pop(key, None)

                yahoo_cls = text_engines.get("yahoo")
                if yahoo_cls is not None and not getattr(yahoo_cls, "_izvor_dual_xpath", False):
                    yahoo_cls.items_xpath = (
                        "//*[(self::div or self::section) "
                        "and (contains(@class, 'algo-sr') or contains(@class, 'relsrch')) "
                        "and not(contains(@class, 'ads'))]"
                    )
                    yahoo_cls.elements_xpath = {
                        "title": ".//div[contains(@class, 'Title')]//h3//text() | .//h3//text()",
                        "href": ".//div[contains(@class, 'Title')]//a/@href | .//a/@href",
                        "body": ".//div[contains(@class, 'Text') or contains(@class, 'grp-tsrc')]//text()",
                    }
                    yahoo_cls._izvor_dual_xpath = True
            except Exception as cfg_err:  # noqa: BLE001
                logger.debug(f"[WEB_SEARCH] Could not tune ddgs engines: {cfg_err}")

        def _collect_ddgs_results(ddgs_client: Any, backend: str) -> list[dict[str, str]]:
            collected: list[dict[str, str]] = []
            seen_urls: set[str] = set()
            fetch_limit = max(max_results * 2, 6)
            for r in ddgs_client.text(cleaned_query, max_results=fetch_limit, backend=backend):
                candidate_url = r.get("href", "") or r.get("link", "") or r.get("url", "")
                if not candidate_url:
                    continue
                if candidate_url.startswith("https://www.bing.com/aclick?"):
                    continue
                is_safe, _, _, _ = validate_url_security(candidate_url)
                if not is_safe or candidate_url in seen_urls:
                    continue
                seen_urls.add(candidate_url)
                collected.append(
                    {
                        "title": r.get("title", ""),
                        "url": candidate_url,
                        "snippet": r.get("body", "") or r.get("snippet", ""),
                    }
                )
                if len(collected) >= max_results:
                    break
            return collected

        def _run_ddgs() -> list[dict[str, str]]:
            from ddgs import DDGS

            with _DDGS_ENGINE_LOCK:
                _configure_ddgs_engines()
            timeout_sec = max(1, int(min(float(get_web_search_timeout()), 8.0)))
            last_exc: Exception | None = None
            for backend in ("yahoo", "yahoo", "auto"):
                try:
                    with DDGS(timeout=timeout_sec) as ddgs:
                        found = _collect_ddgs_results(ddgs, backend)
                        if found:
                            return found
                except Exception as b_err:  # noqa: BLE001 - Try next free scraping attempt/backend
                    last_exc = b_err
                    logger.warning(f"[WEB_SEARCH] DDGS backend '{backend}' failed: {b_err}")
            if last_exc is not None:
                raise last_exc
            return []

        ddgs_results = await asyncio.to_thread(_run_ddgs)
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
    except Exception as e:  # noqa: BLE001 - DDGS search error boundary
        logger.error(f"[WEB_SEARCH] DDGS search failed: {e}")

    return json.dumps(
        {
            "error": (
                f"Unable to fetch web search results for query: '{cleaned_query}'. "
                "Do NOT retry web_search for this query in this turn; proceed using available context."
            ),
            "retryable": False,
            "results": [],
        }
    )


_web_search_override: Any = None
_web_fetch_override: Any = None


def configure_web_search_fn(fn: Any) -> None:
    """Configures an in-memory web_search override for tests and dependency injection."""
    global _web_search_override
    _web_search_override = fn


def reset_web_search_fn() -> None:
    """Resets the in-memory web_search override."""
    global _web_search_override
    _web_search_override = None


def configure_web_fetch_fn(fn: Any) -> None:
    """Configures an in-memory fetch_web_page override for tests and dependency injection."""
    global _web_fetch_override
    _web_fetch_override = fn


def reset_web_fetch_fn() -> None:
    """Resets the in-memory fetch_web_page override."""
    global _web_fetch_override
    _web_fetch_override = None


async def web_search(query: str, max_results: int = 5) -> str:
    if _web_search_override is not None:
        res = _web_search_override(query, max_results=max_results)
        if inspect.isawaitable(res):
            return await res
        return res
    return await _web_search_inner(query, max_results)


# Security constraints for SSRF & outbound HTTP defense
MAX_URL_LENGTH = 2048
MAX_QUERY_LENGTH = 1024
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB response body cap


class PinnedIPBackend(AutoBackend):
    """
    Custom AsyncNetworkBackend that routes TCP connections directly to a pre-verified IP,
    defeating DNS-rebinding Time-Of-Check to Time-Of-Use (TOCTOU) exploits while
    preserving the original Host header and TLS SNI extension.
    """

    def __init__(self, pinned_ip: str):
        super().__init__()
        self._pinned_ip = pinned_ip

    async def connect_tcp(self, host: str, port: int, *args, **kwargs):
        return await super().connect_tcp(self._pinned_ip, port, *args, **kwargs)


class PinnedIPTransport(httpx.AsyncHTTPTransport):
    """
    AsyncHTTPTransport configured to use PinnedIPBackend for connection establishment.
    """

    def __init__(self, pinned_ip: str, **kwargs):
        super().__init__(**kwargs)
        if not (hasattr(self, "_pool") and hasattr(self._pool, "_network_backend")):
            raise RuntimeError(
                "PinnedIPTransport cannot bind PinnedIPBackend: httpx AsyncHTTPTransport internal "
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
    Return True if IP is non-global (private, loopback, link-local, CGNAT, NAT64, 6to4, Teredo, multicast, reserved).
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None and is_ip_blocked(ip.ipv4_mapped):
            return True
        if ip.sixtofour is not None and is_ip_blocked(ip.sixtofour):
            return True
        if ip.teredo is not None:
            server_ip, client_ip = ip.teredo
            if is_ip_blocked(server_ip) or is_ip_blocked(client_ip):
                return True
        # NAT64 well-known prefix 64:ff9b::/96 and local-use 64:ff9b:1::/48
        if ip in ipaddress.IPv6Network("64:ff9b::/96") or ip in ipaddress.IPv6Network("64:ff9b:1::/48"):
            return True
    return not ip.is_global


def sanitize_outbound_url(url: str) -> str:
    """Strips query and fragment from untrusted domains if strip_query_on_untrusted_domains is enabled."""
    try:
        sec_limits = get_web_security_limits()
        if not sec_limits.get("strip_query_on_untrusted_domains", False):
            return url
        allowed_domains = [str(d).lower().lstrip(".") for d in sec_limits.get("allowed_domains", []) if str(d).strip()]
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower().strip(".")
        if not allowed_domains or not any(host == d or host.endswith("." + d) for d in allowed_domains):
            return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        return url
    except Exception:  # noqa: BLE001
        return url



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

    sec_limits = get_web_security_limits()
    max_url_len = sec_limits.get("max_url_length", MAX_URL_LENGTH)
    if len(cleaned_url) > max_url_len:
        return (
            False,
            cleaned_url,
            f"URL length ({len(cleaned_url)}) exceeds maximum permitted limit of {max_url_len} characters.",
            None,
        )

    try:
        parsed = urllib.parse.urlsplit(cleaned_url)
    except (ValueError, TypeError) as e:
        return False, cleaned_url, f"Malformed URL: {e}", None

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, cleaned_url, f"Scheme '{scheme}' is not allowed. Only HTTP and HTTPS are permitted.", None

    # Port allowlist enforcement (including implicit default ports 80/443)
    allowed_ports = sec_limits.get("allowed_ports", [80, 443])
    try:
        port = parsed.port
    except ValueError:
        return False, cleaned_url, "Invalid port in URL.", None
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if effective_port not in allowed_ports:
        return False, cleaned_url, f"Port {effective_port} is not allowed. Permitted ports: {allowed_ports}.", None

    if not parsed.netloc:
        return False, cleaned_url, "URL is missing a valid host.", None

    if parsed.username or parsed.password:
        return False, cleaned_url, "Embedded credentials (userinfo) in URLs are not permitted.", None

    # Validate query string
    if parsed.query:
        max_query_len = sec_limits.get("max_query_length", MAX_QUERY_LENGTH)
        if len(parsed.query) > max_query_len:
            return (
                False,
                cleaned_url,
                f"Query string length ({len(parsed.query)}) exceeds maximum permitted limit of {max_query_len} characters.",
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

    # Domain allowlist enforcement (if configured)
    allowed_domains = [str(d).lower().lstrip(".") for d in sec_limits.get("allowed_domains", []) if str(d).strip()]
    if allowed_domains:
        if not any(hostname_lower == d or hostname_lower.endswith("." + d) for d in allowed_domains):
            return False, cleaned_url, f"Domain '{hostname}' is not in the configured web egress allowlist.", None

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


async def _fetch_web_page_inner(url: str, max_chars: int = 4000) -> str:
    """
    Fetches the content of a web page and extracts clean, readable text/markdown.
    Strips scripts, styles, navigation, headers, footers, and ads.
    Guarded with SSRF protection, IP pinning against DNS rebinding, redirect security validation,
    a 2 MB streaming body cap, and Content-Type filtering.
    """
    if not url or not isinstance(url, str) or not url.strip():
        return json.dumps({"error": "URL cannot be empty."})

    is_safe, cleaned_url, reason, resolved_ip = await asyncio.to_thread(validate_url_security, url)
    if not is_safe:
        logger.warning(f"[FETCH_WEB_PAGE] Blocked unsafe URL '{url}': {reason}")
        return json.dumps(
            {
                "error": f"Security violation: {reason}",
                "url": url.strip() if isinstance(url, str) else "",
            }
        )

    try:
        check_and_increment_web_budget()
    except PermissionError as pe:
        return json.dumps({"error": str(pe), "url": str(url)})

    cleaned_url = sanitize_outbound_url(cleaned_url)
    max_chars = max(500, min(int(max_chars), 15000))
    sec_limits = get_web_security_limits()
    max_redirects = sec_limits.get("max_redirects", MAX_REDIRECTS)
    max_body_bytes = sec_limits.get("max_body_bytes", MAX_BODY_BYTES)
    fetch_timeout = get_web_fetch_timeout()

    try:
        headers = {
            "User-Agent": get_web_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        current_url = cleaned_url
        current_ip = resolved_ip
        redirect_count = 0
        deadline = time.monotonic() + float(fetch_timeout)

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return json.dumps({"error": f"Request to '{cleaned_url}' timed out.", "url": cleaned_url})
            hop_timeout = httpx.Timeout(remaining, connect=min(remaining, 10.0), read=remaining)
            transport = PinnedIPTransport(current_ip) if current_ip else None
            async with httpx.AsyncClient(transport=transport, timeout=hop_timeout, follow_redirects=False) as client:
                async with client.stream("GET", current_url, headers=headers) as response:
                    # Check for redirect (3xx status code with Location header)
                    if (300 <= response.status_code < 400) and response.headers.get("location"):
                        redirect_count += 1
                        if redirect_count > max_redirects:
                            return json.dumps(
                                {
                                    "error": f"Security violation: Exceeded maximum allowed redirects ({max_redirects}).",
                                    "url": cleaned_url,
                                }
                            )
                        loc = response.headers.get("location", "")
                        next_url = urllib.parse.urljoin(current_url, loc)
                        is_safe_redirect, validated_next, redirect_reason, next_ip = await asyncio.to_thread(validate_url_security, next_url)
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
                        cleaned_redirect = sanitize_outbound_url(validated_next)
                        current_url = cleaned_redirect
                        current_ip = next_ip
                        continue

                    if response.status_code != 200:
                        return json.dumps(
                            {
                                "error": f"Failed to fetch URL. HTTP status code: {response.status_code}",
                                "url": current_url,
                            }
                        )

                    # Fail closed on missing, empty, or disallowed Content-Type
                    content_type = response.headers.get("content-type", "").lower()
                    mime_type = content_type.split(";")[0].strip() if content_type else ""
                    is_allowed_mime = bool(mime_type) and (
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
                                "error": f"Unsupported or dangerous Content-Type '{mime_type or 'missing'}'. Only text, HTML, and JSON responses are allowed.",
                                "url": current_url,
                            }
                        )

                    # Read streamed content with byte limit and monotonic wall-clock deadline
                    content_chunks = []
                    total_bytes = 0
                    aiter = response.aiter_bytes(chunk_size=65536).__aiter__()
                    while True:
                        chunk_remaining = deadline - time.monotonic()
                        if chunk_remaining <= 0:
                            raise httpx.ReadTimeout("Total wall-clock fetch deadline exceeded.")
                        try:
                            chunk = await asyncio.wait_for(aiter.__anext__(), timeout=chunk_remaining)
                        except StopAsyncIteration:
                            break
                        except TimeoutError as te:
                            raise httpx.ReadTimeout("Total wall-clock fetch deadline exceeded.") from te
                        total_bytes += len(chunk)
                        if total_bytes > max_body_bytes:
                            return json.dumps(
                                {
                                    "error": f"Response body exceeded maximum permitted limit of {max_body_bytes} bytes.",
                                    "url": current_url,
                                }
                            )
                        content_chunks.append(chunk)

                    raw_bytes = b"".join(content_chunks)
                    encoding = response.encoding or "utf-8"
                    body_text = raw_bytes.decode(encoding, errors="replace")
                    break

        def _extract_readable_html(text: str, target_url: str, limit_chars: int) -> str:
            soup = BeautifulSoup(text, "html.parser")

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
            if len(cleaned_text) > limit_chars:
                cleaned_text = cleaned_text[:limit_chars] + "\n... [Content Truncated]"
                is_truncated = True

            return json.dumps(
                {
                    "url": target_url,
                    "title": title,
                    "content": cleaned_text if cleaned_text else "(No readable text found on page)",
                    "content_length": len(cleaned_text),
                    "truncated": is_truncated,
                },
                indent=2,
            )

        return await asyncio.to_thread(_extract_readable_html, body_text, current_url, max_chars)

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


async def fetch_web_page(url: str, max_chars: int = 4000) -> str:
    if _web_fetch_override is not None:
        res = _web_fetch_override(url, max_chars=max_chars)
        if inspect.isawaitable(res):
            return await res
        return res
    return await _fetch_web_page_inner(url, max_chars)


