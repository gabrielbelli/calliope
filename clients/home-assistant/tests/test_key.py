"""The key's local check, the scope grammar, and the fake gateway's preset and
control fields, against the real gateway, scope table and hub in this
repository.

All are copies the integration and its tests cannot import: Home Assistant
installs this folder alone. These tests hold the copies to the originals, and
skip only in a copy of this folder split out of the repository (README, With
HACS).
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from custom_components.calliope.api import SCOPE, well_formed_key

from .fake_calliope import CONTROL_FIELDS, HOME_ASSISTANT_PRESET, new_key

REPOSITORY = Path(__file__).resolve().parents[3]
GATEWAY_TOKENS = REPOSITORY / "services/gateway/app/tokens.py"
COMMON = REPOSITORY / "packages/common"
HUB = REPOSITORY / "services/satellites/app/main.py"


def _gateway_tokens() -> ModuleType:
    if not GATEWAY_TOKENS.exists():
        pytest.skip("not inside the Calliope repository")
    spec = importlib.util.spec_from_file_location("gateway_tokens", GATEWAY_TOKENS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_key_the_gateway_mints_passes_the_local_check() -> None:
    tokens = _gateway_tokens()
    for _ in range(200):
        assert well_formed_key(tokens.mint(tokens.USER_KEY_PREFIX))


def test_a_service_key_the_gateway_mints_fails_the_local_check() -> None:
    """calliope_svc_ keys belong to Calliope's own services, never to Home
    Assistant."""
    tokens = _gateway_tokens()
    assert not well_formed_key(tokens.mint(tokens.SERVICE_KEY_PREFIX))


def test_any_one_character_changed_fails_the_local_check() -> None:
    """The checksum catches a typo anywhere after the prefix, in the random
    part or in the checksum itself."""
    key = new_key()
    assert well_formed_key(key)
    for at in range(len("calliope_"), len(key)):
        swapped = "x" if key[at] != "x" else "y"
        assert not well_formed_key(key[:at] + swapped + key[at + 1 :]), at


def _scopes(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    if not COMMON.exists():
        pytest.skip("not inside the Calliope repository")
    monkeypatch.syspath_prepend(str(COMMON))
    return importlib.import_module("voice_common.scopes")


def test_the_fake_gateway_holds_the_real_home_assistant_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test runs with the fake's key, so the suite passing means the
    integration needs nothing the home-assistant preset does not hold."""
    assert _scopes(monkeypatch).PRESETS["home-assistant"].scopes == HOME_ASSISTANT_PRESET


def test_the_client_reads_a_challenge_with_the_gateways_scope_grammar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What a 403 names is shown only when it has this shape, so the copy
    must be the grammar itself: every scope there is passes it."""
    scopes = _scopes(monkeypatch)
    assert SCOPE.pattern == scopes.SCOPE.pattern
    assert all(SCOPE.fullmatch(scope) for scope in scopes.SCOPES)


def test_the_fake_hub_takes_the_real_control_fields() -> None:
    """The fake refuses a PATCH outside them without satellites:admin, as the
    hub does, so the suite passing means every setting Home Assistant
    changes is one the home-assistant preset may change. Read from the
    source: the hub's module cannot be imported here."""
    if not HUB.exists():
        pytest.skip("not inside the Calliope repository")
    tree = ast.parse(HUB.read_text())
    [value] = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CONTROL_FIELDS" for t in node.targets)
    ]
    assert isinstance(value, ast.Call) and value.func.id == "frozenset"
    assert ast.literal_eval(value.args[0]) == CONTROL_FIELDS
