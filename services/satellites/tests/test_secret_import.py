"""What the hub held itself, moved into the secret store once (secret_import.py).

Against conftest's FakeStore, which takes an import as the gateway does: the
body ImportBatch takes, hosts kept within `declared`, never overwriting a name
that is set, and closing the window on "final".
"""

from __future__ import annotations

import importlib
import json
import logging
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from voice_common.scopes import SECRET_NAME

from app import secret_client, secret_import
from app.destinations import Target
from app.secret_import import MARKER, Import, button_secret, gather, shred
from app.store import Store

NID = "020000000001"
HA_URL = "https://ha.lan:8123"
LLM_URL = "https://openrouter.test/api/v1"
TOKEN = "ha-token-do-not-leak"
LLM_KEY = "sk-test-import-0123456789"
HOOK = "https://ha.lan:8123/api/webhook/s3cret-hook-id"
BUTTON = button_secret(NID, "mode", "press")
MQTT = "mqtt://hub:broker-pass-do-not-leak@broker.test:1883"


def targets() -> list[Target]:
    return [Target("SATELLITES_HA_TOKEN", HA_URL), Target("OPENROUTER_API_KEY", LLM_URL)]


def satellites() -> dict[str, dict]:
    return {NID: {"buttons": {"rec": {"press": "mute"}, "mode": {"press": f"webhook:{HOOK}"}}}}


def legacy(tmp_path, **values: str) -> None:
    (tmp_path / "secrets.json").write_text(json.dumps({"version": 1, "secrets": values}))


def start(tmp_path, environ: dict, rewritten: dict | None = None, **kw) -> Import:
    """The import as the hub's lifespan makes it."""
    return Import(data_dir=tmp_path, targets=kw.get("targets", targets()),
                  satellites=kw.get("satellites", satellites()),
                  on_buttons=(rewritten if rewritten is not None else {}).update, environ=environ)


def secrets() -> secret_client.Secrets:
    return secret_client.current()


async def test_everything_the_hub_held_goes_to_the_store_with_the_hosts_its_config_names(
        tmp_path, store):
    legacy(tmp_path, OPENROUTER_API_KEY=LLM_KEY)
    rewritten: dict = {}
    job = start(tmp_path, {"SATELLITES_HA_TOKEN": TOKEN, "SATELLITES_MQTT_URL": MQTT}, rewritten)
    assert await job.attempt()

    [body] = store.imports
    assert body["final"] is True
    assert body["declared"]["SATELLITES_HA_TOKEN"] == ["https://ha.lan:8123"]
    assert body["declared"]["OPENROUTER_API_KEY"] == ["https://openrouter.test:443"]
    assert body["declared"][BUTTON] == ["https://ha.lan:8123"]
    assert [(s["name"], s["kind"], s["allowed_hosts"], s["source"]) for s in body["secrets"]] == [
        ("OPENROUTER_API_KEY", "bearer", ["https://openrouter.test:443"], "secrets.json"),
        (BUTTON, "secret_url", ["https://ha.lan:8123"], f"button mode press of satellite {NID}"),
        ("SATELLITES_HA_TOKEN", "bearer", ["https://ha.lan:8123"], "env SATELLITES_HA_TOKEN"),
        ("SATELLITES_MQTT_PASSWORD", "password", ["mqtt://broker.test:1883"], "SATELLITES_MQTT_URL"),
    ]
    assert store.values["SATELLITES_MQTT_PASSWORD"]["value"] == "broker-pass-do-not-leak"
    assert store.values[BUTTON]["value"] == HOOK
    # Confirmed, so the hub gives up its own copies, and says it is done.
    assert not (tmp_path / "secrets.json").exists()
    assert rewritten == {(NID, "mode", "press"): BUTTON}
    done = json.loads((tmp_path / MARKER).read_text())
    assert done["gateway"] == "taken" and done["missing"] == []
    assert TOKEN not in (tmp_path / MARKER).read_text()


async def test_a_webhooks_bearer_beside_a_url_secret_is_imported_as_a_bearer(tmp_path, store):
    """Its URL is in the store, not the configuration, so its host is not
    known; it is a token all the same, never an address."""
    hook = [Target("HOOK_URL", None, holds_url=True), Target("HOOK_TOKEN", None)]
    job = start(tmp_path, {"HOOK_URL": HOOK, "HOOK_TOKEN": "hook-token"}, targets=hook,
                satellites={})
    kinds = {c.name: (c.kind, c.hosts) for c in job.found.candidates.values()}
    assert kinds == {"HOOK_URL": ("secret_url", ["https://ha.lan:8123"]),
                     "HOOK_TOKEN": ("bearer", [])}


async def test_while_the_gateway_is_away_the_hub_keeps_its_own_copies_and_uses_them(
        tmp_path, store):
    """D45: a hub started before its gateway carries on with what it had,
    each value still held to the hosts its configuration sends it to."""
    legacy(tmp_path, OPENROUTER_API_KEY=LLM_KEY)
    store.down = True
    rewritten: dict = {}
    job = start(tmp_path, {"SATELLITES_HA_TOKEN": TOKEN}, rewritten)
    assert not await job.attempt()

    assert await secrets().value_for("SATELLITES_HA_TOKEN", HA_URL) == TOKEN
    assert await secrets().value_for("OPENROUTER_API_KEY", LLM_URL + "/chat/completions") == LLM_KEY
    with pytest.raises(secret_client.HostNotAllowed):
        await secrets().value_for("SATELLITES_HA_TOKEN", "https://attacker.test")
    assert (tmp_path / "secrets.json").exists() and rewritten == {}
    assert not (tmp_path / MARKER).exists()

    store.down = False
    assert await job.attempt()
    assert not (tmp_path / "secrets.json").exists() and rewritten


async def test_where_a_copy_may_go_is_fixed_when_the_hub_starts(tmp_path, store):
    """A wake word saved while the gateway is away (satellites:admin, no
    step-up) naming the HA token at its own host widens nothing: not the
    copy's hosts, and not the hosts the import asks for (D41)."""
    store.down = True
    live = targets()
    job = start(tmp_path, {"SATELLITES_HA_TOKEN": TOKEN}, targets=live)
    live.append(Target("SATELLITES_HA_TOKEN", "https://attacker.test"))
    assert not await job.attempt()
    with pytest.raises(secret_client.HostNotAllowed):
        await secrets().value_for("SATELLITES_HA_TOKEN", "https://attacker.test/api")
    store.down = False
    assert await job.attempt()
    assert store.values["SATELLITES_HA_TOKEN"]["allowed_hosts"] == ["https://ha.lan:8123"]


@pytest.mark.parametrize("refusal", [403, 404, 422])
async def test_a_refused_import_ends_the_hubs_own_copies_and_a_cleared_secret_stays_cleared(
        tmp_path, store, caplog, refusal):
    """403 once secrets:import is removed (D66), 404 from a gateway without
    the route, 422 for a body it does not take: asking again changes none of
    them. The store answers 404 for the token (an admin cleared it), and the
    environment's copy must not bring it back (D42, D44)."""
    store.import_status = refusal
    job = start(tmp_path, {"SATELLITES_HA_TOKEN": TOKEN})
    with caplog.at_level(logging.WARNING, logger="voice-satellites.secrets"):
        assert await job.attempt()
    assert await secrets().value_for("SATELLITES_HA_TOKEN", HA_URL) is None
    assert f"refused the import ({refusal})" in caplog.text and TOKEN not in caplog.text
    assert json.loads((tmp_path / MARKER).read_text())["gateway"] == "closed"


async def test_once_the_gateway_has_answered_the_hubs_copies_are_gone_even_if_it_goes_away(
        tmp_path, store):
    def answers_then_goes(request: httpx.Request) -> httpx.Response:
        answer = store(request)
        store.down = store.down or request.url.path == "/internal/secrets/import"
        return answer
    secret_client.configure(secret_client.Secrets(transport=httpx.MockTransport(answers_then_goes)))
    job = start(tmp_path, {"SATELLITES_HA_TOKEN": TOKEN})
    assert not await job.attempt()          # taken, then nothing could be confirmed
    assert await secrets().value_for("SATELLITES_HA_TOKEN", HA_URL) is None
    assert not (tmp_path / MARKER).exists()
    store.down = False
    assert await job.attempt()
    assert len(store.imports) == 1          # confirmed, not imported again


async def test_after_the_import_a_restart_keeps_no_copy_and_asks_nothing(tmp_path, store, caplog):
    """MARKER: the environment is read once. A restart while the gateway is
    away, or after an admin cleared the token, never brings it back."""
    assert await start(tmp_path, {"SATELLITES_HA_TOKEN": TOKEN}).attempt()
    store.clear("SATELLITES_HA_TOKEN")
    store.down = True
    secret_client.configure(secret_client.Secrets(transport=httpx.MockTransport(store)))
    with caplog.at_level(logging.WARNING, logger="voice-satellites.secrets"):
        again = start(tmp_path, {"SATELLITES_HA_TOKEN": TOKEN})
        assert await again.attempt()
    assert await secrets().value_for("SATELLITES_HA_TOKEN", HA_URL) is None
    store.down = False
    assert await secrets().value_for("SATELLITES_HA_TOKEN", HA_URL) is None
    assert len(store.imports) == 1
    assert "SATELLITES_HA_TOKEN is set in the hub's environment and ignored, because the " \
           "import is done" in caplog.text


async def test_the_import_never_overwrites_what_an_admin_stored(tmp_path, store):
    store.put("SATELLITES_HA_TOKEN", "set-in-admin-secrets", [HA_URL])
    assert await start(tmp_path, {"SATELLITES_HA_TOKEN": TOKEN}).attempt()
    assert store.values["SATELLITES_HA_TOKEN"]["value"] == "set-in-admin-secrets"
    # The store's value is the one used: the environment's is ignored.
    assert await secrets().value_for("SATELLITES_HA_TOKEN", HA_URL) == "set-in-admin-secrets"


async def test_a_variable_still_set_is_named_as_ignored_and_never_logged(tmp_path, store, caplog):
    with caplog.at_level(logging.DEBUG):
        assert await start(tmp_path, {"SATELLITES_HA_TOKEN": TOKEN,
                                      "SATELLITES_MQTT_URL": MQTT}).attempt()
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("SATELLITES_HA_TOKEN is set in the hub's environment and ignored" in w
               for w in warned), warned
    for secret in (TOKEN, "broker-pass-do-not-leak", "s3cret-hook-id"):
        assert secret not in caplog.text


async def test_what_the_store_did_not_take_is_named_and_no_longer_sent(tmp_path, store, caplog):
    """The window closed before this hub imported (reopen-import reopens
    it). Its own copies are given up all the same: the environment is read
    once, and the store is the one source after."""
    store.closed = True
    with caplog.at_level(logging.WARNING, logger="voice-satellites.secrets"):
        assert await start(tmp_path, {"SATELLITES_HA_TOKEN": TOKEN}).attempt()
    assert await secrets().held("SATELLITES_HA_TOKEN") is None
    assert "did not take" in caplog.text and "SATELLITES_HA_TOKEN" in caplog.text
    assert "reopen-import" in caplog.text and MARKER in caplog.text and TOKEN not in caplog.text
    assert json.loads((tmp_path / MARKER).read_text())["missing"] == [
        BUTTON, "SATELLITES_HA_TOKEN"]


async def test_the_hubs_own_settings_are_never_imported_from_the_environment(tmp_path, store):
    """An action naming SATELLITES_MQTT_URL as its key would otherwise carry
    the broker's URL, password and all, into the store with that action's
    host as where it may go."""
    hostile = targets() + [Target("SATELLITES_MQTT_URL", "https://attacker.test")]
    assert await start(tmp_path, {"SATELLITES_MQTT_URL": MQTT}, targets=hostile).attempt()
    names = [s["name"] for s in store.imports[0]["secrets"]]
    assert "SATELLITES_MQTT_URL" not in names and "SATELLITES_MQTT_PASSWORD" in names
    assert store.values["SATELLITES_MQTT_PASSWORD"]["allowed_hosts"] == ["mqtt://broker.test:1883"]


async def test_an_error_nobody_foresaw_is_tried_again_and_the_copies_are_gone_first(
        tmp_path, store, caplog, monkeypatch):
    """A failure after the gateway answered (here, saving the rewritten
    buttons) costs a minute, never the import, and never brings the hub's
    copies back."""
    monkeypatch.setattr(secret_import, "RETRY_S", 0.0)
    copies_left: list[dict] = []

    def fails_once(rewritten: dict) -> None:
        copies_left.append(dict(secrets()._local))
        if len(copies_left) == 1:
            raise RuntimeError("disk full")
    job = Import(data_dir=tmp_path, targets=targets(), satellites=satellites(),
                 on_buttons=fails_once, environ={"SATELLITES_HA_TOKEN": TOKEN})
    with caplog.at_level(logging.WARNING, logger="voice-satellites.secrets"):
        await job.run()
    assert copies_left == [{}, {}] and (tmp_path / MARKER).exists()
    assert "the import failed (RuntimeError)" in caplog.text and "disk full" not in caplog.text


def test_a_buttons_secret_name_is_one_of_its_own_and_always_fits_the_store():
    assert button_secret(NID, "mode", "press") == f"SATELLITES_BUTTON_{NID.upper()}_MODE_PRESS"
    assert button_secret(NID, "vol_up", "press") == f"SATELLITES_BUTTON_{NID.upper()}_VOL_UP_PRESS"
    names = [button_secret(NID, "a" * 32, "release"), button_secret(NID, "a" * 31 + "b", "release"),
             button_secret(NID, "vol-up", "press"), button_secret(NID, "vol_up", "press"),
             button_secret(NID, "Vol_up", "press"), button_secret(NID, "x", "y_press")]
    for name in names:
        assert SECRET_NAME.fullmatch(name), name
    assert len(set(names)) == len(names), names


def test_two_buttons_that_read_the_same_are_imported_as_two_secrets(tmp_path):
    """vol-up and vol_up are both valid button names: one name for both
    would send one's press to the other's webhook."""
    two = {NID: {"buttons": {"vol-up": {"press": "webhook:https://ha.lan/api/webhook/one"},
                             "vol_up": {"press": "webhook:https://ha.lan/api/webhook/two"}}}}
    found = gather(data_dir=tmp_path, targets=[], satellites=two, environ={})
    assert sorted(c.value for c in found.candidates.values()) == [
        "https://ha.lan/api/webhook/one", "https://ha.lan/api/webhook/two"]


def test_secrets_json_is_overwritten_with_zeros_before_it_is_removed(tmp_path):
    legacy(tmp_path, OPENROUTER_API_KEY=LLM_KEY)
    path = tmp_path / "secrets.json"
    witness = tmp_path / "witness"
    os.link(path, witness)   # the same file under a second name: what the disk holds
    size = path.stat().st_size
    shred(path)
    assert not path.exists()
    assert witness.read_bytes() == b"\0" * size


# ---- in the hub: a button's raw URL (D62) ---------------------------------------------


@pytest.fixture
def hub_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SATELLITES_DATA_DIR", str(tmp_path))
    held = Store(tmp_path)
    held.adopt(NID, "kitchen", "esp32-korvo-v1.1")
    held.satellites[NID].config["buttons"]["mode"] = {"press": f"webhook:{HOOK}"}
    held.save_satellites()
    return importlib.reload(importlib.import_module("app.main"))


def test_a_raw_webhook_is_imported_at_start_and_rewritten_only_once_the_store_holds_it(
        hub_app, tmp_path, store):
    store.down = True
    with TestClient(hub_app.app) as client:
        before = client.get(f"/satellites/{NID}").json()["config"]["buttons"]["mode"]
        on_disk = (tmp_path / "satellites.json").read_text()
        store.down = False
        # The minute's retry, now, as the lifespan would make it.
        job = Import(data_dir=hub_app.DATA_DIR,
                     targets=hub_app.wakewords_config.WordActions(
                         hub_app.hub.voice.assignment).targets(),
                     satellites={n: r.config for n, r in hub_app.hub.store.satellites.items()},
                     on_buttons=hub_app.hub.imported_buttons)
        assert client.portal.call(job.attempt)
        after = json.loads((tmp_path / "satellites.json").read_text())

    # Shown under the secret it is being imported as, never as the URL, even
    # to satellites:admin; and kept on disk until the store confirms it.
    assert before == {"press": f"webhook:secret:{BUTTON}"}
    assert HOOK in on_disk
    assert after["satellites"][0]["config"]["buttons"]["mode"] == {"press": f"webhook:secret:{BUTTON}"}
    assert store.values[BUTTON] == {"value": HOOK, "version": 1, "kind": "secret_url",
                                    "allowed_hosts": ["https://ha.lan:8123"]}


def test_a_done_import_names_the_environment_copies_still_set(tmp_path, store):
    """WHAT ADMIN ASKS TO BE REMOVED, AND ONLY WHILE IT IS THERE. The page used
    to warn about every secret ever imported from the environment, for ever,
    so the warning outlived the variable. Once the import is done the hub
    names, in /health, exactly the copies its environment still holds."""
    (tmp_path / "secret-import.done").write_text("")
    still_set = start(tmp_path, {"SATELLITES_HA_TOKEN": "a-stand-in-token-of-some-length"})
    assert still_set.leftover_variables() == ["SATELLITES_HA_TOKEN"]
    removed = start(tmp_path, {})
    assert removed.leftover_variables() == []


def test_before_the_gateway_answers_the_hubs_own_copies_are_not_leftovers(tmp_path, store):
    """Until the import is answered the environment's values stand in for the
    store, so they are in use, not ignored, and Admin must not ask for them."""
    job = start(tmp_path, {"SATELLITES_HA_TOKEN": "a-stand-in-token-of-some-length"})
    assert job.leftover_variables() == []
