"""Whether a downloaded file crosses this service as a stream.

/ui/fetch sends a finished download on to the gateway, and a two-hour podcast
is 131 MB in a container limited to 512 MB. A route that read the file whole
before sending it would deliver exactly the same bytes, the same headers and
the same log line, and the only symptom would be the memory it held. There is
nothing in a response to see it in, so it is asserted here: the transport that
stands in for the gateway reads the request body piece by piece and notes, at
each piece, how much of the file had been read by then.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from conftest import Bytes, finished_job

URL = "https://media.example/instant-talk"
PIECE = 65536
FILE = bytes(range(256)) * (3 * PIECE // 256) + b"tail" * 25


class Watching(httpx.AsyncBaseTransport):
    """The gateway's internal listener, reading as it goes, and the read position at each piece."""

    def __init__(self, read: list[int]) -> None:
        self.read = read
        self.pieces: list[tuple[int, int]] = []
        self.body = b""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async for piece in request.stream:
            self.pieces.append((len(piece), self.read[0]))
            self.body += piece
        # An unread stream, as the service relays it raw.
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=Bytes(b'{"text": "transcribed"}'))


def test_the_file_leaves_in_64_kib_pieces_and_is_never_read_whole(build, sign, monkeypatch):
    main, _, _ = build()
    finished_job(main, URL, FILE, ".wav")
    read = [0]
    real_open = main.ingest.anyio.open_file

    class Counted:
        def __init__(self, handle) -> None:
            self.handle = handle

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            await self.handle.aclose()

        async def read(self, size: int = -1) -> bytes:
            data = await self.handle.read(size)
            read[0] += len(data)
            return data

    async def counting(path, mode="r", *args, **kwargs):
        return Counted(await real_open(path, mode, *args, **kwargs))

    monkeypatch.setattr(main.ingest.anyio, "open_file", counting)
    gateway = Watching(read)
    monkeypatch.setattr(main, "new_client", lambda: httpx.AsyncClient(transport=gateway))

    with TestClient(main.app, headers=sign()) as api:
        assert api.post("/ui/fetch", json={"token": URL}).status_code == 200

    file_pieces = [(size, at) for size, at in gateway.pieces if at and size in (PIECE, len(FILE) % PIECE)]
    assert [size for size, _ in file_pieces] == [PIECE, PIECE, PIECE, len(FILE) % PIECE]
    # The claim: each piece left while only that much of the file had been
    # read. Read whole, every entry would be the file's length.
    assert [at for _, at in file_pieces] == [PIECE, 2 * PIECE, 3 * PIECE, len(FILE)]
    assert FILE in gateway.body
