"""The health contract: an async route, one path, and what an operator must fix."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from voice_common import auth, identity
from voice_common.conformance import FakeGateway
from voice_common.health import PATH, install_health


@pytest.fixture(autouse=True)
def no_removed_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in auth.REMOVED_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_the_route_is_a_coroutine_function_not_a_thread_pool_route() -> None:
    """A sync /health shares AnyIO's 40-thread pool with the blocking routes.

    tts-long paid for this: 40 concurrent synthesis requests each held a
    thread for up to TTS_OPENAI_SYNC_TIMEOUT seconds, /health stopped
    answering, and the orchestrator restarted a service that was merely busy.
    """
    app = FastAPI()
    install_health(app)
    route, = [r for r in app.routes if getattr(r, "path", None) == PATH]
    assert inspect.iscoroutinefunction(route.endpoint)


def test_the_health_route_is_the_one_path_identity_leaves_open(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route and exemption read one constant, so a rename cannot lock the healthcheck out."""
    FakeGateway(tmp_path)
    monkeypatch.setenv(identity.RUN_DIR_ENV, str(tmp_path))
    app = FastAPI()
    install_health(app)
    identity.install(app, "tts")
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get(PATH).status_code == 200
    assert client.get(PATH + "/").status_code != 401


def test_the_status_envelope_is_fixed_and_the_details_are_the_callers() -> None:
    app = FastAPI()
    install_health(app, details=lambda: {"threads": 4, "queued": 0})
    body = TestClient(app).get("/health").json()
    assert body == {"status": "ok", "threads": 4, "queued": 0}


def test_a_service_still_loading_can_say_so() -> None:
    """tts-stack and stt-stack both report "loading" before the model is up."""
    app = FastAPI()
    install_health(app, details=lambda: {"status": "loading"})
    assert TestClient(app).get("/health").json() == {"status": "loading"}


def test_an_async_details_callable_is_awaited() -> None:
    async def details() -> dict[str, int]:
        return {"threads": 8}

    app = FastAPI()
    install_health(app, details=details)
    assert TestClient(app).get("/health").json() == {"status": "ok", "threads": 8}


def test_no_details_callable_still_answers() -> None:
    app = FastAPI()
    install_health(app)
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_a_service_without_its_credential_files_is_not_ready(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§2.4: it would refuse every request, so it does not call itself ok."""
    monkeypatch.setenv(identity.RUN_DIR_ENV, str(tmp_path))
    clock = [0.0]
    app = FastAPI()
    install_health(app)
    identity.install(app, "stt", credentials=identity.Credentials(clock=lambda: clock[0]))
    client = TestClient(app)
    assert client.get("/health").json()["status"] == "not_ready"
    FakeGateway(tmp_path)
    clock[0] += identity.POLL_SECONDS
    assert client.get("/health").json()["status"] == "ok"


def test_a_removed_variable_is_named_in_the_health_body(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Where the gateway's full health tier and the Admin page pick it up (D63)."""
    monkeypatch.setenv("TTS_API_KEYS", "the-old-key")
    app = FastAPI()
    install_health(app)
    body = TestClient(app).get("/health").json()
    assert body == {"status": "ok", "ignored_variables": ["TTS_API_KEYS"]}


def test_a_services_own_ignored_variables_are_merged_with_the_removed_ones(monkeypatch) -> None:
    """A service may name variables it ignores (the hub's copies of credentials
    the secret store now holds); the removed settings are added, not swapped in."""
    removed = sorted(auth.REMOVED_VARIABLES)[0]
    monkeypatch.setenv(removed, "x")
    app = FastAPI()
    install_health(app, details=lambda: {"ignored_variables": ["SATELLITES_HA_TOKEN"]})
    body = TestClient(app).get(PATH).json()
    assert body["ignored_variables"] == sorted({removed, "SATELLITES_HA_TOKEN"})


def test_an_empty_list_from_a_service_leaves_no_field(monkeypatch) -> None:
    for name in auth.REMOVED_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    app = FastAPI()
    install_health(app, details=lambda: {"ignored_variables": []})
    assert "ignored_variables" not in TestClient(app).get(PATH).json()
