"""The strings and icons: English and Brazilian Portuguese hold the same keys
and placeholders, every key the integration uses is in them, and none is
left over.

The keys are read from the code itself (its syntax tree, for the config flow
and the errors) and from a loaded entry (for the entities), so a string
renamed in one place and not the other fails here rather than showing a raw
key to someone."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.calliope.const import (
    BUTTON_EVENT_TYPES,
    BUTTON_LABELS,
    DOMAIN,
    TRIGGER_TYPES,
    VOICE_EVENT_TYPES,
)
from custom_components.calliope.select import DEFAULT, OWN

from .conftest import until
from .fake_calliope import FakeCalliope

COMPONENT = Path(__file__).parents[1] / "custom_components" / DOMAIN
EN = json.loads((COMPONENT / "translations" / "en.json").read_text())
PT_BR = json.loads((COMPONENT / "translations" / "pt-BR.json").read_text())
ICONS = json.loads((COMPONENT / "icons.json").read_text())
SERVICES = yaml.safe_load((COMPONENT / "services.yaml").read_text())
PLACEHOLDER = re.compile(r"\{(\w+)\}")
# What the config flow passes as description_placeholders.
FLOW_PLACEHOLDERS = {"example_url", "scopes"}
# Entities whose icon comes from somewhere other than icons.json: Home
# Assistant gives speech-to-text and text-to-speech entities their domain's.
DOMAIN_ICON = {"stt", "tts"}


def _flat(tree: dict[str, Any], path: tuple[str, ...] = ()) -> dict[tuple[str, ...], str]:
    flat: dict[tuple[str, ...], str] = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            flat |= _flat(value, (*path, key))
        else:
            flat[(*path, key)] = value
    return flat


def _tree(module: str) -> ast.Module:
    return ast.parse((COMPONENT / module).read_text())


def _strings(node: ast.AST) -> set[str]:
    """Every string constant in an expression: `"a" if x else "b"` is both."""
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _keyword(module: ast.Module, name: str, method: str | None = None) -> set[str]:
    """The strings passed as keyword `name` anywhere in a module, or only to
    calls of `method`."""
    found: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and (
            method is None or getattr(node.func, "attr", None) == method
        ):
            for keyword in node.keywords:
                if keyword.arg == name:
                    found |= _strings(keyword.value)
    return found


def _translated(module: ast.Module) -> dict[str, set[str]]:
    """translation_key to the names of its translation_placeholders, for every
    call in a module that names both as literals."""
    found: dict[str, set[str]] = {}
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        given = {k.arg: k.value for k in node.keywords}
        key = given.get("translation_key")
        if not isinstance(key, ast.Constant):
            continue
        placeholders = given.get("translation_placeholders")
        names = (
            {k.value for k in placeholders.keys if isinstance(k, ast.Constant)}
            if isinstance(placeholders, ast.Dict)
            else set()
        )
        found.setdefault(key.value, set()).update(names)
    return found


def test_both_languages_hold_the_same_strings_and_placeholders() -> None:
    """A key in one language only shows English to a Brazilian, or a raw key
    to everyone; a placeholder left out of a translation says less."""
    en, pt_br = _flat(EN), _flat(PT_BR)
    assert set(en) == set(pt_br)
    for key, text in en.items():
        assert text.strip() and pt_br[key].strip(), key
        assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(pt_br[key])), key


def test_the_config_flow_has_a_string_for_every_step_error_and_ending() -> None:
    """Each step's form, each error it can show, each way it can end, and
    nothing else. already_configured and already_in_progress are raised by
    Home Assistant's own unique id checks."""
    flow = _tree("config_flow.py")
    errors = {
        node.args[0].value
        for node in ast.walk(flow)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_Checked"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    config = EN["config"]
    assert set(config["step"]) == _keyword(flow, "step_id")
    assert set(config["error"]) == errors
    assert set(config["abort"]) == _keyword(flow, "reason") | {
        "already_configured",
        "already_in_progress",
    }
    assert set(config["create_entry"]) == _keyword(
        flow, "description", "async_create_entry"
    )
    for step in config["step"].values():
        assert set(step.get("data_description", {})) <= set(step["data"])
    for key, text in _flat(config).items():
        assert set(PLACEHOLDER.findall(text)) <= FLOW_PLACEHOLDERS, key


def test_every_error_and_issue_the_code_raises_is_translated_with_its_placeholders() -> None:
    """Exceptions shown to someone who pressed a button or ran an action, and
    the repair issue: the same keys, and the placeholders each passes."""
    exceptions: dict[str, set[str]] = {}
    issues: dict[str, set[str]] = {}
    for path in sorted(COMPONENT.glob("*.py")):
        found = _translated(_tree(path.name))
        for key, names in found.items():
            (issues if path.name == "issues.py" else exceptions).setdefault(
                key, set()
            ).update(names)
    assert {
        key: set(PLACEHOLDER.findall(value["message"]))
        for key, value in EN["exceptions"].items()
    } == exceptions
    assert {
        key: set(PLACEHOLDER.findall(value["title"] + value["description"]))
        for key, value in EN["issues"].items()
    } == issues


def test_the_actions_and_triggers_match_their_definitions() -> None:
    """services.yaml's actions and fields, the event types each event entity
    fires, the select options that are translated, and the device triggers.
    A trigger subtype is a button the board prints, or one of the hub's
    bundled wake words."""
    assert {
        service: set(spec.get("fields", {})) for service, spec in SERVICES.items()
    } == {service: set(spec["fields"]) for service, spec in EN["services"].items()}
    assert set(ICONS["services"]) == set(SERVICES)
    event = EN["entity"]["event"]
    assert set(event["voice"]["state_attributes"]["event_type"]["state"]) == set(
        VOICE_EVENT_TYPES
    )
    assert set(event["button"]["state_attributes"]["event_type"]["state"]) == set(
        BUTTON_EVENT_TYPES
    )
    select = EN["entity"]["select"]
    assert set(select["output_satellite"]["state"]) == {OWN}
    assert set(select["output_device"]["state"]) == {DEFAULT}
    assert set(select["input_device"]["state"]) == {DEFAULT}
    automation = EN["device_automation"]
    assert set(automation["trigger_type"]) == set(TRIGGER_TYPES)
    assert set(BUTTON_LABELS) <= set(automation["trigger_subtype"])


async def test_every_entity_is_named_and_has_an_icon(
    hass: HomeAssistant, fake: FakeCalliope, entry: MockConfigEntry
) -> None:
    """A Korvo and a Pi between them have every satellite entity there is,
    and a stack serving Parakeet, its pt-BR fine-tune and Whisper has every
    engine with a name of its own. Each entity's translation key has a name,
    every name is some entity's, and an entity with no device class to take
    an icon from has one in icons.json."""
    fake.stt_models = [
        {"id": engine, "family": engine.split("-")[0], "default": engine == "parakeet",
         "languages": ["pt"], "accepts_language": engine == "whisper",
         "accepts_boost": engine != "whisper"}
        for engine in ("parakeet", "parakeet-pt-br", "whisper")
    ]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(hass, lambda: entry.runtime_data.coordinator.connected)
    await hass.async_block_till_done()

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    keyed = {(e.domain, e.translation_key) for e in entities if e.translation_key}
    named = {
        (platform, key) for platform, keys in EN["entity"].items() for key in keys
    }
    assert keyed == named
    with_icons = {
        (platform, key) for platform, keys in ICONS["entity"].items() for key in keys
    }
    assert with_icons <= named
    for e in entities:
        if e.domain in DOMAIN_ICON or e.original_device_class is not None:
            continue
        if e.translation_key is not None:
            assert (e.domain, e.translation_key) in with_icons, e.entity_id
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
