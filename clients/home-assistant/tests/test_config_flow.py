"""The config flow, the reconfigure flow and reauth, against the fake gateway."""

from __future__ import annotations

import re
from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.api import UNNAMED_SCOPE
from custom_components.calliope.const import (
    CONF_LEGACY_STT,
    DOMAIN,
    EXAMPLE_URL,
    KEY_DOCS_URL,
)

from .fake_calliope import FakeCalliope, new_key

SATELLITE_SCOPES = {"satellites:read", "satellites:control", "satellites:update"}


@pytest.fixture
def no_setup() -> Generator[None]:
    """A created entry is not set up: these tests are about the flow."""
    with patch("custom_components.calliope.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _submit(hass: HomeAssistant, fake: FakeCalliope, key: str) -> dict:
    result = await _start(hass)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake.url, CONF_API_KEY: key, CONF_VERIFY_SSL: True}
    )


async def test_the_form_suggests_no_address_and_shows_a_neutral_example(hass: HomeAssistant) -> None:
    """Nobody's own address is baked in: the field starts empty, and the
    example beside it is example.com."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"].schema
    url = next(k for k in schema if k == CONF_URL)
    assert not (url.description or {}).get("suggested_value")
    assert result["description_placeholders"]["example_url"] == EXAMPLE_URL
    assert "example.com" in EXAMPLE_URL


@pytest.mark.usefixtures("no_setup")
async def test_the_key_field_hides_what_is_typed(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The key is a bearer secret: setup, reconfigure and reauth take it in a
    password field."""
    for result in (
        await _start(hass),
        await entry.start_reconfigure_flow(hass),
        await entry.start_reauth_flow(hass),
    ):
        field = result["data_schema"].schema[CONF_API_KEY]
        assert field.config["type"] == "password", result["step_id"]


@pytest.mark.usefixtures("no_setup")
async def test_a_key_is_required(hass: HomeAssistant, fake: FakeCalliope) -> None:
    """Calliope answers nothing but liveness without a key: the form does not
    take one left out, and a blank one is refused before the gateway is
    asked."""
    result = await _start(hass)
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: fake.url, CONF_VERIFY_SSL: True}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake.url, CONF_API_KEY: "  ", CONF_VERIFY_SSL: True}
    )
    assert result["errors"] == {"base": "malformed_key"}
    assert fake.hits == []


@pytest.mark.usefixtures("no_setup")
async def test_the_flow_stores_the_key_the_gateway_accepts(
    hass: HomeAssistant, fake: FakeCalliope
) -> None:
    """A well-formed key the gateway does not know is refused; the right one,
    pasted with spaces around it, is stored without them."""
    result = await _submit(hass, fake, new_key())
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: fake.url + "/", CONF_API_KEY: f" {fake.api_key} ", CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "127.0.0.1"
    assert result["data"] == {
        CONF_URL: fake.url,
        CONF_API_KEY: fake.api_key,
        CONF_VERIFY_SSL: True,
    }
    assert result.get("description") is None
    await hass.async_block_till_done()


def _replace_one(key: str) -> str:
    """The key with one character of its random part changed."""
    swapped = "A" if key[12] != "A" else "B"
    return key[:12] + swapped + key[13:]


@pytest.mark.parametrize(
    "mangle",
    [
        lambda key: "sk-home",
        lambda key: key[:-1],
        _replace_one,
        lambda key: "calliope_svc_" + key.removeprefix("calliope_"),
    ],
    ids=["a_key_from_elsewhere", "one_character_short", "one_character_wrong", "a_service_key"],
)
async def test_a_key_that_is_not_a_calliope_key_is_refused_before_the_gateway_is_asked(
    hass: HomeAssistant, fake: FakeCalliope, mangle: Callable[[str], str]
) -> None:
    """The prefix and the checksum are checked here: a key pasted short or
    with a typo is named as such, not as a key the gateway refused."""
    result = await _submit(hass, fake, mangle(fake.api_key))
    assert result["errors"] == {"base": "malformed_key"}
    assert fake.hits == []


async def test_a_key_without_the_scopes_speech_needs_is_refused_naming_them(
    hass: HomeAssistant, fake: FakeCalliope
) -> None:
    """403 on every route speech needs, and /health without the backends:
    one error names every scope, so one new key fixes it. The reserved
    glossary is named with the alternative the gateway also takes."""
    fake.scopes = {"satellites:read"}
    result = await _submit(hass, fake, fake.api_key)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "missing_scopes"}
    assert result["description_placeholders"]["scopes"] == (
        "glossaries:ha, glossaries:read:all, health:read, models:read, "
        "speech:speak, speech:transcribe"
    )


@pytest.mark.parametrize(
    ("scope", "named"),
    [
        # /health never refuses: without the scope it lists no backends.
        ("health:read", "health:read"),
        ("speech:transcribe", "speech:transcribe"),
        ("glossaries:ha", "glossaries:ha, glossaries:read:all"),
    ],
)
async def test_a_key_without_one_scope_speech_needs_is_refused_naming_it(
    hass: HomeAssistant, fake: FakeCalliope, scope: str, named: str
) -> None:
    """Each is found before the entry is saved, not as a repair issue once
    Assist has failed, and nothing is transcribed or written to find it."""
    fake.scopes.discard(scope)
    result = await _submit(hass, fake, fake.api_key)
    assert result["errors"] == {"base": "missing_scopes"}
    assert result["description_placeholders"]["scopes"] == named
    assert fake.calls("POST", "/v1/audio/transcriptions") == []
    assert fake.glossaries == {}


@pytest.mark.parametrize(
    ("challenge", "named"),
    [
        ("[Renew your key](https://example.net/) speech:speak", "speech:speak"),
        ("[Renew your key](https://example.net/)", UNNAMED_SCOPE),
    ],
    ids=["a_scope_among_links", "nothing_but_links"],
)
async def test_a_refusal_shows_only_well_formed_scopes_and_never_the_servers_text(
    hass: HomeAssistant, fake: FakeCalliope, challenge: str, named: str
) -> None:
    """The form is rendered as Markdown, and a server that is not the gateway
    could put a link in its challenge or its message: only names with the
    scope grammar are shown, or a fixed phrase when there are none."""
    fake.scopes.discard("speech:speak")
    fake.hostile_refusal = (challenge, "[Paste your key here](https://example.net/) " * 20)
    result = await _submit(hass, fake, fake.api_key)
    assert result["errors"] == {"base": "missing_scopes"}
    assert result["description_placeholders"]["scopes"] == named


@pytest.mark.usefixtures("no_setup")
async def test_a_key_that_cannot_reach_the_satellites_is_accepted_with_a_warning(
    hass: HomeAssistant, fake: FakeCalliope
) -> None:
    """The hub is optional: speech works, and the warning names the scope a
    key would need for the satellites."""
    fake.scopes -= SATELLITE_SCOPES
    result = await _submit(hass, fake, fake.api_key)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description"] == "no_satellites"
    assert result["description_placeholders"] == {"scopes": "satellites:read"}
    await hass.async_block_till_done()


async def test_cannot_connect(hass: HomeAssistant, fake: FakeCalliope) -> None:
    """Nothing listening."""
    await fake.stop()
    result = await _submit(hass, fake, fake.api_key)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_not_calliope(hass: HomeAssistant, fake: FakeCalliope) -> None:
    """A server that answers /health with something else."""
    fake.health_body = {"hello": "world"}
    result = await _submit(hass, fake, fake.api_key)
    assert result["errors"] == {"base": "not_calliope"}


async def test_an_unforeseen_failure_is_logged_and_shown_as_unknown(
    hass: HomeAssistant, fake: FakeCalliope, caplog: pytest.LogCaptureFixture
) -> None:
    """The form says something went wrong and the log has the traceback,
    rather than the form failing to load."""
    with patch(
        "custom_components.calliope.config_flow.CalliopeClient.health",
        side_effect=RuntimeError("a bug"),
    ):
        result = await _submit(hass, fake, fake.api_key)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
    assert "Unexpected error while checking Calliope" in caplog.text
    assert "RuntimeError: a bug" in caplog.text


async def test_invalid_url(hass: HomeAssistant, fake: FakeCalliope) -> None:
    """No scheme, no request."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "calliope.test:30080", CONF_API_KEY: fake.api_key, CONF_VERIFY_SSL: True},
    )
    assert result["errors"] == {"base": "invalid_url"}
    assert fake.hits == []


async def test_already_configured(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """One entry per gateway address."""
    result = await _submit(hass, fake, fake.api_key)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_changes_the_url_key_tls_and_unique_id(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The key is validated, saved to the entry and the entry reloads. The
    entry's unique id follows the host, so the old one can be added again;
    the speech-to-text engine that keeps the first entity's id stays with
    it."""
    moved = FakeCalliope()  # the gateway, moved to another port
    await moved.start()
    result = await loaded.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: moved.url, CONF_API_KEY: fake.api_key, CONF_VERIFY_SSL: True},
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: moved.url, CONF_API_KEY: moved.api_key, CONF_VERIFY_SSL: False},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert loaded.data == {
        CONF_URL: moved.url,
        CONF_API_KEY: moved.api_key,
        CONF_VERIFY_SSL: False,
        CONF_LEGACY_STT: "parakeet",
    }
    assert loaded.unique_id == moved.url.split("//", 1)[1]
    assert loaded.unique_id != fake.url.split("//", 1)[1]
    assert loaded.state is config_entries.ConfigEntryState.LOADED
    await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    await moved.stop()


async def test_reconfigure_to_a_gateway_already_set_up_aborts(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """Two entries for one gateway would show every satellite twice."""
    other_key = new_key()
    other = MockConfigEntry(
        domain=DOMAIN,
        title="calliope.test",
        unique_id="calliope.test:30080",
        version=2,
        data={
            CONF_URL: "https://calliope.test:30080",
            CONF_API_KEY: other_key,
            CONF_VERIFY_SSL: True,
        },
    )
    other.add_to_hass(hass)
    with patch("custom_components.calliope.async_setup_entry", return_value=True):
        result = await other.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: fake.url, CONF_API_KEY: fake.api_key, CONF_VERIFY_SSL: True},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert other.data[CONF_URL] == "https://calliope.test:30080"
    assert other.data[CONF_API_KEY] == other_key
    assert other.unique_id == "calliope.test:30080"


async def test_reauth_takes_a_new_key_and_keeps_the_first_engine(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """A refused key asks for a new one, and the entry comes back with it.
    The engine that holds the first speech-to-text entity's id is kept, so
    Assist pipelines keep the engine they picked."""
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LEGACY_STT: "parakeet-pt-br"}
    )
    fake.api_key = new_key()  # the old one revoked, a new one made
    assert not await hass.config_entries.async_setup(entry.entry_id)
    [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_KEY: fake.api_key}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_API_KEY] == fake.api_key
    assert entry.data[CONF_LEGACY_STT] == "parakeet-pt-br"
    assert entry.state is config_entries.ConfigEntryState.LOADED
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_reauth_with_a_key_that_cannot_reach_the_satellites_says_so(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """The upgrade's reauth with a speech-only key: the entry loads for
    speech, the dialog says the satellites are out of reach, and so does a
    repair issue that stays until a fuller key is entered."""
    fake.api_key = new_key()
    assert not await hass.config_entries.async_setup(entry.entry_id)
    fake.scopes -= SATELLITE_SCOPES
    [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_KEY: fake.api_key}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_without_satellites"
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert hass.states.get("stt.calliope_parakeet") is not None
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"missing_scope_{entry.entry_id}")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders["scopes"] == "satellites:read"
    assert issue.learn_more_url == KEY_DOCS_URL
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_the_issues_link_names_a_section_of_the_readme() -> None:
    """The repair issue's Learn more opens the README at the key's section:
    GitHub's anchor for a heading is its text, lower case, hyphens for
    spaces and no punctuation."""
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    anchors = {
        re.sub(r"[^\w\- ]", "", line.lstrip("#").strip().lower()).replace(" ", "-")
        for line in readme.splitlines()
        if line.startswith("#")
    }
    url, _, anchor = KEY_DOCS_URL.partition("#")
    assert url.endswith("/clients/home-assistant")
    assert anchor in anchors
