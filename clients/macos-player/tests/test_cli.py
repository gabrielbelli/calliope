"""calliope, the command line: Markdown to speech, the arguments, and every command.

NOTHING HERE SPEAKS OR LAUNCHES ANYTHING. save, transcribe, voices and status run in-process
against a fake proxy on 127.0.0.1:0, with CALLIOPE_NO_LAUNCH=1 and `launch_app` replaced by a
failure. speak runs as the user runs it -- through the sh wrapper, by a symlink -- but out of a
fake Calliope.app whose player is a shell script that writes down what it was given, under a
temporary HOME so the real queue and pid file are never touched.
"""
import importlib.util
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
            match = re.fullmatch(r"/jobs/([\w-]+)(/audio)?", path)
            if method == "GET" and match:
                job_id = match.group(1)
                if match.group(2):
                    return self.send(200, b"LONG-AUDIO", "audio/mpeg")
                status = state.jobs[job_id].pop(0)
                job = {"id": job_id, "status": status, "format": "mp3", "estimated_seconds": 3}
                if status == "failed":
                    job["error"] = "the GPU ran out of memory"
                return self.send(200, job)
            if method == "POST" and path == "/v1/audio/transcriptions":
                message = BytesParser(policy=policy.default).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                fields = {}
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    fields[name] = ((part.get_filename(), part.get_payload(decode=True))
                                    if part.get_filename() else part.get_content())
                state.transcription = fields
                return self.send(200, {"text": " Hello there. "})
            return self.send(404, error("not_found", "no route " + path))

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


def test_install_never_installs_the_skill_for_anybody():
    """Opt-in only: the Settings window offers it. An installer that writes into ~/.claude
    changes how somebody's agent behaves without asking."""
    assert ".claude" not in (ROOT / "install.sh").read_text()


def test_the_skill_is_generic():
    skill = (ROOT / "skill/calliope-voice/SKILL.md").read_text()
    front = re.match(r"---\nname: calliope-voice\ndescription: (.+?)\n---\n", skill, re.S)

    assert front, "the skill has no name and description"
    assert "out loud" in front.group(1) and "read it to me" in front.group(1)
    assert len(skill.splitlines()) <= 65
    for private in ("/Users/", "obsidian", "Obsidian", "gabrielbelli", "192.168."):
        assert private not in skill
