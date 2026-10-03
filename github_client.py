"""Async client and error handling for the GitHub REST API used by the MCP tools.

Configuration is read from the environment on every call (not at import time) so
that it can be changed in tests and so a missing token never produces a bogus
``Authorization: Bearer None`` header.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("github_insights_mcp")

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_USERNAME = "BC-GELO"
REQUEST_TIMEOUT = 15.0

# GitHub repo names: letters, digits, '.', '-' and '_' only. Anything else (notably
# '/' and '..') could be used to point authenticated requests at other API paths.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")

_warned_no_token = False


class ConfigError(Exception):
    """Raised when the server is misconfigured (e.g. an invalid GITHUB_USERNAME)."""


def is_valid_repo_name(name: str) -> bool:
    return bool(_REPO_NAME_RE.match(name)) and name not in {".", ".."}


def is_valid_full_name(name: str) -> bool:
    if not _FULL_NAME_RE.match(name):
        return False
    return all(part not in {".", ".."} for part in name.split("/"))


def get_api_base() -> str:
    return os.getenv("GITHUB_API_BASE", DEFAULT_API_BASE).strip().rstrip("/")


def get_username() -> str:
    name = os.getenv("GITHUB_USERNAME", DEFAULT_USERNAME).strip()
    if not _USERNAME_RE.match(name):
        raise ConfigError("GITHUB_USERNAME no es un nombre de usuario válido de GitHub.")
    return name


def build_headers() -> dict[str, str]:
    """Request headers. The Authorization header is only sent when a token exists."""
    global _warned_no_token
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-insights-mcp",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif not _warned_no_token:
        _warned_no_token = True
        logger.warning(
            "GITHUB_TOKEN no está configurado: se usará la API sin autenticar "
            "(límite de 60 solicitudes por hora)."
        )
    return headers


async def github_request(path: str, params: dict[str, Any] | None = None) -> httpx.Response:
    """GET against the GitHub API; raises ``httpx.HTTPStatusError`` on 4xx/5xx."""
    async with httpx.AsyncClient(
        base_url=get_api_base(), headers=build_headers(), timeout=REQUEST_TIMEOUT
    ) as client:
        response = await client.get(path, params=params or {})
        response.raise_for_status()
        return response


async def github_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET and decode JSON. Empty bodies (e.g. 204 for an empty repo) decode to ``[]``."""
    response = await github_request(path, params)
    if response.status_code == 204 or not response.content:
        return []
    return response.json()


async def github_count(path: str, params: dict[str, Any] | None = None) -> int:
    """Exact size of a paginated list without downloading every page.

    Requests one item per page and reads the page number from the ``Link: rel="last"``
    header, which equals the total item count.
    """
    response = await github_request(path, {**(params or {}), "per_page": 1})
    if response.status_code == 204 or not response.content:
        return 0
    last = response.links.get("last")
    if last:
        pages = parse_qs(urlparse(last["url"]).query).get("page")
        if pages and pages[0].isdigit():
            return int(pages[0])
    data = response.json()
    return len(data) if isinstance(data, list) else 0


async def github_search_total(endpoint: str, query: str) -> int:
    """``total_count`` of a search query (e.g. open issues), exact regardless of pagination."""
    data = await github_get(f"/search/{endpoint}", {"q": query, "per_page": 1})
    return int(data.get("total_count", 0)) if isinstance(data, dict) else 0


def _rate_limit_message(response: httpx.Response) -> str | None:
    limited = (
        response.status_code == 429
        or response.headers.get("x-ratelimit-remaining") == "0"
        or "retry-after" in response.headers
    )
    if not limited:
        return None
    reset = response.headers.get("x-ratelimit-reset", "")
    if reset.isdigit():
        when = datetime.fromtimestamp(int(reset), tz=timezone.utc).strftime("%H:%M UTC")
        return f"Error: límite de rate de GitHub excedido. Se restablece a las {when}."
    return "Error: límite de rate de GitHub excedido. Intentá de nuevo en unos minutos."


def format_github_error(e: BaseException) -> str:
    """Convert exceptions into clear, actionable messages. Never includes the token."""
    logger.warning("GitHub request failed: %s: %s", type(e).__name__, e)

    if isinstance(e, ConfigError):
        return f"Error de configuración: {e}"

    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        rate = _rate_limit_message(e.response)
        if rate:
            return rate
        if status == 401:
            return "Error: token inválido o expirado."
        if status == 403:
            return "Error: permiso denegado. Revisá los scopes del token."
        if status == 404:
            return "Error: repositorio o recurso no encontrado. Verificá el nombre exacto."
        if status == 422:
            return "Error: GitHub rechazó la consulta por no ser válida."
        if status >= 500:
            return f"Error: GitHub no está disponible temporalmente (estado {status})."
        return f"Error: la API de GitHub respondió con estado {status}."

    if isinstance(e, httpx.TimeoutException):
        return "Error: la solicitud tardó demasiado. Intentá de nuevo."
    if isinstance(e, httpx.RequestError):
        return "Error de red: no se pudo conectar con la API de GitHub."
    if isinstance(e, ValueError):
        return "Error: la API de GitHub devolvió una respuesta inesperada."
    return f"Error inesperado: {type(e).__name__}"
