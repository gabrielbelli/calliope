"""calliope: speak, save and transcribe from a terminal, through the Calliope app.

    calliope speak [TEXT... | -f FILE | -] [--markdown|--plain] [--reader] [--voice REF] [--wait]
    calliope save  [TEXT... | -f FILE | -] -o OUT [--voice REF|NAME] [--model M] [--language xx]
    calliope transcribe FILE [--model M] [--language xx] [--json]
    calliope voices [--language xx] [--json]
    calliope status [--test] [--json]

Lives in Calliope.app/Contents/Resources/cli/ beside the `calliope` sh wrapper, which picks the
interpreter. `speak` hands the text to the player and returns; everything else talks to the
proxy the app keeps on 127.0.0.1.

STDLIB ONLY, AND OLD STDLIB. The wrapper prefers the bundle's own Python, then the runtime's
venv, then whatever `python3` is on the PATH -- which on a Mac without either may be the system's
3.9. Nothing here needs more than that.
"""
import argparse
import html
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import urllib.error
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

# Seconds between polls of a long-form job, and how long a launch of the app may take to bring
# the proxy up. Module constants so the tests can shrink them.
POLL_SECONDS = 2.0
LAUNCH_WAIT_SECONDS = 15.0

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


class CalliopeError(Exception):
    """One line for stderr, and exit status 1."""


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


def _error_line(status, body):
    """The proxy's {"error": {"code", "message"}} as one line."""
    try:
        error = json.loads(body.decode("utf-8")).get("error") or {}
        message, code = error.get("message"), error.get("code")
    except (ValueError, AttributeError, UnicodeDecodeError):
        message, code = None, None
    if not message:
        message = body.decode("utf-8", "replace").strip()[:200] or "no details"
        return "HTTP %d: %s" % (status, " ".join(message.split()))
    message = " ".join(str(message).split())
    return "%s (%s)" % (message, code) if code else message


def call(port, method, path, body=None, headers=None, timeout=60):
    """(status, headers, body) for a 2xx; CalliopeError with the proxy's own words otherwise."""
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body,
                                     method=method, headers=headers or {})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        raise CalliopeError(_error_line(exc.code, exc.read()))
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CalliopeError("no answer from Calliope on 127.0.0.1:%d (%s)" % (port, reason))


def get_json(port, path, timeout=60):
    _, _, body = call(port, "GET", path, timeout=timeout)
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CalliopeError("%s did not answer with JSON" % path)


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


# ------------------------------------------------------------------------------- speak --

def stop_current():
    """Stop the player that is speaking now, if there is one -- and only if it is the player."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return
    # A STALE PID FILE NAMES WHATEVER HAS THAT PID NOW. The player writes the file and nothing
    # removes it when the player dies, so the number is checked against the process's own
    # command line before anything is signalled.
    command = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True).stdout
    if PLAYER in command:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


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


# -------------------------------------------------------------------------------- save --

class _Progress:
    """One line on stderr, rewritten in place on a terminal and printed per change otherwise."""

    def __init__(self):
        self.tty = sys.stderr.isatty()
        self.last = None

    def show(self, line):
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


def wait_for_job(port, job):
    """The audio of a long-form job the server answered 202 with, once it is made."""
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
        raise CalliopeError("stopped waiting; job %s carries on on the Calliope server" % job_id)
    progress.done()
    if job["status"] == "failed":
        raise CalliopeError("the Calliope server could not make it: %s"
                            % (job.get("error") or "the job failed"))
    if job["status"] == "cancelled":
        raise CalliopeError("job %s was cancelled on the Calliope server" % job_id)
    _, _, audio = call(port, "GET", "/jobs/%s/audio" % job_id, timeout=600)
    return audio


def cmd_save(args):
    out = args.output
    fmt = os.path.splitext(out)[1].lower().lstrip(".")
    if fmt not in FORMATS:
        raise CalliopeError("cannot tell the format from %r: end it in .%s"
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
    if status == 202:
        audio = wait_for_job(port, json.loads(audio.decode("utf-8")))
    elif "json" in (headers.get("Content-Type") or ""):
        raise CalliopeError("expected audio, got JSON: %s" % audio[:200].decode("utf-8", "replace"))
    # BESIDE ITS NAME, THEN RENAMED, so an interrupted save never leaves a short file under the
    # name somebody is about to play.
    partial = out + ".part"
    try:
        with open(partial, "wb") as f:
            f.write(audio)
        os.replace(partial, out)
    except OSError as exc:
        raise CalliopeError("cannot write %s: %s" % (out, exc.strerror or exc))
    print("Saved %s (%d KB, %s)" % (out, (len(audio) + 1023) // 1024,
                                    "%s on %s" % (voice, model) if voice else model))
    return 0


# -------------------------------------------------------------------------- transcribe --

def _multipart(fields, name, filename, data):
    boundary = "calliope-%s" % uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (boundary, key, value)).encode("utf-8"))
    parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n'
                  % (boundary, name, filename.replace('"', "_"))).encode("utf-8"))
    parts.append(data + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary


def cmd_transcribe(args):
    try:
        with open(args.file, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise CalliopeError("cannot read %s: %s" % (args.file, exc.strerror or exc))
    # THE PREFIX IS STRIPPED HERE because nobody else will: the proxy forwards multipart
    # untouched rather than parse it, so "calliope/whisper-1" would reach the server verbatim.
    model = args.model or "whisper-1"
    if model.startswith("calliope/"):
        model = model[len("calliope/"):]
    fields = {"model": model, "response_format": "json"}
    if args.language:
        fields["language"] = args.language
    body, content_type = _multipart(fields, "file", os.path.basename(args.file), data)
    port = ensure_proxy()
    _, _, answer = call(port, "POST", "/v1/audio/transcriptions", body,
                        {"Content-Type": content_type}, timeout=900)
    try:
        result = json.loads(answer.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        result = {"text": answer.decode("utf-8", "replace")}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print((result.get("text") or "").strip())
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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="calliope", description="Speak, save and transcribe with Calliope.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.required = True

    speak = commands.add_parser("speak", help="read text aloud in the Calliope player")
    _add_input(speak)
    speak.add_argument("--reader", action="store_true", help="open the reader with the text")
    speak.add_argument("--voice", metavar="REF", help="a voice such as mac/af_heart")
    speak.add_argument("--wait", action="store_true", help="return when the speaking ends")
    speak.set_defaults(run=cmd_speak)

    save = commands.add_parser("save", help="write the speech to an audio file")
    _add_input(save)
    save.add_argument("-o", "--output", required=True, metavar="OUT",
                      help="the file; its extension picks the format: " + ", ".join(FORMATS))
    save.add_argument("--voice", metavar="REF|NAME",
                      help="mac/af_heart, calliope/pf_dora, or a bare name")
    save.add_argument("--model", metavar="M",
                      help="a model, such as calliope/<long-form model>")
    save.add_argument("--language", metavar="xx",
                      help="whose default voice to use when --voice is not given (en)")
    save.set_defaults(run=cmd_save)

    transcribe = commands.add_parser("transcribe", help="turn an audio file into text")
    transcribe.add_argument("file", metavar="FILE")
    transcribe.add_argument("--model", metavar="M", help="default whisper-1")
    transcribe.add_argument("--language", metavar="xx")
    transcribe.add_argument("--json", action="store_true")
    transcribe.set_defaults(run=cmd_transcribe)

    voices = commands.add_parser("voices", help="list the voices, by language")
    voices.add_argument("--language", metavar="xx")
    voices.add_argument("--json", action="store_true")
    voices.set_defaults(run=cmd_voices)

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
