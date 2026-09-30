"""The config flow, the reconfigure flow and reauth, against the fake gateway."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.const import CONF_LEGACY_STT, DOMAIN, EXAMPLE_URL

from .fake_calliope import FakeCalliope


@pytest.fixture
def no_setup() -> Generator[None]:
    """A created entry is not set up: these tests are about the flow."""
    with patch("custom_components.calliope.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
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
async def test_user_flow_creates_entry(hass: HomeAssistant, fake: FakeCalliope) -> None:
    """A keyless gateway: /health answers, the key check passes without one."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake.url + "/", CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "127.0.0.1"
    assert result["data"] == {CONF_URL: fake.url, CONF_VERIFY_SSL: True}
    await hass.async_block_till_done()


@pytest.mark.usefixtures("no_setup")
async def test_user_flow_with_a_key(hass: HomeAssistant, fake: FakeCalliope) -> None:
    """No key is refused; the right key is stored."""
    fake.api_key = "sk-home"
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake.url, CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: fake.url, CONF_API_KEY: " sk-home ", CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_KEY] == "sk-home"
    await hass.async_block_till_done()


async def test_cannot_connect(hass: HomeAssistant, fake: FakeCalliope) -> None:
    """Nothing listening."""
    url = fake.url
    await fake.stop()
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: url, CONF_VERIFY_SSL: True}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_not_calliope(hass: HomeAssistant, fake: FakeCalliope) -> None:
    """A server that answers /health with something else."""
    fake.health_body = {"hello": "world"}
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake.url, CONF_VERIFY_SSL: True}
    )
    assert result["errors"] == {"base": "not_calliope"}


async def test_invalid_url(hass: HomeAssistant) -> None:
    """No scheme, no request."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "calliope.test:30080", CONF_VERIFY_SSL: True}
    )
    assert result["errors"] == {"base": "invalid_url"}


async def test_already_configured(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """One entry per gateway address."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: fake.url, CONF_VERIFY_SSL: True}
    )
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
    moved.api_key = "sk-new"
    result = await loaded.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: moved.url, CONF_API_KEY: "wrong", CONF_VERIFY_SSL: True},
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: moved.url, CONF_API_KEY: "sk-new", CONF_VERIFY_SSL: False},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert loaded.data == {
        CONF_URL: moved.url,
        CONF_API_KEY: "sk-new",
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
    other = MockConfigEntry(
        domain=DOMAIN,
        title="calliope.test",
        unique_id="calliope.test:30080",
        data={CONF_URL: "https://calliope.test:30080", CONF_VERIFY_SSL: True},
    )
    other.add_to_hass(hass)
    with patch("custom_components.calliope.async_setup_entry", return_value=True):
        result = await other.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: fake.url, CONF_VERIFY_SSL: True}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert other.data[CONF_URL] == "https://calliope.test:30080"
    assert other.unique_id == "calliope.test:30080"


async def test_reauth(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """A refused key asks for a new one, and the entry comes back with it."""
    fake.api_key = "sk-rotated"
    assert not await hass.config_entries.async_setup(entry.entry_id)
    [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_API_KEY: "sk-rotated"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_API_KEY] == "sk-rotated"
    assert entry.state is config_entries.ConfigEntryState.LOADED
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
