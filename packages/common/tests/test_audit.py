"""One audit row, one stdout line, the same columns as the gateway's table."""

from __future__ import annotations

import json

import pytest

from voice_common import audit


def row(**overrides: object) -> dict:
    base = {"ts": "2026-10-01T12:00:00Z", "actor_kind": "anonymous",
            "action": "assertion_rejected", "outcome": "denied"}
    base.update(overrides)
    return base


def test_a_row_is_one_line_carrying_every_column() -> None:
    text = audit.line(row(ip="10.0.0.1"))
    assert text.startswith("audit {") and "\n" not in text
    parsed = json.loads(text[len("audit "):])
    assert list(parsed) == list(audit.FIELDS)
    assert parsed["ip"] == "10.0.0.1" and parsed["actor_id"] is None
    assert parsed["aggregated"] == 0


def test_a_hostile_string_cannot_start_a_line_of_its_own() -> None:
    """An attempted username is internet input; CR and LF must stay escaped."""
    forged = 'admin\naudit {"action":"login","outcome":"ok"}'
    text = audit.line(row(target=forged, detail={"paths": ["/\r\nx"]}))
    assert "\n" not in text and "\r" not in text
    assert json.loads(text[len("audit "):])["target"] == forged


@pytest.mark.parametrize("bad", [
    row(token="v1.1.a.b"), row(actor_kind="root"), row(outcome="maybe"),
    row(action=""), row(ts=None)])
def test_a_row_the_table_would_refuse_is_refused_here_too(bad: dict) -> None:
    with pytest.raises(ValueError):
        audit.line(bad)


def test_emit_writes_one_flushed_line_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    audit.emit(row(aggregated=True))
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and json.loads(out[6:])["aggregated"] == 1


def test_timestamps_are_utc_to_the_second() -> None:
    assert audit.timestamp(0) == "1970-01-01T00:00:00Z"
