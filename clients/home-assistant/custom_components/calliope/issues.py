"""The repair issue for an API key that lacks a scope.

Calliope answers a route the key may not use with 403 and names the scopes
that route needs. Home Assistant cannot fix that by itself: a key's scopes are
chosen in Calliope when the key is made. So the issue names them, says
that a key made with the home-assistant preset holds every one the
integration uses, and links to the README's section on the key.

One issue per entry lists every scope refused since the entry was set up. It
goes when the entry is set up again, which is what entering a new key does
(Reconfigure, Reauthenticate), and when the entry is removed. A key that only
cannot reach the satellites is a warning rather than an error: speech works
without them, as it does on a stack with no satellite hub.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .api import CalliopeScopeError
from .const import DOMAIN, KEY_DOCS_URL


def _issue_id(entry: ConfigEntry) -> str:
    return f"missing_scope_{entry.entry_id}"


@callback
def reporter(
    hass: HomeAssistant, entry: ConfigEntry
) -> Callable[[CalliopeScopeError], None]:
    """What the entry's client calls on every 403: raise the issue, or widen
    it when a scope it does not list yet is refused."""
    refused: set[str] = set()

    @callback
    def report(err: CalliopeScopeError) -> None:
        known = len(refused)
        refused.update(err.lacking)
        if len(refused) == known:
            # The vocabulary is written every hour: the same refusal must not
            # rewrite the issue each time.
            return
        satellites_only = all(scope.startswith("satellites:") for scope in refused)
        ir.async_create_issue(
            hass,
            DOMAIN,
            _issue_id(entry),
            is_fixable=False,
            learn_more_url=KEY_DOCS_URL,
            severity=ir.IssueSeverity.WARNING
            if satellites_only
            else ir.IssueSeverity.ERROR,
            translation_key="missing_scope",
            translation_placeholders={
                "entry": entry.title,
                "scopes": ", ".join(sorted(refused)),
            },
        )

    return report


@callback
def clear(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The entry is set up again with whatever key it now has, or is gone."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
