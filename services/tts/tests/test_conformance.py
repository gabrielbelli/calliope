"""Run voice-common's shipped conformance suite against this service's own app.

Sharing the identity check stops each backend from writing its own verifier.
It does nothing about the parts this repo still writes itself — its routes, its
error paths, its health payload — and that is where the same class of defect
reappears. So the package ships the assertions too and every consumer runs them
against the app object it actually builds: a bad voice-common bump fails here,
at this repo's build, rather than on a deployed service.

What it asserts: every request but `/health` needs the gateway's assertion
addressed to `tts`, so one for another service, an expired one or a forged one
is a 401; `GET /health/` is not a 401, which it once was here the moment keys
were configured; `/docs` and `/openapi.json` do not exist; no handler sees an
`X-Calliope-*` header; a leftover `TTS_API_KEYS` is reported and never fatal;
and every /v1 error carries all four envelope fields.

The star import is deliberate. It puts those test functions in a module inside
this repo's own tree, so this rootdir and any conftest here apply normally;
`pytest --pyargs voice_common.conformance` would collect them out of
site-packages instead.

Nothing here loads Kokoro. The suite never enters the app's lifespan, on
purpose — a conformance run that pulled 340 MB of weights into CI would be
switched off within a week, which is worse than any defect it guards.
"""

from __future__ import annotations

import pytest
from voice_common.conformance import *  # noqa: F401,F403
from voice_common.conformance import Service, module_app


@pytest.fixture
def voice_service() -> Service:
    return Service(audience="tts",
                   build=module_app("app.main"),
                   v1_path="/v1/audio/speech")
