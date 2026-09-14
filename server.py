from datetime import datetime, timedelta, timezone
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional
import os
from dotenv import load_dotenv

from github_client import github_get, format_github_error

load_dotenv()
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "BC-GELO")

mcp = FastMCP("github_mcp")


class ListReposInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    limit: Optional[int] = Field(
        default=20, ge=1, le=100,
        description="Cantidad máxima de repos a devolver (1-100)."
    )


@mcp.tool(
    name="github_list_repos",
    annotations={
        "title": "Listar repositorios",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def list_repos(params: ListReposInput) -> str:
    """Lista los repositorios del usuario/organización configurado.

    Args:
        params (ListReposInput): limit (int, opcional) - cuántos repos devolver.

    Returns:
        str: Markdown con nombre, lenguaje principal, estrellas y fecha
        de última actualización de cada repo.
    """
    try:
        repos = await github_get(
            f"/users/{GITHUB_USERNAME}/repos",
            params={"per_page": params.limit, "sort": "updated"},
        )
    except Exception as e:
        return format_github_error(e)

    if not repos:
        return "No se encontraron repositorios."

    lines = [f"## Repositorios de {GITHUB_USERNAME}\n"]
    for repo in repos:
        lines.append(
            f"- **{repo['name']}** — {repo.get('language') or 'N/A'} · "
            f"⭐ {repo['stargazers_count']} · actualizado {repo['updated_at'][:10]}"
        )
    return "\n".join(lines)


class RepoStatsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    repo_name: str = Field(
        ..., min_length=1,
        description="Nombre exacto del repositorio (ej. 'Carstuff_App')."
    )


@mcp.tool(
    name="github_get_repo_stats",
    annotations={
        "title": "Obtener estadísticas de un repositorio",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_repo_stats(params: RepoStatsInput) -> str:
    """Obtiene estadísticas de un repositorio específico del usuario/organización configurado.

    Args:
        params (RepoStatsInput): repo_name (str) - nombre del repositorio.

    Returns:
        str: Markdown con estrellas, forks, lenguaje, contribuidores, issues
        y pull requests abiertos del repositorio.
    """
    repo_name = params.repo_name
    try:
        stats = await github_get(f"/repos/{GITHUB_USERNAME}/{repo_name}")
        contributors = await github_get(f"/repos/{GITHUB_USERNAME}/{repo_name}/contributors")
        # /issues devuelve issues Y pull requests mezclados en la API de GitHub;
        # los PRs siempre traen la clave 'pull_request', los issues reales no.
        issues_and_prs = await github_get(
            f"/repos/{GITHUB_USERNAME}/{repo_name}/issues",
            params={"state": "open"},
        )
    except Exception as e:
        return format_github_error(e)

    if not stats:
        return f"No se encontraron estadísticas para el repositorio {repo_name}."

    open_issues = [i for i in issues_and_prs if "pull_request" not in i]
    open_prs = [i for i in issues_and_prs if "pull_request" in i]

    lines = [
        f"## Estadísticas de {repo_name}\n",
        f"- **Lenguaje principal:** {stats.get('language') or 'N/A'}",
        f"- **Descripción:** {stats.get('description') or 'Sin descripción'}",
        f"- **Estrellas:** {stats['stargazers_count']}",
        f"- **Forks:** {stats['forks_count']}",
        f"- **Contribuidores:** {len(contributors)}",
        f"- **Issues abiertos:** {len(open_issues)}",
        f"- **Pull requests abiertos:** {len(open_prs)}",
    ]
    return "\n".join(lines)


class RecentActivityInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    days: Optional[int] = Field(
        default=7, ge=1, le=90,
        description="Cuántos días hacia atrás revisar (1-90)."
    )


@mcp.tool(
    name="github_get_repo_insights",
    annotations={
        "title": "Obtener actividad reciente",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
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
        events = await github_get(
            f"/users/{GITHUB_USERNAME}/events/public",
            params={"per_page": 100},
        )
    except Exception as e:
        return format_github_error(e)

    if not events:
        return "No se encontró actividad reciente."

    cutoff = datetime.now(timezone.utc) - timedelta(days=params.days)
    lines = [f"## Actividad reciente (últimos {params.days} días)\n"]
    found = False

    for event in events:
        if event["type"] != "PushEvent":
            continue
        event_date = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
        if event_date < cutoff:
            continue
        found = True
        repo_name = event["repo"]["name"]
        for commit in event["payload"].get("commits", []):
            lines.append(f"- **{repo_name}**: {commit['message']} ({event_date.date()})")

    if not found:
        return f"No hubo actividad de tipo push en los últimos {params.days} días."

    return "\n".join(lines)


class SearchCommitsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    repo_name: str = Field(..., min_length=1, description="Nombre exacto del repositorio.")
    keyword: str = Field(..., min_length=1, description="Palabra o frase a buscar en los mensajes de commit.")


@mcp.tool(
    name="github_search_commit_messages",
    annotations={
        "title": "Buscar mensajes de commit en un repositorio",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def search_commits(params: SearchCommitsInput) -> str:
    """Busca commits que contengan una palabra clave dentro de un repositorio.

    Args:
        params (SearchCommitsInput): repo_name (str), keyword (str).

    Returns:
        str: Markdown con los commits que coinciden, su mensaje y fecha.
    """
    try:
        result = await github_get(
            "/search/commits",
            params={"q": f"{params.keyword} repo:{GITHUB_USERNAME}/{params.repo_name}"},
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
        message = commit["commit"]["message"].split("\n")[0]  # solo la primera línea
        date = commit["commit"]["author"]["date"][:10]
        lines.append(f"- {message} — {date}")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()