"""GET /health in three tiers, each built from an allowlist, never a pass-through (D50).

    anonymous        {"status": "ok" | "degraded"}                 liveness only
    health:read      + each backend's status, engines, models, preset voices,
                       shared glossary names and queue depth
    health:detail    + the runner, MQTT, satellite topology, thread counts and
                       the variables a backend ignores

**Fields are named, not filtered out.** A backend's body is never copied
through: each tier keeps only the keys listed below for that backend, one
level down where a value is a list of rows. A field a backend adds tomorrow
therefore appears in neither tier until somebody decides which tier it
belongs to, and topology stops being public by default.

**Probes are cached for 5 seconds and shared while in flight**, so an
anonymous caller cannot turn one request here into four backend probes:
ten calls in a second cost each backend one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from . import db as dbmod

CACHE_SECONDS = 5.0

# health:read. The page's speech tabs read these: engine lists, voices,
# languages, readiness and the realtime factors its estimates come from.
_STT_MODEL_ROW = frozenset({"id", "family", "default", "languages", "accepts_language",
                            "accepts_boost", "can_translate", "can_stream"})
# loudness_lufs is not published yet, but the page's quieter-engine note reads
# it (services/ui/tests/test_engine_picker.py, UNPUBLISHED): a number about an
# engine, so it belongs in this tier the day tts-long sends it.
_ENGINE_ROW = frozenset({"label", "default", "languages", "controls",
                         "min_reference_seconds", "cold_load_seconds", "reference_audio",
                         "language_from_voice", "voices", "native_sample_rate",
                         "loudness_lufs"})
_VOICE_ROW = frozenset({"name", "language"})
# stt lists its reserved profile with the shared ones, and the hub needs to see
# it there; a `user` or `user-jobs` account never does (D34), so it is named
# only to a caller who may use it, with the scopes stt itself checks.
RESERVED_GLOSSARY = "home-assistant"
RESERVED_GLOSSARY_SCOPES = frozenset({"glossaries:ha", "glossaries:read:all",
                                      "glossaries:write:all"})

FILTERED: Mapping[str, frozenset[str]] = {
    "stt": frozenset({"status", "model", "models", "accepts_vocabulary", "translations",
                      "streaming", "hotwords", "glossaries", "vad"}),
    "tts": frozenset({"status", "voices", "default_voice", "realtime_factor"}),
    "tts_long": frozenset({"status", "model_loaded", "queued", "queue_capacity", "running",
                           "default_engine", "engines", "realtime_factor",
                           "realtime_factor_by_backend", "realtime_factor_by_engine"}),
    "satellites": frozenset({"status"}),
}

# health:detail adds these. `ignored_variables` is a list of NAMES (voice_common.auth).
DETAIL: Mapping[str, frozenset[str]] = {
    "stt": FILTERED["stt"] | {"model_id", "threads", "max_concurrent", "host_label",
                              "runlog", "ignored_variables"},
    "tts": FILTERED["tts"] | {"threads", "host_label", "runlog", "realtime_factor_samples",
                              "ignored_variables"},
    # `runners` is every GPU runner, one row each; `runner` is the first alone,
    # kept for a page or a script written while there was one. Both are the
    # operator's: what each machine is doing is detail, not a tab's business.
    "tts_long": FILTERED["tts_long"] | {"threads", "backend_observations",
                                        "engine_observations", "backend_order", "runner",
                                        "runners", "dispatch", "host_label",
                                        "ignored_variables"},
    "satellites": FILTERED["satellites"] | {"satellites", "tts", "voice", "routing", "mqtt",
                                            "ignored_variables"},
}


def _rows(value: Any, keys: frozenset[str]) -> Any:
    if isinstance(value, list):
        return [{k: v for k, v in row.items() if k in keys}
                for row in value if isinstance(row, dict)]
    return None


def _engines(value: Any, *, detail: bool) -> Any:
    if not isinstance(value, dict):
        return None
    shown: dict[str, Any] = {}
    for name, row in value.items():
        if not isinstance(row, dict):
            continue
        kept = {k: v for k, v in row.items() if k in _ENGINE_ROW}
        if "voices" in kept:
            kept["voices"] = _rows(kept["voices"], _VOICE_ROW)
        for lane in ("local", "runner"):
            lane_value = row.get(lane)
            if isinstance(lane_value, dict):
                # Readiness is what a tab needs; where the runner is and how
                # it is set up is the operator's.
                kept[lane] = dict(lane_value) if detail else {
                    k: lane_value[k] for k in ("ready", "resident") if k in lane_value}
        shown[str(name)] = kept
    return shown


def view(backend: str, body: Any, *, detail: bool,
         scopes: frozenset[str] = frozenset()) -> dict[str, Any]:
    """One backend's health body, cut down to a tier's allowlist."""
    if not isinstance(body, dict):
        return {}
    allowed = (DETAIL if detail else FILTERED).get(backend, frozenset({"status"}))
    shown = {key: value for key, value in body.items() if key in allowed}
    if "models" in shown and backend == "stt":
        shown["models"] = _rows(shown["models"], _STT_MODEL_ROW)
    if (backend == "stt" and isinstance(shown.get("glossaries"), list)
            and not scopes & RESERVED_GLOSSARY_SCOPES):
        shown["glossaries"] = [name for name in shown["glossaries"]
                               if name != RESERVED_GLOSSARY]
    if "engines" in shown:
        shown["engines"] = _engines(shown["engines"], detail=detail)
    return shown


class ProbeCache:
    """The last probe of every backend, for 5 s, with concurrent callers sharing one."""

    def __init__(self, probe: Callable[[], Awaitable[dict[str, dict[str, Any]]]]) -> None:
        self.probe = probe
        self._value: dict[str, dict[str, Any]] | None = None
        self._at = float("-inf")
        self._inflight: asyncio.Future | None = None

    async def get(self) -> dict[str, dict[str, Any]]:
        if self._value is not None and dbmod.now() - self._at < CACHE_SECONDS:
            return self._value
        if self._inflight is None:
            self._inflight = asyncio.ensure_future(self.probe())
            try:
                self._value = await asyncio.shield(self._inflight)
                self._at = dbmod.now()
            finally:
                self._inflight = None
            return self._value
        return await asyncio.shield(self._inflight)


def body(probes: dict[str, dict[str, Any]], *, scopes: frozenset[str],
         locked: list[tuple[str, str | None]]) -> dict[str, Any]:
    """The /health answer for a caller holding `scopes`."""
    healthy = not locked and all(p.get("reachable") for p in probes.values())
    answer: dict[str, Any] = {"status": "ok" if healthy else "degraded"}
    detail = "health:detail" in scopes
    if not detail and "health:read" not in scopes:
        return answer
    backends: dict[str, Any] = {}
    for name, probe in probes.items():
        entry: dict[str, Any] = {"reachable": bool(probe.get("reachable"))}
        if detail:
            entry.update({k: probe[k] for k in ("url", "http_status", "error") if k in probe})
        entry["health"] = view(name, probe.get("health"), detail=detail, scopes=scopes)
        backends[name] = entry
    answer["backends"] = backends
    if detail:
        answer["gateway"] = {"locked": [{"reason": r, "variable": v} for r, v in locked]}
    return answer
