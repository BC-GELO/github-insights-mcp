# GitHub Insights MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets an LLM client
(Claude Desktop, MCP Inspector, …) answer questions about a GitHub account. It exposes four
**read-only** tools built with Python, FastMCP, Pydantic and the GitHub REST API.

| Tool | What it does |
| --- | --- |
| `github_list_repos` | Lists the account's public repos (language, stars, last update). Optional `limit` (1–100). |
| `github_get_repo_stats` | Stars, forks, language, contributors, open issues and open pull requests for one repo. Counts are exact, not capped at one API page. |
| `github_get_repo_insights` | Recent push activity: commits from the last `days` days (1–90), grouped by repo. |
| `github_search_commit_messages` | Finds commits in one repo whose message matches a word or phrase. Optional `limit` (1–30). |

All tools return readable Markdown, and GitHub failures (401, 403, rate limits, 404, 5xx,
timeouts, network errors) come back as clear messages instead of stack traces.

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then edit it
```

| Variable | Required | Meaning |
| --- | --- | --- |
| `GITHUB_USERNAME` | no (default `BC-GELO`) | Account whose repositories are read. |
| `GITHUB_TOKEN` | no | Raises the rate limit from 60 to 5,000 requests/hour. Create a **fine-grained** token with read-only access; public data needs no extra permissions. |
| `GITHUB_API_BASE` | no | Override for GitHub Enterprise Server. |

## Use it

**MCP Inspector** (quickest way to try the tools):

```bash
mcp dev server.py
```

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "github-insights": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"],
      "env": { "GITHUB_USERNAME": "BC-GELO", "GITHUB_TOKEN": "<your token>" }
    }
  }
}
```

Then ask things like *"What did I push to GitHub this week?"* or *"Which commits in Carstuff
mention 'security'?"*

## Security notes

- **Read-only by design.** Every tool only issues `GET` requests and is annotated
  `readOnlyHint: true`.
- **Validated inputs.** Repo names must match GitHub's allowed characters, so a model (or a
  prompt-injected instruction) cannot use `../` or `/` to aim your token at other API paths.
  Commit-search keywords are sent as one quoted phrase, so search qualifiers (`org:…`) and
  operators (`OR`) cannot widen a search beyond the configured repo.
- **Token hygiene.** The token is read from the environment, sent only in the
  `Authorization` header, and never logged or included in error messages. Without a token,
  no `Authorization` header is sent at all.
- **Untrusted text.** Commit messages and repo descriptions are written by third parties
  and are passed to the model as-is. Treat them as data, not instructions.

## Known limits

- `github_list_repos` and the activity tool see **public** data only.
- GitHub's public events feed covers roughly the last 90 days and 300 events, and the
  activity tool looks at up to 15 recently pushed branches per request.
- GitHub's search API has its own, lower rate limit (30 requests/minute when authenticated).
- Since October 2025 GitHub no longer includes commit lists in push events, so activity
  details are fetched from the commits endpoint instead.

## Development

```bash
pip install -r requirements-dev.txt
pytest                               # unit tests + end-to-end test over real stdio
ruff check .
mypy server.py github_client.py
```

The end-to-end test launches `server.py` as a real MCP subprocess and talks to it through the
MCP client against a local fake GitHub API, so no network or token is needed. CI runs all
three checks on Python 3.10, 3.12 and 3.13.
