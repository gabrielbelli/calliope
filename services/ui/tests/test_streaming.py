"""Whether a downloaded file crosses this service as a stream.

/ui/media relays a finished MeTube download to a <video> or <audio> element,
and a two-hour video is gigabytes. A relay that read the upstream body to the
end before answering would deliver exactly the same bytes, the same headers
and the same log line, and the only symptoms would be a player that waits for
the whole file and a container that holds it -- in 384 MB. There is nothing in
a response to see it in, so it is asserted here.

(This file used to drive the proxy the page's speech calls went through. The
page calls the gateway itself now, so that stream no longer crosses this
service; this one still does.)

NOT THROUGH TestClient, and that is why this file exists rather than a few more
tests in test_ingest.py. starlette's TestClient writes every response body
message into a BytesIO and hands back the finished bytes, so time to first byte
through it is always time to last byte: a streamed response and a collected one
are indistinguishable, which is how a relay that quietly buffers passes a
suite. The app is driven as raw ASGI instead, and the `send` callable is the
witness.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx

from conftest import ALICE

URL = "https://media.example/watch?v=abcdef"


class Paced(httpx.AsyncByteStream):
    """A body that is still being made, and a record of what has left so far.

    `produced` gets one entry per piece as it is yielded, so a test can read
    how much of the upstream body existed at the moment the app sent its own
    Nth message. That comparison is the only thing that separates a relay from
    a buffer, because the bytes are identical either way.
    """

    def __init__(self, pieces: list[bytes]) -> None:
        self.pieces = pieces
        self.produced: list[bytes] = []

    async def __aiter__(self):
        for piece in self.pieces:
            self.produced.append(piece)
            yield piece


def _drive(app, headers: dict[str, str], watch=None) -> list[bytes]:
    """One GET /ui/media through the real ASGI app, recording each body message.

    `watch` is called as each body message is sent, before the next piece of
    the upstream body is asked for.
    """
    delivered: list[bytes] = []
    path = "/ui/media"

    async def receive():
        await asyncio.Event().wait()          # a client that is still there
        return {"type": "http.disconnect"}    # pragma: no cover

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            delivered.append(message["body"])
            if watch is not None:
                watch()

    async def run():
        # The lifespan is what opens the httpx client this service relays
        # with, so it is entered rather than skipped.
        async with app.router.lifespan_context(app):
            await app({
                "type": "http", "asgi": {"version": "3.0"},
                "http_version": "1.1", "method": "GET", "scheme": "http",
                "path": path, "raw_path": path.encode(),
                "query_string": f"token={quote(URL, safe='')}".encode(),
                "root_path": "", "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "headers": [(b"host", b"testserver"),
                            *((name.lower().encode(), value.encode())
                              for name, value in headers.items())],
            }, receive, send)

    asyncio.run(run())
    return delivered


PIECES = [b"\x00\x00\x00\x18ftypmp42", b"moov-and-the-first-second",
          b"the-rest-of-the-file"]


def _finished_and_paced(build) -> tuple[object, Paced]:
    main, _, tube = build()
    tube.finish(URL, "A Title.mp4")
    main.ingest.OWNERS.claim(URL, ALICE)
    paced = Paced(PIECES)
    tube.paced = paced
    return main, paced


def test_each_piece_of_the_file_leaves_before_the_next_one_is_read(build, sign):
    """The claim playback rests on, and the one no response can show.

    Recorded at each body message, `produced` is how many pieces the upstream
    had made at that moment. The first entry is the whole claim: the first
    piece must leave while one piece exists. Collecting the body first makes
    every entry 3, because everything exists before anything is sent, and
    `await upstream.aread()` in place of the aiter_raw loop in the relay is
    exactly that failure.
    """
    main, paced = _finished_and_paced(build)
    at_send: list[int] = []
    delivered = _drive(main.app, sign(),
                       watch=lambda: at_send.append(len(paced.produced)))

    assert at_send[0] == 1, (
        f"the first piece left after {at_send[0]} of {len(PIECES)} existed; "
        "3 of 3 is the whole file collected before anything was sent")
    assert at_send == sorted(at_send)
    assert delivered == PIECES, "and the bytes are untouched as well as prompt"


def test_the_pieces_are_not_regrouped_on_the_way_through(build, sign):
    """Nothing downstream would notice -- a player reads bytes, not messages --
    but a relay that regroups is a relay that is holding bytes, and this is the
    cheapest way to see it."""
    main, _ = _finished_and_paced(build)
    assert _drive(main.app, sign()) == PIECES
