"""Set up Calliope: the gateway's URL, its API key, and TLS checking.

The key is required: Calliope answers nothing but liveness without one. Its
shape and checksum are checked here first, so a key pasted short is refused
without asking the gateway. Then the gateway is asked, in this order:

    GET /health                     reachable, and a Calliope gateway
    GET /v1/models                  401: the key is refused; 403: it lacks
                                    models:read
    (the /health answer again)      no backends: it lacks health:read
    GET /voices                     403: it lacks speech:speak
    POST /v1/audio/transcriptions   403: it lacks speech:transcribe
    GET /glossaries/home-assistant  403: it lacks glossaries:ha
    GET /satellites                 403: it cannot reach the satellites

A scope missing for speech is an error that names every such scope, so one
new key fixes it. One missing for /satellites is a warning: the hub is
optional, and speech works without it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    HEALTH_READ,
    CalliopeAuthError,
    CalliopeClient,
    CalliopeError,
    CalliopeScopeError,
    lacks_health_read,
    well_formed_key,
)
from .const import CONF_LEGACY_STT, DOMAIN, EXAMPLE_URL

_LOGGER = logging.getLogger(__name__)


def _schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            # Empty on a first setup; what was entered, when reconfiguring.
            vol.Required(CONF_URL, description={"suggested_value": defaults.get(CONF_URL)}): str,
            vol.Required(
                CONF_API_KEY,
                description={"suggested_value": defaults.get(CONF_API_KEY)},
            ): str,
            vol.Required(
                CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)
            ): bool,
        }
    )


def _normalise(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        CONF_URL: str(data[CONF_URL]).strip().rstrip("/"),
        CONF_API_KEY: str(data.get(CONF_API_KEY) or "").strip(),
        CONF_VERIFY_SSL: bool(data.get(CONF_VERIFY_SSL, True)),
    }


@dataclass(frozen=True)
class _Checked:
    """What asking the gateway found."""

    # The form's error key; None when the entry can be saved.
    error: str | None = None
    # missing_scopes: what the key lacks for speech.
    missing: str = ""
    # Saved, with a warning: what the key lacks for the satellites.
    no_satellites: str = ""


async def _validate(hass: HomeAssistant, data: Mapping[str, Any]) -> _Checked:
    parts = urlsplit(data[CONF_URL])
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return _Checked("invalid_url")
    if not well_formed_key(data[CONF_API_KEY]):
        return _Checked("malformed_key")
    session = async_get_clientsession(hass, verify_ssl=data[CONF_VERIFY_SSL])
    client = CalliopeClient(session, data[CONF_URL], data[CONF_API_KEY])
    missing: list[str] = []
    try:
        health = await client.health()
        if not isinstance(health, dict) or "status" not in health:
            return _Checked("not_calliope")
        await client.check_key()
    except CalliopeScopeError as err:
        missing += err.lacking
    except CalliopeAuthError:
        return _Checked("invalid_auth")
    except CalliopeError as err:
        _LOGGER.debug("Calliope at %s did not answer: %s", data[CONF_URL], err)
        return _Checked("cannot_connect")
    # The gateway knows the key now, so /health without the backends is its
    # answer to a key without health:read. Without them the engines Assist
    # offers would be a guess.
    if lacks_health_read(health):
        missing.append(HEALTH_READ)
    # Each asked even when one before was refused, so one answer names
    # everything the key lacks for speech.
    for probe in (client.voices, client.check_transcribe, client.check_glossary):
        missing += await _lacks(probe)
    if missing:
        return _Checked("missing_scopes", missing=_listed(missing))
    if refused := await _lacks(client.satellites):
        return _Checked(no_satellites=_listed(refused))
    return _Checked()


async def _lacks(probe: Callable[[], Awaitable[Any]]) -> list[str]:
    """The scopes `probe` was refused for. Any other failure is not the key's:
    a backend that is down now is setup's business, and setup retries."""
    try:
        await probe()
    except CalliopeScopeError as err:
        return list(err.lacking)
    except CalliopeError as err:
        _LOGGER.debug("Calliope did not answer %s: %s", probe.__name__, err)
    return []


def _listed(scopes: list[str]) -> str:
    return ", ".join(sorted(set(scopes)))


class CalliopeConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per gateway."""

    # 2: the key is required (async_migrate_entry).
    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the gateway."""
        checked = _Checked()
        if user_input is not None:
            data = _normalise(user_input)
            await self.async_set_unique_id(urlsplit(data[CONF_URL]).netloc.lower())
            self._abort_if_unique_id_configured()
            checked = await _validate(self.hass, data)
            if checked.error is None:
                return self.async_create_entry(
                    title=urlsplit(data[CONF_URL]).hostname or "Calliope",
                    data=data,
                    description="no_satellites" if checked.no_satellites else None,
                    description_placeholders={"scopes": checked.no_satellites},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=_errors(checked),
            description_placeholders={"example_url": EXAMPLE_URL, "scopes": checked.missing},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The gateway refused the key, or the entry has none."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new key."""
        entry = self._get_reauth_entry()
        checked = _Checked()
        if user_input is not None:
            data = _normalise({**entry.data, CONF_API_KEY: user_input[CONF_API_KEY]})
            checked = await _validate(self.hass, data)
            if checked.error is None:
                # As in reconfigure: the engine that keeps the first entity's
                # id stays with it, so a new key moves no Assist pipeline.
                kept = {k: v for k, v in entry.data.items() if k == CONF_LEGACY_STT}
                return self.async_update_reload_and_abort(
                    entry,
                    data=data | kept,
                    reason="reauth_without_satellites"
                    if checked.no_satellites
                    else "reauth_successful",
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=_errors(checked),
            description_placeholders={"scopes": checked.missing},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the URL, key or TLS checking. The entry is one gateway, so
        its unique id follows the host: moving it to a gateway that is set up
        already is refused, and the old host is free to be added again."""
        entry = self._get_reconfigure_entry()
        checked = _Checked()
        if user_input is not None:
            data = _normalise(user_input)
            checked = await _validate(self.hass, data)
            if checked.error is None:
                new_uid = urlsplit(data[CONF_URL]).netloc.lower()
                if new_uid != entry.unique_id:
                    await self.async_set_unique_id(new_uid)
                    self._abort_if_unique_id_configured()
                # The engine that keeps the first entity's id stays with it.
                kept = {k: v for k, v in entry.data.items() if k == CONF_LEGACY_STT}
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=new_uid,
                    title=urlsplit(data[CONF_URL]).hostname or "Calliope",
                    data=data | kept,
                    reason="reconfigure_without_satellites"
                    if checked.no_satellites
                    else "reconfigure_successful",
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(user_input or entry.data),
            errors=_errors(checked),
            description_placeholders={"example_url": EXAMPLE_URL, "scopes": checked.missing},
        )


def _errors(checked: _Checked) -> dict[str, str]:
    return {"base": checked.error} if checked.error else {}
