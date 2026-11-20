"""First-run setup endpoints: which keys are needed, and do they work."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from clipstudio import app as app_module
from clipstudio.api import settings as settings_api
from clipstudio.app import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client(clipstudio_home, fake_keyring, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv(app_module.ENV_NO_WORKER, "1")
    with TestClient(create_app()) as test_client:
        yield test_client


def _fake_http(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Route the validator's outgoing request through ``handler``."""
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        return real(*args, transport=httpx.MockTransport(wrapped), **kwargs)

    monkeypatch.setattr(settings_api.httpx, "AsyncClient", factory)
    return seen


class TestSetupStatus:
    def test_fresh_install_is_not_ready(self, client: TestClient) -> None:
        body = client.get("/api/setup/status").json()

        assert body["ready"] is False
        keys = {s["key"]: s for s in body["steps"]}
        assert keys["groq"]["required"] and not keys["groq"]["present"]
        assert keys["nvidia"]["required"] and not keys["nvidia"]["present"]

    def test_ready_once_both_keys_are_saved(self, client: TestClient) -> None:
        client.put("/api/settings/secrets", json={"key": "groq", "value": "gsk_" + "a" * 30})
        client.put("/api/settings/secrets", json={"key": "nvidia", "value": "nvapi-" + "b" * 30})

        body = client.get("/api/setup/status").json()

        assert body["ready"] is True
        assert client.get("/api/settings").json()["keys_present"]["nvidia"] is True

    def test_stored_keys_are_only_ever_masked(self, client: TestClient) -> None:
        secret = "gsk_" + "z" * 30
        client.put("/api/settings/secrets", json={"key": "groq", "value": secret})

        raw = client.get("/api/setup/status").text

        assert secret not in raw
        assert secret[-4:] in raw

    def test_environment_key_counts_as_present(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "c" * 30)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-" + "d" * 30)

        assert client.get("/api/setup/status").json()["ready"] is True

    def test_local_whisper_makes_groq_optional(self, client: TestClient) -> None:
        client.put("/api/settings", json={"whisper": {"backend": "local"}})
        client.put("/api/settings/secrets", json={"key": "nvidia", "value": "nvapi-" + "e" * 30})

        assert client.get("/api/setup/status").json()["ready"] is True


class TestValidateKey:
    def test_accepted_key(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _fake_http(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))

        body = client.post("/api/setup/validate", json={"key": "groq", "value": "gsk_x"}).json()

        assert body["ok"] is True
        assert seen[0].headers["authorization"] == "Bearer gsk_x"
        assert seen[0].url.host == "api.groq.com"

    def test_rejected_key_does_not_echo_the_key(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_http(monkeypatch, lambda r: httpx.Response(401, json={"error": "bad"}))

        response = client.post("/api/setup/validate", json={"key": "groq", "value": "gsk_secret"})

        assert response.json()["ok"] is False
        assert "gsk_secret" not in response.text

    def test_nvidia_uses_an_authenticated_call(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _fake_http(monkeypatch, lambda r: httpx.Response(200, json={}))

        client.post("/api/setup/validate", json={"key": "nvidia", "value": "nvapi-x"})

        assert seen[0].method == "POST"
        assert seen[0].url.path.endswith("/chat/completions")

    def test_network_failure_is_reported_without_the_url(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        _fake_http(monkeypatch, boom)

        body = client.post("/api/setup/validate", json={"key": "groq", "value": "gsk_x"}).json()

        assert body["ok"] is False
        assert "api.groq.com" not in body["message"]

    def test_missing_value_without_stored_key(self, client: TestClient) -> None:
        body = client.post("/api/setup/validate", json={"key": "groq"}).json()

        assert body["ok"] is False

    def test_unknown_key_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/setup/validate", json={"key": "aws", "value": "x"})

        assert response.status_code == 400


def test_legacy_env_var_prefix_still_supplies_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipstudio import config

    monkeypatch.setenv("AUTOCLIP_GROQ_KEY", "gsk_legacy")

    assert config.get_secret("groq") == "gsk_legacy"
