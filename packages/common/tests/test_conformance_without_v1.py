"""The shipped suite, as the hub and voice-ui run it: no /v1 surface, a probe route instead.

The same sample app, told it has no /v1 route. The two /v1 checks skip, and
every identity check runs against /speak, so a service without an OpenAI
surface still gets the missing, wrong-audience, expired and forged assertion
checks rather than none.
"""

from __future__ import annotations

import pytest

from voice_common.conformance import *  # noqa: F401,F403
from voice_common.conformance import Service, module_app


@pytest.fixture
def voice_service() -> Service:
    return Service(audience="tts",
                   build=module_app("sample_service.main"),
                   v1_path=None,
                   probe_path="/speak")
