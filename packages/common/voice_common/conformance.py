"""A pytest suite the package ships and every consumer's CI runs against its own app.

This is the highest-value thing in voice-common and the one a code library
cannot deliver on its own: the invariants that are not functions.

Sharing `identity.py` stops each backend writing its own verifier. It does
nothing about the parts each service still writes itself — its own routes, its
own error paths, its own health payload — and those are where the same class of
defect reappears. So the package ships the assertions too, and each service's
CI runs them against the app object it actually builds. A bad voice-common bump
then fails at the consumer's build rather than in production.

Every assertion here is a bug that was really found, or a contract the
security design (D52, D63, D65) makes:

  * /health answers 200 with no assertion, trailing slash included
  * any other request without a valid assertion is a 401 in the envelope
  * an assertion for another service, an expired one, or one signed by any key
    but the gateway's is refused
  * /docs, /redoc and /openapi.json do not exist, whoever asks
  * no handler ever sees an X-Calliope-* header
  * a removed variable of the old key scheme is reported, never fatal
  * a bad /v1 body is 400 with a readable error.message (stt-stack answered 422)
  * /health is a coroutine function, not a thread-pool route
  * every /v1 error carries all four fields the schema requires

The two /v1 checks are skipped for a service with no /v1 surface (the hub,
voice-ui): it passes `v1_path=None` and names any route of its own as
`probe_path`, and every identity check runs against that instead.

Use it from a consumer by creating one test module:

    # tests/test_conformance.py
    import pytest
    from voice_common.conformance import *          # noqa: F401,F403
    from voice_common.conformance import Service, module_app

    @pytest.fixture
    def voice_service():
        return Service(audience="tts",
                       build=module_app("app.main"),
                       v1_path="/v1/audio/speech")

The star import is deliberate: it puts the test functions and the
`calliope_gateway` fixture in a module inside the consumer's own tree, so its
conftest, its fixtures and its rootdir all apply normally. `pytest --pyargs`
would collect them out of site-packages, where the consumer's conftest is not
visible.

`FakeGateway` is also what a consumer's other tests use to sign the assertions
its requests need: the format then lives in identity.py alone, not in a
second copy in every test suite.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import secrets
import string
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from .auth import REMOVED_VARIABLES
from .identity import (ASSERTION_HEADER, AUDIENCES, DELEGATION_HEADER,
                       PUBLIC_KEYS_FILE, RUN_DIR_ENV, SERVICE_KEY_FILE, Signer,
                       claims_of, write_public_keys)
from .scopes import SERVICE_PRINCIPALS, session_scopes

__all__ = [
    "Service", "module_app", "voice_service", "FakeGateway", "calliope_gateway",
    "assert_four_field_envelope",
    "test_health_answers_without_an_assertion",
    "test_a_request_without_an_assertion_is_refused",
    "test_an_assertion_for_another_service_is_refused",
    "test_an_expired_assertion_is_refused",
    "test_an_assertion_signed_by_another_key_is_refused",
    "test_docs_and_openapi_do_not_exist",
    "test_no_handler_sees_an_assertion_header",
    "test_a_removed_variable_is_reported_and_never_fatal",
    "test_a_bad_v1_body_is_400_with_a_readable_message",
    "test_health_is_a_coroutine_function",
    "test_every_v1_error_carries_all_four_fields",
]

# `type`, `message`, `param` and `code`. `param` and `code` are
# required-but-NULLABLE — present as JSON null, never absent.
ENVELOPE_FIELDS = {"message", "type", "param", "code"}

DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")
PROBE_PATH = "/__conformance__/headers"


@dataclass(frozen=True)
class Service:
    """What the suite needs to know about the app under test.

    audience     the service's audience, as identity.install was given it
    build        returns a fresh app. Called AFTER the environment is set, so
                 it must re-read it — see module_app.
    v1_path      any POST route on the OpenAI compatibility surface, used to
                 check the error envelope; None for a service without one,
                 which skips the two /v1 checks
    probe_path   any route that needs an assertion, used by the identity
                 checks; defaults to v1_path
    health_path  the unauthenticated probe path
    """

    audience: str
    build: Callable[[], FastAPI]
    v1_path: str | None = "/v1/audio/speech"
    probe_path: str | None = None
    health_path: str = "/health"

    @property
    def guarded_path(self) -> str:
        path = self.probe_path or self.v1_path
        if path is None:
            pytest.fail("Service needs a probe_path when it has no v1_path: "
                        "the identity checks need a route to be refused on.")
        return path


def module_app(module: str, attr: str = "app") -> Callable[[], FastAPI]:
    """A build callable that re-imports `module` from scratch each time.

    The services build their app at import, and read their environment then.
    A plain `import app.main` returns the app built under whatever environment
    the first import saw, and an assertion about a removed variable being set
    would be tested against an app that never saw it. So the module and its
    package are dropped from sys.modules first.
    """
    root = module.split(".")[0]

    def build() -> FastAPI:
        for name in [n for n in sys.modules
                     if n == root or n.startswith(f"{root}.")]:
            del sys.modules[name]
        return getattr(importlib.import_module(module), attr)

    return build


class FakeGateway:
    """The gateway's half of the identity contract, for a backend's tests.

    Writes identity.pub and service.key into `directory`, as the real gateway
    writes them into each service's credential volume (D7), and signs
    assertions with the matching private key. Point CALLIOPE_RUN_DIR at the
    directory (the `calliope_gateway` fixture does) and an app built at import
    verifies them like the real thing.
    """

    USER = "u_aaaaaaaaaaaaaaaa"

    def __init__(self, directory: Path, *, kid: str = "1") -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.signer = Signer.generate(kid)
        write_public_keys(directory / PUBLIC_KEYS_FILE, {kid: self.signer.public_key})
        alphabet = string.ascii_letters + string.digits
        self.service_key = "calliope_svc_" + "".join(
            secrets.choice(alphabet) for _ in range(36))
        (directory / SERVICE_KEY_FILE).write_text(self.service_key + "\n",
                                                  encoding="ascii")

    def assertion(self, audience: str, *, sub: str | None = None,
                  kind: str = "user", scopes: Iterable[str] | None = None,
                  cred: str | None = None, now: float | None = None,
                  lifetime: int = 60) -> str:
        """An assertion for `audience`. A user defaults to every scope a person can hold;
        a service (`sub="svc:<name>"`) to its principal's scopes."""
        if kind == "service":
            sub = sub or "svc:satellites"
            name = sub.removeprefix("svc:")
            scopes = SERVICE_PRINCIPALS.get(name, frozenset()) if scopes is None else scopes
            cred = sub
        else:
            sub = sub or self.USER
            scopes = session_scopes("admin") if scopes is None else scopes
            cred = cred or "session"
        return self.signer.assertion(audience=audience, sub=sub, kind=kind,
                                     scopes=scopes, cred=cred, now=now,
                                     lifetime=lifetime)

    def headers(self, audience: str, **kwargs: object) -> dict[str, str]:
        """The one header a forwarded request carries."""
        return {ASSERTION_HEADER: self.assertion(audience, **kwargs)}  # type: ignore[arg-type]

    def delegation(self, *, sub: str | None = None,
                   cred: str = "session:conformance-session-ref") -> str:
        return self.signer.delegation(sub=sub or self.USER, cred=cred)


@pytest.fixture
def calliope_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    """A FakeGateway whose credential directory this process reads."""
    gateway = FakeGateway(tmp_path / "calliope-run")
    monkeypatch.setenv(RUN_DIR_ENV, str(gateway.directory))
    return gateway


def assert_four_field_envelope(response) -> dict:  # noqa: ANN001 - httpx or requests
    """Assert one response is an OpenAI error envelope, and return its error.

    A function rather than only a test, because the gateway, voice-ui and the
    hub cannot run the suite around it and call this directly from their own
    tests. A shared assertion beats another copy of `set(body) == {...}`.
    """
    body = response.json()
    assert isinstance(body, dict) and isinstance(body.get("error"), dict), (
        f"{response.status_code} is not an error envelope: {response.text[:200]}")
    error = body["error"]
    assert set(error) == ENVELOPE_FIELDS, (
        f"{response.status_code} envelope has {sorted(error)}, "
        f"needs {sorted(ENVELOPE_FIELDS)}: {response.text[:200]}")
    assert isinstance(error["message"], str) and error["message"].strip(), (
        f"openai-python reads error.message and shows it: {response.text[:200]}")
    return error


@pytest.fixture
def voice_service() -> Service:
    """Overridden by the consumer. Defined here only to fail readably."""
    pytest.fail(
        "voice_common.conformance needs a `voice_service` fixture returning a "
        "Service(audience=..., build=..., v1_path=...). See the module "
        "docstring for the four-line version.")


def _client(service: Service) -> TestClient:
    """A client on a freshly built app.

    Deliberately NOT used as a context manager, so the app's lifespan never
    runs. Nothing asserted below needs a loaded model, and tts-stack's startup
    downloads 340 MB while tts-long's allocates 6.5 GB — a conformance suite
    that pulled either into every consumer's CI would be abandoned within a
    week, which is a worse outcome than any of the defects it guards.

    raise_server_exceptions=False so a 500 is asserted on rather than raised
    through the test, which would report the service's traceback instead of
    the contract that failed.
    """
    return TestClient(service.build(), raise_server_exceptions=False)


def _another_audience(audience: str) -> str:
    return sorted(AUDIENCES - {audience})[0]


def _v1_path(service: Service) -> str:
    if service.v1_path is None:
        pytest.skip("this service has no /v1 surface")
    return service.v1_path


def test_health_answers_without_an_assertion(
        voice_service: Service, calliope_gateway: FakeGateway) -> None:
    """The container healthcheck has no assertion and no way to get one.

    And the trailing slash must not matter: the check runs before routing, so
    FastAPI's 307 to /health never happens, and a probe written `/health/`
    once went permanently unhealthy the day authentication was switched on.
    """
    client = _client(voice_service)
    plain = client.get(voice_service.health_path)
    slashed = client.get(voice_service.health_path + "/")
    assert plain.status_code == 200, plain.text
    assert slashed.status_code == 200, slashed.text


def test_a_request_without_an_assertion_is_refused(
        voice_service: Service, calliope_gateway: FakeGateway) -> None:
    """Network isolation stops being the only boundary (D52).

    401, in OpenAI's envelope, with the challenge RFC 9110 asks for.
    """
    response = _client(voice_service).post(voice_service.guarded_path, json={})
    assert response.status_code == 401, response.text
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    assert assert_four_field_envelope(response)["code"] == "unauthenticated"


def test_an_assertion_for_another_service_is_refused(
        voice_service: Service, calliope_gateway: FakeGateway) -> None:
    """An assertion the gateway signed for the hub is not one for this service."""
    headers = calliope_gateway.headers(_another_audience(voice_service.audience))
    response = _client(voice_service).post(voice_service.guarded_path, json={},
                                           headers=headers)
    assert response.status_code == 401, response.text


def test_an_expired_assertion_is_refused(
        voice_service: Service, calliope_gateway: FakeGateway) -> None:
    """An assertion lives 60 seconds (D4); a captured one is not a credential."""
    headers = calliope_gateway.headers(voice_service.audience,
                                       now=time.time() - 300)
    response = _client(voice_service).post(voice_service.guarded_path, json={},
                                           headers=headers)
    assert response.status_code == 401, response.text


def test_an_assertion_signed_by_another_key_is_refused(
        voice_service: Service, calliope_gateway: FakeGateway,
        tmp_path: Path) -> None:
    """Same kid, different key: what anyone without the gateway's key can make."""
    forger = FakeGateway(tmp_path / "forger")
    response = _client(voice_service).post(
        voice_service.guarded_path, json={},
        headers=forger.headers(voice_service.audience))
    assert response.status_code == 401, response.text


def test_docs_and_openapi_do_not_exist(
        voice_service: Service, calliope_gateway: FakeGateway) -> None:
    """A schema dump is a free map of the service, so there is none (D52).

    Asked WITH a valid assertion: refusing an anonymous caller would only
    prove the middleware runs, not that the routes are gone.
    """
    client = _client(voice_service)
    headers = calliope_gateway.headers(voice_service.audience)
    for path in DOCS_PATHS:
        assert client.get(path, headers=headers).status_code == 404, path


def test_no_handler_sees_an_assertion_header(
        voice_service: Service, calliope_gateway: FakeGateway) -> None:
    """A handler that proxies onward has no assertion to forward (D65).

    A probe route is added to the built app, FIRST, so no catch-all of the
    service's own can answer in its place.
    """
    app = voice_service.build()

    async def probe(request: Request) -> dict[str, object]:
        return {"headers": sorted(request.headers.keys()),
                "sub": claims_of(request).sub}

    app.add_api_route(PROBE_PATH, probe, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())
    headers = {**calliope_gateway.headers(voice_service.audience),
               DELEGATION_HEADER: calliope_gateway.delegation(),
               "X-Calliope-Anything": "1"}
    response = TestClient(app, raise_server_exceptions=False).get(
        PROBE_PATH, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sub"] == FakeGateway.USER
    assert not [h for h in body["headers"] if h.startswith("x-calliope-")]


def test_a_removed_variable_is_reported_and_never_fatal(
        voice_service: Service, calliope_gateway: FakeGateway,
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A line nobody deleted from an app config must not take the service down (D63).

    The old rule refused to start on a degenerate key list. Applied to a
    variable this release removed, it would have stopped every satellite. So
    the service starts, serves, names the variable at ERROR and in /health,
    and never repeats its value.
    """
    marker = "removed-value-must-not-appear"
    for name in REMOVED_VARIABLES:
        monkeypatch.setenv(name, marker)
    with caplog.at_level(logging.ERROR, logger="voice_common.auth"):
        client = _client(voice_service)
    logged = "\n".join(caplog.messages)
    assert all(name in logged for name in REMOVED_VARIABLES), logged
    assert marker not in logged
    body = client.get(voice_service.health_path).json()
    assert body.get("ignored_variables") == sorted(REMOVED_VARIABLES), body
    response = client.post(voice_service.guarded_path, json={},
                           headers=calliope_gateway.headers(voice_service.audience))
    assert response.status_code != 401, response.text


def test_a_bad_v1_body_is_400_with_a_readable_message(
        voice_service: Service, calliope_gateway: FakeGateway) -> None:
    """stt-stack answered 422 where the other two, and OpenAI, answer 400.

    And the body must carry error.message: openai-python reads that field and
    reports a useless "unknown error" off anything else, so a client is told
    nothing about the field it left out.
    """
    response = _client(voice_service).post(
        _v1_path(voice_service), json={},
        headers=calliope_gateway.headers(voice_service.audience))
    assert response.status_code == 400, response.text
    message = response.json().get("error", {}).get("message")
    assert isinstance(message, str) and message.strip(), response.text


def test_health_is_a_coroutine_function(voice_service: Service,
                                        calliope_gateway: FakeGateway) -> None:
    """A sync /health shares AnyIO's 40-thread pool with the blocking routes.

    Forty concurrent synthesis requests took the pool, /health stopped
    answering, and the orchestrator restarted a service that was merely busy.
    """
    app = voice_service.build()
    matches = [route for route in app.routes
               if getattr(route, "path", None) == voice_service.health_path]
    assert matches, f"no route at {voice_service.health_path}"
    for route in matches:
        assert inspect.iscoroutinefunction(route.endpoint), (
            f"{voice_service.health_path} is a sync route and will queue for "
            f"an AnyIO worker thread")


def test_every_v1_error_carries_all_four_fields(
        voice_service: Service, calliope_gateway: FakeGateway) -> None:
    """The assertion that would have caught the gateway's silent omission.

    `param` and `code` are required-but-NULLABLE in OpenAI's `Error`: present
    as JSON null, never absent. voice-gateway emitted a three-key envelope for
    its whole life, to clients using the openai-python SDK, and nobody looked
    because there was no assertion that looked.

    So this drives every error path a service has in common with the others,
    from OUTSIDE the app. A fifth service that forgets `param`, or registers
    no handler for an unrouted /v1 path, fails here on its first build.
    """
    v1_path = _v1_path(voice_service)
    client = _client(voice_service)
    # No assertion: the 401 the identity middleware builds from outside every
    # exception handler, which is the one body a handler cannot fix.
    assert_four_field_envelope(client.post(v1_path, json={}))

    headers = calliope_gateway.headers(voice_service.audience)
    # A body the service rejects.
    assert_four_field_envelope(client.post(v1_path, json={}, headers=headers))
    # An unrouted /v1 path, and a wrong method on a routed one. Both used to
    # leak FastAPI's `{"detail": ...}`, which openai-python reads no message
    # off and reports as a bare "unknown error".
    assert_four_field_envelope(client.post("/v1/definitely-not-a-route",
                                           json={}, headers=headers))
    assert_four_field_envelope(client.get(v1_path, headers=headers))
