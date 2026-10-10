"""End-to-end: run server.py as a real stdio MCP server against a local fake GitHub API."""

import json
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []  # (path, Authorization header)

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status, body, headers=None):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        type(self).seen.append((url.path, self.headers.get("Authorization")))
        path = url.path
        if path == "/users/octo/repos":
            self._send(200, [{"name": "demo", "language": "Python", "stargazers_count": 4,
                              "updated_at": "2026-09-30T00:00:00Z"}])
        elif path == "/repos/octo/demo":
            self._send(200, {"language": "Python", "description": "A demo",
                             "stargazers_count": 4, "forks_count": 1})
        elif path == "/repos/octo/demo/contributors":
            base = f"http://{self.headers['Host']}{path}?per_page=1"
            self._send(200, [{"login": "octo"}],
                       {"Link": f'<{base}&page=2>; rel="next", <{base}&page=45>; rel="last"'})
        elif path == "/search/issues":
            is_pr = "type:pr" in query["q"][0]
            self._send(200, {"total_count": 33 if is_pr else 77})
        elif path == "/users/octo/events/public":
            events = [{"type": "PushEvent", "created_at": NOW, "repo": {"name": "octo/demo"},
                       "payload": {"ref": "refs/heads/main", "head": "h", "before": "b"}}]
            self._send(200, events if query.get("page") == ["1"] else [])
        elif path == "/repos/octo/demo/commits":
            self._send(200, [{"sha": "f" * 40, "commit": {
                "message": "Ship it\n\nbody", "author": {"date": "2026-10-01T09:00:00Z"}}}])
        elif path == "/search/commits":
            self._send(200, {"items": [{"sha": "1234567890", "commit": {
                "message": "Fix bug", "author": {"date": "2026-09-20T09:00:00Z"}}}]})
        else:
            self._send(404, {"message": "Not Found"})


@pytest.fixture
def fake_github():
    FakeGitHub.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    thread.join(timeout=5)


def _params(base_url: str, token: str) -> StdioServerParameters:
    # All three variables are set explicitly so a developer's real .env can't leak in.
    return StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py")],
        cwd=str(ROOT),
        env={"GITHUB_API_BASE": base_url, "GITHUB_TOKEN": token, "GITHUB_USERNAME": "octo"},
    )


def _text(result) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


async def test_all_four_tools_over_real_stdio(fake_github):
    async with stdio_client(_params(fake_github, "e2e-token")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert len(tools) == 4
            assert all(t.annotations and t.annotations.readOnlyHint for t in tools.values())

            repos = _text(await session.call_tool(
                "github_list_repos", {"params": {"limit": 5}}))
            assert "- **demo** - Python | stars 4" in repos

            stats = _text(await session.call_tool(
                "github_get_repo_stats", {"params": {"repo_name": "demo"}}))
            assert "- **Contribuidores:** 45" in stats
            assert "- **Issues abiertos:** 77" in stats
            assert "- **Pull requests abiertos:** 33" in stats

            activity = _text(await session.call_tool(
                "github_get_repo_insights", {"params": {"days": 7}}))
            assert "- **octo/demo**: Ship it (2026-10-01)" in activity

            found = _text(await session.call_tool(
                "github_search_commit_messages",
                {"params": {"repo_name": "demo", "keyword": "bug"}}))
            assert "- Fix bug — 2026-09-20 (`1234567`)" in found

    assert FakeGitHub.seen, "the server never reached the fake GitHub API"
    assert all(auth == "Bearer e2e-token" for _, auth in FakeGitHub.seen)


async def test_path_traversal_attempt_is_rejected_and_never_sent(fake_github):
    async with stdio_client(_params(fake_github, "e2e-token")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "github_get_repo_stats", {"params": {"repo_name": "../../orgs/anthropics"}})
    assert result.isError is True
    assert not [p for p, _ in FakeGitHub.seen if "orgs" in p]
    assert FakeGitHub.seen == []


async def test_missing_token_sends_no_authorization_header(fake_github):
    async with stdio_client(_params(fake_github, "")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("github_list_repos", {"params": {}})
    assert FakeGitHub.seen == [("/users/octo/repos", None)]


async def test_unknown_repo_returns_readable_error_not_a_crash(fake_github):
    async with stdio_client(_params(fake_github, "e2e-token")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "github_get_repo_stats", {"params": {"repo_name": "does-not-exist"}})
    assert result.isError is False
    assert "no encontrado" in _text(result)
