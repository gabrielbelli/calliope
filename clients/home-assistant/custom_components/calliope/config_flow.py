"""Set up Calliope: the gateway's URL, an optional key, and TLS checking."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CalliopeAuthError, CalliopeClient, CalliopeError
from .const import DEFAULT_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, DEFAULT_URL)): str,
            vol.Optional(
                CONF_API_KEY,
                description={"suggested_value": defaults.get(CONF_API_KEY)},
            ): str,
            vol.Required(
                CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)
            ): bool,
        }
    )


def _normalise(data: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        CONF_URL: str(data[CONF_URL]).strip().rstrip("/"),
        CONF_VERIFY_SSL: bool(data.get(CONF_VERIFY_SSL, True)),
    }
    if key := str(data.get(CONF_API_KEY) or "").strip():
        out[CONF_API_KEY] = key
    return out


async def _validate(hass: HomeAssistant, data: Mapping[str, Any]) -> str | None:
    """None when Calliope answers /health and accepts the key; else the
    form's error key."""
    parts = urlsplit(data[CONF_URL])
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return "invalid_url"
    session = async_get_clientsession(hass, verify_ssl=data[CONF_VERIFY_SSL])
    client = CalliopeClient(session, data[CONF_URL], data.get(CONF_API_KEY))
    try:
        health = await client.health()
        if not isinstance(health, dict) or "status" not in health:
            return "not_calliope"
        await client.check_key()
    except CalliopeAuthError:
        return "invalid_auth"
    except CalliopeError as err:
        _LOGGER.debug("Calliope at %s did not answer: %s", data[CONF_URL], err)
        return "cannot_connect"
    return None


class CalliopeConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry per gateway."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the gateway."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalise(user_input)
            await self.async_set_unique_id(urlsplit(data[CONF_URL]).netloc.lower())
            self._abort_if_unique_id_configured()
            if (error := await _validate(self.hass, data)) is None:
                return self.async_create_entry(
                    title=urlsplit(data[CONF_URL]).hostname or "Calliope", data=data
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
            errors=errors,
            description_placeholders={"example_url": DEFAULT_URL},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The gateway started refusing the key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new key."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalise(
                {**entry.data, CONF_API_KEY: user_input.get(CONF_API_KEY)}
            )
            if (error := await _validate(self.hass, data)) is None:
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_API_KEY): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """The same three fields, changed after setup."""
        return CalliopeOptionsFlow()


class CalliopeOptionsFlow(OptionsFlow):
    """Change the URL, key or TLS checking. They live in the entry's data,
    so a reauth and this flow edit the same values; the entry reloads."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the current values."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalise(user_input)
            if (error := await _validate(self.hass, data)) is None:
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=data
                )
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
                return self.async_create_entry(data={})
            errors["base"] = error
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(user_input or self.config_entry.data),
            errors=errors,
            description_placeholders={"example_url": DEFAULT_URL},
        )
