"""The media lane: Home Assistant's music and announcements, as WAVs already in
the satellite's own format, relayed by the hub and never decoded.

A fake Pi (caps "media": frame kind 5) and a fake Korvo (speaker frames only)
on Starlette's test socket, as in test_satellites.py. An upload is answered
only when it has played, so one that has to be stopped while it plays is sent
from a thread, and the test acts once its first frame has arrived. Nothing
plays anywhere: every frame lands in the test socket's queue.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import struct
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from test_satellites import NID, SETTINGS, adopt

PI_MAC = "02:00:00:00:00:02"
PI = "020000000002"
# What calliope_pi.agent says in its hello (the spec's section 2.1), less the
# earcons, whose upload would come between a test and the frames it reads.
PI_CAPS = {"speaker": {"rate": 44100, "channels": 1, "format": "s16le"},
           "media": {"rate": 44100, "channels": 2, "format": "s16le"},
           "mic": {"rate": 16000, "channels": 2, "format": "s16le", "reference": True,
                   "max_gain_db": 3.5},
           "duck": True, "audio_devices": True, "bundle": "tar.gz",
           "airplay": {"version": 2, "controls": True},
           "health": ["temp_c", "throttled", "under_voltage", "load"]}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SATELLITES_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SATELLITES_TTS_URL", raising=False)
    return importlib.reload(importlib.import_module("app.main"))


@pytest.fixture
def client(app):
    with TestClient(app.app) as c:
        yield c


@pytest.fixture
def published(app, client) -> list[dict]:
    events: list[dict] = []
    publish = app.hub.publish
    app.hub.publish = lambda e: (events.append(e), publish(e))
    return events


def pi_hello(token: str = "") -> dict:
    return {"type": "hello", "id": PI_MAC, "model": "raspberry-pi", "fw": "v0.2.0", "token": token,
            "caps": PI_CAPS} | SETTINGS


def adopt_pi(client, ws, name: str = "Lounge") -> None:
    ws.send_json(pi_hello())
    assert ws.receive_json() == {"type": "pending"}
    assert client.post(f"/satellites/{PI}/adopt", json={"name": name}).status_code == 200
    ws.send_json(pi_hello(ws.receive_json()["token"]))
    assert ws.receive_json()["type"] == "welcome"


def pcm(seconds: float, rate: int, channels: int, value: int | None = None) -> bytes:
    """Audio that can be told apart frame by frame: a ramp, or one value."""
    n = int(seconds * rate) * channels
    x = np.full(n, value) if value is not None else np.arange(n) % 20000
    return x.astype("<i2").tobytes()


def wav(data: bytes, rate: int, channels: int, *, tag: int = 1, bits: int = 16,
        size: int | None = None, before: bytes = b"") -> bytes:
    """A WAV as ffmpeg writes one to a pipe when `size` is 0 or 0xFFFFFFFF,
    with any chunks `before` the audio."""
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * channels * bits // 8,
                      channels * bits // 8, bits)
    if tag == 0xFFFE:
        fmt += struct.pack("<HHI", 22, bits, 0) + b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + before
            + b"data" + struct.pack("<I", len(data) if size is None else size) + data)
    return b"RIFF" + struct.pack("<I", len(body) if size is None else size) + body


def frames(ws, kind: int, count: int) -> list[bytes]:
    """The next `count` binary frames of `kind`, whatever text comes between."""
    got = []
    while len(got) < count:
        msg = ws.receive()
        if msg.get("bytes") and msg["bytes"][0] == kind:
            got.append(msg["bytes"])
    return got


def text_until(ws, kind: str) -> dict:
    """Read on to the next text message of `kind`, over any frames."""
    while True:
        msg = ws.receive()
        if msg.get("text") and json.loads(msg["text"])["type"] == kind:
            return json.loads(msg["text"])


def said_until(ws, kind: str) -> list[str]:
    """The types of the text messages up to and with the next of `kind`, in
    the order they came, over any frames."""
    said: list[str] = []
    while not said or said[-1] != kind:
        msg = ws.receive()
        if msg.get("text"):
            said.append(json.loads(msg["text"])["type"])
    return said


class Upload(threading.Thread):
    """A POST /satellites/{id}/media that is still playing: its answer comes
    when the stream ends, so it waits in a thread of its own."""

    def __init__(self, client, nid: str, body: bytes, announce: bool = False):
        super().__init__(daemon=True)
        self.args = (client, nid, body, announce)
        self.answer = None

    def run(self) -> None:
        client, nid, body, announce = self.args
        self.answer = client.post(f"/satellites/{nid}/media", params={"announce": int(announce)},
                                  content=body)

    def result(self):
        self.join(timeout=10)
        assert not self.is_alive(), "the upload was never answered"
        return self.answer


# ---- what to send -------------------------------------------------------------


def test_the_media_block_says_what_to_send_and_through_whom(client, app):
    with client.websocket_connect("/satellites/ws") as korvo:
        adopt(client, korvo)
        assert client.get(f"/satellites/{NID}").json()["media"] == {
            "through": NID, "music": {"rate": 48000, "channels": 1},
            "announce": {"rate": 48000, "channels": 1}, "playing": None}
        with client.websocket_connect("/satellites/ws") as pi:
            adopt_pi(client, pi)
            assert client.get(f"/satellites/{PI}").json()["media"] == {
                "through": PI, "music": {"rate": 44100, "channels": 2},
                "announce": {"rate": 44100, "channels": 1}, "playing": None}
            assert client.patch(f"/satellites/{NID}", json={"output_satellite": PI}).status_code == 200
            through = client.get(f"/satellites/{NID}").json()["media"]
            assert through["through"] == PI and through["music"] == {"rate": 44100, "channels": 2}
            assert through["announce"] == {"rate": 44100, "channels": 1}
    assert client.get(f"/satellites/{NID}").json()["media"] is None, "offline"


# ---- music -------------------------------------------------------------------------


def test_music_reaches_a_pi_as_stereo_media_frames(client, published):
    music = pcm(0.2, 44100, 2)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        r = client.post(f"/satellites/{PI}/media", params={"announce": 0}, content=wav(music, 44100, 2))
        assert r.status_code == 200, r.json()
        assert r.json() == {"played_s": 0.2, "stopped": False, "reason": "ended"}
        got = frames(ws, 5, len(music) // 3528)
    assert all(f[2] == 2 and len(f) == 16 + 3528 for f in got)
    assert [struct.unpack_from("<I", f, 4)[0] for f in got] == list(range(len(got)))
    assert b"".join(f[16:] for f in got) == music
    media = [(e["state"], e["reason"], e["announce"]) for e in published if e["type"] == "media"]
    assert media == [("playing", None, False), ("ended", "ended", False)]


def test_music_reaches_a_korvo_as_speaker_frames_only_after_the_voice(client, app):
    """The Korvo has one stream of speaker frames, and a reply must not
    queue behind the music or be flushed by it: the music waits for the
    voice lane to be idle, and the two share one count of frames."""
    music = pcm(0.2, 48000, 1, value=1000)
    tone = app.audio.tone(440, 0.1, 48000)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        assert client.post(f"/satellites/{NID}/tone", json={"seconds": 0.1}).status_code == 204
        r = client.post(f"/satellites/{NID}/media", content=wav(music, 48000, 1))
        assert r.json()["reason"] == "ended"
        got = frames(ws, 2, (len(tone) + len(music)) // 1920)
    assert all(f[2] == 1 for f in got), "mono"
    assert b"".join(f[16:] for f in got) == tone + music, "the whole tone first"
    assert [struct.unpack_from("<I", f, 4)[0] for f in got] == list(range(len(got)))


def test_a_tone_that_arrives_mid_music_on_a_korvo_plays_whole_between_its_frames(client, app):
    """The music pauses at its next frame for the voice, and carries on after
    it, on the one count of frames the Korvo has: a gap or a repeat in it
    would be heard as a dropout."""
    beat = pcm(0.02, 48000, 1, value=1000)                 # one frame of the music
    tone = app.audio.tone(440, 0.1, 48000)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        upload = Upload(client, NID, wav(pcm(10.0, 48000, 1, value=1000), 48000, 1))
        upload.start()
        got = frames(ws, 2, 1)
        assert client.post(f"/satellites/{NID}/tone", json={"seconds": 0.1}).status_code == 204
        heard = ""
        while len(got) < 200 and not ("t" in heard and heard.endswith("m")):
            got += frames(ws, 2, 1)
            heard = "".join("m" if f[16:] == beat else "t" for f in got)
        assert client.post(f"/satellites/{NID}/media/stop").status_code == 204
        assert upload.result().json()["reason"] == "stopped"
    assert heard.strip("m") == "t" * (len(tone) // 1920), heard
    assert b"".join(f[16:] for f in got if f[16:] != beat) == tone
    assert [struct.unpack_from("<I", f, 4)[0] for f in got] == list(range(len(got)))


def test_music_on_a_korvo_waits_for_a_reply_between_its_sentences(client, app):
    """A reply is queued a sentence at a time, as each is synthesised, so the
    lane is empty between two of them while the next is made: music sent
    then was heard in the middle of the answer."""
    music = pcm(0.2, 48000, 1, value=1000)
    sentence = app.audio.tone(440, 0.1, 48000)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt(client, ws)
        s = app.hub.sessions[NID]
        # A conversation replying here, its next sentence not yet made.
        s.conversation = SimpleNamespace(phase="replying", player=SimpleNamespace(session=lambda: s))
        upload = Upload(client, NID, wav(music, 48000, 1))
        upload.start()
        while s.media is None:
            time.sleep(0.01)
        time.sleep(0.1)                                    # long enough to have sent it all
        assert client.post(f"/satellites/{NID}/tone", json={"seconds": 0.1}).status_code == 204
        s.conversation = None                              # the reply is over
        assert upload.result().json()["reason"] == "ended"
        got = frames(ws, 2, (len(sentence) + len(music)) // 1920)
    assert b"".join(f[16:] for f in got) == sentence + music


def test_a_wav_in_another_format_is_refused_with_what_to_send(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        r = client.post(f"/satellites/{PI}/media", content=wav(pcm(0.1, 48000, 1), 48000, 1))
    assert r.status_code == 415
    error = r.json()["error"]
    assert error["code"] == "format_mismatch" and error["param"] == "body"
    assert "44100" in error["message"] and "2 channel" in error["message"]


@pytest.mark.parametrize("body", [
    wav(pcm(0.1, 44100, 2) * 2, 44100, 2, tag=3, bits=32),
    b"these bytes are not a WAV at all",
    wav(pcm(0.1, 44100, 2), 44100, 2, before=b"LIST" + struct.pack("<I", 70000) + bytes(70000)),
], ids=["float", "garbage", "a header over 64 KiB"])
def test_what_is_not_a_16_bit_wav_is_refused(client, body):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        r = client.post(f"/satellites/{PI}/media", content=body)
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_audio"


def test_wav_stream_reads_a_header_written_to_a_pipe(app):
    """ffmpeg writing to a pipe cannot go back for its sizes: 0 or 0xFFFFFFFF.
    It puts its tags in a LIST before the audio, and writes the extensible
    format tag for PCM too. The header is read as it arrives, a few bytes
    at a time."""
    audio = app.audio
    data = pcm(0.05, 44100, 2)
    tags = b"LIST" + struct.pack("<I", 26) + b"INFOISFT" + struct.pack("<I", 13) + b"Lavf61.7.100\x00\x00"

    async def read(body: bytes, step: int = 7) -> tuple[tuple[int, int], bytes]:
        async def chunks():
            for i in range(0, len(body), step):
                yield body[i:i + step]
        fmt, rest = await audio.wav_stream(chunks())
        return fmt, b"".join([c async for c in rest])

    for size in (0, 0xFFFFFFFF):
        for tag in (1, 0xFFFE):
            got = asyncio.run(read(wav(data, 44100, 2, tag=tag, size=size, before=tags)))
            assert got == ((44100, 2), data), (size, tag)
    assert audio.wav_format(wav(data, 44100, 2)[:30]) is None, "more of it is needed"
    with pytest.raises(ValueError):
        audio.wav_format(b"RIFX")


# ---- announcements -------------------------------------------------------------------


def test_an_announcement_goes_on_the_voice_lane_and_is_answered_once_played(client, published):
    speech = pcm(0.2, 44100, 1)
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        r = client.post(f"/satellites/{PI}/media", params={"announce": 1}, content=wav(speech, 44100, 1))
        assert r.json() == {"played_s": 0.2, "stopped": False, "reason": "ended"}
        got = frames(ws, 2, len(speech) // 1764)
    assert all(f[2] == 1 for f in got) and b"".join(f[16:] for f in got) == speech
    media = [(e["state"], e["reason"], e["announce"]) for e in published if e["type"] == "media"]
    assert media == [("playing", None, True), ("ended", "ended", True)]


def test_an_announcement_over_two_minutes_is_refused(client, app):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        long = bytes(2 * 44100 * (app.ANNOUNCE_MAX_S + 1))
        r = client.post(f"/satellites/{PI}/media", params={"announce": 1}, content=wav(long, 44100, 1))
    assert r.status_code == 413 and r.json()["error"]["code"] == "announce_too_long"


# ---- stopping --------------------------------------------------------------------------


def playing(client, ws, nid: str = PI, seconds: float = 10.0) -> Upload:
    """Music playing on the Pi, with its first frame arrived."""
    upload = Upload(client, nid, wav(pcm(seconds, 44100, 2), 44100, 2))
    upload.start()
    frames(ws, 5, 1)
    return upload


def test_a_new_stream_supersedes_the_one_playing_and_flushes_it(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        first = playing(client, ws)
        second = client.post(f"/satellites/{PI}/media", content=wav(pcm(0.1, 44100, 2), 44100, 2))
        assert text_until(ws, "media_flush") == {"type": "media_flush"}
        assert first.result().json()["reason"] == "superseded"
    assert second.json() == {"played_s": 0.1, "stopped": False, "reason": "ended"}


def test_media_stop_ends_the_stream_and_flushes_the_pi(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        upload = playing(client, ws)
        assert client.post(f"/satellites/{PI}/media/stop").status_code == 204
        assert text_until(ws, "media_flush") == {"type": "media_flush"}
        answer = upload.result().json()
        assert client.post(f"/satellites/{PI}/media/stop").status_code == 204, "nothing playing"
    assert answer["reason"] == "stopped" and answer["stopped"] is True and answer["played_s"] > 0


def test_stop_on_the_page_stops_media_too(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        upload = playing(client, ws)
        assert client.post(f"/satellites/{PI}/flush").status_code == 204
        assert text_until(ws, "media_flush") == {"type": "media_flush"}
        assert upload.result().json()["reason"] == "stopped"


def test_turning_the_speaker_off_ends_the_stream_and_flushes_the_pi(client):
    """The media_flush is sent before the config: while the config waited on
    the socket, the upload found the speaker off and ended by itself, and a
    Pi was left with nothing to tell it to drop the second it holds."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        upload = playing(client, ws)
        assert client.patch(f"/satellites/{PI}", json={"speaker_enabled": False}).status_code == 200
        assert said_until(ws, "config")[0] == "media_flush"
        assert upload.result().json()["reason"] == "speaker_off"


def test_forgetting_a_satellite_ends_its_stream_as_unadopted_and_flushes_it_first(client, published):
    """Before the "forget", which the Pi takes as the end of its adoption:
    after it, it would take nothing more from the hub."""
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        upload = playing(client, ws)
        assert client.post(f"/satellites/{PI}/forget").status_code == 204
        assert said_until(ws, "forget")[0] == "media_flush"
        answer = upload.result().json()
    assert answer["reason"] == "unadopted" and answer["stopped"] is True
    ended = [e for e in published if e["type"] == "media" and e["state"] == "ended"]
    assert [e["reason"] for e in ended] == ["unadopted"]


def test_the_privacy_mute_ends_the_stream_as_muted(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        upload = playing(client, ws)
        ws.send_json({"type": "status", "muted": True})
        assert text_until(ws, "media_flush") == {"type": "media_flush"}
        assert upload.result().json()["reason"] == "muted"


def test_an_upload_whose_client_goes_away_ends_as_cancelled_and_flushes_the_pi(
        client, app, published, gateway):
    """Home Assistant stopping its ffmpeg, or going away itself, mid-body.
    Driven as raw ASGI on the hub's own loop: TestClient cannot hang up in
    the middle of a body. Signed by hand, as the gateway would."""
    first = wav(pcm(0.1, 44100, 2), 44100, 2, size=0)
    pieces = iter([{"type": "http.request", "body": first, "more_body": True}])
    answer = bytearray()

    async def receive() -> dict:
        return next(pieces, {"type": "http.disconnect"})

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body":
            answer.extend(message.get("body", b""))

    path = f"/satellites/{PI}/media"
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"announce=0",
             "root_path": "", "headers": [(b"host", b"hub.test"), (b"content-type", b"audio/wav"),
                                          (b"x-calliope-identity",
                                           gateway.assertion("satellites").encode())],
             "client": ("127.0.0.1", 50000), "server": ("hub.test", 80)}
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        client.portal.call(app.app, scope, receive, send)
        assert text_until(ws, "media_flush") == {"type": "media_flush"}
    assert json.loads(answer) == {"played_s": 0.1, "stopped": True, "reason": "cancelled"}
    ended = [e for e in published if e["type"] == "media" and e["state"] == "ended"]
    assert [e["reason"] for e in ended] == ["cancelled"]


def test_a_satellite_that_goes_away_ends_its_stream_and_its_announcement(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        music = playing(client, ws)
        announcement = Upload(client, PI, wav(pcm(10.0, 44100, 1), 44100, 1), announce=True)
        announcement.start()
        frames(ws, 2, 1)
    assert music.result().json()["reason"] == "disconnected"
    assert announcement.result().json() == {"played_s": None, "stopped": True, "reason": "disconnected"}



def test_an_announcement_dropped_by_stop_says_so_and_played_nothing(client, published):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        announcement = Upload(client, PI, wav(pcm(10.0, 44100, 1), 44100, 1), announce=True)
        announcement.start()
        frames(ws, 2, 1)
        assert client.post(f"/satellites/{PI}/flush").status_code == 204
        assert announcement.result().json() == {"played_s": None, "stopped": True, "reason": "stopped"}
    ended = [e for e in published if e["type"] == "media" and e["state"] == "ended"]
    assert [(e["reason"], e["played_s"]) for e in ended] == [("stopped", None)]

def test_media_to_a_satellite_with_its_speaker_off_is_refused(client):
    with client.websocket_connect("/satellites/ws") as ws:
        adopt_pi(client, ws)
        assert client.patch(f"/satellites/{PI}", json={"speaker_enabled": False}).status_code == 200
        r = client.post(f"/satellites/{PI}/media", content=wav(pcm(0.1, 44100, 2), 44100, 2))
    assert r.status_code == 409 and r.json()["error"]["code"] == "speaker_disabled"


def test_media_follows_the_output_satellite(client, published):
    """Sent to the Korvo, whose Output is the Pi: the Pi plays it on its
    media lane, and the Korvo's card shows what plays for it."""
    with client.websocket_connect("/satellites/ws") as korvo:
        adopt(client, korvo)
        with client.websocket_connect("/satellites/ws") as pi:
            adopt_pi(client, pi)
            assert client.patch(f"/satellites/{NID}", json={"output_satellite": PI}).status_code == 200
            upload = playing(client, pi, nid=NID)
            view = client.get(f"/satellites/{NID}").json()["media"]
            assert view["through"] == PI and view["playing"]["source"] == NID
            assert client.post(f"/satellites/{NID}/media/stop").status_code == 204
            assert upload.result().json()["reason"] == "stopped"
    [start] = [e for e in published if e["type"] == "media" and e["state"] == "playing"]
    assert (start["satellite"], start["source"]) == (PI, NID)
