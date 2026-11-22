import asyncio
import ipaddress
import logging
from pathlib import Path
import re
import socket
import time
from typing import Any
import urllib.parse
import uuid

import httpx
from slack_sdk.errors import SlackApiError

from core.paths import get_downloads_dir
from services.rate_limiter import slack_rate_limiter
from services.slack.client_core import get_slack_async_client, get_slack_bot_token

logger = logging.getLogger(__name__)

SAFE_DOWNLOAD_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".md",
        ".pdf",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".log",
    }
)


def sanitize_slack_download_filename(file_name: str, prefix_len: int = 6) -> str:
    """
    Sanitizes untrusted filenames from Slack attachments:
    - Extracts base name, stripping any path traversal characters (../)
    - Sanitizes characters in filename stem and extension
    - Prepends a short UUID prefix to avoid collisions and overwrites
    - Neutralizes non-safe or executable extensions (.py, .sh, .bat, etc.) by appending .txt
    """
    raw_name = Path(file_name or "file").name
    stem = Path(raw_name).stem
    suffix = Path(raw_name).suffix

    clean_stem = re.sub(r"[^\w\-]", "_", stem).strip("._") or "file"
    clean_suffix = re.sub(r"[^\w\.]", "", suffix)
    if clean_suffix.lower() not in SAFE_DOWNLOAD_EXTENSIONS:
        clean_suffix = f"{clean_suffix}.txt"

    short_uuid = uuid.uuid4().hex[:prefix_len]
    return f"{short_uuid}_{clean_stem}{clean_suffix}"


def _extract_pdf_pages(raw_bytes: bytes) -> str:
    """Helper to synchronously parse PDF pages; executed in a background thread."""
    import io
    import pypdf

    pdf_stream = io.BytesIO(raw_bytes)
    reader = pypdf.PdfReader(pdf_stream)
    pages_text = []
    for idx, page in enumerate(reader.pages):
        page_text = (page.extract_text() or "").strip()
        if page_text:
            pages_text.append(f"--- Page {idx + 1} ---\n{page_text}")
        else:
            pages_text.append(f"--- Page {idx + 1} ---\n[No extractable text]")
    return "\n\n".join(pages_text) if pages_text else "[Empty PDF document]"


_fetch_thread_files_override: Any = None


def configure_fetch_thread_files(fn: Any) -> None:
    """Configures an in-memory fetch_thread_files override for tests and DI."""
    global _fetch_thread_files_override
    _fetch_thread_files_override = fn


def reset_fetch_thread_files() -> None:
    """Resets the in-memory fetch_thread_files override."""
    global _fetch_thread_files_override
    _fetch_thread_files_override = None


async def fetch_thread_files(channel_id: str, thread_ts: str, max_messages: int = 500) -> list[dict[str, Any]]:
    """
    Fetches all file attachment metadata uploaded in a Slack thread.
    Implements cursor-based pagination (up to max_messages) to discover files across long threads.
    Returns a list of file dicts with keys: id, name, title, mimetype, filetype, size, url_private_download, url_private.
    """
    if _fetch_thread_files_override is not None:
        res = _fetch_thread_files_override(channel_id, thread_ts, max_messages=max_messages)
        if asyncio.iscoroutine(res):
            return await res
        return res

    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        return []

    try:
        client = get_slack_async_client()
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        max_pages = max(1, (max_messages + 99) // 100)

        for _ in range(max_pages):
            kwargs: dict[str, Any] = {
                "channel": channel_id,
                "ts": thread_ts,
                "limit": 100,
            }
            if cursor:
                kwargs["cursor"] = cursor

            response = await client.conversations_replies(**kwargs)
            page_messages = response.get("messages", [])
            messages.extend(page_messages)

            if not response.get("has_more"):
                break

            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        thread_files: list[dict[str, Any]] = []
        for msg in messages:
            files = msg.get("files", [])
            for f in files:
                thread_files.append(
                    {
                        "id": f.get("id"),
                        "name": f.get("name", "unknown_file"),
                        "title": f.get("title", ""),
                        "mimetype": f.get("mimetype", ""),
                        "filetype": f.get("filetype", ""),
                        "size": f.get("size", 0),
                        "url_private_download": f.get("url_private_download") or f.get("url_private", ""),
                        "url_private": f.get("url_private", ""),
                        "user": f.get("user") or msg.get("user"),
                        "ts": msg.get("ts"),
                    }
                )
        logger.info(f"[SLACK FILES] Found {len(thread_files)} files in thread {thread_ts}")
        return thread_files
    except SlackApiError as e:
        logger.error(f"[SLACK FILES] Error fetching thread files: {e.response.get('error', e)}")
        return []
    except Exception as e:  # noqa: BLE001 - File listing error boundary returns empty list
        logger.error(f"[SLACK FILES] Unexpected error fetching thread files: {e}")
        return []


_TRUSTED_SLACK_FILE_HOSTS = frozenset(
    {
        "files.slack.com",
        "files-edu.slack.com",
        "slack-files.com",
    }
)


def _is_trusted_slack_file_url(url: str) -> tuple[bool, str]:
    """
    Requires HTTPS scheme and a trusted Slack file domain before ever
    attaching the Slack Bearer token or allowing initial file download.
    """
    parsed_url = urllib.parse.urlsplit(url)
    scheme = (parsed_url.scheme or "").lower()
    hostname = (parsed_url.hostname or "").lower()
    if scheme != "https":
        return (
            False,
            f"Security violation: Slack file download URL must use https:// (got scheme '{scheme or 'none'}').",
        )
    is_slack_host = (
        hostname in _TRUSTED_SLACK_FILE_HOSTS
        or hostname.endswith(".slack.com")
        or hostname.endswith(".slack-files.com")
    )
    if not is_slack_host:
        return (
            False,
            f"Security violation: File download URL domain '{hostname}' is not a recognized Slack domain.",
        )
    return True, ""


def _validate_slack_redirect_url(next_url: str) -> tuple[bool, str | None, str | None]:
    """
    Validates a Slack file download HTTP redirect target against HTTPS-only and SSRF
    protections (blocked hostnames, internal suffixes, and non-global IPs), returning
    the resolved global IP for PinnedIPTransport without blocking the asyncio loop.
    """
    from services.web_tools import BLOCKED_DOMAIN_SUFFIXES, BLOCKED_HOSTNAMES, is_ip_blocked

    try:
        parsed = urllib.parse.urlsplit(next_url)
    except (ValueError, TypeError) as e:
        return False, f"Malformed redirect URL: {e}", None

    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        return False, f"Redirect scheme '{scheme or 'none'}' is not allowed; only HTTPS is permitted.", None

    if not parsed.netloc:
        return False, "Redirect URL is missing a valid host.", None

    if parsed.username or parsed.password:
        return False, "Embedded credentials (userinfo) in redirect URLs are not permitted.", None

    try:
        port = parsed.port
    except ValueError:
        return False, "Invalid port in redirect URL.", None
    if port is not None and port != 443:
        return False, f"Redirect port {port} is not allowed; only port 443 is permitted.", None

    hostname = parsed.hostname
    if not hostname:
        return False, "Redirect hostname could not be determined.", None

    hostname_lower = hostname.lower().strip(".")
    if hostname_lower in BLOCKED_HOSTNAMES:
        return False, f"Redirect to blocked host '{hostname}' is forbidden.", None

    for suffix in BLOCKED_DOMAIN_SUFFIXES:
        if hostname_lower == suffix[1:] or hostname_lower.endswith(suffix):
            return False, f"Redirect to internal domain '{hostname}' is blocked.", None

    try:
        ip = ipaddress.ip_address(hostname_lower)
        if is_ip_blocked(ip):
            return False, f"Redirect to restricted IP address '{ip}' is blocked.", None
        return True, None, str(ip)
    except ValueError:
        pass

    primary_ip: str | None = None
    try:
        addr_info = socket.getaddrinfo(hostname_lower, None, proto=socket.IPPROTO_TCP)
        if not addr_info:
            return False, f"Could not resolve redirect host '{hostname_lower}'.", None
        for entry in addr_info:
            ip_str = entry[4][0]
            ip = ipaddress.ip_address(ip_str)
            if is_ip_blocked(ip):
                return False, f"Redirect host '{hostname_lower}' resolved to restricted IP '{ip}'.", None
            if primary_ip is None:
                primary_ip = ip_str
    except socket.gaierror as e:
        return False, f"DNS resolution failed for redirect host '{hostname_lower}': {e}", None
    except (OSError, ValueError) as e:
        return False, f"Redirect host validation error for '{hostname_lower}': {e}", None

    return True, None, primary_ip


async def download_slack_file(file_info: dict[str, Any], max_chars: int = 50000) -> dict[str, Any]:
    """
    Downloads a private Slack file attachment using SLACK_BOT_TOKEN and extracts its text content.
    Returns dict: {"ok": bool, "file_name": str, "content": str, "truncated": bool, "size": int, "error": str | None}
    """
    file_name = file_info.get("name", "unknown_file")
    url = file_info.get("url_private_download") or file_info.get("url_private")
    file_size = file_info.get("size", 0)

    from orchestration.system_settings import (
        get_max_file_size_bytes,
        get_max_file_size_mb,
    )

    max_size_bytes = get_max_file_size_bytes()
    max_size_mb = get_max_file_size_mb()

    if file_size > max_size_bytes:
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": f"File '{file_name}' ({file_size} bytes) exceeds maximum allowed size of {max_size_mb:.1f} MB.",
        }

    if not url:
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": "No download URL available for this file.",
        }

    # Verify HTTPS scheme and origin hostname belongs to allowed Slack domains before attaching Bearer auth
    is_trusted, trust_err = _is_trusted_slack_file_url(url)
    if not is_trusted:
        logger.warning(f"[SLACK FILE] Blocked unsafe/non-Slack file download URL '{url}': {trust_err}")
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": trust_err,
        }

    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        return {
            "ok": True,
            "file_name": file_name,
            "content": f"[Mock file content for {file_name}]",
            "truncated": False,
            "size": file_size,
            "error": None,
        }

    try:
        from orchestration.system_settings import get_slack_file_download_timeout_seconds

        download_timeout = get_slack_file_download_timeout_seconds()
    except Exception:  # noqa: BLE001
        download_timeout = 30.0

    try:
        from services.web_tools import PinnedIPTransport

        current_dl_url = url
        current_ip: str | None = None
        dl_redirect_count = 0
        max_dl_redirects = 5
        raw_bytes = b""

        while True:
            is_slack_target, _ = _is_trusted_slack_file_url(current_dl_url)
            req_headers = {"Authorization": f"Bearer {bot_token}"} if is_slack_target else {}
            client_kwargs: dict[str, Any] = {
                "timeout": download_timeout,
                "follow_redirects": False,
            }
            if current_ip:
                client_kwargs["transport"] = PinnedIPTransport(current_ip)

            next_dl_url: str | None = None
            next_ip: str | None = None
            async with httpx.AsyncClient(**client_kwargs) as client:
                async with client.stream("GET", current_dl_url, headers=req_headers) as resp:
                    resp_headers = getattr(resp, "headers", None) or {}
                    loc = resp_headers.get("location") if (300 <= resp.status_code < 400) else None
                    if loc:
                        dl_redirect_count += 1
                        if dl_redirect_count > max_dl_redirects:
                            return {
                                "ok": False,
                                "file_name": file_name,
                                "content": "",
                                "truncated": False,
                                "size": file_size,
                                "error": f"Security violation: Exceeded maximum allowed redirects ({max_dl_redirects}) on Slack file download.",
                            }
                        next_dl_url = urllib.parse.urljoin(current_dl_url, str(loc))
                        is_safe_dl, dl_err, resolved_next_ip = await asyncio.to_thread(
                            _validate_slack_redirect_url, next_dl_url
                        )
                        if not is_safe_dl:
                            logger.warning(
                                f"[SLACK FILE] Blocked redirect to unsafe URL '{next_dl_url}': {dl_err}"
                            )
                            return {
                                "ok": False,
                                "file_name": file_name,
                                "content": "",
                                "truncated": False,
                                "size": file_size,
                                "error": f"Security violation on redirect: {dl_err}",
                            }
                        next_ip = resolved_next_ip
                    else:
                        if resp.status_code != 200:
                            return {
                                "ok": False,
                                "file_name": file_name,
                                "content": "",
                                "truncated": False,
                                "size": file_size,
                                "error": f"Slack file download failed with HTTP status {resp.status_code}",
                            }

                        chunks = []
                        total_bytes = 0
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            total_bytes += len(chunk)
                            if total_bytes > max_size_bytes:
                                return {
                                    "ok": False,
                                    "file_name": file_name,
                                    "content": "",
                                    "truncated": False,
                                    "size": total_bytes,
                                    "error": f"File '{file_name}' ({total_bytes} bytes) exceeds maximum allowed size of {max_size_mb:.1f} MB.",
                                }
                            chunks.append(chunk)

                        raw_bytes = b"".join(chunks)
                        break

            current_dl_url = next_dl_url
            current_ip = next_ip

        # Cache local copy to workspace safely
        try:
            downloads_dir = get_downloads_dir().resolve()
            downloads_dir.mkdir(parents=True, exist_ok=True)
            safe_name = sanitize_slack_download_filename(file_name, prefix_len=6)
            file_path = (downloads_dir / safe_name).resolve()
            if not file_path.is_relative_to(downloads_dir):
                raise PermissionError(
                    f"Path traversal detected: '{file_name}' resolves outside downloads directory"
                )
            await asyncio.to_thread(file_path.write_bytes, raw_bytes)
            logger.info(f"[SLACK FILE] Cached Slack file safely to workspace: {file_path}")
        except (OSError, PermissionError) as save_err:
            logger.warning(f"[SLACK FILE] Could not save local copy to workspace: {save_err}")

        if file_name.lower().endswith(".pdf"):
            try:
                text_content = await asyncio.to_thread(_extract_pdf_pages, raw_bytes)
            except Exception as pdf_err:  # noqa: BLE001 - PDF parsing error boundary falls back cleanly
                logger.warning(f"[SLACK FILE] Could not parse PDF attachment {file_name}: {pdf_err}")
                text_content = f"[Failed to extract text from PDF: {pdf_err}]"
        else:
            text_content = raw_bytes.decode("utf-8", errors="replace")

        truncated = False
        if len(text_content) > max_chars:
            text_content = (
                text_content[:max_chars] + f"\n\n... [TRUNCATED: File exceeds {max_chars} characters] ..."
            )
            truncated = True

        return {
            "ok": True,
            "file_name": file_name,
            "content": text_content,
            "truncated": truncated,
            "size": len(raw_bytes),
            "error": None,
        }
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.error(f"[SLACK FILE] Network error downloading Slack file {file_name}: {e}")
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": str(e),
        }
    except Exception as e:  # noqa: BLE001 - File download error boundary returns error dict
        logger.error(f"[SLACK FILE] Error downloading Slack file {file_name}: {e}")
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": str(e),
        }


async def upload_text_file_to_slack(
    channel_id: str,
    content: str,
    filename: str = "scribe_notes.txt",
    title: str = "Discussion Notes",
    initial_comment: str | None = None,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """
    Uploads a plain text file directly to a Slack channel or DM.
    Uses modern files_upload_v2.
    """
    from services.security import scan_output_for_secrets

    matched_rule = scan_output_for_secrets(content, user_id="file_uploader")
    if matched_rule:
        logger.warning(f"[SECURITY] File upload '{filename}' blocked by DLP rule: {matched_rule}")
        return {
            "ok": False,
            "error": f"Security violation: File content matches restricted DLP pattern '{matched_rule}'.",
        }

    bot_token = get_slack_bot_token()
    if (
        not bot_token
        or bot_token == "xoxb-your-bot-user-oauth-token"
        or (channel_id and str(channel_id).startswith("mock_"))
    ):
        mock_file_id = f"mock_file_{int(time.time() * 1000)}"
        logger.info(
            f"[MOCK FILE] Uploaded mock file '{filename}' ({len(content)} chars) to {channel_id}: {mock_file_id}"
        )
        return {
            "ok": True,
            "file": {
                "id": mock_file_id,
                "name": filename,
                "title": title,
                "channels": [channel_id],
                "content": content,
            },
            "mock": True,
        }

    try:
        await slack_rate_limiter.acquire()
        client = get_slack_async_client()
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "content": content,
            "filename": filename,
            "title": title,
        }
        if initial_comment:
            kwargs["initial_comment"] = initial_comment
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        res = await client.files_upload_v2(**kwargs)
        file_data = res.data if hasattr(res, "data") else res
        logger.info(f"[SLACK FILE] Successfully uploaded file '{filename}' to {channel_id}")
        return {"ok": True, "file": file_data}
    except SlackApiError as e:
        logger.error(f"[SLACK FILE] SlackApiError uploading file: {e.response.get('error', e)}")
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - File upload boundary returns error dict
        logger.error(f"[SLACK FILE] Error uploading file: {e}")
        return {"ok": False, "error": str(e)}


def cleanup_old_downloads(max_age_seconds: float = 86400.0, downloads_dir: Path | None = None) -> int:
    """
    Cleans up cached downloaded Slack files older than max_age_seconds (default 24h).
    Checks either explicit downloads_dir or dynamic get_downloads_dir().
    Returns the count of evicted files.
    """
    now = time.time()
    evicted_count = 0
    target_dir = downloads_dir or get_downloads_dir()
    candidate_dirs = [
        target_dir.resolve(),
    ]
    seen_dirs: set[Path] = set()
    for d in candidate_dirs:
        if d in seen_dirs or not d.exists() or not d.is_dir():
            continue
        seen_dirs.add(d)
        try:
            for item in d.iterdir():
                if item.is_file():
                    try:
                        mtime = item.stat().st_mtime
                        if (now - mtime) > max_age_seconds:
                            item.unlink(missing_ok=True)
                            evicted_count += 1
                    except Exception as e:  # noqa: BLE001
                        logger.debug(f"[CLEANUP] Failed to prune downloaded file {item}: {e}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CLEANUP] Error inspecting downloads directory {d}: {e}")

    if evicted_count > 0:
        logger.info(f"[CLEANUP] Pruned {evicted_count} downloaded file(s) older than {max_age_seconds}s")
    return evicted_count
