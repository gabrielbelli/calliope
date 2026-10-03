"""Calliope's local proxy, and the Kokoro engine it starts when this Mac has to speak.

One file, two processes:
  python server.py            THE PROXY. Standard library only: it never imports Kokoro or
                              numpy. It answers everything below on 127.0.0.1:$CALLIOPE_PORT,
                              forwards what belongs to a Calliope server, and starts the engine
                              when this Mac is asked to speak.
  python server.py --engine   THE ENGINE. A child of the proxy that loads Kokoro and answers
                              over its pipes: one JSON request per line on stdin,
                              {"input", "voice", "lang"}; one JSON header line on stdout,
                              {"ok": true, "bytes": N, "timings": [[start, end], ...] | null}
                              followed by N bytes of PCM, or {"ok": false, "status",
                              "code", "message"}. It exits when its stdin closes.

Every route but GET /health refuses a request that carries an Origin header:
  GET  /health                  -> 200 "ok": the proxy is up; the engine may be unloaded
  GET  /status                  -> {"port", "mac": {"on", "loaded", "keep_loaded",
                                   "idle_seconds"}, "calliope": {"on", "url"}}
  GET  /v1/models               -> kokoro, tts-1, tts-1-hd (owned_by calliope-local) and every
                                   Calliope server model as "calliope/<id>" (calliope-remote)
  GET  /voices                  -> {"voices": [names], "detail": [{"name", "origin", "ref",
                                   "language", "model"}]}, both sides merged
  GET  /calliope/test           -> fresh checks of the Calliope server, stopping at the first
                                   that fails
  POST /v1/audio/speech         {"model", "input", "voice", "response_format": "pcm" | "wav"}
                                -> a local model: 24 kHz 16-bit mono PCM (or that in a WAV),
                                   X-Word-Timings: [[start, end], ...] seconds, one pair per
                                   whitespace-separated word of `input` (an extension;
                                   services/tts has no equivalent, and the player falls back to
                                   an estimate from word lengths when the header is missing or
                                   does not fit)
                                -> "calliope/<id>": forwarded to the Calliope server as "<id>"
  POST /v1/audio/transcriptions, /v1/audio/translations, and /jobs, /jobs/{id},
       /jobs/{id}/audio         -> forwarded to the Calliope server as they are
The language comes from the voice's first letter, as in Kokoro's own naming, and `speed` is
ignored: the player changes speed itself, instantly, while playing.

The proxy exits by itself after CALLIOPE_IDLE_SECONDS without requests; the engine is
stopped after CALLIOPE_ENGINE_IDLE_SECONDS without speech, unless CALLIOPE_KEEP_LOADED=1.
"""
import json
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import ipaddress
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UNVERIFIED = ssl._create_unverified_context()


def tls_for(url):
    """Verify the certificate unless there is no name to verify it against.

    A KEY TRAVELS ON THIS CONNECTION, so this is not a cosmetic choice: an
    unverified TLS session is one anything on the path can sit in the middle
    of, and it would be handed the Authorization header on the way past.

    An earlier draft of this file disabled verification outright, on the
    reasoning that a home server presents a certificate for a name it is not
    reached by. Measured against the real deployment that was simply false --
    it answers on its own hostname with a Let's Encrypt certificate that
    verifies. What remains true is the case it was reaching for: a server typed
    in as "https://192.168.1.5:30080" has no name in it, so no certificate can
    match, and refusing would make that address unusable rather than safer.

    So: verify by name, and accept that an address given as a bare IP is
    trusted on the strength of being on the owner's own network.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https":
        # NOT A TLS DECISION AT ALL, and worth saying out loud: an http address
        # sends the Authorization header in clear over whatever network is
        # between here and there. Refusing it outright would make a plain-HTTP
        # server on a home LAN unusable, which is a real deployment, so it is
        # allowed and said rather than allowed and hidden.
        print("warning: %s is not https; the key is sent in clear" % url, flush=True)
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None          # a name -- urlopen's default context verifies it
    return UNVERIFIED


def upstream(method, path, body=None, headers=None, keyed=True, timeout=600):
    """One request to the Calliope server: (status, headers, body), refusals included.

    THE ONLY PLACE THIS FILE OPENS A CONNECTION, so the two decisions every
    outbound request has to make are made once: the key goes on it (unless the
    caller says otherwise -- only the reachability check does), and the
    certificate is decided by tls_for. A second request written beside this one
    is how a credential ends up on an unverified connection.

    An HTTP refusal is an answer, not a failure, and comes back like any other;
    only a server that did not answer at all raises.
    """
    sent = dict(headers or {})
    if keyed:
        sent["authorization"] = "Bearer " + CALLIOPE_KEY
    request = urllib.request.Request(CALLIOPE_URL + path, data=body, headers=sent, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=tls_for(CALLIOPE_URL)) as answer:
            return answer.status, answer.headers, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def remote_model_names(fresh=False):
    """What the Calliope server says it has, asked once and remembered.

    CACHED, AND OFF THE REQUEST PATH. Asking the server on every /v1/models
    would make a listing as slow as the network on a good day and hang it on a
    bad one -- the same defect as a health check that blocks on a backend, which
    this project has already paid for once.

    EVERY NAME, KOKORO INCLUDED. These used to be filtered against the local
    names, because unprefixed they collided: "kokoro" could only mean one side.
    With the "calliope/" prefix the remote kokoro has a name of its own, and it
    is the one a voice chosen from the Calliope side is spoken with.
    """
    now = time.time()
    if not fresh and REMOTE_MODELS[1] > now - 60:
        return REMOTE_MODELS[0]
    names = []
    try:
        status, _, data = upstream("GET", "/v1/models", timeout=5)
        if status != 200:
            raise RuntimeError("HTTP %d" % status)
        names = [row["id"] for row in json.loads(data).get("data", []) if row.get("id")]
    except Exception as error:
        print("could not list remote models: %r" % error, flush=True)
        names = REMOTE_MODELS[0]          # keep the last good answer
    REMOTE_MODELS[0] = names
    REMOTE_MODELS[1] = now
    return names


REMOTE_MODELS = [[], 0.0]
REMOTE_VOICES = [[], 0.0]


def remote_voice_names(fresh=False):
    """The Calliope server's own preset voices, cached like its model list.

    ONLY THE FAST ONES CAN BE HERE, and that is a property of the endpoint
    rather than a filter applied afterwards: the gateway routes GET /voices to
    tts-stack alone. The engines that answer 202 with a job instead of audio --
    everything the gateway marks owned_by tts-long -- have no voices on that
    path at all, so a name that arrives here is a name the player can actually
    speak with. Offering a job-based voice in a picker would produce a reader
    that shows a capsule and never makes a sound.
    """
    now = time.time()
    if not fresh and REMOTE_VOICES[1] > now - 60:
        return REMOTE_VOICES[0]
    names = []
    try:
        status, _, data = upstream("GET", "/voices", timeout=5)
        if status != 200:
            raise RuntimeError("HTTP %d" % status)
        names = [name for name in json.loads(data).get("voices", []) if isinstance(name, str)]
    except Exception as error:
        print("could not list remote voices: %r" % error, flush=True)
        names = REMOTE_VOICES[0]
    REMOTE_VOICES[0] = names
    REMOTE_VOICES[1] = now
    return names

# THE MODEL IS NOT BESIDE THIS FILE ANY MORE, and assuming it was failed
# totally: this script lives inside Calliope.app now, where a 310 MB model
# cannot go -- a signed bundle must not be written to, and a re-install would
# have to download it again. So the code is in the bundle and the model is in
# the runtime, and this is the line that knows the difference. It matches
# shared/paths.swift, which a test holds it to.
RUNTIME = os.path.expanduser("~/.local/share/calliope")

# WHERE THE MODEL IS, which is not always the same place. install.sh downloads
# it into the runtime directory; a Homebrew formula cannot write there, because
# its install step is sandboxed to the formula's own prefix, so it puts the
# model beside this file inside Calliope.app. Beside-this-file first, because an
# app that carries its own model is the self-contained one.
HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = HERE if os.path.exists(os.path.join(HERE, "kokoro-v1.0.onnx")) else RUNTIME
# The daemon owns the port, because it is the one with a setting for it; 47815
# is the port everything used before there was a setting.
PORT = int(os.environ.get("CALLIOPE_PORT", 47815))
# WHO IS HOLDING THIS OPEN DECIDES WHEN IT CLOSES. Started by the one-shot
# player, the server has to time itself out or it would outlive every reason to
# exist -- 15 minutes, measured against how long somebody keeps reading things
# aloud in one sitting. Started by the daemon, the daemon IS the reason, and a
# server that shuts itself down underneath it makes "kept warm" a claim rather
# than a behaviour: the next press pays the load again.
#
# The load is 1.69-1.86 s, measured from this server's own log. Small, and
# exactly the kind of small that is felt, because it lands between pressing the
# key and hearing anything.
#
# 0 means never, and the environment is the seam because the daemon already
# spawns this process and the player does not have to learn anything new.
IDLE_SECONDS = int(os.environ.get("CALLIOPE_IDLE_SECONDS", 15 * 60))
# THIS MAC'S VOICE CAN BE SWITCHED OFF, and off means off: no local rows in the
# listings, and a request for a local model is refused with what to ask for
# instead rather than quietly answered by the other side.
LOCAL = os.environ.get("CALLIOPE_LOCAL", "1") != "0"
# THE ENGINE IS STOPPED WHEN NOTHING HAS SPOKEN FOR A WHILE, because stopping it
# is the only thing that gives its memory back (see Engine). Ten minutes by
# default: long enough that reading through an article does not pay the load
# between paragraphs. Kept loaded is the owner choosing an instant first word
# over half a gigabyte, and then the engine starts with the proxy.
ENGINE_IDLE_SECONDS = int(os.environ.get("CALLIOPE_ENGINE_IDLE_SECONDS", 600))
KEEP_LOADED = os.environ.get("CALLIOPE_KEEP_LOADED", "0") == "1"
# WHERE THE OTHER ENGINES LIVE, WHEN THERE ARE ANY. Empty means this machine is
# the whole of it: Kokoro, 54 preset voices, no network. Set, and a model this
# server does not have is forwarded rather than refused -- cloned voices and
# Chatterbox need a GPU this Mac does not have.
#
# THE DAEMON SUPPLIES BOTH, because it is the thing that knows: it reads the URL
# from the settings and the key from the Keychain, and restarts this process
# when either changes. Nothing is read from disk here, so a server started by
# the one-shot player has no credential and no remote at all, which is the
# correct default for a path nobody configured.
CALLIOPE_URL = os.environ.get("CALLIOPE_URL", "").rstrip("/")
CALLIOPE_KEY = os.environ.get("CALLIOPE_KEY", "")
# AN ADDRESS WITHOUT A KEY IS NO REMOTE AT ALL. The gateway answers nothing but
# its liveness without one, so the address alone would list models it can never
# reach and forward requests only to have each one refused. Said once, here,
# rather than as a 401 per request.
if CALLIOPE_URL and not CALLIOPE_KEY:
    print("warning: CALLIOPE_URL is set without CALLIOPE_KEY, and the Calliope server "
          "requires a key; nothing is forwarded", flush=True)
    CALLIOPE_URL = ""

# What this machine can say without asking anybody.
LOCAL_MODELS = ("kokoro", "tts-1", "tts-1-hd")
# A model asked for with this in front is the Calliope server's, whatever the
# rest of the name is -- "calliope/kokoro" is the server's Kokoro, not this one.
REMOTE_PREFIX = "calliope/"
# Kept on a forwarded reply: word timings for the karaoke, and the two headers
# a long-form voice answers with. Location is "/jobs/{id}", relative, so it
# resolves against this proxy and comes back through it with the key added --
# which is why it needs no rewriting.
PASSED_HEADERS = ("X-Word-Timings", "Location", "Retry-After")
JOB_PATH = re.compile(r"/jobs(/[A-Za-z0-9_-]+(/audio)?)?")

LANG_BY_VOICE_PREFIX = {
    "a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi",
    "i": "it", "j": "ja", "p": "pt-br", "z": "cmn",
}
# The same prefixes as the language codes the player and the settings use.
LANGUAGE_BY_VOICE_PREFIX = {
    "a": "en", "b": "en", "e": "es", "f": "fr", "h": "hi",
    "i": "it", "j": "ja", "p": "pt", "z": "zh",
}
SAMPLE_RATE = 24000

KOKORO = None           # the engine's model; the proxy never has one
LAST_REQUEST = [time.time()]
LOCAL_VOICES = []


def word_timings(words, spoken, lang):
    """[start, end] per word, from Kokoro's per-phoneme timings (spaces separate words)."""
    groups, current = [], None
    for timing in spoken:
        if timing.phoneme == " ":
            if current:
                groups.append(current)
            current = None
        elif current is None:
            current = [timing.start, timing.end]
        else:
            current[1] = max(current[1], timing.end)
    if current:
        groups.append(current)
    if not groups or not words:
        return None
    if len(groups) == len(words):
        return groups

    # Numbers and abbreviations expand ("42" -> "forty two"): count each word's own groups.
    counts = [max(1, len(KOKORO.tokenizer.phonemize(word, lang).split())) for word in words]
    if sum(counts) == len(groups):
        result, position = [], 0
        for count in counts:
            result.append([groups[position][0], groups[position + count - 1][1]])
            position += count
        return result

    # Still no match: spread the spoken span over the words by length.
    start, end = groups[0][0], groups[-1][1]
    weights = [len(word) + 1 for word in words]
    total, cursor, result = sum(weights), 0, []
    for weight in weights:
        first = start + (end - start) * cursor / total
        cursor += weight
        result.append([first, start + (end - start) * cursor / total])
    return result


def local_voice_names():
    """This Mac's voices, read from the voices file rather than from the model.

    THE NAMES COST NOTHING AND THE MODEL COSTS HALF A GIGABYTE. voices-v1.0.bin
    is an npz -- a zip with one <name>.npy per voice, which is exactly what
    Kokoro's own get_voices() lists once it has loaded it -- so the names are the
    zip's directory, read without decompressing anything. Asking the engine
    would start it, and a settings window that lists voices would then hold the
    model resident for the next ten minutes for the sake of a menu.
    """
    if not LOCAL_VOICES:
        try:
            with zipfile.ZipFile(os.path.join(MODELS, "voices-v1.0.bin")) as voices:
                LOCAL_VOICES[:] = sorted(name[:-len(".npy")] for name in voices.namelist()
                                         if name.endswith(".npy"))
        except (OSError, zipfile.BadZipFile) as error:
            print("could not list this Mac's voices: %r" % error, flush=True)
    return LOCAL_VOICES


def voice_language(name):
    """"en", "pt", ... for a Kokoro-style name ("pf_dora"), None for any other name."""
    if isinstance(name, str) and re.fullmatch(r"[a-z][fm]_\w+", name):
        return LANGUAGE_BY_VOICE_PREFIX.get(name[0])
    return None


def wav(pcm):
    """The engine's PCM with the 44 bytes that make it a file: 24 kHz, mono, 16-bit.

    FOR A CALLER THAT SAVES WHAT IT GETS. Headerless PCM is what a player wants,
    and it is useless as a file: nothing opens it without being told the rate,
    the width and the channel count, which is exactly what this header says.
    """
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16,
                       1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16, b"data", len(pcm)) + pcm


class EngineFailure(Exception):
    """The engine's answer, or its absence, as the status and error the caller gets."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Engine:
    """The Kokoro child: started when this Mac has to speak, stopped when it has not.

    A SEPARATE PROCESS, BECAUSE NOTHING ELSE GIVES THE MEMORY BACK. Measured:
    this server with Kokoro loaded has a footprint of 429-509 MB, and deleting
    the model and collecting garbage took it from 509 to 508. onnxruntime's
    arenas and numpy's buffers stay mapped for the life of the process, so the
    only way to stop paying for a model nobody is using is to end the process
    that loaded it. The proxy stays -- 9-15 MB of standard library -- and the
    model lives in a child it can let go of.

    THE PIPE IS THE LEASH. The engine reads its requests from stdin and exits
    when stdin closes, and the proxy holds the only write end: closing it is
    how the proxy stops the engine, and the proxy exiting, for any reason,
    closes it too. So there is no orphaned half a gigabyte to hunt for after a
    crash, and the engine's exit is always a normal one, which is what
    phonemizer needs to remove its espeak copy.

    One synthesis at a time, under the lock, as before; the player fetches
    sequentially anyway.
    """

    def __init__(self, command):
        self.command = command
        self.process = None
        self.lock = threading.Lock()
        self.used = time.time()

    @property
    def loaded(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        """Spawn the engine; the caller holds the lock.

        IT DOES NOT WAIT FOR THE MODEL. A request written now waits in the pipe
        until the engine has loaded and warmed up, so the first request pays the
        load once, and a keep-loaded start at boot pays it before anybody asks.

        NONE OF CALLIOPE'S ENVIRONMENT GOES WITH IT, the key least of all. The
        engine never talks to a network, and a credential in the environment of
        a process that does not need it is one more place for it to be read
        from -- a crash report, a process listing tool, a library that logs its
        environment. It also keeps the engine's copy of this module quiet: the
        warning about an address without a key would otherwise be printed onto
        the pipe the replies travel on.
        """
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith("CALLIOPE_")}
        try:
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, env=environment)
        except OSError as error:
            self.process = None
            raise EngineFailure(500, "engine_failed",
                                "this Mac's engine could not be started: %s" % error)
        print("engine started (pid %d)" % self.process.pid, flush=True)

    def stop(self, reason):
        """Close the engine's stdin and wait for it to leave by itself.

        Only an engine that ignores its closed stdin is signalled, SIGTERM
        first, because the engine turns that into a normal exit too. SIGKILL is
        the last resort for one that is stuck, where half a gigabyte held by a
        process nobody can talk to is worse than a leaked espeak copy.
        """
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass           # already gone: the write end was the last thing holding it
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        print("engine stopped (%s; exit status %d)" % (reason, process.returncode), flush=True)

    def speak(self, text, voice, lang):
        """(pcm, timings) for one request, starting the engine if it is not running.

        AN ENGINE THAT DIED IS STARTED AGAIN, ONCE, AND NOBODY HAS TO KNOW. One
        that crashed between requests is noticed before anything is written; one
        that dies under a request gets a fresh engine and one more try, because
        the usual cause is the process rather than the text. A second death is
        reported rather than retried forever, since by then it may well be the
        text, and the proxy stays up to answer the next request either way.
        """
        with self.lock:
            try:
                for attempt in (1, 2):
                    if not self.loaded:
                        if self.process is not None:
                            self.stop("it had exited")
                        self.start()
                    process = self.process
                    try:
                        return self.ask(process, text, voice, lang)
                    except (OSError, ValueError, EOFError):
                        process.poll()
                        status = process.returncode
                        self.stop("it stopped answering")
                        if attempt == 2:
                            raise EngineFailure(
                                500, "engine_failed",
                                "this Mac's engine stopped before answering (exit status %s); "
                                "the server log says why" % status)
            finally:
                self.used = time.time()

    @staticmethod
    def ask(process, text, voice, lang):
        request = {"input": text, "voice": voice, "lang": lang}
        process.stdin.write(json.dumps(request).encode() + b"\n")
        process.stdin.flush()
        header = process.stdout.readline()
        if not header:
            raise EOFError("the engine closed its end")
        # Anything that is not a reply means the wire is out of step, and an
        # engine whose next header would be read from the middle of some audio
        # is one to replace, not to keep asking.
        reply = json.loads(header)
        if not isinstance(reply, dict):
            raise ValueError("not a reply: %r" % header[:80])
        if not reply.get("ok"):
            raise EngineFailure(int(reply.get("status") or 500),
                                str(reply.get("code") or "internal_error"),
                                str(reply.get("message") or ""))
        size = reply.get("bytes")
        if not isinstance(size, int) or size < 0:
            raise ValueError("not a reply: %r" % header[:80])
        pcm = process.stdout.read(size)
        if len(pcm) != size:
            raise EOFError("the engine stopped mid-reply")
        return pcm, reply.get("timings")

    def unload_if_idle(self, seconds):
        # Never under a synthesis: one in progress is the opposite of idle.
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if self.process is None or time.time() - self.used < seconds:
                return False
            self.stop("nothing spoken for %d s" % seconds)
            return True
        finally:
            self.lock.release()


ENGINE = Engine([sys.executable, os.path.abspath(__file__), "--engine"])


def check_calliope():
    """What GET /calliope/test answers: the configured server, asked now.

    UNCACHED ON PURPOSE. This is what the settings window shows after a change
    and what somebody runs when something is wrong, and a minute-old answer from
    the listing cache would report the server as it was before the change.

    IN ORDER, AND IT STOPS AT THE FIRST FAILURE. Each check needs the one before
    it: a server that cannot be reached cannot refuse a key, and a refused key
    makes the voices and the speech fail too. Reporting all four would bury the
    one cause under three consequences of it.
    """
    if not CALLIOPE_URL:
        return {"configured": False, "ok": False, "url": None,
                "message": "No Calliope server is configured: Calliope's Settings need its "
                           "address and a key."}
    checks = []

    def check(name, method, path, judge, keyed=True, body=None, timeout=10):
        started = time.monotonic()
        try:
            headers = {"content-type": "application/json"} if body is not None else None
            status, _, data = upstream(method, path, body, headers, keyed=keyed, timeout=timeout)
            ok, detail = judge(status, data, time.monotonic() - started)
        except Exception as error:
            ok, detail = False, unreachable(error, timeout)
        checks.append({"name": name, "ok": ok, "detail": detail,
                       "ms": round((time.monotonic() - started) * 1000)})
        return ok

    def reached(status, data, seconds):
        return status == 200, "answered" if status == 200 else "HTTP %d" % status

    def key(status, data, seconds):
        if status == 200:
            return True, "accepted"
        return False, {401: "key not accepted",
                       403: "key lacks models:read"}.get(status, "HTTP %d" % status)

    def voices(status, data, seconds):
        if status != 200:
            return False, refusal(status, data)
        return True, "%d voices" % len(json.loads(data).get("voices", []))

    def speech(status, data, seconds):
        if status != 200:
            return False, refusal(status, data)
        return True, "%.1f s, %d KB" % (seconds, round(len(data) / 1024))

    sample = {"model": "kokoro", "input": "Calliope.", "voice": "af_heart",
              "response_format": "pcm"}
    passed = (check("reach", "GET", "/health", reached, keyed=False, timeout=5)
              and check("key", "GET", "/v1/models", key)
              and check("voices", "GET", "/voices", voices)
              and check("speech", "POST", "/v1/audio/speech", speech,
                        body=json.dumps(sample).encode(), timeout=60))
    last = checks[-1]
    message = {
        "reach": "%s did not answer: %s",
        "key": "%s answered, but the key failed: %s",
        "voices": "%s took the key, but listing voices failed: %s",
        "speech": "%s took the key, but speaking failed: %s",
    }[last["name"]] % (CALLIOPE_URL, last["detail"])
    if passed:
        message = "%s: key accepted, %s, speech in %s" % (
            CALLIOPE_URL, checks[2]["detail"], checks[3]["detail"].split(",")[0])
    return {"configured": True, "ok": passed, "url": CALLIOPE_URL, "message": message,
            "checks": checks}


def refusal(status, data):
    """"HTTP 503: <the server's own message>", in one line."""
    try:
        said = json.loads(data)["error"]["message"]
    except Exception:
        said = data[:120].decode("utf-8", "replace").strip()
    return ("HTTP %d: %s" % (status, said) if said else "HTTP %d" % status).splitlines()[0]


def unreachable(error, timeout):
    """Why a server did not answer, in words somebody can act on."""
    reason = getattr(error, "reason", error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "its certificate did not verify: %s" % reason.verify_message
    if isinstance(reason, TimeoutError):
        return "no answer within %d s" % timeout
    return str(reason) or type(reason).__name__


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def reply(self, status, body, content_type, headers=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client gave up (health probe timed out while the model was loading, or playback stopped)

    def answer(self, value):
        self.reply(200, json.dumps(value).encode(), "application/json")

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        path = urllib.parse.urlsplit(self.path).path
        if method == "GET" and path == "/health":
            return self.reply(200, b"ok", "text/plain")
        # NO BROWSER MAY SPEND THIS. Loopback is not a boundary a web page
        # respects: any site the owner visits can POST JSON here, and with a
        # Calliope server configured that means their GPU, their gateway key
        # and their OpenAI credit. Browsers attach Origin to every cross-site
        # POST and the real clients -- the player, curl, a script -- attach
        # none, so the header's presence is the whole test. No CORS headers are
        # sent anywhere in this file, so nothing can read a reply either.
        #
        # EVERY ROUTE, NOT ONLY SPEECH. A GET is not harmless here: /jobs/{id}
        # can be deleted, and /calliope/test spends a synthesis on the server.
        # Only /health is left open, because it says nothing but "up".
        if self.headers.get("Origin"):
            return self.refuse(403, "browser_not_allowed",
                               "this server answers local programs, not web pages")
        LAST_REQUEST[0] = time.time()
        if method == "GET" and path == "/status":
            return self.answer(self.status())
        if method == "GET" and path == "/v1/models":
            return self.answer(self.models())
        if method == "GET" and path == "/voices":
            return self.answer(self.voices())
        if method == "GET" and path == "/calliope/test":
            return self.answer(check_calliope())
        if method == "POST" and path == "/v1/audio/speech":
            return self.speech()
        if method == "POST" and path in ("/v1/audio/transcriptions", "/v1/audio/translations"):
            return self.forward_raw(method)
        if JOB_PATH.fullmatch(path):
            return self.forward_raw(method)   # the gateway knows which methods each one takes
        return self.refuse(404, "not_found", "nothing answers %s %s here" % (method, path))

    def status(self):
        return {"port": PORT,
                "mac": {"on": LOCAL, "loaded": ENGINE.loaded, "keep_loaded": KEEP_LOADED,
                        "idle_seconds": ENGINE_IDLE_SECONDS},
                "calliope": {"on": bool(CALLIOPE_URL), "url": CALLIOPE_URL or None}}

    def models(self):
        """Everything this address can be asked for, local and remote alike.

        REMOTE MODELS STAY LISTED WHEN THE SERVER IS DOWN, and that is the
        decision worth writing down. A client caches this list; an entry that
        disappears reads as a misconfiguration and sends somebody digging
        through their own settings. A 503 that says which backend is unreachable
        and since when is a sentence they can act on instead.

        owned_by names where it runs, because OpenAI's model object has no field
        for "what this can do" and the engine name is the closest honest thing.
        """
        data = []
        if LOCAL:
            data += [{"id": name, "object": "model", "owned_by": "calliope-local"}
                     for name in LOCAL_MODELS]
        if CALLIOPE_URL:
            data += [{"id": REMOTE_PREFIX + name, "object": "model", "owned_by": "calliope-remote"}
                     for name in remote_model_names()]
        return {"object": "list", "data": data}

    def voices(self):
        """Every preset this address can speak with, local first.

        The same "voices" list as the gateway's /voices, so a caller that knows
        one knows the other, and "detail" beside it saying where each one runs.
        A name can be on both sides -- af_heart is -- and is then one name in
        the list and two rows in the detail, because "mac/af_heart" and
        "calliope/af_heart" are different machines answering. Merged rather than
        replaced: a Calliope server adds voices, it does not take away the ones
        on this Mac, and the local ones keep working when the network does not.
        """
        detail = []
        if LOCAL:
            detail += [{"name": name, "origin": "mac", "ref": "mac/" + name,
                        "language": voice_language(name), "model": "kokoro"}
                       for name in local_voice_names()]
        if CALLIOPE_URL:
            detail += [{"name": name, "origin": "calliope", "ref": REMOTE_PREFIX + name,
                        "language": voice_language(name), "model": REMOTE_PREFIX + "kokoro"}
                       for name in sorted(remote_voice_names())]
        return {"voices": sorted({row["name"] for row in detail}), "detail": detail}

    def speech(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if not isinstance(body, dict):
                raise TypeError("the body is not a JSON object")
            model = body.get("model") or "kokoro"
            if not isinstance(model, str):
                raise TypeError("model is not a string")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            return self.refuse(400, "invalid_request", str(error) or type(error).__name__)
        if model.startswith(REMOTE_PREFIX):
            return self.forward(body, model[len(REMOTE_PREFIX):], model)
        if model not in LOCAL_MODELS:
            return self.forward(body, model, model)   # checked there, against the real listing
        if not LOCAL:
            return self.refuse(404, "mac_engine_off",
                               "This Mac's voice is off in Calliope's Settings; ask for "
                               "calliope/%s to use the Calliope server" % model)
        self.speak_here(body)

    def speak_here(self, body):
        try:
            form = body.get("response_format") or "pcm"
            if form not in ("pcm", "wav"):
                raise ValueError("this Mac answers response_format pcm or wav, not %r" % (form,))
            # Decomposed accents ("e" + U+0301) reach espeak as a bare "e": "avó" becomes "avô".
            text = unicodedata.normalize("NFC", body["input"])
            voice = body.get("voice") or "af_heart"
            # CHECKED BEFORE THE ENGINE IS STARTED, so a typo costs a sentence
            # instead of two seconds and half a gigabyte; the names are the
            # voices file's own, so the engine would refuse exactly these.
            names = local_voice_names()
            if names and voice not in names:
                raise ValueError("%r is not one of this Mac's voices" % (voice,))
            lang = LANG_BY_VOICE_PREFIX.get(voice[:1], "en-us")
            pcm, timings = ENGINE.speak(text, voice, lang)
        except EngineFailure as failure:
            return self.refuse(failure.status, failure.code, failure.message)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            # 400, NOT 500. Every client mistake used to come back as a server
            # error with a bare text/plain body: {} gave 500 "'input'", a
            # truncated body gave 500 "Expecting value", an unknown voice gave
            # 500 with a KeyError. A caller cannot tell "I sent that wrong"
            # from "the server is broken", and neither can anybody reading a
            # bug report about it.
            return self.refuse(400, "invalid_request", str(error) or type(error).__name__)
        except Exception as error:
            print("speech failed: %r" % error, flush=True)
            return self.refuse(500, "internal_error", str(error))
        headers = {}
        if timings:
            headers["X-Word-Timings"] = json.dumps(timings, separators=(",", ":"))
        if form == "wav":
            return self.reply(200, wav(pcm), "audio/wav", headers)
        self.reply(200, pcm, "audio/pcm", headers)

    def forward(self, body, name, asked):
        """A model this machine does not have, asked of the one that does.

        503 AND NEVER A SUBSTITUTION. Answering a request for a cloned voice in
        a preset one, with nothing saying so, is a defect noticed only after the
        audio has been sent to somebody. The error names the backend and the
        reason, because "it did not work" is not something anybody can act on.

        WHICH IS WHY THE NAME IS CHECKED HERE RATHER THAN LEFT TO THE BACKEND.
        Measured: a request for "nonesuch" came back 200 with eighteen kilobytes
        of MP3 in a voice nobody asked for, because the gateway answers an
        unknown model with a default instead of refusing. Forwarding blind makes
        this proxy the thing that hid the typo, so it does not forward blind --
        a name that is on neither side is refused with both lists in the error.

        The listing is refreshed once before refusing, because the cache is
        sixty seconds old and a model added on the server in that window is a
        real name, not a typo. That costs one request, only on the path that was
        about to fail anyway.

        `asked` is the name as it arrived and `name` the server's own:
        "calliope/chatterbox" is sent as "chatterbox". An unprefixed name the
        server lists is still forwarded, because clients written before the
        prefix ask for "chatterbox" and got it.
        """
        # Before the listing, which has no server to ask: an address set
        # without its key is dropped at startup, and this is where it shows.
        if not CALLIOPE_URL:
            if asked != name:
                return self.no_server()
            return self.refuse(404, "no_such_model",
                               "%r is not one of this machine's models (%s), and no Calliope "
                               "server, an address with its key, is configured to ask."
                               % (asked, model_list()))
        known = remote_model_names()
        if name not in known:
            known = remote_model_names(fresh=True)
        if name not in known:
            return self.refuse(404, "no_such_model",
                               "%r is not a model here (%s) or on the Calliope server at %s (%s)"
                               % (asked, model_list(), CALLIOPE_URL,
                                  ", ".join(REMOTE_PREFIX + known_name for known_name in known)
                                  or "nothing it would name"))
        # LONGER THAN FEELS REASONABLE, ON PURPOSE (the relay's 600 s).
        # Chatterbox is slower than realtime, so a minute of speech is minutes
        # of compute, and a timeout tuned to a local model would cut off every
        # cloned voice.
        self.relay("POST", "/v1/audio/speech", json.dumps(dict(body, model=name)).encode(),
                   "application/json", "%r" % asked)

    def forward_raw(self, method):
        """A transcription, a translation or a job, passed on byte for byte.

        THE BODY IS NOT READ, ONLY CARRIED. A transcription is multipart, and
        parsing it here would mean a parser that can disagree with the
        gateway's about a boundary, a filename or a charset, for no gain: this
        proxy decides nothing about it. So the bytes and the Content-Type, which
        carries the boundary, go on exactly as they came, and the path goes
        with its query string.
        """
        if not CALLIOPE_URL:
            return self.no_server()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        self.relay(method, self.path, body, self.headers.get("Content-Type"),
                   "%s %s" % (method, urllib.parse.urlsplit(self.path).path))

    def relay(self, method, path, body, content_type, what):
        headers = {"content-type": content_type} if content_type else None
        try:
            status, received, data = upstream(method, path, body, headers)
        except Exception as error:
            return self.refuse(503, "backend_unreachable",
                               "%s runs on the Calliope server at %s, which did not answer: %s"
                               % (what, CALLIOPE_URL, unreachable(error, 600)))
        passed = {name: received[name] for name in PASSED_HEADERS if received.get(name)}
        self.reply(status, data, received.get("content-type", "application/octet-stream"), passed)

    def no_server(self):
        self.refuse(404, "no_calliope_server",
                    "No Calliope server is configured to send this to: it takes an address "
                    "with its key, in Calliope's Settings.")

    def refuse(self, status, code, message):
        body = json.dumps({"error": {"code": code, "message": message}}).encode()
        self.reply(status, body, "application/json")


def model_list():
    return ", ".join(LOCAL_MODELS) if LOCAL else "none: this Mac's voice is off"


def exit_when_idle(server):
    if IDLE_SECONDS <= 0:
        print("idle exit disabled; something else owns this process", flush=True)
        return
    while True:
        time.sleep(30)
        if time.time() - LAST_REQUEST[0] > IDLE_SECONDS:
            print("idle for %d s; exiting" % IDLE_SECONDS, flush=True)
            server.shutdown()
            return


def unload_engine_when_idle():
    if KEEP_LOADED:
        print("the engine stays loaded: an instant first word was asked for", flush=True)
        return
    # A quarter of the idle time, so the engine goes within a quarter of it of
    # when it was due -- and never more than 30 s late, or more often than 1 s.
    while True:
        time.sleep(min(30, max(1, ENGINE_IDLE_SECONDS / 4)))
        ENGINE.unload_if_idle(ENGINE_IDLE_SECONDS)


def proxy_main():
    try:
        os.setsid()  # leave the player's process group, so stopping playback keeps the model warm
    except PermissionError:
        pass  # already a session/group leader (started by hand)
    # Exit through the interpreter on SIGTERM, so the finally below closes the
    # engine's stdin and the engine, too, leaves through a normal exit.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print("port %d busy; another server is already running" % PORT, flush=True)
        return
    print("proxy on port %d: this Mac %s, Calliope server %s" % (
        PORT, ("on, kept loaded" if KEEP_LOADED else "on") if LOCAL else "off",
        CALLIOPE_URL or "none"), flush=True)
    if LOCAL and KEEP_LOADED:
        with ENGINE.lock:
            try:
                ENGINE.start()
            except EngineFailure as failure:
                print(failure.message, flush=True)
    LAST_REQUEST[0] = time.time()
    threading.Thread(target=exit_when_idle, args=(server,), daemon=True).start()
    threading.Thread(target=unload_engine_when_idle, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        ENGINE.stop("the proxy is exiting")


def engine_main():
    """Load Kokoro, then answer requests from stdin until it closes.

    STDOUT IS THE WIRE, so nothing else may write to it. Kokoro, onnxruntime,
    phonemizer and espeak are all free to print, and espeak does it from C,
    below anything sys.stdout could intercept. So the real stdout is kept on a
    private descriptor for the replies, and descriptor 1 is pointed at stderr
    -- the server log -- before any of them is imported. A stray line on the
    wire would be read as a reply header and lose the proxy its engine.
    """
    global KOKORO
    sys.stdout.flush()
    wire = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    # Exit through the interpreter on SIGTERM: phonemizer copies libespeak-ng into a temp
    # directory per process and only removes it on a normal exit. Ctrl-C is the proxy's to
    # handle; the engine follows it when the proxy's exit closes the pipe.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    from kokoro_onnx import Kokoro

    started = time.time()
    KOKORO = Kokoro(os.path.join(MODELS, "kokoro-v1.0.onnx"),
                    os.path.join(MODELS, "voices-v1.0.bin"))
    for voice in ("af_heart", "pf_dora"):
        KOKORO.create("ok.", voice=voice, lang=LANG_BY_VOICE_PREFIX[voice[0]])  # first call per language is slow
    print("engine: model ready in %.2f s" % (time.time() - started), file=sys.stderr, flush=True)
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            break
        header, pcm = engine_answer(line)
        wire.write(json.dumps(header).encode() + b"\n" + pcm)
        wire.flush()
    print("engine: stdin closed; exiting", file=sys.stderr, flush=True)


def engine_answer(line):
    try:
        request = json.loads(line)
        text, voice, lang = request["input"], request["voice"], request["lang"]
        audio, _, spoken = KOKORO.create_timed(text, voice=voice, lang=lang)
        timings = word_timings(text.split(), spoken, lang)
        pcm = (audio.clip(-1.0, 1.0) * 32767).astype("<i2").tobytes()
        rounded = [[round(start, 3), round(end, 3)] for start, end in timings] if timings else None
        return {"ok": True, "bytes": len(pcm), "timings": rounded}, pcm
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        return {"ok": False, "status": 400, "code": "invalid_request",
                "message": str(error) or type(error).__name__}, b""
    except Exception as error:
        print("speech failed: %r" % error, file=sys.stderr, flush=True)
        return {"ok": False, "status": 500, "code": "internal_error", "message": str(error)}, b""


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments == ["--engine"]:
        return engine_main()
    proxy_main()


if __name__ == "__main__":
    main()
