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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 47815
IDLE_SECONDS = 15 * 60
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
        if self.path != "/health":
            return self.reply(404, b"not found", "text/plain")
        self.reply(200, b"ok", "text/plain")

    def do_POST(self):
        if self.path != "/v1/audio/speech":
            return self.reply(404, b"not found", "text/plain")
        LAST_REQUEST[0] = time.time()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
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
        except Exception as error:
            print("speech failed: %r" % error, flush=True)
            self.reply(500, str(error).encode(), "text/plain")
        finally:
            LAST_REQUEST[0] = time.time()


def exit_when_idle(server):
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
    KOKORO = Kokoro(os.path.join(HERE, "kokoro-v1.0.onnx"), os.path.join(HERE, "voices-v1.0.bin"))
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
