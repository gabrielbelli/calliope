"""The removed key variables: reported by name, ignored, and never fatal (D63).

The old suite here pinned the key middleware: a set-but-keyless variable
refused to start, and an unset one disabled authentication. Both rules are
gone with the middleware. An unset variable no longer means open, because
nothing a backend serves is open any more; and a set one no longer stops
anything, because stopping the hub over a line left in an app config would
take every satellite down with it.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from voice_common import auth, identity
from voice_common.conformance import FakeGateway
from voice_common.health import install_health


@pytest.fixture
def clean(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in auth.REMOVED_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_the_removed_list_is_exactly_the_old_schemes_variables() -> None:
    """§1.9's table. A name missing here would be silently obeyed by nobody and reported by nobody."""
    assert set(auth.REMOVED_VARIABLES) == {
        "GATEWAY_API_KEYS", "UI_GATEWAY_API_KEY", "STT_API_KEYS",
        "TTS_API_KEYS", "SATELLITES_API_KEYS", "RUNLOG_KEY"}


def test_nothing_is_ignored_when_nothing_is_set(clean: pytest.MonkeyPatch) -> None:
    assert auth.ignored_variables() == []


def test_a_removed_variable_is_reported_when_set_even_to_nothing(
        clean: pytest.MonkeyPatch) -> None:
    """`-e STT_API_KEYS=$SECRET` with SECRET unset is still a line to delete."""
    clean.setenv("STT_API_KEYS", "")
    clean.setenv("RUNLOG_KEY", "k")
    assert auth.ignored_variables() == ["RUNLOG_KEY", "STT_API_KEYS"]


def test_a_removed_variable_is_logged_at_error_by_name_and_never_by_value(
        clean: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    clean.setenv("TTS_API_KEYS", "the-old-secret-key")
    with caplog.at_level(logging.ERROR, logger="voice_common.auth"):
        assert auth.watch_removed_variables() == ["TTS_API_KEYS"]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "TTS_API_KEYS" in errors[0].getMessage()
    assert "the-old-secret-key" not in caplog.text


def test_the_reminder_repeats_every_interval_for_the_life_of_the_process(
        clean: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Once a minute in production: an ERROR at start scrolls away, a repeating one does not."""
    clean.setenv("SATELLITES_API_KEYS", "k")
    naps: list[float] = []

    class Enough(Exception):
        pass

    def nap(seconds: float) -> None:
        if len(naps) == 3:
            raise Enough
        naps.append(seconds)

    # auth's own reference only: every other sleeper in the process keeps time.sleep.
    clean.setattr(auth, "time", SimpleNamespace(sleep=nap))
    with caplog.at_level(logging.ERROR, logger="voice_common.auth"), \
            pytest.raises(Enough):
        auth._repeat(60.0)
    assert naps == [60.0, 60.0, 60.0]
    assert sum("SATELLITES_API_KEYS" in m for m in caplog.messages) == 3


def test_only_one_reminder_thread_runs_however_many_apps_install(
        clean: pytest.MonkeyPatch) -> None:
    clean.setenv("STT_API_KEYS", "k")
    for _ in range(3):
        auth.watch_removed_variables()
    assert len([t for t in threading.enumerate()
                if t.name == "removed-variables"]) == 1


def test_a_backend_with_a_removed_variable_keeps_serving_and_says_so(
        clean: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """H5: the variable that used to refuse to start now costs an ERROR line, not an outage."""
    gateway = FakeGateway(tmp_path)
    clean.setenv(identity.RUN_DIR_ENV, str(tmp_path))
    clean.setenv("STT_API_KEYS", ",")  # the value that used to stop the process
    app = FastAPI()
    install_health(app)

    @app.get("/v1/models")
    async def models() -> dict[str, list]:
        return {"data": []}

    identity.install(app, "stt")
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/v1/models", headers=gateway.headers("stt")).status_code == 200
    assert client.get("/health").json()["ignored_variables"] == ["STT_API_KEYS"]


def test_with_no_variable_set_every_request_still_needs_an_assertion(
        clean: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Replaces "an unset variable disables auth": unset no longer means open."""
    FakeGateway(tmp_path)
    clean.setenv(identity.RUN_DIR_ENV, str(tmp_path))
    app = FastAPI()

    @app.get("/v1/models")
    async def models() -> dict[str, list]:
        return {"data": []}

    identity.install(app, "stt")
    assert TestClient(app).get("/v1/models").status_code == 401
