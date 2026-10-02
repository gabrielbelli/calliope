"""voice-common's conformance suite, run against the app this repo builds.

The star import is how the package intends this to be used: it puts the test
functions in a module inside this tree, so this repo's rootdir and conftest
apply. `pytest --pyargs voice_common.conformance` would collect them out of
site-packages instead, where none of that is visible.

What it asserts is the wire contract the backends share: every request but
`/health` needs the gateway's assertion addressed to `stt`, so an assertion for
another service, an expired one or a forged one is a 401; `/health/` with a
trailing slash is not a 401; `/docs` and `/openapi.json` do not exist; no
handler sees an `X-Calliope-*` header; a removed key variable is reported and
never fatal; a bad /v1 body is 400 with a readable `error.message`; and
`/health` is a coroutine function rather than a route queueing for an AnyIO
worker thread.

The suite deliberately never runs the app's lifespan, so nothing here downloads
Parakeet. It exercises routing, the identity check and the error envelope,
which is all of what it claims to cover.
"""

from __future__ import annotations

import pytest
from voice_common.conformance import *  # noqa: F401,F403
from voice_common.conformance import Service, module_app


@pytest.fixture
def voice_service() -> Service:
    return Service(
        audience="stt",
        build=module_app("app.main"),
        # The only POST on this service's compatibility surface. It takes
        # multipart rather than JSON, which does not matter to the suite: an
        # empty JSON body is rejected by the same validation handler.
        v1_path="/v1/audio/transcriptions",
    )
