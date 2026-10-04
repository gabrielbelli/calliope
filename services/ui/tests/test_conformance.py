"""voice_common's shared contract, run against the app this service actually builds.

voice-ui has no /v1 surface, so the two /v1 checks skip; every identity check
runs against /ui/abandon, a route that needs an assertion and answers an empty
body with a 422 of its own rather than doing anything.
"""

from __future__ import annotations

import pytest
from voice_common.conformance import *  # noqa: F401,F403
from voice_common.conformance import Service, module_app


@pytest.fixture
def voice_service() -> Service:
    return Service(audience="ui", build=module_app("app.main"), v1_path=None,
                   probe_path="/ui/abandon")
