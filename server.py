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

#Tool for listing repositories
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

#Tool for getting repository statistics
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
async def get_repo_stats(repo_name: str) -> str:
    """Obtiene estadisiticas de un respositorio especifico del usuario/organización configurado.
    
    Args:
        repo_name (str): nombre del repositorio.

    Returns:
        str: Markdown con estadísticas, commits, contribuidores y issues del repositorio.
    """
    try:
        stats = await github_get(f"/repos/{GITHUB_USERNAME}/{repo_name}")
        commits_per_page = await github_get(f"/repos/{GITHUB_USERNAME}/{repo_name}/commits?per_page=1")
        contibutors = await github_get(f"/repos/{GITHUB_USERNAME}/{repo_name}/contributors")
        issues = await github_get(f"/repos/{GITHUB_USERNAME}/{repo_name}/issues", params={"state": "all"})

    except Exception as e:
        return format_github_error(e)

    if not stats:
        return f"No se encontraron estadísticas para el repositorio {repo_name}."

    lines = [f"## Estadísticas de {repo_name}\n"]
    lines.append(f"## Estadisticas Generales: {stats}\n")
    lines.append(f"## Cantidad de commits: {len(commits_per_page)}\n")
    lines.append(f"## Cantidad de contribuidores: {len(contibutors)}\n")
    lines.append(f"## Cantidad de issues: {len(issues)}\n")

    return "\n".join(lines)

#Tool for getting insights of repository recent activity
@mcp.tool(
    name="github_get_repo_insights",
    annotations={
        "title": "Obtener insights de un repositorio",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_recent_activity(days: int) -> str:
    """Resume la actividad reciente (commits vía push) del usuario en todos sus repos.
 
    Args:
        params (RecentActivityInput): days (int) - ventana de días a revisar.
 
    Returns:
        str: Markdown con los commits recientes agrupados por repo, dentro
        de la ventana de días indicada.
    """
    try:
        activity = await github_get(f"/users/{GITHUB_USERNAME}/events/public", params={"push_events": "true", "per_page": 100})
    except Exception as e:
        return format_github_error(e)

    if not activity:
        return "No se encontró actividad reciente."

    lines = [f"## Actividad Reciente (últimos {days} días)\n"]
    for event in activity:
        lines.append(f"- {event['type']} — {event['created_at'][:10]}")

    return "\n".join(lines)

#Tool for searching commit messages in a repository
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
async def search_commits(repo_name: str, keyword: str) -> str:
    """Busca commits que contengan una palabra clave dentro de un repositorio.
 
    Args:
        params (SearchCommitsInput): repo_name (str), keyword (str).
 
    Returns:
        str: Markdown con los commits que coinciden, su mensaje y fecha.
    """
    try:
        commits = await github_get(f"/search/commits?q={keyword}+repo:{GITHUB_USERNAME}/{repo_name}", headers={"Accept": "application/vnd.github.cloak-preview"})
    except Exception as e:
        return format_github_error(e)

    if not commits:
        return f"No se encontraron commits que coincidan con '{keyword}' en el repositorio {repo_name}."

    lines = [f"## Commits que coinciden con '{keyword}' en {repo_name}\n"]
    for commit in commits:
        lines.append(f"- {commit['commit']['message']} — {commit['commit']['author']['date'][:10]}")

    return "\n".join(lines)

if __name__ == "__main__":
    mcp.run()