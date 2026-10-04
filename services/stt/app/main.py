"""Self-hosted speech-to-text. One container, one model.

    audio -> VAD -> recogniser -> glossary repair -> text

Parakeet by default, Whisper by request. There is no LLM cleanup stage and no
second recogniser; both were tried and measured, and both made the transcript
worse. The reasoning is in asr.py and in the README.

Three routes reach the same pipeline. /transcribe is native and returns
everything the run measured; /v1/audio/transcriptions and
/v1/audio/translations are OpenAI-compatible and return the subset that
specification has fields for, so existing clients work unchanged. Prefer the
native one where you control the client — see openai_api.py for what the
compatible shape has to drop, and for the one rule that surface is built
around: every field is honoured or refused by name, never accepted and
dropped.

The wire contract around those routes — the gateway's identity assertion and
the 401 without one, OpenAI's error envelope, the health route and the log
configuration — comes from voice_common, which this service shares with
tts-stack and tts-long. Three hand-vendored copies of that code had drifted
into three different defects; the package docstrings carry the detail.
app/errors.py is gone with them: it completed the envelope with `param` and
the 404/405 handlers while voice-common was pinned by tarball SHA and could
not be changed from here, and all of it now lives in voice_common.errors.
Everything below this line is what is genuinely particular to speech-to-text.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from voice_common import errors, health, identity
from voice_common import logging as voice_logging
from voice_common.identity import has
from voice_common.scopes import check_owner_filter

from . import asr, openai_api, pipeline, profiles

# STT_LOG_LEVEL comes with this: until now the only way to get DEBUG out of a
# running container was to edit the source and rebuild the image.
log = voice_logging.setup("stt-stack", "STT")


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    started = time.monotonic()
    pipeline.start()

    log.info("ready in %.1fs, models=%s, %d threads",
             time.monotonic() - started, ",".join(pipeline.engines()), pipeline.THREADS)
    yield
    pipeline.stop()


app = FastAPI(
    title="stt-stack",
    description="VAD, one recogniser, glossary repair. Parakeet by default.",
    lifespan=lifespan,
)

# ApiError, the /v1 validation handler, the 404/405 handler and the unhandled
# 500, all rendered in OpenAI's four-field envelope.
errors.install_errors(app)


def _health_details() -> dict[str, object]:
    """The per-service half of /health. Must not block: it runs on the loop.

    Every value here is a module constant or a dict lookup, so it does not.
    `status` is overridden while the model is still loading — that is the one
    field voice_common fills in itself, and the one this service has to
    contradict.
    """
    loaded = pipeline.loaded()
    engines = list(pipeline.engines().values()) or ([loaded] if loaded else [])

    def any_engine(flag: str) -> bool:
        return any(bool(getattr(e, flag, False)) for e in engines)
    return {
        "status": "ok" if loaded else "loading",
        "model": pipeline.MODEL,
        # EVERY ENGINE THIS PROCESS SERVES, the default first: what a request
        # may name as `model`, what each can hear, and what it takes. The Home
        # Assistant integration builds its speech-to-text entities from this.
        "models": [
            {"id": engine_id,
             "family": engine.name,
             "default": index == 0,
             "languages": list(getattr(engine, "languages", ()) or ()),
             "accepts_language": bool(engine.accepts_language),
             "accepts_boost": bool(getattr(engine, "accepts_boost", False)),
             "can_translate": bool(getattr(engine, "can_translate", False)),
             "can_stream": bool(getattr(engine, "can_stream", False))}
            for index, (engine_id, engine) in enumerate(pipeline.engines().items())
        ],
        # STT_MODEL_ID names the one engine STT_MODEL loads; STT_MODELS
        # ignores it, and each entry above says its own.
        **({} if os.getenv("STT_MODELS", "").strip()
           else {"model_id": os.getenv("STT_MODEL_ID", "")}),
        "accepts_vocabulary": any_engine("accepts_vocabulary"),
        # What the compatibility surface will and will not do on this
        # deployment, so a client can find out without spending a request on a
        # refusal: whether any loaded engine can, which a request reaches by
        # its `model` (each entry above says which).
        "translations": any_engine("can_translate"),
        "streaming": any_engine("can_stream"),
        "hotwords": pipeline.HOTWORDS_ENABLED,
        # The system's profiles and the built-ins, so a client can see the
        # shared set without spending a request on a 400 for a name that is
        # not there. NEVER A USER'S (D50): this body is open inside the
        # network and reaches every health:read holder through the gateway,
        # and a profile name is somebody's vocabulary. The hub reads
        # `home-assistant` here before it names it.
        #
        # Read straight out of the pipeline's state rather than through
        # pipeline.registry(), and NOT refreshed: both of those stat() the
        # directories, and this function runs on the event loop, where a stat
        # against a hung NFS mount would block every request including the
        # container's own healthcheck. Whether writes are possible is a
        # question for GET /glossaries, which is allowed to touch the disk.
        "glossaries": _shared_glossaries(),
        "vad": pipeline.VAD_ENABLED,
        "threads": pipeline.THREADS,
        "max_concurrent": pipeline.MAX_CONCURRENT,
        # WHICH MACHINE ANSWERED. Every run record carries this label, so a
        # reader looking at a listing needs somewhere to check what it means.
        "host_label": pipeline.runlog.host,
        # A DROPPED RECORD IS INVISIBLE AS AN ABSENCE. The record queue is
        # bounded and drops rather than delaying a transcript, which is only
        # the right trade while somebody can see it happening: `dropped` going
        # up, or `last_error` holding a status, is the difference between
        # "nothing ran" and "the log could not keep up". Reading two counters
        # off an object does not block, which is this function's one rule.
        "runlog": pipeline.runlog.stats(),
    }


def _shared_glossaries() -> list[str]:
    registry = pipeline.state.get("glossaries")
    return registry.shared_names() if isinstance(registry, profiles.Registry) else []


# Registers GET /health at the one path voice_common.identity leaves open, so
# the route and the exemption can never come to name different strings.
# Container healthchecks call it and have no assertion; requiring one would
# turn a working service into a restart loop.
health.install_health(app, details=_health_details)

# Every other request needs the gateway's signed assertion for this service
# (D52). Middleware rather than a per-route dependency, so a route added later
# cannot be forgotten, and installed outermost whatever is added after it.
# /docs, /redoc and /openapi.json are removed rather than guarded: a schema
# dump is a free map of the service, and nobody here reads it.
identity.install(app, "stt")

app.include_router(openai_api.router)


class Transcript(BaseModel):
    text: str
    raw: str
    repaired: list[str]
    model: str
    audio_seconds: float
    speech_seconds: float
    compute_seconds: float
    realtime_factor: float


# Deliberately `def`, not `async def`. pipeline.run is blocking CPU work;
# declared async it would run ON the event loop and starve every other
# request, /health included, so a container healthcheck fails under load and
# the orchestrator restarts a service that is working correctly.
@app.post("/transcribe", response_model=Transcript)
def transcribe(
    request: Request,
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    # The same selector /v1 takes, spelled the same way. The native route is
    # not bound by ADR 0001 and could have used any name; using a different one
    # would mean the two routes on one service disagree about what a glossary
    # profile is called, which is the sort of difference a client discovers by
    # being wrong.
    glossary: str | None = Form(default=None),
) -> Transcript:
    # Built field by field rather than from asdict(): the pipeline's Result now
    # also carries segments, words and logprobs for the /v1 shapes, and this
    # body is a contract that already has clients. Widening it because another
    # route needed the data would be the same mistake as narrowing it.
    claims = identity.claims_of(request)
    result = pipeline.run(
        file.file.read(),
        asr.Options(language=language),
        rules=_select(glossary, profiles.view_of(claims)).rules,
        # 16 kHz only, still. See pipeline.decode: /v1 resamples because no
        # OpenAI client expects otherwise, and this route does not because
        # telling a client its audio is the wrong rate is the documented
        # behaviour it was built with.
        allow_resample=False,
        # Which door this came in by, for the run record and nothing else. The
        # client is left null on purpose: the page, a script and a shell all
        # send the same multipart body here, and a guess made from a user agent
        # would be stored as a fact.
        origin=pipeline.Origin(route="/transcribe", owner=claims.sub,
                               credential=claims.cred),
    )
    return Transcript(
        text=result.text,
        raw=result.raw,
        repaired=result.repaired,
        model=result.model,
        audio_seconds=result.audio_seconds,
        speech_seconds=result.speech_seconds,
        compute_seconds=result.compute_seconds,
        realtime_factor=result.realtime_factor,
    )


# ── glossary profiles ─────────────────────────────────────────────────────────
#
# NATIVE ROUTES, NOT /v1, and the reason is ADR 0001 rather than convenience:
# OpenAI has no concept of a glossary profile, so there is nothing here to be
# 1:1 with. Claiming /v1/glossaries would be taking specification territory
# that does not exist, and would collide the day OpenAI uses that path. Native
# routes are explicitly out of that ADR's scope, so these keep FastAPI's
# {"detail": ...} bodies like /transcribe does.
#
# WHO MAY CALL THEM is the gateway's decision (D5): glossaries:read:own to
# read, glossaries:write:own to write, glossaries:ha for home-assistant. What
# is decided here is what the gateway cannot see:
#
#   * whose profiles a request acts on. A user's own by default, the system's
#     for a holder of the `:all` form, or the namespace ?owner= names, which
#     needs the `:all` form of the route's scope (D33);
#   * whether a name, spelled any way at all, is the reserved home-assistant.
#     The gateway matches that path segment exactly, and profile names are
#     case-insensitive, so `/glossaries/Home-Assistant` reaches this file as an
#     ordinary name meaning the same profile (D34).
#
# Another user's profile is not refused, it is absent: a 404, and an "unknown
# profile" that lists only what the caller could have named.
#
# Writability follows the volume. This is not a permission system and calling
# it one would be dishonest: a deployment that mounted nowhere to persist has
# said, by omission, that it does not want run-time profiles, and accepting a
# PUT that evaporates on the next restart would be worse than refusing it. Same
# reasoning as the UI's clips.writable().


def _select(names: str | None, view: profiles.View) -> profiles.Selection:
    """Resolve a native request's `glossary=` into compiled rules, as `view` sees them.

    Mirrors openai_api._glossary, including the 400 on an unknown name or too
    many, but raises HTTPException so the native body stays {"detail": ...}. An unknown
    profile is named, never ignored: a caller who believes their vocabulary was
    applied when it was not has no way to discover the difference.
    """
    wanted = profiles.split_selection(names)
    if not wanted:
        return profiles.Selection(rules=pipeline.default_rules())
    registry = pipeline.registry()
    registry.refresh()
    try:
        return registry.select(wanted, view)
    except profiles.TooManyProfiles as exc:
        raise HTTPException(400, str(exc)) from exc
    except profiles.UnknownProfile as exc:
        raise HTTPException(
            400,
            f"unknown glossary profile {exc.name!r}; you can use: "
            f"{', '.join(exc.known) or 'none'}",
        ) from exc


def _registry() -> profiles.Registry:
    registry = pipeline.registry()
    registry.refresh()
    return registry


def _owner(request: Request) -> str | None:
    """?owner=, checked before it is used anywhere, a path join included (D32)."""
    raw = request.query_params.get("owner")
    if raw is None:
        return None
    try:
        return check_owner_filter(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _view(request: Request, name: str | None, *, write: bool) -> profiles.View:
    """The profiles one glossary route acts on, for the caller that sent it.

    `name` is the path's profile name, or None on the listing. A service has
    no namespace of its own, so every namespace needs the `:all` form for one.
    """
    claims = identity.claims_of(request)
    owner = _owner(request)
    wide = "glossaries:write:all" if write else "glossaries:read:all"

    if name is not None and profiles.is_reserved(name):
        # The hub holds glossaries:ha to select this profile for Assist; only a
        # person (the Home Assistant integration's key) may change it with it.
        accepted = [wide] if write and claims.kind != "user" else ["glossaries:ha", wide]
        if not any(has(claims, scope) for scope in accepted):
            raise errors.insufficient_scope(accepted)
        if owner not in (None, "system"):
            raise HTTPException(
                400, f"{profiles.RESERVED!r} is reserved and always the "
                     "system's profile; send no owner")
        return profiles.DEPLOYMENT

    own = claims.sub if claims.kind == "user" else None
    default = profiles.view_of(claims)
    if owner is None:
        namespace = default.namespace
    elif owner == "me":
        if own is None:
            raise HTTPException(400, "owner=me names a user's profiles, and "
                                     "this caller is a service")
        namespace = own
    elif owner == "system":
        namespace = profiles.SYSTEM
    elif owner == "all":
        raise HTTPException(400, "owner=all lists every profile; it names no "
                                 "single one")
    else:
        namespace = owner
    if namespace != own and not has(claims, wide):
        raise errors.insufficient_scope([wide])
    # The reserved profile is the system's, so it belongs only in a listing of
    # the system's or of the caller's own resolution. An admin listing a user
    # would otherwise find it filed under that user, where a GET of it is a 400.
    return profiles.View(namespace, reserved=default.reserved
                         and namespace in (profiles.SYSTEM, own))


def _profile(registry: profiles.Registry, name: str,
             view: profiles.View) -> profiles.Profile:
    if not profiles.valid_name(name):
        raise HTTPException(
            400,
            f"{name!r} is not a usable profile name; it becomes a filename, so "
            f"it must match {profiles.NAME_PATTERN.pattern}")
    try:
        return registry.get(name, view)
    except profiles.UnknownProfile as exc:
        raise HTTPException(404, f"no glossary profile named {exc.name!r}") from exc


def _require_writable(registry: profiles.Registry) -> None:
    writable, reason = registry.writability()
    if not writable:
        # 503 rather than 403: nothing about the CALLER is being refused. The
        # deployment has no writable volume, and the message says which one and
        # what to do about it, because "forbidden" would send an operator
        # looking for a permission they never configured.
        raise HTTPException(503, f"glossary profiles are read-only here: {reason}")


def _refuse_shadowing(profile: profiles.Profile) -> None:
    """A built-in name is a 409, not a silent shadow.

    A profile whose contents depend on which directory won is a profile nobody
    can reason about, and "why is `tech` different on that box" is not a
    question worth creating. Copy it under another name instead.
    """
    if not profile.writable:
        raise HTTPException(
            409,
            f"{profile.name!r} is a {profile.source} profile and is read-only. "
            "Copy it to a new name and edit that: GET /glossaries/"
            f"{profile.name} gives you its text.")


@app.get("/glossaries")
def list_glossaries(request: Request) -> dict[str, object]:
    """Every profile the caller can name, whose it is, and how many terms it carries.

    `?owner=all`, for a holder of glossaries:read:all, lists every namespace at
    once: the built-ins, the system's, then each user's, each entry naming its
    owner.
    """
    if _owner(request) == "all":
        if not has(identity.claims_of(request), "glossaries:read:all"):
            raise errors.insufficient_scope(["glossaries:read:all"])
        registry = _registry()
        listed = list(registry.every())
    else:
        view = _view(request, None, write=False)
        registry = _registry()
        visible = registry.visible(view)
        listed = [visible[name] for name in sorted(visible)]
    writable, reason = registry.writability()
    body: dict[str, object] = {
        "glossaries": [profile.summary() for profile in listed],
        "writable": writable,
        # The profiles a request gets when it selects none. Empty on a default
        # deployment, and that is the point: an irrelevant glossary raised WER
        # by 28% on Whisper, and nothing measurable on Parakeet across 25 cells, so
        # always-on is opted into by name rather than inherited.
        "default": profiles.split_selection(pipeline.DEFAULT_PROFILES),
    }
    if not writable:
        body["reason"] = reason
    return body


@app.get("/glossaries/{name}")
def get_glossary(name: str, request: Request) -> dict[str, object]:
    """One profile's terms, and the file text they were parsed from.

    `text` is returned as well as the parsed halves so that editing a profile
    is a round trip — GET, change a line, PUT — rather than a reconstruction
    from two JSON objects that would drop every comment in the file. The
    comments are where a glossary explains why a rule is a hotword rather than
    a replacement, which is exactly the knowledge worth not losing.
    """
    view = _view(request, name, write=False)
    profile = _profile(_registry(), name, view)
    return {
        **profile.summary(),
        "replacements": profile.parsed.replacements,
        "hotwords": list(profile.parsed.hotwords),
        "text": profile.text,
    }


@app.put("/glossaries/{name}")
async def put_glossary(name: str, request: Request) -> Response:
    """Create or replace a custom profile, in the caller's namespace or ?owner='s.

    Two body shapes, because both callers are real: `application/json` with
    {"text": ..., "force": ...}, and a raw `text/plain` body for
    `curl -X PUT --data-binary @mine.txt`, which is a perfectly good client for
    a text file. `?force=true` works with either.

    `async def` only because the body has to be awaited. Everything after that
    stat()s and writes a mounted volume, which is exactly the work that must
    not happen on the event loop — a hung NFS mount would otherwise take
    /health down with it and have the orchestrator restart a service that is
    working — so it runs in a worker thread. Whose namespace it lands in is
    settled before the body is read.
    """
    view = _view(request, name, write=True)
    text, force = await _body(request)
    return await run_in_threadpool(_write_profile, name, text, force, view.namespace)


def _write_profile(name: str, text: str, force: bool, owner: str) -> Response:
    """The blocking half of PUT. See put_glossary for why it is split off.

    NOTHING IS WRITTEN IF ANYTHING WAS REJECTED. The rejected lines come back
    with their line numbers and reasons and the file on disk is untouched.

    That is a deliberate reading of "the response says how many terms were
    accepted and lists every line that was not": a 200 carrying a `rejected`
    array is trivially ignored by a script, and a profile that silently lost
    three of its rules is precisely the half-succeeded write this repository
    has already been bitten by three times in other forms. An error status
    cannot be ignored by accident, and re-sending a corrected file costs one
    request.
    """
    registry = _registry()
    if not profiles.valid_name(name):
        raise HTTPException(
            400,
            f"{name!r} is not a usable profile name; it becomes a filename, so "
            f"it must match {profiles.NAME_PATTERN.pattern}")

    # A name already taken by a built-in is a 409 BEFORE the writability check,
    # so an operator on an unmounted box is not told to mount a volume and then
    # told, one deploy later, that the name was never available anyway. In a
    # user's namespace too: a built-in is everyone's, so nobody's own `tech`
    # may hide it.
    key = name.strip().lower()
    held = registry.namespace(owner)
    existing = registry.builtins.get(key) or held.get(key)
    if existing is not None:
        _refuse_shadowing(existing)
    if (existing is None and owner != profiles.SYSTEM
            and len(held) >= profiles.MAX_PER_USER):
        raise HTTPException(
            409, f"this namespace already holds {len(held)} profiles, the most "
                 "one user may keep; delete one first")
    _require_writable(registry)

    try:
        parsed = profiles.check(text, force=force)
    except profiles.TooLarge as exc:
        # 413, not 400: the payload is the problem, and a client that reads
        # status codes rather than messages should still learn the right thing.
        raise HTTPException(413, str(exc)) from exc

    if parsed.rejected:
        raise HTTPException(400, {
            "message": (
                f"{len(parsed.rejected)} line(s) rejected; nothing was written. "
                f"{parsed.terms} term(s) would have been accepted."),
            "accepted": parsed.terms,
            "rejected": [r.as_dict() for r in parsed.rejected],
        })

    profile = registry.write(name, text, owner)
    log.info("glossary profile %r of %s written: %d terms (%s)", profile.name,
             profile.owner, profile.parsed.terms,
             "forced" if force else "validated")
    return JSONResponse(
        status_code=200 if existing is not None else 201,
        content={**profile.summary(), "forced": force,
                 "created": existing is None},
    )


@app.delete("/glossaries/{name}")
def delete_glossary(name: str, request: Request) -> dict[str, object]:
    view = _view(request, name, write=True)
    registry = _registry()
    profile = _profile(registry, name, view)
    _refuse_shadowing(profile)
    _require_writable(registry)
    registry.remove(profile)
    log.info("glossary profile %r of %s deleted", profile.name, profile.owner)
    return {"name": profile.name, "deleted": True}


async def _body(request: Request) -> tuple[str, bool]:
    """The proposed file text, and whether the caller forced it.

    `force` is read from the query string first so that it works for both body
    shapes; a JSON body may also carry it. Anything but JSON is taken as the
    file itself, which is what makes `--data-binary @mine.txt` work with no
    content type set at all.
    """
    force = request.query_params.get("force", "").strip().lower() in {
        "1", "true", "yes", "on"}
    raw = await request.body()
    media = (request.headers.get("content-type") or "").split(";")[0].strip()

    if media == "application/json":
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            raise HTTPException(400, f"body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(
                payload.get("text"), str):
            raise HTTPException(
                400, "a JSON body must be an object with a string 'text' field; "
                     "send the file as text/plain to skip the wrapper")
        return payload["text"], force or bool(payload.get("force"))

    try:
        return raw.decode("utf-8"), force
    except UnicodeDecodeError as exc:
        raise HTTPException(
            400, "glossary files are UTF-8 text; this body is not") from exc
