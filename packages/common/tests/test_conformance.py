"""Run the shipped conformance suite against a sample consumer.

This is the same few lines every service's CI writes, and it is here so that a
change to voice_common.conformance is caught by this package's own tests
rather than by three downstream builds.

The star import is deliberate and is what a consumer writes too: it puts the
test functions and the `calliope_gateway` fixture in a module inside this
tree, so the local conftest and fixtures apply. `pytest --pyargs
voice_common.conformance` would collect them out of site-packages, where a
consumer's conftest is not visible.
"""

from __future__ import annotations

import pytest

from voice_common.conformance import *  # noqa: F401,F403
from voice_common.conformance import Service, module_app


@pytest.fixture
def voice_service() -> Service:
    return Service(audience="tts",
                   build=module_app("sample_service.main"),
                   v1_path="/v1/audio/speech")
