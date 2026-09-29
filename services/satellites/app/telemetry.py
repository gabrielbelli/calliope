"""Telemetry: what each turn did, stage by stage, kept on the hub for tuning.

OFF BY DEFAULT. Nothing is collected or written until it is turned on, with
PUT /satellites/telemetry or the Satellites tab; SATELLITES_TELEMETRY only
sets what a hub with no saved choice starts with. The choice is kept in
<data>/telemetry.json, so it survives a restart and an update.

WHAT IS KEPT, one JSON object per line in <data>/telemetry/YYYY-MM-DD.jsonl
(UTC days), each with `kind`:

  turn       one utterance through to its reply: the wake word and its
             score, the command's length and loudness, the language, every
             stage's timing, and `events`: each call the turn made, in order
             (speech-to-text attempts, the language model's rounds and tool
             calls, Home Assistant's pipeline events and its agent's tool
             calls, each text-to-speech call), timed from the end of speech
  wake       a wake word the hub acted on, or chose not to, and why
  near_miss  a wake word's score that rose towards its threshold and fell
             back without firing: the words people say that are not heard
  session    a satellite connecting or going away, with the close code
  device     a satellite's health once a minute while telemetry is on:
             Wi-Fi signal, free memory, dropped audio

LEVELS. `full` keeps what was said and answered, tool arguments and results
(each cut to PREVIEW characters). `timings` keeps everything else and none of
the words: the keys in CONTENT are dropped as a record is written.

Records are written by one background thread, so a turn never waits on the
disk. Files older than `retention_days`, and the oldest beyond `max_mb`, are
deleted. GET /satellites/telemetry/records reads them back and
GET /satellites/telemetry/summary aggregates them (summarise())."""

from __future__ import annotations

import contextvars
import json
import logging
import math
import os
import queue
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("satellites.telemetry")

LEVELS = ("timings", "full")
DEFAULTS = {"enabled": False, "level": "full", "retention_days": 14, "max_mb": 200}
RETENTION_DAYS = (1, 365)
MAX_MB = (10, 5000)
PREVIEW = 400
# What the `timings` level drops, wherever it appears in a record.
CONTENT = frozenset({"transcript", "reply_text", "spoken_text", "text", "said", "speech",
                     "args", "result", "query", "input", "content", "prompt", "reply"})
DEVICE_EVERY_S = 60.0
NEAR_MISS_EVERY_S = 2.0
PRUNE_EVERY = 200     # records between two prunes
FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


def preview(value: object, limit: int = PREVIEW) -> object:
    """A string cut to `limit`, a container as compact JSON cut the same way;
    numbers, booleans and None as they are."""
    if value is None or isinstance(value, bool | int | float):
        return value
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


# ---- a turn's trace -----------------------------------------------------------


class Trace:
    """The calls one turn made, in order, each timed from `origin` (the end
    of speech, time.monotonic()), so a trace reads against timeline_ms."""

    __slots__ = ("events", "origin")

    def __init__(self, origin: float, events: list[dict] | None = None):
        self.origin = origin
        self.events: list[dict] = events if events is not None else []

    def note(self, stage: str, what: str, **data) -> None:
        event = {"t_ms": round((time.monotonic() - self.origin) * 1000, 1),
                 "stage": stage, "what": what}
        event.update((k, v) for k, v in data.items() if v is not None)
        self.events.append(event)


_trace: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("telemetry_trace",
                                                                     default=None)


def begin(origin: float, events: list[dict] | None = None) -> Trace | None:
    """A trace for the turn running in this task, and in the tasks it starts
    (asyncio copies the context into them), writing into `events`, when
    telemetry is on; else None, and note() does nothing."""
    trace = Trace(origin, events) if RECORDER is not None and RECORDER.enabled else None
    _trace.set(trace)
    return trace


def note(stage: str, what: str, **data) -> None:
    trace = _trace.get()
    if trace is not None:
        trace.note(stage, what, **data)


def since(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000, 1)


# ---- the recorder -------------------------------------------------------------


class Recorder:
    def __init__(self, data_dir: Path | str, default: str | None = None):
        self.data_dir = Path(data_dir)
        self.dir = self.data_dir / "telemetry"
        self._settings_file = self.data_dir / "telemetry.json"
        self._settings = dict(DEFAULTS)
        saved = self._load()
        if saved is not None:
            self._settings.update(saved)
        elif default:
            self._settings.update(_from_env(default))
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._written = 0
        self._device_at: dict[str, float] = {}
        self._device_last: dict[str, dict] = {}
        self._near_at: dict[tuple[str, str], float] = {}

    # -- settings --

    @property
    def enabled(self) -> bool:
        return bool(self._settings["enabled"])

    @property
    def level(self) -> str:
        return self._settings["level"]

    def settings(self) -> dict:
        return dict(self._settings)

    def update(self, changes: dict) -> dict:
        """Validated, saved and in force at once. Raises ValueError with the
        sentence for a 422."""
        new = dict(self._settings)
        for key, value in changes.items():
            if value is None:
                continue
            if key == "enabled":
                if not isinstance(value, bool):
                    raise ValueError("enabled must be true or false")
            elif key == "level":
                if value not in LEVELS:
                    raise ValueError(f"level must be one of {', '.join(LEVELS)}")
            elif key == "retention_days":
                if not isinstance(value, int) or isinstance(value, bool) \
                        or not RETENTION_DAYS[0] <= value <= RETENTION_DAYS[1]:
                    raise ValueError(f"retention_days must be a whole number from "
                                     f"{RETENTION_DAYS[0]} to {RETENTION_DAYS[1]}")
            elif key == "max_mb":
                if not isinstance(value, int) or isinstance(value, bool) \
                        or not MAX_MB[0] <= value <= MAX_MB[1]:
                    raise ValueError(f"max_mb must be a whole number from {MAX_MB[0]} to {MAX_MB[1]}")
            else:
                raise ValueError(f"{key} is not a telemetry setting")
            new[key] = value
        was = self.enabled
        self._settings = new
        self._save()
        if new["enabled"] != was:
            log.info("telemetry %s (level %s)", "on" if new["enabled"] else "off", new["level"])
        return self.settings()

    def _load(self) -> dict | None:
        try:
            raw = json.loads(self._settings_file.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("telemetry: %s is unreadable (%s); telemetry stays off",
                        self._settings_file, e)
            return {"enabled": False}
        if not isinstance(raw, dict):
            return {"enabled": False}
        out = {}
        if isinstance(raw.get("enabled"), bool):
            out["enabled"] = raw["enabled"]
        if raw.get("level") in LEVELS:
            out["level"] = raw["level"]
        for key, (lo, hi) in (("retention_days", RETENTION_DAYS), ("max_mb", MAX_MB)):
            v = raw.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                out[key] = v
        return out

    def _save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._settings, indent=1) + "\n")
        os.replace(tmp, self._settings_file)

    # -- writing --

    def write(self, record: dict) -> None:
        """Queue one record, stamped with `at` (UTC), when telemetry is on."""
        if not self.enabled:
            return
        record = {"at": datetime.now(UTC).isoformat(timespec="milliseconds")} | record
        if self.level != "full":
            record = _without_content(record)
        try:
            line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":"))
        except (TypeError, ValueError) as e:
            log.warning("telemetry: a %s record could not be written: %s", record.get("kind"), e)
            return
        self._start()
        self._queue.put(line)

    def _start(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._drain, name="telemetry", daemon=True)
                self._thread.start()

    def _drain(self) -> None:
        """Everything queued, in one append per wake-up; None stops it."""
        while True:
            batch = [self._queue.get()]
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            lines = [line for line in batch if line is not None]
            try:
                if lines:
                    self._append(lines)
            except Exception:
                log.exception("telemetry: writing failed")
            finally:
                for _ in batch:
                    self._queue.task_done()
            if None in batch:
                return

    def _append(self, lines: list[str]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        by_day: dict[str, list[str]] = defaultdict(list)
        for line in lines:
            by_day[line[7:17] if line.startswith('{"at":"') else _today()].append(line)
        for day, rows in by_day.items():
            with open(self.dir / f"{day}.jsonl", "a", encoding="utf-8") as fh:
                fh.write("\n".join(rows) + "\n")
        self._written += len(lines)
        if self._written >= PRUNE_EVERY:
            self._written = 0
            self.prune()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until every queued record is on disk (tests, and a read that
        must see the last turn)."""
        if self._thread is None:
            return
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)

    def prune(self) -> list[str]:
        """Delete the days past retention, then the oldest while the total is
        over max_mb. Today's file is never deleted. The names deleted."""
        files = self.files()
        cutoff = (datetime.now(UTC) - timedelta(days=self._settings["retention_days"])).date()
        gone = []
        for f in files:
            if datetime.fromisoformat(f["date"]).date() < cutoff:
                gone.append(f)
        total = sum(f["bytes"] for f in files if f not in gone)
        cap = self._settings["max_mb"] * 1024 * 1024
        for f in files:
            if total <= cap or f["date"] == _today():
                break
            if f not in gone:
                gone.append(f)
                total -= f["bytes"]
        for f in gone:
            try:
                (self.dir / f"{f['date']}.jsonl").unlink()
            except OSError:
                pass
        return [f["date"] for f in gone]

    def files(self) -> list[dict]:
        """The day files, oldest first, with their sizes."""
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.iterdir()):
            m = FILE.match(p.name)
            if m:
                try:
                    out.append({"date": m.group(1), "bytes": p.stat().st_size})
                except OSError:
                    pass
        return out

    def wipe(self) -> int:
        self.flush()
        n = 0
        for f in self.files():
            try:
                (self.dir / f"{f['date']}.jsonl").unlink()
                n += 1
            except OSError:
                pass
        return n

    def status(self) -> dict:
        files = self.files()
        return self.settings() | {"files": files, "bytes": sum(f["bytes"] for f in files)}

    # -- reading --

    def read(self, *, since: datetime | None = None, until: datetime | None = None,
             kinds: set[str] | None = None, satellite: str | None = None,
             word: str | None = None, limit: int = 500) -> list[dict]:
        """The newest `limit` records that match, oldest first."""
        self.flush()
        found: list[dict] = []
        for f in reversed(self.files()):
            day = datetime.fromisoformat(f["date"]).replace(tzinfo=UTC)
            if since is not None and day + timedelta(days=1) <= since:
                break
            if until is not None and day > until:
                continue
            try:
                lines = (self.dir / f"{f['date']}.jsonl").read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue  # a line cut short by a crash
                at = _at(r)
                if since is not None and at is not None and at < since:
                    continue
                if until is not None and at is not None and at > until:
                    continue
                if kinds and r.get("kind") not in kinds:
                    continue
                if satellite and r.get("satellite") != satellite:
                    continue
                if word and r.get("word") != word:
                    continue
                found.append(r)
                if len(found) >= limit:
                    return found[::-1]
        return found[::-1]

    # -- what the hub reports between turns --

    def device(self, satellite: str, status: dict) -> None:
        """A satellite's health, at most once a DEVICE_EVERY_S, with the audio
        it dropped since the last one."""
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self._device_at.get(satellite, -math.inf) < DEVICE_EVERY_S:
            return
        self._device_at[satellite] = now
        last = self._device_last.get(satellite, {})
        rec = {"kind": "device", "satellite": satellite}
        for key in ("uptime_s", "rssi", "heap", "psram", "spk_buffered_ms", "muted", "volume",
                    "mic_gain_db"):
            if key in status:
                rec[key] = status[key]
        for key in ("mic_dropped", "spk_dropped"):
            if isinstance(status.get(key), int):
                was = last.get(key)
                rec[key] = status[key]
                if isinstance(was, int) and status[key] >= was:
                    rec[key + "_since"] = status[key] - was
        self._device_last[satellite] = {k: status.get(k) for k in ("mic_dropped", "spk_dropped")}
        self.write(rec)

    def near_miss(self, satellite: str, word: str, peak: float, threshold: float | None) -> None:
        if not self.enabled:
            return
        key, now = (satellite, word), time.monotonic()
        if now - self._near_at.get(key, -math.inf) < NEAR_MISS_EVERY_S:
            return
        self._near_at[key] = now
        self.write({"kind": "near_miss", "satellite": satellite, "word": word,
                    "peak": round(peak, 3), "threshold": threshold})


RECORDER: Recorder | None = None


def install(recorder: Recorder | None) -> None:
    global RECORDER
    RECORDER = recorder


def _from_env(value: str) -> dict:
    v = value.strip().lower()
    if v in ("", "0", "off", "false", "no"):
        return {"enabled": False}
    if v in LEVELS:
        return {"enabled": True, "level": v}
    if v in ("1", "on", "true", "yes"):
        return {"enabled": True}
    log.warning("SATELLITES_TELEMETRY=%r is not off, on, timings or full; telemetry stays off", value)
    return {"enabled": False}


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _at(r: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(r["at"])
    except (KeyError, TypeError, ValueError):
        return None


def _without_content(value: object) -> object:
    if isinstance(value, dict):
        return {k: _without_content(v) for k, v in value.items() if k not in CONTENT}
    if isinstance(value, list):
        return [_without_content(v) for v in value]
    return value


# ---- the summary --------------------------------------------------------------

TIMELINE = ("stt_done", "first_token", "answer_done", "first_audio", "reply_done")
STAGES = ("stt", "pipeline", "destination", "tts", "total")


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, max(0, math.ceil(q * len(v)) - 1))], 1)


def _spread(values: list[float]) -> dict | None:
    if not values:
        return None
    return {"n": len(values), "p50": _pct(values, 0.5), "p90": _pct(values, 0.9),
            "max": round(max(values), 1)}


def _kind_of_error(error: str) -> str:
    """"destination: no answer within 15 s" and "destination: Home Assistant
    ..." are counted apart; the numbers in a message are not."""
    return re.sub(r"\d+(\.\d+)?", "N", error)[:90]


def summarise(records: list[dict], hours: float) -> dict:
    """What the records say, per wake word and per stage: how often each
    worked, how long each stage takes (p50, p90, max, in ms), which calls a
    turn makes and how they fare, and how the wake words and satellites
    behave between turns. `bottleneck` names the stage with the largest
    median share of a word's time to first audio."""
    every = [r for r in records if r.get("kind") == "turn"]
    # A clip sent through /inject is a test, not something said in the room.
    turns = [r for r in every if r.get("trigger") != "inject"]
    words: dict[str, dict] = {}
    for word in sorted({r.get("word") or "?" for r in turns}):
        mine = [r for r in turns if (r.get("word") or "?") == word]
        ok = [r for r in mine if not r.get("error")]
        errors = Counter(_kind_of_error(r["error"]) for r in mine if r.get("error"))
        timeline = {k: _spread([r["timeline_ms"][k] for r in mine
                                if isinstance((r.get("timeline_ms") or {}).get(k), int | float)])
                    for k in TIMELINE}
        stages = {k: _spread([r["timings_ms"][k] for r in mine
                              if isinstance((r.get("timings_ms") or {}).get(k), int | float)])
                  for k in STAGES}
        medians = {k: v["p50"] for k, v in stages.items() if v and k in ("stt", "destination", "tts")}
        words[word] = {
            "turns": len(mine), "ok": len(ok),
            "actions": dict(Counter(r.get("action") or "?" for r in mine)),
            "errors": dict(errors.most_common(8)),
            "empty_after_wake": sum(1 for r in mine if (r.get("error") or "").startswith("nothing was said")),
            "interrupted": sum(1 for r in mine if r.get("interrupted")),
            "handed_over": sum(1 for r in mine if r.get("handed_over_to")),
            "languages": dict(Counter(r.get("language") or "?" for r in mine)),
            "command_s": _spread([r["command"]["seconds"] for r in mine
                                  if isinstance((r.get("command") or {}).get("seconds"), int | float)]),
            "command_dbfs": _spread([r["command"]["rms_dbfs"] for r in mine
                                     if isinstance((r.get("command") or {}).get("rms_dbfs"), int | float)]),
            "timeline_ms": {k: v for k, v in timeline.items() if v},
            "stages_ms": {k: v for k, v in stages.items() if v},
            "bottleneck": max(medians, key=medians.get) if medians else None,
        }

    events = [e for r in turns for e in (r.get("events") or [])]

    def calls(stage: str, what: str) -> list[dict]:
        return [e for e in events if e.get("stage") == stage and e.get("what") == what]

    stt = defaultdict(list)
    for e in calls("stt", "request"):
        stt[e.get("engine") or "?"].append(e)
    llm_rounds = calls("llm", "round")
    tools = defaultdict(list)
    for e in calls("tool", "run"):
        tools[e.get("name") or "?"].append(e)
    ha_tools = Counter(e.get("name") or "?" for e in calls("ha", "tool_call"))
    ha_results = [e for e in calls("ha", "tool_result")]
    ha_end = calls("ha", "intent_end")
    tts = defaultdict(list)
    for e in calls("tts", "synth"):
        tts[e.get("engine") or "?"].append(e)

    wakes = [r for r in records if r.get("kind") == "wake"]
    near = [r for r in records if r.get("kind") == "near_miss"]
    wake_words = {}
    for word in sorted({r.get("word") or "?" for r in wakes + near}):
        heard = [r for r in wakes if r.get("word") == word]
        missed = [r for r in near if r.get("word") == word]
        wake_words[word] = {
            "heard": len(heard),
            "decisions": dict(Counter(r.get("decision") or "?" for r in heard)),
            "score": _spread([r["score"] for r in heard if isinstance(r.get("score"), int | float)]),
            "threshold": next((r.get("threshold") for r in reversed(heard + missed)
                               if r.get("threshold") is not None), None),
            "near_misses": len(missed),
            "near_miss_peak": _spread([r["peak"] for r in missed if isinstance(r.get("peak"), int | float)]),
        }

    satellites = {}
    for sid in sorted({r.get("satellite") for r in records if r.get("satellite")}):
        dev = [r for r in records if r.get("kind") == "device" and r.get("satellite") == sid]
        ses = [r for r in records if r.get("kind") == "session" and r.get("satellite") == sid]
        satellites[sid] = {
            "turns": sum(1 for r in turns if r.get("satellite") == sid),
            "rssi": _spread([r["rssi"] for r in dev if isinstance(r.get("rssi"), int | float)]),
            "heap_min": min((r["heap"] for r in dev if isinstance(r.get("heap"), int)), default=None),
            "mic_dropped": sum(r.get("mic_dropped_since", 0) for r in dev),
            "spk_dropped": sum(r.get("spk_dropped_since", 0) for r in dev),
            "connects": sum(1 for r in ses if r.get("event") == "connected"),
            "disconnects": dict(Counter(str(r.get("close_code")) for r in ses
                                        if r.get("event") == "disconnected")),
        }

    return {
        "hours": hours, "records": len(records), "turns": len(turns),
        "injected_turns": len(every) - len(turns),
        "words": words,
        "stt": {engine: {"requests": len(es), "ms": _spread([e["ms"] for e in es if "ms" in e]),
                         "audio_s": _spread([e["audio_s"] for e in es if "audio_s" in e]),
                         "retries": sum(1 for e in es if e.get("retry")),
                         "statuses": dict(Counter(str(e.get("status")) for e in es))}
                for engine, es in stt.items()},
        "llm": {"rounds": len(llm_rounds),
                "rounds_per_turn": _spread([float(sum(1 for e in (r.get("events") or [])
                                                      if e.get("stage") == "llm" and e.get("what") == "round"))
                                            for r in turns if any(e.get("stage") == "llm"
                                                                  for e in (r.get("events") or []))]),
                "first_token_ms": _spread([e["first_token_ms"] for e in llm_rounds if "first_token_ms" in e]),
                "round_ms": _spread([e["ms"] for e in llm_rounds if "ms" in e]),
                "models": dict(Counter(e.get("model") or "?" for e in llm_rounds)),
                "finish": dict(Counter(e.get("finish") or "?" for e in llm_rounds)),
                "tokens": {k: sum(e.get(k) or 0 for e in llm_rounds)
                           for k in ("prompt_tokens", "completion_tokens")}},
        "tools": {name: {"calls": len(es), "failed": sum(1 for e in es if not e.get("ok", True)),
                         "ms": _spread([e["ms"] for e in es if "ms" in e])}
                  for name, es in tools.items()},
        "home_assistant": {"tool_calls": dict(ha_tools),
                           "tool_errors": sum(1 for e in ha_results if e.get("error")),
                           "response_types": dict(Counter(e.get("response_type") or "?" for e in ha_end)),
                           "processed_locally": sum(1 for e in ha_end if e.get("processed_locally")),
                           "failed_targets": sum(e.get("failed") or 0 for e in ha_end),
                           "intent_ms": _spread([e["intent_ms"] for e in ha_end if "intent_ms" in e])},
        "tts": {engine: {"calls": len(es), "ms": _spread([e["ms"] for e in es if "ms" in e]),
                         "ms_per_audio_s": _spread([e["ms"] / e["audio_s"] for e in es
                                                    if e.get("audio_s") and "ms" in e])}
                for engine, es in tts.items()},
        "wake_words": wake_words,
        "satellites": satellites,
    }
