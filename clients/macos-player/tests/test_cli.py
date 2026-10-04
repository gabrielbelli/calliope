"""calliope, the command line: Markdown to speech, the arguments, and every command.

NOTHING HERE SPEAKS OR LAUNCHES ANYTHING. save, transcribe, voices and status run in-process
against a fake proxy on 127.0.0.1:0, with CALLIOPE_NO_LAUNCH=1 and `launch_app` replaced by a
failure. speak runs as the user runs it -- through the sh wrapper, by a symlink -- but out of a
fake Calliope.app whose player is a shell script that writes down what it was given, under a
temporary HOME so the real queue and pid file are never touched.
"""
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI_DIR = ROOT / "cli"


def load_cli():
    spec = importlib.util.spec_from_file_location("calliope_cli", CLI_DIR / "calliope.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_cli()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No launches, no real preferences, and polls that do not wait."""
    monkeypatch.setenv("CALLIOPE_NO_LAUNCH", "1")
    monkeypatch.setattr(cli, "read_prefs", lambda: {})
    monkeypatch.setattr(cli, "_prefs_cache", [])
    monkeypatch.setattr(cli, "launch_app", lambda: pytest.fail("the tests tried to launch Calliope"))
    monkeypatch.setattr(cli, "POLL_SECONDS", 0.01)


def use_prefs(monkeypatch, settings):
    monkeypatch.setattr(cli, "read_prefs", lambda: settings)
    monkeypatch.setattr(cli, "_prefs_cache", [])


# ---------------------------------------------------------------------- Markdown to speech --

md = cli.markdown_to_speech


def test_front_matter_goes():
    assert md("---\ntitle: Note\ntags: [a, b]\n---\nHello there.") == "Hello there."


def test_a_rule_at_the_top_without_a_closing_one_is_not_front_matter():
    assert md("---\nHello there.") == "Hello there."


@pytest.mark.parametrize("fence", ["```", "```python", "~~~", "````"])
def test_fenced_code_goes(fence):
    closing = fence.rstrip("python")
    text = f"Before.\n\n{fence}\nprint('no')\n# not a heading\n{closing}\n\nAfter."
    assert md(text) == "Before.\n\nAfter."


def test_indented_code_goes_but_a_list_continuation_stays():
    assert md("Before.\n\n    code()\n    more()\n\nAfter.") == "Before.\n\nAfter."
    assert md("- First item\n\n    More about it") == "First item.\n\nMore about it"


def test_html_tags_and_comments_go_and_their_text_stays():
    text = "<!-- hidden\nacross lines -->\n<details><summary>Open</summary>\nInside &amp; out.</details>"
    assert md(text) == "Open Inside & out."


def test_images_go_and_link_text_stays():
    text = ("![A diagram](img/a.png) See [the guide](https://example.com/a_(b) \"Guide\") "
            "and [the other one][ref].\n\n[ref]: https://example.com/other \"Other\"")
    assert md(text) == "See the guide and the other one."


def test_horizontal_rules_go():
    assert md("One.\n\n***\n\nTwo.\n\n- - -\n\nThree.\n\n___") == "One.\n\nTwo.\n\nThree."


def test_inline_markup_is_unwrapped():
    text = "Run `make test`, then **really** *check* ~~twice~~ ***now*** __here__ _there_."
    assert md(text) == "Run make test, then really check twice now here there."


def test_identifiers_and_arithmetic_keep_their_characters():
    assert md("Set snake_case_name to 2 * 3 * 4.") == "Set snake_case_name to 2 * 3 * 4."
    assert md(r"Literal \*stars\* and `a_b_c`.") == "Literal *stars* and a_b_c."


@pytest.mark.parametrize("text, spoken", [
    ("# Title", "Title."),
    ("### Is it? ###", "Is it?"),
    ("## Steps:", "Steps:"),
    ("Setext title\n============", "Setext title."),
    ("Second level\n---", "Second level."),
    ("#hashtag is not a heading", "#hashtag is not a heading"),
])
def test_headings_lose_their_marks_and_end_a_sentence(text, spoken):
    assert md(text) == spoken


def test_list_items_lose_their_markers_and_end_a_sentence():
    text = "- one\n* two!\n+ three\n1. four\n2) five\n- [ ] six\n- [x] seven."
    assert md(text) == "one.\ntwo!\nthree.\nfour.\nfive.\nsix.\nseven."


def test_a_list_item_wrapped_over_lines_is_one_item():
    assert md("- a long item\n  that wraps\n- next") == "a long item that wraps.\nnext."


def test_blockquote_markers_go():
    assert md("> Quoted\n> > nested line") == "Quoted nested line"


def test_table_rows_become_their_cells():
    text = "| Name | Value |\n|:-----|------:|\n| a | 1 |\n| b \\| c | `2` |\n\nAfter."
    assert md(text) == "Name, Value.\na, 1.\nb | c, 2.\n\nAfter."


def test_bare_urls_go_and_their_punctuation_stays():
    text = "Read https://example.com/page, or www.example.com. Or <https://example.com/x> (see https://example.com)."
    assert md(text) == "Read, or. Or (see)."


def test_paragraph_breaks_stay_and_soft_wraps_join():
    text = "First line\nsame paragraph.\n\n\n\nSecond paragraph.\n\n"
    assert md(text) == "First line same paragraph.\n\nSecond paragraph."


def test_a_hard_break_keeps_its_line():
    assert md("One  \nTwo\\\nThree") == "One\nTwo\nThree"


def test_windows_line_endings_and_a_bom():
    assert md("﻿# Title\r\n\r\nBody.\r\n") == "Title.\n\nBody."


# ------------------------------------------------------------------------- the arguments --

def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


def test_speak_takes_text_a_file_or_standard_input():
    args = parse("speak", "Hello", "there", "--reader", "--voice", "mac/af_heart", "--wait")
    assert (args.text, args.reader, args.voice, args.wait) == (["Hello", "there"], True, "mac/af_heart", True)
    assert parse("speak", "-").text == ["-"]
    assert parse("speak", "-f", "notes.md").file == "notes.md"


def test_markdown_and_plain_exclude_each_other():
    with pytest.raises(SystemExit):
        parse("speak", "--markdown", "--plain", "x")


def test_save_needs_an_output():
    with pytest.raises(SystemExit):
        parse("save", "Hello")
    args = parse("save", "Hello", "-o", "out.wav", "--model", "calliope/x", "--language", "pt")
    assert (args.output, args.model, args.language) == ("out.wav", "calliope/x", "pt")


def test_a_command_is_required():
    with pytest.raises(SystemExit):
        parse()


def test_text_and_a_file_together_are_refused(capsys):
    assert cli.main(["speak", "Hello", "-f", "notes.md"]) == 1
    assert "not both" in capsys.readouterr().err


def test_nothing_to_say_is_refused(capsys):
    assert cli.main(["save", "-o", "x.wav"]) == 1
    assert "nothing to say" in capsys.readouterr().err


def test_the_port_comes_from_the_environment_then_the_settings(monkeypatch):
    monkeypatch.setenv("CALLIOPE_PORT", "50001")
    assert cli.proxy_port() == 50001
    monkeypatch.delenv("CALLIOPE_PORT")
    use_prefs(monkeypatch, {"proxyPort": 50002})
    assert cli.proxy_port() == 50002
    use_prefs(monkeypatch, {})
    assert cli.proxy_port() == 47815


@pytest.mark.parametrize("settings, language, ref", [
    ({}, "en", "mac/af_heart"),
    ({}, "pt", "mac/pf_dora"),
    ({"macOn": False}, "ja", "calliope/jf_alpha"),
    ({"voices": {"en": "calliope/am_adam"}, "macOn": True}, "en", "calliope/am_adam"),
    ({"voices": {"en": "calliope/am_adam"}}, "fr", "mac/ff_siwis"),
    ({}, "xx", None),
])
def test_the_default_voice_is_the_players(settings, language, ref):
    assert cli.default_ref(settings, language) == ref


# ------------------------------------------------------------------------ the fake proxy --

DETAIL = [
    {"name": "af_heart", "origin": "mac", "ref": "mac/af_heart", "language": "en", "model": "kokoro"},
    {"name": "bf_emma", "origin": "mac", "ref": "mac/bf_emma", "language": "en", "model": "kokoro"},
    {"name": "pf_dora", "origin": "mac", "ref": "mac/pf_dora", "language": "pt", "model": "kokoro"},
    {"name": "af_heart", "origin": "calliope", "ref": "calliope/af_heart", "language": "en",
     "model": "calliope/kokoro"},
    {"name": "only_remote", "origin": "calliope", "ref": "calliope/only_remote", "language": "en",
     "model": "calliope/kokoro"},
    {"name": "narrator", "origin": "calliope", "ref": "calliope/narrator", "language": None,
     "model": "calliope/kokoro"},
]
LOCAL = {"kokoro", "tts-1", "tts-1-hd"}


def error(code, message):
    return {"error": {"code": code, "message": message}}


LINK = "https://video.example/watch?v=talk"
LINK_WITH_CAPTIONS = "https://video.example/watch?v=captioned"
MEDIA = bytes(range(256)) * 1024
VTT = ("WEBVTT\n\n00:00:00.000 --> 00:00:02.500\nHello <i>there</i>,\n\n"
       "00:00:02.500 --> 00:00:04.000\nfrom the &amp; captions.\n")
SRT = "1\n00:00:00,000 --> 00:00:01,500\nFrom the link.\n"
NOW = time.time()
RECORDS = [
    {"id": "0b8f7e1c-1d2a-4e5b-8c9d-0123456789ab", "status": "done", "kind": "clone",
     "engine": "chatterbox", "created_at": NOW - 7200, "voice": "narrator", "format": "mp3",
     "audio": {"state": "present", "format": "mp3", "bytes": 10,
               "url": "/jobs/0b8f7e1c-1d2a-4e5b-8c9d-0123456789ab/audio"},
     "text_preview": "Once upon a time", "text": "Once upon a time, there was a voice."},
    {"id": "7c4a9e2d-0000-4e5b-8c9d-0123456789ab", "status": "running", "kind": "clone",
     "engine": "chatterbox", "created_at": NOW - 30, "format": "mp3",
     "audio": {"state": "pending"}, "text_preview": "A long story"},
    {"id": "0b8f0000-aaaa-4e5b-8c9d-0123456789ab", "status": "done", "kind": "transcribe",
     "engine": "parakeet", "created_at": NOW - 90000, "audio": {"state": "never"},
     "text_preview": "a meeting"},
]
GLOSSARIES = [
    {"name": "tech", "owner": "system", "source": "builtin", "terms": 84, "replacements": 4,
     "hotwords": 80, "writable": False},
    {"name": "mine", "owner": "u_abcdefghijklmnop", "source": "custom", "terms": 2,
     "replacements": 1, "hotwords": 1, "writable": True},
]
MINE = "# mine\nCalliope\ncloud code = Claude Code\n"
MODELS = [
    {"id": "kokoro", "owned_by": "calliope-local"}, {"id": "tts-1", "owned_by": "calliope-local"},
    {"id": "calliope/kokoro", "owned_by": "calliope-remote", "served_by": "tts-stack"},
    {"id": "calliope/chatterbox", "owned_by": "calliope-remote", "served_by": "tts-long"},
    {"id": "calliope/parakeet", "owned_by": "calliope-remote", "served_by": "stt-stack"},
    {"id": "calliope/whisper-1", "owned_by": "calliope-remote", "served_by": "stt-stack"},
]


def parse_multipart(content_type, body):
    """({name: value or (filename, bytes)}, [(name, value)...] in order)."""
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    fields, pairs = {}, []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        # UTF-8, as Starlette decodes a field that names no charset of its own;
        # get_content() would read it as ASCII and mangle "São Paulo".
        payload = part.get_payload(decode=True)
        fields[name] = ((part.get_filename(), payload)
                        if part.get_filename() else payload.decode("utf-8"))
        pairs.append((name, fields[name]))
    return fields, pairs


class State:
    def __init__(self):
        self.requests = []
        self.healthy = True
        self.test = {"configured": True, "ok": True, "url": "https://calliope.example",
                     "message": "Calliope server works",
                     "checks": [{"name": "reach", "ok": True, "ms": 12, "detail": "up"},
                                {"name": "key", "ok": True, "ms": 20, "detail": "accepted"}]}
        self.jobs = {"job-ok-0123456789": ["queued", "running", "done"],
                     "job-bad-0123456789": ["running", "failed"]}
        self.transcription = None
        # A Calliope server behind the proxy; False answers its routes as the proxy does
        # when none is configured.
        self.server = True
        self.repaired = None
        # What /ui/progress answers after a commit, one answer per poll, the last repeated.
        self.download = [
            {"where": "queue", "status": "pending", "ready": False},
            {"where": "queue", "status": "downloading", "ready": False, "percent": 40.0,
             "speed": 2000000, "eta": 2},
            {"where": "done", "status": "finished", "ready": True,
             "filename": "A talk, recorded.weba"}]
        self.progress = []
        self.records = {job["id"]: dict(job) for job in RECORDS}

    def to(self, method, path):
        """Every request for one route, in order."""
        return [r for r in self.requests
                if r["method"] == method and r["path"].split("?")[0] == path]

    def bodies(self, method, path):
        return [json.loads(r["body"]) for r in self.to(method, path)]

    def speeches(self):
        return [json.loads(r["body"]) for r in self.requests
                if r["method"] == "POST" and r["path"] == "/v1/audio/speech"]


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type="application/json", headers=()):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_PUT(self):
            self.route("PUT")

        def do_DELETE(self):
            self.route("DELETE")

        def route(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            state.requests.append({"method": method, "path": self.path,
                                   "headers": dict(self.headers), "body": body})
            path = self.path
            if path == "/health":
                return self.send(200 if state.healthy else 503, "ok", "text/plain")
            if self.headers.get("Origin"):
                return self.send(403, error("browser_not_allowed", "no browsers"))
            if method == "GET" and path == "/status":
                return self.send(200, {"port": self.server.server_address[1],
                                       "mac": {"on": True, "loaded": False, "keep_loaded": False,
                                               "idle_seconds": 600},
                                       "calliope": {"on": True, "url": "https://calliope.example"}})
            if method == "GET" and path == "/calliope/test":
                return self.send(200, state.test)
            if method == "GET" and path == "/voices":
                return self.send(200, {"voices": sorted({d["name"] for d in DETAIL}), "detail": DETAIL})
            if method == "POST" and path == "/v1/audio/speech":
                return self.speech(json.loads(body))
            bare, _, query_string = path.partition("?")
            query = urllib.parse.parse_qs(query_string)
            if not state.server and bare.startswith(("/ui/", "/glossaries", "/jobs",
                                                     "/v1/audio/transcriptions",
                                                     "/v1/audio/translations")):
                return self.send(404, error("no_calliope_server",
                                            "No Calliope server is configured to send this to"))
            if method == "GET" and bare == "/v1/models":
                return self.send(200, {"object": "list", "data": MODELS})
            if method == "GET" and bare == "/jobs":
                return self.send(200, {"jobs": list(state.records.values()), "counts": {},
                                       "truncated": False})
            match = re.fullmatch(r"/jobs/([\w-]+)(/audio)?", bare)
            if match and match.group(1) in state.records:
                job = state.records[match.group(1)]
                if method == "GET" and match.group(2):
                    return self.send(200, b"LONG-AUDIO", "audio/mpeg")
                if method == "GET":
                    return self.send(200, job)
                if match.group(2):
                    return self.send(200, {"id": job["id"], "audio": {"state": "deleted"}})
                live = job["status"] in ("queued", "running")
                return self.send(200, {"id": job["id"], "status": "cancelling" if live else "deleted"})
            if method == "GET" and match:
                job_id = match.group(1)
                if match.group(2):
                    return self.send(200, b"LONG-AUDIO", "audio/mpeg")
                if job_id not in state.jobs:
                    return self.send(404, {"detail": "job %s not found" % job_id})
                status = state.jobs[job_id].pop(0)
                job = {"id": job_id, "status": status, "format": "mp3", "estimated_seconds": 3}
                if status == "failed":
                    job["error"] = "the GPU ran out of memory"
                return self.send(200, job)
            if method == "POST" and path == "/v1/audio/transcriptions":
                fields, pairs = parse_multipart(self.headers["Content-Type"], body)
                state.transcription = fields
                # Every part in order, because a repeated field such as keywords[] is the point.
                state.transcription_parts = pairs
                headers = [("X-Glossary-Repaired", state.repaired)] if state.repaired else []
                return self.transcript(fields.get("response_format"), " Hello there. ", headers)
            if method == "POST" and path == "/v1/audio/translations":
                state.translation, state.translation_parts = parse_multipart(
                    self.headers["Content-Type"], body)
                return self.transcript(state.translation.get("response_format"),
                                       "Hello, in English.")
            if bare == "/ui/resolve":
                url = json.loads(body)["url"]
                if "nope" in url:
                    return self.send(400, error("unresolvable", "Could not read that link"))
                return self.send(200, {"token": url, "title": "A talk, recorded",
                                       "uploader": "Somebody", "duration": 192.0,
                                       "bytes": 3200000, "is_live": False,
                                       "has_subtitles": url == LINK_WITH_CAPTIONS,
                                       "video": True, "probed": True, "confirm": False})
            if bare == "/ui/commit":
                state.progress = [dict(answer) for answer in state.download]
                return self.send(200, {"token": json.loads(body)["token"], "status": "started"})
            if bare == "/ui/progress":
                answer = state.progress.pop(0) if len(state.progress) > 1 else state.progress[0]
                return self.send(200, dict(answer, token=query["token"][0]))
            if bare == "/ui/fetch":
                return self.transcript(query.get("response_format", ["json"])[0],
                                       " From the link. ")
            if bare == "/ui/captions":
                return self.send(200, {"token": json.loads(body)["token"],
                                       "filename": "A talk, recorded.vtt", "format": "vtt",
                                       "text": VTT})
            if bare == "/ui/media":
                return self.send(200, MEDIA, "audio/webm")
            if bare == "/ui/abandon":
                return self.send(200, {"token": json.loads(body)["token"], "reaped": True})
            if method == "GET" and bare == "/glossaries":
                return self.send(200, {"glossaries": GLOSSARIES, "writable": True, "default": []})
            match = re.fullmatch(r"/glossaries/([\w.-]+)", bare)
            if match:
                name = match.group(1)
                if method == "GET":
                    if name != "mine":
                        return self.send(404, {"detail": "no glossary profile named %r" % name})
                    return self.send(200, dict(GLOSSARIES[1], replacements={
                        "cloud code": "Claude Code"}, hotwords=["Calliope"], text=MINE))
                if method == "DELETE":
                    return self.send(200, {"name": name, "deleted": True})
                text = body.decode("utf-8")
                if " = = " in text:
                    return self.send(400, {"detail": {
                        "message": "1 line(s) rejected; nothing was written. 1 term(s) "
                                   "would have been accepted.",
                        "accepted": 1,
                        "rejected": [{"line": 2, "text": "a = = b", "reason": "two '='"}]}})
                lines = [line for line in text.splitlines() if line and not line.startswith("#")]
                return self.send(201 if name != "mine" else 200, {
                    "name": name, "owner": "u_abcdefghijklmnop", "source": "custom",
                    "terms": len(lines), "replacements": sum("=" in line for line in lines),
                    "hotwords": sum("=" not in line for line in lines), "writable": True,
                    "forced": query.get("force") == ["true"], "created": name != "mine"})
            if method == "GET" and bare == "/ui/clips":
                return self.send(200, {"voices": [
                    {"name": "narrator", "owner": "u_abcdefghijklmnop", "bytes": 540000,
                     "modified": NOW - 3600, "seconds": 12.3}], "writable": True,
                    "max_seconds": 30})
            if method == "POST" and bare == "/ui/clips":
                state.clip, _ = parse_multipart(self.headers["Content-Type"], body)
                return self.send(201, {"voice": {"name": state.clip["name"].lower(),
                                                 "seconds": 12.3}, "voices": []})
            match = re.fullmatch(r"/ui/clips/([\w-]+)", bare)
            if method == "DELETE" and match:
                if match.group(1) != "narrator":
                    return self.send(404, error("unknown_voice",
                                                "no voice called %r" % match.group(1)))
                return self.send(200, {"deleted": "narrator", "voices": []})
            return self.send(404, error("not_found", "no route " + path))

        def transcript(self, fmt, text, headers=()):
            fmt = fmt or "json"
            if fmt == "json":
                return self.send(200, {"text": text}, headers=headers)
            if fmt == "verbose_json":
                return self.send(200, {"text": text.strip(), "segments": [
                    {"start": 0.0, "end": 1.5, "text": text.strip()}]}, headers=headers)
            if fmt == "srt":
                return self.send(200, SRT, "text/plain; charset=utf-8", headers)
            return self.send(200, text.strip() + "\n", "text/plain; charset=utf-8", headers)

        def speech(self, request):
            model, voice, fmt = request.get("model"), request.get("voice"), request.get("response_format")
            if voice == "missing":
                return self.send(404, error("voice_not_found", "no voice called\nmissing"))
            if model in LOCAL and fmt not in ("wav", "pcm"):
                return self.send(400, error("invalid_value", "pcm or wav only"))
            if model in ("calliope/chatterbox", "calliope/broken"):
                job_id = "job-ok-0123456789" if model == "calliope/chatterbox" else "job-bad-0123456789"
                return self.send(202, {"id": job_id, "status": "queued", "queued_ahead": 1,
                                       "estimated_seconds": 3, "audio_url": f"/jobs/{job_id}/audio"},
                                 headers=[("Location", f"/jobs/{job_id}"), ("Retry-After", "3")])
            return self.send(200, f"AUDIO {model} {voice} {fmt}".encode(), "audio/wav")

    return Handler


@pytest.fixture
def proxy(monkeypatch):
    state = State()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    monkeypatch.setenv("CALLIOPE_PORT", str(server.server_address[1]))
    yield state
    server.shutdown()
    server.server_close()
    assert not any("Origin" in r["headers"] for r in state.requests), "the CLI sent an Origin header"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------- reaching the proxy --

def test_no_proxy_and_no_launch_fails_at_once_with_a_clear_message(monkeypatch, capsys):
    monkeypatch.setenv("CALLIOPE_PORT", str(free_port()))
    started = time.monotonic()

    assert cli.main(["voices"]) == 1
    assert time.monotonic() - started < 5
    err = capsys.readouterr().err
    assert err.startswith("calliope: Calliope is not answering on 127.0.0.1:")
    assert err.count("\n") == 1


def test_no_proxy_launches_the_app_once_then_gives_up(monkeypatch, capsys):
    monkeypatch.setenv("CALLIOPE_PORT", str(free_port()))
    monkeypatch.delenv("CALLIOPE_NO_LAUNCH")
    monkeypatch.setattr(cli, "LAUNCH_WAIT_SECONDS", 0.5)
    launches = []
    monkeypatch.setattr(cli, "launch_app", lambda: launches.append(1))

    assert cli.main(["status"]) == 1
    assert launches == [1]
    assert "not answering" in capsys.readouterr().err


def test_a_launched_app_is_waited_for(proxy, monkeypatch, capsys):
    proxy.healthy = False
    monkeypatch.delenv("CALLIOPE_NO_LAUNCH")
    monkeypatch.setattr(cli, "launch_app", lambda: setattr(proxy, "healthy", True))

    assert cli.main(["voices", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == DETAIL


# ------------------------------------------------------------------------------------ save --

def test_save_with_a_mac_voice(proxy, tmp_path, capsys):
    out = tmp_path / "hello.wav"

    assert cli.main(["save", "Hello", "there", "-o", str(out), "--voice", "mac/af_heart"]) == 0
    assert proxy.speeches() == [{"model": "kokoro", "input": "Hello there", "voice": "af_heart",
                                 "response_format": "wav"}]
    assert out.read_bytes() == b"AUDIO kokoro af_heart wav"
    assert not (tmp_path / "hello.wav.part").exists()
    assert capsys.readouterr().out.startswith(f"Saved {out}")


def test_save_with_a_server_voice_in_another_format(proxy, tmp_path):
    out = tmp_path / "hello.mp3"

    assert cli.main(["save", "Olá", "-o", str(out), "--voice", "calliope/pf_dora"]) == 0
    assert proxy.speeches()[0]["model"] == "calliope/kokoro"
    assert out.read_bytes() == b"AUDIO calliope/kokoro pf_dora mp3"


@pytest.mark.parametrize("name, model", [("bf_emma", "kokoro"), ("af_heart", "kokoro"),
                                         ("only_remote", "calliope/kokoro"),
                                         ("nobody", "calliope/kokoro")])
def test_a_bare_name_prefers_this_mac(proxy, tmp_path, name, model):
    assert cli.main(["save", "Hi", "-o", str(tmp_path / "x.wav"), "--voice", name]) == 0
    assert proxy.speeches()[0]["model"] == model
    assert proxy.speeches()[0]["voice"] == name


def test_a_mac_voice_cannot_make_an_mp3(proxy, tmp_path, capsys):
    assert cli.main(["save", "Hi", "-o", str(tmp_path / "x.mp3"), "--voice", "mac/af_heart"]) == 1
    assert "only .wav or .pcm" in capsys.readouterr().err
    assert proxy.speeches() == []


@pytest.mark.parametrize("settings, extra, voice, model", [
    ({}, [], "af_heart", "kokoro"),
    ({}, ["--language", "pt"], "pf_dora", "kokoro"),
    ({"macOn": False}, [], "af_heart", "calliope/kokoro"),
    ({"voices": {"en": "calliope/am_adam"}}, [], "am_adam", "calliope/kokoro"),
    ({}, ["--model", "tts-1"], "af_heart", "tts-1"),
])
def test_save_without_a_voice_uses_the_players_choice(proxy, tmp_path, monkeypatch,
                                                      settings, extra, voice, model):
    use_prefs(monkeypatch, settings)

    assert cli.main(["save", "Hi", "-o", str(tmp_path / "x.wav"), *extra]) == 0
    assert (proxy.speeches()[0]["voice"], proxy.speeches()[0]["model"]) == (voice, model)


def test_an_unknown_extension_is_refused(proxy, tmp_path, capsys):
    assert cli.main(["save", "Hi", "-o", str(tmp_path / "x.txt")]) == 1
    assert "cannot tell the format" in capsys.readouterr().err
    assert proxy.requests == []


def test_a_long_form_job_is_polled_and_collected(proxy, tmp_path, capsys):
    out = tmp_path / "long.mp3"

    assert cli.main(["save", "A long story.", "-o", str(out), "--model", "calliope/chatterbox"]) == 0
    assert "voice" not in proxy.speeches()[0], "a Kokoro voice was sent to a long-form model"
    assert out.read_bytes() == b"LONG-AUDIO"
    polls = [r["path"] for r in proxy.requests if r["path"].startswith("/jobs/")]
    assert polls == ["/jobs/job-ok-0123456789"] * 3 + ["/jobs/job-ok-0123456789/audio"]
    err = capsys.readouterr().err
    assert "job job-ok-0: queued, 1 ahead" in err and "running" in err


def test_a_failed_job_says_why(proxy, tmp_path, capsys):
    out = tmp_path / "long.mp3"

    assert cli.main(["save", "A long story.", "-o", str(out), "--model", "calliope/broken"]) == 1
    assert "the GPU ran out of memory" in capsys.readouterr().err
    assert not out.exists()


def test_a_proxy_error_is_one_line(proxy, tmp_path, capsys):
    assert cli.main(["save", "Hi", "-o", str(tmp_path / "x.wav"), "--voice", "calliope/missing"]) == 1
    assert capsys.readouterr().err == "calliope: no voice called missing (voice_not_found)\n"


def test_a_markdown_file_is_saved_as_prose(proxy, tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Title\n\nSome **bold** text.\n\n```\ncode\n```\n")

    assert cli.main(["save", "-f", str(note), "-o", str(tmp_path / "x.wav")]) == 0
    assert proxy.speeches()[0]["input"] == "Title.\n\nSome bold text."


def test_plain_keeps_the_markdown(proxy, tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Title")

    assert cli.main(["save", "-f", str(note), "--plain", "-o", str(tmp_path / "x.wav")]) == 0
    assert proxy.speeches()[0]["input"] == "# Title"


# ------------------------------------------------------------------------------ transcribe --

def test_transcribe_sends_the_file_as_multipart(proxy, tmp_path, capsys):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF\x00\x01binary")

    assert cli.main(["transcribe", str(audio), "--language", "pt"]) == 0
    assert capsys.readouterr().out == "Hello there.\n"
    assert proxy.transcription["model"] == "whisper-1"
    assert proxy.transcription["language"] == "pt"
    assert proxy.transcription["file"] == ("clip.wav", b"RIFF\x00\x01binary")


def test_transcribe_strips_the_calliope_prefix_and_can_answer_json(proxy, tmp_path, capsys):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"x")

    assert cli.main(["transcribe", str(audio), "--model", "calliope/large-v3", "--json"]) == 0
    assert proxy.transcription["model"] == "large-v3"
    assert json.loads(capsys.readouterr().out) == {"text": " Hello there. "}


def test_transcribe_a_missing_file(proxy, tmp_path, capsys):
    assert cli.main(["transcribe", str(tmp_path / "nope.wav")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_no_vocabulary_sends_no_vocabulary_fields(proxy, tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"x")

    assert cli.main(["transcribe", str(audio)]) == 0
    assert [name for name, _ in proxy.transcription_parts] == ["model", "response_format", "file"]


def test_terms_and_vocabulary_files_are_sent_as_keywords(proxy, tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"x")
    words = tmp_path / "words.txt"
    words.write_text("﻿# names I say\n\nCalliope\n  Blue Heron  \nC#\nSão Paulo, SP\nkubectl\n")

    assert cli.main(["transcribe", str(audio), "--term", "Kubernetes, kubectl",
                     "--term", "Harmonia", "--vocabulary-file", str(words)]) == 0
    keywords = [value for name, value in proxy.transcription_parts if name == "keywords[]"]
    # Commas split a --term as the server splits `prompt`; a file line is one term, comma and all.
    assert keywords == ["Kubernetes", "kubectl", "Harmonia", "Calliope", "Blue Heron", "C#",
                        "São Paulo, SP"]
    assert "prompt" not in proxy.transcription


def test_glossaries_and_boost_are_sent_as_the_server_names_them(proxy, tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"x")

    assert cli.main(["transcribe", str(audio), "--glossary", "tech, dictation",
                     "--glossary", "mine", "--glossary", "tech", "--boost", "--term", "Fennell"]) == 0
    assert proxy.transcription["glossary"] == "tech,dictation,mine"
    assert proxy.transcription["boost"] == "true"
    assert proxy.transcription["keywords[]"] == "Fennell"


def test_a_replacement_line_is_refused_before_anything_is_sent(proxy, tmp_path, capsys):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"x")
    words = tmp_path / "personal.txt"
    words.write_text("Calliope\n# a rule\ncloud code = Claude Code\n")

    assert cli.main(["transcribe", str(audio), "--vocabulary-file", str(words)]) == 1
    err = capsys.readouterr().err
    assert "line 3 is a replacement (cloud code = Claude Code)" in err
    assert "--glossary NAME" in err
    assert proxy.requests == []


def test_a_missing_vocabulary_file(proxy, tmp_path, capsys):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"x")

    assert cli.main(["transcribe", str(audio), "--vocabulary-file", str(tmp_path / "no.txt")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("calliope: cannot read ") and "no.txt" in err
    assert proxy.requests == []


# --------------------------------------------------------------------------------- voices --

def test_voices_are_grouped_by_language(proxy, capsys):
    assert cli.main(["voices"]) == 0
    out = capsys.readouterr().out
    english, portuguese, other = out.split("\n\n")
    assert english.splitlines()[0] == "en  English  (default mac/af_heart)"
    assert re.search(r"^  mac +af_heart bf_emma$", english, re.M)
    assert re.search(r"^  calliope +af_heart only_remote$", english, re.M)
    assert portuguese.startswith("pt  Portuguese")
    assert other.startswith("other") and "narrator" in other


def test_voices_for_one_language(proxy, capsys):
    assert cli.main(["voices", "--language", "pt", "--json"]) == 0
    assert [row["ref"] for row in json.loads(capsys.readouterr().out)] == ["mac/pf_dora"]


# --------------------------------------------------------------------------------- status --

def test_status_is_short(proxy, capsys):
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("Calliope on 127.0.0.1:")
    assert out[1] == "  This Mac  on, model not loaded (loads on first use), unloads after 600 s idle"
    assert out[2] == "  Server    on, https://calliope.example"


def test_status_test_lists_the_checks(proxy, capsys):
    assert cli.main(["status", "--test"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"^  reach +ok +12 ms  up$", out, re.M)
    assert out.rstrip().endswith("Server test: Calliope server works")


def test_a_failing_server_test_exits_non_zero(proxy, capsys):
    proxy.test = {"configured": True, "ok": False, "message": "key not accepted",
                  "checks": [{"name": "reach", "ok": True, "ms": 5, "detail": "up"},
                             {"name": "key", "ok": False, "ms": 9, "detail": "key not accepted"}]}

    assert cli.main(["status", "--test", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["test"]["ok"] is False


# ---------------------------------------------------------------------------------- speak --
#
# Through the wrapper, by a symlink, out of a fake Calliope.app: the code under test is the
# path from `~/.local/bin/calliope` to the player, which is where a rename or a moved file breaks.

STUB_PLAYER = """#!/bin/sh
# Stands in for calliope-player: writes down how it was called and what it was given.
runtime="$HOME/.local/share/calliope"
{ printf 'ARGS %s\\n' "$*"; cat "$1"; printf '\\nEND\\n'; } >> "$runtime/calls"
echo $$ > "$runtime/player.pid"
sleep "${STUB_SLEEP:-0}"
"""


def interpreter_shim(label):
    return f'#!/bin/sh\necho {label} >> "$HOME/interpreter"\nexec "{sys.executable}" "$@"\n'


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def app(tmp_path):
    """A Calliope.app with the real command in it and a stub where the player goes."""
    root = tmp_path / "Applications/Calliope.app"
    cli_dir = root / "Contents/Resources/cli"
    cli_dir.mkdir(parents=True)
    for name in ("calliope", "calliope.py"):
        shutil.copy2(CLI_DIR / name, cli_dir / name)
    executable(root / "Contents/Helpers/CalliopePlayer.app/Contents/MacOS/calliope-player",
               STUB_PLAYER)
    executable(root / "Contents/Resources/python/bin/python3", interpreter_shim("bundled"))
    home = tmp_path / "home"
    (home / ".local/share/calliope").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "calliope").symlink_to(cli_dir / "calliope")

    class App:
        pass

    a = App()
    a.root, a.home, a.bin, a.runtime = root, home, bin_dir, home / ".local/share/calliope"
    a.env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "CALLIOPE_NO_LAUNCH": "1",
             "CALLIOPE_PORT": str(free_port())}

    def run(*argv, input=None, **env):
        return subprocess.run([str(bin_dir / "calliope"), *argv], input=input, text=True,
                              capture_output=True, env={**a.env, **env}, timeout=30)

    def calls(count=1):
        log = a.runtime / "calls"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if log.exists() and log.read_text().count("\nEND\n") >= count:
                break
            time.sleep(0.05)
        return re.findall(r"ARGS ([^\n]*)\n(.*?)\nEND\n", log.read_text(), re.S)

    a.run, a.calls = run, calls
    yield a
    try:
        os.killpg(int((a.runtime / "player.pid").read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


def test_speak_queues_the_text_and_starts_the_player(app):
    result = app.run("speak", "Hello", "there")

    assert result.returncode == 0, result.stderr
    [(args, text)] = app.calls()
    queued = args.split()[0]
    assert re.fullmatch(re.escape(str(app.runtime.resolve() / "queue")) + r"/[0-9a-f-]{36}\.txt", queued)
    assert text == "Hello there"
    assert (app.home / "interpreter").read_text() == "bundled\n"


def test_speak_passes_the_reader_and_the_voice_and_converts_markdown(app, tmp_path):
    note = tmp_path / "note.md"
    note.write_text("## Plan\n\n- [link](https://example.com) one\n")

    result = app.run("speak", "-f", str(note), "--reader", "--voice", "mac/bf_emma", "--wait")

    assert result.returncode == 0, result.stderr
    [(args, text)] = app.calls()
    assert args.split()[1:] == ["--reader", "--voice", "mac/bf_emma"]
    assert text == "Plan.\n\nlink one."


def test_speak_reads_standard_input_as_it_is(app):
    assert app.run("speak", "-", input="# not a heading\n\nkept\n").returncode == 0
    assert app.calls()[0][1] == "# not a heading\n\nkept"


def test_a_new_speak_stops_the_player_that_is_speaking(app):
    assert app.run("speak", "one", STUB_SLEEP="30").returncode == 0
    app.calls()
    first = int((app.runtime / "player.pid").read_text())

    assert app.run("speak", "two", STUB_SLEEP="30").returncode == 0
    app.calls(2)
    assert wait_for_exit(first), "the first player is still running"


def test_a_stale_pid_file_never_stops_another_program(app):
    bystander = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        (app.runtime / "player.pid").write_text(str(bystander.pid))
        assert app.run("speak", "hello").returncode == 0
        app.calls()
        time.sleep(0.2)
        assert bystander.poll() is None, "speak killed a process that was not the player"
    finally:
        bystander.kill()
        bystander.wait()


def test_empty_text_stops_the_player_and_says_so(app):
    assert app.run("speak", "one", STUB_SLEEP="30").returncode == 0
    app.calls()
    first = int((app.runtime / "player.pid").read_text())

    result = app.run("speak", "-", input="  \n")

    assert result.returncode == 1
    assert result.stderr == "calliope: No text to speak\n"
    assert wait_for_exit(first)


def test_speak_without_a_player_says_where_it_looked(app):
    shutil.rmtree(app.root / "Contents/Helpers")

    result = app.run("speak", "hello")

    assert result.returncode == 1
    assert str(app.root.resolve()) in result.stderr
    assert not (app.runtime / "queue").exists()


@pytest.mark.parametrize("have, used", [({"bundled", "venv", "path"}, "bundled"),
                                        ({"venv", "path"}, "venv"),
                                        ({"path"}, "path")])
def test_the_wrapper_finds_python_as_paths_swift_does(app, tmp_path, have, used):
    if "bundled" not in have:
        shutil.rmtree(app.root / "Contents/Resources/python")
    if "venv" in have:
        executable(app.runtime / ".venv/bin/python", interpreter_shim("venv"))
    shims = tmp_path / "shims"
    executable(shims / "python3", interpreter_shim("path"))

    result = app.run("speak", "hi", PATH=f"{app.bin}:{shims}:/usr/bin:/bin")

    assert result.returncode == 0, result.stderr
    assert (app.home / "interpreter").read_text() == used + "\n"


def wait_for_exit(pid, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------- the OpenClip action --

def openclip_action(tmp_path, app_root):
    """openclip/calliope.py as install.sh installs it: __APP__ replaced."""
    action = tmp_path / "action.py"
    action.write_text((ROOT / "openclip/calliope.py").read_text().replace("__APP__", str(app_root)))
    return action


def test_the_openclip_action_hands_the_selection_to_the_command(app, tmp_path):
    action = openclip_action(tmp_path, app.root)

    result = subprocess.run([sys.executable, str(action)], capture_output=True, text=True,
                            env={**app.env, "OPENCLIP_TEXT": "Selected *text*"}, timeout=30)

    assert json.loads(result.stdout) == {"type": "success"}
    assert app.calls()[0][1] == "Selected *text*"


def test_the_openclip_action_reports_an_empty_selection(app, tmp_path):
    action = openclip_action(tmp_path, app.root)

    result = subprocess.run([sys.executable, str(action)], input="", capture_output=True,
                            text=True, env=app.env, timeout=30)

    assert json.loads(result.stdout) == {"type": "toast", "message": "No text to speak",
                                         "style": "error"}


def test_the_openclip_action_without_the_app(tmp_path):
    action = openclip_action(tmp_path, tmp_path / "Nowhere/Calliope.app")

    result = subprocess.run([sys.executable, str(action)], capture_output=True, text=True,
                            env={"HOME": str(tmp_path), "OPENCLIP_TEXT": "hi"}, timeout=30)

    reply = json.loads(result.stdout)
    assert reply["style"] == "error" and "not installed" in reply["message"]


# --------------------------------------------------------------- install.sh and release.sh --

def block(script, first_line, last_line):
    text = (ROOT / script).read_text()
    start = text.index(first_line)
    return text[start:text.index(last_line, start) + len(last_line)]


@pytest.mark.parametrize("script", ["install.sh", "release.sh"])
def test_the_command_and_the_skill_are_bundled(script, tmp_path):
    contents = tmp_path / "Calliope.app/Contents"
    (contents / "Resources").mkdir(parents=True)
    shell = f"""
set -euo pipefail
here="{ROOT}"
contents="{contents}"
{block(script, 'echo "==> Command line"', 'cp -R "$here/skill" "$contents/Resources/skill"')}
"""
    result = subprocess.run(["bash", "-c", shell], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    for name in ("calliope", "calliope.py"):
        assert (contents / "Resources/cli" / name).stat().st_mode & 0o777 == 0o755
    assert (contents / "Resources/skill/calliope-voice/SKILL.md").exists()


def link_block(tmp_path, path, existing=None):
    home = tmp_path / "home"
    home.mkdir()
    if existing is not None:
        executable(home / ".local/bin/calliope", existing)
    lines = block("install.sh", 'echo "==> The calliope command"', "\nesac")
    shell = f"""
set -euo pipefail
HOME="{home}"
PATH="{path.replace('~', str(home))}"
app="{tmp_path}/Applications/Calliope.app"
{lines}
"""
    return home, subprocess.run(["bash", "-c", shell], capture_output=True, text=True)


def test_install_links_the_command_into_local_bin(tmp_path):
    home, result = link_block(tmp_path, "/usr/bin:/bin")

    assert result.returncode == 0, result.stderr
    link = home / ".local/bin/calliope"
    assert os.readlink(link) == f"{tmp_path}/Applications/Calliope.app/Contents/Resources/cli/calliope"
    assert "not on your PATH" in result.stdout


def test_install_says_nothing_about_a_path_that_has_local_bin(tmp_path):
    _, result = link_block(tmp_path, "/usr/bin:~/.local/bin:/bin")

    assert result.returncode == 0, result.stderr
    assert "PATH" not in result.stdout


def test_install_leaves_somebody_elses_calliope_alone(tmp_path):
    home, result = link_block(tmp_path, "/usr/bin:/bin", existing="#!/bin/sh\necho mine\n")

    assert result.returncode == 0, result.stderr
    assert not (home / ".local/bin/calliope").is_symlink()
    assert "left alone" in result.stdout


def test_nothing_installs_the_skill_for_anybody():
    """THE SKILL IS A FILE. The user: "just provide the skill, and the person uses the
    SKILL.md as they want." Neither the installer nor the app writes it into an agent's
    configuration -- Settings only shows where it is."""
    assert ".claude" not in (ROOT / "install.sh").read_text()
    assert ".claude" not in (ROOT / "daemon/settings.swift").read_text()


def test_the_skill_is_generic():
    skill = (ROOT / "skill/calliope-voice/SKILL.md").read_text()
    front = re.match(r"---\nname: calliope-voice\ndescription: (.+?)\n---\n", skill, re.S)

    assert front, "the skill has no name and description"
    assert "out loud" in front.group(1) and "read it to me" in front.group(1)
    assert len(skill.splitlines()) <= 65
    for private in ("/Users/", "obsidian", "Obsidian", "gabrielbelli", "192.168."):
        assert private not in skill


# ------------------------------------------------------------- transcribe: formats and input --

def audio_file(tmp_path, data=b"RIFF\x00\x01binary"):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(data)
    return audio


@pytest.mark.parametrize("fmt, out", [("srt", SRT), ("text", "Hello there.\n"),
                                      ("vtt", "Hello there.\n")])
def test_a_text_format_is_printed_as_the_server_wrote_it(proxy, tmp_path, capsys, fmt, out):
    assert cli.main(["transcribe", str(audio_file(tmp_path)), "--format", fmt]) == 0
    assert proxy.transcription["response_format"] == fmt
    assert capsys.readouterr().out == out


def test_verbose_json_is_printed_whole_with_the_timestamps_asked_for(proxy, tmp_path, capsys):
    assert cli.main(["transcribe", str(audio_file(tmp_path)), "--format", "verbose_json",
                     "--timestamps", "word", "--timestamps", "segment"]) == 0
    grains = [value for name, value in proxy.transcription_parts
              if name == "timestamp_granularities[]"]
    assert grains == ["word", "segment"]
    assert json.loads(capsys.readouterr().out)["segments"][0]["end"] == 1.5


@pytest.mark.parametrize("argv, said", [
    (["--timestamps", "word"], "--timestamps needs --format verbose_json"),
    (["--format", "srt", "--json"], "--format srt is not JSON"),
    (["--captions"], "--captions takes a link"),
])
def test_impossible_combinations_are_refused_before_anything_is_sent(proxy, tmp_path, capsys,
                                                                      argv, said):
    assert cli.main(["transcribe", str(audio_file(tmp_path)), *argv]) == 1
    assert said in capsys.readouterr().err
    assert proxy.requests == []


@pytest.mark.parametrize("clip, start, end", [("1:30-2:45", "90.0", "165.0"),
                                              ("90-", "90.0", None), ("-0:01:05.5", None, "65.5")])
def test_a_clip_is_sent_as_the_window_stt_decodes(proxy, tmp_path, clip, start, end):
    # --clip=, because argparse reads a value starting with "-" as another option.
    assert cli.main(["transcribe", str(audio_file(tmp_path)), "--clip=" + clip]) == 0
    assert proxy.transcription.get("clip_start") == start
    assert proxy.transcription.get("clip_end") == end


@pytest.mark.parametrize("clip", ["abc", "2:00-1:00", "-", "1:2:3:4-5", "90"])
def test_a_clip_that_says_nothing_sensible_is_refused(proxy, tmp_path, capsys, clip):
    assert cli.main(["transcribe", str(audio_file(tmp_path)), "--clip=" + clip]) == 1
    assert "--clip" in capsys.readouterr().err
    assert proxy.requests == []


def test_transcribe_reads_standard_input(proxy, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"OggS\x00audio")))

    assert cli.main(["transcribe", "-"]) == 0
    assert proxy.transcription["file"] == ("stdin", b"OggS\x00audio")
    assert capsys.readouterr().out == "Hello there.\n"


def test_a_large_file_is_sent_whole(proxy, tmp_path):
    data = os.urandom(3 * 1024 * 1024)
    assert cli.main(["transcribe", str(audio_file(tmp_path, data))]) == 0
    assert proxy.transcription["file"] == ("clip.wav", data)


def test_what_the_glossary_rewrote_is_said(proxy, tmp_path, capsys):
    proxy.repaired = "Calliope, S%C3%A3o Paulo"

    assert cli.main(["transcribe", str(audio_file(tmp_path)), "--glossary", "mine"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Hello there.\n"
    assert captured.err == "Glossary repaired: Calliope, São Paulo\n"

    assert cli.main(["transcribe", str(audio_file(tmp_path)), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["repaired"] == ["Calliope", "São Paulo"]


# ---------------------------------------------------------------------- transcribe: links --

def test_a_link_is_downloaded_and_transcribed_on_the_server(proxy, capsys):
    assert cli.main(["transcribe", LINK]) == 0

    assert capsys.readouterr().out == "From the link.\n"
    assert [(r["method"], r["path"].split("?")[0]) for r in proxy.requests
            if r["path"] != "/health"] == [
        ("POST", "/ui/resolve"), ("POST", "/ui/commit"), ("GET", "/ui/progress"),
        ("GET", "/ui/progress"), ("GET", "/ui/progress"), ("POST", "/ui/fetch"),
        ("POST", "/ui/abandon")]
    assert proxy.bodies("POST", "/ui/resolve") == [{"url": LINK}]
    assert proxy.bodies("POST", "/ui/commit") == [{"token": LINK}]
    assert proxy.bodies("POST", "/ui/fetch") == [{"token": LINK}]
    assert proxy.bodies("POST", "/ui/abandon") == [{"token": LINK}]
    assert proxy.to("GET", "/ui/progress")[0]["path"] == (
        "/ui/progress?token=" + urllib.parse.quote(LINK, safe=""))


def test_a_link_says_what_it_is_and_how_the_download_goes(proxy, capsys):
    assert cli.main(["transcribe", LINK]) == 0
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "A talk, recorded (3:12, about 3.1 MB of audio)"
    assert "waiting to download A talk, recorded" in err
    assert "downloading A talk, recorded, 40%, 1.9 MB/s, about 2 s left" in err


def test_a_link_carries_its_format_window_timestamps_and_glossary(proxy, capsys):
    assert cli.main(["transcribe", LINK, "--format", "srt", "--clip", "10-20",
                     "--glossary", "tech, mine"]) == 0
    assert proxy.bodies("POST", "/ui/commit") == [{"token": LINK, "clip_start": 10.0,
                                                   "clip_end": 20.0}]
    query = urllib.parse.parse_qs(proxy.to("POST", "/ui/fetch")[0]["path"].partition("?")[2])
    assert query == {"response_format": ["srt"], "glossary": ["tech,mine"]}
    assert capsys.readouterr().out == SRT

    assert cli.main(["transcribe", LINK, "--format", "verbose_json", "--timestamps", "word"]) == 0
    query = urllib.parse.parse_qs(proxy.to("POST", "/ui/fetch")[1]["path"].partition("?")[2])
    assert query == {"response_format": ["verbose_json"], "timestamp_granularities": ["word"]}


@pytest.mark.parametrize("argv", [["--term", "Calliope"], ["--language", "pt"], ["--boost"],
                                  ["--model", "whisper-1"]])
def test_a_link_refuses_what_its_route_cannot_carry(proxy, capsys, argv):
    assert cli.main(["transcribe", LINK, *argv]) == 1
    err = capsys.readouterr().err
    assert argv[0] in err and "calliope fetch URL -o FILE" in err
    assert proxy.requests == []


def test_a_link_without_a_server_says_links_need_one(proxy, capsys):
    proxy.server = False

    assert cli.main(["transcribe", LINK]) == 1
    assert capsys.readouterr().err == (
        "calliope: a link is downloaded by the Calliope server, and none is configured: "
        "Calliope's Settings need its address and a key (no_calliope_server)\n")


def test_a_failed_download_says_why_and_lets_the_link_go(proxy, capsys):
    proxy.download = [{"where": "done", "status": "error", "ready": False,
                       "error": "The download took longer than 30 minutes and was stopped."}]

    assert cli.main(["transcribe", LINK]) == 1
    assert "took longer than 30 minutes" in capsys.readouterr().err
    assert proxy.to("POST", "/ui/fetch") == []
    assert proxy.bodies("POST", "/ui/abandon") == [{"token": LINK}]


def test_an_unreadable_link_is_the_servers_refusal(proxy, capsys):
    assert cli.main(["transcribe", "https://video.example/nope"]) == 1
    assert capsys.readouterr().err == "calliope: Could not read that link (unresolvable)\n"


def test_a_link_interrupted_is_let_go(proxy, monkeypatch, capsys):
    def interrupted(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", interrupted)

    assert cli.main(["transcribe", LINK]) == 1
    assert "let the download go" in capsys.readouterr().err
    assert proxy.bodies("POST", "/ui/abandon") == [{"token": LINK}]


@pytest.mark.parametrize("fmt, out", [
    (None, "Hello there, from the & captions.\n"),
    ("vtt", VTT),
    ("srt", "1\n00:00:00,000 --> 00:00:02,500\nHello there,\n\n"
            "2\n00:00:02,500 --> 00:00:04,000\nfrom the & captions.\n"),
])
def test_captions_are_read_and_never_transcribed(proxy, capsys, fmt, out):
    argv = ["transcribe", LINK_WITH_CAPTIONS, "--captions"] + (["--format", fmt] if fmt else [])

    assert cli.main(argv) == 0
    assert capsys.readouterr().out == out
    assert proxy.bodies("POST", "/ui/commit") == [{"token": LINK_WITH_CAPTIONS, "captions": True}]
    assert proxy.to("POST", "/ui/fetch") == []
    assert len(proxy.to("POST", "/ui/captions")) == 1


def test_captions_as_json(proxy, capsys):
    assert cli.main(["transcribe", LINK_WITH_CAPTIONS, "--captions", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "text": "Hello there, from the & captions.", "source": "captions", "format": "vtt"}


def test_captions_where_there_are_none_are_refused_and_nothing_downloads(proxy, capsys):
    assert cli.main(["transcribe", LINK, "--captions"]) == 1
    assert "has no captions of its own" in capsys.readouterr().err
    assert proxy.to("POST", "/ui/commit") == []
    assert proxy.bodies("POST", "/ui/abandon") == [{"token": LINK}]


def test_subtitles_convert_both_ways():
    cues = cli.parse_cues(VTT)
    assert cues == [(0.0, 2.5, "Hello there,"), (2.5, 4.0, "from the & captions.")]
    assert cli.parse_cues(cli.render_cues(cues, "srt")) == cues
    assert cli.parse_cues(cli.render_cues(cues, "vtt")) == cues


# ---------------------------------------------------------------------------------- fetch --

def test_fetch_saves_a_links_audio_under_its_own_name(proxy, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["fetch", LINK]) == 0
    assert (tmp_path / "A talk, recorded.weba").read_bytes() == MEDIA
    assert capsys.readouterr().out == "Saved A talk, recorded.weba (256 KB, A talk, recorded)\n"
    assert proxy.bodies("POST", "/ui/commit") == [{"token": LINK}]
    assert proxy.bodies("POST", "/ui/abandon") == [{"token": LINK}]
    assert not list(tmp_path.glob("*.part"))


def test_fetch_into_a_directory_or_a_name(proxy, tmp_path, capsys):
    assert cli.main(["fetch", LINK, "-o", str(tmp_path)]) == 0
    assert (tmp_path / "A talk, recorded.weba").read_bytes() == MEDIA

    assert cli.main(["fetch", LINK, "-o", str(tmp_path / "talk.mp3")]) == 0
    assert (tmp_path / "talk.mp3").read_bytes() == MEDIA
    assert "the file is .weba" in capsys.readouterr().err


def test_fetch_to_standard_output(proxy, capsysbinary):
    assert cli.main(["fetch", LINK, "-o", "-"]) == 0
    captured = capsysbinary.readouterr()
    assert captured.out == MEDIA
    assert captured.err.endswith(b"Saved - (256 KB, A talk, recorded)\n")


def test_fetch_the_video_or_the_captions(proxy, tmp_path):
    assert cli.main(["fetch", LINK, "--video", "-o", str(tmp_path / "talk.weba")]) == 0
    assert proxy.bodies("POST", "/ui/commit")[-1] == {"token": LINK, "video": True}

    assert cli.main(["fetch", LINK_WITH_CAPTIONS, "--captions", "-o", str(tmp_path)]) == 0
    assert (tmp_path / "A talk, recorded.vtt").read_text() == VTT


def test_fetch_takes_only_a_link(proxy, capsys):
    assert cli.main(["fetch", "notes.wav"]) == 1
    assert "fetch takes a link" in capsys.readouterr().err


# ------------------------------------------------------------------------------ translate --

def test_translate_sends_only_the_fields_its_route_takes(proxy, tmp_path, capsys):
    words = tmp_path / "words.txt"
    words.write_text("Calliope\nHarmonia\n")

    assert cli.main(["translate", str(audio_file(tmp_path)), "--term", "Fennell",
                     "--vocabulary-file", str(words), "--glossary", "mine",
                     "--model", "calliope/whisper-1"]) == 0
    assert capsys.readouterr().out == "Hello, in English.\n"
    assert [name for name, _ in proxy.translation_parts] == [
        "model", "response_format", "prompt", "glossary", "file"]
    assert proxy.translation["prompt"] == "Fennell, Calliope, Harmonia"
    assert proxy.translation["model"] == "whisper-1"


def test_translate_refuses_a_term_its_prompt_would_split(proxy, tmp_path, capsys):
    assert cli.main(["translate", str(audio_file(tmp_path)), "--term", "x"]) == 0
    words = tmp_path / "words.txt"
    words.write_text("São Paulo, SP\n")
    proxy.requests.clear()

    assert cli.main(["translate", str(audio_file(tmp_path)), "--vocabulary-file", str(words)]) == 1
    assert "'São Paulo, SP' would arrive as two terms" in capsys.readouterr().err
    assert proxy.requests == []


@pytest.mark.parametrize("flag", [["--language", "pt"], ["--boost"], ["--clip", "1-2"]])
def test_translate_has_no_flags_its_route_would_refuse(flag):
    with pytest.raises(SystemExit):
        parse("translate", "a.wav", *flag)


def test_translate_a_link_through_the_servers_download(proxy, capsys):
    assert cli.main(["translate", LINK, "--format", "text"]) == 0
    assert capsys.readouterr().out == "Hello, in English.\n"
    assert proxy.translation["file"] == ("A talk, recorded.weba", MEDIA)
    assert proxy.bodies("POST", "/ui/abandon") == [{"token": LINK}]


# ----------------------------------------------------------------------------- glossaries --

def test_glossaries_are_listed_with_whose_each_is(proxy, capsys):
    assert cli.main(["glossaries"]) == 0
    assert capsys.readouterr().out == (
        "tech  built-in    84 terms, 4 of them replacements\n"
        "mine  yours        2 terms, 1 of them replacements\n"
        "Used when a request names none: none\n")


def test_a_glossary_is_shown_as_its_file(proxy, capsys):
    assert cli.main(["glossaries", "show", "mine"]) == 0
    assert capsys.readouterr().out == MINE

    assert cli.main(["glossaries", "show", "nope"]) == 1
    assert capsys.readouterr().err == "calliope: no glossary profile named 'nope'\n"


def test_a_glossary_is_put_as_text(proxy, tmp_path, capsys):
    rules = tmp_path / "rules.txt"
    rules.write_text("Calliope\nfennel = Fennell\n")

    assert cli.main(["glossaries", "put", "names", "-f", str(rules), "--force"]) == 0
    [put] = proxy.to("PUT", "/glossaries/names")
    assert put["path"] == "/glossaries/names?force=true"
    assert put["headers"]["Content-Type"] == "text/plain; charset=utf-8"
    assert put["body"] == b"Calliope\nfennel = Fennell\n"
    assert capsys.readouterr().out == "Created glossary names: 2 terms, 1 of them replacements.\n"


def test_a_glossary_from_standard_input_replaces_one(proxy, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("Calliope\n"))

    assert cli.main(["glossaries", "put", "mine", "-f", "-"]) == 0
    assert proxy.to("PUT", "/glossaries/mine")[0]["body"] == b"Calliope\n"
    assert capsys.readouterr().out == "Replaced glossary mine: 1 terms.\n"


def test_a_refused_glossary_lists_each_line(proxy, tmp_path, capsys):
    rules = tmp_path / "rules.txt"
    rules.write_text("Calliope\na = = b\n")

    assert cli.main(["glossaries", "put", "mine", "-f", str(rules)]) == 1
    assert capsys.readouterr().err == (
        "calliope: 1 line(s) rejected; nothing was written. 1 term(s) would have been "
        "accepted.\n  line 2: a = = b -- two '='\n")


def test_a_glossary_is_deleted(proxy, capsys):
    assert cli.main(["glossaries", "rm", "mine"]) == 0
    assert capsys.readouterr().out == "Deleted glossary mine.\n"
    assert len(proxy.to("DELETE", "/glossaries/mine")) == 1


@pytest.mark.parametrize("argv, said", [
    (["glossaries", "show", "../x"], "not a glossary name"),
    (["glossaries", "rm", ".hidden"], "not a glossary name"),
    (["glossaries", "put", "mine"], "-f FILE"),
    (["glossaries", "show"], "takes one name"),
    (["glossaries", "list", "mine"], "takes no name"),
])
def test_glossary_mistakes_are_caught_here(proxy, capsys, argv, said):
    assert cli.main(argv) == 1
    assert said in capsys.readouterr().err
    assert proxy.requests == []


# ----------------------------------------------------------------------------------- jobs --

def test_jobs_are_listed_one_line_each(proxy, capsys):
    assert cli.main(["jobs"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert re.fullmatch(r"0b8f7e1c  done +clone +chatterbox +2 h ago +mp3 1 KB +Once upon a time",
                        lines[0])
    assert re.fullmatch(r"7c4a9e2d  running +clone +chatterbox +just now +A long story", lines[1])
    assert re.fullmatch(r"0b8f0000  done +transcribe +parakeet +1 d ago +no audio kept +a meeting",
                        lines[2])
    assert proxy.to("GET", "/jobs")[0]["path"] == "/jobs?limit=20"


def test_jobs_list_filters_on_the_server(proxy):
    assert cli.main(["jobs", "list", "--kind", "clone", "--status", "live", "--limit", "5"]) == 0
    assert proxy.to("GET", "/jobs")[0]["path"] == "/jobs?limit=5&kind=clone&status=live"


def test_a_job_is_found_by_the_start_of_its_id(proxy, capsys):
    assert cli.main(["jobs", "show", "7c4a"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Job 7c4a9e2d-0000-4e5b-8c9d-0123456789ab\n  status  running, just now\n")

    assert cli.main(["jobs", "show", "0b8f"]) == 1
    assert "0b8f is the start of 2 jobs" in capsys.readouterr().err


def test_a_jobs_audio_is_fetched_whole(proxy, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["jobs", "fetch", "0b8f7e1c"]) == 0
    saved = tmp_path / "0b8f7e1c-1d2a-4e5b-8c9d-0123456789ab.mp3"
    assert saved.read_bytes() == b"LONG-AUDIO"
    assert capsys.readouterr().out.startswith("Saved 0b8f7e1c-1d2a-4e5b-8c9d-0123456789ab.mp3")


def test_a_short_answer_is_not_saved_as_the_audio(proxy, tmp_path, capsys):
    proxy.records["0b8f7e1c-1d2a-4e5b-8c9d-0123456789ab"]["audio"]["bytes"] = 2048

    assert cli.main(["jobs", "fetch", "0b8f7e1c", "-o", str(tmp_path / "x.mp3")]) == 1
    assert "broke off" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_a_job_without_audio_says_why(proxy, capsys):
    assert cli.main(["jobs", "fetch", "0b8f0000"]) == 1
    assert "it is a transcribe run, which keeps none" in capsys.readouterr().err


def test_fetch_waits_for_a_live_job(proxy, tmp_path, capsys):
    assert cli.main(["jobs", "fetch", "job-ok-0123456789", "-o", str(tmp_path / "a.mp3")]) == 1
    # The stand-in's three-step job has no `audio` object, which is what a real one carries.
    assert "has no audio to fetch" in capsys.readouterr().err
    assert len(proxy.to("GET", "/jobs/job-ok-0123456789")) == 3


def test_a_live_job_is_cancelled_and_a_finished_one_is_not(proxy, capsys):
    assert cli.main(["jobs", "cancel", "7c4a"]) == 0
    assert capsys.readouterr().out.startswith("Cancelling job 7c4a9e2d")
    assert len(proxy.to("DELETE", "/jobs/7c4a9e2d-0000-4e5b-8c9d-0123456789ab")) == 1

    assert cli.main(["jobs", "cancel", "0b8f7e1c"]) == 1
    assert "`calliope jobs rm 0b8f7e1c` deletes it" in capsys.readouterr().err
    assert proxy.to("DELETE", "/jobs/0b8f7e1c-1d2a-4e5b-8c9d-0123456789ab") == []


def test_a_finished_job_is_removed_or_only_its_audio(proxy, capsys):
    assert cli.main(["jobs", "rm", "0b8f7e1c", "--audio"]) == 0
    assert capsys.readouterr().out == "Deleted the audio of job 0b8f7e1c; its record stays.\n"
    assert len(proxy.to("DELETE", "/jobs/0b8f7e1c-1d2a-4e5b-8c9d-0123456789ab/audio")) == 1

    assert cli.main(["jobs", "rm", "0b8f7e1c"]) == 0
    assert capsys.readouterr().out == "Deleted job 0b8f7e1c.\n"

    assert cli.main(["jobs", "rm", "7c4a"]) == 1
    assert "`calliope jobs cancel 7c4a9e2d` stops it first" in capsys.readouterr().err


def test_save_says_the_job_id_before_waiting(proxy, tmp_path, capsys):
    assert cli.main(["save", "A long story.", "-o", str(tmp_path / "x.mp3"),
                     "--model", "calliope/chatterbox"]) == 0
    assert "job job-ok-0123456789 is making it on the Calliope server" in capsys.readouterr().err


def test_save_interrupted_says_how_to_collect_it(proxy, tmp_path, monkeypatch, capsys):
    def interrupted(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", interrupted)

    assert cli.main(["save", "A long story.", "-o", str(tmp_path / "x.mp3"),
                     "--model", "calliope/chatterbox"]) == 1
    assert "`calliope jobs fetch job-ok-0` collects it" in capsys.readouterr().err


# ----------------------------------------------------------------------- save: stdin, stdout --

def test_save_to_standard_output(proxy, capsysbinary):
    assert cli.main(["save", "Hi", "-o", "-", "--voice", "mac/af_heart"]) == 0
    captured = capsysbinary.readouterr()
    assert captured.out == b"AUDIO kokoro af_heart wav"
    assert captured.err.startswith(b"Saved - ")


def test_save_reads_standard_input(proxy, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("From a pipe.\n"))

    assert cli.main(["save", "-", "-o", str(tmp_path / "x.wav")]) == 0
    assert proxy.speeches()[0]["input"] == "From a pipe."


def test_save_refuses_a_name_that_says_another_format(proxy, tmp_path, capsys):
    assert cli.main(["save", "Hi", "-o", str(tmp_path / "x.wav"), "--format", "mp3"]) == 1
    assert "would hold mp3" in capsys.readouterr().err
    assert proxy.requests == []


# ----------------------------------------------------------------------- models, clips, errors --

def test_models_say_what_each_is_for(proxy, capsys):
    assert cli.main(["models"]) == 0
    assert capsys.readouterr().out == (
        "This Mac        kokoro tts-1\n"
        "Calliope server\n"
        "  speech        calliope/kokoro\n"
        "  long-form     calliope/chatterbox\n"
        "  transcription calliope/parakeet calliope/whisper-1\n"
        "A long-form model answers with a job: `calliope save` waits for it.\n")


def test_voice_clips_are_listed_added_and_removed(proxy, tmp_path, capsys):
    assert cli.main(["clips"]) == 0
    assert re.fullmatch(r"narrator +12\.3 s +528 KB +1 h ago\n", capsys.readouterr().out)

    clip = tmp_path / "me.m4a"
    clip.write_bytes(b"\x00\x00\x00 ftypM4A")
    assert cli.main(["clips", "add", "Narrator", str(clip), "--replace"]) == 0
    assert proxy.clip == {"name": "Narrator", "replace": "true",
                          "file": ("me.m4a", b"\x00\x00\x00 ftypM4A")}
    assert capsys.readouterr().out == "Saved voice clip narrator (12.3 s).\n"

    assert cli.main(["clips", "rm", "Narrator"]) == 0
    assert len(proxy.to("DELETE", "/ui/clips/narrator")) == 1
    assert cli.main(["clips", "rm", "nobody"]) == 1
    assert capsys.readouterr().err.endswith("calliope: no voice called 'nobody' (unknown_voice)\n")


def test_a_validation_error_names_the_field():
    body = json.dumps({"detail": [{"loc": ["body", "token"], "msg": "Field required"}]}).encode()
    assert cli._refusal(422, body) == ("token: Field required", None)


def test_stop_stops_the_player(app):
    assert app.run("speak", "one", STUB_SLEEP="30").returncode == 0
    app.calls()
    first = int((app.runtime / "player.pid").read_text())

    result = app.run("stop")
    assert (result.returncode, result.stdout) == (0, "Stopped.\n")
    assert wait_for_exit(first)
    assert app.run("stop").stdout == "Nothing is playing.\n"
