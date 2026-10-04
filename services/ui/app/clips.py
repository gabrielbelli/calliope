"""The reference-clip store: what "use my own voice" actually writes.

WHAT A VOICE IS, ON THIS STACK. Chatterbox has no named voices; it clones from
a reference clip, and `generate(audio_prompt_path=...)` takes a FILESYSTEM
PATH. tts-long turns that into a registry -- TTS_VOICE_DIR/narrator.wav is the
voice "narrator" -- and there is no per-request reference field on the wire at
any layer: `job["reference"]` is filled only from Registry.resolve() and is
explicitly stripped from every /jobs response (main.py:563). So making a clip
usable means putting a file in that directory, and nothing else.

WHY THE STORE LIVES HERE AND NOT IN tts-long. Two reasons, and the second is
the load-bearing one.

  * This is where the multipart already arrives. The browser is the transcoder
    (see below), so by the time bytes reach a server they are already a WAV of
    known duration, and the work left is a sanitise-write-validate-unlink
    sequence with no audio library in it.
  * tts-long is a 6.5 GB image with torch in it. Adding python-multipart, an
    upload route and a delete route to the process that holds Chatterbox, to
    serve a browser feature, puts a write path into the heaviest and slowest-
    to-rebuild container in the estate. A named volume costs nothing and this
    service is the only writer: tts-long mounts the same volume READ-ONLY.

The one change tts-long does need is small and is justified on its own terms
rather than by this feature: its registry scanned the directory once at start,
so a file copied in by ANY means -- this service, scp, a TrueNAS share --
needed a container restart to be seen. It now rescans when the directory's
mtime changes. See services/tts-long/app/voices.py.

THE SEQUENCE IS devnen/Chatterbox-TTS-Server's, MIT, server.py:670-753,
transplanted in shape rather than in code: sanitise the name, enforce an
extension allowlist, write, validate the duration against a ceiling, unlink on
failure, and answer with per-item errors alongside the refreshed list. Three
things are added that upstream lacks and that matter once it sits behind a
gateway: a size cap, a collision policy, and a name derived from a whitelist
rather than from the uploaded filename -- upstream's `sanitize_filename` is the
only thing between a caller and path traversal, and "the only thing" is not
where a write path belongs.

WHY WAV ONLY, when tts-long's registry accepts .wav .flac .mp3 .ogg .m4a
.opus. Because the browser transcodes everything to WAV before it is sent:
MediaRecorder produces `audio/webm;codecs=opus` on Chrome and Firefox and
`audio/mp4` on Safari, neither of which is safely in that set, and a webm
renamed .ogg does not decode. AudioContext.decodeAudioData ->
OfflineAudioContext at 24 kHz mono -> a hand-written WAV header is about fifty
lines of vanilla JS, needs no dependency on either side, and means the server
receives one format it is certain to read -- which is also what lets the
validation below be the stdlib `wave` module rather than librosa.

A VOICE IS PERSONAL DATA, SO EACH PERSON HAS A DIRECTORY (D35). A person's
clips live in `users/<user id>/` and the top level is the SYSTEM namespace,
which keeps every clip that existed before ownership did. `owner` below is a
user ID or None for the system, and every path is built from a validated ID:
nothing a caller types reaches a path join but the whitelisted stem.
tts-long resolves a voice in the requesting user's directory only, so a clip
here is usable by the person who saved it and nobody else.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import wave
from pathlib import Path

from voice_common.identity import Claims
from voice_common.scopes import USER_ID

from . import config

log = logging.getLogger("voice-ui.clips")

__all__ = ["ClipError", "SUFFIX", "SUFFIXES", "USERS", "slug", "owner_of",
           "listing", "listing_all", "save", "remove", "writable"]

# The directory under the store that holds one subdirectory per person.
USERS = "users"

SUFFIX = ".wav"

# EVERY FORMAT tts-long CAN READ, copied from its voices.py _SUFFIXES. That
# service loads a reference clip with librosa, which handles all of these; this
# one only ever WROTE .wav, so a WhatsApp voice note -- ogg/opus -- had to be
# converted in the browser before it could be stored at all.
#
# That made the browser a hard dependency for format support, and it is not a
# reliable one: an ogg/opus file from a phone often carries no duration in its
# container, and decodeAudioData handled one by returning a single second of
# audio and no error. A 1.0 s reference clip is useless and looked like a
# successful upload.
#
# So the original bytes are stored when the browser cannot make sense of them,
# and tts-long decodes what it was always able to decode. Kept in step with
# that list by a test; a format here that it cannot read would be a clip that
# saves and then fails at generation time.
SUFFIXES = (".wav", ".flac", ".mp3", ".ogg", ".m4a", ".opus")

# tts-long's own built-in speaker. A clip wins over an alias in
# Registry.resolve, which is deliberate and lets someone give `alloy` a real
# voice -- but a clip named `default` would shadow the built-in itself, which
# is the one name that has to keep meaning what it means.
RESERVED = frozenset({"default"})

# The filename is built from this and nothing else. Not from the uploaded
# filename, not from a sanitiser applied to it: a whitelist that cannot express
# "/", "..", a NUL or a leading dot cannot be tricked into expressing them.
_SAFE = re.compile(r"[^a-z0-9_-]+")


class ClipError(ValueError):
    """Rejected. The message is shown to the user verbatim."""


def slug(name: str) -> str:
    """A voice name from whatever the user typed.

    Lowercased because the registry keys on the file stem and a filesystem that
    is case-insensitive (macOS, SMB) would otherwise make Narrator and narrator
    the same voice on one host and two on another.
    """
    text = _SAFE.sub("-", (name or "").strip().lower()).strip("-")
    if not text:
        raise ClipError("give the voice a name -- letters, digits, - and _")
    if len(text) > 48:
        text = text[:48].rstrip("-")
    if text in RESERVED:
        raise ClipError(
            f"{text!r} is the name of Chatterbox's own built-in speaker; "
            "pick another")
    return text


def _dir() -> Path:
    return Path(config.VOICE_DIR)


def owner_of(claims: Claims) -> str | None:
    """The namespace a caller's OWN clips live in: a person's ID, or the system's for a service.

    A service owns system data everywhere in this stack (D31, D32), and holds
    no voice scope anyway; giving it a namespace of its own would invent one.
    """
    return claims.sub if claims.kind == "user" else None


def _namespace(owner: str | None) -> Path:
    """One owner's directory: users/<id>/ for a person, the top level for the system."""
    if owner is None:
        return _dir()
    if not isinstance(owner, str) or not USER_ID.fullmatch(owner):
        # Never a path join on an unchecked value (D32), even one the
        # assertion vouched for: this is the line that would carry `../`.
        raise ClipError("that is not a user ID")
    return _dir() / USERS / owner


def _clips(directory: Path, owner: str | None) -> list[dict[str, object]]:
    if not directory.is_dir():
        return []
    out: list[dict[str, object]] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file() or entry.suffix.lower() not in SUFFIXES:
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        out.append({"name": entry.stem, "owner": owner, "bytes": stat.st_size,
                    "modified": stat.st_mtime,
                    "seconds": _duration(entry)})
    return out


def _newest_first(found: list[dict[str, object]]) -> list[dict[str, object]]:
    found.sort(key=lambda clip: -float(clip["modified"]))  # type: ignore[arg-type]
    return found


def listing(owner: str | None) -> list[dict[str, object]]:
    """One owner's clips, newest first. Never raises.

    A missing or unwritable directory is a configuration state, not an error:
    it means no voices, the picker shows none, and the page says why. Raising
    would take the whole Speak tab down over a volume that was not mounted.
    `owner` is None for the system namespace; each row carries it, so a list
    that mixes namespaces still says whose each clip is.
    """
    return _newest_first(_clips(_namespace(owner), owner))


def listing_all() -> list[dict[str, object]]:
    """Every clip in every namespace, for a holder of `voices:write:all`.

    A subdirectory of users/ whose name is not a user ID is skipped rather
    than listed: nothing this service writes is named that way, so it is
    something else's, and its name would not survive the delete route's check.
    """
    found = _clips(_dir(), None)
    people = _dir() / USERS
    if people.is_dir():
        for entry in sorted(people.iterdir()):
            if entry.is_dir() and USER_ID.fullmatch(entry.name):
                found += _clips(entry, entry.name)
    return _newest_first(found)


def _trim(path: Path, seconds: float) -> float | None:
    """Cut a WAV down to `seconds` in place. Returns the new length.

    WHY THIS EXISTS RATHER THAN A REJECTION. yt-dlp's clip trimming seeks to
    KEYFRAMES, so asking for a twenty-second window yields whatever the nearest
    keyframes bracket -- measured against the deployed instance, twenty seconds
    requested came back as 25.97 and thirty came back as 36. The ceiling was
    therefore unreachable from the link path: asking for the maximum guaranteed
    exceeding it, and the error told the user their clip was too long for a
    length they had not chosen.

    Trimming rather than refusing also makes the two import paths agree. The
    browser's toWav has always trimmed to the ceiling instead of rejecting; the
    server refusing was the odd one out.

    stdlib `wave` throughout: this image has no ffmpeg and no audio library, and
    truncating frames needs neither -- the format is a header and a block of
    samples, and fewer samples is a shorter file.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if not rate:
                return None
            keep = int(seconds * rate)
            if handle.getnframes() <= keep:
                return handle.getnframes() / rate
            params = handle.getparams()
            frames = handle.readframes(keep)
    except (wave.Error, OSError, EOFError):
        return None

    try:
        with wave.open(str(path), "wb") as out:
            out.setparams(params._replace(nframes=keep))
            out.writeframes(frames)
    except (wave.Error, OSError) as exc:
        log.warning("could not trim %s: %s", path, exc)
        return None
    return keep / rate


def _duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else None
    except (wave.Error, OSError, EOFError):
        return None


def writable() -> bool:
    """Is the store mounted and writable? Each person's directory is made under it on first save."""
    directory = _dir()
    return directory.is_dir() and os.access(directory, os.W_OK)


def save(name: str, data: bytes, *, owner: str | None, replace: bool = False,
         suffix: str = SUFFIX) -> dict[str, object]:
    """Write one clip into `owner`'s namespace, or raise ClipError a person can act on.

    `owner` is keyword-only and has no default: which namespace a clip lands
    in is never something a call site gets by leaving an argument out.
    """
    root = _dir()
    if not root.is_dir():
        raise ClipError(
            f"the voice directory {root} is not mounted, so a cloned "
            "voice would vanish on the next restart. Mount the `voices` "
            "volume -- see this service's README.")
    if not os.access(root, os.W_OK):
        raise ClipError(f"the voice directory {root} is not writable")
    directory = _namespace(owner)
    stem = slug(name)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ClipError(f"could not make a voice directory: {exc}") from None

    if not data:
        raise ClipError("that clip is empty")
    if len(data) > config.MAX_CLIP_BYTES:
        raise ClipError(
            f"that clip is {len(data) / 1024**2:.1f} MB; the ceiling is "
            f"{config.MAX_CLIP_BYTES / 1024**2:.0f} MB. Ten to thirty seconds "
            "is what Chatterbox wants anyway.")

    suffix = suffix.lower()
    if suffix not in SUFFIXES:
        raise ClipError(
            f"{suffix or 'that file'} is not a format the voice service can "
            f"read; it takes {', '.join(SUFFIXES)}.")
    # One voice, one file. Saving narrator.mp3 next to an existing narrator.wav
    # would leave two clips claiming the name and the registry picking by
    # directory order.
    for existing in directory.glob(stem + ".*"):
        if existing.suffix.lower() in SUFFIXES and existing.suffix.lower() != suffix:
            if not replace:
                raise ClipError(f"a voice called {stem!r} already exists")
            with contextlib.suppress(OSError):
                existing.unlink()

    target = directory / (stem + suffix)
    if target.exists() and not replace:
        raise ClipError(f"a voice called {stem!r} already exists")

    # Written to a temporary name in the SAME directory and renamed into place.
    # tts-long is reading this directory and rescans it on mtime, so a
    # half-written file under the final name is a voice it can pick up and fail
    # to decode mid-job. os.replace is atomic within a filesystem.
    temporary = directory / f".{stem}{suffix}.part"
    try:
        temporary.write_bytes(data)
        seconds = _duration(temporary)
        if seconds is None and suffix == SUFFIX:
            raise ClipError(
                "that file claims to be a WAV and cannot be read as one.")
        if seconds is None:
            # Not measurable here: `wave` reads WAV and this image has no
            # ffmpeg and no audio library. tts-long decodes it with librosa
            # when it uses it. The byte ceiling above is the bound that still
            # applies, and the page reports the duration when its own decoder
            # managed to read the file.
            log.info("stored %s%s without a duration check", stem, suffix)
            os.replace(temporary, target)
            stat = target.stat()
            return {"name": stem, "owner": owner, "bytes": stat.st_size,
                    "modified": stat.st_mtime, "seconds": None}
        if seconds > config.MAX_CLIP_SECONDS:
            trimmed = _trim(temporary, config.MAX_CLIP_SECONDS)
            if trimmed is None:
                raise ClipError(
                    f"that clip is {seconds:.0f} s, over the "
                    f"{config.MAX_CLIP_SECONDS:.0f} s ceiling, and it could "
                    "not be trimmed.")
            log.info("trimmed %s from %.1f s to %.1f s", stem, seconds, trimmed)
            seconds = trimmed
        if seconds < 1.0:
            raise ClipError(f"that clip is only {seconds:.1f} s of audio")
        os.replace(temporary, target)
    except ClipError:
        # unlink on failure, so a rejected upload leaves nothing behind for the
        # registry to find. Upstream does the same and it is the half of the
        # sequence people forget.
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise ClipError(f"could not write the clip: {exc}") from None

    log.info("saved reference clip %s (%.1f s, %d bytes)", stem, seconds, len(data))
    return {"name": stem, "owner": owner, "bytes": len(data), "seconds": seconds,
            "modified": target.stat().st_mtime}


def remove(name: str, *, owner: str | None) -> bool:
    """Delete one clip from `owner`'s namespace. False if it was not there."""
    stem = slug(name)
    directory = _namespace(owner)
    matches = [p for p in directory.glob(stem + ".*")
               if p.suffix.lower() in SUFFIXES]
    target = matches[0] if matches else directory / (stem + SUFFIX)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ClipError(f"could not delete {stem}: {exc}") from None
    log.info("deleted reference clip %s", stem)
    return True
