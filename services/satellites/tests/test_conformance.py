"""voice_common's shipped assertions, run against the hub's own app.

The hub has no /v1 surface, so the two OpenAI-envelope checks are skipped
and every identity check runs against GET /satellites instead.
"""

import sys

import pytest
from voice_common.conformance import *  # noqa: F401,F403
from voice_common.conformance import FakeGateway, Service, module_app


@pytest.fixture
def gateway(calliope_gateway) -> FakeGateway:
    """The suite's own gateway, and no signing of every request: the suite
    signs what it means to and sends nothing where it means to."""
    return calliope_gateway


@pytest.fixture(autouse=True)
def the_rest_of_the_suite_keeps_its_modules():
    """module_app imports app.main afresh, its package and every sibling with
    it. The modules the other test files imported are put back afterwards, or
    a later test would configure one copy of secret_client while the hub it
    builds reads another."""
    def ours() -> dict:
        return {n: m for n, m in sys.modules.items() if n == "app" or n.startswith("app.")}
    kept = ours()
    yield
    for name in ours():
        del sys.modules[name]
    sys.modules.update(kept)


@pytest.fixture
def voice_service():
    return Service(audience="satellites", build=module_app("app.main"), v1_path=None,
                   probe_path="/satellites")
