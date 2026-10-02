"""Scopes, roles and presets: the constants every service reads, and the rules over them.

The H1 tests are the reason this file exists. An `admin` preset that held
`users:manage` made a 90-day key a permanent bypass of step-up, and a speech
user who could tick `keys:manage:own` could hand a leaked key the power to
mint more keys.
"""

from __future__ import annotations

import pytest

from voice_common import scopes
from voice_common.scopes import (ADMIN_ONLY, EXPIRY_CAPPED, PRESETS, ROLES,
                                 SCOPES, SERVICE_ONLY, SERVICE_PRINCIPALS,
                                 SESSION_ONLY, effective, expand)


# ── the grammar ──────────────────────────────────────────────────────────────

def test_every_scope_obeys_the_grammar() -> None:
    assert all(scopes.valid_scope(scope) for scope in SCOPES)


@pytest.mark.parametrize("bad", ["jobs:*", "*", "jobs", "jobs:read:mine",
                                 "Jobs:read", "jobs:read:all:own", "jobs: read",
                                 "", None, 3])
def test_the_grammar_refuses_wildcards_and_every_other_shape(bad: object) -> None:
    assert not scopes.valid_scope(bad)


def test_every_all_scope_has_its_own_counterpart() -> None:
    """`:all` implies `:own`, which only means something if the `:own` exists."""
    for scope in SCOPES:
        if scope.endswith(":all"):
            assert scope[:-3] + "own" in SCOPES, scope


def test_the_special_sets_name_only_real_scopes() -> None:
    for group in (SESSION_ONLY, SERVICE_ONLY, ADMIN_ONLY, EXPIRY_CAPPED):
        assert group <= set(SCOPES)
    assert SESSION_ONLY == {"users:manage", "secrets:manage", "keys:manage:own",
                            "keys:manage:all"}


# ── :all implies :own, and effective scopes (D29) ────────────────────────────

def test_all_implies_own_after_expansion() -> None:
    assert expand({"jobs:read:all"}) == {"jobs:read:all", "jobs:read:own"}
    assert expand({"jobs:read:own"}) == {"jobs:read:own"}


def test_effective_keeps_own_when_the_key_and_the_role_hold_all() -> None:
    """Expanding one side only would have lost `:own` after the intersection."""
    granted = effective({"jobs:read:all"}, "admin")
    assert {"jobs:read:all", "jobs:read:own"} <= granted


def test_a_key_with_all_owned_by_a_speech_user_keeps_only_own() -> None:
    """A demotion narrows existing keys at once: the role is read on every request."""
    assert effective({"jobs:read:all"}, "speech") == {"jobs:read:own"}


def test_effective_never_returns_a_session_only_scope_even_from_a_key_row_listing_one() -> None:
    """H1: a row written outside the API must not carry users:manage."""
    for role in ROLES:
        assert not effective(SCOPES, role) & SESSION_ONLY


def test_an_unknown_role_has_no_scopes() -> None:
    assert effective(SCOPES, "superuser") == frozenset()
    assert scopes.session_scopes("superuser") == frozenset()
    assert scopes.grantable("superuser") == frozenset()


def test_a_session_holds_the_session_only_scopes_of_its_role() -> None:
    assert SESSION_ONLY <= scopes.session_scopes("admin")
    assert scopes.session_scopes("speech") & SESSION_ONLY == {"keys:manage:own"}


# ── roles and presets (§1.6) ─────────────────────────────────────────────────

def test_the_admin_role_is_every_scope_but_the_services() -> None:
    assert ROLES["admin"] == set(SCOPES) - SERVICE_ONLY


def test_every_preset_is_within_the_admin_role() -> None:
    for name, preset in PRESETS.items():
        assert preset.scopes <= ROLES["admin"], name


def test_no_preset_contains_a_session_only_scope() -> None:
    for name, preset in PRESETS.items():
        assert not preset.scopes & SESSION_ONLY, name


def test_no_preset_contains_a_service_only_scope() -> None:
    for name, preset in PRESETS.items():
        assert not preset.scopes & SERVICE_ONLY, name


def test_the_admin_preset_is_the_admin_role_without_session_only_scopes() -> None:
    assert PRESETS["admin"].scopes == ROLES["admin"] - SESSION_ONLY


def test_the_speech_preset_is_within_the_speech_role() -> None:
    assert PRESETS["speech"].scopes <= ROLES["speech"]
    assert PRESETS["speech"].scopes == ROLES["speech"] - {"ingest:links",
                                                          "keys:manage:own"}


def test_a_speech_user_is_offered_exactly_the_presets_that_fit() -> None:
    assert scopes.presets_for("speech") == ("speech", "transcribe-only",
                                            "speak-only", "read-only")
    assert scopes.presets_for("admin") == tuple(PRESETS)


# ── what a key may be created with ───────────────────────────────────────────

def test_a_speech_user_cannot_give_a_key_the_power_to_mint_keys() -> None:
    assert scopes.ungrantable({"keys:manage:own", "speech:speak"}, "speech") == {
        "keys:manage:own"}


@pytest.mark.parametrize("scope", sorted(SESSION_ONLY | SERVICE_ONLY))
def test_no_key_of_any_role_may_hold_a_session_or_service_scope(scope: str) -> None:
    for role in ROLES:
        assert scope in scopes.ungrantable({scope}, role)


def test_a_speech_user_cannot_grant_any_all_scope() -> None:
    every_all = {scope for scope in SCOPES if scope.endswith(":all")}
    assert scopes.ungrantable(every_all, "speech") == every_all


def test_an_unknown_scope_is_never_grantable() -> None:
    assert scopes.ungrantable({"jobs:read:everything"}, "admin") == {
        "jobs:read:everything"}


def test_the_home_assistant_preset_may_live_a_year() -> None:
    """recheck M-5: Assist breaking every 90 days is a cost paid for nothing."""
    ha = PRESETS["home-assistant"].scopes
    assert not ha & EXPIRY_CAPPED
    assert scopes.expiry_allowed(ha, 365)


def test_a_key_holding_an_admin_only_scope_never_lives_forever() -> None:
    """The home-assistant preset can push firmware; a leaked copy must still expire."""
    ha = PRESETS["home-assistant"].scopes
    assert scopes.max_expiry_days(ha) == 365
    assert not scopes.expiry_allowed(ha, None)


@pytest.mark.parametrize("name", ["admin", "monitor", "firmware-release"])
def test_presets_that_reach_other_peoples_data_or_firmware_are_capped_at_ninety_days(
        name: str) -> None:
    preset = PRESETS[name].scopes
    assert scopes.max_expiry_days(preset) == 90
    assert scopes.expiry_allowed(preset, 90)
    assert not scopes.expiry_allowed(preset, 365)
    assert not scopes.expiry_allowed(preset, None)


def test_only_the_offered_expiries_are_allowed() -> None:
    speech = PRESETS["speech"].scopes
    assert all(scopes.expiry_allowed(speech, days) for days in (30, 90, 365, None))
    assert not scopes.expiry_allowed(speech, 7)
    assert not scopes.expiry_allowed(speech, 3650)


def test_every_all_scope_is_capped() -> None:
    assert {scope for scope in SCOPES if scope.endswith(":all")} <= EXPIRY_CAPPED


def test_a_key_with_an_admin_only_scope_needs_a_step_up_and_a_speech_key_does_not() -> None:
    """D13: an admin's open session must not quietly become a satellite-controlling key."""
    assert scopes.needs_step_up(PRESETS["home-assistant"].scopes)
    assert scopes.needs_step_up({"jobs:read:all"})
    assert not scopes.needs_step_up(PRESETS["speech"].scopes)


# ── service principals ───────────────────────────────────────────────────────

def test_service_principals_hold_only_known_scopes_and_nothing_session_only() -> None:
    for name, held in SERVICE_PRINCIPALS.items():
        assert held <= set(SCOPES), name
        assert not held & SESSION_ONLY, name


def test_no_user_role_holds_a_service_only_scope() -> None:
    for role, held in ROLES.items():
        assert not held & SERVICE_ONLY, role


def test_a_principals_sub_is_its_name_under_svc() -> None:
    assert scopes.principal("tts-long") == "svc:tts-long"
    with pytest.raises(ValueError):
        scopes.principal("gateway-relay")


# ── owners (D32) ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["me", "all", "system", "u_mfrggzdfmztwq2lk"])
def test_an_owner_filter_accepts_the_three_words_and_a_user_id(value: str) -> None:
    assert scopes.check_owner_filter(value) == value


@pytest.mark.parametrize("value", ["../x", "u_../../etc", "u_MFRGGZDFMZTWQ2LK",
                                   "u_mfrggzdfmztwq2l", "svc:satellites", "",
                                   "everyone", "u_mfrggzdfmztwq2lk/"])
def test_an_owner_filter_is_refused_before_it_reaches_a_path_join(value: str) -> None:
    with pytest.raises(ValueError):
        scopes.check_owner_filter(value)


@pytest.mark.parametrize(("owner", "system"), [
    (None, True), ("svc:satellites", True), ("u_mfrggzdfmztwq2lk", False)])
def test_no_owner_and_a_service_owner_are_system(owner: object, system: bool) -> None:
    assert scopes.is_system_owner(owner) is system


# ── the import window's allowlists (D66) ─────────────────────────────────────

def test_tts_long_may_import_its_runner_key_and_nothing_else() -> None:
    assert scopes.importable("tts-long", "TTS_RUNNER_API_KEY")
    assert not scopes.importable("tts-long", "SATELLITES_HA_TOKEN")
    assert not scopes.importable("tts-long", "OPENROUTER_API_KEY",
                                 declared=["OPENROUTER_API_KEY"])


def test_the_hub_may_import_its_fixed_names_button_webhooks_and_what_it_declares() -> None:
    assert scopes.importable("satellites", "SATELLITES_HA_TOKEN")
    assert scopes.importable("satellites", "SATELLITES_MQTT_PASSWORD")
    assert scopes.importable("satellites", "SATELLITES_BUTTON_KITCHEN_A_PRESS")
    assert scopes.importable("satellites", "OPENROUTER_API_KEY",
                             declared=["OPENROUTER_API_KEY"])
    assert not scopes.importable("satellites", "OPENROUTER_API_KEY")


@pytest.mark.parametrize("name", ["TTS_RUNNER_API_KEY", "CALLIOPE_MASTER_KEY_FILE",
                                  "GATEWAY_API_KEYS", "RUNLOG_KEY"])
def test_the_hub_cannot_plant_another_services_or_the_gateways_names(name: str) -> None:
    """Declaring a name does not make it the hub's to set (recheck M7)."""
    assert not scopes.importable("satellites", name, declared=[name])


@pytest.mark.parametrize("name", ["lower_case", "1LEADING_DIGIT", "A" * 65,
                                  "WITH-HYPHEN", ""])
def test_a_name_that_is_not_an_environment_name_is_never_importable(name: str) -> None:
    assert not scopes.importable("satellites", name, declared=[name])


def test_an_unknown_service_may_import_nothing() -> None:
    assert not scopes.importable("ui", "SATELLITES_HA_TOKEN")
    assert not scopes.importable("gateway", "TTS_RUNNER_API_KEY")
