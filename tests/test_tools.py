from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

import server


def _iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _push(repo="octo/app", ref="refs/heads/main", age=timedelta(hours=1), **payload):
    return {
        "type": "PushEvent",
        "created_at": _iso(age),
        "repo": {"name": repo},
        "payload": {"ref": ref, "head": "h", "before": "b", **payload},
    }


def _commit(sha, message, date="2026-09-30T12:00:00Z"):
    return {"sha": sha, "commit": {"message": message, "author": {"date": date}}}


def _page1(events):
    """Events feed that returns `events` on page 1 and nothing afterwards."""
    return lambda r: httpx.Response(200, json=events if r.url.params["page"] == "1" else [])


# --------------------------------- registration ---------------------------------- #
async def test_four_read_only_tools_are_registered():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert set(tools) == {
        "github_list_repos",
        "github_get_repo_stats",
        "github_get_repo_insights",
        "github_search_commit_messages",
    }
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False


# ------------------------------------ list repos --------------------------------- #
async def test_list_repos_formats_and_requests_correctly(gh):
    route = gh.get("/users/octo/repos").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "app", "language": "Python", "stargazers_count": 3,
                 "updated_at": "2026-09-01T10:00:00Z"},
                {"name": "notes", "language": None, "stargazers_count": 0,
                 "updated_at": "2026-08-15T10:00:00Z"},
            ],
        )
    )
    out = await server.list_repos(server.ListReposInput(limit=2))
    assert "## Repositorios de octo" in out
    assert "- **app** - Python | stars 3 | actualizado 2026-09-01" in out
    assert "- **notes** - N/A | stars 0 | actualizado 2026-08-15" in out
    params = route.calls[0].request.url.params
    assert params["per_page"] == "2" and params["sort"] == "updated"


async def test_list_repos_empty(gh):
    gh.get("/users/octo/repos").mock(return_value=httpx.Response(200, json=[]))
    assert await server.list_repos(server.ListReposInput()) == "No se encontraron repositorios."


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_list_repos_limit_bounds(limit):
    with pytest.raises(ValidationError):
        server.ListReposInput(limit=limit)


def test_inputs_forbid_unknown_fields():
    with pytest.raises(ValidationError):
        server.ListReposInput(limit=5, sneaky="x")


async def test_list_repos_reports_errors_instead_of_raising(gh):
    gh.get("/users/octo/repos").mock(return_value=httpx.Response(401))
    assert "token inválido" in await server.list_repos(server.ListReposInput())
    gh.get("/users/octo/repos").mock(side_effect=httpx.ConnectError("down"))
    assert "no se pudo conectar" in await server.list_repos(server.ListReposInput())


async def test_invalid_username_config_is_reported(gh, monkeypatch):
    monkeypatch.setenv("GITHUB_USERNAME", "../orgs/evil")
    out = await server.list_repos(server.ListReposInput())
    assert "configuración" in out
    assert not gh.calls


# ------------------------------------ repo stats --------------------------------- #
def _search_totals(request: httpx.Request) -> httpx.Response:
    q = request.url.params["q"]
    return httpx.Response(200, json={"total_count": 61 if "type:pr" in q else 142})


async def test_repo_stats_counts_are_exact_beyond_one_page(gh):
    gh.get("/repos/octo/app").mock(
        return_value=httpx.Response(
            200,
            json={"language": "Python", "description": "demo", "stargazers_count": 9,
                  "forks_count": 2},
        )
    )
    link = '<https://api.github.com/x?per_page=1&page=57>; rel="last"'
    gh.get("/repos/octo/app/contributors").mock(
        return_value=httpx.Response(200, json=[{"login": "a"}], headers={"Link": link})
    )
    gh.get("/search/issues").mock(side_effect=_search_totals)

    out = await server.get_repo_stats(server.RepoStatsInput(repo_name="app"))
    assert "- **Contribuidores:** 57" in out
    assert "- **Issues abiertos:** 142" in out
    assert "- **Pull requests abiertos:** 61" in out
    assert "- **Estrellas:** 9" in out and "- **Forks:** 2" in out
    searches = [c.request for c in gh.calls if c.request.url.path == "/search/issues"]
    queries = [r.url.params["q"] for r in searches]
    assert len(queries) == 2
    assert all("repo:octo/app" in q for q in queries)


async def test_repo_stats_empty_repo_has_zero_contributors(gh):
    gh.get("/repos/octo/empty").mock(
        return_value=httpx.Response(
            200, json={"language": None, "description": None, "stargazers_count": 0,
                       "forks_count": 0}
        )
    )
    gh.get("/repos/octo/empty/contributors").mock(return_value=httpx.Response(204))
    gh.get("/search/issues").mock(return_value=httpx.Response(200, json={"total_count": 0}))
    out = await server.get_repo_stats(server.RepoStatsInput(repo_name="empty"))
    assert "- **Contribuidores:** 0" in out
    assert "Sin descripción" in out and "N/A" in out


async def test_repo_stats_not_found(gh):
    gh.get("/repos/octo/nope").mock(return_value=httpx.Response(404))
    gh.get("/repos/octo/nope/contributors").mock(return_value=httpx.Response(404))
    gh.get("/search/issues").mock(return_value=httpx.Response(200, json={"total_count": 0}))
    out = await server.get_repo_stats(server.RepoStatsInput(repo_name="nope"))
    assert "no encontrado" in out


@pytest.mark.parametrize(
    "bad", ["../../orgs/anthropics", "a/b", "..", "", "a b", "x?y=1", "x" * 101]
)
async def test_repo_stats_rejects_path_traversal_before_any_request(gh, bad):
    with pytest.raises(ValidationError):
        server.RepoStatsInput(repo_name=bad)
    assert not gh.calls


# ---------------------------------- recent activity ------------------------------ #
async def test_recent_activity_works_without_commits_in_event_payload(gh):
    """Since 2025-10-07 GitHub no longer includes `commits` in PushEvent payloads."""
    gh.get("/users/octo/events/public").mock(side_effect=_page1([_push()]))
    commits = gh.get("/repos/octo/app/commits").mock(
        return_value=httpx.Response(
            200,
            json=[
                _commit("a" * 40, "Add login\n\nLong body\nline two", "2026-09-30T12:00:00Z"),
                _commit("b" * 40, "Fix typo", "2026-09-29T08:00:00Z"),
            ],
        )
    )
    out = await server.get_recent_activity(server.RecentActivityInput(days=7))
    assert "## Actividad reciente (últimos 7 días)" in out
    assert "- **octo/app**: Add login (2026-09-30)" in out
    assert "- **octo/app**: Fix typo (2026-09-29)" in out
    assert "Long body" not in out  # only the first line of each message
    params = commits.calls[0].request.url.params
    assert params["sha"] == "main" and params["author"] == "octo" and "since" in params


async def test_recent_activity_ignores_old_events_tags_and_other_event_types(gh):
    events = [
        {"type": "WatchEvent", "created_at": _iso(), "repo": {"name": "octo/app"}, "payload": {}},
        _push(ref="refs/tags/v1.0"),
        _push(age=timedelta(days=30)),
    ]
    gh.get("/users/octo/events/public").mock(return_value=httpx.Response(200, json=events))
    out = await server.get_recent_activity(server.RecentActivityInput(days=7))
    assert out == "No hubo actividad de tipo push en los últimos 7 días."
    assert [c.request.url.path for c in gh.calls] == ["/users/octo/events/public"]


async def test_recent_activity_skips_events_with_suspicious_repo_names(gh):
    bad_events = [_push(repo="octo/../../orgs/x"), _push(repo="a b/c")]
    gh.get("/users/octo/events/public").mock(
        side_effect=lambda r: httpx.Response(
            200, json=bad_events if r.url.params["page"] == "1" else []
        )
    )
    out = await server.get_recent_activity(server.RecentActivityInput(days=7))
    assert out.startswith("No hubo actividad")
    # only the events feed was queried: no request was ever built from the bad names
    assert {c.request.url.path for c in gh.calls} == {"/users/octo/events/public"}


async def test_recent_activity_dedupes_commits_shared_by_branches(gh):
    gh.get("/users/octo/events/public").mock(
        side_effect=lambda r: httpx.Response(
            200,
            json=[_push(ref="refs/heads/feature"), _push(ref="refs/heads/main")]
            if r.url.params["page"] == "1" else [],
        )
    )
    gh.get("/repos/octo/app/commits").mock(
        return_value=httpx.Response(200, json=[_commit("c" * 40, "Shared commit")])
    )
    out = await server.get_recent_activity(server.RecentActivityInput(days=7))
    assert out.count("Shared commit") == 1


async def test_recent_activity_partial_failure_is_reported(gh):
    gh.get("/users/octo/events/public").mock(
        side_effect=lambda r: httpx.Response(
            200,
            json=[_push(repo="octo/app"), _push(repo="octo/gone")]
            if r.url.params["page"] == "1" else [],
        )
    )
    gh.get("/repos/octo/app/commits").mock(
        return_value=httpx.Response(200, json=[_commit("d" * 40, "Works")])
    )
    gh.get("/repos/octo/gone/commits").mock(return_value=httpx.Response(404))
    out = await server.get_recent_activity(server.RecentActivityInput(days=7))
    assert "Works" in out and "No se pudieron consultar 1 rama(s)" in out


async def test_recent_activity_all_branch_lookups_failing_returns_error(gh):
    gh.get("/users/octo/events/public").mock(side_effect=_page1([_push()]))
    gh.get("/repos/octo/app/commits").mock(return_value=httpx.Response(403, headers={
        "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"}))
    out = await server.get_recent_activity(server.RecentActivityInput(days=7))
    assert "límite de rate" in out


async def test_recent_activity_pushes_but_no_commits_by_user(gh):
    gh.get("/users/octo/events/public").mock(side_effect=_page1([_push()]))
    gh.get("/repos/octo/app/commits").mock(return_value=httpx.Response(200, json=[]))
    out = await server.get_recent_activity(server.RecentActivityInput(days=7))
    assert "no se encontraron commits de octo" in out


async def test_recent_activity_pages_through_events_until_window_ends(gh):
    page1 = [_push(age=timedelta(minutes=i + 1)) for i in range(100)]
    route = gh.get("/users/octo/events/public").mock(
        side_effect=lambda r: httpx.Response(200, json=page1 if r.url.params["page"] == "1" else [])
    )
    gh.get("/repos/octo/app/commits").mock(return_value=httpx.Response(200, json=[]))
    await server.get_recent_activity(server.RecentActivityInput(days=7))
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]


async def test_recent_activity_stops_paging_once_events_are_older_than_window(gh):
    page1 = [_push(), _push(age=timedelta(days=40))]
    route = gh.get("/users/octo/events/public").mock(return_value=httpx.Response(200, json=page1))
    gh.get("/repos/octo/app/commits").mock(return_value=httpx.Response(200, json=[]))
    await server.get_recent_activity(server.RecentActivityInput(days=7))
    assert len(route.calls) == 1


@pytest.mark.parametrize("days", [0, 91])
def test_recent_activity_days_bounds(days):
    with pytest.raises(ValidationError):
        server.RecentActivityInput(days=days)


async def test_recent_activity_events_failure_is_reported(gh):
    gh.get("/users/octo/events/public").mock(return_value=httpx.Response(500))
    out = await server.get_recent_activity(server.RecentActivityInput())
    assert "no está disponible" in out


# ----------------------------------- search commits ------------------------------ #
async def test_search_commits_formats_results(gh):
    route = gh.get("/search/commits").mock(
        return_value=httpx.Response(
            200,
            json={"items": [
                {"sha": "abcdef1234567890", "commit": {
                    "message": "Fix login\n\nbody", "author": {"date": "2026-09-01T10:00:00Z"}}},
            ]},
        )
    )
    out = await server.search_commits(
        server.SearchCommitsInput(repo_name="app", keyword="login", limit=5)
    )
    assert "## Commits que coinciden con 'login' en app" in out
    assert "- Fix login — 2026-09-01 (`abcdef1`)" in out
    assert "body" not in out
    params = route.calls[0].request.url.params
    assert params["per_page"] == "5"
    assert params["sort"] == "author-date" and params["order"] == "desc"


@pytest.mark.parametrize(
    "keyword, expected_q",
    [
        ("login", '"login" repo:octo/app'),
        ("x org:other-org", '"x org:other-org" repo:octo/app'),  # qualifier stays inside the phrase
        ("a OR b", '"a OR b" repo:octo/app'),  # operator can't escape the repo scope
        ('say "hi"', '"say hi" repo:octo/app'),  # embedded quotes can't close the phrase
        ("  spaced   out  ", '"spaced out" repo:octo/app'),
    ],
)
async def test_search_query_is_scoped_to_the_configured_repo(gh, keyword, expected_q):
    route = gh.get("/search/commits").mock(return_value=httpx.Response(200, json={"items": []}))
    await server.search_commits(server.SearchCommitsInput(repo_name="app", keyword=keyword))
    assert route.calls[0].request.url.params["q"] == expected_q


@pytest.mark.parametrize("keyword", ["", "   ", '""', '" "', "x" * 201])
def test_search_keyword_validation(keyword):
    with pytest.raises(ValidationError):
        server.SearchCommitsInput(repo_name="app", keyword=keyword)


def test_search_limit_default_and_bounds():
    assert server.SearchCommitsInput(repo_name="app", keyword="x").limit == 10
    for limit in (0, 31):
        with pytest.raises(ValidationError):
            server.SearchCommitsInput(repo_name="app", keyword="x", limit=limit)


def test_search_rejects_traversal_repo_name():
    with pytest.raises(ValidationError):
        server.SearchCommitsInput(repo_name="../x", keyword="y")


async def test_search_no_results_and_errors(gh):
    gh.get("/search/commits").mock(return_value=httpx.Response(200, json={"items": []}))
    out = await server.search_commits(server.SearchCommitsInput(repo_name="app", keyword="zzz"))
    assert "No se encontraron commits que coincidan con 'zzz'" in out
    gh.get("/search/commits").mock(return_value=httpx.Response(422))
    out = await server.search_commits(server.SearchCommitsInput(repo_name="app", keyword="zzz"))
    assert "no ser válida" in out
