import json
import logging
import re
import urllib.parse
from typing import Any

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def web_search(query: str, max_results: int = 5) -> str:
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
                # DuckDuckGo redirect link parsing: //duckduckgo.com/l/?uddg=http%3A%2F%2F...
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

    except Exception as e:
        logger.warning(f"[WEB_SEARCH] DuckDuckGo HTML endpoint failed: {e}. Attempting fallback...")

    # Strategy 2: duckduckgo_search library fallback
    try:
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
    except Exception as e:
        logger.error(f"[WEB_SEARCH] DDGS fallback failed: {e}")

    return json.dumps(
        {
            "error": f"Unable to fetch web search results for query: '{cleaned_query}'.",
            "results": [],
        }
    )


def fetch_web_page(url: str, max_chars: int = 4000) -> str:
    """
    Fetches the content of a web page and extracts clean, readable text/markdown.
    Strips scripts, styles, navigation, headers, footers, and ads.
    """
    if not url or not url.strip():
        return json.dumps({"error": "URL cannot be empty."})

    cleaned_url = url.strip()
    if not (cleaned_url.startswith("http://") or cleaned_url.startswith("https://")):
        cleaned_url = f"https://{cleaned_url}"

    max_chars = max(500, min(int(max_chars), 15000))

    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            response = client.get(cleaned_url, headers=headers)

        if response.status_code != 200:
            return json.dumps(
                {
                    "error": f"Failed to fetch URL. HTTP status code: {response.status_code}",
                    "url": cleaned_url,
                }
            )

        soup = BeautifulSoup(response.text, "html.parser")

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
                "url": cleaned_url,
                "title": title,
                "content": cleaned_text if cleaned_text else "(No readable text found on page)",
                "content_length": len(cleaned_text),
                "truncated": is_truncated,
            },
            indent=2,
        )

    except httpx.TimeoutException:
        return json.dumps({"error": f"Request to '{cleaned_url}' timed out.", "url": cleaned_url})
    except Exception as e:
        logger.error(f"[FETCH_WEB_PAGE] Error fetching '{cleaned_url}': {e}")
        return json.dumps({"error": f"Error fetching web page: {str(e)}", "url": cleaned_url})


def web_scrape(url: str, max_chars: int = 4000) -> str:
    """Alias for fetch_web_page."""
    return fetch_web_page(url=url, max_chars=max_chars)
