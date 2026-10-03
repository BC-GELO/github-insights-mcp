import pytest
import respx

import github_client

GITHUB = "https://api.github.com"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Deterministic config, independent of any real .env on the developer's machine."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_USERNAME", "octo")
    monkeypatch.delenv("GITHUB_API_BASE", raising=False)
    monkeypatch.setattr(github_client, "_warned_no_token", False)


@pytest.fixture
def gh():
    with respx.mock(base_url=GITHUB, assert_all_called=False) as router:
        yield router
