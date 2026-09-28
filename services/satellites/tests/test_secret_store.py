"""secrets.json: the API keys the hub holds by name, on its own.

The routes that write it, and the turns that read it, are in
test_wake_assignment.py; these are the file's own promises. A key used here is
built for the test and names nothing real.
"""

from __future__ import annotations

import json
import logging
import stat

import pytest

from app import destinations, secret_store, store
from app.secret_store import FILE, SecretStore

KEY = "sk-test-do-not-leak-9c2e"


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_atomic_with_a_mode_leaves_that_mode_and_no_temporary_file(tmp_path):
    target = tmp_path / "secrets.json"
    store.write_atomic(target, "{}", mode=0o600)
    store.write_atomic(target, '{"a": 1}', mode=0o600)
    assert mode(target) == 0o600 and target.read_text() == '{"a": 1}'
    assert [p.name for p in tmp_path.iterdir()] == ["secrets.json"]


def test_a_key_is_written_0600_read_back_and_the_file_goes_with_the_last_one(tmp_path):
    held = SecretStore(tmp_path)
    held.set("SATELLITES_LLM_API_KEY", KEY)
    assert mode(tmp_path / FILE) == 0o600
    assert json.loads((tmp_path / FILE).read_text()) == {
        "version": 1, "secrets": {"SATELLITES_LLM_API_KEY": KEY}}
    again = SecretStore(tmp_path)
    assert again.get("SATELLITES_LLM_API_KEY") == KEY and again.names() == ["SATELLITES_LLM_API_KEY"]
    again.set("SATELLITES_LLM_API_KEY", None)
    assert not (tmp_path / FILE).exists() and again.names() == []


def test_a_file_readable_by_others_is_tightened_with_a_warning(tmp_path, caplog):
    (tmp_path / FILE).write_text(json.dumps({"version": 1, "secrets": {"OPENAI_API_KEY": KEY}}))
    (tmp_path / FILE).chmod(0o644)
    with caplog.at_level(logging.WARNING):
        held = SecretStore(tmp_path)
    assert mode(tmp_path / FILE) == 0o600 and held.get("OPENAI_API_KEY") == KEY
    assert "is 0600 now" in caplog.text and KEY not in caplog.text


def test_a_file_that_does_not_load_says_so_without_quoting_it(tmp_path, caplog):
    (tmp_path / FILE).write_text('{"version": 1, "secrets": {"OPENAI_API_KEY": "' + KEY)
    with caplog.at_level(logging.ERROR):
        held = SecretStore(tmp_path)
    assert held.names() == [] and "could not be loaded (JSONDecodeError)" in held.load_error
    assert KEY not in caplog.text and KEY not in held.load_error
    # Storing a key replaces the file, as a Save replaces a wake_words.json
    # that did not load.
    held.set("OPENAI_API_KEY", KEY)
    assert held.load_error is None and SecretStore(tmp_path).get("OPENAI_API_KEY") == KEY


def test_an_entry_that_is_not_a_name_and_a_printable_value_is_ignored(tmp_path, caplog):
    (tmp_path / FILE).write_text(json.dumps({"version": 1, "secrets": {
        "OPENAI_API_KEY": KEY, "lower_case": KEY, "NEWLINE_KEY": KEY + "\n", "NUMBER_KEY": 7}}))
    with caplog.at_level(logging.WARNING):
        held = SecretStore(tmp_path)
    assert held.names() == ["OPENAI_API_KEY"]
    assert "NEWLINE_KEY" in caplog.text and KEY not in caplog.text


def test_a_write_that_fails_leaves_the_keys_that_were_in_use(tmp_path, monkeypatch):
    held = SecretStore(tmp_path)
    held.set("OPENAI_API_KEY", KEY)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(secret_store, "write_atomic", full)
    with pytest.raises(OSError):
        held.set("OPENAI_API_KEY", "sk-test-replacement")
    assert held.get("OPENAI_API_KEY") == KEY


def test_without_a_directory_it_holds_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    held = SecretStore()
    held.set("OPENAI_API_KEY", KEY)
    assert held.get("OPENAI_API_KEY") == KEY and list(tmp_path.iterdir()) == []


def test_the_environment_wins_over_a_key_the_hub_holds(monkeypatch):
    secret_store.configure(SecretStore()).set("OPENAI_API_KEY", KEY)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert destinations._secret("OPENAI_API_KEY") == KEY
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-environment")
    assert destinations._secret("OPENAI_API_KEY") == "sk-test-environment"
    # Blanked in a compose file is unset, and the held key is read again.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert destinations._secret("OPENAI_API_KEY") == KEY
    assert destinations._secret(None) is None and destinations._secret("") is None


def test_the_names_and_values_it_takes_are_the_ones_the_routes_take():
    """NAME is destinations.ENV_NAME, restated because destinations imports
    this module; SECRET_VALUE is what main.SecretBody checks a value with."""
    assert secret_store.NAME == destinations.ENV_NAME
    import re
    for good in (KEY, "x", "~" * 4096, "Bearer-like.token_with/every+sort=of!char"):
        assert re.fullmatch(secret_store.SECRET_VALUE, good)
    for bad in ("", "has space", "tab\tin", KEY + "\n", "é", "x" * 4097):
        assert not re.fullmatch(secret_store.SECRET_VALUE, bad)
