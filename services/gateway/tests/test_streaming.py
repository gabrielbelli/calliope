"""Whether a streamed answer crosses the gateway as a stream.

tts-stack answers `stream_format: "sse"` with one event per chunk of audio, so
a caller can play the first sentence while the rest is still being made, and
every byte of that crosses this process. A proxy that read the upstream body to
the end before answering would deliver identical bytes, identical headers and
an identical log line: the only symptom is a stack that looks slow, and there
is nothing in a response to see it in.

NOT THROUGH httpx.ASGITransport, which is what the rest of this suite uses.
That transport collects the response body before it returns, so time to first
byte through it is always time to last byte and a proxy that buffers is
indistinguishable from one that does not. The app is driven as raw ASGI here
and the `send` callable is the witness.
"""

from __future__ import annotations

import json

import anyio
import httpx
from conftest import MockBackend, Router, reload_gateway

SPEECH = "/v1/audio/speech"

FRAMES = [b'data: {"type":"speech.audio.delta","audio":"AAAA"}\n\n',
          b'data: {"type":"speech.audio.delta","audio":"BBBB"}\n\n',
          b'data: {"type":"speech.audio.done","usage":{}}\n\n']


class Paced(httpx.AsyncByteStream):
    """A backend answer that is still being made, and a record of what has left.

    `produced` gets one entry per piece as it is yielded, so a test can read
    how much of the answer existed at the moment the gateway sent its own Nth
    message. The bytes are the same either way; only the timing differs, so the
    timing is what has to be asserted.

    A byte stream rather than the MockBackend the rest of this suite uses,
    because Router reaches a mock backend through httpx.ASGITransport and that
    transport collects the whole response body before it returns. A backend
    that cannot stream cannot show whether the gateway streams.
    """

    def __init__(self, pieces: list[bytes]) -> None:
        self.pieces = pieces
        self.produced: list[bytes] = []

    async def __aiter__(self):
        for piece in self.pieces:
            self.produced.append(piece)
            yield piece


class Streaming(httpx.AsyncBaseTransport):
    """The tts-stack an SSE request actually meets: headers now, body later."""

    def __init__(self, paced: Paced) -> None:
        self.paced = paced

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            "x-accel-buffering": "no"}, stream=self.paced)


async def _drive(app, payload: bytes, watch=None) -> list[bytes]:
    """One POST through the real ASGI app, recording each body message."""
    delivered: list[bytes] = []
    sent_body = False

    async def receive():
        nonlocal sent_body
        if sent_body:
            # A caller that is still there and says nothing. Answering
            # http.request twice makes starlette raise, and answering
            # http.disconnect makes it abandon the response after the first
            # event, which would leave this file asserting that a stream is
            # cut short rather than that it streams.
            await anyio.Event().wait()
        sent_body = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            delivered.append(message["body"])
            if watch is not None:
                watch()

    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": SPEECH,
        "raw_path": SPEECH.encode(), "query_string": b"", "root_path": "",
        "client": ("127.0.0.1", 12345), "server": ("gateway.test", 80),
        "headers": [(b"host", b"gateway.test"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode())],
    }, receive, send)
    return delivered



async def _app(monkeypatch, backend):
    """The real gateway, pointed at a tts-stack that answers in pieces."""
    main = reload_gateway(monkeypatch)
    router = Router({"stt.test": MockBackend("stt-stack"),
                     "tts.test": Streaming(backend),
                     "long.test": MockBackend("tts-long")})
    monkeypatch.setattr(main, "new_client",
                        lambda: httpx.AsyncClient(transport=router,
                                                  follow_redirects=False))
    return main


async def test_each_event_is_forwarded_before_the_next_one_is_made(monkeypatch):
    """The claim the whole feature rests on, and the one no response can show.

    Recorded at each body message, `produced` is how many pieces the backend
    had sent at that moment. The first entry is the claim: the first event must
    leave the gateway while one piece exists. Reading the upstream body to the
    end first makes every entry 3, because everything exists before anything is
    sent, and `await upstream.aread()` in place of the aiter_raw loop in _body
    is exactly that failure -- one that breaks nothing else in this suite.

    Only the FIRST entry is pinned to a value. The later ones can read ahead by
    a message, because starlette pumps a streaming body through a memory
    stream. That moves nothing on the wire, and time to first byte is what this
    feature is.
    """
    backend = Paced(FRAMES)
    main = await _app(monkeypatch, backend)
    at_send: list[int] = []

    async with main.app.router.lifespan_context(main.app):
        delivered = await _drive(
            main.app, json.dumps({"model": "kokoro", "input": "One. Two.",
                                  "stream_format": "sse"}).encode(),
            watch=lambda: at_send.append(len(backend.produced)))

    assert at_send[0] == 1, (
        f"the first event left after {at_send[0]} of {len(FRAMES)} existed; "
        "3 of 3 is the whole body read before anything was sent")
    assert at_send == sorted(at_send)
    assert delivered == FRAMES, "and the bytes are untouched as well as prompt"


async def test_the_instruction_not_to_buffer_survives_the_proxy(monkeypatch):
    """X-Accel-Buffering: no is addressed to whatever proxy is in front, and
    both producers set it. Dropped here, the stream is reassembled a hop later
    and nothing in this service can tell.

    Cache-Control is set for the reason tts-long gives: an intermediary that
    cached part of a stream would replay someone else's audio.
    """
    backend = Paced(FRAMES)
    main = await _app(monkeypatch, backend)
    async with main.app.router.lifespan_context(main.app):
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://gateway.test") as client:
            response = await client.post(
                SPEECH, content=json.dumps({"model": "kokoro", "input": "One.",
                                            "stream_format": "sse"}))
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-type"] == "text/event-stream"
    assert "content-length" not in response.headers, \
        "a length this response cannot keep makes a client stop reading early"


class Taking(httpx.AsyncBaseTransport):
    """voice-satellites taking an upload: each piece recorded as it arrives,
    and the answer given once the body has ended, as POST
    /satellites/{id}/media answers once the music has played."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.query = b""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.query = request.url.query
        async for piece in request.stream:
            if piece:
                self.received.append(piece)
        # stream=, not json=: a body given whole is read at once, and the
        # proxy, which streams every answer, would find nothing left to read.
        answer = json.dumps({"played_s": 0.04, "stopped": False, "reason": "ended"}).encode()
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(answer))


async def _upload(app, path: str, query: bytes, pieces: list[bytes],
                  before_last) -> tuple[int, bytes]:
    """One chunked POST through the real ASGI app, a piece per message, as
    Home Assistant sends a WAV its ffmpeg is still writing. `before_last` is
    called just before the last piece leaves the client."""
    left = list(pieces)
    status, answer = 0, b""

    async def receive():
        if not left:
            await anyio.Event().wait()   # still there, saying nothing (see _drive)
        piece = left.pop(0)
        if not left:
            before_last()
        return {"type": "http.request", "body": piece, "more_body": bool(left)}

    async def send(message):
        nonlocal status, answer
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            answer += message.get("body", b"")

    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": query, "root_path": "", "client": ("127.0.0.1", 12345),
        "server": ("gateway.test", 80),
        "headers": [(b"host", b"gateway.test"), (b"content-type", b"audio/wav"),
                    (b"transfer-encoding", b"chunked")],
    }, receive, send)
    return status, answer


async def test_a_media_upload_reaches_the_hub_while_it_is_still_being_sent(monkeypatch):
    """Home Assistant's play_media is a WAV its ffmpeg is still writing, for
    as long as the music lasts, and the hub plays it as it arrives. A gateway
    that read the upload to its end before forwarding it would play a radio
    stream never, and a song only after it had all been converted. The hub
    must have the first piece before the client has sent the last."""
    main = reload_gateway(monkeypatch)
    hub = Taking()
    router = Router({"stt.test": MockBackend("stt-stack"), "tts.test": MockBackend("tts-stack"),
                     "long.test": MockBackend("tts-long"), "satellites.test": hub})
    monkeypatch.setattr(main, "new_client",
                        lambda: httpx.AsyncClient(transport=router, follow_redirects=False))
    pieces = [b"RIFF\xff\xff\xff\xffWAVE", bytes(3528), bytes([1]) * 3528]
    at_last: list[int] = []

    async with main.app.router.lifespan_context(main.app):
        status, answer = await _upload(main.app, "/satellites/020000000002/media", b"announce=0",
                                       pieces, lambda: at_last.append(len(hub.received)))

    assert at_last and at_last[0] >= 1, (
        "the hub had nothing when the last piece left the client: the upload "
        "was read whole before it was forwarded")
    assert b"".join(hub.received) == b"".join(pieces) and hub.query == b"announce=0"
    assert status == 200
    assert json.loads(answer) == {"played_s": 0.04, "stopped": False, "reason": "ended"}


async def test_the_chunk_plan_survives_the_proxy_in_both_directions(monkeypatch):
    """X-Chunk-Phonemes coming back, X-Chunk-Plan going out.

    tts-stack announces the sizes of the chunks a stream is about to carry, so
    a client can draw a bar that moves before any audio exists, and a client
    sends X-Chunk-Plan to say it has read them and can therefore be given the
    shorter ramp. Neither is hop-by-hop and neither needed anything added for
    it to pass; this is what keeps it that way. A stripped header leaves a
    working stack that is quietly slower and says nothing.
    """
    backend = Paced(FRAMES)
    main = await _app(monkeypatch, backend)

    class Announcing(Streaming):
        seen: list[httpx.Request] = []

        async def handle_async_request(self, request):
            Announcing.seen.append(request)
            response = await super().handle_async_request(request)
            response.headers["x-chunk-phonemes"] = "60,60,98,157"
            return response

    router = Router({"stt.test": MockBackend("stt-stack"),
                     "tts.test": Announcing(backend),
                     "long.test": MockBackend("tts-long")})
    monkeypatch.setattr(main, "new_client",
                        lambda: httpx.AsyncClient(transport=router,
                                                  follow_redirects=False))

    async with main.app.router.lifespan_context(main.app):
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://gateway.test") as client:
            response = await client.post(
                SPEECH, headers={"X-Chunk-Plan": "1"},
                content=json.dumps({"model": "kokoro", "input": "One.",
                                    "stream_format": "sse"}))
    assert response.headers["x-chunk-phonemes"] == "60,60,98,157"
    assert Announcing.seen[-1].headers.get("x-chunk-plan") == "1"
