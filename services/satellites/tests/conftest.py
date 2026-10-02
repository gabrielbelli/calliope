"""Settings every test in this directory starts from.

SATELLITES_WAKE_WORDS is emptied because the hub's lifespan loads its wake word
models in the background, and with the default ("hey_jarvis") that is a fetch
from GitHub at every TestClient start: measured, the whole suite went from
13.9 s to 21.9 s, and tests about adoption needed the network. Tests of
the listening path set it back and stand fakes in for the models
(test_pipeline.py); the tests of the models themselves fetch them once per
session (test_wakeword.py).

The other three are integrations that must not reach out of a test run from a
shell that happens to have them set. SATELLITES_LANGUAGES goes too, so the
language tests start from the default household, English only. So do the
credentials an older hub read from the environment: the import would carry
them into the store (secret_import.py).

EVERY REQUEST IS SIGNED AS THE GATEWAY WOULD SIGN IT. The hub answers only
requests that carry the gateway's assertion (identity.install). `gateway`
writes the files a FakeGateway mints into a temporary CALLIOPE_RUN_DIR, and
every TestClient signs each request it sends: an admin's session (every scope
a person can hold) over HTTP, and the relay's assertion on the device socket,
with the gateway's X-Forwarded-For only where a test gives one. A request that
already carries X-Calliope-Identity keeps it: a test plays a narrower caller
with `gateway.headers(...)`, and sends none at all with an empty value.

THE SECRET STORE IS A FAKE GATEWAY ON ITS OWN ADDRESS. `store` answers
/internal/secrets/{name} and /internal/secrets/import as the gateway's internal
listener does, only to the service key the FakeGateway wrote, and the hub's
secret_client is pointed at it. Nothing a test does reaches the network, and a
secret one test stores is never seen by the next. Its import takes the body the
gateway's ImportBatch takes, no more (422 otherwise), and keeps a row's hosts
within the ones `declared` names for it, as the gateway does.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from starlette.testclient import TestClient
from voice_common.conformance import FakeGateway
from voice_common.identity import ASSERTION_HEADER, RUN_DIR_ENV

from app import secret_client

RELAY = "svc:gateway-relay"
SECRET_PATH = re.compile(r"^/internal/secrets/([A-Z][A-Z0-9_]{0,63})$")


@pytest.fixture(autouse=True)
def quiet_integrations(monkeypatch):
    monkeypatch.setenv("SATELLITES_WAKE_WORDS", "")
    for name in ("SATELLITES_MQTT_URL", "SATELLITES_FIRMWARE_PUBKEY", "SATELLITES_STT_URL",
                 "SATELLITES_LANGUAGES", "SATELLITES_HA_TOKEN", "SATELLITES_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _sign(gw: FakeGateway, request: httpx.Request) -> None:
    if ASSERTION_HEADER in request.headers:
        return
    if request.url.scheme in ("ws", "wss"):
        token = gw.assertion("satellites", sub=RELAY, kind="service", scopes=())
    else:
        token = gw.assertion("satellites")
    request.headers[ASSERTION_HEADER] = token


@pytest.fixture(autouse=True)
def gateway(tmp_path_factory, monkeypatch) -> FakeGateway:
    gw = FakeGateway(tmp_path_factory.mktemp("calliope-run"))
    monkeypatch.setenv(RUN_DIR_ENV, str(gw.directory))
    unsigned = TestClient.__init__

    def signed(self, *args, **kwargs):
        unsigned(self, *args, **kwargs)
        self.event_hooks["request"].append(lambda request: _sign(gw, request))

    monkeypatch.setattr(TestClient, "__init__", signed)
    return gw


class FakeStore:
    """The gateway's /internal/secrets, in memory: what the hub may read, and
    what it imported. `down` makes it unreachable, `status` makes it answer
    that to everything (503: a keyring it cannot read), `import_status` to the
    import alone, and `kinds = False` leaves `kind` out of what it serves."""

    def __init__(self, key: str):
        self.key = key
        self.values: dict[str, dict] = {}
        self.imports: list[dict] = []
        self.asked: list[str] = []
        self.closed = False
        self.down = False
        self.status: int | None = None
        self.import_status: int | None = None
        self.kinds = True

    def put(self, name: str, value: str, hosts=(), *, kind: str = "bearer") -> None:
        version = self.values.get(name, {}).get("version", 0) + 1
        self.values[name] = {"value": value, "version": version, "allowed_hosts": list(hosts),
                             "kind": kind}

    def clear(self, name: str) -> None:
        self.values.pop(name, None)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("[Errno 111] Connection refused", request=request)
        if request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"error": {"code": "invalid_api_key"}})
        if self.status is not None:
            return httpx.Response(self.status, json={"error": {"code": "locked"}})
        if request.url.path == "/internal/secrets/import" and request.method == "POST":
            return self._import(json.loads(request.content))
        named = SECRET_PATH.match(request.url.path)
        if named is None or request.method != "GET":
            return httpx.Response(404)
        self.asked.append(named.group(1))
        held = self.values.get(named.group(1))
        if held is None:
            return httpx.Response(404, json={"error": {"code": "secret_not_found"}})
        served = held if self.kinds else {k: v for k, v in held.items() if k != "kind"}
        return httpx.Response(200, json=served | {"max_age": 60},
                              headers={"Cache-Control": "no-store"})

    def _import(self, body: dict) -> httpx.Response:
        if self.import_status is not None:
            return httpx.Response(self.import_status, json={"error": {"code": "refused"}})
        if self.closed:
            return httpx.Response(410, json={"error": {"code": "import_closed"}})
        if not _an_import_batch(body):
            return httpx.Response(422, json={"error": {"code": "invalid_request"}})
        self.imports.append(body)
        for item in body["secrets"]:
            if item["name"] not in self.values:
                hosts = set(item["allowed_hosts"]) & set(body["declared"].get(item["name"], ()))
                self.put(item["name"], item["value"], sorted(hosts), kind=item["kind"])
        self.closed = self.closed or body["final"]
        return httpx.Response(200, json={"results": [], "closed": body["final"]})


def _an_import_batch(body: object) -> bool:
    """The gateway's ImportBatch and ImportEntry (routes_secrets.py), which
    forbid any other field."""
    entry = {"name", "kind", "value", "allowed_hosts", "source"}
    return (isinstance(body, dict) and set(body) <= {"secrets", "declared", "final"}
            and isinstance(body.get("final"), bool)
            and isinstance(body.get("declared"), dict)
            and all(isinstance(v, list) for v in body["declared"].values())
            and isinstance(body.get("secrets"), list)
            and all(isinstance(e, dict) and set(e) <= entry for e in body["secrets"]))


@pytest.fixture(autouse=True)
def store(gateway) -> FakeStore:
    fake = FakeStore(gateway.service_key)
    secret_client.configure(secret_client.Secrets(transport=httpx.MockTransport(fake)))
    yield fake
    secret_client.configure(secret_client.Secrets())
