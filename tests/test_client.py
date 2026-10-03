import httpx
import pytest

import github_client as gc


def _status_error(status, headers=None):
    request = httpx.Request("GET", "https://api.github.com/x")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


# ------------------------------- headers / config ------------------------------- #
def test_authorization_header_sent_when_token_set():
    assert gc.build_headers()["Authorization"] == "Bearer test-token"


def test_no_authorization_header_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN")
    assert "Authorization" not in gc.build_headers()


def test_blank_token_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    assert "Authorization" not in gc.build_headers()


def test_user_agent_is_set():
    assert gc.build_headers()["User-Agent"] == "github-insights-mcp"


def test_api_base_override(monkeypatch):
    monkeypatch.setenv("GITHUB_API_BASE", "https://ghe.example.com/api/v3/")
    assert gc.get_api_base() == "https://ghe.example.com/api/v3"


@pytest.mark.parametrize("bad", ["", "a/b", "../x", "-start", "has space", "x" * 40])
def test_invalid_username_rejected(monkeypatch, bad):
    monkeypatch.setenv("GITHUB_USERNAME", bad)
    with pytest.raises(gc.ConfigError):
        gc.get_username()


# ----------------------------------- validation ---------------------------------- #
@pytest.mark.parametrize("name", ["Carstuff_App", "github-insights-mcp", "a.b-c_d", "x"])
def test_valid_repo_names(name):
    assert gc.is_valid_repo_name(name)


@pytest.mark.parametrize(
    "name", ["", ".", "..", "a/b", "../../orgs/x", "a b", "a?b", "a#b", "a%2fb", "x" * 101]
)
def test_invalid_repo_names(name):
    assert not gc.is_valid_repo_name(name)


@pytest.mark.parametrize("name", ["octo/app", "BC-GELO/Carstuff_App"])
def test_valid_full_names(name):
    assert gc.is_valid_full_name(name)


@pytest.mark.parametrize("name", ["", "octo", "octo/../x", "../app", "a/b/c", "a/ b", "./."])
def test_invalid_full_names(name):
    assert not gc.is_valid_full_name(name)


# ------------------------------------ counting ----------------------------------- #
async def test_count_uses_link_header_last_page(gh):
    link = '<https://api.github.com/r/contributors?per_page=1&page=2>; rel="next", ' \
           '<https://api.github.com/r/contributors?per_page=1&page=57>; rel="last"'
    route = gh.get("/r/contributors").mock(
        return_value=httpx.Response(200, json=[{"login": "a"}], headers={"Link": link})
    )
    assert await gc.github_count("/r/contributors") == 57
    assert route.calls[0].request.url.params["per_page"] == "1"


async def test_count_without_link_header_counts_items(gh):
    gh.get("/r/contributors").mock(return_value=httpx.Response(200, json=[{"a": 1}]))
    assert await gc.github_count("/r/contributors") == 1


async def test_count_handles_204_empty_repo(gh):
    gh.get("/r/contributors").mock(return_value=httpx.Response(204))
    assert await gc.github_count("/r/contributors") == 0


async def test_github_get_handles_204(gh):
    gh.get("/x").mock(return_value=httpx.Response(204))
    assert await gc.github_get("/x") == []


async def test_requests_carry_auth_and_api_version_headers(gh):
    route = gh.get("/x").mock(return_value=httpx.Response(200, json={}))
    await gc.github_get("/x")
    sent = route.calls[0].request.headers
    assert sent["authorization"] == "Bearer test-token"
    assert sent["x-github-api-version"] == "2022-11-28"


# ----------------------------------- error text ---------------------------------- #
@pytest.mark.parametrize(
    "status, headers, expected",
    [
        (401, {}, "token inválido o expirado"),
        (403, {}, "permiso denegado"),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"}, "UTC"),
        (403, {"retry-after": "30"}, "límite de rate"),
        (429, {}, "límite de rate"),
        (404, {}, "no encontrado"),
        (422, {}, "no ser válida"),
        (503, {}, "no está disponible"),
        (418, {}, "estado 418"),
    ],
)
def test_http_status_errors_are_readable(status, headers, expected):
    assert expected in gc.format_github_error(_status_error(status, headers))


def test_timeout_message():
    assert "tardó demasiado" in gc.format_github_error(httpx.ReadTimeout("slow"))


def test_network_error_message():
    assert "no se pudo conectar" in gc.format_github_error(httpx.ConnectError("down"))


def test_config_error_message():
    assert "configuración" in gc.format_github_error(gc.ConfigError("bad"))


def test_bad_json_message():
    assert "inesperada" in gc.format_github_error(ValueError("not json"))


def test_unknown_error_does_not_leak_details():
    msg = gc.format_github_error(RuntimeError("secret test-token inside"))
    assert msg == "Error inesperado: RuntimeError"
    assert "test-token" not in msg
