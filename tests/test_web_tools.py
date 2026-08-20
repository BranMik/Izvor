import json
from unittest.mock import MagicMock, patch
import pytest

from mcp_tools import MCP_TOOLS_SCHEMAS, execute_mcp_tool
from web_tools import fetch_web_page, web_scrape, web_search


def test_mcp_schemas_include_web_tools():
    tool_names = [tool["function"]["name"] for tool in MCP_TOOLS_SCHEMAS]
    assert "web_search" in tool_names
    assert "fetch_web_page" in tool_names


def test_web_search_empty_query():
    res = web_search("")
    data = json.loads(res)
    assert "error" in data
    assert "empty" in data["error"]


def test_web_search_mocked_html():
    sample_html = """
    <html>
      <body>
        <div class="result">
          <h2 class="result__title"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fproduct">Example Product</a></h2>
          <a class="result__snippet">The leading enterprise platform for multi-agent workflows.</a>
          <span class="result__url">example.com/product</span>
        </div>
      </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = sample_html

    with patch("httpx.Client.post", return_value=mock_resp):
        res = web_search("AI agent platform", max_results=3)
        data = json.loads(res)
        assert data.get("query") == "AI agent platform"
        assert data.get("results_count") == 1
        assert len(data.get("results", [])) == 1
        result = data["results"][0]
        assert result["title"] == "Example Product"
        assert result["url"] == "https://example.com/product"
        assert "multi-agent" in result["snippet"]


def test_fetch_web_page_empty_url():
    res = fetch_web_page("")
    data = json.loads(res)
    assert "error" in data


def test_fetch_web_page_clean_extraction():
    sample_html = """
    <!DOCTYPE html>
    <html>
      <head>
        <title>Competitor X - Pricing & Features</title>
        <style>body { font-size: 14px; }</style>
        <script>console.log("tracking");</script>
      </head>
      <body>
        <nav><a href="/home">Home</a></nav>
        <header>Header Banner</header>
        <main>
          <h1>Enterprise Agentic Platform</h1>
          <p>We provide full-stack autonomous AI agents with Slack integration.</p>
          <p>Pricing starts at $49/user/month with unlimited tool calls.</p>
        </main>
        <footer>Copyright 2026</footer>
      </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = sample_html

    with patch("httpx.Client.get", return_value=mock_resp):
        res = fetch_web_page("https://competitor.com/pricing", max_chars=2000)
        data = json.loads(res)
        assert data.get("url") == "https://competitor.com/pricing"
        assert data.get("title") == "Competitor X - Pricing & Features"
        content = data.get("content", "")
        assert "Enterprise Agentic Platform" in content
        assert "$49/user/month" in content
        assert "console.log" not in content
        assert "font-size" not in content
        assert data.get("truncated") is False


def test_fetch_web_page_truncation():
    sample_html = "<html><body><p>" + ("Market analysis detail paragraph. " * 100) + "</p></body></html>"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = sample_html

    with patch("httpx.Client.get", return_value=mock_resp):
        res = fetch_web_page("https://example.com/long-page", max_chars=500)
        data = json.loads(res)
        assert data.get("truncated") is True
        assert len(data.get("content", "")) <= 600


def test_fetch_web_page_http_error():
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "Not Found"

    with patch("httpx.Client.get", return_value=mock_resp):
        res = fetch_web_page("https://example.com/nonexistent")
        data = json.loads(res)
        assert "error" in data
        assert "404" in data["error"]


def test_execute_mcp_web_tools():
    with patch("mcp_tools.web_search", return_value=json.dumps({"query": "test", "results": []})) as mock_search:
        res = execute_mcp_tool("web_search", {"query": "test", "max_results": 3})
        mock_search.assert_called_once_with(query="test", max_results=3)
        assert "results" in res

    with patch("mcp_tools.fetch_web_page", return_value=json.dumps({"url": "https://example.com", "content": "mocked"})) as mock_fetch:
        res = execute_mcp_tool("fetch_web_page", {"url": "https://example.com", "max_chars": 1000})
        mock_fetch.assert_called_once_with(url="https://example.com", max_chars=1000)
        assert "mocked" in res

    with patch("mcp_tools.fetch_web_page", return_value=json.dumps({"url": "https://example.com", "content": "scraped"})) as mock_scrape:
        res = execute_mcp_tool("web_scrape", {"url": "https://example.com"})
        mock_scrape.assert_called_once_with(url="https://example.com", max_chars=4000)
        assert "scraped" in res
