"""What the gateway promises, asserted against mock backends.

One test per rule in the contract, named for the rule rather than for the
function, because the rules are the thing that must not drift. Where a test
exists to prevent a specific failure, the failure is in the docstring.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import httpx
import pytest
from conftest import (PUBLIC_ORIGIN, MockBackend, Slow, Unreachable, bearer, gateway,
                      make_key, make_user, reload_gateway)
from voice_common.conformance import assert_four_field_envelope
from voice_common.engines import CATALOGUE

SPEECH = "/v1/audio/speech"

# The catalogue rows this backend owns, read at collection time so a new engine
# becomes a NAMED case in the pytest report rather than another iteration of a
# loop that already said `1 passed`. Filtered on `owned_by` for the same reason
# main.py filters LONG_KNOWN on it: the catalogue is the estate's fact table
# and not this backend's inventory, so a row for a checkpoint the fast path
# owns must not be dragged into a test about long-form routing.
OURS = tuple(sorted(e for e, f in CATALOGUE.items() if f.owned_by == "tts-long"))


def body(**kwargs) -> bytes:
    return json.dumps({"input": "Here is the change to make.", **kwargs}).encode()


# ------------------------------------------------------------------ routing --


@pytest.mark.parametrize("model", ["kokoro", "tts-1", "tts-1-hd",
                                   "gpt-4o-mini-tts", None, "banana",
                                   "gpt-9-turbo-audio", ""])
async def test_everything_but_the_two_long_names_goes_fast(monkeypatch, backends, model):
    """An unknown model goes fast, and is not a 400.

    The two wrong answers are asymmetric: Kokoro on long-form costs some
    quality, Chatterbox on an ordinary request turns a 17-second call into a
    ten-minute job nobody asked for. Rejecting unknown names would also break
    a client that sends whatever string its UI was left holding.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        payload = body() if model is None else body(model=model)
        response = await client.post(SPEECH, content=payload)

    assert response.status_code == 200
    assert len(tts.seen) == 1 and not long.seen


@pytest.mark.parametrize("model", ["chatterbox", "tts-long", "Chatterbox",
                                   " chatterbox "])
async def test_only_the_two_opt_in_names_reach_the_long_backend(monkeypatch, backends, model):
    """Case and surrounding whitespace do not decide a nine-minute difference."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=body(model=model))

    assert response.status_code == 200
    assert len(long.seen) == 1 and not tts.seen


async def test_transcription_routes_reach_the_only_stt_backend(monkeypatch, backends):
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        for path in ("/v1/audio/transcriptions", "/transcribe"):
            assert (await client.post(path, content=b"RIFF....")).status_code == 200

    assert [r["path"] for r in stt.seen] == ["/v1/audio/transcriptions", "/transcribe"]
    assert not tts.seen and not long.seen


async def test_native_fast_routes_reach_tts_stack(monkeypatch, backends):
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        assert (await client.post("/speak", content=body())).status_code == 200
        assert (await client.get("/voices")).status_code == 200

    assert [r["path"] for r in tts.seen] == ["/speak", "/voices"]


async def test_job_routes_mount_flat_and_are_not_rewritten(monkeypatch, backends):
    """The path reaches tts-long verbatim, which is what keeps its own URLs valid.

    tts-long answers a 202 with `Location: /jobs/{id}` and a body field
    `audio_url: /jobs/{id}/audio`, both relative to its own root. Any prefix
    here would force the gateway to rewrite a header and a JSON field, and
    that rule rots the first time the backend adds a field.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        await client.post("/jobs", content=body())
        await client.get("/jobs")
        await client.get("/jobs/abc-123")
        await client.get("/jobs/abc-123/audio?format=wav")

    assert [r["path"] for r in long.seen] == [
        "/jobs", "/jobs", "/jobs/abc-123", "/jobs/abc-123/audio"]
    assert long.seen[-1]["query"] == "format=wav"


async def test_a_job_can_be_cancelled_through_the_gateway(monkeypatch, backends):
    """tts-long has had DELETE /jobs/{id} all along (main.py:580); this table
    simply never carried it, so the request met Starlette's 405 and
    `method_not_supported`. At 0.138x realtime the jobs that most need calling
    off are the ones measured in tens of minutes, and a job that can be started
    through the front door should be stoppable through it.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.delete("/jobs/abc-123")

    assert response.status_code == 200
    assert (long.seen[-1]["method"], long.seen[-1]["path"]) == ("DELETE", "/jobs/abc-123")


async def test_a_glossary_can_be_written_and_deleted_through_the_gateway(
        monkeypatch, backends):
    """The four /glossaries routes are the only WRITE surface behind this door.

    Routing them is not enough and the DELETE /jobs/{id} failure above is why:
    a path registered for the wrong methods meets Starlette's 405 and never
    reaches a backend that has had the route all along. So the method, the
    body and the query string are asserted where they land, not here.

    ?force=true carries the most meaning of any query string on this service.
    It is what lets a single-word left-hand side through, so dropping it turns
    an accepted `belly = Belli` rule into a 400 that names a rule the operator
    did send. Streaming the body makes that easy to get wrong: an empty
    forwarded body would still be a 200 from a backend that writes an empty
    profile.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        written = await client.put("/glossaries/mine?force=true",
                                   content=b"belly = Belli\n")
        listed = await client.get("/glossaries")
        read = await client.get("/glossaries/mine")
        removed = await client.delete("/glossaries/mine")

    assert [(r["method"], r["path"]) for r in stt.seen] == [
        ("PUT", "/glossaries/mine"), ("GET", "/glossaries"),
        ("GET", "/glossaries/mine"), ("DELETE", "/glossaries/mine")]
    assert stt.seen[0]["body"] == b"belly = Belli\n"
    assert stt.seen[0]["query"] == "force=true"
    for response in (written, listed, read, removed):
        assert response.status_code == 200
    assert not tts.seen and not long.seen


async def test_a_json_glossary_body_keeps_its_content_type(monkeypatch, backends):
    """stt reads the content type to decide between JSON and the raw file.

    `_body` in services/stt/app/main.py takes anything that is not
    `application/json` as the file itself, so a stripped content type would
    write the literal string `{"text": "..."}` into the profile and answer 200
    while doing it.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        await client.put("/glossaries/mine", json={"text": "a b = C\n"})

    assert stt.seen[-1]["headers"]["content-type"] == "application/json"


@pytest.mark.parametrize("long_models",
                         [None, "chatterbox,tts-long,chatterbox-turbo",
                          "chatterbox,tts-long,chatterbox-turbo,voxtral"])
async def test_advertised_models_route_where_the_list_says_they_do(
        monkeypatch, backends, long_models):
    """GET /v1/models is the routing table, so it must not drift from it.

    The list names an `owned_by` per model. This sends every advertised TTS
    name through the router and checks it lands on the backend the list
    claims — the one failure that would make the discoverable contract a lie.

    Read off the RELOADED module rather than imported from app.openai_api,
    because the long-form rows are no longer a literal there: they are
    generated from the same frozenset the router branches on, under this
    deployment's GATEWAY_LONG_MODELS. Importing the static table would have
    tested a table nothing routes. Both shapes are run — the default set, and a
    set with a second engine in it — so enabling one cannot make the advertised
    list and the router disagree.
    """
    stt, tts, long = backends

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       long_models=long_models) as (client, main):
        for entry in main.MODEL_LIST["data"]:
            if entry["owned_by"] == "stt-stack":
                continue  # no decision to make: one STT backend
            await client.post(SPEECH, content=body(model=entry["id"]))
            landed = "tts-stack" if tts.seen else "tts-long"
            assert landed == entry["owned_by"], entry["id"]
            tts.seen.clear()
            long.seen.clear()


async def test_models_is_answered_without_touching_a_backend(monkeypatch, backends):
    """It must answer while a backend is restarting — that is when it is needed."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.get("/v1/models")

    assert response.status_code == 200
    ids = [m["id"] for m in response.json()["data"]]
    assert {"kokoro", "chatterbox", "whisper-1"} <= set(ids)
    assert not stt.seen and not tts.seen and not long.seen


# ------------------------------------------------- a second long-form engine --
#
# The engine is the model string. That makes GATEWAY_LONG_MODELS a routing
# table with a second entry in it, and it opens one failure the single-engine
# design could not have: a caller names an engine this gateway RECOGNISES,
# this deployment has not enabled it, and the old "everything else goes fast"
# rule hands back Kokoro. 200, audio, wrong engine, no error.


async def test_a_second_engine_reaches_the_long_backend_when_enabled(
        monkeypatch, backends):
    """The whole point of the package: a named engine is reachable over the API.

    It is still a job — this route may answer 202 with a job id and that is
    tts-long's call, not this service's. All the gateway owes is that the name
    lands on tts-long and nothing about it is rewritten on the way.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       long_models="chatterbox,tts-long,chatterbox-turbo",
                       ) as (client, _):
        response = await client.post(SPEECH, content=body(model="chatterbox-turbo"))

    assert response.status_code == 200
    assert len(long.seen) == 1 and not tts.seen
    assert json.loads(long.seen[0]["body"])["model"] == "chatterbox-turbo"


@pytest.mark.parametrize("model", ["chatterbox-turbo", "Chatterbox-Turbo",
                                   " chatterbox-turbo "])
async def test_a_known_but_disabled_long_model_is_404_not_kokoro(
        monkeypatch, backends, model):
    """THE DEFECT THIS PREVENTS: a typo, or a client configured against another
    box, names an engine this deployment has not enabled — and gets audio.

    Under the rule this service shipped with, an unrecognised name goes fast.
    That was the recoverable mistake while there was one long-form model to be
    downgraded from. It stops being recoverable when the name IS an engine:
    Kokoro answers 200 with audio in a different voice from a different model,
    and nothing in the response says the request was not honoured. The caller
    typed an engine name; the one thing they cannot have meant is "surprise
    me".

    Case and whitespace are folded here for the same reason they are on the
    routing branch: whether you get a 404 or silent Kokoro must not be decided
    by a capital letter.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=body(model=model))

    assert response.status_code == 404
    assert not tts.seen and not long.seen
    error = assert_four_field_envelope(response)
    assert error["code"] == "model_not_found"
    assert error["param"] == "model"
    # The way out, named. An error that says "not enabled" and not which
    # variable enables it costs a grep of five services.
    assert "GATEWAY_LONG_MODELS" in error["message"]
    assert "chatterbox" in error["message"]


async def test_a_disabled_long_model_still_writes_its_log_line(monkeypatch, backends,
                                                               caplog):
    """One line per request is this service's entire observability budget.

    The two other ways out of `speech` that never reach a backend — a client
    disconnect and unparseable JSON — both log, and both had to be fixed to.
    A refusal that answers 404 in silence is a class of failure that is
    invisible to grep, which is the only tool pointed at this.
    """
    stt, tts, long = backends
    with caplog.at_level("INFO", logger="voice-gateway"):
        async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
            await client.post(SPEECH, content=body(model="chatterbox-turbo"))

    lines = [r.getMessage() for r in caplog.records if "route=" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "model=chatterbox-turbo" in lines[0]
    assert "status=404-model" in lines[0]


async def test_the_advertised_long_models_are_exactly_GATEWAY_LONG_MODELS(
        monkeypatch, backends):
    """Advertised and routed come off one frozenset, so they cannot disagree.

    Asserted as an EQUALITY rather than a subset. The subset check next door
    answers "is chatterbox still there"; this one answers the question that
    actually bites, which is whether a name nobody enabled is being published
    to every client's model picker — or whether one that is enabled is missing
    from it, so the only way to learn the name is to read the compose file.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       long_models="chatterbox,tts-long,chatterbox-turbo,"
                                   "voxtral",
                       ) as (client, main):
        listed = (await client.get("/v1/models")).json()["data"]

    advertised = {m["id"] for m in listed if m["owned_by"] == "tts-long"}
    assert advertised == set(main.LONG_MODELS)
    # FOUR NAMES, SPELLED OUT. The equality above already holds whatever the
    # variable says, which is exactly why it cannot catch a set that shrank:
    # both sides are read from one env var, so dropping a name from compose
    # keeps this green. The literal is the second witness, and it is the one
    # that has to be edited by hand when an engine is added or removed.
    assert advertised == {"chatterbox", "tts-long", "chatterbox-turbo",
                          "voxtral"}


async def test_a_deployment_that_enables_nothing_long_advertises_nothing_long(
        monkeypatch, backends):
    """An empty GATEWAY_LONG_MODELS is a legal deployment: a box with no card
    and no local engine, running the fast path alone.

    It must not leave `chatterbox` advertised. A model picker that offers a
    name the router will not take is worse than a short list, because the
    failure arrives as a 404 from a name the service itself published.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       long_models=" , ") as (client, main):
        listed = (await client.get("/v1/models")).json()["data"]
        response = await client.post(SPEECH, content=body(model="chatterbox"))

    assert not main.LONG_MODELS
    assert not [m for m in listed if m["owned_by"] == "tts-long"]
    # `chatterbox` is still a name this gateway KNOWS, so it is refused rather
    # than sent to Kokoro — the same rule, one engine further down.
    assert response.status_code == 404
    assert not long.seen and not tts.seen


async def test_every_catalogue_id_is_either_routed_or_404_never_fast(
        monkeypatch, backends):
    """THE FENCE FOR THE MODEL TABLE, coming at it from the catalogue's side.

    The three allowlist fences below ask the same question about PATHS from
    three directions, because a route present in two tables out of three is how
    both of this week's live bugs shipped. This is that question about MODEL
    STRINGS, and it comes from the direction nothing else does: not "does every
    advertised name route", but "is every name the shared catalogue defines
    accounted for here".

    An engine added to voice_common.engines and never wired into this service
    is the exact shape of `chatterbox-cpu` — configured somewhere, reachable
    nowhere, and detectable only by whoever noticed. Here it cannot fall
    through to the fast path in silence: it either routes long or it is refused
    by name.

    SCOPED TO THE ROWS TTS-LONG OWNS, and the scope is the whole point rather
    than a caveat. The catalogue is the estate's fact table, not this
    backend's: `EngineFacts.owned_by` names the service, and the fast path's
    own `kokoro` is a row waiting to be written. Asserting "every catalogue id
    routes long or 404s" would be asserting that a Kokoro row must 404 — this
    fence demanding the outage the code was changed to prevent. The companion
    below states the other half for the rows this backend does not own.
    """
    assert CATALOGUE, "read no engine ids at all out of voice_common.engines"
    assert OURS, "no catalogue row says tts-long owns it; this fence sees nothing"

    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       long_models="chatterbox,tts-long") as (client, main):
        for engine in OURS:
            response = await client.post(SPEECH, content=body(model=engine))
            if engine in main.LONG_MODELS:
                assert response.status_code == 200 and long.seen, engine
            else:
                assert response.status_code == 404, engine
                assert response.json()["error"]["code"] == "model_not_found", engine
            assert not tts.seen, (
                f"{engine} is an engine tts-long owns and it reached the fast "
                "backend: the caller named an engine and got Kokoro")
            tts.seen.clear()
            long.seen.clear()


@pytest.mark.parametrize("engine", OURS)
async def test_a_catalogue_name_not_in_GATEWAY_LONG_MODELS_is_404_not_kokoro(
        monkeypatch, backends, engine):
    """The same guarantee as the loop above, held per row rather than in bulk.

    PARAMETRISED OFF THE CATALOGUE SO A NEW ENGINE ARRIVES WITH ITS OWN CASE,
    named after itself in the pytest output. The loop above is one test that
    passes or fails as a unit; a third engine landing in the catalogue widened
    it silently and the report still read `1 passed`. Here `voxtral` shows up
    as a line with `voxtral` in it, which is the difference between a suite
    that covers a name and a suite that can be read to say so.

    Every OTHER catalogue name is enabled while this one is not, because the
    failure being fenced is a name falling off the end of a NON-EMPTY set — the
    shape a real deployment has when the operator adds an engine to
    TTS_ENGINES and forgets GATEWAY_LONG_MODELS. An empty set is a different
    test and it is next door.
    """
    others = sorted((set(CATALOGUE) - {engine}) | {"tts-long"})
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       long_models=",".join(others)) as (client, _):
        response = await client.post(SPEECH, content=body(model=engine))

    assert response.status_code == 404, (
        f"{engine} is not in GATEWAY_LONG_MODELS and this deployment answered "
        f"{response.status_code}: the caller named an engine and got Kokoro")
    assert not tts.seen and not long.seen
    error = assert_four_field_envelope(response)
    assert error["code"] == "model_not_found"
    assert "GATEWAY_LONG_MODELS" in error["message"]


async def test_a_catalogue_row_the_fast_backend_owns_is_never_refused_here(
        monkeypatch, backends):
    """THE DEFECT THIS PREVENTS: the day Kokoro gets a catalogue row, every
    unconfigured OpenAI client on the stack starts getting 404 model_not_found.

    `LONG_KNOWN` decides which names are refused instead of sent fast. It read
    the WHOLE catalogue, which encodes "every checkpoint anybody writes a row
    for belongs to tts-long" — true of every row written so far, and false of
    the next one. Kokoro is `owned_by: "tts-stack"`, its row is named in
    docs/adr as work already scheduled, and on the morning it lands `kokoro`,
    the one string every default client sends, would be answered 404 with a
    message telling the caller to add it to GATEWAY_LONG_MODELS — which would
    then route it to a backend that has never held those weights.

    The row is planted with `dataclasses.replace` rather than built field by
    field ON PURPOSE: EngineFacts is growing columns this quarter and a
    constructor call here would fail for the wrong reason, which is how a fence
    gets deleted instead of read.
    """
    import dataclasses

    from voice_common import engines

    stt, tts, long = backends
    planted = dataclasses.replace(engines.CATALOGUE["chatterbox"],
                                  id="kokoro", owned_by="tts-stack")
    monkeypatch.setitem(engines.CATALOGUE, "kokoro", planted)

    # Reloaded AFTER the row is planted: LONG_KNOWN is computed at import, as
    # every other piece of this service's configuration is.
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       long_models="chatterbox,tts-long") as (client, main):
        assert "kokoro" not in main.LONG_KNOWN, (
            "a row the fast backend owns was counted as a tts-long name")
        response = await client.post(SPEECH, content=body(model="kokoro"))

    assert response.status_code == 200, (
        "a catalogue row owned by tts-stack was refused by the gateway: "
        "writing down a fact about the fast path took the fast path away")
    assert len(tts.seen) == 1 and not long.seen
    # And it is still refused when tts-long DOES own it, so the fence above is
    # narrowing on the owner rather than on the catalogue having any row at all.
    assert "chatterbox-turbo" in main.LONG_KNOWN


# ------------------------------------------------------------- pass-through --


async def test_the_202_deviation_passes_through_untouched(monkeypatch, backends):
    """tts-long's honest 202 is forwarded, header and body byte-for-byte.

    The gateway does not invent this and must not re-implement it: the backend
    already decides when a wait is honest. Rewriting the body would break the
    audio_url the caller needs.
    """
    stt, tts, long = backends
    payload = json.dumps({"id": "job-1", "status": "queued", "queued_ahead": 0,
                          "estimated_seconds": 580,
                          "audio_url": "/jobs/job-1/audio"}).encode()
    long.reply = lambda record: (202, {"content-type": "application/json",
                                       "location": "/jobs/job-1"}, payload)

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=body(model="chatterbox"))

    assert response.status_code == 202
    assert response.headers["location"] == "/jobs/job-1"
    assert response.content == payload
    assert response.headers["content-type"] == "application/json"


@pytest.mark.parametrize("status,code", [(400, "invalid_value"),
                                         (422, "missing_required_parameter"),
                                         (503, "model_loading")])
async def test_a_backend_envelope_is_never_rewrapped(monkeypatch, backends, status, code):
    """stt-stack answers 422 where the TTS services answer 400. Both pass as-is.

    Re-wrapping destroys the `code` the client switches on, and normalising
    the difference would make the gateway a second, lying source of truth.
    """
    stt, tts, long = backends
    payload = json.dumps({"error": {"message": "no", "type": "invalid_request_error",
                                    "code": code}}).encode()
    tts.reply = lambda record: (status, {"content-type": "application/json"}, payload)

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=body())

    assert response.status_code == status
    assert response.content == payload
    assert response.json()["error"]["code"] == code


async def test_the_clients_key_is_stripped_and_an_assertion_sent_in_its_place(
        monkeypatch, backends):
    """A backend checks no credential, so copying the key would only copy a
    secret into another log stream. What it receives instead is the gateway's
    signed statement of who asked and with which scopes (D4, D51)."""
    from voice_common.identity import ASSERTION_HEADER, verify

    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, main):
        response = await client.post(SPEECH, content=body())
        keys = main.runtime.get().keys.published_keys()

    assert response.status_code == 200
    seen = tts.last["headers"]
    assert "authorization" not in seen
    claims = verify(seen[ASSERTION_HEADER.lower()], "tts", keys)
    assert claims.kind == "user" and claims.cred.startswith("k_")
    assert "speech:speak" in claims.scopes


async def test_response_headers_survive_except_the_hop_by_hop_ones(monkeypatch, backends):
    """X-Realtime-Factor is how the operator sees the box keeping up.

    `connection` and `transfer-encoding` describe one connection and must not
    be copied onto the next.
    """
    stt, tts, long = backends
    tts.reply = lambda record: (200, {"content-type": "audio/mpeg",
                                      "x-realtime-factor": "1.4",
                                      "connection": "keep-alive",
                                      "transfer-encoding": "chunked"}, b"ID3audio")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=body())

    assert response.content == b"ID3audio"
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["x-realtime-factor"] == "1.4"
    assert "connection" not in response.headers
    assert "transfer-encoding" not in response.headers


# ----------------------------------------------------------------- streaming --


async def test_an_upload_is_forwarded_chunk_by_chunk(monkeypatch, backends):
    """An hour of wav is 100 MB+; buffering it here doubles resident memory.

    The mock counts one ASGI message per chunk it received, so more than one
    chunk out of a five-chunk upload proves the body was passed through rather
    than collected first.
    """
    stt, tts, long = backends

    async def upload():
        for _ in range(5):
            yield b"x" * 65536

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post("/transcribe", content=upload())

    assert response.status_code == 200
    assert stt.last["chunks"] > 1
    assert stt.last["body"] == b"x" * 65536 * 5


async def test_the_speech_body_is_buffered_but_forwarded_unchanged(monkeypatch, backends):
    """This is the one body the gateway reads: `model` has to come out of it.

    It is text measured in kilobytes, unlike the audio uploads. What reaches
    the backend is still the client's own bytes, not a re-serialisation —
    re-encoding would drop any field the gateway does not know about.
    """
    stt, tts, long = backends
    payload = b'{"model":"kokoro","input":"hi","instructions":"whisper it"}'

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        await client.post(SPEECH, content=payload)

    assert tts.last["body"] == payload
    assert tts.last["headers"]["content-length"] == str(len(payload))


# ------------------------------------------------------------------ failures --


async def test_a_container_that_is_down_is_503_and_names_itself(monkeypatch, backends):
    """503, not 502: 502 claims the upstream answered badly and it never answered.

    With three backends behind one URL, "upstream failed" is unactionable.
    """
    stt, tts, _ = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=Unreachable()) as (client, _):
        response = await client.post(SPEECH, content=body(model="chatterbox"))

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    error = response.json()["error"]
    assert error["code"] == "backend_unavailable"
    assert "tts-long" in error["message"]


async def test_a_timeout_names_the_rate_and_the_way_out(monkeypatch, backends):
    """A 504 whose body does not name the alternative just gets retried."""
    stt, _, long = backends
    async with gateway(monkeypatch, stt=stt, tts=Slow(), long=long) as (client, _):
        response = await client.post(SPEECH, content=body())

    assert response.status_code == 504
    error = response.json()["error"]
    assert error["code"] == "backend_timeout"
    assert "1.2-1.5x realtime" in error["message"]
    assert "chatterbox" in error["message"]
    assert "/jobs/{id}/audio" in error["message"]


async def test_the_way_out_of_a_timeout_names_a_model_this_box_will_take(
        monkeypatch, backends):
    """A 504 that points at a model the same process answers 404 for is worse
    than one that points nowhere: the caller retries against advice.

    The old string was the literal `chatterbox`, which was correct while it was
    the only long-form name. It is now whatever this deployment enabled, and
    the two shapes that matter are a renamed set and an empty one.
    """
    stt, _, long = backends
    async with gateway(monkeypatch, stt=stt, tts=Slow(), long=long,
                       long_models="chatterbox-turbo") as (client, _):
        renamed = (await client.post(SPEECH, content=body())).json()["error"]
    assert "chatterbox-turbo" in renamed["message"]

    # A box with no long-form engine at all. There is no way out to offer, so
    # none is offered — rather than "send one of ()".
    stt, _, long = backends
    async with gateway(monkeypatch, stt=stt, tts=Slow(), long=long,
                       long_models=" , ") as (client, _):
        bare = (await client.post(SPEECH, content=body())).json()["error"]
    assert "1.2-1.5x realtime" in bare["message"]
    assert "long-form" not in bare["message"] and "()" not in bare["message"]


async def test_a_long_path_timeout_points_at_the_queue(monkeypatch, backends):
    """The job may still be running; a 504 that hid the id would be a leak."""
    stt, tts, _ = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=Slow()) as (client, _):
        response = await client.post(SPEECH, content=body(model="chatterbox"))

    assert response.status_code == 504
    assert "GET /jobs" in response.json()["error"]["message"]


async def test_the_long_read_timeout_sits_above_the_backends_own(monkeypatch):
    """240 s vs tts-long's SYNC_TIMEOUT of 180 s, so its honest 202 wins the race.

    A gateway timing out first would return 504 for a job that is still
    running and will produce audio, and would throw the job id away.
    """
    main = reload_gateway(monkeypatch)
    assert main.LONG.read_timeout > 180
    assert main.STT.read_timeout == 900
    assert main.TTS.read_timeout == 300
    assert main.CONNECT_TIMEOUT == 2


async def test_a_non_json_5xx_is_wrapped_and_truncated(monkeypatch, backends):
    """An HTML error page or a bare uvicorn 500 is not an envelope.

    Truncated because a stack trace reaches a client that cannot use it, and
    200 bytes is enough to tell an HTML page from a Python exception. The
    whole body goes to the log.
    """
    stt, tts, long = backends
    page = b"<html><body>" + b"z" * 4000 + b"</body></html>"
    tts.reply = lambda record: (500, {"content-type": "text/html"}, page)

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=body())

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "backend_error"
    assert "tts-stack" in error["message"]
    assert "z" * 100 in error["message"]
    assert len(error["message"]) < 400  # not the whole 4 KB page


async def test_a_json_5xx_is_left_alone(monkeypatch, backends):
    """It is already an envelope; wrapping it would hide the backend's own code."""
    stt, tts, long = backends
    payload = json.dumps({"error": {"message": "synthesis failed: ffmpeg",
                                    "type": "server_error",
                                    "code": "synthesis_failed"}}).encode()
    tts.reply = lambda record: (500, {"content-type": "application/json"}, payload)

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=body())

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "synthesis_failed"


async def test_a_loading_backend_gets_a_retry_after_and_nothing_else(monkeypatch, backends):
    """tts-stack's own 503 model_loading passes through; the gateway adds a header.

    Kokoro is 330 MB and always resident, so the window is seconds at
    container start, not minutes.
    """
    stt, tts, long = backends
    payload = json.dumps({"error": {"message": "model still loading",
                                    "type": "server_error",
                                    "code": "model_loading"}}).encode()
    tts.reply = lambda record: (503, {"content-type": "application/json"}, payload)

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=body())

    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"
    assert response.content == payload


async def test_a_backends_own_retry_after_is_not_overwritten(monkeypatch, backends):
    stt, tts, long = backends
    tts.reply = lambda record: (503, {"content-type": "application/json",
                                      "retry-after": "120"}, b"{}")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=body())

    assert response.headers["retry-after"] == "120"


async def test_unparseable_json_is_the_only_body_validation(monkeypatch, backends):
    """The gateway cannot route what it cannot parse — and nothing else is its business.

    Empty input, a bad voice, an unsupported response_format: the backend has
    better messages for all of them, and never sees this one.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=b"{not json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_value"
    assert not tts.seen and not long.seen


async def test_a_json_body_that_is_not_an_object_still_reaches_the_backend(monkeypatch, backends):
    """It has no `model`, so it goes fast and the backend says what is wrong."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=b'["input"]')

    assert response.status_code == 200
    assert tts.last["body"] == b'["input"]'


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc",
                                  "/anything", "/ui/nope", "/ui/transcribex"])
async def test_everything_outside_the_table_is_404(monkeypatch, backends, path):
    """No catch-all pass-through.

    stt-stack deliberately put /docs, /redoc and /openapi.json behind its key
    because FastAPI's defaults were handing out a free map of the service. A
    wildcard route here would quietly undo that decision.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.get(path)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_url"
    assert not stt.seen and not tts.seen and not long.seen


async def test_a_wrong_method_is_an_envelope_too(monkeypatch, backends):
    """A NATIVE route, and its wording and code are deliberately unchanged.

    /transcribe, /speak, /voices and /jobs have clients — bench/bench.py, the
    integration suite, Open WebUI — and `method_not_supported` is a string one
    of them may already branch on. The /v1 side now answers `method_not_allowed`
    with the three backends' wording; this side keeps what it had.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.get("/transcribe")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_supported"


async def test_a_v1_error_reads_the_same_here_as_from_the_backend(monkeypatch,
                                                                 backends):
    """A 404 and a 405 under /v1 are the estate's, not this service's own.

    A client that hits the gateway and a client that hits stt-stack directly
    should not have to learn two wordings for the same condition — and `code`
    tells the two conditions apart, which a shared null would not.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        unrouted = await client.post("/v1/nope", content=b"{}")
        wrong_method = await client.get(SPEECH)

    assert unrouted.status_code == 404
    assert unrouted.json()["error"]["code"] == "unknown_url"
    # The routing-table sentence is this service's own and worth keeping: it is
    # the one 404 in the estate that can say where the accepted paths are
    # published.
    assert unrouted.json()["error"]["message"].startswith(
        "Invalid URL (POST /v1/nope). This gateway routes a fixed set")

    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "method_not_allowed"
    assert wrong_method.json()["error"]["message"] == \
        "Invalid URL (GET /v1/audio/speech)"


async def test_every_v1_error_carries_all_four_fields(monkeypatch, backends):
    """The omission this service carried silently for its whole life.

    `param` is required-but-NULLABLE in OpenAI's `Error` — present as JSON
    null, never absent. Three sibling services noticed voice-common built three
    keys and each vendored its own fix; this one, the process an openai-python
    client actually talks to, had none of it and nobody looked. So the
    assertion is shared with them rather than written a fifth time: it is
    voice_common.conformance.assert_four_field_envelope, and the same call runs
    in all four CIs.

    Every error this service can produce without a backend is driven here: the
    401 built by the auth middleware from outside every exception handler, an
    unrouted path, a wrong method, a body it cannot parse, and a backend that
    is not there.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       authenticate=False) as (client, _):
        assert_four_field_envelope(await client.get("/voices"))

    async with gateway(monkeypatch, stt=stt, tts=tts, long=Unreachable()) as (client, _):
        assert_four_field_envelope(await client.post("/v1/nope", content=b"{}"))
        assert_four_field_envelope(await client.get(SPEECH))
        assert_four_field_envelope(
            await client.post(SPEECH, content=b"{not json"))
        assert_four_field_envelope(
            await client.post(SPEECH, content=body(model="chatterbox")))


# --------------------------------------------------------------------- auth --
#
# The full matrix (sessions, keys, CSRF, locked mode, delegation) is in
# test_auth.py and its siblings. These are the two rules every route here
# depends on: nothing reaches a backend without a credential, and /health is
# the one path that answers without one.


async def test_a_request_with_no_credential_never_reaches_a_backend(monkeypatch, backends):
    """Default deny (D48): there is no setting under which a route is open."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       authenticate=False) as (client, _):
        response = await client.get("/voices")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthenticated"
    assert not tts.seen


@pytest.mark.parametrize("path", ["/health", "/health/"])
async def test_health_needs_no_key_with_or_without_the_slash(monkeypatch, backends, path):
    """The bug tts-stack paid for once.

    Middleware runs before routing, so /health/ never reached the 307 that
    would have normalised it, and a probe written that way went permanently
    unhealthy the moment keys were set.
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       authenticate=False) as (client, _):
        response = await client.get(path, follow_redirects=True)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ------------------------------------------------------------------- health --


async def test_health_reports_every_backend_to_a_key_that_may_read_it(monkeypatch, backends):
    """One call, every answer -- the main justification beyond routing -- for
    a caller holding health:detail. The probes carry no credential: a
    backend's /health is the one path its guard leaves open."""
    stt, tts, long = backends
    stt.reply = lambda r: (200, {"content-type": "application/json"},
                           b'{"status":"ok","model":"parakeet"}')
    tts.reply = lambda r: (200, {"content-type": "application/json"},
                           b'{"status":"ok","voices":54}')
    long.reply = lambda r: (200, {"content-type": "application/json"},
                            b'{"status":"ok","model_loaded":false,"queued":0}')

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.get("/health")

    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["gateway"] == {"locked": []}
    assert payload["backends"]["stt"]["health"]["model"] == "parakeet"
    assert payload["backends"]["tts"]["health"]["voices"] == 54
    assert payload["backends"]["tts_long"]["health"]["model_loaded"] is False
    assert all("authorization" not in b.last["headers"]
               and "x-calliope-identity" not in b.last["headers"] for b in (stt, tts, long))
    assert [b.last["path"] for b in (stt, tts, long)] == ["/health"] * 3


async def test_the_engine_detail_the_voice_picker_reads_reaches_a_speech_user(
        monkeypatch, backends):
    """THE VOICE PICKER'S ONLY SUPPLY LINE RUNS THROUGH /health.

    An engine whose speakers are baked into its weights has a voice list, and
    there is no route to fetch it: tts-long's own `GET /voices` is deliberately
    unrouted (NOT_ROUTED below says why). So the names ride the health body.

    Health is an allowlist now (D50), which is exactly the change this test
    used to fence against: a field list in front of tts-long's answer. So the
    fence moves to the list itself. Everything a speech tab reads from an
    engine -- label, default, languages, controls, voices, the sample rate and
    each lane's readiness -- reaches a caller with health:read, and where the
    runner is and how it is set up does not.
    """
    stt, tts, long = backends
    detail = {
        "status": "ok", "model_loaded": False, "queued": 0,
        "engines": {
            "preset-engine": {"label": "Preset Engine", "default": False,
                              "reference_audio": False, "native_sample_rate": 24000,
                              "languages": ["de", "pt"],
                              "controls": ["cfg_alpha", "flow_steps"],
                              "voices": [{"name": "de_female", "language": "de"},
                                         {"name": "pt_male", "language": "pt"}],
                              "min_reference_seconds": 0.0,
                              "local": {"ready": False, "why": "not here",
                                        "resident": False},
                              "runner": {"ready": True, "why": None,
                                         "service": "runner-host:9000",
                                         "settings": {"flow_steps": 32}}},
        },
        "runner": {"host": "runner-host"},
        "brand_new_field": "nobody has decided which tier this is",
    }
    long.reply = lambda r: (200, {"content-type": "application/json"},
                            json.dumps(detail).encode())

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       authenticate=False) as (client, main):
        from conftest import bearer, make_key, make_user
        reader = make_key(make_user("sam", role="speech"),
                          scopes={"health:read", "models:read"})
        shown = (await client.get("/health", headers=bearer(reader))).json()
        operator = make_key(make_user("ana"), scopes={"health:detail"})
        full = (await client.get("/health", headers=bearer(operator))).json()

    engine = shown["backends"]["tts_long"]["health"]["engines"]["preset-engine"]
    expected = detail["engines"]["preset-engine"]
    for field in ("label", "default", "languages", "controls", "voices",
                  "native_sample_rate", "reference_audio", "min_reference_seconds"):
        assert engine[field] == expected[field], field
    assert engine["local"] == {"ready": False, "resident": False}
    assert engine["runner"] == {"ready": True}
    tts_long = shown["backends"]["tts_long"]
    assert "runner" not in tts_long["health"] and "url" not in tts_long
    assert "brand_new_field" not in json.dumps(shown)
    # The operator's tier has the runner and the lane settings, and still
    # nothing nobody has placed in a tier.
    assert full["backends"]["tts_long"]["health"]["runner"] == {"host": "runner-host"}
    assert full["backends"]["tts_long"]["health"]["engines"]["preset-engine"]["runner"][
        "settings"] == {"flow_steps": 32}
    assert "brand_new_field" not in json.dumps(full)


async def test_a_speech_user_never_sees_the_home_assistant_glossary_in_health(
        monkeypatch, backends):
    """D34: only a caller stt would let use the profile is told it exists."""
    stt, tts, long = backends
    stt.reply = lambda r: (200, {"content-type": "application/json"}, json.dumps(
        {"status": "ok", "glossaries": ["tech", "home-assistant"]}).encode())
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long,
                       authenticate=False) as (client, main):
        from conftest import bearer, make_key, make_user
        speech = make_key(make_user("sam", role="speech"), scopes={"health:read"})
        ha = make_key(make_user("ana"), scopes={"health:read", "glossaries:ha"})
        hidden = (await client.get("/health", headers=bearer(speech))).json()
        named = (await client.get("/health", headers=bearer(ha))).json()

    assert hidden["backends"]["stt"]["health"]["glossaries"] == ["tech"]
    assert named["backends"]["stt"]["health"]["glossaries"] == ["tech", "home-assistant"]


async def test_health_answers_200_while_a_sibling_is_down(monkeypatch, backends):
    """A container must not be restarted because a sibling is restarting.

    The TrueNAS healthcheck for this container calls this endpoint. A 503 here
    for tts-long's cold start would have the orchestrator kill the gateway.
    Read `status`, not the code.
    """
    stt, tts, _ = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=Unreachable()) as (client, _):
        response = await client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["backends"]["tts_long"]["reachable"] is False
    assert "ConnectError" in payload["backends"]["tts_long"]["error"]
    # The two that are up are still reported.
    assert payload["backends"]["stt"]["reachable"] is True
    assert payload["backends"]["tts"]["http_status"] == 200


async def test_a_backend_that_is_loading_still_counts_as_answering(monkeypatch, backends):
    """It answered, and its own body says "loading" for anyone reading past the first field."""
    stt, tts, long = backends
    long.reply = lambda r: (200, {"content-type": "application/json"},
                            b'{"status":"ok","model_loaded":false}')
    tts.reply = lambda r: (200, {"content-type": "application/json"},
                           b'{"status":"loading","voices":0}')

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        payload = (await client.get("/health")).json()

    assert payload["status"] == "ok"
    assert payload["backends"]["tts"]["health"]["status"] == "loading"


async def test_health_survives_a_backend_answering_html(monkeypatch, backends):
    """Something else on the port, or a proxy in the way. Say so, do not raise."""
    stt, tts, long = backends
    long.reply = lambda r: (502, {"content-type": "text/html"}, b"<html>bad gateway")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        payload = (await client.get("/health")).json()

    assert payload["status"] == "degraded"
    assert payload["backends"]["tts_long"]["http_status"] == 502
    assert payload["backends"]["tts_long"]["health"] == {}


async def test_health_does_not_wait_forever_on_a_wedged_backend(monkeypatch, backends):
    """One call must answer even when a container has stopped talking."""
    stt, tts, _ = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=Slow()) as (client, _):
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["backends"]["tts_long"]["reachable"] is False


# --------------------------------------------------------------------- logs --


async def test_one_log_line_per_request_carries_the_model_and_the_rate(monkeypatch, backends, caplog):
    """The entire observability budget. `grep` has to be able to answer

    "why did that take nine minutes", so the line names the route, the backend
    it chose, the model string as the client sent it, the upstream status, the
    duration this process observed, and the backend's own realtime factor.
    """
    stt, tts, long = backends
    tts.reply = lambda r: (200, {"content-type": "audio/mpeg",
                                 "x-realtime-factor": "1.4"}, b"ID3")

    with caplog.at_level("INFO", logger="voice-gateway"):
        async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
            await client.post(SPEECH, content=body(model="tts-1"))

    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("route=")]
    assert len(lines) == 1
    for fragment in ("route=/v1/audio/speech", "backend=tts-stack", "model=tts-1",
                     "status=200", "duration=", "rtf=1.4"):
        assert fragment in lines[0]


async def test_a_client_that_hangs_up_mid_upload_still_writes_its_line(monkeypatch, backends, caplog):
    """A 499 used to return silently, which made "one line per request" false.

    /v1/audio/speech is the one route that reads the whole body before it can
    route, so it is the one route that can lose the client before a backend is
    ever chosen. It answered 499 and logged nothing, so the disconnects were
    invisible to the grep that is this service's entire observability budget.

    Driven as raw ASGI rather than through httpx: the disconnect has to arrive
    as an `http.disconnect` message while the handler is reading the body, and
    a transport that delivers a complete request cannot produce that.
    """
    stt, tts, long = backends
    sent: list[dict] = []

    async def receive():
        # The client vanished before any body arrived. Starlette turns this
        # message into ClientDisconnect inside request.body().
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "path": SPEECH, "raw_path": SPEECH.encode(),
        "query_string": b"", "root_path": "", "scheme": "http",
        "headers": [(b"host", b"gateway.test"),
                    (b"content-type", b"application/json"),
                    (b"content-length", b"64")],
        "client": ("10.0.0.9", 51234), "server": ("gateway.test", 8080),
    }

    with caplog.at_level("INFO", logger="voice-gateway"):
        async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, main):
            scope["headers"].append(
                (b"authorization", client.headers["authorization"].encode()))
            await main.app(scope, receive, send)

    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [499]
    # Nothing was forwarded: the request died before a backend was picked.
    assert not tts.seen and not long.seen

    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("route=")]
    assert len(lines) == 1
    assert "status=client-disconnect" in lines[0]
    assert "route=/v1/audio/speech" in lines[0]


async def test_a_repeated_response_header_is_not_collapsed_into_one(monkeypatch, backends):
    """Two Links must stay two, not become `<a>, <b>`.

    The proxy built its response headers with a dict comprehension over httpx's
    Headers.items(), which is a Mapping view: it joins duplicates with a comma.
    Measured — httpx.Headers([("set-cookie","a=1"),("set-cookie","b=2")])
    .items() yields 'a=1, b=2', which is one malformed value rather than two
    good ones. (Set-Cookie was the example; a backend may no longer send one
    at all, see the test below.)
    """
    stt, tts, long = backends
    tts.reply = lambda record: (200, [("content-type", "audio/mpeg"),
                                      ("link", "</a>; rel=a"),
                                      ("link", "</b>; rel=b"),
                                      ("vary", "accept"),
                                      ("vary", "origin")], b"ID3audio")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(SPEECH, content=body())

    assert response.status_code == 200
    assert response.content == b"ID3audio"
    pairs = response.headers.multi_items()
    assert ("link", "</a>; rel=a") in pairs and ("link", "</b>; rel=b") in pairs
    assert ("vary", "accept") in pairs and ("vary", "origin") in pairs
    # The failure this guards against, stated as the thing that must not appear.
    assert not any(", " in v for k, v in pairs if k in ("link", "vary"))


async def test_a_repeated_request_header_reaches_the_backend_intact(monkeypatch, backends):
    """The same collapsing, in the other direction and with the other cause.

    Starlette's Headers.items() yields every pair, so a dict comprehension did
    not comma-join here — it let the LAST duplicate overwrite the first and
    dropped one silently. Two Accept headers must arrive as two.
    """
    stt, tts, long = backends

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(
            SPEECH, content=body(),
            headers=[("content-type", "application/json"),
                     ("accept", "audio/mpeg"), ("accept", "audio/wav")])

    assert response.status_code == 200
    assert len(tts.seen) == 1
    accepted = [v for k, v in tts.last["raw_headers"] if k == "accept"]
    assert accepted == ["audio/mpeg", "audio/wav"]


async def test_credentials_and_forwarding_headers_never_cross_in_either_direction(
        monkeypatch, backends):
    """D51. A cookie or a caller's own X-Calliope-Identity or X-Forwarded-For
    reaching a backend would be a credential in its log, or an identity and
    an address the caller chose. And a backend has no business setting a
    cookie on the origin the session cookie lives on."""
    stt, tts, long = backends
    tts.reply = lambda record: (200, [("content-type", "audio/mpeg"),
                                      ("set-cookie", "planted=1; Path=/"),
                                      ("access-control-allow-origin", "*")], b"ID3")

    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, _):
        response = await client.post(
            SPEECH, content=body(),
            headers={"content-type": "application/json", "cookie": "a=1",
                     "x-calliope-identity": "v1.1.forged.forged",
                     "x-calliope-delegation": "forged", "x-forwarded-for": "203.0.113.9",
                     "x-forwarded-host": "evil.example", "forwarded": "for=203.0.113.9"})

    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert "access-control-allow-origin" not in response.headers
    seen = tts.last["raw_headers"]
    names = [k for k, _ in seen]
    for dropped in ("cookie", "x-calliope-delegation", "x-forwarded-host", "forwarded"):
        assert dropped not in names, dropped
    assert [v for k, v in seen if k == "x-forwarded-for"] == ["192.0.2.1"]
    identities = [v for k, v in seen if k == "x-calliope-identity"]
    assert len(identities) == 1 and "forged" not in identities[0]



# ------------------------------------------------------------- the allowlists --
#
# TWO TABLES HAVE TO AGREE BEFORE ONE REQUEST ARRIVES, and neither can see the
# other:
#
#   this service's routes        what may reach a backend, and with which scope
#   each backend's own routes    what it answers
#
# and the page has to issue only requests the first one carries. (There was a
# third: voice-ui's PROXIED, which forwarded the page's calls back here under
# /ui/api/. The page now calls this service directly, with its session.)
#
# Every failure this section exists for has the same shape: a path already
# present under one method, a second method added to one table and not the
# other, and a 405 that shows up only against the deployed stack. It happened
# with PUT /glossaries/{name} and with DELETE /jobs/{job_id}/audio, and the
# test meant to catch the second could not, because it compared sets of METHOD
# STRINGS: DELETE was already in both tables for another path, so the missing
# pair added no method and the assertion stayed green through the whole
# failure.
#
# The tests below compare PAIRS, and they come at the tables from all three
# sides: what the page asks for, what the backends answer, and what the
# tables claim exists.
#
# THEY ASSERT AND NEVER SKIP. The test they replace called pytest.skip when a
# sibling service was absent from the checkout, which reads as a pass and
# asserts nothing. The services are separately deployable; this repository
# still holds all five, so a missing one is a broken checkout and must say so.

# parents[0] tests, [1] gateway, [2] services. Getting this wrong used to make
# the old test SKIP; here it fails, loudly, on the first assertion it reaches.
SERVICES = Path(__file__).resolve().parents[2]
UI_PAGE = SERVICES / "ui" / "app" / "static" / "ui.html"

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH"})

# Routes a backend answers that deliberately never reach a browser, keyed by
# the service that answers them because two backends answer /voices. Each entry
# carries the reason it is exempt: an exemption with no reason is how a route
# that SHOULD be routed gets quietly parked here.
#
# /health is absent from this table and so are /docs and /openapi.json, because
# none of the three is a route anyone declares -- health comes from
# voice_common.health with the path as a variable, and the other two are
# FastAPI's own and already off in every service. The reader below sees
# decorated paths and literal ones, so naming them here would be three entries
# that exempt nothing.
#
# An entry may name a route that has not landed yet. That is deliberate: this
# table is written where the routing decision is made, not where the route is,
# and a new backend route arriving with its exemption already recorded is the
# outcome this whole section is for.
NOT_ROUTED: dict[tuple[str, str, str], str] = {
    # POST /v1/audio/translations WAS EXEMPT HERE AND THE EXEMPTION WAS WRONG.
    # It read "Parakeet refuses translation, so the route exists only to say so
    # in the OpenAI envelope. Routing it would publish a 400." That makes the
    # route table depend on which checkpoint the STT container loaded: under
    # STT_MODEL=whisper the route works, and the exemption hid it. It is routed
    # now, in all three tables, and services/stt answers either the translation
    # or its own 400 naming the engine.
    ("tts-long", "GET", "/voices"):
        "the voice list a caller wants is tts-stack's, and that is the one "
        "routed. Two backends answering the same path is why this table is "
        "keyed by service.",
    ("tts-long", "POST", "/runs"):
        "service-to-service. stt and tts post a finished run record through "
        "the gateway's internal listener, with runs:write (internal.py); the "
        "browser must never be able to write a row into the history it is "
        "reading.",
    ("satellites", "GET", "/satellites/{nid}/listen"):
        "opening a microphone is a POST (D15, H3): a GET is what an <img> or a "
        "link on a sibling site can make a browser send. The gateway routes "
        "the POST only.",
    ("satellites", "PUT", "/satellites/secrets"):
        "removed (D47): the Satellites tab's key boxes write to "
        "PUT /admin/secrets/{name}, the one secret store.",
}

# Routes the gateway serves, behind their scopes, that the page deliberately
# never calls. Same rule as above, every entry carries its reason.
NOT_ON_PAGE: dict[tuple[str, str, str], str] = {
    ("satellites", "POST", "/satellites/{nid}/inject"):
        "a test hook: a recorded clip through a satellite's wake word, endpoint "
        "and routing path, for a script verifying the pipeline with nobody in "
        "earshot. A button for it on the page would be one press from a real "
        "rule acting on a clip, with Home Assistant on the other end.",
    ("satellites", "POST", "/satellites/{nid}/ptt"):
        "for Home Assistant's integration (clients/home-assistant): push-to-talk "
        "from an automation or a dashboard. On the page the satellite's own "
        "PLAY button is the push-to-talk, and a remote one would open a "
        "microphone in a room the person pressing it is not in.",
    ("satellites", "POST", "/satellites/{nid}/media"):
        "Home Assistant's play_media and announcements, converted by its own "
        "ffmpeg to the satellite's format before they are sent. The page has "
        "Say and Stop, and nothing to convert with.",
    ("satellites", "POST", "/satellites/{nid}/media/stop"):
        "Home Assistant's media_stop. The page's Stop is /flush, which stops "
        "the media as well.",
    ("satellites", "GET", "/satellites/telemetry/clips/{name}"):
        "for tuning and retraining; not shown on the page. The audio of a wake "
        "word the hub's double-check did not hear, named in a wake record, for "
        "whoever retrains that word's model from the records.",
}

# What this service answers itself, with no backend behind it. Without these
# the reverse test reads a correct allowlist entry as pointing at nothing.
#
# Spelled with real parameter names and run through `_pattern` at the point of
# comparison, like both other tables. Writing `{p}` in here directly would work
# and would be the one table in the fence whose entries could not be pasted from
# a route declaration -- which is how a reader stops trusting it.
ANSWERED_HERE = frozenset({
    ("GET", "/v1/models"),
    # Retrieve-model, indexed off the very list /v1/models publishes. No
    # backend holds it, and no backend should: the names are a property of this
    # service's routing contract.
    ("GET", "/v1/models/{model_id}"),
    # The chat surface. It reaches stt-stack for a transcription, but it is not
    # a PROXY of any backend route -- the request is synthesised here and the
    # answer is wrapped here -- so no backend declares a path that matches it.
    ("POST", "/v1/chat/completions"),
    ("GET", "/health"),
})


def _pattern(path: str) -> str:
    """`/jobs/{job_id}` and `/jobs/{id}` are the same route.

    A path parameter's NAME is a local variable, not part of the wire
    contract. Comparing the raw strings would fail this fence for a rename in
    tts-long that no client could observe.
    """
    return re.sub(r"\{[^{}]*\}", "{p}", path)


def _declared_routes(service: str) -> set[tuple[str, str]]:
    """Every (METHOD, path) a service declares, read from source, never imported.

    AST RATHER THAN IMPORT, and it is not fastidiousness: importing tts-long's
    app loads Chatterbox, which is 6.5 GB of model and a CUDA probe inside a
    unit test. It also lets this read a service that is not installed in this
    environment, which is the normal case -- each service has its own venv.

    Routers are followed, so stt's /v1 prefix and voice-ui's ingest routes are
    both visible. A route mounted through include_router is exactly the shape
    that hides from anyone grepping for `@app.`.
    """
    app_dir = SERVICES / service / "app"
    assert app_dir.is_dir(), (
        f"services/{service}/app is missing from this checkout, so this fence "
        "cannot see what that service answers.")

    routes: set[tuple[str, str]] = set()
    for module in sorted(app_dir.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        # One router per module in this estate, so its prefix is the module's.
        # ITS NAME IS WHATEVER IT IS ASSIGNED TO. This reader once knew only
        # `router`, and services/satellites/app/router.py names its APIRouter
        # `routes` (the module is already called router): its three routes
        # were invisible here, so the fence could not have said they were
        # missing from either table.
        prefix = ""
        owners = {"app", "router"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "APIRouter":
                for keyword in node.keywords:
                    if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                        prefix = keyword.value.value
            if (isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", "") == "APIRouter"):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                owners |= {t.id for t in targets if isinstance(t, ast.Name)}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                attribute = decorator.func
                if not isinstance(attribute, ast.Attribute):
                    continue
                method = attribute.attr.upper()
                owner = getattr(attribute.value, "id", "")
                if method not in HTTP_METHODS or owner not in owners:
                    continue
                if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                    continue
                path = decorator.args[0].value
                routes.add((method, path if owner == "app" else prefix + path))
    assert routes, f"read no routes at all out of services/{service}/app"
    return routes


def _gateway_routes(module) -> set[tuple[str, str]]:
    """What this service routes to a backend, read off the live app.

    app.routes rather than the source, because it is the table Starlette
    matches against and a 405 is what happens when a request misses it. HEAD
    is dropped: Starlette adds one free beside every GET, and nothing in this
    estate is asked for with it.

    The /ui/* family is subtracted -- those go to voice-ui, and
    test_no_allowlist_entry_points_at_nothing checks them against voice-ui's
    own routes instead.
    """
    to_ui = {(method, path) for method, path, _ in module.UI_PATHS}
    routes: set[tuple[str, str]] = set()
    for route in module.app.routes:
        for method in getattr(route, "methods", None) or ():
            if method in HTTP_METHODS and (method, route.path) not in to_ui:
                routes.add((method, route.path))
    # The emptiness guard every reader here carries. This one feeds a
    # comprehension that produces NOTHING when it goes blank, which is how a
    # fence stops fencing without anybody being told.
    assert routes, "read no routes at all off the gateway app"
    return routes


def _everything_routed(module) -> set[tuple[str, str]]:
    """Every (METHOD, pattern) a browser can reach through this service, /ui/* included."""
    return {(method, _pattern(path)) for route in module.app.routes
            for method in getattr(route, "methods", None) or ()
            if method in HTTP_METHODS for path in (route.path,)}


# ---------------------------------- reading the page's own requests -----------
#
# The page is one HTML file with its JavaScript inline: no module graph to walk
# and no bundler output to read. What follows is a scanner rather than a
# parser -- enough JavaScript to find a call, its first argument and its
# `method`, and nothing beyond that.


def _skip_string(text: str, i: int) -> int:
    """The index just past the string literal starting at `i`.

    `${...}` inside a template literal is brace-counted rather than walked
    over, because a hole may hold a call whose arguments hold anything.
    """
    quote = text[i]
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        if quote == "`" and text[i] == "$" and text[i + 1:i + 2] == "{":
            depth = 0
            while i < len(text):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
        i += 1
    raise AssertionError("unterminated string literal in ui.html")


def _closing(text: str, opening: int) -> int:
    """The index of the bracket that closes the one at `opening`."""
    depth = 0
    i = opening
    while i < len(text):
        character = text[i]
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
            if depth == 0:
                return i
        elif character in "\"'`":
            i = _skip_string(text, i)
            continue
        i += 1
    raise AssertionError("unbalanced brackets in ui.html")


def _first_argument(arguments: str) -> tuple[str, str]:
    """Split an argument list at its first top-level comma."""
    depth = 0
    i = 0
    while i < len(arguments):
        character = arguments[i]
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character in "\"'`":
            i = _skip_string(arguments, i)
            continue
        elif character == "," and depth == 0:
            return arguments[:i], arguments[i + 1:]
        i += 1
    return arguments, ""


_TEMPLATE_HOLE = re.compile(r"\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")


def _route_of(expression: str) -> str:
    """A path EXPRESSION reduced to the one route it can reach.

    Literal characters are kept and everything computed collapses to `{p}`, so
    `"/glossaries/" + encodeURIComponent(name)` and `` `/jobs/${id}/audio` ``
    come out spelled the way the allowlists spell them. The query string is
    cut: ?force=true and ?token=... are arguments to a route, not routes, and
    both proxies append request.url.query without consulting any table.

    A TRAILING HOLE THAT DOES NOT BEGIN A SEGMENT IS A QUERY STRING, and is cut
    with it. `json("/jobs" + (query ? "?" + query : ""))` builds its own `?`
    inside the computed half, so the cut above cannot see one -- and the route
    is /jobs either way. The hole has to be at the end and NOT after a slash: a
    `{p}` mid-path is a segment this scanner could not read, and it stays in so
    the assertion fails rather than quietly matching a shorter route.
    """
    parts: list[str] = []
    i = 0
    while i < len(expression):
        character = expression[i]
        if character in "\"'`":
            end = _skip_string(expression, i)
            literal = expression[i + 1:end - 1]
            parts.append(_TEMPLATE_HOLE.sub("{p}", literal)
                         if character == "`" else literal)
            i = end
            continue
        if character in " \t\r\n+":
            i += 1
            continue
        # A call, an identifier or a parenthesised group: one opaque segment.
        depth = 0
        while i < len(expression):
            character = expression[i]
            if character in "([{":
                depth += 1
            elif character in ")]}":
                depth -= 1
            elif depth == 0 and character in "\"'`+":
                break
            i += 1
        parts.append("{p}")
    route = re.sub(r"(\{p\})+", "{p}", "".join(parts).split("?")[0])
    return re.sub(r"(?<!/)\{p\}$", "", route)


def _helper_spans(source: str) -> list[tuple[int, int]]:
    """Where api() and json() are DEFINED, so their bodies are not read as uses.

    json() calls api(path, options) and api() calls fetch(target, ...): both
    forward a variable that names no route of its own. Counted as call sites
    they would turn every literal ever assigned to `path` anywhere in the file
    into a GET the page never issues.
    """
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"\basync\s+function\s+(?:api|json)\s*\(", source):
        arguments = _closing(source, match.end() - 1)
        body = source.index("{", arguments)
        spans.append((match.start(), _closing(source, body) + 1))
    return spans


_CALL = re.compile(r"(?<![\w.$])(?:api|json|fetch)\s*\(")


def _page_requests() -> set[tuple[str, str]]:
    """Every (METHOD, route) the page can issue, read out of ui.html.

    api() and json() are the two seams every proxied call goes through, and
    bare fetch() is read as well because /ui/health and /ui/config still use
    it. A URL assigned to a media element's `src` is NOT read here: /ui/media
    is the only one and test_the_media_relay_is_routed stands over it already.
    """
    assert UI_PAGE.exists(), (
        f"{UI_PAGE} is missing from this checkout, so the one assertion that "
        "catches a 405 from the direction a person hits it cannot run.")
    source = UI_PAGE.read_text(encoding="utf-8")
    spans = _helper_spans(source)
    assert spans, "could not find api() or json() in ui.html"

    requests: set[tuple[str, str]] = set()
    for match in _CALL.finditer(source):
        if any(start <= match.start() < end for start, end in spans):
            continue
        first, rest = _first_argument(_balanced(source, match.end() - 1))
        first = first.strip()
        if not first:
            continue
        method = re.search(r"""method\s*:\s*["']([A-Za-z]+)["']""", rest)
        method = method.group(1).upper() if method else "GET"

        if first.isidentifier():
            # `path` is chosen in a branch just above the call -- /speak or
            # /v1/audio/speech, the native transcribe route or the OpenAI one.
            # Every literal it is ever given is a route that call can reach, so
            # all of them are checked.
            line = source.count("\n", 0, match.start()) + 1
            assigned = re.findall(
                r"\b" + re.escape(first) + r"""\s*=\s*(["'`][^"'`]*["'`])""",
                source)
            assert assigned, (
                f"ui.html:{line} makes a request with the variable `{first}` "
                "and this fence cannot tell which route that is. Give it a "
                "literal, or the allowlists have a hole nothing here can see.")
            requests.update((method, _route_of(literal)) for literal in assigned)
            continue
        if re.match(r"""["'`]\s*/""", first):
            requests.add((method, _route_of(first)))
        # Anything else is an absolute URL or a blob and reaches no table here.
    return requests


def _balanced(text: str, opening: int) -> str:
    """The text between `opening` and the bracket that closes it."""
    return text[opening + 1:_closing(text, opening)]


def test_every_request_the_page_issues_has_a_route():
    """THE DEFECT THIS PREVENTS, reproduced against the deployed stack: the
    Jobs tab's "delete the audio" button died with 405 method_not_supported.

    tts-long had answered DELETE /jobs/{id}/audio all along and the tables in
    front of it carried only the GET on that path. The test this replaces read
    the tables as sets of METHOD STRINGS, so the missing pair added no method
    they did not already hold and it stayed green.

    This one starts from the page instead: every call site in ui.html, the
    method it sends and the route it can reach. The page calls this service's
    paths directly now, so every one of them must be a route here.
    """
    from app import main as gateway_main

    page = _page_requests()
    assert page, "read no requests at all out of ui.html"

    missing = sorted(page - _everything_routed(gateway_main))
    assert not missing, (
        "the page issues these and this service does not route them, which is "
        f"a 404 or a 405 in the browser and a line in no log: {missing}")


def test_every_backend_route_is_routed_or_named_as_unrouted():
    """A route a backend answers is either reachable or exempt on the record.

    The failure this prevents is the quiet one: tts-long grows a route, the
    page grows a control for it, and the table in between gets written by
    whoever remembers. Everything a backend answers has to be accounted for
    here, and an exemption has to say why in NOT_ROUTED -- so "we never routed
    it" stops being something you find out from a 405.
    """
    from app import main as gateway_main

    gateway = {(method, _pattern(path))
               for method, path in _gateway_routes(gateway_main)}
    page = _page_requests()

    unreachable: list[str] = []
    for service in ("stt", "tts", "tts-long", "satellites"):
        for method, path in sorted(_declared_routes(service)):
            if (service, method, path) in NOT_ROUTED:
                continue
            pair = (method, _pattern(path))
            if pair not in gateway:
                unreachable.append(
                    f"{service} answers {method} {path} and this service does "
                    "not route it")
            elif (service, method, path) in NOT_ON_PAGE and pair in page:
                unreachable.append(
                    f"{service} answers {method} {path}, which NOT_ON_PAGE "
                    "keeps off the page, and ui.html calls it")
    assert not unreachable, "\n".join(unreachable) + (
        "\n\nRoute it here with its scope, or name it in NOT_ROUTED with the "
        "reason it never reaches a browser.")


def test_no_allowlist_entry_points_at_nothing():
    """The same comparison the other way round, which catches the other rot.

    An entry left behind after a route is renamed forwards a request to a
    backend 404, and from the outside that reads exactly like a broken
    backend. Router prefixes are followed on both sides -- stt's /v1 and
    voice-ui's ingest router -- because a route visible only through an
    include_router is the one this would otherwise report as missing when it
    is there.
    """
    from app import main as gateway_main

    backends = {(method, _pattern(path))
                for service in ("stt", "tts", "tts-long", "satellites")
                for method, path in _declared_routes(service)}
    voice_ui = {(method, _pattern(path))
                for method, path in _declared_routes("ui")}
    # Signing in, the account and the admin views are this service's own.
    answered = {(method, _pattern(path)) for method, path in ANSWERED_HERE} | {
        (method, _pattern(route.path))
        for router in (gateway_main.routes_auth.router, gateway_main.routes_admin.router,
                       gateway_main.routes_secrets.admin_router)
        for route in router.routes for method in route.methods}

    dangling = [f"this service routes {method} {path} and no backend answers it"
                for method, path in sorted(_gateway_routes(gateway_main))
                if (method, _pattern(path)) not in backends | answered]
    dangling += [f"UI_PATHS carries {method} {path} and voice-ui answers no "
                 "such route"
                 for method, path, _ in sorted(gateway_main.UI_PATHS)
                 if (method, _pattern(path)) not in voice_ui]
    assert not dangling, "\n".join(dangling)


def test_the_fence_is_reading_the_real_services_and_not_an_empty_set():
    """THE FENCE'S OWN FAILURE MODE, WHICH IS SILENCE.

    Every assertion in this section is of the form "nothing is missing", and
    that sentence is also what a reader that found nothing at all says.
    `SERVICES` is `parents[2]`; the version this replaced pointed one level
    off, could not find a sibling service, and called `pytest.skip` -- which
    reads as a pass in every report and asserts nothing. It is asserted here
    rather than left to the readers' own guards, because a skip is the outcome
    nobody reads.
    """
    assert SERVICES.name == "services", (
        f"SERVICES resolved to {SERVICES}, which is not the services directory. "
        "Every reader below would then find nothing and every assertion in this "
        "section would pass by default.")
    for service in ("gateway", "stt", "tts", "tts-long", "ui"):
        assert (SERVICES / service / "app").is_dir(), (
            f"services/{service}/app is not where this fence is looking")


def test_the_route_reader_follows_an_apirouter_whatever_it_is_called():
    """services/satellites/app/router.py names its APIRouter `routes`, and a
    reader that knew only `router` saw none of its three routes: the fence
    above could not have reported them missing."""
    satellites = _declared_routes("satellites")
    for pair in (("GET", "/satellites/routing"), ("PUT", "/satellites/routing"),
                 ("POST", "/satellites/routing/test")):
        assert pair in satellites, (
            f"the reader does not see {pair} in services/satellites")


async def test_the_wake_word_routes_reach_the_hub_and_are_not_taken_for_a_satellite_id(
        monkeypatch):
    """The page assigns wake words to satellites with a GET and a PUT on
    /satellites/wake-words. Matched as /satellites/{nid}, the PUT would be a
    405 here, since no PUT is routed under that pattern, and the GET would
    only work by accident. Both have to arrive at the hub on their own path,
    the PUT with its body intact."""
    hub = MockBackend("voice-satellites")
    body = {"words": [{"name": "alexa", "threshold": 0.6, "satellites": ["020000000001"]}]}
    async with gateway(monkeypatch, satellites=hub) as (client, _):
        got = await client.get("/satellites/wake-words")
        put = await client.put("/satellites/wake-words", json=body)
    assert (got.status_code, put.status_code) == (200, 200)
    assert [(r["method"], r["path"]) for r in hub.seen] == [
        ("GET", "/satellites/wake-words"), ("PUT", "/satellites/wake-words")]
    assert json.loads(hub.seen[1]["body"]) == body


class Clocked(httpx.AsyncBaseTransport):
    """voice-satellites, keeping the timeouts each request was sent under."""

    def __init__(self) -> None:
        self.timeouts: dict[str, dict] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.timeouts[request.url.path.rsplit("/", 1)[-1]] = request.extensions["timeout"]
        await request.aread()
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(b"{}"))


async def test_an_announcement_has_until_it_has_played_to_be_answered_but_a_stalled_upload_does_not(
        monkeypatch):
    """An announcement is answered once it has been heard, after whatever the
    satellite already had queued: up to the hub's 120 s of its own, behind a
    reply or another. Under the 120 s every other satellite route has, a long
    one would be a 504 while it still played, and Home Assistant would call
    it failed.
    Its writes keep the 120 s, so an upload the hub stops taking still ends."""
    hub = Clocked()
    async with gateway(monkeypatch, satellites=hub) as (client, main):
        media = await client.post("/satellites/020000000002/media?announce=1", content=b"RIFF")
        say = await client.post("/satellites/020000000002/say", json={"text": "Hello."})
    assert (media.status_code, say.status_code) == (200, 200)
    assert hub.timeouts["media"]["read"] == main.SATELLITES_MEDIA_TIMEOUT == 300
    assert hub.timeouts["media"]["write"] == main.SATELLITES.read_timeout == 120
    assert hub.timeouts["say"]["read"] == hub.timeouts["say"]["write"] == 120


def test_a_board_on_firmware_from_before_the_rename_is_still_relayed_to_the_hub(
        monkeypatch):
    """The device socket was /nodes/ws until 2026-09-25, and a board in the
    field connects there until it is updated -- over that same socket. A
    gateway that answered only /satellites/ws would strand the board on its
    old image with USB as the only way back. Both paths are one handler, and
    both reach the hub's new path, which the hub answers as well as the old."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    main = reload_gateway(monkeypatch)
    sockets = {r.path: r.endpoint for r in main.app.routes
               if r.path in (main.SATELLITES_SOCKET, main.LEGACY_SATELLITES_SOCKET)}
    assert sockets == {"/satellites/ws": main.satellites_socket,
                       "/nodes/ws": main.satellites_socket}

    dialled: list[str] = []

    async def no_hub(target, **_):
        # Refused rather than faked end to end: what is under test is where
        # each path is relayed to; the relay itself is
        # test_the_relay_carries_frames_both_ways_and_the_hubs_close_code.
        dialled.append(target)
        raise OSError("no hub in a unit test")

    monkeypatch.setattr(main, "ws_connect", no_hub)
    with TestClient(main.app) as client:
        for path in ("/nodes/ws", "/satellites/ws"):
            with client.websocket_connect(path) as ws:
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_text()
            assert closed.value.code == 1013, path
    assert dialled == ["ws://satellites.test/satellites/ws"] * 2


def test_health_asks_the_hub_alongside_the_other_backends(monkeypatch):
    """The hub was probed after the other three had answered, so a slow hub
    and a slow backend took twice one probe's worst case (about 14 s against
    7) on the endpoint the container healthcheck waits on."""
    import asyncio

    from starlette.testclient import TestClient

    main = reload_gateway(monkeypatch)
    out, most = [0], [0]

    async def probe(backend):
        out[0] += 1
        most[0] = max(most[0], out[0])
        await asyncio.sleep(0.05)
        out[0] -= 1
        return {"reachable": True}
    monkeypatch.setattr(main, "_probe", probe)
    with TestClient(main.app, base_url=PUBLIC_ORIGIN) as client:
        key = make_key(make_user("ana"), scopes={"health:read"})
        body = client.get("/health", headers=bearer(key)).json()
    assert set(body["backends"]) == {"stt", "tts", "tts_long", "satellites"}
    assert most[0] == 4, "the probes were not asked together"


@pytest.mark.parametrize("failure", ["404", "dropped"])
def test_a_hub_that_answers_but_not_with_a_socket_is_try_again_later(monkeypatch, failure):
    """A 404 (a wrong GATEWAY_SATELLITES_URL, a proxy, an old hub) or a hub
    dropping the handshake as it restarts is no OSError: it escaped as a
    traceback, and the device saw 1006 instead of 1013."""
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidMessage, InvalidStatus
    from websockets.http11 import Response as WsResponse

    main = reload_gateway(monkeypatch)

    async def refused(target, **_):
        if failure == "404":
            raise InvalidStatus(WsResponse(404, "Not Found", Headers()))
        raise InvalidMessage("did not receive a valid HTTP response")
    monkeypatch.setattr(main, "ws_connect", refused)
    with TestClient(main.app) as client:
        with client.websocket_connect("/satellites/ws") as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
    assert closed.value.code == 1013


def test_the_relay_carries_frames_both_ways_and_the_hubs_close_code(monkeypatch):
    """The relay, end to end, against a hub on a local port: a binary and a
    text frame each way, the device's leaving closing the hub's side, and the
    hub's close code reaching the device (1008 and 1012 arrived as 1000). A
    hub gone without a close frame (crashed, OOM-killed, the network dropped)
    is 1006 to websockets, which may not be sent: the device gets 1000."""
    import asyncio
    import threading

    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from websockets.asyncio.server import serve

    closed_upstream: list[bool] = []
    ready = threading.Event()
    port: list[int] = []

    async def hub(conn):
        try:
            async for msg in conn:
                if isinstance(msg, bytes):
                    await conn.send(msg[::-1])
                elif msg == "refuse":
                    await conn.close(1008, "bad token")
                elif msg == "drop":
                    conn.transport.abort()  # no close frame
                    return
                else:
                    await conn.send(msg.upper())
        finally:
            closed_upstream.append(True)

    loop = asyncio.new_event_loop()

    async def run() -> None:
        async with serve(hub, "127.0.0.1", 0) as server:
            port.append(server.sockets[0].getsockname()[1])
            ready.set()
            await asyncio.Future()
    task = loop.create_task(run())
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        assert ready.wait(5)
        main = reload_gateway(monkeypatch)
        monkeypatch.setattr(main, "SATELLITES",
                            main.SATELLITES._replace(url=f"http://127.0.0.1:{port[0]}"))
        with TestClient(main.app) as client:
            with client.websocket_connect("/satellites/ws") as ws:
                ws.send_bytes(b"\x01\x02\x03")
                assert ws.receive_bytes() == b"\x03\x02\x01"
                ws.send_text("hello")
                assert ws.receive_text() == "HELLO"
            for _ in range(100):
                if closed_upstream:
                    break
                threading.Event().wait(0.02)
            assert closed_upstream, "the device left and the hub's side stayed open"

            with client.websocket_connect("/nodes/ws") as ws:
                ws.send_text("refuse")
                with pytest.raises(WebSocketDisconnect) as refused:
                    ws.receive_text()
            assert refused.value.code == 1008

            with client.websocket_connect("/satellites/ws") as ws:
                ws.send_text("drop")
                with pytest.raises(WebSocketDisconnect) as dropped:
                    ws.receive_text()
            assert dropped.value.code == 1000
    finally:
        loop.call_soon_threadsafe(task.cancel)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)


def test_the_device_is_only_sent_a_close_code_a_close_frame_may_carry():
    """Checked against websockets' own rule, which uvicorn's websockets
    implementation applies to the relay's close: 1005 (an empty close frame)
    and 1006 (none at all) raised ProtocolError there, a traceback in the log
    and 1006 at the device, where a plain 1000 is what it used to get."""
    from websockets.frames import Close

    from app.main import _close_code_for_device

    for code in (None, *range(0, 5100)):
        sent = _close_code_for_device(code)
        Close(sent, "").check()  # raises ProtocolError on a code it refuses
        try:
            Close(code, "").check()
        except Exception:
            assert sent == 1000, code
        else:
            assert sent == code, "a code that may be sent was not passed on"
    assert _close_code_for_device(1005) == _close_code_for_device(1006) == 1000


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/audio/translations"),
])
def test_a_route_the_backend_answers_is_in_every_table_in_front_of_it(
        method, path):
    """The pair-wise check for the routes added with this change, from both ends.

    THE TABLE AND THE BACKEND HAVE TO AGREE. POST /v1/audio/translations was
    the third instance of one defect: the backend implements it, and the
    tables in front of it did not carry the pair -- which is a 404 or a 405
    that reproduces only against the deployed stack. It is asserted on the
    PAIR rather than on the path, because the two earlier instances (PUT
    /glossaries/{name}, DELETE /jobs/{id}/audio) both added a method to a path
    that was already listed.
    """
    from app import main as gateway_main

    pair = (method, _pattern(path))
    assert pair in {(m, _pattern(p))
                    for m, p in _declared_routes("stt")
                    | _declared_routes("tts") | _declared_routes("tts-long")}, (
        f"no backend answers {method} {path}")
    assert pair in {(m, _pattern(p))
                    for m, p in _gateway_routes(gateway_main)}, (
        f"this service does not route {method} {path}")


def test_a_second_model_string_adds_no_row_to_the_route_table():
    """The claim the routing package makes and must not simply assert in prose.

    Selecting an engine is a REQUEST FIELD, not a route. The table is keyed on
    (METHOD, path), so a second model string is invisible to it -- and the
    three speech paths it can arrive on are already listed, each with its
    scope. That is the whole reason a new engine reaches the page without a
    new row or a new scope.

    It is asserted rather than believed because the opposite mistake is the one
    this estate keeps making: the two live bugs this week were both a surface
    that grew and a table that did not. If the engine ever stops being a field
    -- a /v1/audio/speech/{engine}, a per-engine job route -- this fails and
    says which table to write in.
    """
    from app import main as gateway_main

    raw = sorted(_gateway_routes(gateway_main))
    routed = {(method, _pattern(path)) for method, path in raw}
    for pair in (("POST", "/v1/audio/speech"), ("POST", "/jobs"),
                 ("GET", "/voices")):
        assert pair in routed, (
            f"the page reaches the engine selector through {pair[0]} {pair[1]} "
            "and this service does not route it")

    # READ OFF THE RAW PATHS, NOT THE PATTERNED ONES, and the first draft of
    # this test got that wrong in a way that proves the point of writing it.
    # `_pattern` rewrites every `{name}` to `{p}` on purpose -- a parameter's
    # name is a local variable and renaming it breaks no client -- so a
    # deliberately planted `/v1/audio/speech/{engine}` came through as
    # `/v1/audio/speech/{p}`, the word this was searching for had been erased,
    # and the mutation PASSED. That is the same silent-green shape as the
    # earlier fence which compared sets of method strings. Here the parameter's
    # name IS the thing being looked for, so it must survive to be looked at.
    offenders = sorted(
        f"{method} {path}" for method, path in raw
        # A path parameter named for an engine or a model: the engine promoted
        # out of the body and into the URL. `/v1/models/{model_id}` is not
        # that and must not be caught -- it is the meta route that publishes
        # the names, not one that chooses between them.
        if not path.startswith("/v1/models")
        and re.search(r"\{[^{}]*(?:engine|model)[^{}]*\}", path)
        # Or a new segment hung off one of the two speech routes, which is what
        # a per-engine variant would look like without a parameter:
        # /v1/audio/speech/turbo. /jobs is excluded because /jobs/{id} and
        # /jobs/{id}/audio are legitimate and already listed.
        or path.startswith(("/v1/audio/speech/", "/speak/")))
    assert not offenders, (
        "an engine-shaped PATH appeared in this service's routes: the engine is "
        "a request field, and a path is a row, a scope and a backend route's "
        f"worth of work: {offenders}")


async def test_an_upload_has_a_ceiling(monkeypatch, backends):
    """"STREAMED, SO IT COSTS NOTHING HERE" WAS ONLY TRUE OF HERE. The gateway
    hands the body straight to stt-stack, so its own memory stays flat whatever
    arrives -- and the service at the other end reads the clip to decode it,
    inside a container with 6 GB. One POST from any key holder on the only
    published port could take that container down, and every transcription
    with it.

    This route's own 413 message used to say so, in the chat route's refusal:
    "streams it through this gateway rather than holding it, and has no ceiling
    here".
    """
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, main):
        for path in ("/v1/audio/transcriptions", "/v1/audio/translations", "/transcribe"):
            answer = await client.post(
                path, content=b"RIFF....",
                headers={"content-length": str(main.UPLOAD_MAX_BYTES + 1)})
            assert answer.status_code == 413, f"{path} accepts any declared size"
            assert answer.json()["error"]["code"] == "upload_too_large"

    assert not stt.seen, "an oversized upload was forwarded before being refused"


async def test_the_ceiling_is_counted_and_not_merely_declared(monkeypatch, backends):
    """content-length is a claim: omit it, send chunked, and the declared size
    is no size at all. So the bytes are counted as they pass and the forward is
    abandoned mid-flight, which costs the caller their upload and this stack
    nothing."""
    stt, tts, long = backends
    async with gateway(monkeypatch, stt=stt, tts=tts, long=long) as (client, main):
        monkeypatch.setattr(main, "UPLOAD_MAX_BYTES", 1024)

        async def chunked():
            for _ in range(4):
                yield b"\0" * 512

        answer = await client.post("/v1/audio/transcriptions", content=chunked())
        assert answer.status_code == 413, \
            "an upload with no content-length is unbounded, which is the whole hole"
