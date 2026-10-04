"""app/fetcher.py itself: the guard, the argv, the formats, the facts and real fetches.

The guard patches the socket module for the whole process, so every case that
installs it runs in a `python -c` of its own. Nothing here reaches past
loopback: the refusals happen before a packet leaves, and the real fetches are
from http.servers on 127.0.0.1 and ::1. Where they go through yt-dlp with the
guard in, a stand-in rule allows 127.0.0.1 and refuses ::1, so a second hop
has somewhere to be refused, and somewhere to be counted if it is not.
"""

from __future__ import annotations

import errno
import gzip
import http.server
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import wave
from pathlib import Path

import pytest

from app import fetcher

UI = Path(__file__).resolve().parents[1]
SCRIPT = UI / "app" / "fetcher.py"
NEVER = {"max_downloads", "enable_file_urls", "cookiefile", "cookiesfrombrowser",
         "usenetrc", "netrc_cmd", "external_downloader", "impersonate",
         "remote_components", "wait_for_video", "live_from_start",
         "load_info_filename", "compat_opts"}


def child(code: str) -> dict:
    """Run `code` after the guard is installed, in a process of its own; its last JSON line."""
    script = textwrap.dedent(f"""
        import json, socket, sys
        sys.path.insert(0, {str(UI)!r})
        from app import fetcher, guard
    """) + textwrap.dedent(code)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.strip(), out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


CONNECT = """
    fetcher.install_guard(guard._forbidden, guard.ALLOWED_PORTS)
    family = socket.AF_INET6 if ":" in {host!r} else socket.AF_INET
    try:
        socket.socket(family, socket.SOCK_STREAM).connect(({host!r}, {port}))
        print(json.dumps({{"connected": True}}))
    except ConnectionRefusedError as exc:
        print(json.dumps({{"refused": str(exc), "first": fetcher.REFUSED[0]}}))
"""


@pytest.mark.parametrize("host,port,why", [
    ("127.0.0.1", 80, "loopback"),
    ("10.1.2.3", 443, "private"),
    ("::ffff:127.0.0.1", 443, "loopback"),
    ("64:ff9b::a00:5", 443, "private"),
    ("93.184.216.34", 8080, "port 8080 is not 80 or 443"),
])
def test_the_guard_refuses_a_connection_before_it_is_made(host, port, why):
    out = child(CONNECT.format(host=host, port=port))
    assert why in out["refused"]
    assert why in out["first"], "REFUSED does not record the reason"


def test_the_guard_refuses_a_socket_that_is_not_an_internet_one():
    out = child("""
        fetcher.install_guard(guard._forbidden, guard.ALLOWED_PORTS)
        try:
            socket.socket(socket.AF_UNIX).connect("/nonexistent/socket")
        except ConnectionRefusedError as exc:
            print(json.dumps({"refused": str(exc)}))
    """)
    assert out["refused"] == "refused: not an internet socket"


def test_the_guard_refuses_a_name_that_answers_a_private_address():
    """Every answer getaddrinfo gives is checked, so a name rebound to the LAN
    is refused before anything connects to it."""
    out = child("""
        def lying(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.5", port))]
        socket.getaddrinfo = lying
        fetcher.install_guard(guard._forbidden, guard.ALLOWED_PORTS)
        try:
            socket.getaddrinfo("nas.example", 443)
        except ConnectionRefusedError as exc:
            print(json.dumps({"refused": str(exc), "first": fetcher.REFUSED[0]}))
    """)
    assert out["first"] == "nas.example: 192.168.1.5 is a private address"


def test_the_first_refusal_decides_the_message():
    out = child("""
        fetcher.install_guard(guard._forbidden, guard.ALLOWED_PORTS)
        for address in (("10.0.0.1", 443), ("127.0.0.1", 443)):
            try:
                socket.socket().connect(address)
            except ConnectionRefusedError:
                pass
        print(json.dumps(fetcher.failure(OSError("anything"))))
    """)
    assert out == {"error": "refusing to fetch: 10.0.0.1 is a private address",
                   "code": "refused"}


def test_a_probe_of_a_loopback_link_is_refused_and_writes_nothing(tmp_path):
    """The whole child, run as voice-ui runs it: the guard is in before yt_dlp
    is imported, so the extractor's own first request is refused."""
    out = subprocess.run([sys.executable, "-I", str(SCRIPT), "probe", "--",
                          "http://127.0.0.1/talk"], cwd=tmp_path, capture_output=True,
                         text=True, timeout=120, env={"PATH": os.defpath, "LANG": "C.UTF-8",
                                                      "HOME": "/nonexistent",
                                                      "YTDLP_NO_PLUGINS": "1"})
    assert out.returncode == 1, out.stderr
    [line] = out.stdout.splitlines()
    assert json.loads(line) == {"error": "refusing to fetch: 127.0.0.1: 127.0.0.1 is loopback",
                                "code": "refused"}
    assert list(tmp_path.iterdir()) == []


class _V6Server(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


@pytest.fixture
def two_hosts():
    """127.0.0.1, which the stand-in rule allows, and ::1, which it refuses.

    127.0.0.1 answers /redirect with a 302 to ::1, and /page with a page whose
    <video> is on ::1. ::1 serves anything and records every request.
    """
    reached: list[str] = []
    ports: dict[str, int] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.server.address_family == socket.AF_INET6:
                reached.append(self.path)
            elsewhere = f"http://[::1]:{ports['v6']}"
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"{elsewhere}/tone.wav")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/page":
                body = (f'<html><head><title>t</title></head><body>'
                        f'<video src="{elsewhere}/tone.mp4"></video></body></html>').encode()
                kind = "text/html"
            else:
                body, kind = tone(), "audio/wav"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_HEAD = do_GET

        def log_message(self, *args):
            pass

    try:
        v6 = _V6Server(("::1", 0), Handler)
    except OSError:
        pytest.skip("this machine has no IPv6 loopback")
    v4 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ports.update(v4=v4.server_address[1], v6=v6.server_address[1])
    for server in (v4, v6):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield ports, reached
    for server in (v4, v6):
        server.shutdown()


THROUGH_YT_DLP = """
    import os
    rule = (lambda address: None) if {open_!r} else (
        lambda address: None if address == "127.0.0.1" else guard._forbidden(address))
    fetcher.install_guard(rule, {{{v4}, {v6}}})
    os.chdir({cwd!r})
    url = "http://127.0.0.1:{v4}/{path}"
    try:
        if {mode!r} == "probe":
            fetcher.probe(url)
        else:
            fetcher.fetch("audio", 10 * 2**20, "-", url)
    except BaseException as exc:
        fetcher.say(**fetcher.failure(exc, 10 * 2**20))
"""


@pytest.mark.parametrize("path,mode", [("redirect", "probe"), ("redirect", "fetch"),
                                       ("page", "fetch")])
def test_the_guard_refuses_a_second_hop_that_yt_dlp_itself_makes(two_hosts, tmp_path,
                                                                  path, mode):
    """A redirect, or a media URL found inside a page, through yt-dlp's own
    HTTP stack: the request it makes on its own is refused before a packet
    leaves. This is what a yt-dlp bump with a network stack of its own breaks.
    A probe takes a page's facts without fetching its media, so it has no
    second hop to refuse there."""
    ports, reached = two_hosts
    out = child(THROUGH_YT_DLP.format(open_=False, cwd=str(tmp_path), path=path, mode=mode,
                                      **ports))
    assert out == {"error": "refusing to fetch: ::1: ::1 is loopback", "code": "refused"}
    assert reached == [], "a request reached the address the rule refuses"
    assert not list(tmp_path.glob("media.*"))


def test_without_the_rule_the_second_hop_is_made(two_hosts, tmp_path):
    """The control for the test above: the refusal is the guard's, not the setup's."""
    ports, reached = two_hosts
    out = child(THROUGH_YT_DLP.format(open_=True, cwd=str(tmp_path), path="page",
                                      mode="fetch", **ports))
    assert out["ok"] is True, out
    assert reached == ["/tone.mp4"]


def test_a_child_cannot_write_past_its_cap(tmp_path):
    """RLIMIT_FSIZE, with SIGXFSZ ignored: the write fails with EFBIG and the
    process goes on to say so, where the default action would kill it silently."""
    target = tmp_path / "media.wav"
    out = child(f"""
        import resource
        fetcher.prepare(1000)
        limit = resource.getrlimit(resource.RLIMIT_FSIZE)
        try:
            with open({str(target)!r}, "wb") as out:
                out.write(b"x" * 2000)
            failed = None
        except OSError as exc:
            failed = exc.errno
        print(json.dumps({{"limit": limit, "errno": failed}}))
    """)
    assert out["limit"] == [1000, 1000]
    assert out["errno"] == errno.EFBIG
    assert target.stat().st_size <= 1000


# -------------------------------------------------------------------- argv --


@pytest.mark.parametrize("argv", [
    ["download", "--", "https://media.example/x"],
    ["fetch", "music", "100", "-", "--", "https://media.example/x"],
    ["fetch", "audio", "lots", "-", "--", "https://media.example/x"],
    ["fetch", "audio", str(2**31 + 1), "-", "--", "https://media.example/x"],
    ["fetch", "captions", "100", ".*", "--", "https://media.example/x"],
    ["fetch", "audio", "100", "-", "https://media.example/x"],
    ["probe", "https://media.example/x"],
    ["probe", "--", "https://media.example/x", "https://media.example/y"],
])
def test_bad_arguments_are_one_error_line_and_exit_2(argv, tmp_path):
    out = subprocess.run([sys.executable, "-I", str(SCRIPT), *argv], cwd=tmp_path,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 2
    assert [json.loads(line) for line in out.stdout.splitlines()] == [
        {"error": "bad arguments", "code": "failed"}]


def test_good_arguments_parse():
    assert fetcher.parse(["probe", "--", "https://x.example/"]) == (
        "probe", "audio", 0, "-", "https://x.example/")
    assert fetcher.parse(["fetch", "captions", "8388608", "pt-BR", "--", "https://x.example/"]) == (
        "fetch", "captions", 8388608, "pt-BR", "https://x.example/")


# ----------------------------------------------------------------- formats --


def f(id, ext, ac, vc, h=None, abr=None, proto="https", tbr=None):  # noqa: A002
    return dict(format_id=id, ext=ext, acodec=ac, vcodec=vc, height=h, abr=abr,
                tbr=tbr or abr or 1000, protocol=proto, url=f"https://e.example/{id}")


LISTS = {
    "youtube": [f("251", "webm", "opus", "none", abr=130), f("250", "webm", "opus", "none", abr=70),
                f("140", "m4a", "mp4a.40.2", "none", abr=129),
                f("137", "mp4", "none", "avc1", h=1080, tbr=4000),
                f("248", "webm", "none", "vp9", h=1080, tbr=3000),
                f("hls-1", "mp4", "mp4a", "avc1", h=720, proto="m3u8_native", tbr=2500)],
    "muxed 360-1080": [f("m360", "mp4", "mp4a", "avc1", h=360, tbr=700),
                       f("m720", "mp4", "mp4a", "avc1", h=720, tbr=2000),
                       f("m1080", "mp4", "mp4a", "avc1", h=1080, tbr=4000)],
    "muxed 720-1080": [f("m720", "mp4", "mp4a", "avc1", h=720, tbr=2000),
                       f("m1080", "mp4", "mp4a", "avc1", h=1080, tbr=4000)],
    "hls only": [f("h480", "mp4", "mp4a", "avc1", h=480, proto="m3u8_native", tbr=900),
                 f("h1080", "mp4", "mp4a", "avc1", h=1080, proto="m3u8", tbr=4000)],
    "dash": [f("a1", "m4a", "mp4a", "none", abr=128, proto="http_dash_segments"),
             f("v1", "mp4", "none", "avc1", h=720, proto="http_dash_segments")],
    "direct": [dict(format_id="0", ext="mp3", url="https://e.example/a.mp3", protocol="https")],
}

CHOSEN = {  # list: (audio, clip, video); None is "Requested format is not available"
    "youtube": ("250", "140", None),
    "muxed 360-1080": ("m360", "m360", "m720"),
    "muxed 720-1080": ("m720", "m720", "m720"),
    "hls only": (None, None, None),
    "dash": ("a1", "a1", None),
    "direct": ("0", "0", "0"),
}


@pytest.mark.parametrize("name", list(LISTS))
def test_the_formats_pick_one_native_file_and_never_hls(name):
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError, ExtractorError

    picked = []
    for kind in ("audio", "clip", "video"):
        info = {"id": "x", "title": "t", "formats": [dict(x) for x in LISTS[name]],
                "extractor": "generic", "extractor_key": "Generic",
                "webpage_url": "https://e.example/"}
        with YoutubeDL({**fetcher.PROBE, "format": fetcher.FORMATS[kind]}) as ydl:
            try:
                chosen = ydl.process_ie_result(info, download=False)
            except (DownloadError, ExtractorError) as exc:
                assert "Requested format is not available" in str(exc)
                picked.append(None)
                continue
        assert not str(chosen.get("protocol")).startswith("m3u8"), (name, kind)
        picked.append(chosen["format_id"])
    assert tuple(picked) == CHOSEN[name]


def test_an_unavailable_format_says_there_is_no_ffmpeg():
    said = fetcher.failure(Exception(
        "ERROR: [generic] x: Requested format is not available. Use --list-formats"))
    assert said == {"error": fetcher.NO_FFMPEG, "code": "failed"}


def test_an_error_is_one_line_without_its_prefix_and_at_most_300_characters():
    said = fetcher.failure(Exception("ERROR: [generic] " + "x" * 400 + "\nsecond line"))
    assert said["code"] == "failed"
    assert said["error"].startswith("[generic] x") and len(said["error"]) == 300


def test_the_parameters_set_none_of_the_options_that_must_never_be_set():
    params = fetcher.fetch_params("audio", 1000, "-")
    captions = fetcher.fetch_params("captions", 1000, "en")
    assert set(fetcher.BASE) == {
        "quiet", "no_warnings", "noprogress", "noplaylist", "extract_flat",
        "allowed_extractors", "postprocessors", "cachedir", "updatetime", "proxy",
        "socket_timeout", "retries", "fragment_retries", "extractor_retries",
        "concurrent_fragment_downloads"}
    assert set(fetcher.PROBE) - set(fetcher.BASE) == {"simulate", "format"}
    assert set(params) - set(fetcher.BASE) == {
        "paths", "outtmpl", "restrictfilenames", "max_filesize", "match_filter",
        "progress_hooks", "format"}
    assert set(captions) - set(fetcher.BASE) == {
        "paths", "outtmpl", "restrictfilenames", "max_filesize", "match_filter",
        "progress_hooks", "skip_download", "writesubtitles", "writeautomaticsub",
        "subtitleslangs", "subtitlesformat"}
    for each in (fetcher.BASE, fetcher.PROBE, params, captions):
        assert not NEVER & set(each)
        assert each["postprocessors"] == [] and each["proxy"] == ""
    assert captions["subtitleslangs"] == ["en"]
    assert params["max_filesize"] == 1000


def test_every_format_is_a_native_file_over_http():
    for selector in fetcher.FORMATS.values():
        for alternative in selector.split("/"):
            assert "[protocol^=http]" in alternative, alternative
            assert "+" not in alternative, "a merge needs ffmpeg"


def test_the_match_filter_takes_a_video_and_refuses_a_live_stream():
    accept = fetcher.fetch_params("audio", 1000, "-")["match_filter"]
    assert accept({"id": "x", "title": "t"}) is None
    assert accept({"id": "x", "title": "t", "is_live": True}) is not None
    assert accept({"id": "x", "title": "t", "live_status": "is_upcoming"}) is not None


def test_no_native_network_stack_is_installed():
    """curl_cffi resolves and connects in C, where the guard cannot see it."""
    assert importlib.util.find_spec("curl_cffi") is None


# ------------------------------------------------------------------- facts --


def test_facts_use_the_selected_format_s_size():
    info = {"title": "T", "duration": 60, "filesize": 123_456, "abr": 64,
            "formats": [{"vcodec": "none", "acodec": "opus", "protocol": "https"}]}
    out = fetcher.facts(info)
    assert out["bytes"] == 123_456 and out["duration"] == 60.0
    assert out["video"] is False


def test_facts_fall_back_to_duration_times_bitrate_and_then_a_mebibyte_a_minute():
    assert fetcher.facts({"duration": 100, "abr": 64})["bytes"] == 100 * 64 * 125
    assert fetcher.facts({"duration": 120})["bytes"] == 2 * 1024**2
    assert fetcher.facts({})["bytes"] is None and fetcher.facts({})["duration"] is None


def test_facts_say_whether_there_is_one_file_with_picture_and_sound():
    muxed = {"formats": [{"vcodec": "avc1", "acodec": "mp4a", "protocol": "https"}]}
    hls = {"formats": [{"vcodec": "avc1", "acodec": "mp4a", "protocol": "m3u8_native"}]}
    assert fetcher.facts(muxed)["video"] is True
    assert fetcher.facts(hls)["video"] is False


def test_subtitles_a_person_wrote_and_the_language_to_ask_for():
    def subs(*langs, language=None):
        return fetcher.facts({"language": language,
                              "subtitles": {lang: [{"ext": "vtt"}] for lang in langs}})
    assert subs()["has_subtitles"] is False
    chat = subs("live_chat")
    assert (chat["has_subtitles"], chat["subtitles_lang"]) == (False, None)
    assert subs("de", "en-GB")["subtitles_lang"] == "en-GB"
    assert subs("de", "pt", language="pt")["subtitles_lang"] == "pt"
    assert subs("de", "fr")["subtitles_lang"] == "de"
    assert subs("x.*")["subtitles_lang"] is None, "a language that is not a tag is not passed on"


def test_titles_and_uploaders_are_capped():
    out = fetcher.facts({"title": "t" * 400, "channel": "c" * 400})
    assert len(out["title"]) == 300 and len(out["uploader"]) == 200


# --------------------------------------------------------- one real fetch --


def tone() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x01" * 16000)
    return buffer.getvalue()


@pytest.fixture
def served():
    body = tone()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/tone.wav", body
    server.shutdown()


def test_a_real_fetch_writes_one_media_file_and_says_ok(served, tmp_path, monkeypatch, capsys):
    """Without the guard, against loopback: this is what catches a yt-dlp pin
    that changes what extract_info returns or what it names the file."""
    url, body = served
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fetcher, "TOO_BIG", [])
    assert fetcher.fetch("audio", 10 * 2**20, "-", url) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1] == {"ok": True, "video": False}
    assert [p.name for p in tmp_path.iterdir()] == ["media.wav"]
    assert (tmp_path / "media.wav").read_bytes() == body


def test_a_real_fetch_over_the_cap_writes_nothing_and_says_so(served, tmp_path, monkeypatch,
                                                               capsys):
    url, _ = served
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fetcher, "TOO_BIG", [])
    assert fetcher.fetch("audio", 1000, "-", url) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1]["code"] in ("failed", "too_big")
    assert not list(tmp_path.glob("media.*"))


@pytest.fixture
def unsized():
    """200 kB that max_filesize cannot see coming: chunked with no Content-Length,
    or gzipped with the Content-Length of the 200 bytes that cross the wire."""
    body = tone() + bytes(200_000)
    packed = gzip.compress(body)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Connection", "close")
            if self.path.startswith("/chunked"):
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for start in range(0, len(body), 8192):
                    piece = body[start:start + 8192]
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(packed)))
                self.end_headers()
                self.wfile.write(packed)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", len(packed)
    server.shutdown()


@pytest.mark.parametrize("path", ["chunked.wav", "gzip.wav"])
def test_a_body_with_no_size_up_front_is_stopped_by_the_progress_hook(unsized, path, tmp_path,
                                                                      monkeypatch, capsys):
    """max_filesize reads Content-Length and nothing else. Past the cap, the
    progress hook is what stops these, and too_big says it was the hook."""
    base, packed = unsized
    assert packed < 1000, "the gzipped body must look small on the wire"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fetcher, "TOO_BIG", [])
    try:
        fetcher.fetch("audio", 1000, "-", f"{base}/{path}")
    except Exception as exc:  # noqa: BLE001 - as main() does
        fetcher.say(**fetcher.failure(exc, 1000))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1]["code"] == "too_big", lines[-1]
    assert not (tmp_path / "media.wav").exists()


# ---------------------------------------------------------------- memory --


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the limits are Linux's")
def test_a_child_is_held_to_its_memory_and_is_killed_first():
    out = child("""
        import resource
        fetcher.prepare(0)
        limit = resource.getrlimit(resource.RLIMIT_DATA)
        score = open("/proc/self/oom_score_adj").read().strip()
        try:
            hog = bytearray(512 * 2**20)
            said = None
        except MemoryError as exc:
            said = fetcher.failure(exc)
        print(json.dumps({"limit": limit, "score": score, "said": said}))
    """)
    assert out["limit"] == [fetcher.CHILD_MEMORY, fetcher.CHILD_MEMORY]
    assert out["score"] == "1000"
    assert out["said"] is not None and out["said"]["code"] == "failed"
