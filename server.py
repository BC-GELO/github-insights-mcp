"""GitHub Insights MCP server: four read-only tools over the GitHub REST API."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from github_client import (
    format_github_error,
    get_username,
    github_count,
    github_get,
    github_search_total,
    is_valid_full_name,
    is_valid_repo_name,
)

mcp = FastMCP("github_mcp")


def _read_only(title: str) -> ToolAnnotations:
    """Every tool here only reads from GitHub; say so to the MCP client."""
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )

MAX_EVENT_PAGES = 3  # the public events feed holds at most 300 events (3 x 100)
MAX_BRANCHES = 15  # cap on (repo, branch) pairs queried per activity request
COMMITS_PER_BRANCH = 30
MAX_MESSAGE_CHARS = 200


def _check_repo_name(value: str) -> str:
    if not is_valid_repo_name(value):
        raise ValueError(
            "Nombre de repositorio inválido: solo se permiten letras, números, "
            "'.', '-' y '_' (máximo 100 caracteres)."
        )
    return value


def _clean_keyword(value: str) -> str:
    # Quotes are removed so the keyword can be sent to GitHub as one quoted phrase.
    # That stops search qualifiers/operators ("org:x", "a OR b") from widening the
    # search beyond the configured repository.
    cleaned = " ".join(value.replace('"', " ").split())
    if not cleaned:
        raise ValueError("La palabra clave no puede estar vacía.")
    return cleaned


RepoName = Annotated[str, AfterValidator(_check_repo_name)]
Keyword = Annotated[str, AfterValidator(_clean_keyword)]


def _first_line(message: str | None) -> str:
    """First line of a commit message, so multi-line bodies don't break Markdown lists."""
    lines = (message or "").strip().splitlines()
    line = lines[0].strip() if lines else "(sin mensaje)"
    if len(line) > MAX_MESSAGE_CHARS:
        line = line[: MAX_MESSAGE_CHARS - 3] + "..."
    return line


async def _gather(*coros: Any) -> list[Any]:
    """Run coroutines concurrently; if any failed, wait for all and raise the first error."""
    results = await asyncio.gather(*coros, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return list(results)


# --------------------------------------------------------------------------- #
# Tool 1: list repositories
# --------------------------------------------------------------------------- #
class ListReposInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    limit: int | None = Field(
        default=20, ge=1, le=100,
        description="Cantidad máxima de repos a devolver (1-100).",
    )


@mcp.tool(
    name="github_list_repos",
    annotations=_read_only("Listar repositorios"),
)
async def list_repos(params: ListReposInput) -> str:
    """Lista los repositorios públicos del usuario configurado.

    Args:
        params (ListReposInput): limit (int, opcional) - cuántos repos devolver.

    Returns:
        str: Markdown con nombre, lenguaje principal, estrellas y fecha
        de última actualización de cada repo.
    """
    try:
        username = get_username()
        repos = await github_get(
            f"/users/{username}/repos",
            params={"per_page": params.limit, "sort": "updated"},
        )
    except Exception as e:
        return format_github_error(e)

    if not repos:
        return "No se encontraron repositorios."

    lines = [f"## Repositorios de {username}\n"]
    for repo in repos:
        lines.append(
            f"- **{repo['name']}** - {repo.get('language') or 'N/A'} | "
            f"stars {repo['stargazers_count']} | actualizado {repo['updated_at'][:10]}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tool 2: repository statistics
# --------------------------------------------------------------------------- #
class RepoStatsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    repo_name: RepoName = Field(
        ..., min_length=1,
        description="Nombre exacto del repositorio (ej. 'Carstuff_App').",
    )


@mcp.tool(
    name="github_get_repo_stats",
    annotations=_read_only("Obtener estadísticas de un repositorio"),
)
async def get_repo_stats(params: RepoStatsInput) -> str:
    """Obtiene estadísticas de un repositorio específico del usuario configurado.

    Args:
        params (RepoStatsInput): repo_name (str) - nombre del repositorio.

    Returns:
        str: Markdown con estrellas, forks, lenguaje, contribuidores, issues
        y pull requests abiertos del repositorio.
    """
    repo_name = params.repo_name
    try:
        username = get_username()
        repo_path = f"/repos/{username}/{repo_name}"
        # Counts come from Link headers / search totals so they stay exact above
        # the API's default page size of 30. GitHub's issues endpoint mixes issues
        # and pull requests, so they are counted separately via search.
        stats, contributors, open_issues, open_prs = await _gather(
            github_get(repo_path),
            github_count(f"{repo_path}/contributors"),
            github_search_total("issues", f"repo:{username}/{repo_name} type:issue state:open"),
            github_search_total("issues", f"repo:{username}/{repo_name} type:pr state:open"),
        )
    except Exception as e:
        return format_github_error(e)

    lines = [
        f"## Estadísticas de {repo_name}\n",
        f"- **Lenguaje principal:** {stats.get('language') or 'N/A'}",
        f"- **Descripción:** {stats.get('description') or 'Sin descripción'}",
        f"- **Estrellas:** {stats['stargazers_count']}",
        f"- **Forks:** {stats['forks_count']}",
        f"- **Contribuidores:** {contributors}",
        f"- **Issues abiertos:** {open_issues}",
        f"- **Pull requests abiertos:** {open_prs}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tool 3: recent activity
# --------------------------------------------------------------------------- #
class RecentActivityInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    days: int | None = Field(
        default=7, ge=1, le=90,
        description="Cuántos días hacia atrás revisar (1-90).",
    )


async def _recent_push_targets(username: str, cutoff: datetime) -> list[tuple[str, str]]:
    """Distinct (repo, branch) pairs the user pushed to since ``cutoff``, newest first.

    The public events feed is only used to discover *where* activity happened. GitHub
    removed the ``commits`` array from PushEvent payloads on 2025-10-07, so commit
    details are fetched from the commits endpoint instead.
    """
    targets: list[tuple[str, str]] = []
    for page in range(1, MAX_EVENT_PAGES + 1):
        events = await github_get(
            f"/users/{username}/events/public", params={"per_page": 100, "page": page}
        )
        if not events:
            break
        for event in events:
            created = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
            if created < cutoff or event.get("type") != "PushEvent":
                continue
            repo = event.get("repo", {}).get("name", "")
            ref = event.get("payload", {}).get("ref", "")
            if not is_valid_full_name(repo) or not ref.startswith("refs/heads/"):
                continue  # skip tag pushes and anything with an unexpected shape
            target = (repo, ref[len("refs/heads/"):])
            if target not in targets:
                targets.append(target)
        last_created = datetime.fromisoformat(events[-1]["created_at"].replace("Z", "+00:00"))
        if last_created < cutoff:
            break
    return targets


@mcp.tool(
    name="github_get_repo_insights",
    annotations=_read_only("Obtener actividad reciente"),
)
async def get_recent_activity(params: RecentActivityInput) -> str:
    """Resume la actividad reciente (commits vía push) del usuario en todos sus repos.

    Args:
        params (RecentActivityInput): days (int) - ventana de días a revisar.

    Returns:
        str: Markdown con los commits recientes agrupados por repo, dentro
        de la ventana de días indicada.
    """
    try:
        username = get_username()
        cutoff = datetime.now(timezone.utc) - timedelta(days=params.days or 7)
        targets = (await _recent_push_targets(username, cutoff))[:MAX_BRANCHES]
    except Exception as e:
        return format_github_error(e)

    if not targets:
        return f"No hubo actividad de tipo push en los últimos {params.days} días."

    since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    results = await asyncio.gather(
        *(
            github_get(
                f"/repos/{repo}/commits",
                params={
                    "sha": branch, "author": username, "since": since,
                    "per_page": COMMITS_PER_BRANCH,
                },
            )
            for repo, branch in targets
        ),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    if len(failures) == len(results):
        return format_github_error(failures[0])

    by_repo: dict[str, list[str]] = {}
    seen: set[str] = set()
    for (repo, _branch), commits in zip(targets, results, strict=True):
        if isinstance(commits, BaseException):
            continue
        for commit in commits:
            sha = commit.get("sha", "")
            if sha in seen:  # the same commit can be reachable from several branches
                continue
            seen.add(sha)
            info = commit.get("commit", {})
            date = (info.get("author") or {}).get("date", "")[:10]
            by_repo.setdefault(repo, []).append(
                f"- **{repo}**: {_first_line(info.get('message'))} ({date})"
            )

    if not by_repo:
        return (
            f"Se detectaron pushes en los últimos {params.days} días, pero no se "
            f"encontraron commits de {username} en esa ventana."
        )

    lines = [f"## Actividad reciente (últimos {params.days} días)\n"]
    for repo_lines in by_repo.values():
        lines.extend(repo_lines)
    if failures:
        lines.append(
            f"\n_No se pudieron consultar {len(failures)} rama(s); "
            "la lista puede estar incompleta._"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Tool 4: commit message search
# --------------------------------------------------------------------------- #
class SearchCommitsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    repo_name: RepoName = Field(..., min_length=1, description="Nombre exacto del repositorio.")
    keyword: Keyword = Field(
        ..., min_length=1, max_length=200,
        description="Palabra o frase a buscar en los mensajes de commit.",
    )
    limit: int | None = Field(
        default=10, ge=1, le=30,
        description="Cantidad máxima de commits a devolver (1-30).",
    )


@mcp.tool(
    name="github_search_commit_messages",
    annotations=_read_only("Buscar mensajes de commit en un repositorio"),
)
async def search_commits(params: SearchCommitsInput) -> str:
    """Busca commits que contengan una palabra o frase dentro de un repositorio.

    Args:
        params (SearchCommitsInput): repo_name (str), keyword (str), limit (int, opcional).

    Returns:
        str: Markdown con los commits que coinciden (más recientes primero),
        su mensaje y fecha.
    """
    try:
        username = get_username()
        result = await github_get(
            "/search/commits",
            params={
                "q": f'"{params.keyword}" repo:{username}/{params.repo_name}',
                "sort": "author-date",
                "order": "desc",
                "per_page": params.limit,
            },
        )
    except Exception as e:
        return format_github_error(e)

    commits = result.get("items", []) if isinstance(result, dict) else []

    if not commits:
        return (
            f"No se encontraron commits que coincidan con '{params.keyword}' "
            f"en el repositorio {params.repo_name}."
        )

    lines = [f"## Commits que coinciden con '{params.keyword}' en {params.repo_name}\n"]
    for commit in commits:
        info = commit["commit"]
        date = (info.get("author") or {}).get("date", "")[:10]
        sha = commit.get("sha", "")[:7]
        lines.append(f"- {_first_line(info.get('message'))} — {date} (`{sha}`)")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
