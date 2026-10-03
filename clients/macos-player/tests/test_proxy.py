"""server.py as a proxy that never holds the model, and the engine it starts and stops.

NOTHING HERE LOADS KOKORO, and that is not only about speed. The engine the
proxy starts is a stand-in written below -- a few lines of Python speaking the
same pipe protocol and keeping a diary of what it was asked -- and the real
`--engine` mode runs against a stub kokoro_onnx that makes no sound and needs
no model. What is real is everything the proxy does: server.py itself, its
routes, its child process, its pipes and its idle timer.
"""
import importlib.util
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
import zipfile
from http.server import ThreadingHTTPServer

import pytest

SERVER_PY = pathlib.Path(__file__).resolve().parent.parent / "server" / "server.py"

# THE STAND-IN ENGINE. Two bytes of PCM per word times a hundred, one timing
# pair per word, and a line in its diary for every start, request and exit, so
# a test can see which process answered and whether it left through EOF.
FAKE_ENGINE = r'''
import json, os, sys

diary = open(sys.argv[1], "a", buffering=1)

def note(event, **fields):
    diary.write(json.dumps(dict(fields, event=event, pid=os.getpid())) + "\n")

note("start", environment=sorted(name for name in os.environ if name.startswith("CALLIOPE_")))
while True:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    request = json.loads(line)
    note("request", request=request)
    if request["input"] == "die":
        os._exit(3)
    if request["input"] == "refuse":
        header = {"ok": False, "status": 400, "code": "invalid_request", "message": "refused by the stand-in"}
        sys.stdout.buffer.write(json.dumps(header).encode() + b"\n")
    else:
        words = request["input"].split()
        pcm = b"\x01\x02" * (100 * len(words))
        timings = [[index * 0.5, index * 0.5 + 0.4] for index in range(len(words))]
        sys.stdout.buffer.write(json.dumps({"ok": True, "bytes": len(pcm), "timings": timings}).encode()
                                + b"\n" + pcm)
    sys.stdout.buffer.flush()
note("eof")
'''

# THE STUB KOKORO, for the one thing the stand-in cannot test: server.py's own
# --engine mode. It is noisy on purpose -- from Python and from a raw write to
# descriptor 1, the way espeak's C would be -- because the engine's stdout is
# the wire and none of that may arrive on it.
STUB_KOKORO = r'''
import atexit, os, sys

atexit.register(lambda: print("stub: normal exit", file=sys.stderr, flush=True))


class Timing:
    def __init__(self, phoneme, start, end):
        self.phoneme, self.start, self.end = phoneme, start, end


class Audio:
    def __init__(self, samples):
        self.samples = samples

    def clip(self, low, high):
        return self

    def __mul__(self, factor):
        return self

    def astype(self, kind):
        return self

    def tobytes(self):
        return b"\x10\x00" * self.samples


class Tokenizer:
    def phonemize(self, word, lang):
        return word


class Kokoro:
    def __init__(self, model, voices):
        self.tokenizer = Tokenizer()
        print("stub: noise on stdout while loading")

    def create(self, text, voice, lang):
        print("stub: warm %s %s" % (voice, lang), file=sys.stderr, flush=True)
        return self.create_timed(text, voice=voice, lang=lang)[:2]

    def create_timed(self, text, voice, lang):
        if voice not in ("af_heart", "pf_dora"):
            raise KeyError(voice)
        print("stub: noise on stdout while speaking", flush=True)
        os.write(1, b"stub: noise written to descriptor 1\n")
        spoken, clock = [], 0.0
        for index, word in enumerate(text.split()):
            if index:
                spoken.append(Timing(" ", clock, clock))
            spoken.append(Timing(word[0], clock, clock + 0.25))
            clock += 0.3
        return Audio(240 * len(text.split())), 24000, spoken
'''

# Runs server.py's real main() in a process of its own, with every import of
# the model's stack refused and remembered, then asks it what a client would.
DRIVER = r'''
import importlib.util, json, os, signal, sys, threading, time, urllib.error, urllib.request

HEAVY = ("kokoro_onnx", "numpy", "onnxruntime", "phonemizer")
attempts = []


class Refuse:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in HEAVY:
            attempts.append(name)
            raise ImportError("the proxy imported " + name)
        return None


sys.meta_path.insert(0, Refuse())
server_py, fake, diary, models, out = sys.argv[1:]
spec = importlib.util.spec_from_file_location("calliope_proxy", server_py)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
proxy.ENGINE.command = [sys.executable, fake, diary]
proxy.MODELS = models
base = "http://127.0.0.1:%d" % proxy.PORT


def ask(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, data=data), timeout=20) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def loaded():
    return json.loads(ask("/status")[1])["mac"]["loaded"]


def drive():
    seen = {}
    try:
        for _ in range(100):
            try:
                if ask("/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        seen["loaded at start"] = loaded()
        status, audio = ask("/v1/audio/speech",
                            {"input": "one two", "voice": "af_heart", "response_format": "wav"})
        seen["speech"] = [status, audio[:4].decode("latin-1"), len(audio)]
        seen["loaded after speech"] = loaded()
        deadline = time.time() + 3
        while time.time() < deadline and loaded():
            time.sleep(0.2)
        seen["loaded 3 s later"] = loaded()
        seen["speech again"] = ask("/v1/audio/speech", {"input": "three", "voice": "af_heart"})[0]
    finally:
        seen["heavy modules"] = sorted(name for name in sys.modules if name.split(".")[0] in HEAVY)
        seen["attempts"] = attempts
        with open(out, "w") as written:
            json.dump(seen, written)
        os.kill(os.getpid(), signal.SIGTERM)


threading.Thread(target=drive, daemon=True).start()
proxy.main([])
'''


def _models(directory):
    """A voices file shaped like the real one: an npz is a zip of <name>.npy."""
    directory.mkdir(exist_ok=True)
    with zipfile.ZipFile(directory / "voices-v1.0.bin", "w") as voices:
        for name in ("af_heart", "pf_dora", "bm_george", "zf_xiaobei"):
            voices.writestr(name + ".npy", b"not really an array")
    return directory


def _write(path, text):
    path.write_text(text)
    return path


def _events(diary):
    if not diary.exists():
        return []
    return [json.loads(line) for line in diary.read_text().splitlines()]


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _environment(**extra):
    """This process's environment without anything Calliope's, plus `extra`."""
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith("CALLIOPE_")}
    environment.update(extra)
    return environment


class Proxy:
    def __init__(self, module, url, diary):
        self.module, self.url, self.diary = module, url, diary

    def ask(self, path, body=None, method=None, headers=None):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, headers=headers or {},
                                         method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(request, timeout=20) as answer:
                return answer.status, answer.headers, answer.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def speak(self, **body):
        return self.ask("/v1/audio/speech", body)

    def starts(self):
        return [event for event in _events(self.diary) if event["event"] == "start"]


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    """server.py loaded with an environment, serving on a free port, its engine the stand-in."""
    fake = _write(tmp_path / "fake_engine.py", FAKE_ENGINE)
    models = _models(tmp_path / "models")
    started = []

    def start(**environment):
        for name in list(os.environ):
            if name.startswith("CALLIOPE_"):
                monkeypatch.delenv(name)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        spec = importlib.util.spec_from_file_location("calliope_proxy_under_test", SERVER_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.MODELS = str(models)
        diary = tmp_path / ("engine-%d.log" % len(started))
        module.ENGINE.command = [sys.executable, str(fake), str(diary)]
        server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
        started.append((server, module))
        return Proxy(module, "http://127.0.0.1:%d" % server.server_address[1], diary)

    yield start
    for server, module in started:
        server.shutdown()
        server.server_close()
        module.ENGINE.stop("the test is over")


def _run_proxy_process(tmp_path, **environment):
    """server.py's main() in its own process: what it saw, its exit status, the engine's diary."""
    fake = _write(tmp_path / "fake_engine.py", FAKE_ENGINE)
    driver = _write(tmp_path / "driver.py", DRIVER)
    diary, out = tmp_path / "engine.log", tmp_path / "seen.json"
    models = _models(tmp_path / "models")
    finished = subprocess.run(
        [sys.executable, str(driver), str(SERVER_PY), str(fake), str(diary), str(models), str(out)],
        env=_environment(CALLIOPE_PORT=str(_free_port()), CALLIOPE_IDLE_SECONDS="0", **environment),
        capture_output=True, text=True, timeout=60)
    assert out.exists(), finished.stdout + finished.stderr
    return json.loads(out.read_text()), finished, _events(diary)


def test_the_proxy_never_imports_the_model(tmp_path):
    """THE WHOLE POINT OF THE SPLIT. Kokoro resident is 430-510 MB and only a
    process exit gives it back, so the process that stays up must never have
    imported it -- not numpy, not onnxruntime, not even attempted and caught.
    Run with keep-loaded, so the engine starts with the proxy and the proxy is
    exercised at its busiest; and with a key in its environment, which the
    engine must not inherit."""
    seen, finished, events = _run_proxy_process(
        tmp_path, CALLIOPE_KEEP_LOADED="1", CALLIOPE_ENGINE_IDLE_SECONDS="1",
        CALLIOPE_URL="http://127.0.0.1:9", CALLIOPE_KEY="stand-in-key")

    assert seen["heavy modules"] == [] and seen["attempts"] == [], \
        "the proxy imported the model's stack itself"
    assert seen["loaded at start"] is True, "keep-loaded did not start the engine with the proxy"
    assert seen["speech"][:2] == [200, "RIFF"]
    assert seen["loaded 3 s later"] is True, "a kept engine was unloaded for being idle"
    assert seen["speech again"] == 200
    # SIGTERM to the proxy is a normal exit, and the engine follows it through EOF.
    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert [event["event"] for event in events] == ["start", "request", "request", "eof"]
    assert events[0]["environment"] == [], "the engine was handed Calliope's environment, key included"


def test_an_idle_engine_leaves_through_its_stdin_and_comes_back(tmp_path):
    seen, finished, events = _run_proxy_process(tmp_path, CALLIOPE_ENGINE_IDLE_SECONDS="1")

    assert seen["loaded at start"] is False, "the engine started before anything asked to speak"
    assert seen["speech"][:2] == [200, "RIFF"]
    assert seen["loaded after speech"] is True
    assert seen["loaded 3 s later"] is False, "the idle engine was not stopped"
    assert seen["speech again"] == 200, "a stopped engine did not come back for the next request"
    assert finished.returncode == 0
    assert [event["event"] for event in events] == ["start", "request", "eof"] * 2
    assert events[0]["pid"] != events[3]["pid"]


def test_pcm_by_default_and_a_wav_when_asked(proxy, tmp_path):
    local = proxy()
    status, headers, pcm = local.speak(input="one two three", voice="af_heart")
    assert status == 200 and headers["Content-Type"] == "audio/pcm"
    assert pcm == b"\x01\x02" * 300
    assert json.loads(headers["X-Word-Timings"]) == [[0, 0.4], [0.5, 0.9], [1.0, 1.4]]

    status, headers, body = local.speak(input="one two three", voice="af_heart",
                                        response_format="wav")
    assert status == 200 and headers["Content-Type"] == "audio/wav"
    assert headers["X-Word-Timings"]
    saved = tmp_path / "saved.wav"
    saved.write_bytes(body)
    with wave.open(str(saved)) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (24000, 1, 2)
        assert audio.readframes(audio.getnframes()) == pcm
    # One engine served both: it stays up between requests.
    assert len(local.starts()) == 1


def test_a_mistake_is_refused_before_the_engine_starts(proxy):
    """A typo costs a sentence, not two seconds and half a gigabyte."""
    local = proxy()
    for body in ({"input": "hi", "response_format": "mp3"},
                 {"input": "hi", "voice": "xx_nobody"},
                 {"voice": "af_heart"},
                 [1, 2],
                 b"{not json"):
        status, _, refusal = local.ask("/v1/audio/speech", body)
        assert status == 400, body
        assert json.loads(refusal)["error"]["code"] == "invalid_request"
    assert local.starts() == []


def test_the_engines_own_refusal_reaches_the_caller(proxy):
    status, _, refusal = proxy().speak(input="refuse", voice="af_heart")
    assert status == 400
    assert json.loads(refusal) == {"error": {"code": "invalid_request",
                                             "message": "refused by the stand-in"}}


def test_a_dead_engine_is_replaced_without_the_caller_noticing(proxy):
    local = proxy()
    assert local.speak(input="first", voice="af_heart")[0] == 200
    first = local.starts()[0]["pid"]
    os.kill(first, signal.SIGKILL)       # the stand-in; the real engine is never killed
    deadline = time.time() + 5
    while local.module.ENGINE.loaded and time.time() < deadline:
        time.sleep(0.05)

    assert local.speak(input="second", voice="af_heart")[0] == 200
    assert [start["pid"] for start in local.starts()][0] == first
    assert len({start["pid"] for start in local.starts()}) == 2


def test_an_engine_that_dies_under_a_request_is_tried_once_more_then_reported(proxy):
    local = proxy()
    status, _, refusal = local.speak(input="die", voice="af_heart")
    assert status == 500
    assert json.loads(refusal)["error"]["code"] == "engine_failed"
    assert "exit status 3" in json.loads(refusal)["error"]["message"]
    assert len(local.starts()) == 2, "the request was not retried on a fresh engine"
    # And the proxy is still there for the next one.
    assert local.speak(input="alive", voice="af_heart")[0] == 200


def test_the_engine_is_given_composed_text(proxy):
    """Decomposed accents reach espeak as a bare letter: "avó" became "avô"."""
    local = proxy()
    assert local.speak(input="avó", voice="pf_dora")[0] == 200
    request = [event for event in _events(local.diary) if event["event"] == "request"][0]
    assert request["request"] == {"input": "avó", "voice": "pf_dora", "lang": "pt-br"}


def test_a_busy_engine_is_never_unloaded_and_an_idle_one_leaves_through_eof(proxy):
    local = proxy()
    engine = local.module.ENGINE
    assert local.speak(input="hello", voice="af_heart")[0] == 200
    assert engine.unload_if_idle(3600) is False
    with engine.lock:                    # what a synthesis in progress looks like from outside
        assert engine.unload_if_idle(0) is False
    assert engine.unload_if_idle(0) is True
    assert _events(local.diary)[-1]["event"] == "eof", "the engine was signalled, not let go"
    status, _, body = local.ask("/status")
    assert json.loads(body)["mac"]["loaded"] is False


def test_this_macs_voice_switched_off_is_off(proxy):
    local = proxy(CALLIOPE_LOCAL="0")
    assert json.loads(local.ask("/v1/models")[2])["data"] == []
    assert json.loads(local.ask("/voices")[2]) == {"voices": [], "detail": []}
    for model in ("kokoro", "tts-1"):
        status, _, refusal = local.speak(model=model, input="hello", voice="af_heart")
        assert status == 404
        error = json.loads(refusal)["error"]
        assert error["code"] == "mac_engine_off"
        assert "calliope/%s" % model in error["message"]
    assert json.loads(local.ask("/status")[2])["mac"]["on"] is False
    assert local.starts() == []


def test_the_voices_come_from_the_voices_file_without_the_engine(proxy):
    local = proxy()
    status, _, body = local.ask("/voices")
    listing = json.loads(body)
    assert status == 200
    assert listing["voices"] == ["af_heart", "bm_george", "pf_dora", "zf_xiaobei"]
    assert {row["name"]: (row["origin"], row["ref"], row["language"], row["model"])
            for row in listing["detail"]} == {
        "af_heart": ("mac", "mac/af_heart", "en", "kokoro"),
        "bm_george": ("mac", "mac/bm_george", "en", "kokoro"),
        "pf_dora": ("mac", "mac/pf_dora", "pt", "kokoro"),
        "zf_xiaobei": ("mac", "mac/zf_xiaobei", "zh", "kokoro"),
    }
    models = json.loads(local.ask("/v1/models")[2])["data"]
    assert [(row["id"], row["owned_by"]) for row in models] == [
        ("kokoro", "calliope-local"), ("tts-1", "calliope-local"), ("tts-1-hd", "calliope-local")]
    assert local.starts() == [], "listing voices started the model"


def test_a_web_page_is_refused_on_every_route_but_health(proxy):
    local = proxy()
    page = {"Origin": "https://example.com"}
    for method, path, body in (("GET", "/status", None), ("GET", "/v1/models", None),
                               ("GET", "/voices", None), ("GET", "/calliope/test", None),
                               ("POST", "/v1/audio/speech", {"input": "hi"}),
                               ("POST", "/v1/audio/transcriptions", b"--x--"),
                               ("GET", "/jobs", None), ("DELETE", "/jobs/abc", None)):
        status, headers, refusal = local.ask(path, body, method=method, headers=page)
        assert status == 403, (method, path)
        assert json.loads(refusal)["error"]["code"] == "browser_not_allowed"
        assert "Access-Control-Allow-Origin" not in headers
    assert local.ask("/health", headers=page)[0] == 200
    assert local.starts() == []
    status, _, refusal = local.ask("/nowhere")
    assert status == 404 and json.loads(refusal)["error"]["code"] == "not_found"


def test_status_says_what_is_running(proxy):
    local = proxy(CALLIOPE_PORT="47999", CALLIOPE_ENGINE_IDLE_SECONDS="120")
    status, _, body = local.ask("/status")
    assert status == 200
    assert json.loads(body) == {
        "port": 47999,
        "mac": {"on": True, "loaded": False, "keep_loaded": False, "idle_seconds": 120},
        "calliope": {"on": False, "url": None},
    }
    local.speak(input="hello", voice="af_heart")
    assert json.loads(local.ask("/status")[2])["mac"]["loaded"] is True


def _engine(tmp_path):
    """server.py --engine against the stub Kokoro, its stdout the wire."""
    stub = tmp_path / "stub" / "kokoro_onnx"
    stub.mkdir(parents=True)
    _write(stub / "__init__.py", STUB_KOKORO)
    return subprocess.Popen([sys.executable, str(SERVER_PY), "--engine"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=_environment(PYTHONPATH=str(tmp_path / "stub")))


def _request(engine, **request):
    engine.stdin.write(json.dumps(request).encode() + b"\n")
    engine.stdin.flush()
    header = json.loads(engine.stdout.readline())
    return header, engine.stdout.read(header["bytes"]) if header["ok"] else b""


def test_the_engine_speaks_the_pipe_protocol_and_leaves_on_eof(tmp_path):
    engine = _engine(tmp_path)
    try:
        header, pcm = _request(engine, input="hello there world", voice="af_heart", lang="en-us")
        assert header == {"ok": True, "bytes": 1440,
                          "timings": [[0.0, 0.25], [0.3, 0.55], [0.6, 0.85]]}
        assert pcm == b"\x10\x00" * 720
        # Refusals come back on the wire as refusals; the engine carries on.
        assert _request(engine, input="hi", voice="xx_nobody", lang="en-us")[0]["status"] == 400
        engine.stdin.write(b"not json\n")
        engine.stdin.flush()
        assert json.loads(engine.stdout.readline())["code"] == "invalid_request"
        assert _request(engine, input="again", voice="pf_dora", lang="pt-br")[0]["ok"] is True
        rest, log = engine.communicate(timeout=20)     # closes stdin
    finally:
        if engine.poll() is None:
            engine.terminate()
            engine.wait()
    assert rest == b"", "something other than a reply reached the wire"
    assert engine.returncode == 0
    log = log.decode()
    assert "stub: warm af_heart en-us" in log and "stub: warm pf_dora pt-br" in log, \
        "the engine no longer warms both languages it is most often asked for"
    assert "noise on stdout" in log and "noise written to descriptor 1" in log, \
        "the stub's noise went somewhere other than the log"
    assert "stub: normal exit" in log, "EOF did not end the engine through a normal exit"


def test_the_engine_turns_sigterm_into_a_normal_exit(tmp_path):
    """phonemizer removes its espeak copy only on a normal exit."""
    engine = _engine(tmp_path)
    try:
        assert _request(engine, input="ready", voice="af_heart", lang="en-us")[0]["ok"] is True
        engine.terminate()
        _, log = engine.communicate(timeout=20)
    finally:
        if engine.poll() is None:
            engine.kill()
            engine.wait()
    assert engine.returncode == 0
    assert "stub: normal exit" in log.decode()

