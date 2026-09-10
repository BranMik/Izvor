import ipaddress
import json
from unittest.mock import MagicMock, patch

from services.mcp_tools import MCP_TOOLS_SCHEMAS, execute_mcp_tool
from services.web_tools import (
    fetch_web_page,
    is_ip_blocked,
    validate_url_security,
    web_search,
)


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
    with patch("services.mcp_tools.web_search", return_value=json.dumps({"query": "test", "results": []})) as mock_search:
        res = execute_mcp_tool("web_search", {"query": "test", "max_results": 3})
        mock_search.assert_called_once_with(query="test", max_results=3)
        assert "results" in res

    with patch("services.mcp_tools.fetch_web_page", return_value=json.dumps({"url": "https://example.com", "content": "mocked"})) as mock_fetch:
        res = execute_mcp_tool("fetch_web_page", {"url": "https://example.com", "max_chars": 1000})
        mock_fetch.assert_called_once_with(url="https://example.com", max_chars=1000)
        assert "mocked" in res

    with patch("services.mcp_tools.fetch_web_page", return_value=json.dumps({"url": "https://example.com", "content": "scraped"})) as mock_scrape:
        res = execute_mcp_tool("web_scrape", {"url": "https://example.com"})
        mock_scrape.assert_called_once_with(url="https://example.com", max_chars=4000)
        assert "scraped" in res


def test_fetch_web_page_ssrf_loopback():
    for blocked_url in [
        "http://localhost:8000/secret",
        "http://127.0.0.1:8000",
        "http://[::1]:8080",
        "http://0.0.0.0:80",
        "http://service.localhost",
    ]:
        res = fetch_web_page(blocked_url)
        data = json.loads(res)
        assert "error" in data
        assert "Security violation" in data["error"]


def test_fetch_web_page_ssrf_cloud_metadata():
    res = fetch_web_page("http://169.254.169.254/latest/meta-data")
    data = json.loads(res)
    assert "error" in data
    assert "Security violation" in data["error"]
    assert "restricted IP" in data["error"] or "blocked" in data["error"]


def test_fetch_web_page_ssrf_private_network():
    for private_url in [
        "http://10.0.0.1/admin",
        "http://192.168.1.1",
        "http://172.16.0.5",
        "http://100.64.0.1",
    ]:
        res = fetch_web_page(private_url)
        data = json.loads(res)
        assert "error" in data
        assert "Security violation" in data["error"]


def test_fetch_web_page_query_length_limit():
    huge_query = "https://example.com/search?q=" + ("x" * 1050)
    res = fetch_web_page(huge_query)
    data = json.loads(res)
    assert "error" in data
    assert "Query string length" in data["error"]


def test_fetch_web_page_blocked_mutating_actions():
    for dangerous_url in [
        "https://example.com/wiki?title=Test&action=edit",
        "https://example.com/wiki?title=Test&action=delete",
        "https://example.com/wiki?title=Test&action=save",
        "https://example.com/forum?topic=1&do=post",
        "https://example.com/api?cmd=destroy",
    ]:
        res = fetch_web_page(dangerous_url)
        data = json.loads(res)
        assert "error" in data
        assert "Blocked mutating parameter" in data["error"]


def test_fetch_web_page_allowed_safe_query_params():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.text = "<html><body><h1>Article Title</h1><p>Safe content paragraph.</p></body></html>"

    with patch("httpx.Client.get", return_value=mock_resp):
        res = fetch_web_page("https://example.com/wiki?title=Main_Page&action=view&lang=en")
        data = json.loads(res)
        assert "title" in data
        assert "Article Title" in data["title"]
        assert "Safe content paragraph." in data["content"]


def test_fetch_web_page_redirect_to_ssrf_blocked():
    redirect_resp = MagicMock()
    redirect_resp.status_code = 302
    redirect_resp.headers = {"location": "http://127.0.0.1:8000/internal-metrics"}

    with patch("httpx.Client.get", return_value=redirect_resp):
        res = fetch_web_page("https://example.com/redirect-target")
        data = json.loads(res)
        assert "error" in data
        assert "Security violation on redirect" in data["error"]


def test_fetch_web_page_too_many_redirects():
    redirect_resp = MagicMock()
    redirect_resp.status_code = 302
    redirect_resp.headers = {"location": "https://example.com/hop"}

    with patch("httpx.Client.get", return_value=redirect_resp):
        res = fetch_web_page("https://example.com/hop")
        data = json.loads(res)
        assert "error" in data
        assert "Exceeded maximum allowed redirects" in data["error"]


def test_is_ip_blocked_unit():
    assert is_ip_blocked(ipaddress.ip_address("127.0.0.1")) is True
    assert is_ip_blocked(ipaddress.ip_address("10.0.0.1")) is True
    assert is_ip_blocked(ipaddress.ip_address("172.16.0.1")) is True
    assert is_ip_blocked(ipaddress.ip_address("192.168.1.1")) is True
    assert is_ip_blocked(ipaddress.ip_address("169.254.169.254")) is True
    assert is_ip_blocked(ipaddress.ip_address("0.0.0.0")) is True
    assert is_ip_blocked(ipaddress.ip_address("100.64.0.1")) is True
    assert is_ip_blocked(ipaddress.ip_address("::1")) is True
    assert is_ip_blocked(ipaddress.ip_address("fe80::1")) is True
    assert is_ip_blocked(ipaddress.ip_address("fc00::1")) is True
    assert is_ip_blocked(ipaddress.ip_address("8.8.8.8")) is False
    assert is_ip_blocked(ipaddress.ip_address("93.184.215.14")) is False


def test_validate_url_security_schemes():
    ok, _, reason = validate_url_security("ftp://example.com/file.txt")
    assert ok is False
    assert "Only HTTP and HTTPS" in reason

    ok, _, reason = validate_url_security("javascript:alert(1)")
    assert ok is False

    ok, _, reason = validate_url_security("http://user:pass@example.com")
    assert ok is False
    assert "userinfo" in reason
