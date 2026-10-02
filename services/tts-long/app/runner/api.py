"""The runner's HTTP surface: offpeak's protocol, limited to what tts-long calls.

    GET    /v1/services                                    what will run, now
    GET    /v1/status                                      the status panel's
    HEAD   /v1/assets/{sha256}                             is this clip here
    POST   /v1/assets                                      raw clip bytes
    POST   /v1/services/{svc}/jobs                         submit
    GET    /v1/services/{svc}/jobs/{id}                    poll
    GET    /v1/services/{svc}/jobs/{id}/result?artefact=   one segment's f32
    DELETE /v1/services/{svc}/jobs/{id}                    cancel

THE KEY IS CHECKED BEFORE ANYTHING ELSE RUNS, FROM THE HEADERS ALONE. As a
FastAPI dependency the check ran after the body had been read and parsed:
measured with the pinned versions, a 300 MiB POST with no key grew the process
by 1.5 GB before its 401, /openapi.json and /docs answered 200, and malformed
JSON answered 422, all without a key. `Guard` is pure ASGI and wraps the whole
app: key, then Transfer-Encoding, then Content-Length against the route's cap,
then the in-flight limit, and `receive()` is never awaited before all four pass.

EVERY ERROR IS OFFPEAK'S SHAPE, {"error": "<one sentence>", "status": <code>},
and every route reads its own body, so FastAPI's 422 never appears. tts-long
logs the first 200 bytes of whatever comes back.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from voice_common.engines import CONTROL_RANGES, WIRE_CONTROLS

from . import gpu
from .jobs import HEX64, JOB_ID, Dispatcher, KeyConflict, QueueFull, Store

log = logging.getLogger("tts-runner.api")

ONE_MIB = 1 << 20
# Above voice-ui's 25 MiB clip limit: a voice that works on the CPU lane must
# be accepted here. Everything else is a JSON document or nothing.
CAPS = {"/v1/assets": 32 * ONE_MIB}
IN_FLIGHT = 16
MAX_SEGMENTS = 2000
MAX_SEGMENT_CHARS = 2000
MAX_KEY_CHARS = 512
# How often "refused a request without a valid key" may be said.
REFUSAL_LOG_EVERY_S = 60.0


async def refuse(send, status: int, message: str, extra=()) -> None:  # noqa: ANN001
    """An error in offpeak's shape, and the connection closed behind it."""
    body = json.dumps({"error": message, "status": status}).encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"connection", b"close"), *extra]})
    await send({"type": "http.response.body", "body": body})


class Guard:
    """Every request is refused from its headers before the app sees it, or passed.

    LIFESPAN SCOPES PASS THROUGH, because the lifespan is what runs
    `Dispatcher.close()` at shutdown. Any other scope that is not HTTP (a
    websocket, which uvicorn is also told not to speak) is dropped.

    THE IN-FLIGHT LIMIT COUNTS AUTHENTICATED REQUESTS, NOT SOCKETS. uvicorn's
    `limit_concurrency` counted idle TLS connections, so sixteen silent ones
    turned every later request into a 503 without a key between them.
    """

    def __init__(self, app, key: bytes, limit: int = IN_FLIGHT) -> None:  # noqa: ANN001
        self.app = app
        self.key = key
        self.slots = asyncio.Semaphore(limit)
        self._said_at = -math.inf

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return None
        headers = dict(scope["headers"])
        scheme, _, token = headers.get(b"authorization", b"").partition(b" ")
        if not (scheme.lower() == b"bearer"
                and hmac.compare_digest(token.strip(), self.key)):
            self._said(scope)
            return await refuse(send, 401, "a valid bearer key is required",
                                [(b"www-authenticate", b"Bearer")])
        if b"transfer-encoding" in headers:
            return await refuse(send, 411, "send Content-Length, not Transfer-Encoding")
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            # h11 has already refused a malformed or repeated Content-Length;
            # this is for an app driven some other way.
            return await refuse(send, 400, "Content-Length is not a number")
        if length > CAPS.get(scope["path"], ONE_MIB):
            return await refuse(send, 413, "the body is over this route's cap")
        # locked() and then acquiring with no await between them is atomic on
        # one event loop, and a request is refused rather than queued.
        if self.slots.locked():
            return await refuse(send, 503, "too many requests in flight")
        async with self.slots:
            await self.app(scope, receive, send)
        return None

    def _said(self, scope) -> None:  # noqa: ANN001
        now = time.monotonic()
        if now - self._said_at < REFUSAL_LOG_EVERY_S:
            return
        self._said_at = now
        client = scope.get("client") or ("an unknown address",)
        log.warning("refused a request without a valid key from %s", client[0])


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message, "status": status}, status_code=status)


def _takes_language(spec) -> bool:  # noqa: ANN001
    """The same test `generate_kwargs` uses to build `language_id`."""
    return not spec.facts.language_from_voice and len(spec.languages) > 1


def honoured(spec) -> list[str]:  # noqa: ANN001
    """The fields a job body for this engine may carry, in the order said."""
    fields = ["segments", "sample_rate"]
    if _takes_language(spec):
        fields.append("language")
    fields.append("reference_sha256")
    fields += [c for c in WIRE_CONTROLS if c in spec.controls]
    return fields


def validate(spec, body: dict, store: Store) -> dict | str:  # noqa: ANN001
    """The worker's request from a job body, or the sentence that refuses it.

    EVERY FIELD IS HONOURED OR REFUSED BY NAME, read off the engine's catalogue
    row. The runner applies no defaults of its own: tts-long has resolved every
    default it wants to send, and a field it leaves out gets the model's own
    default, exactly as on its CPU lane.
    """
    service = spec.facts.runner_service
    allowed = honoured(spec)
    for name in body:
        if name not in allowed:
            return (f"unsupported parameter '{name}': {service} honours "
                    f"{', '.join(allowed)}")
    segments = body.get("segments")
    if (not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS
            or not all(isinstance(s, str) for s in segments)):
        return f"segments must be a list of 1 to {MAX_SEGMENTS} strings"
    if any(len(s) > MAX_SEGMENT_CHARS for s in segments):
        return f"a segment is over {MAX_SEGMENT_CHARS} characters; chunk it"
    rate = spec.facts.native_sample_rate
    if "sample_rate" in body:
        given = body["sample_rate"]
        if isinstance(given, bool) or not isinstance(given, int) or given != rate:
            return f"sample_rate must be {rate}, the rate {service} produces"
    language = body.get("language")
    if _takes_language(spec):
        if not isinstance(language, str) or language not in spec.languages:
            return (f"language is required and must be one of "
                    f"{', '.join(spec.languages)}")
    reference = None
    if body.get("reference_sha256") is not None:
        digest = body["reference_sha256"]
        if not isinstance(digest, str) or not store.has_asset(digest):
            return "unknown reference_sha256; POST it to /v1/assets first"
        reference = str(store.asset_path(digest))
    controls: dict[str, float] = {}
    for name in WIRE_CONTROLS:
        value = body.get(name)
        if value is None:
            continue
        kind, low, high = CONTROL_RANGES[name]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or (kind is int and not isinstance(value, int))
                or not math.isfinite(value) or not low <= value <= high):
            return f"{name} must be a number from {low} to {high}"
        controls[name] = value
    return {"segments": list(segments),
            "language": language if _takes_language(spec) else None,
            "controls": controls, "reference": reference}


def create_app(dispatcher: Dispatcher, *, services: dict, key: bytes) -> Guard:
    """The routes, wrapped in the guard. `services` is {service id: EngineSpec}."""
    store = dispatcher.store

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        dispatcher.start()
        try:
            yield
        finally:
            await run_in_threadpool(dispatcher.close)

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None,
                  lifespan=lifespan)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        said = {404: "no such route", 405: "method not allowed"}
        return _error(exc.status_code, said.get(exc.status_code, str(exc.detail)))

    def device_word() -> str:
        return "gpu" if dispatcher.device.startswith("cuda") else "cpu"

    def job_of(service: str, job_id: str):
        if service not in services or JOB_ID.fullmatch(job_id) is None:
            return None
        job = store.polled(job_id)
        return job if job is not None and job.service == service else None

    def document(job, reused: bool = False) -> dict:
        return {"job_id": job.id, "service": job.service, "status": job.status,
                "reused": reused, "artefacts": store.artefacts(job),
                "record": job.record}

    @app.get("/v1/services")
    async def list_services():
        rows = []
        running = dispatcher.engine
        for service, spec in services.items():
            ok, why = dispatcher.available(service)
            rate = spec.facts.native_sample_rate
            rows.append({
                "id": service, "known": True, "installed": True, "enabled": True,
                "ready": dispatcher.probe.state == gpu.OK,
                "device": device_word(), "available": ok,
                "unavailable_reason": why,
                "running": running == spec.id,
                "queued": store.queued_count(service),
                "manifest": {"id": service, "outputs": f"audio/pcm-f32@{rate}",
                             "interruptible": "between-units"}})
        # NO `limits`, `settings` OR PROSE besides `unavailable_reason`: the
        # panel then shows no cap rather than a cap of zero.
        return {"mode": "always-on",
                "gpu_available": dispatcher.probe.state == gpu.OK,
                "machine_state": dispatcher.machine_state(),
                "services": rows}

    @app.get("/v1/status")
    async def status():
        # offpeak's words for the state, so tts-long's panel already has a
        # sentence for every one of them.
        active = store.active_job()
        machine = dispatcher.machine_state()
        if dispatcher.probe.state == gpu.CHECKING:
            state, can_run = "checking", False
        elif dispatcher.probe.state == gpu.FAILED:
            state, can_run = "no gpu", False
        elif machine == "busy":
            state, can_run = "busy", False
        elif active is not None:
            state, can_run = "busy", True
        else:
            state, can_run = "ready", True
        reading = dispatcher.sampler.latest()
        gpu_doc = None
        if reading is not None:
            gpu_doc = {"healthy": dispatcher.probe.state == gpu.OK,
                       **{k: reading.get(k) for k in ("utilisation_pct",
                                                      "memory_used_mib",
                                                      "power_watts", "pstate")}}
        running = dispatcher.engine
        return {"state": state, "can_run": can_run, "mode": "always-on",
                "machine_state": machine, "seconds_until_available": 0,
                "job_running": active is not None,
                "running_service": active.service if active is not None else None,
                "yields": 0, "gpu": gpu_doc,
                "services": [{"id": service, "running": running == spec.id,
                              "queued": store.queued_count(service)}
                             for service, spec in services.items()]}

    # NO GET FOR A CLIP. Reference clips are recordings of somebody's voice
    # and nothing needs one back over the network.
    @app.head("/v1/assets/{sha256}")
    async def has_asset(sha256: str):
        if HEX64.fullmatch(sha256) is None:
            return Response(status_code=400)
        return Response(status_code=200 if store.has_asset(sha256) else 404)

    @app.post("/v1/assets")
    async def put_asset(request: Request):
        raw = await request.body()
        if not raw:
            return _error(400, "the body is empty; send the clip's bytes")
        digest, created = await run_in_threadpool(store.put_asset, raw)
        return JSONResponse({"sha256": digest, "bytes": len(raw)},
                            status_code=201 if created else 200)

    @app.post("/v1/services/{service}/jobs")
    async def submit(service: str, request: Request):
        spec = services.get(service)
        if spec is None:
            return _error(404, "no such service on this runner")
        try:
            body = json.loads(await request.body())
        except ValueError:
            return _error(400, "the body is not JSON")
        if not isinstance(body, dict):
            return _error(400, "the body must be one JSON object")
        key = request.headers.get("idempotency-key")
        if key is not None:
            if not key or len(key) > MAX_KEY_CHARS:
                return _error(400, f"Idempotency-Key must be 1 to "
                                   f"{MAX_KEY_CHARS} characters")
            # THE SAME KEY NEVER SPEAKS TWICE. A key seen before answers with
            # the job it named, without validating or running anything again.
            found = store.lookup(key)
            if found is not None:
                if found.service != service:
                    return _error(409, "this Idempotency-Key names a job for "
                                       "another service")
                return JSONResponse(document(found, reused=True))
        parsed = validate(spec, body, store)
        if isinstance(parsed, str):
            return _error(400, parsed)
        ok, why = dispatcher.available(service)
        if not ok:
            return _error(503, why or "not available")
        try:
            job, reused = store.create(service, spec.id, parsed, key)
        except QueueFull:
            return _error(503, "the queue is full")
        except KeyConflict:
            return _error(409, "this Idempotency-Key names a job for another service")
        if reused:
            return JSONResponse(document(job, reused=True))
        return JSONResponse({"job_id": job.id, "service": service,
                             "status": "queued", "reused": False}, status_code=202)

    @app.get("/v1/services/{service}/jobs/{job_id}")
    async def poll(service: str, job_id: str):
        job = job_of(service, job_id)
        if job is None:
            return _error(404, "no such job")
        return JSONResponse(document(job))

    @app.get("/v1/services/{service}/jobs/{job_id}/result")
    async def result(service: str, job_id: str, request: Request):
        job = job_of(service, job_id)
        if job is None:
            return _error(404, "no such job")
        names = store.artefacts(job)
        if not names:
            return _error(409, "the job has no artefacts yet")
        name = request.query_params.get("artefact") or ""
        if (re.fullmatch(re.escape(job.id) + r"\.\d{1,5}\.f32", name) is None
                or name not in names):
            return _error(404, "no such artefact")
        data = await run_in_threadpool((job.dir / name).read_bytes)
        return Response(data, media_type="application/octet-stream")

    @app.delete("/v1/services/{service}/jobs/{job_id}")
    async def cancel(service: str, job_id: str):
        job = job_of(service, job_id)
        if job is None:
            return _error(404, "no such job")
        said = store.cancel(job.id)
        dispatcher.wake()
        return JSONResponse(said)

    return Guard(app, key)
