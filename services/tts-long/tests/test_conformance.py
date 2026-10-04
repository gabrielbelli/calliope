"""The suite voice-common ships, run against the app this service builds.

Sharing identity.py stops each backend writing its own verifier. It does
nothing about the parts this service still writes itself — the queue, the /v1
route, the health body — and those are where the same class of defect
reappears. So the package ships the assertions as well, and they run here
against the real `app.main:app`, which makes a bad voice-common bump fail at
this build rather than in production.

The star import is how the package intends this to be used: it puts the test
functions in this repo's own tree, so this conftest, this rootdir and these
fixtures apply. `pytest --pyargs voice_common.conformance` would collect them
out of site-packages, where the fixture below is not visible.

No model is loaded. The suite never enters the app's lifespan, deliberately —
this one allocates 6.5 GB and downloads ~3 GB of weights on a cold start, and a
conformance suite that did that would be switched off within a week.
"""

from __future__ import annotations

import pytest
from voice_common.conformance import *  # noqa: F401,F403
from voice_common.conformance import Service, module_app


@pytest.fixture
def voice_service() -> Service:
    """What the shared suite needs to know about this service.

    `module_app` rebuilds `app.main` from scratch for each case, because the
    environment is read once at import: a removed variable the suite sets has
    to be there when the app is built, or the check that it is reported would
    pass against an app that never saw it.
    """
    return Service(audience="tts-long",
                   build=module_app("app.main"),
                   v1_path="/v1/audio/speech")
