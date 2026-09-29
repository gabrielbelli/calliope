"""Telemetry: off until turned on, what it keeps, and what a turn's trace says.

The recorder writes to a temporary directory; the language model, SearXNG and
Home Assistant are fakes (test_llm.py, test_tools.py). The whole path through
a satellite is in test_pipeline.py.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pytest
from test_llm import LLM, ask, delta, sse
from test_router import Fake

from app import telemetry, tools, wakeword
from app.destinations import HaAssist, Llm


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("SATELLITES_SEARXNG_URL", "http://searx.test")
    for name in ("SATELLITES_HOME_LAT", "SATELLITES_HOME_LON", "SATELLITES_TIMEZONE", "TZ",
                 "SATELLITES_LLM_API_KEY", "SATELLITES_UNITS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tools, "_home", None)
    monkeypatch.setattr(tools, "_home_at", None)
    monkeypatch.setattr(Llm, "limit_names", {})
    yield
    telemetry.install(None)


@pytest.fixture
def rec(tmp_path) -> telemetry.Recorder:
    r = telemetry.Recorder(tmp_path)
    telemetry.install(r)
    return r


def lines(rec: telemetry.Recorder) -> list[dict]:
    rec.flush()
    out = []
    for f in rec.files():
        out += [json.loads(x) for x in (rec.dir / f"{f['date']}.jsonl").read_text().splitlines()]
    return out


# ---- off by default ---------------------------------------------------------------


def test_a_new_hub_records_nothing_and_writes_no_file(rec, tmp_path):
    assert rec.enabled is False
    rec.write({"kind": "turn", "word": "alexa"})
    rec.device("k", {"rssi": -60})
    rec.near_miss("k", "alexa", 0.3, 0.5)
    assert telemetry.begin(time.monotonic()) is None
    telemetry.note("stt", "request", ms=1)  # nothing to note into, and no error
    assert rec.files() == [] and not (tmp_path / "telemetry.json").exists()


@pytest.mark.parametrize("env,on,level", [("off", False, "full"), ("on", True, "full"),
                                          ("full", True, "full"), ("timings", True, "timings"),
                                          ("nonsense", False, "full")])
def test_the_environment_only_sets_where_a_hub_with_no_saved_choice_starts(tmp_path, env, on, level):
    r = telemetry.Recorder(tmp_path, env)
    assert (r.enabled, r.level) == (on, level)


def test_a_saved_choice_wins_over_the_environment_after_a_restart(tmp_path):
    telemetry.Recorder(tmp_path).update({"enabled": False})
    assert telemetry.Recorder(tmp_path, "full").enabled is False
    telemetry.Recorder(tmp_path).update({"enabled": True, "level": "timings", "retention_days": 3})
    again = telemetry.Recorder(tmp_path, "off")
    assert (again.enabled, again.level, again.settings()["retention_days"]) == (True, "timings", 3)


@pytest.mark.parametrize("changes", [{"enabled": "yes"}, {"level": "everything"},
                                     {"retention_days": 0}, {"retention_days": 1.5},
                                     {"max_mb": 1}, {"colour": "blue"}])
def test_a_setting_out_of_range_is_refused_and_nothing_changes(rec, changes):
    before = rec.settings()
    with pytest.raises(ValueError):
        rec.update(changes)
    assert rec.settings() == before


# ---- writing and reading ------------------------------------------------------------


def test_records_come_back_filtered_and_oldest_first(rec):
    rec.update({"enabled": True})
    for i in range(5):
        rec.write({"kind": "turn", "satellite": "k", "word": "alexa" if i % 2 else "lumos", "i": i})
    rec.write({"kind": "wake", "satellite": "b", "word": "alexa"})
    assert [r["i"] for r in rec.read(kinds={"turn"})] == [0, 1, 2, 3, 4]
    assert [r["i"] for r in rec.read(word="alexa", kinds={"turn"})] == [1, 3]
    assert [r["i"] for r in rec.read(kinds={"turn"}, limit=2)] == [3, 4]
    assert [r["kind"] for r in rec.read(satellite="b")] == ["wake"]
    assert all(datetime.fromisoformat(r["at"]).tzinfo for r in rec.read())


def test_the_timings_level_keeps_every_number_and_none_of_the_words(rec):
    rec.update({"enabled": True, "level": "timings"})
    rec.write({"kind": "turn", "transcript": "turn on the kitchen", "reply_text": "Done.",
               "timings_ms": {"stt": 300}, "events": [
                   {"stage": "tool", "what": "run", "ms": 900, "args": '{"query": "x"}',
                   "result": "snippets"},
                   {"stage": "ha", "what": "intent_end", "speech": "Done.", "success": 1}]})
    [r] = lines(rec)
    assert "transcript" not in r and "reply_text" not in r
    assert r["timings_ms"] == {"stt": 300}
    assert r["events"] == [{"stage": "tool", "what": "run", "ms": 900},
                           {"stage": "ha", "what": "intent_end", "success": 1}]


def test_a_line_cut_short_by_a_crash_is_skipped(rec):
    rec.update({"enabled": True})
    rec.write({"kind": "turn", "i": 1})
    rec.flush()
    [f] = rec.files()
    with open(rec.dir / f"{f['date']}.jsonl", "a") as fh:
        fh.write('{"at":"2026-09-29T10:00:00+00:00","kind":"tu')
    assert [r["i"] for r in rec.read()] == [1]


def test_old_days_and_the_oldest_beyond_the_size_cap_are_deleted_but_never_today(rec):
    rec.update({"enabled": True, "retention_days": 7, "max_mb": 10})
    rec.dir.mkdir(parents=True)
    today = datetime.now(UTC).date()
    old = (today - timedelta(days=30)).isoformat()
    recent = (today - timedelta(days=2)).isoformat()
    (rec.dir / f"{old}.jsonl").write_text("{}\n")
    (rec.dir / f"{recent}.jsonl").write_bytes(b"x" * (6 * 1024 * 1024))
    (rec.dir / f"{today.isoformat()}.jsonl").write_bytes(b"x" * (6 * 1024 * 1024))
    assert sorted(rec.prune()) == sorted([old, recent])
    assert [f["date"] for f in rec.files()] == [today.isoformat()]


def test_a_satellites_health_is_kept_once_a_minute_with_what_it_dropped_since(rec):
    rec.update({"enabled": True})
    rec.device("k", {"rssi": -61, "heap": 90000, "mic_dropped": 10, "spk_dropped": 0, "volume": 5})
    rec.device("k", {"rssi": -70, "mic_dropped": 12})  # too soon: not kept
    rec._device_at["k"] -= telemetry.DEVICE_EVERY_S
    rec.device("k", {"rssi": -64, "heap": 88000, "mic_dropped": 14, "spk_dropped": 1})
    first, second = [r for r in lines(rec) if r["kind"] == "device"]
    assert first["rssi"] == -61 and "mic_dropped_since" not in first
    assert (second["rssi"], second["mic_dropped_since"], second["spk_dropped_since"]) == (-64, 4, 1)


# ---- what a turn's trace says --------------------------------------------------------


async def test_a_language_model_turn_notes_each_round_and_each_tool_call(rec):
    rec.update({"enabled": True})
    fake = Fake()
    fake.handlers["searx.test"] = lambda r: httpx.Response(200, json={
        "results": [{"title": "Race", "content": "Norris won."}], "answers": [], "infoboxes": []})

    def model(r: httpx.Request) -> httpx.Response:
        if not any(m.get("role") == "tool" for m in json.loads(r.content)["messages"]):
            return sse(delta("tool_calls", tool_calls=[{"index": 0, "id": "c1", "type": "function",
                                                        "function": {"name": "web_search",
                                                                     "arguments": '{"query": "gp"}'}}]))
        return sse(delta(content="Norris "), delta("stop", content="won."),
                   {"choices": [], "usage": {"prompt_tokens": 812, "completion_tokens": 4}})
    fake.handlers["llm.test"] = model
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    events: list[dict] = []
    telemetry.begin(time.monotonic(), events)

    llm = Llm.model_validate(LLM | {"tools": ["web_search"]})
    assert "".join([p async for p in llm.answer(client, ask("who won"))]) == "Norris won."

    rounds = [e for e in events if e["stage"] == "llm"]
    assert [(e["n"], e.get("tool_calls"), e.get("finish")) for e in rounds] == [
        (0, ["web_search"], "tool_calls"), (1, None, "stop")]
    assert rounds[1]["prompt_tokens"] == 812 and rounds[1]["text"] == "Norris won."
    assert rounds[1]["first_token_ms"] <= rounds[1]["ms"]
    [search] = [e for e in events if e["stage"] == "tool"]
    assert (search["name"], search["ok"], search["args"]) == ("web_search", True, '{"query": "gp"}')
    assert "Norris won." in search["result"]
    assert [e["stage"] for e in events] == ["llm", "tool", "llm"]


class FakeSocket:
    def __init__(self, *messages: dict):
        self.messages = [json.dumps(m) for m in messages]

    async def recv(self) -> str:
        return self.messages.pop(0)


def event(kind: str, **data) -> dict:
    return {"id": 2, "type": "event", "event": {"type": kind, "data": data}}


async def test_an_assist_run_notes_the_agents_tool_calls_their_results_and_the_answer(rec):
    """An agent that calls light.turn_on with only entity_id and says it set
    cool white: each call, its arguments and HA's answer are in the trace,
    so the claim can be checked against what was sent."""
    rec.update({"enabled": True})
    events: list[dict] = []
    telemetry.begin(time.monotonic(), events)
    ha = HaAssist.model_validate({"type": "ha_assist", "url": "https://ha.test",
                                  "token_env": "SATELLITES_HA_TOKEN"})
    ws = FakeSocket(
        {"id": 2, "type": "result", "success": True},
        event("intent-start", engine="conversation.grok", language="en", device_id="d1",
              prefer_local_intents=True),
        event("intent-progress", chat_log_delta={"tool_calls": [
            {"tool_name": "execute_services", "tool_args": {"list": [
                {"domain": "light", "service": "turn_on",
                 "service_data": {"entity_id": ["light.desk"]}}]}}]}),
        event("intent-progress", chat_log_delta={"role": "tool_result", "tool_name": "execute_services",
                                                 "tool_result": {"success": True}}),
        event("intent-progress", chat_log_delta={"content": "The"}),
        event("intent-progress", chat_log_delta={"content": " desk"}),
        event("intent-end", processed_locally=False, intent_output={
            "conversation_id": "c1", "response": {
                "response_type": "action_done", "speech": {"plain": {"speech": "The desk is cool white."}},
                "data": {"success": [], "failed": []}}}))

    assert await ha._run(ws, ask("set the lights to cool light")) == "The desk is cool white."
    kinds = [e["what"] for e in events]
    assert kinds == ["intent_start", "tool_call", "tool_result", "first_text", "intent_end"]
    call = events[1]
    assert call["name"] == "execute_services" and '"entity_id": ["light.desk"]' in call["args"]
    end = events[-1]
    assert (end["response_type"], end["success"], end["failed"], end["processed_locally"]) == (
        "action_done", 0, 0, False)
    assert end["speech"] == "The desk is cool white." and "intent_ms" in end


# ---- near misses ------------------------------------------------------------------


class Scores:
    def __init__(self, seq):
        self.seq = list(seq)

    def predict(self, x):
        return {"w": self.seq.pop(0)}

    def reset(self):
        pass


def detector(scores) -> wakeword.WakeWords:
    ww = wakeword.WakeWords.__new__(wakeword.WakeWords)
    ww._model, ww._keys, ww.thresholds = Scores(scores), {"w": "w"}, {"w": 0.5}
    ww._warmup, ww._refractory = 0, wakeword.FRAME * 3
    ww.reset()
    return ww


def test_a_score_that_rises_towards_the_threshold_and_falls_back_is_a_near_miss():
    ww = detector([0.1, 0.3, 0.42, 0.2, 0.1, 0.4, 0.6, 0.1])
    found = ww.feed(np.zeros(wakeword.FRAME * 8, dtype=np.int16))
    # The rise to 0.42 fell back: a near miss. The rise to 0.4 went on to
    # fire at 0.6: a detection, and no near miss.
    assert [d.name for d in found] == ["w"]
    assert ww.take_near_misses() == [("w", 0.42)]
    assert ww.take_near_misses() == []


def test_near_misses_nobody_takes_do_not_grow_without_bound():
    ww = detector([0.3, 0.1] * 50)
    ww.feed(np.zeros(wakeword.FRAME * 100, dtype=np.int16))
    assert len(ww.near_misses) == wakeword.NEAR_MISS_KEPT


# ---- the summary ------------------------------------------------------------------


def turn(word, first_audio, stt, dest, tts, error=None, **more) -> dict:
    return {"kind": "turn", "satellite": "k", "word": word, "action": "llm", "error": error,
            "timeline_ms": {"first_audio": first_audio}, "language": "en",
            "timings_ms": {"stt": stt, "destination": dest, "tts": tts}, **more}


def test_the_summary_names_each_words_slowest_stage_and_its_errors():
    records = [
        turn("hey_jarvis", 12800, 300, 9300, 1500),
        turn("hey_jarvis", 9000, 350, 6000, 1400),
        turn("hey_jarvis", 0, 300, 15000, 0, error="destination: no answer within 15 s"),
        turn("alexa", 2500, 400, 900, 700, events=[
            {"stage": "stt", "what": "request", "engine": "parakeet", "ms": 380, "audio_s": 2.1,
             "status": 400, "boost": True},
            {"stage": "stt", "what": "request", "engine": "parakeet", "ms": 360, "audio_s": 2.1,
             "status": 200, "retry": "boost_refused"},
            {"stage": "ha", "what": "tool_call", "name": "execute_services"},
            {"stage": "ha", "what": "intent_end", "response_type": "action_done", "failed": 1,
             "intent_ms": 800}]),
        {"kind": "wake", "satellite": "k", "word": "alexa", "score": 0.7, "threshold": 0.4,
         "decision": "started"},
        {"kind": "wake", "satellite": "k", "word": "alexa", "score": 0.5, "threshold": 0.4,
         "decision": "ignored_busy"},
        {"kind": "near_miss", "satellite": "k", "word": "lumos", "peak": 0.31, "threshold": 0.5},
        {"kind": "device", "satellite": "k", "rssi": -67, "heap": 81000, "mic_dropped_since": 3},
        {"kind": "session", "satellite": "k", "event": "disconnected", "close_code": 1006},
    ]
    s = telemetry.summarise(records, 24)
    jarvis = s["words"]["hey_jarvis"]
    assert (jarvis["turns"], jarvis["ok"], jarvis["bottleneck"]) == (3, 2, "destination")
    assert jarvis["errors"] == {"destination: no answer within N s": 1}
    assert jarvis["stages_ms"]["destination"]["p50"] == 9300
    assert s["stt"]["parakeet"]["retries"] == 1 and s["stt"]["parakeet"]["statuses"] == {"400": 1, "200": 1}
    assert s["home_assistant"]["tool_calls"] == {"execute_services": 1}
    assert s["home_assistant"]["failed_targets"] == 1
    assert s["wake_words"]["alexa"]["decisions"] == {"started": 1, "ignored_busy": 1}
    assert s["wake_words"]["lumos"]["near_misses"] == 1
    assert s["satellites"]["k"]["mic_dropped"] == 3
    assert s["satellites"]["k"]["disconnects"] == {"1006": 1}
