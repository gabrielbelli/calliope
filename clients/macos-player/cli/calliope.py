"""calliope: speak, save, transcribe and translate from a terminal, through the Calliope app.

    calliope speak [TEXT... | -f FILE | -] [--markdown|--plain] [--reader] [--voice REF] [--wait]
    calliope stop
    calliope save  [TEXT... | -f FILE | -] -o OUT|- [--format F] [--voice REF|NAME] [--model M]
                   [--language xx]
    calliope transcribe FILE|URL|- [--format F] [--json] [--clip START-END] [--captions]
                        [--model M] [--language xx] [--term T]... [--vocabulary-file F]...
                        [--glossary NAME]... [--boost] [--timestamps word|segment]
    calliope translate FILE|URL|- [--format F] [--json] [--model M] [--term T]...
                       [--vocabulary-file F]... [--glossary NAME]...
    calliope fetch URL [-o FILE|DIR|-] [--video | --captions]
    calliope glossaries [list | show NAME | put NAME -f FILE|- [--force] | rm NAME] [--json]
    calliope jobs [list | show ID | fetch ID [-o OUT|-] | cancel ID | rm ID [--audio]] [--json]
    calliope clips [list | add NAME FILE [--replace] | rm NAME] [--json]
    calliope voices [--language xx] [--json]
    calliope models [--json]
    calliope status [--test] [--json]

Lives in Calliope.app/Contents/Resources/cli/ beside the `calliope` sh wrapper, which picks the
interpreter. `speak` hands the text to the player and returns, and `stop` stops it; everything
else talks to the proxy the app keeps on 127.0.0.1, which adds the Calliope server's key on the
way out. Links (a URL where a file would go) are downloaded by the Calliope server's own
yt-dlp, never on this Mac.

STDLIB ONLY, AND OLD STDLIB. The wrapper prefers the bundle's own Python, then the runtime's
venv, then whatever `python3` is on the PATH -- which on a Mac without either may be the system's
3.9. Nothing here needs more than that.
"""
import argparse
import html
import http.client
import io
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

RUNTIME = os.path.expanduser("~/.local/share/calliope")
QUEUE_DIR = os.path.join(RUNTIME, "queue")
PID_FILE = os.path.join(RUNTIME, "player.pid")
LOG_FILE = os.path.join(RUNTIME, "player.log")

DEFAULTS_DOMAIN = "com.gabrielbelli.calliope-player"
APP_BUNDLE_ID = "com.gabrielbelli.calliope"
DEFAULT_PORT = 47815


def _find_app():
    """Calliope.app, from where this file really is: <APP>/Contents/Resources/cli/calliope.py.

    FROM THE FILE, NOT FROM A CONSTANT, because ~/.local/bin/calliope is a symlink and the app
    may have been installed somewhere other than /Applications (CALLIOPE_APP_DIR). Run straight
    out of the repository there is no app around it, and the installed one is the best guess.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    resources = os.path.dirname(here)
    app = os.path.dirname(os.path.dirname(resources))
    if os.path.basename(resources) == "Resources" and app.endswith(".app"):
        return app
    return "/Applications/Calliope.app"


APP = _find_app()
PLAYER = os.path.join(APP, "Contents/Helpers/CalliopePlayer.app/Contents/MacOS/calliope-player")

# Seconds between polls of a long-form job or a link's download, and how long a launch of the
# app may take to bring the proxy up. Module constants so the tests can shrink them.
POLL_SECONDS = 2.0
LAUNCH_WAIT_SECONDS = 15.0
# How long a transcription or a translation may take to answer. The proxy waits 960 s for the
# server, which waits 900 s for its transcriber -- about two hours of audio -- so this waits a
# little longer than both, and the error that arrives is theirs, saying which of them gave up.
TRANSCRIBE_SECONDS = 1000

LANGUAGES = {"en": "English", "es": "Spanish", "fr": "French", "hi": "Hindi",
             "it": "Italian", "ja": "Japanese", "pt": "Portuguese", "zh": "Chinese"}
# The voice each language gets when nobody has chosen one -- the player's table, repeated here
# because a file written by `save` should sound like the same sentence spoken by `speak`.
BUILT_IN_VOICES = {"en": "af_heart", "es": "ef_dora", "fr": "ff_siwis", "hi": "hf_alpha",
                   "it": "if_sara", "ja": "jf_alpha", "pt": "pf_dora", "zh": "zf_xiaobei"}
# What this Mac answers itself. Everything else is the Calliope server's.
LOCAL_MODELS = {"kokoro", "tts-1", "tts-1-hd"}
FORMATS = ("wav", "mp3", "opus", "aac", "flac", "pcm")
# The proxy wraps local PCM in a WAV header and does nothing else: there is no encoder on this
# side, by design, so an mp3 has to come from the server.
LOCAL_FORMATS = ("wav", "pcm")
FINISHED = ("done", "failed", "cancelled")
# What /v1/audio/transcriptions and /translations answer with (services/stt openai_api.FORMATS).
TRANSCRIPT_FORMATS = ("json", "text", "srt", "vtt", "verbose_json")


class CalliopeError(Exception):
    """One line for stderr, and exit status 1. `code` is the server's or the proxy's, if any."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------- Markdown to speech --
#
# THE READER SHOWS WHAT IS SPOKEN, so this produces text for the eye as well as the ear: the
# paragraph breaks stay, each heading, list item and table row keeps its own line, and only
# what makes no sense aloud goes -- code, URLs, markup. A heading or a list item without its own
# punctuation gets a full stop, or Kokoro runs it into the next line as one breathless sentence.

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INDENTED = re.compile(r"^(?: {4}|\t)")
_QUOTE = re.compile(r"^(?: {0,3}>[ \t]?)+")
_ATX = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(?:=+|-{2,})[ \t]*$")
_HR = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_LIST = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])(?:[ \t]+(.*))?$")
_CHECKBOX = re.compile(r"^\[[ xX]\][ \t]+")
_REFDEF = re.compile(r"^ {0,3}\[(?!\^)[^\]]+\]:[ \t]*<?\S+>?"
                     r"""(?:[ \t]+(?:"[^"]*"|'[^']*'|\([^)]*\)))?[ \t]*$""")
_FOOTDEF = re.compile(r"^ {0,3}\[\^[^\]]+\]:[ \t]*")
_ALIGN = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_COMMENT = re.compile(r"<!--.*?-->", re.S)

_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
_DEST = r"""\([ \t]*<?(?:[^()\s<>]|\([^()\s]*\))*>?(?:[ \t]+(?:"[^"]*"|'[^']*'|\([^)]*\)))?[ \t]*\)"""
_IMAGE = re.compile(r"!\[[^\]]*\](?:" + _DEST + r"|\[[^\]]*\])")
_LINK = re.compile(r"\[([^\]]*)\](?:" + _DEST + r"|\[[^\]]*\])")
_FOOTREF = re.compile(r"\[\^[^\]]+\]")
_AUTOLINK = re.compile(r"<(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\s]*|[^<>\s@]+@[^<>\s@]+)>")
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_BARE_URL = re.compile(r"(?:\b(?:https?|ftp)://|\bwww\.)[^\s<>]*[^\s<>.,;:!?'\")\]]")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_EMPHASIS = (
    re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
    re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"),
    re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"),
    re.compile(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)"),
)
_TERMINAL = re.compile(r"[.!?:;…。！？][\"'”’»)\]]*$")


def _stop(line):
    """A full stop, unless the line already ends a sentence."""
    return line if not line or _TERMINAL.search(line) else line + "."


def _inline(text):
    """One line of Markdown as plain words."""
    kept = []

    def keep(value):
        kept.append(value)
        return "\x00%d\x00" % (len(kept) - 1)

    # Escapes and code spans first, and set aside: `a_b_c` and \*literal\* must reach the
    # emphasis rules as something they cannot touch.
    text = _ESCAPE.sub(lambda m: keep(m.group(1)), text)
    text = _CODE_SPAN.sub(lambda m: keep(m.group(2).strip()), text)
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _FOOTREF.sub("", text)
    text = _AUTOLINK.sub("", text)
    text = _TAG.sub("", text)
    text = _BARE_URL.sub("", text)
    text = _STRIKE.sub(r"\1", text)
    for _ in range(3):  # nested emphasis unwraps from the outside in
        before = text
        for pattern in _EMPHASIS:
            text = pattern.sub(r"\1", text)
        if text == before:
            break
    text = html.unescape(text)
    # What the removals leave behind: "see (https://...)." is "see ()." by now.
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ([,.;:!?])", r"\1", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)
    return text.strip()


def _strip_front_matter(text):
    if text.startswith("---\n"):
        end = re.search(r"^(?:---|\.\.\.)[ \t]*$", text[4:], re.M)
        if end:
            return text[4 + end.end():]
    return text


def _table_lines(lines):
    """{index: "row" | "align"} for every line that is part of a pipe table."""
    kinds = {}
    for k in range(1, len(lines)):
        if ("|" in lines[k] and _ALIGN.match(lines[k])
                and "|" in lines[k - 1] and lines[k - 1].strip() and k - 1 not in kinds):
            kinds[k - 1], kinds[k] = "row", "align"
            j = k + 1
            while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                kinds[j] = "row"
                j += 1
    return kinds


def _cells(line):
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell for cell in (_inline(c) for c in _CELL_SPLIT.split(row)) if cell]


def markdown_to_speech(text):
    """Markdown as something to read aloud: prose, paragraphs intact, nothing unspeakable."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    text = _COMMENT.sub("", _strip_front_matter(text))
    lines = [_QUOTE.sub("", line) for line in text.split("\n")]
    tables = _table_lines(lines)
    out = []
    para = []      # the lines of one paragraph or list item, joined when it ends
    state = {"item": False}

    def flush():
        if para:
            line = _inline(" ".join(para))
            if line:
                out.append(_stop(line) if state["item"] else line)
        del para[:]
        state["item"] = False

    fence = None
    in_list = False
    prev_blank = True
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if fence:
            if re.match(r"^ {0,3}%s{%d,}[ \t]*$" % (re.escape(fence[0]), len(fence)), line):
                fence = None
            continue
        opening = _FENCE.match(line)
        if opening:
            flush()
            out.append("")
            fence = opening.group(1)
            prev_blank = True
            continue
        if not line.strip():
            flush()
            out.append("")
            prev_blank = True
            continue
        # INDENTED CODE ONLY AFTER A BLANK LINE AND OUTSIDE A LIST. Inside one, four spaces are
        # a continuation of the item -- which is how nested lists and their paragraphs look.
        if prev_blank and not in_list and _INDENTED.match(line):
            while i < len(lines) and (not lines[i].strip() or _INDENTED.match(lines[i])):
                i += 1
            out.append("")
            continue
        if in_list and prev_blank and not line[:1].isspace() and not _LIST.match(line):
            in_list = False
        kind = tables.get(i - 1)
        if kind == "align":
            continue
        if kind == "row":
            flush()
            cells = _cells(line)
            if cells:
                out.append(_stop(", ".join(cells)))
            prev_blank = False
            continue
        if para and not state["item"] and _SETEXT.match(line):
            heading = _inline(" ".join(para))
            del para[:]
            if heading:
                out.append(_stop(heading))
            prev_blank = False
            continue
        if _HR.match(line):
            flush()
            out.append("")
            prev_blank = True
            continue
        if _REFDEF.match(line):
            continue
        heading = _ATX.match(line)
        if heading:
            flush()
            words = _inline(heading.group(1) or "")
            if words:
                out.append(_stop(words))
            prev_blank = False
            continue
        item = _LIST.match(line)
        if item:
            flush()
            in_list = True
            state["item"] = True
            words = _CHECKBOX.sub("", (item.group(1) or "").strip())
            line = words
        else:
            footnote = _FOOTDEF.match(line)
            if footnote:
                flush()
                line = line[footnote.end():]
        prev_blank = False
        hard_break = line.endswith("  ") or line.rstrip(" ").endswith("\\")
        words = line.strip()
        if hard_break and words.endswith("\\"):
            words = words[:-1].rstrip()
        if words:
            para.append(words)
        if hard_break:
            flush()
    flush()

    result = []
    for line in out:
        line = line.rstrip()
        if line or (result and result[-1]):
            result.append(line)
    while result and not result[-1]:
        result.pop()
    return "\n".join(result)




# ------------------------------------------------------------------------- settings, proxy --

def read_prefs():
    """The player's settings, or {} when there are none or `defaults` is not there.

    `defaults export` and plistlib rather than `defaults read`: the latter prints an old-style
    plist that is fiddly to parse, and `voices` is a dictionary.
    """
    try:
        result = subprocess.run(["defaults", "export", DEFAULTS_DOMAIN, "-"],
                                capture_output=True, timeout=10)
        prefs = plistlib.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException):
        return {}
    return prefs if isinstance(prefs, dict) else {}


_prefs_cache = []


def prefs():
    if not _prefs_cache:
        _prefs_cache.append(read_prefs())
    return _prefs_cache[0]


def proxy_port():
    value = os.environ.get("CALLIOPE_PORT") or prefs().get("proxyPort") or DEFAULT_PORT
    try:
        return int(value)
    except (TypeError, ValueError):
        raise CalliopeError("CALLIOPE_PORT is not a port number: %r" % value)


# NO HTTP PROXY, EVER. urllib honours http_proxy and the system's proxy settings, and a request
# for 127.0.0.1 sent to a corporate proxy is a request for somebody else's machine.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _refusal(status, body):
    """(one line, code) for a refusal, in whichever of the two shapes it came.

    {"error": {"code", "message"}} is the proxy's and the gateway's. The server's native routes
    -- glossaries, jobs -- keep FastAPI's {"detail": ...}: a sentence, an object with a message
    (a glossary refused line by line, each rejected line listed under it), or a list of
    validation errors. Every one of them is read, because "HTTP 400" is not something anybody
    can act on.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None
    message = code = None
    lines = []
    if isinstance(payload, dict):
        error, detail = payload.get("error"), payload.get("detail")
        if isinstance(error, dict):
            message, code = error.get("message"), error.get("code")
        elif isinstance(detail, str):
            message = detail
        elif isinstance(detail, dict):
            message = detail.get("message")
            for row in detail.get("rejected") or ():
                if isinstance(row, dict):
                    lines.append("  line %s: %s -- %s" % (row.get("line"), row.get("text"),
                                                           row.get("reason")))
        elif isinstance(detail, list):
            message = "; ".join(
                "%s: %s" % (".".join(str(part) for part in (row.get("loc") or ())[1:]) or "body",
                            row.get("msg"))
                for row in detail if isinstance(row, dict)) or None
    if not message:
        text = body.decode("utf-8", "replace").strip()[:200] or "no details"
        return "HTTP %d: %s" % (status, " ".join(text.split())), None
    line = " ".join(str(message).split())
    if code:
        line = "%s (%s)" % (line, code)
    return "\n".join([line] + lines), code


def _open(port, method, path, body=None, headers=None, timeout=60):
    """The proxy's answer, open, for a 2xx; CalliopeError with the proxy's own words otherwise."""
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body,
                                     method=method, headers=headers or {})
    try:
        return _OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            line, code = _refusal(exc.code, exc.read())
        finally:
            exc.close()
        raise CalliopeError(line, code)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CalliopeError("no answer from Calliope on 127.0.0.1:%d (%s)" % (port, reason))


def call(port, method, path, body=None, headers=None, timeout=60):
    """(status, headers, body) for a 2xx; CalliopeError with the proxy's own words otherwise."""
    response = _open(port, method, path, body, headers, timeout)
    try:
        return response.status, response.headers, response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise CalliopeError("the answer to %s %s broke off: %s" % (method, path.split("?")[0], exc))
    finally:
        response.close()


def _json_of(body, path):
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CalliopeError("%s did not answer with JSON" % path.split("?")[0])


def get_json(port, path, timeout=60):
    _, _, body = call(port, "GET", path, timeout=timeout)
    return _json_of(body, path)


def send_json(port, method, path, payload=None, timeout=60):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else None
    _, _, answer = call(port, method, path, body, headers, timeout)
    return _json_of(answer, path)


def _healthy(port):
    try:
        with _OPENER.open("http://127.0.0.1:%d/health" % port, timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def launch_app():
    """Ask macOS to start Calliope in the background; the daemon brings the proxy up."""
    subprocess.run(["open", "-g", "-b", APP_BUNDLE_ID], capture_output=True)


def ensure_proxy():
    """The proxy's port, starting Calliope once if nothing answers there.

    CALLIOPE_NO_LAUNCH=1 never starts anything, which is what the tests run under.
    """
    port = proxy_port()
    if _healthy(port):
        return port
    if os.environ.get("CALLIOPE_NO_LAUNCH", "") not in ("1", "true", "yes"):
        launch_app()
        deadline = time.monotonic() + LAUNCH_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.25)
            if _healthy(port):
                return port
    raise CalliopeError("Calliope is not answering on 127.0.0.1:%d. Open Calliope.app and try "
                        "again." % port)


# ------------------------------------------------------------------------- small helpers --

class _Progress:
    """One line on stderr, rewritten in place on a terminal and printed per change otherwise.

    `tty_only` keeps it off a log or a pipe altogether, for lines that only count seconds.
    """

    def __init__(self, tty_only=False):
        self.tty = sys.stderr.isatty()
        self.silent = tty_only and not self.tty
        self.last = None

    def show(self, line):
        if self.silent:
            return
        if self.tty:
            sys.stderr.write("\r" + line + "\x1b[K")
            sys.stderr.flush()
        elif line.split(",")[0] != self.last:
            sys.stderr.write(line + "\n")
        self.last = line.split(",")[0]

    def done(self):
        if self.tty and self.last is not None:
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()


def _size(count):
    count = count or 0
    if count < 1024 * 1024:
        return "%d KB" % ((count + 1023) // 1024)
    return "%.1f MB" % (count / 1024.0 / 1024.0)


def _clock(seconds):
    whole = int(seconds)
    if whole >= 3600:
        return "%d:%02d:%02d" % (whole // 3600, whole % 3600 // 60, whole % 60)
    return "%d:%02d" % (whole // 60, whole % 60)


def _ago(epoch):
    if not isinstance(epoch, (int, float)):
        return ""
    seconds = max(0, time.time() - epoch)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return "%d min ago" % (seconds // 60)
    if seconds < 86400:
        return "%d h ago" % (seconds // 3600)
    return "%d d ago" % (seconds // 86400)


def waiting(label, work):
    """work(), with "label, N s" counting on a terminal while the answer is on its way.

    IN A THREAD, because the answer to a transcription arrives all at once at the end, and a
    terminal that shows nothing for ten minutes looks exactly like one that has hung.
    """
    box = {}

    def run():
        try:
            box["value"] = work()
        except BaseException as exc:  # handed to the caller's thread below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    progress = _Progress(tty_only=True)
    started = time.monotonic()
    try:
        while thread.is_alive():
            progress.show("%s, %d s" % (label, time.monotonic() - started))
            thread.join(1.0)
    finally:
        progress.done()
    if "error" in box:
        raise box["error"]
    return box["value"]


def save_stream(response, out, what, expected=None):
    """Copy an open answer into `out`, or to standard output for "-"; the bytes written.

    BESIDE ITS NAME, THEN RENAMED, so an interrupted download never leaves a short file under
    the name somebody is about to play. A body shorter than it said it would be is refused for
    the same reason: the proxy closes the connection when the server stops mid-file, and only
    the length says that what arrived is not all of it.
    """
    declared = response.headers.get("Content-Length")
    declared = int(declared) if declared and declared.isdigit() else None
    total = declared or expected
    progress = _Progress(tty_only=True)
    partial = None if out == "-" else out + ".part"
    written = 0
    try:
        sink = sys.stdout.buffer if partial is None else open(partial, "wb")
    except OSError as exc:
        raise CalliopeError("cannot write %s: %s" % (out, exc.strerror or exc))
    try:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            sink.write(chunk)
            written += len(chunk)
            if total:
                progress.show("%s, %s of %s" % (what, _size(written), _size(total)))
        for whole in (declared, expected):
            if whole is not None and written != whole:
                raise CalliopeError("%s broke off after %s of %s"
                                    % (what, _size(written), _size(whole)))
        sink.flush()
        if partial is not None:
            sink.close()
            os.replace(partial, out)
            partial = None
    except (OSError, http.client.HTTPException) as exc:
        raise CalliopeError("%s failed: %s" % (what, getattr(exc, "strerror", None) or exc))
    finally:
        progress.done()
        response.close()
        if partial is not None:
            sink.close()
            try:
                os.remove(partial)
            except OSError:
                pass
    return written


def write_file(out, data):
    """Bytes to `out`, or to standard output for "-": beside the name first, then renamed."""
    if out == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
        return
    partial = out + ".part"
    try:
        with open(partial, "wb") as f:
            f.write(data)
        os.replace(partial, out)
    except OSError as exc:
        raise CalliopeError("cannot write %s: %s" % (out, exc.strerror or exc))


def report(out, line):
    """The one line saying what was written: on stderr when stdout carries the file itself."""
    (sys.stderr if out == "-" else sys.stdout).write(line + "\n")


# ------------------------------------------------------------------------------ voices --

def split_ref(ref):
    """("mac" | "calliope" | None, name) for "mac/af_heart", "calliope/pf_dora" or "af_heart"."""
    origin, slash, name = ref.partition("/")
    if slash and origin in ("mac", "calliope") and name:
        return origin, name
    return None, ref


def default_ref(settings, language):
    """The voice the player would use for `language`: the chosen one, else the built-in."""
    chosen = (settings.get("voices") or {}).get(language)
    if isinstance(chosen, str) and chosen:
        origin, name = split_ref(chosen)
        return "%s/%s" % (origin or "mac", name)
    name = BUILT_IN_VOICES.get(language)
    if not name:
        return None
    return "%s/%s" % ("mac" if settings.get("macOn", True) else "calliope", name)


def voice_detail(port):
    return get_json(port, "/voices").get("detail") or []


def resolve_bare(name, port):
    """A bare voice name as a ref: this Mac's when it has one by that name, else the server's."""
    mac = any(row.get("name") == name and row.get("origin") == "mac" for row in voice_detail(port))
    return "%s/%s" % ("mac" if mac else "calliope", name)


def resolve_voice(args, port):
    """(ref or None, voice name or None, model) for `save`."""
    language = args.language or "en"
    if args.voice:
        ref = args.voice if split_ref(args.voice)[0] else resolve_bare(args.voice, port)
    else:
        ref = default_ref(prefs(), language)
        if ref is None:
            raise CalliopeError("no voice for language %r: give --voice, or one of %s"
                                % (language, ", ".join(sorted(BUILT_IN_VOICES))))
    origin, name = split_ref(ref)
    if args.model:
        model = args.model
        # A long-form model has voices of its own, and a Kokoro name it does not know is an
        # error or, worse, a guess. Left out, the server uses its own default speaker.
        if not args.voice and model.split("/")[-1] not in LOCAL_MODELS:
            return None, None, model
    else:
        model = "kokoro" if origin == "mac" else "calliope/kokoro"
    return ref, name, model


# --------------------------------------------------------------------------- the input --

def gather_text(args):
    """The text to speak or save, converted from Markdown when it is Markdown."""
    markdown = False
    if args.file and args.text:
        raise CalliopeError("give the text or -f FILE, not both")
    if args.file == "-" or args.text == ["-"]:
        text = sys.stdin.read()
    elif args.file:
        try:
            with open(args.file, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as exc:
            raise CalliopeError("cannot read %s: %s" % (args.file, exc.strerror or exc))
        markdown = os.path.splitext(args.file)[1].lower() in (".md", ".markdown")
    elif args.text:
        text = " ".join(args.text)
    else:
        raise CalliopeError("nothing to say: give the text, -f FILE, or - to read standard input")
    if args.plain:
        markdown = False
    elif args.markdown:
        markdown = True
    return markdown_to_speech(text) if markdown else text.strip()


def read_audio(name):
    """(filename, an open file to read, its size) for an audio file, or "-" for standard input.

    OPEN, NOT READ: the multipart body reads it as it is sent. Standard input has no size to
    declare until it has ended, so that one is read whole first.
    """
    if name == "-":
        data = sys.stdin.buffer.read()
        if not data:
            raise CalliopeError("nothing arrived on standard input")
        return "stdin", io.BytesIO(data), len(data)
    try:
        source = open(name, "rb")
        size = os.fstat(source.fileno()).st_size
    except OSError as exc:
        raise CalliopeError("cannot read %s: %s" % (name, exc.strerror or exc))
    return os.path.basename(name), source, size


def is_link(source):
    return re.match(r"https?://", source or "", re.I) is not None


# ------------------------------------------------------------------------------- speak --

def stop_current():
    """Stop the player that is speaking now, if there is one -- and only if it is the player.

    True when a player was signalled.
    """
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    # A STALE PID FILE NAMES WHATEVER HAS THAT PID NOW. The player writes the file and nothing
    # removes it when the player dies, so the number is checked against the process's own
    # command line before anything is signalled.
    command = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True).stdout
    if PLAYER in command:
        try:
            os.killpg(pid, signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError):
            pass
    return False


def cmd_speak(args):
    text = gather_text(args)
    ref = None
    if args.voice:
        ref = args.voice if split_ref(args.voice)[0] else resolve_bare(args.voice, ensure_proxy())
    if not os.access(PLAYER, os.X_OK):
        raise CalliopeError("the player is missing: is Calliope.app installed at %s?" % APP)
    # STOPPED BEFORE THE EMPTY CHECK, as the OpenClip action always has: Speak with nothing
    # selected is how the menu says "stop".
    stop_current()
    if not text:
        raise CalliopeError("No text to speak")
    os.makedirs(QUEUE_DIR, exist_ok=True)
    path = os.path.join(QUEUE_DIR, "%s.txt" % uuid.uuid4())
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    argv = [PLAYER, path]
    if args.reader:
        argv.append("--reader")
    if ref:
        argv += ["--voice", ref]
    # A NEW SESSION, SO A NEW PROCESS GROUP. The player writes its own pid file after setsid(),
    # and the next speak stops it with killpg -- which reaches only a group leader.
    with open(LOG_FILE, "w") as log:
        player = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                  start_new_session=True)
    if not args.wait:
        return 0
    try:
        return 0 if player.wait() == 0 else 1
    except KeyboardInterrupt:
        try:
            os.killpg(player.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        player.wait()
        return 130


def cmd_stop(args):
    print("Stopped." if stop_current() else "Nothing is playing.")
    return 0


# -------------------------------------------------------------------------------- save --

def wait_for_job(port, job):
    """A long-form job the server answered 202 with, once it has finished; failure raises.

    A CANCELLED JOB IS RETURNED, not refused: what it said before it stopped may be kept, and
    whether that is wanted is the caller's question.
    """
    job_id = job.get("id")
    if not job_id:
        raise CalliopeError("the Calliope server answered 202 without a job id")
    started = time.monotonic()
    estimate = job.get("estimated_seconds")
    progress = _Progress()
    try:
        while job.get("status") not in FINISHED:
            line = "job %s: %s" % (job_id[:8], job.get("status") or "queued")
            if job.get("status", "queued") == "queued" and job.get("queued_ahead"):
                line += ", %d ahead" % job["queued_ahead"]
            line += ", %d s" % (time.monotonic() - started)
            if estimate:
                line += " of about %d s" % estimate
            progress.show(line)
            time.sleep(POLL_SECONDS)
            job = get_json(port, "/jobs/%s" % job_id)
            estimate = job.get("estimated_seconds") or estimate
    except KeyboardInterrupt:
        progress.done()
        raise CalliopeError("stopped waiting; job %s carries on on the Calliope server, and "
                            "`calliope jobs fetch %s` collects it" % (job_id, job_id[:8]))
    progress.done()
    if job["status"] == "failed":
        raise CalliopeError("the Calliope server could not make it: %s"
                            % (job.get("error") or "the job failed"))
    return job


def cmd_save(args):
    out = args.output
    named = "" if out == "-" else os.path.splitext(out)[1].lower().lstrip(".")
    if args.format and named and named != args.format:
        raise CalliopeError("%s would hold %s: name it .%s, or leave out --format"
                            % (out, args.format, args.format))
    fmt = args.format or named or ("wav" if out == "-" else "")
    if fmt not in FORMATS:
        raise CalliopeError("cannot tell the format from %r: end it in .%s, or give --format"
                            % (out, ", .".join(FORMATS)))
    text = gather_text(args)
    if not text:
        raise CalliopeError("No text to save")
    port = ensure_proxy()
    _, voice, model = resolve_voice(args, port)
    if model in LOCAL_MODELS and fmt not in LOCAL_FORMATS:
        raise CalliopeError("this Mac's voice writes only .wav or .pcm: save as .wav, or use a "
                            "Calliope server voice (--voice calliope/%s) for .%s"
                            % (voice or BUILT_IN_VOICES["en"], fmt))
    body = {"model": model, "input": text, "response_format": fmt}
    if voice:
        body["voice"] = voice
    status, headers, audio = call(port, "POST", "/v1/audio/speech",
                                  json.dumps(body).encode("utf-8"),
                                  {"Content-Type": "application/json"}, timeout=900)
    described = "%s on %s" % (voice, model) if voice else model
    if status == 202:
        job = _json_of(audio, "/v1/audio/speech")
        # THE ID BEFORE ANYTHING IS WAITED FOR. A long job can take an hour, and a Ctrl-C, a
        # closed lid or a dropped connection must not lose what it is making.
        if job.get("id"):
            sys.stderr.write("job %s is making it on the Calliope server\n" % job["id"])
        job = wait_for_job(port, job)
        if job["status"] == "cancelled":
            raise CalliopeError("job %s was cancelled on the Calliope server" % job["id"])
        written = save_stream(_open(port, "GET", "/jobs/%s/audio" % job["id"], timeout=600),
                              out, "saving job %s" % job["id"][:8])
        report(out, "Saved %s (%s, %s)" % (out, _size(written), described))
        return 0
    if "json" in (headers.get("Content-Type") or ""):
        raise CalliopeError("expected audio, got JSON: %s" % audio[:200].decode("utf-8", "replace"))
    write_file(out, audio)
    report(out, "Saved %s (%s, %s)" % (out, _size(len(audio)), described))
    return 0


# ------------------------------------------------------------------- transcribe, translate --

# What would end a quoted multipart parameter or its line: a quote, a backslash, CR or LF.
_FRAME_BREAKING = re.compile(r'["\\\r\n]')


class Multipart:
    """A multipart/form-data body that reads its file as it is sent, never holding it whole.

    `fields` is a list of (name, value) pairs, because `keywords[]` is sent once per term. The
    length is known before anything is sent -- the framing plus the file's size -- so it goes
    out with a Content-Length, and urllib reads this object in blocks as the socket takes them.
    An hour of wav is 100 MB, which is not something a command line should hold to send.
    """

    def __init__(self, fields, name, filename, source, size):
        boundary = "calliope-%s" % uuid.uuid4().hex
        head = []
        for key, value in fields:
            if "\r" in value or "\n" in value:
                raise CalliopeError("%s cannot contain a line break" % key)
            head.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                         % (boundary, key, value)).encode("utf-8"))
        head.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                     'Content-Type: application/octet-stream\r\n\r\n'
                     % (boundary, name, _FRAME_BREAKING.sub("_", filename))).encode("utf-8"))
        head = b"".join(head)
        tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        self.parts = [io.BytesIO(head), source, io.BytesIO(tail)]
        self.length = len(head) + size + len(tail)
        self.headers = {"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                        "Content-Length": str(self.length)}

    def read(self, size=-1):
        out = b""
        while self.parts and (size is None or size < 0 or len(out) < size):
            piece = self.parts[0].read(-1 if size is None or size < 0 else size - len(out))
            if piece:
                out += piece
            else:
                self.parts.pop(0).close()
        return out

    def close(self):
        for part in self.parts:
            part.close()


def upload(port, path, fields, audio, label):
    """POST an audio file as multipart, waiting as long as a transcription may take."""
    filename, source, size = audio
    body = Multipart(fields, "file", filename, source, size)
    try:
        return waiting(label, lambda: call(port, "POST", path, body, body.headers,
                                           timeout=TRANSCRIBE_SECONDS))
    finally:
        body.close()


# The separators the server splits `prompt` on (services/stt openai_api._TERM_SEPARATORS), so
# `--term "Kubernetes, kubectl"` means here what it would mean there.
_TERM_SEPARATORS = re.compile(r"[,\r\n]+")


def read_vocabulary(path):
    """The terms in a vocabulary file: one per line, blank lines and # lines skipped.

    A GLOSSARY PROFILE'S BARE FORM, AND ONLY THAT. The server's profiles take two line forms and
    a request can carry only one of them: `heard = intended` is a rewrite, and no request field
    can say "rewrite this as that" -- `keywords[]` and `prompt` are bare terms. Sending the
    right-hand side alone would drop the rewrite without a word, which is the silence the server
    refuses by name everywhere else, so the line is refused here with its number instead.

    A COMMENT ONLY AT THE START OF A LINE, as in a profile: `C#` and `F#` are terms.
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CalliopeError("cannot read %s: %s" % (path, getattr(exc, "strerror", None) or exc))
    terms = []
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            raise CalliopeError(
                "%s line %d is a replacement (%s); a request carries terms only. Keep the "
                "intended spelling on a line of its own, or save the rule in a glossary on the "
                "Calliope server and pass --glossary NAME" % (path, number, line))
        terms.append(line)
    return terms


def vocabulary_terms(args):
    """--term and --vocabulary-file as one list, in order, repeats dropped."""
    terms = []
    for value in args.term or ():
        terms += [part.strip() for part in _TERM_SEPARATORS.split(value)]
    for path in args.vocabulary_file or ():
        terms += read_vocabulary(path)
    return [term for term in dict.fromkeys(terms) if term]


def glossary_names(args):
    names = [name.strip() for value in args.glossary or () for name in value.split(",")]
    return [name for name in dict.fromkeys(names) if name]


def vocabulary_fields(args):
    """The multipart fields for --term, --vocabulary-file, --glossary and --boost.

    `keywords[]`, ONE FIELD PER TERM, rather than the terms joined into `prompt`: the server reads
    both as one list (openai_api._terms), but only `keywords[]` keeps a term whole -- a file line
    with a comma in it would be cut in two by `prompt`'s splitting. The bracketed name is what
    openai-python sends, and the server reads it and the bare name alike.

    Nothing is sent when nothing was asked for: absent, the server applies no vocabulary at all,
    which is its own default (STT_GLOSSARY_DEFAULT is empty), and an empty field is not absent.
    """
    terms = vocabulary_terms(args)
    fields = []
    names = glossary_names(args)
    if names:
        fields.append(("glossary", ",".join(names)))
    # BOOST IS PARAKEET'S, AND OPT-IN THERE. Without it a term reaches Parakeet only as a
    # post-decode repair of its own spelling; with it, the decoder is biased towards the term
    # (boosting.py). Whisper takes its terms as hotwords unconditionally and refuses `boost` by
    # name, and the CLI passes that refusal on rather than guessing which engine will answer.
    if args.boost:
        fields.append(("boost", "true"))
    fields += [("keywords[]", term) for term in terms]
    return fields


def parse_clip(value):
    """(start, end) in seconds from "1:30-2:45", "90-165", "1:30-" or "-2:00"; None for open."""
    usage = ("--clip takes START-END in seconds or [h:]m:ss, such as 1:30-2:45, 90-165, "
             "or 1:30- for the rest")
    first, dash, last = value.partition("-")
    if not dash:
        raise CalliopeError(usage)

    def seconds(text):
        text = text.strip()
        if not text:
            return None
        parts = text.split(":")
        if len(parts) > 3:
            raise CalliopeError(usage)
        total = 0.0
        for part in parts:
            try:
                number = float(part)
            except ValueError:
                raise CalliopeError(usage)
            if number < 0 or number != number:
                raise CalliopeError(usage)
            total = total * 60 + number
        return total

    start, end = seconds(first), seconds(last)
    if start is None and end is None:
        raise CalliopeError(usage)
    if end is not None and end <= (start or 0.0):
        raise CalliopeError("the end of --clip must come after its start")
    return start, end


def _terms_header(value):
    """A comma-separated header of percent-encoded terms (stt's X-Glossary-Repaired), as a list."""
    return [urllib.parse.unquote(term.strip()) for term in (value or "").split(",") if term.strip()]


def show_transcript(answer, headers, fmt, as_json):
    """Print a transcription's answer the way it was asked for.

    THE TERMS THE GLOSSARY REWROTE ARE SAID, because a transcript that was quietly corrected
    and one that was not look the same. stt puts them in X-Glossary-Repaired (and what the
    decoder was steered towards in X-Boost-Applied); in JSON they become `repaired` and
    `boosted`, and beside text, srt or vtt they go to stderr so stdout stays the transcript.
    """
    repaired = _terms_header(headers.get("X-Glossary-Repaired"))
    boosted = _terms_header(headers.get("X-Boost-Applied"))
    if fmt in ("json", "verbose_json"):
        try:
            result = json.loads(answer.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            result = {"text": answer.decode("utf-8", "replace")}
        if as_json or fmt == "verbose_json":
            if isinstance(result, dict):
                if repaired:
                    result["repaired"] = repaired
                if boosted:
                    result["boosted"] = boosted
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(((result.get("text") if isinstance(result, dict) else None) or "").strip())
    else:
        text = answer.decode("utf-8", "replace")
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    if repaired:
        sys.stderr.write("Glossary repaired: %s\n" % ", ".join(repaired))
    if boosted:
        sys.stderr.write("Boost applied to: %s\n" % ", ".join(boosted))


def _check_format(args, fmt):
    if args.json and fmt in ("text", "srt", "vtt"):
        raise CalliopeError("--json prints the JSON answer, and --format %s is not JSON: leave "
                            "one of them out" % fmt)


# --------------------------------------------------------------------------- captions --
#
# A link's own captions arrive as the site wrote them, WebVTT or SubRip. They are parsed into
# cues so that either can be printed as the other, or as plain text -- the page does the same
# from the same two formats.

_CUE_TIMES = re.compile(r"((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
                        r"((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})")


def _seconds_of(stamp):
    total = 0.0
    for part in stamp.replace(",", ".").split(":"):
        total = total * 60 + float(part)
    return total


def parse_cues(text):
    """[(start, end, words)] from a .vtt or an .srt, tags and entities resolved."""
    cues = []
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n[ \t]*\n", text):
        lines = block.split("\n")
        for index, line in enumerate(lines):
            times = _CUE_TIMES.search(line)
            if times:
                words = " ".join(html.unescape(re.sub(r"<[^>]*>", "", rest))
                                 for rest in lines[index + 1:])
                words = " ".join(words.split())
                if words:
                    cues.append((_seconds_of(times.group(1)), _seconds_of(times.group(2)), words))
                break
    return cues


def _stamp(seconds, decimal):
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    whole, millis = divmod(millis, 1000)
    return "%02d:%02d:%02d%s%03d" % (hours, minutes, whole, decimal, millis)


def render_cues(cues, fmt):
    if fmt == "srt":
        return "\n".join("%d\n%s --> %s\n%s\n" % (number, _stamp(start, ","), _stamp(end, ","), words)
                         for number, (start, end, words) in enumerate(cues, start=1))
    if fmt == "vtt":
        return "WEBVTT\n\n" + "\n".join("%s --> %s\n%s\n" % (_stamp(start, "."), _stamp(end, "."),
                                                              words)
                                         for start, end, words in cues)
    said = []
    for _, _, words in cues:
        if not said or said[-1] != words:      # a cue repeated across a boundary is said once
            said.append(words)
    return " ".join(said)


def show_captions(payload, fmt, as_json):
    raw, kind = payload.get("text") or "", payload.get("format") or "vtt"
    if fmt == kind:
        sys.stdout.write(raw if raw.endswith("\n") else raw + "\n")
        return
    cues = parse_cues(raw)
    if fmt in ("srt", "vtt"):
        sys.stdout.write(render_cues(cues, fmt))
        return
    prose = render_cues(cues, "text")
    if as_json:
        print(json.dumps({"text": prose, "source": "captions", "format": kind},
                         ensure_ascii=False, indent=2))
    else:
        print(prose)


# ------------------------------------------------------------------------------- links --

class Link:
    """One link on the Calliope server's downloader: resolved, downloaded, then let go.

    THE SERVER DOWNLOADS IT, NOT THIS MAC. yt-dlp runs in a guarded child on the server
    (services/ui/app/fetcher.py), the file lands in its cache, and /ui/fetch streams it straight
    into the transcriber there -- so a two-hour podcast costs this Mac a transcript. The routes
    are the page's own: /ui/resolve says what the link is and downloads nothing, /ui/commit
    starts the download, /ui/progress follows it, and /ui/abandon lets the job go.

    LET GO ON THE WAY OUT, whatever happened. A running download is stopped and a file too big
    for the cache is deleted then rather than kept for an hour; a small one stays cached, so the
    same link a second time is not downloaded twice.
    """

    def __init__(self, port, url):
        self.port, self.url, self.token, self.facts = port, url, None, {}

    def __enter__(self):
        try:
            self.facts = send_json(self.port, "POST", "/ui/resolve", {"url": self.url}, timeout=120)
        except CalliopeError as exc:
            if exc.code == "no_calliope_server":
                raise CalliopeError("a link is downloaded by the Calliope server, and none is "
                                    "configured: Calliope's Settings need its address and a key "
                                    "(%s)" % exc.code, exc.code)
            raise
        self.token = self.facts.get("token") or self.url
        sys.stderr.write(self.describe() + "\n")
        return self

    def __exit__(self, kind, value, traceback):
        try:
            send_json(self.port, "POST", "/ui/abandon", {"token": self.token}, timeout=30)
        except CalliopeError:
            pass
        if kind is KeyboardInterrupt:
            raise CalliopeError("stopped; the Calliope server has let the download go")
        return False

    @property
    def title(self):
        return " ".join(str(self.facts.get("title") or self.url).split())[:70]

    def describe(self):
        bits = []
        if isinstance(self.facts.get("duration"), (int, float)):
            bits.append(_clock(self.facts["duration"]))
        if self.facts.get("bytes"):
            bits.append("about %s of audio" % _size(self.facts["bytes"]))
        return self.title + (" (%s)" % ", ".join(bits) if bits else "")

    def download(self, kind="audio", clip=(None, None)):
        """Start the download and follow it to the end; the last /ui/progress answer."""
        if kind == "captions" and self.facts.get("probed", True) \
                and not self.facts.get("has_subtitles"):
            raise CalliopeError("%s has no captions of its own, only automatic ones or none: "
                                "leave out --captions to transcribe it" % self.title)
        body = {"token": self.token}
        if kind in ("captions", "video"):
            body[kind] = True
        if clip[0] is not None:
            body["clip_start"] = clip[0]
        if clip[1] is not None:
            body["clip_end"] = clip[1]
        send_json(self.port, "POST", "/ui/commit", body)
        progress = _Progress()
        query = "/ui/progress?token=" + urllib.parse.quote(self.token, safe="")
        try:
            while True:
                state = get_json(self.port, query)
                if state.get("ready"):
                    return state
                if state.get("status") == "error" or state.get("where") == "done":
                    raise CalliopeError("the Calliope server could not download it: %s"
                                        % (state.get("error") or "no reason given"))
                if state.get("status") == "downloading":
                    line = "downloading %s" % self.title
                    if isinstance(state.get("percent"), (int, float)):
                        line += ", %d%%" % state["percent"]
                    if state.get("speed"):
                        line += ", %s/s" % _size(state["speed"])
                    if isinstance(state.get("eta"), (int, float)):
                        line += ", about %d s left" % state["eta"]
                else:
                    line = "waiting to download %s" % self.title
                progress.show(line)
                time.sleep(POLL_SECONDS)
        finally:
            progress.done()

    def media(self):
        """The downloaded file, open for reading."""
        return _open(self.port, "GET",
                     "/ui/media?token=" + urllib.parse.quote(self.token, safe=""), timeout=600)

    def captions(self):
        return send_json(self.port, "POST", "/ui/captions", {"token": self.token})


def _destination(output, suggested):
    """Where a download goes: -o as given, a directory with the server's name in it, or that
    name in the current directory."""
    name = os.path.basename((suggested or "").replace("/", "_")).strip() or "download"
    if not output:
        return name
    if output != "-" and os.path.isdir(output):
        return os.path.join(output, name)
    if output != "-":
        given, kept = os.path.splitext(output)[1].lower(), os.path.splitext(name)[1].lower()
        if kept and given != kept:
            sys.stderr.write("note: the file is %s, as the site sent it, whatever its name "
                             "says; nothing here converts it\n" % kept)
    return output


def cmd_transcribe(args):
    fmt = args.format or "json"
    _check_format(args, fmt)
    if args.timestamps and fmt != "verbose_json":
        raise CalliopeError("--timestamps needs --format verbose_json")
    clip = parse_clip(args.clip) if args.clip else (None, None)
    if is_link(args.source):
        return transcribe_link(args, fmt, clip)
    if args.captions:
        raise CalliopeError("--captions takes a link: a file has no captions to fetch")
    model = args.model or "whisper-1"
    # THE PREFIX IS STRIPPED HERE because nobody else will: the proxy forwards multipart
    # untouched rather than parse it, so "calliope/whisper-1" would reach the server verbatim.
    if model.startswith("calliope/"):
        model = model[len("calliope/"):]
    fields = [("model", model), ("response_format", fmt)]
    if args.language:
        fields.append(("language", args.language))
    fields += [("timestamp_granularities[]", grain) for grain in args.timestamps or ()]
    if clip[0] is not None:
        fields.append(("clip_start", repr(clip[0])))
    if clip[1] is not None:
        fields.append(("clip_end", repr(clip[1])))
    fields += vocabulary_fields(args)
    audio = read_audio(args.source)
    port = ensure_proxy()
    _, headers, answer = upload(port, "/v1/audio/transcriptions", fields, audio,
                                "transcribing %s" % audio[0])
    show_transcript(answer, headers, fmt, args.json)
    return 0


def transcribe_link(args, fmt, clip):
    """A link, transcribed where it was downloaded: on the Calliope server.

    WHAT /ui/fetch CANNOT CARRY IS REFUSED BY NAME. The server's link route transcribes with
    its own engine choice and takes the format, the timestamps and named glossaries; a
    language, one-off terms and boost have no field there. Dropping them would hand back a
    transcript made without what was asked for, so the way that does take them is named.
    """
    unsent = [flag for flag, given in (("--model", args.model), ("--language", args.language),
                                       ("--term", args.term),
                                       ("--vocabulary-file", args.vocabulary_file),
                                       ("--boost", args.boost)) if given]
    if unsent:
        raise CalliopeError("a link is transcribed by the Calliope server's link route, which "
                            "takes no %s. Download it first, then transcribe the file: calliope "
                            "fetch URL -o FILE" % ", ".join(unsent))
    if args.captions:
        if fmt == "verbose_json" or args.clip or args.glossary:
            raise CalliopeError("--captions fetches the transcript a person wrote, whole: it "
                                "takes --format text, json, srt or vtt, and no --clip, "
                                "--glossary or --timestamps")
    port = ensure_proxy()
    with Link(port, args.source) as link:
        if args.captions:
            link.download("captions")
            show_captions(link.captions(), fmt, args.json)
            return 0
        link.download("audio", clip)
        query = [("response_format", fmt)]
        query += [("timestamp_granularities", grain) for grain in args.timestamps or ()]
        names = glossary_names(args)
        if names:
            query.append(("glossary", ",".join(names)))
        path = "/ui/fetch?" + urllib.parse.urlencode(query)
        body = json.dumps({"token": link.token}).encode("utf-8")
        _, headers, answer = waiting(
            "transcribing %s" % link.title,
            lambda: call(port, "POST", path, body, {"Content-Type": "application/json"},
                         timeout=TRANSCRIBE_SECONDS))
    show_transcript(answer, headers, fmt, args.json)
    return 0


def cmd_translate(args):
    """Speech in any language, English text out: /v1/audio/translations.

    THE FIELDS ARE THE SERVER'S, AND THEY ARE FEWER. The route takes model, prompt,
    response_format and glossary (TRANSLATION_FIELDS in services/stt openai_api.py) and refuses
    anything else by name -- no language (the target is English), no window, no keywords[].
    So one-off terms travel in `prompt`, which the server splits at commas: a term with a comma
    in it would arrive as two, and is refused here instead.

    A LINK IS DOWNLOADED THROUGH THE SERVER AND SENT BACK. The link route only transcribes, so
    the file comes down from the server's cache and goes up again to be translated.
    """
    fmt = args.format or "json"
    _check_format(args, fmt)
    terms = vocabulary_terms(args)
    split = [term for term in terms if "," in term]
    if split:
        raise CalliopeError("translate sends its terms in `prompt`, which the server splits at "
                            "commas, so %r would arrive as two terms: drop the comma, or put it "
                            "in a glossary and pass --glossary NAME" % split[0])
    model = args.model or "whisper-1"
    if model.startswith("calliope/"):
        model = model[len("calliope/"):]
    fields = [("model", model), ("response_format", fmt)]
    if terms:
        fields.append(("prompt", ", ".join(terms)))
    names = glossary_names(args)
    if names:
        fields.append(("glossary", ",".join(names)))
    if not is_link(args.source):
        audio = read_audio(args.source)
        port = ensure_proxy()
        _, headers, answer = upload(port, "/v1/audio/translations", fields, audio,
                                    "translating %s" % audio[0])
        show_transcript(answer, headers, fmt, args.json)
        return 0
    port = ensure_proxy()
    with Link(port, args.source) as link:
        state = link.download("audio")
        name = state.get("filename") or "link"
        with tempfile.TemporaryDirectory(prefix="calliope-") as scratch:
            copy = os.path.join(scratch, "media" + os.path.splitext(name)[1])
            save_stream(link.media(), copy, "fetching %s" % link.title)
            with open(copy, "rb") as source:
                _, headers, answer = upload(port, "/v1/audio/translations", fields,
                                            (name, source, os.fstat(source.fileno()).st_size),
                                            "translating %s" % link.title)
    show_transcript(answer, headers, fmt, args.json)
    return 0


def cmd_fetch(args):
    """A link's audio -- or its video, or its own captions -- downloaded by the Calliope server."""
    if not is_link(args.url):
        raise CalliopeError("fetch takes a link, http:// or https://")
    kind = "captions" if args.captions else ("video" if args.video else "audio")
    port = ensure_proxy()
    with Link(port, args.url) as link:
        state = link.download(kind)
        if kind == "captions":
            payload = link.captions()
            out = _destination(args.output, payload.get("filename"))
            data = (payload.get("text") or "").encode("utf-8")
            write_file(out, data)
            written = len(data)
        else:
            out = _destination(args.output, state.get("filename"))
            written = save_stream(link.media(), out, "fetching %s" % link.title)
    report(out, "Saved %s (%s, %s)" % (out, _size(written), link.title))
    return 0


# -------------------------------------------------------------------------- glossaries --
#
# STORED VOCABULARY, ON THE SERVER. A glossary is a text file of terms and `heard = intended`
# rules that `transcribe --glossary NAME` selects; these are the server's own /glossaries
# routes, and a key reaches its owner's glossaries and the built-ins. `show` prints the file
# itself, comments and all, so editing one is a round trip: show > file, edit, put -f file.

_GLOSSARY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _glossary_path(name):
    if not name or not _GLOSSARY_NAME.fullmatch(name):
        raise CalliopeError("%r is not a glossary name: up to 64 letters, digits, '.', '_' and "
                            "'-', starting with a letter or a digit" % (name or ""))
    return "/glossaries/" + name


def _one(args, action, count=1):
    if len(args.targets) != count:
        raise CalliopeError("%s %s takes %s" % (args.command, action,
                                                {1: "one name", 2: "a name and a file"}[count]
                                                if args.command != "jobs" else "one job id"))
    return args.targets


def cmd_glossaries(args):
    action = args.action or "list"
    if action == "list" and args.targets:
        raise CalliopeError("glossaries list takes no name: glossaries show NAME prints one")
    if action == "put" and not args.file:
        raise CalliopeError("glossaries put NAME takes the glossary with -f FILE, or -f - to "
                            "read standard input")
    path = None if action == "list" else _glossary_path(_one(args, action)[0])
    text = None
    if action == "put":
        if args.file == "-":
            text = sys.stdin.read()
        else:
            try:
                with open(args.file, encoding="utf-8-sig") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError) as exc:
                raise CalliopeError("cannot read %s: %s"
                                    % (args.file, getattr(exc, "strerror", None) or exc))
    port = ensure_proxy()
    if action == "list":
        listing = get_json(port, "/glossaries")
        if args.json:
            print(json.dumps(listing, ensure_ascii=False, indent=2))
            return 0
        rows = listing.get("glossaries") or []
        width = max([len(str(row.get("name", ""))) for row in rows] + [4])
        for row in rows:
            owner = ("built-in" if row.get("source") == "builtin" else
                     "server" if row.get("owner") == "system" else "yours")
            rules = row.get("replacements") or 0
            print("%-*s  %-8s  %4d terms%s" % (width, row.get("name", "?"), owner,
                                              row.get("terms") or 0,
                                              ", %d of them replacements" % rules if rules else ""))
        if not rows:
            print("No glossaries.")
        print("Used when a request names none: %s" % (", ".join(listing.get("default") or [])
                                                      or "none"))
        if listing.get("writable") is False:
            print("Read-only on this server: %s" % (listing.get("reason") or "no reason given"))
        return 0
    if action == "show":
        profile = get_json(port, path)
        if args.json:
            print(json.dumps(profile, ensure_ascii=False, indent=2))
        else:
            text = profile.get("text") or ""
            sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")
        return 0
    if action == "put":
        _, _, answer = call(port, "PUT", path + ("?force=true" if args.force else ""),
                            text.encode("utf-8"), {"Content-Type": "text/plain; charset=utf-8"})
        saved = _json_of(answer, path)
        if args.json:
            print(json.dumps(saved, ensure_ascii=False, indent=2))
        else:
            rules = saved.get("replacements") or 0
            print("%s glossary %s: %d terms%s." % (
                "Created" if saved.get("created") else "Replaced", saved.get("name"),
                saved.get("terms") or 0, ", %d of them replacements" % rules if rules else ""))
        return 0
    gone = send_json(port, "DELETE", path)
    print("Deleted glossary %s." % gone.get("name", args.targets[0]))
    return 0


# -------------------------------------------------------------------------------- jobs --
#
# THE LONG-FORM LANE'S JOBS, AND THE RUN RECORDS BESIDE THEM. A long-form voice answers with a
# job rather than audio; `save` waits for it, and these are how one is found again after the
# wait was cut short, collected, called off or cleared away. A key sees its owner's jobs only.

_JOB_ID = re.compile(r"[A-Za-z0-9_-]+")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def find_job(port, given):
    """A job's full id from the whole of it or from the start of it, as `jobs` prints it."""
    if not _JOB_ID.fullmatch(given or ""):
        raise CalliopeError("%r is not a job id" % (given or ""))
    if _UUID.fullmatch(given):
        return given
    listing = get_json(port, "/jobs?limit=200")
    found = [str(job.get("id")) for job in listing.get("jobs") or []
             if str(job.get("id", "")).startswith(given)]
    if len(found) > 1:
        raise CalliopeError("%s is the start of %d jobs; give more of the id" % (given, len(found)))
    return found[0] if found else given


def _audio_text(job):
    audio = job.get("audio") or {}
    state = audio.get("state")
    if state == "present":
        return "%s %s" % (audio.get("format") or "audio", _size(audio.get("bytes")))
    return {"never": "no audio kept", "pending": "", None: ""}.get(state, "audio %s" % state)


def _job_line(job, width):
    line = "%-8s  %-9s  %-10s  %-10s  %-10s  %-14s  %s" % (
        str(job.get("id", ""))[:8], job.get("status", ""), job.get("kind", ""),
        job.get("engine") or "", _ago(job.get("created_at")), _audio_text(job),
        job.get("text_preview") or "")
    return line.rstrip()[:width]


def cmd_jobs(args):
    action = args.action or "list"
    port = ensure_proxy()
    if action == "list":
        if args.targets:
            raise CalliopeError("jobs list takes no id: jobs show ID prints one")
        query = [("limit", str(args.limit))]
        query += [(name, value) for name, value in (("kind", args.kind), ("status", args.status))
                  if value]
        listing = get_json(port, "/jobs?" + urllib.parse.urlencode(query))
        if args.json:
            print(json.dumps(listing, ensure_ascii=False, indent=2))
            return 0
        jobs = listing.get("jobs") or []
        width = max(60, shutil.get_terminal_size((120, 24)).columns)
        for job in jobs:
            print(_job_line(job, width))
        if not jobs:
            print("No jobs.")
        elif listing.get("truncated"):
            print("(more than %d: --limit N shows more)" % args.limit)
        return 0
    job_id = find_job(port, _one(args, action)[0])
    job = get_json(port, "/jobs/" + job_id)
    if action == "show":
        if args.json:
            print(json.dumps(job, ensure_ascii=False, indent=2))
            return 0
        rows = [("status", "%s, %s" % (job.get("status"), _ago(job.get("created_at")))),
                ("kind", ", ".join(str(part) for part in (job.get("kind"), job.get("engine"))
                                   if part)),
                ("voice", job.get("voice")), ("audio", _audio_text(job)),
                ("error", job.get("error"))]
        print("Job %s" % job.get("id", job_id))
        for name, value in rows:
            if value:
                print("  %-7s %s" % (name, value))
        if job.get("text"):
            print(textwrap.fill(" ".join(str(job["text"]).split()), width=78,
                                initial_indent="  text    ", subsequent_indent=" " * 10))
        return 0
    if action == "fetch":
        if job.get("status") not in FINISHED:
            job = wait_for_job(port, job)
        audio = job.get("audio") or {}
        if audio.get("state") != "present":
            raise CalliopeError("job %s has no audio to fetch: %s" % (
                job_id[:8], {"deleted": "it was deleted", "expired": "it has expired",
                             "never": "it is a %s run, which keeps none" % job.get("kind"),
                             }.get(audio.get("state"), "it is %s" % job.get("status"))))
        out = args.output or "%s.%s" % (job_id, audio.get("format") or job.get("format") or "audio")
        written = save_stream(_open(port, "GET", "/jobs/%s/audio" % job_id, timeout=600), out,
                              "fetching job %s" % job_id[:8], expected=audio.get("bytes"))
        report(out, "Saved %s (%s, job %s)" % (out, _size(written), job_id[:8]))
        return 0
    live = job.get("status") not in FINISHED
    if action == "cancel":
        if not live:
            raise CalliopeError("job %s has already finished (%s); `calliope jobs rm %s` deletes "
                                "it" % (job_id[:8], job.get("status"), job_id[:8]))
        send_json(port, "DELETE", "/jobs/" + job_id)
        print("Cancelling job %s: it stops at the end of the sentence it is on." % job_id[:8])
        return 0
    if live:
        raise CalliopeError("job %s is %s; `calliope jobs cancel %s` stops it first"
                            % (job_id[:8], job.get("status"), job_id[:8]))
    if args.audio:
        send_json(port, "DELETE", "/jobs/%s/audio" % job_id)
        print("Deleted the audio of job %s; its record stays." % job_id[:8])
    else:
        send_json(port, "DELETE", "/jobs/" + job_id)
        print("Deleted job %s." % job_id[:8])
    return 0


# ------------------------------------------------------------------------------- clips --
#
# YOUR VOICE, FOR A CLONING ENGINE. A clip saved here is a voice name on the server's long-form
# lane, usable by its owner alone: `calliope save --model calliope/<long-form model> --voice
# NAME`. The server names it from what was typed -- lowercase letters, digits, - and _ -- and
# takes .wav, .flac, .mp3, .ogg, .m4a or .opus; a wav over 30 s is trimmed there.

def _clip_slug(name):
    """The name as the server stores it (services/ui clips.slug), so rm finds what add saved."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")[:48].rstrip("-")
    if not slug:
        raise CalliopeError("give the voice a name: letters, digits, - and _")
    return slug


def cmd_clips(args):
    action = args.action or "list"
    if action == "list" and args.targets:
        raise CalliopeError("clips list takes no name")
    audio = None
    if action == "add":
        name, path = _one(args, action, 2)
        audio = read_audio(path)
    elif action == "rm":
        name = _clip_slug(_one(args, action)[0])
    port = ensure_proxy()
    if action == "list":
        listing = get_json(port, "/ui/clips")
        if args.json:
            print(json.dumps(listing, ensure_ascii=False, indent=2))
            return 0
        clips = listing.get("voices") or []
        width = max([len(str(clip.get("name", ""))) for clip in clips] + [4])
        for clip in clips:
            seconds = clip.get("seconds")
            print("%-*s  %8s  %8s  %s" % (width, clip.get("name", "?"),
                                          "%.1f s" % seconds if isinstance(seconds, (int, float))
                                          else "", _size(clip.get("bytes")),
                                          _ago(clip.get("modified"))))
        if not clips:
            print("No voice clips.")
        if listing.get("writable") is False:
            print("Saving voice clips is switched off on this server.")
        return 0
    if action == "add":
        fields = [("name", name)] + ([("replace", "true")] if args.replace else [])
        _, _, answer = upload(port, "/ui/clips", fields, audio, "saving %s" % audio[0])
        voice = (_json_of(answer, "/ui/clips").get("voice") or {})
        seconds = voice.get("seconds")
        print("Saved voice clip %s%s." % (voice.get("name", name),
                                          " (%.1f s)" % seconds
                                          if isinstance(seconds, (int, float)) else ""))
        return 0
    gone = send_json(port, "DELETE", "/ui/clips/" + name)
    print("Deleted voice clip %s." % gone.get("deleted", name))
    return 0


# ------------------------------------------------------------------------------ voices --

def cmd_voices(args):
    port = ensure_proxy()
    detail = voice_detail(port)
    if args.language:
        detail = [row for row in detail if row.get("language") == args.language]
    if args.json:
        print(json.dumps(detail, ensure_ascii=False, indent=2))
        return 0
    if not detail:
        print("No voices%s." % (" for %r" % args.language if args.language else ""))
        return 0
    order = list(LANGUAGES) + sorted({r.get("language") or "" for r in detail} - set(LANGUAGES))
    width = max(40, min(100, shutil.get_terminal_size((80, 24)).columns))
    blocks = []
    for language in order:
        rows = [r for r in detail if (r.get("language") or "") == language]
        if not rows:
            continue
        title = "%s  %s" % (language, LANGUAGES[language]) if language in LANGUAGES else \
            (language or "other")
        preferred = default_ref(prefs(), language) if language in LANGUAGES else None
        lines = [title + ("  (default %s)" % preferred if preferred else "")]
        for origin in ("mac", "calliope"):
            names = sorted(r.get("name", "") for r in rows if r.get("origin") == origin)
            if names:
                lines.append(textwrap.fill(" ".join(names), width=width,
                                           initial_indent="  %-9s " % origin,
                                           subsequent_indent=" " * 12))
        blocks.append("\n".join(lines))
    print("\n\n".join(blocks))
    return 0


# ------------------------------------------------------------------------------ models --

# What each of the server's own owners does, as the proxy passes it on in `served_by`.
SERVED_BY = (("tts-stack", "speech"), ("tts-long", "long-form"),
             ("stt-stack", "transcription"))


def cmd_models(args):
    port = ensure_proxy()
    listing = get_json(port, "/v1/models")
    if args.json:
        print(json.dumps(listing, ensure_ascii=False, indent=2))
        return 0
    rows = [row for row in listing.get("data") or [] if isinstance(row, dict)]
    width = max(40, min(100, shutil.get_terminal_size((80, 24)).columns))

    def line(label, names):
        return textwrap.fill(" ".join(names), width=width, initial_indent="%-16s" % label,
                             subsequent_indent=" " * 16)

    local = [row["id"] for row in rows if row.get("owned_by") == "calliope-local"]
    remote = [row for row in rows if row.get("owned_by") == "calliope-remote"]
    lines = [line("This Mac", local) if local else "This Mac        off"]
    if not remote:
        lines.append("Calliope server none configured, or nothing listed")
    else:
        lines.append("Calliope server")
        known = dict(SERVED_BY)
        for owner, label in SERVED_BY:
            names = [row["id"] for row in remote if row.get("served_by") == owner]
            if names:
                lines.append(line("  " + label, names))
        other = [row["id"] for row in remote if row.get("served_by") not in known]
        if other:
            lines.append(line("  other", other))
        if any(row.get("served_by") == "tts-long" for row in remote):
            lines.append("A long-form model answers with a job: `calliope save` waits for it.")
    print("\n".join(lines))
    return 0


# ------------------------------------------------------------------------------ status --

def _describe(status, port):
    mac = status.get("mac") or {}
    server = status.get("calliope") or {}
    if not mac.get("on"):
        mac_line = "off"
    elif mac.get("loaded"):
        mac_line = "on, model loaded"
    else:
        mac_line = "on, model not loaded (loads on first use)"
    if mac.get("on") and mac.get("keep_loaded"):
        mac_line += ", kept loaded"
    elif mac.get("on") and mac.get("idle_seconds"):
        mac_line += ", unloads after %s s idle" % mac["idle_seconds"]
    server_line = ("on, %s" % server.get("url")) if server.get("on") and server.get("url") \
        else ("on, no address" if server.get("on") else "off")
    return ["Calliope on 127.0.0.1:%s" % status.get("port", port),
            "  This Mac  %s" % mac_line,
            "  Server    %s" % server_line]


def cmd_status(args):
    port = ensure_proxy()
    status = get_json(port, "/status")
    test = get_json(port, "/calliope/test", timeout=120) if args.test else None
    if args.json:
        print(json.dumps({"status": status, "test": test} if args.test else status, indent=2))
    else:
        lines = _describe(status, port)
        if test is not None:
            lines.append("")
            for check in test.get("checks") or []:
                ms = check.get("ms")
                lines.append("  %-7s %-4s %6s  %s" % (
                    check.get("name", "?"), "ok" if check.get("ok") else "FAIL",
                    "%d ms" % ms if isinstance(ms, (int, float)) else "",
                    check.get("detail") or ""))
            lines.append("Server test: %s" % (test.get("message")
                                               or ("ok" if test.get("ok") else "failed")))
        print("\n".join(line.rstrip() for line in lines))
    return 1 if test is not None and not test.get("ok") else 0


# ------------------------------------------------------------------------------- main --

def _add_input(parser):
    parser.add_argument("text", nargs="*", metavar="TEXT",
                        help="the text; - reads standard input")
    parser.add_argument("-f", "--file", metavar="FILE",
                        help="read the text from FILE (- for standard input); .md is Markdown")
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument("--markdown", action="store_true", help="treat the text as Markdown")
    kind.add_argument("--plain", action="store_true", help="never treat the text as Markdown")


def _add_vocabulary(parser, boost=True):
    parser.add_argument("--term", action="append", metavar="TERM",
                        help="a word or name to listen for; repeat it, or separate several "
                             "with commas")
    parser.add_argument("--vocabulary-file", action="append", metavar="FILE",
                        help="terms, one per line; blank lines and lines starting with # "
                             "are skipped")
    parser.add_argument("--glossary", action="append", metavar="NAME",
                        help="a glossary saved on the Calliope server, such as tech, "
                             "dictation or one of yours; repeat it, or separate with commas")
    if boost:
        parser.add_argument("--boost", action="store_true",
                            help="also steer Parakeet's decoder towards the terms (Whisper "
                                 "always does, and refuses the flag)")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="calliope",
        description="Speak, save, transcribe and translate with Calliope. This Mac speaks with "
                    "its own voices; transcription, links, glossaries, jobs and voice clips "
                    "need the Calliope server.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.required = True

    speak = commands.add_parser("speak", help="read text aloud in the Calliope player")
    _add_input(speak)
    speak.add_argument("--reader", action="store_true", help="open the reader with the text")
    speak.add_argument("--voice", metavar="REF", help="a voice such as mac/af_heart")
    speak.add_argument("--wait", action="store_true", help="return when the speaking ends")
    speak.set_defaults(run=cmd_speak)

    stop = commands.add_parser("stop", help="stop the player")
    stop.set_defaults(run=cmd_stop)

    save = commands.add_parser("save", help="write the speech to an audio file")
    _add_input(save)
    save.add_argument("-o", "--output", required=True, metavar="OUT",
                      help="the file, or - for standard output; its extension picks the "
                           "format: " + ", ".join(FORMATS))
    save.add_argument("--format", choices=FORMATS, help="the format, for -o - (default wav)")
    save.add_argument("--voice", metavar="REF|NAME",
                      help="mac/af_heart, calliope/pf_dora, or a bare name")
    save.add_argument("--model", metavar="M",
                      help="a model, such as calliope/<long-form model>")
    save.add_argument("--language", metavar="xx",
                      help="whose default voice to use when --voice is not given (en)")
    save.set_defaults(run=cmd_save)

    transcribe = commands.add_parser(
        "transcribe", help="turn audio, a file or a link, into text",
        description="Transcribe a file, standard input (-), or a link, which the Calliope "
                    "server downloads and transcribes itself.")
    transcribe.add_argument("source", metavar="FILE|URL|-")
    transcribe.add_argument("--format", choices=TRANSCRIPT_FORMATS,
                            help="what to print (json prints the text; default json)")
    transcribe.add_argument("--json", action="store_true", help="print the JSON answer")
    transcribe.add_argument("--clip", metavar="START-END",
                            help="only this part, such as 1:30-2:45 or 90-165")
    transcribe.add_argument("--captions", action="store_true",
                            help="a link's own captions, written by a person, instead")
    transcribe.add_argument("--timestamps", action="append", choices=("word", "segment"),
                            help="with --format verbose_json")
    transcribe.add_argument("--model", metavar="M", help="default whisper-1 (files only)")
    transcribe.add_argument("--language", metavar="xx", help="files only")
    _add_vocabulary(transcribe)
    transcribe.set_defaults(run=cmd_transcribe)

    translate = commands.add_parser("translate", help="turn speech in any language into English")
    translate.add_argument("source", metavar="FILE|URL|-")
    translate.add_argument("--format", choices=TRANSCRIPT_FORMATS,
                           help="what to print (json prints the text; default json)")
    translate.add_argument("--json", action="store_true", help="print the JSON answer")
    translate.add_argument("--model", metavar="M", help="default whisper-1")
    _add_vocabulary(translate, boost=False)
    translate.set_defaults(run=cmd_translate)

    fetch = commands.add_parser("fetch", help="download a link's audio through the Calliope "
                                              "server")
    fetch.add_argument("url", metavar="URL")
    fetch.add_argument("-o", "--output", metavar="FILE|DIR|-",
                       help="where to put it (default: its title, here)")
    what = fetch.add_mutually_exclusive_group()
    what.add_argument("--video", action="store_true", help="keep the picture too")
    what.add_argument("--captions", action="store_true", help="its own captions instead")
    fetch.set_defaults(run=cmd_fetch)

    glossaries = commands.add_parser(
        "glossaries", help="your stored vocabulary on the Calliope server",
        description="list | show NAME | put NAME -f FILE | rm NAME")
    glossaries.add_argument("action", nargs="?", choices=("list", "show", "put", "rm"))
    glossaries.add_argument("targets", nargs="*", metavar="NAME")
    glossaries.add_argument("-f", "--file", metavar="FILE", help="for put; - reads standard input")
    glossaries.add_argument("--force", action="store_true",
                            help="for put: keep a rule the server would refuse as risky")
    glossaries.add_argument("--json", action="store_true")
    glossaries.set_defaults(run=cmd_glossaries)

    jobs = commands.add_parser(
        "jobs", help="long-form jobs and run records on the Calliope server",
        description="list | show ID | fetch ID [-o OUT] | cancel ID | rm ID [--audio]. An ID "
                    "may be the start of one, as list prints it.")
    jobs.add_argument("action", nargs="?", choices=("list", "show", "fetch", "cancel", "rm"))
    jobs.add_argument("targets", nargs="*", metavar="ID")
    jobs.add_argument("-o", "--output", metavar="OUT", help="for fetch; - for standard output")
    jobs.add_argument("--audio", action="store_true", help="for rm: the audio only, not the record")
    jobs.add_argument("--kind", metavar="K", help="for list: clone, speech or transcribe")
    jobs.add_argument("--status", metavar="S",
                      help="for list: queued, running, done, failed, cancelled or live")
    jobs.add_argument("--limit", type=int, default=20, metavar="N", help="for list (20)")
    jobs.add_argument("--json", action="store_true")
    jobs.set_defaults(run=cmd_jobs)

    clips = commands.add_parser(
        "clips", help="your voice clips for cloning, on the Calliope server",
        description="list | add NAME FILE [--replace] | rm NAME")
    clips.add_argument("action", nargs="?", choices=("list", "add", "rm"))
    clips.add_argument("targets", nargs="*", metavar="NAME [FILE]")
    clips.add_argument("--replace", action="store_true", help="for add: over one of that name")
    clips.add_argument("--json", action="store_true")
    clips.set_defaults(run=cmd_clips)

    voices = commands.add_parser("voices", help="list the voices, by language")
    voices.add_argument("--language", metavar="xx")
    voices.add_argument("--json", action="store_true")
    voices.set_defaults(run=cmd_voices)

    models = commands.add_parser("models", help="list the models, and what each is for")
    models.add_argument("--json", action="store_true")
    models.set_defaults(run=cmd_models)

    status = commands.add_parser("status", help="what is running, and where")
    status.add_argument("--test", action="store_true", help="also test the Calliope server")
    status.add_argument("--json", action="store_true")
    status.set_defaults(run=cmd_status)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except CalliopeError as exc:
        sys.stderr.write("calliope: %s\n" % exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
