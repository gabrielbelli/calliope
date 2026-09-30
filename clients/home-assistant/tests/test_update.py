"""Firmware: the update the hub would send, installing it, and its
progress from the hub's ota events."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import until
from .fake_calliope import KITCHEN_ID, FakeCalliope

NEW = "v0.1.2-200-gabc1234"
IMAGE = "ab" * 32


def _firmware(hass: HomeAssistant) -> dict:
    state = hass.states.get("update.kitchen_firmware")
    return {"state": state.state, **state.attributes}


async def _install(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        "update", "install", {"entity_id": "update.kitchen_firmware"}, blocking=True
    )


async def test_an_update_is_offered_installed_and_its_progress_shown(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """A git-describe version AwesomeVersion cannot order is still offered;
    installing asks the hub for that image; the hub's events show it being
    sent, the restart, and the new firmware."""
    fake.satellites[KITCHEN_ID]["update"] = {
        "sha256": IMAGE,
        "version": NEW,
        "uploaded_at": 1_700_000_000.0,
    }
    fake.push(
        {
            "type": "firmware",
            "action": "added",
            "sha256": IMAGE,
            "model": "esp32-korvo-v1.1",
            "version": NEW,
        }
    )
    await until(hass, lambda: _firmware(hass)["state"] == "on")
    firmware = _firmware(hass)
    assert firmware["installed_version"] == "v0.1.2-163-gf60932d"
    assert firmware["latest_version"] == NEW
    assert firmware["title"] == "esp32-korvo-v1.1"
    assert firmware["in_progress"] is False

    await _install(hass)
    assert fake.calls("POST", "/satellites/ota") == [
        {"satellite": KITCHEN_ID, "sha256": IMAGE}
    ]
    # The hub no longer offers the version it is installing: the entity
    # shows it on its way, not "up to date".
    firmware = _firmware(hass)
    assert firmware["in_progress"] is True
    assert firmware["state"] == "on"
    assert firmware["latest_version"] == NEW

    for state, pct in (("started", 0), ("progress", 40)):
        fake.push(
            {
                "type": "ota",
                "satellite": KITCHEN_ID,
                "state": state,
                "pct": pct,
                "version": None,
                "error": None,
            }
        )
    await until(hass, lambda: _firmware(hass)["update_percentage"] == 40)
    fake.push(
        {
            "type": "ota",
            "satellite": KITCHEN_ID,
            "state": "rebooting",
            "pct": 100,
            "version": None,
            "error": None,
        }
    )
    await until(hass, lambda: _firmware(hass)["update_percentage"] is None)
    assert _firmware(hass)["in_progress"] is True
    assert _firmware(hass)["latest_version"] == NEW

    sat = fake.satellites[KITCHEN_ID]
    sat["firmware"] = NEW
    sat["ota"] = {"state": "verified", "version": NEW}
    fake.push(
        {
            "type": "ota",
            "satellite": KITCHEN_ID,
            "state": "verified",
            "pct": None,
            "version": NEW,
            "error": None,
        }
    )
    await until(hass, lambda: _firmware(hass)["installed_version"] == NEW)
    assert _firmware(hass)["state"] == "off"
    assert _firmware(hass)["in_progress"] is False


async def test_an_update_the_hub_skips_says_why(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """The hub's reason, not a silent nothing."""
    fake.satellites[KITCHEN_ID]["update"] = {"sha256": IMAGE, "version": NEW}
    fake.ota_skip[KITCHEN_ID] = "already updating"
    fake.push({"type": "config", "satellite": KITCHEN_ID, "changed": []})
    await until(hass, lambda: _firmware(hass)["state"] == "on")
    with pytest.raises(HomeAssistantError, match="already updating") as err:
        await _install(hass)
    assert err.value.translation_key == "update_skipped"


async def test_no_update_means_up_to_date(
    hass: HomeAssistant, fake: FakeCalliope, loaded: MockConfigEntry
) -> None:
    """No update from the hub (none newer, or a hub from before the field):
    the latest version is the installed one."""
    firmware = _firmware(hass)
    assert firmware["state"] == "off"
    assert firmware["latest_version"] == firmware["installed_version"]
    with pytest.raises(HomeAssistantError):
        await _install(hass)
    assert fake.calls("POST", "/satellites/ota") == []
