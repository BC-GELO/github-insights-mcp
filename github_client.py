import os
import httpx
from dotenv import load_dotenv

load_dotenv()

GITHUB_API_BASE = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def github_get(path: str, params: dict | None = None) -> dict | list:
    """Hace un GET autenticado contra la API de GitHub y devuelve el JSON."""
    async with httpx.AsyncClient(base_url=GITHUB_API_BASE, headers=HEADERS, timeout=15.0) as client:
        response = await client.get(path, params=params or {})
        response.raise_for_status()
        return response.json()


def format_github_error(e: Exception) -> str:
    """Convierte errores de httpx en mensajes claros y accionables."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 404:
            return "Error: repositorio o recurso no encontrado. Verificá el nombre exacto."
        if status == 403:
            return "Error: permiso denegado o límite de rate excedido. Revisá los scopes del token."
        if status == 401:
            return "Error: token inválido o expirado."
        return f"Error: la API de GitHub respondió con estado {status}."
    if isinstance(e, httpx.TimeoutException):
        return "Error: la solicitud tardó demasiado. Intentá de nuevo."
    return f"Error inesperado: {type(e).__name__}"