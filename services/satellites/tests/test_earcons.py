"""Earcons: the default sounds, the frames that carry them, and the upload
state machine against a fake satellite that follows the firmware's rules
(clients/korvo-satellite/src/earcons.cpp). No socket and no board."""

from __future__ import annotations

import hashlib
import json
import struct

import numpy as np
import pytest

from app import earcons

WS_LIBRARY_LIMIT = 15 * 1024  # WEBSOCKETS_MAX_DATA_SIZE on the ESP32


def samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768


def dominant_hz(x: np.ndarray) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / earcons.RATE)[spectrum.argmax()])


class FakeSatellite:
    """The satellite's side of an upload, with the checks earcons.cpp makes:
    one upload at a time, chunks only at the expected offset, never past the
    announced size, and the SHA-256 checked before the earcon is kept."""

    def __init__(self, ready: bool = True, have: dict[str, bytes] | None = None):
        self.ready = ready
        self.files: dict[str, bytes] = dict(have or {})
        self.put: dict | None = None
        self.sent: list[dict] = []

    def listing(self) -> dict:
        items = [{"id": k, "size": len(v), "sha256": hashlib.sha256(v).hexdigest()}
                 for k, v in self.files.items()] if self.ready else []
        return {"type": "earcons", "ready": self.ready, "items": items, "last_load_us": 0}

    def receive(self, out: dict | bytes) -> dict | None:
        if isinstance(out, dict):
            assert out["type"] == "earcon_put"
            json.dumps(out)  # must be sendable as a text frame
            if self.put is not None:
                return {"type": "earcon_failed", "op": "put", "id": out["id"], "error": "busy"}
            if out["id"] not in self.files and len(self.files) >= earcons.MAX_EARCONS:
                return {"type": "earcon_failed", "op": "put", "id": out["id"], "error": "full"}
            self.put = {"id": out["id"], "size": out["size"], "sha256": out["sha256"],
                        "buf": bytearray()}
            return {"type": "earcon_next", "id": out["id"], "offset": 0}
        kind, _, _, _, offset = struct.unpack("<BBBBI", out[:earcons.HEADER])
        assert kind == earcons.FRAME_EARCON
        p = self.put
        if p is None or offset != len(p["buf"]):
            return None  # stale or duplicate: ignored
        data = out[earcons.HEADER:]
        assert len(data) <= p["size"] - len(p["buf"]), "chunk runs past the announced size"
        p["buf"] += data
        if len(p["buf"]) < p["size"]:
            return {"type": "earcon_next", "id": p["id"], "offset": len(p["buf"])}
        self.put = None
        if hashlib.sha256(p["buf"]).hexdigest() != p["sha256"]:
            return {"type": "earcon_failed", "op": "put", "id": p["id"], "error": "sha256 mismatch"}
        self.files[p["id"]] = bytes(p["buf"])
        return {"type": "earcon_stored", "id": p["id"], "size": p["size"], "sha256": p["sha256"]}


def converse(sync: earcons.Sync, satellite: FakeSatellite, limit: int = 1000) -> None:
    """Runs the exchange from earcon_list until neither side has anything to say."""
    msg: dict | None = satellite.listing()
    for _ in range(limit):
        if msg is None:
            return
        out = sync.handle(msg)
        if out is None:
            return
        msg = satellite.receive(out)
    raise AssertionError("the exchange did not settle")


# ---- the default sounds ---------------------------------------------------------


def test_defaults_are_the_three_the_hub_relies_on():
    assert set(earcons.defaults()) == {"wake", "done", "error"}


@pytest.mark.parametrize("eid", ["wake", "done", "error"])
def test_default_earcons_fit_what_a_satellite_will_store(eid):
    pcm = earcons.defaults()[eid]
    earcons.check(eid, pcm)
    assert len(pcm) % 2 == 0 and len(pcm) <= earcons.MAX_BYTES


@pytest.mark.parametrize("eid,ms", [("wake", 150), ("done", 150), ("error", 210)])
def test_default_earcons_are_short_enough_to_be_feedback(eid, ms):
    assert len(earcons.defaults()[eid]) / 2 / earcons.RATE * 1000 == pytest.approx(ms, abs=1)


def test_wake_rises_and_done_falls():
    w, d = samples(earcons.wake()), samples(earcons.done())
    n = earcons.RATE // 20  # 50 ms windows at each end
    assert dominant_hz(w[:n]) < dominant_hz(w[-n:])
    assert dominant_hz(d[:n]) > dominant_hz(d[-n:])
    # Same two notes, so the pair sounds like one family.
    assert dominant_hz(w[:n]) == pytest.approx(dominant_hz(d[-n:]), abs=25)


def test_error_is_two_low_pulses_with_a_silent_gap():
    x = samples(earcons.error())
    block = earcons.RATE // 200  # 5 ms
    rms = np.sqrt((x[:len(x) // block * block].reshape(-1, block) ** 2).mean(axis=1))
    loud = rms > rms.max() * 0.1
    runs = np.count_nonzero(loud[1:] & ~loud[:-1]) + int(loud[0])
    assert runs == 2
    assert not loud[len(loud) // 2]  # the middle of the sound is the gap
    first_pulse = x[:int(0.08 * earcons.RATE)]
    assert dominant_hz(first_pulse) < dominant_hz(samples(earcons.wake())[:len(first_pulse)])


@pytest.mark.parametrize("eid", ["wake", "done", "error"])
def test_earcons_start_and_end_on_silence_so_they_do_not_click(eid):
    x = samples(earcons.defaults()[eid])
    assert x[0] == 0 and x[-1] == 0
    # No step between neighbouring samples larger than a smooth 1 kHz tone at
    # this level would make.
    assert np.abs(np.diff(x)).max() < 0.05


@pytest.mark.parametrize("eid", ["wake", "done", "error"])
def test_earcons_leave_headroom_to_be_mixed_over_speech(eid):
    peak_db = 20 * np.log10(np.abs(samples(earcons.defaults()[eid])).max())
    assert -20 < peak_db < -9


def test_defaults_are_identical_every_time_so_satellites_are_not_re_sent_them():
    assert earcons.defaults() == earcons.defaults()


# ---- the frames ------------------------------------------------------------------


def test_every_frame_stays_under_the_websocket_librarys_limit():
    loud = b"\x01\x00" * (earcons.MAX_BYTES // 2)
    sizes = [len(f) for f in earcons.frames(loud)]
    assert max(sizes) == earcons.CHUNK + earcons.HEADER
    assert max(sizes) < WS_LIBRARY_LIMIT


def test_frame_header_is_kind_4_padding_and_the_offset_little_endian():
    pcm = bytes(range(256)) * 100
    f = earcons.frame(pcm, 8192)
    assert f[:8] == bytes([4, 0, 0, 0]) + (8192).to_bytes(4, "little")
    assert f[8:] == pcm[8192:8192 + earcons.CHUNK]


def test_frames_reassemble_to_the_bytes_the_sha256_names():
    pcm = earcons.error() * 5
    msg = earcons.put_message("error5", pcm)
    body = b"".join(f[earcons.HEADER:] for f in earcons.frames(pcm))
    assert body == pcm
    assert msg == {"type": "earcon_put", "id": "error5", "size": len(pcm),
                   "sha256": hashlib.sha256(body).hexdigest()}


@pytest.mark.parametrize("eid", ["", "Wake", "../boot", "a/b", "x" * 25, "wake.pcm", "sp ace"])
def test_ids_that_are_not_safe_file_names_on_the_satellite_are_refused(eid):
    with pytest.raises(earcons.EarconError):
        earcons.put_message(eid, earcons.wake())


def test_an_earcon_over_two_seconds_is_refused_before_it_is_sent():
    with pytest.raises(earcons.EarconError):
        earcons.put_message("long", b"\x00\x00" * (2 * earcons.RATE + 1))


def test_half_a_sample_is_refused():
    with pytest.raises(earcons.EarconError):
        earcons.put_message("odd", b"\x00\x00\x00")


def test_more_earcons_than_a_satellite_keeps_is_refused_up_front():
    with pytest.raises(earcons.EarconError):
        earcons.Sync({f"e{i}": earcons.wake() for i in range(earcons.MAX_EARCONS + 1)})


def test_old_firmware_without_earcons_is_not_sent_them():
    assert not earcons.supported({"mic": {"rate": 16000}})
    assert earcons.supported({"earcons": {"max": 16, "max_bytes": 192000, "rate": 48000}})


# ---- the upload, against a fake satellite -----------------------------------------


def test_a_fresh_satellite_receives_every_default_intact():
    satellite, sync = FakeSatellite(), earcons.Sync(earcons.defaults())
    converse(sync, satellite)
    assert satellite.files == earcons.defaults()
    assert sync.done and sync.stored == ["wake", "done", "error"] and not sync.failed


def test_a_satellite_that_has_them_already_is_sent_nothing():
    satellite = FakeSatellite(have=earcons.defaults())
    sync = earcons.Sync(earcons.defaults())
    assert sync.handle(satellite.listing()) is None
    assert sync.done and sync.stored == []


def test_only_the_stale_earcon_is_re_sent():
    have = earcons.defaults() | {"done": earcons.error()}
    satellite, sync = FakeSatellite(have=have), earcons.Sync(earcons.defaults())
    converse(sync, satellite)
    assert sync.stored == ["done"]
    assert satellite.files == earcons.defaults()


def test_a_large_earcon_travels_in_several_chunks():
    big = (earcons.wake() * 14)[:earcons.MAX_BYTES]
    satellite, sync = FakeSatellite(), earcons.Sync({"big": big})
    converse(sync, satellite)
    assert satellite.files["big"] == big
    assert len(big) > 20 * earcons.CHUNK


def test_a_failed_upload_is_recorded_and_the_next_one_still_goes():
    have = {f"x{i}": earcons.wake() for i in range(earcons.MAX_EARCONS)}
    satellite = FakeSatellite(have=have)
    sync = earcons.Sync({"x0": earcons.done(), "new": earcons.wake()})
    converse(sync, satellite)
    assert sync.failed == {"new": "full"}
    assert sync.stored == ["x0"] and satellite.files["x0"] == earcons.done()
    assert sync.done


def test_a_failed_play_does_not_end_an_upload_of_the_same_id():
    sync = earcons.Sync({"wake": earcons.wake()})
    put = sync.handle({"type": "earcons", "ready": True, "items": []})
    assert put["id"] == "wake"
    assert sync.handle({"type": "earcon_failed", "op": "play", "id": "wake",
                        "error": "unknown earcon"}) is None
    assert sync.current == "wake" and not sync.failed
    assert isinstance(sync.handle({"type": "earcon_next", "id": "wake", "offset": 0}), bytes)


def test_a_satellite_still_formatting_its_storage_is_asked_again_not_sent_to():
    sync = earcons.Sync(earcons.defaults())
    assert sync.handle(FakeSatellite(ready=False).listing()) is None
    assert not sync.ready and not sync.done
    converse(sync, FakeSatellite())
    assert sync.done


def test_a_listing_that_crosses_an_upload_does_not_restart_it():
    sync = earcons.Sync(earcons.defaults())
    first = sync.handle({"type": "earcons", "ready": True, "items": []})
    assert sync.handle({"type": "earcons", "ready": True, "items": []}) is None
    assert sync.current == first["id"]


@pytest.mark.parametrize("offset", [-1, 10**9, "0", None])
def test_a_nonsense_offset_gets_no_frame(offset):
    sync = earcons.Sync({"wake": earcons.wake()})
    sync.handle({"type": "earcons", "ready": True, "items": []})
    assert sync.on_next({"type": "earcon_next", "id": "wake", "offset": offset}) is None


def test_a_request_for_another_earcon_gets_no_frame():
    sync = earcons.Sync({"wake": earcons.wake()})
    sync.handle({"type": "earcons", "ready": True, "items": []})
    assert sync.on_next({"type": "earcon_next", "id": "done", "offset": 0}) is None


def test_play_message_names_the_earcon():
    assert earcons.play_message("wake") == {"type": "earcon", "id": "wake"}


def test_has_is_true_only_for_what_the_satellite_holds_with_the_wanted_content():
    """The hub plays an earcon only when the satellite can: anything else
    comes back as earcon_failed, after a silence where the chime should have
    been."""
    want = earcons.defaults()
    sync = earcons.Sync(want)
    assert not sync.has("wake")  # nothing listed yet
    held = {"id": "wake", "size": len(want["wake"]), "sha256": earcons.sha256(want["wake"])}
    stale = {"id": "done", "size": 4, "sha256": "0" * 64}
    put = sync.handle({"type": "earcons", "ready": True, "items": [held, stale]})
    assert put["id"] == "done"
    assert sync.has("wake") and not sync.has("done") and not sync.has("error")
    sync.handle({"type": "earcon_stored", "id": "done", "size": len(want["done"]),
                 "sha256": earcons.sha256(want["done"])})
    sync.handle({"type": "earcon_failed", "op": "put", "id": "error", "error": "full"})
    assert sync.has("done") and not sync.has("error") and not sync.has("chirp")
