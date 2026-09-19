"""Local Kokoro server for calliope-player.

A deliberately small subset of services/tts, answering only what the player asks for. The
player talks to this and to nothing else:
  GET  /health            -> 200 "ok"
  POST /v1/audio/speech   {"input", "voice", "response_format": "pcm"}
                          -> headerless 24 kHz 16-bit mono PCM, like services/tts
                             X-Word-Timings: [[start, end], ...] seconds, one pair per
                             whitespace-separated word of `input` (an extension; services/tts
                             has no equivalent, and the player falls back to an estimate
                             from word lengths when the header is missing or does not fit)
The language comes from the voice's first letter, as in Kokoro's own naming. Only `pcm` is
served, and `speed` is ignored: the player changes speed itself, instantly, while playing.

Loads the model once and exits by itself after IDLE_SECONDS without requests.
"""
import json
import os
import signal
import sys
import threading
import time
import unicodedata
import ipaddress
import ssl
import urllib.error
import urllib.parse
import urllib.request
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


def remote_model_names(fresh=False):
    """What the Calliope server says it has, asked once and remembered.

    CACHED, AND OFF THE REQUEST PATH. Asking the server on every /v1/models
    would make a listing as slow as the network on a good day and hang it on a
    bad one -- the same defect as a health check that blocks on a backend, which
    this project has already paid for once.
    """
    now = time.time()
    if not fresh and REMOTE_MODELS[1] > now - 60:
        return REMOTE_MODELS[0]
    names = []
    try:
        request = urllib.request.Request(
            CALLIOPE_URL + "/v1/models",
            headers={"authorization": "Bearer " + CALLIOPE_KEY} if CALLIOPE_KEY else {})
        with urllib.request.urlopen(request, timeout=5, context=tls_for(CALLIOPE_URL)) as answer:
            listing = json.loads(answer.read())
        names = [row["id"] for row in listing.get("data", [])
                 if row.get("id") not in LOCAL_MODELS]
    except Exception as error:
        print("could not list remote models: %r" % error, flush=True)
        names = REMOTE_MODELS[0]          # keep the last good answer
    REMOTE_MODELS[0] = names
    REMOTE_MODELS[1] = now
    return names


REMOTE_MODELS = [[], 0.0]

# THE MODEL IS NOT BESIDE THIS FILE ANY MORE, and assuming it was failed
# totally: this script lives inside Calliope.app now, where a 310 MB model
# cannot go -- a signed bundle must not be written to, and a re-install would
# have to download it again. So the code is in the bundle and the model is in
# the runtime, and this is the line that knows the difference. It matches
# shared/paths.swift, which a test holds it to.
RUNTIME = os.path.expanduser("~/.local/share/calliope")
PORT = 47815
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

# What this machine can say without asking anybody.
LOCAL_MODELS = ("kokoro", "tts-1", "tts-1-hd")

LANG_BY_VOICE_PREFIX = {
    "a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi",
    "i": "it", "j": "ja", "p": "pt-br", "z": "cmn",
}

KOKORO = None
LOCK = threading.Lock()  # one synthesis at a time; the player fetches sequentially anyway
LAST_REQUEST = [time.time()]


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

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, b"ok", "text/plain")
        if self.path == "/v1/models":
            return self.reply(200, json.dumps(self.models()).encode(), "application/json")
        return self.reply(404, b"not found", "text/plain")

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
        data = [{"id": name, "object": "model", "owned_by": "calliope-local"}
                for name in LOCAL_MODELS]
        if CALLIOPE_URL:
            for name in remote_model_names():
                data.append({"id": name, "object": "model", "owned_by": "calliope-remote"})
        return {"object": "list", "data": data}

    def do_POST(self):
        if self.path != "/v1/audio/speech":
            return self.reply(404, b"not found", "text/plain")
        # NO BROWSER MAY SPEND THIS. Loopback is not a boundary a web page
        # respects: any site the owner visits can POST JSON here, and with a
        # Calliope server configured that means their GPU, their gateway key
        # and their OpenAI credit. Browsers attach Origin to every cross-site
        # POST and the real clients -- the player, curl, a script -- attach
        # none, so the header's presence is the whole test. No CORS headers are
        # sent anywhere in this file, so nothing can read a reply either.
        if self.headers.get("Origin"):
            return self.refuse(403, "browser_not_allowed",
                               "this server answers local programs, not web pages")
        LAST_REQUEST[0] = time.time()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            model = body.get("model") or "kokoro"
            if model not in LOCAL_MODELS:
                return self.forward(body, model)   # checked there, against the real listing
            if body.get("response_format", "pcm") != "pcm":
                return self.reply(400, b"this server only returns response_format pcm", "text/plain")
            # Decomposed accents ("e" + U+0301) reach espeak as a bare "e": "avó" becomes "avô".
            text = unicodedata.normalize("NFC", body["input"])
            voice = body.get("voice") or "af_heart"
            lang = LANG_BY_VOICE_PREFIX.get(voice[:1], "en-us")
            with LOCK:
                audio, _, spoken = KOKORO.create_timed(text, voice=voice, lang=lang)
                timings = word_timings(text.split(), spoken, lang)
            pcm = (audio.clip(-1.0, 1.0) * 32767).astype("<i2").tobytes()
            headers = {}
            if timings:
                rounded = [[round(start, 3), round(end, 3)] for start, end in timings]
                headers["X-Word-Timings"] = json.dumps(rounded, separators=(",", ":"))
            self.reply(200, pcm, "audio/pcm", headers)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            # 400, NOT 500. Every client mistake used to come back as a server
            # error with a bare text/plain body: {} gave 500 "'input'", a
            # truncated body gave 500 "Expecting value", an unknown voice gave
            # 500 with a KeyError. A caller cannot tell "I sent that wrong"
            # from "the server is broken", and neither can anybody reading a
            # bug report about it.
            self.refuse(400, "invalid_request", str(error) or type(error).__name__)
        except Exception as error:
            print("speech failed: %r" % error, flush=True)
            self.refuse(500, "internal_error", str(error))
        finally:
            LAST_REQUEST[0] = time.time()

    def forward(self, body, model):
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
        """
        known = remote_model_names()
        if model not in known:
            known = remote_model_names(fresh=True)
        if model not in known:
            return self.refuse(404, "no_such_model",
                               "%r is not a model here (%s) or on the Calliope server at %s (%s)"
                               % (model, ", ".join(LOCAL_MODELS), CALLIOPE_URL,
                                  ", ".join(known) or "nothing it would name"))
        if not CALLIOPE_URL:
            return self.refuse(404, "no_such_model",
                               "%r is not one of this machine's models (%s), and no Calliope "
                               "server is configured to ask." % (model, ", ".join(LOCAL_MODELS)))
        request = urllib.request.Request(
            CALLIOPE_URL + "/v1/audio/speech",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json",
                     **({"authorization": "Bearer " + CALLIOPE_KEY} if CALLIOPE_KEY else {})},
            method="POST")
        try:
            # LONGER THAN FEELS REASONABLE, ON PURPOSE. Chatterbox is slower
            # than realtime, so a minute of speech is minutes of compute, and a
            # timeout tuned to a local model would cut off every cloned voice.
            with urllib.request.urlopen(request, timeout=600,
                                        context=tls_for(CALLIOPE_URL)) as answer:
                self.reply(answer.status, answer.read(),
                           answer.headers.get("content-type", "application/octet-stream"))
        except urllib.error.HTTPError as error:
            self.reply(error.code, error.read(), error.headers.get("content-type", "text/plain"))
        except Exception as error:
            self.refuse(503, "backend_unreachable",
                        "%r runs on the Calliope server at %s, which did not answer: %s"
                        % (model, CALLIOPE_URL, error))

    def refuse(self, status, code, message):
        body = json.dumps({"error": {"code": code, "message": message}}).encode()
        self.reply(status, body, "application/json")


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


def main():
    global KOKORO
    try:
        os.setsid()  # leave the player's process group, so stopping playback keeps the model warm
    except PermissionError:
        pass  # already a session/group leader (started by hand)
    # Exit through the interpreter on SIGTERM: phonemizer copies libespeak-ng into a temp
    # directory per process and only removes it on a normal exit.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print("port %d busy; another server is already running" % PORT, flush=True)
        return

    from kokoro_onnx import Kokoro

    started = time.time()
    KOKORO = Kokoro(os.path.join(RUNTIME, "kokoro-v1.0.onnx"),
                    os.path.join(RUNTIME, "voices-v1.0.bin"))
    for voice in ("af_heart", "pf_dora"):
        KOKORO.create("ok.", voice=voice, lang=LANG_BY_VOICE_PREFIX[voice[0]])  # first call per language is slow
    print("model ready in %.2f s on port %d" % (time.time() - started, PORT), flush=True)
    LAST_REQUEST[0] = time.time()
    threading.Thread(target=exit_when_idle, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
