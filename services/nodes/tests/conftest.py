"""Settings every test in this directory starts from.

NODES_WAKE_WORDS is emptied because the hub's lifespan loads its wake word
models in the background, and with the default ("hey_jarvis") that is a fetch
from GitHub at every TestClient start: measured, the whole suite went from
13.9 s to 21.9 s, and tests about adoption needed the network. Tests of
the listening path set it back and stand fakes in for the models
(test_pipeline.py); the tests of the models themselves fetch them once per
session (test_wakeword.py).

The other three are integrations that must not reach out of a test run from a
shell that happens to have them set.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def quiet_integrations(monkeypatch):
    monkeypatch.setenv("NODES_WAKE_WORDS", "")
    for name in ("NODES_MQTT_URL", "NODES_FIRMWARE_PUBKEY", "NODES_STT_URL"):
        monkeypatch.delenv(name, raising=False)
