# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml==6.0.2"]
# ///
"""Held-out clips from macOS `say`, for evaluate.py. Runs on a Mac only.

    uv run make_say_clips.py --out /tmp/heldout [--accent pt_BR ...]
    rsync -a /tmp/heldout/ gpu-host:/srv/wakeword-train/data/heldout/

`say -o` renders to a file and plays nothing. Clips are 16 kHz mono 16-bit
WAV. Positives are each model's `eval` texts in every English voice
installed, and in every voice of each `--accent` locale: those voices read
the English words with their own language's accent, which is the nearest
test of how a speaker with that accent says the wake word. An accent's
voices speak each text at a slower rate as well. One directory per kind of
voice, which evaluate.py reports separately:

    <out>/<model>/positive/say-en/               natural English voices
    <out>/<model>/positive/say-en-robotic/       Eloquence and MacinTalk voices
    <out>/<model>/positive/say-<accent>/         natural voices of an accent
                                                 (say-ptbr for --accent pt_BR)
    <out>/<model>/positive/say-<accent>-robotic/ Eloquence voices of it

The robotic voices are formant or diphone synthesis from the 1990s; they test
something, but not what a person sounds like. Near misses are the
`eval_negatives` in three voices of English and of each accent, in
<out>/<model>/negative/say/. The novelty voices (Bells, Zarvox and the like)
are left out. No model trains on any of this: none of the training voices is
a macOS voice.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent

# The novelty voices (sound effects, singing, robots) are no test of anything.
NOVELTY = {"Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Good News", "Jester", "Organ",
           "Superstar", "Trinoids", "Whisper", "Wobble", "Zarvox"}
# Eloquence (Eddy ... Shelley, in several languages) and MacinTalk voices.
ROBOTIC = {"Eddy", "Flo", "Grandma", "Grandpa", "Reed", "Rocko", "Sandy", "Shelley",
           "Albert", "Fred", "Junior", "Kathy", "Ralph"}
LOCALE = re.compile(r"[a-z]{2,3}_[A-Z0-9]{2,3}")


def label(locale: str) -> str:
    """The directory name of an accent: "pt_BR" -> "ptbr"."""
    return locale.lower().replace("_", "")


def group(voice: str, language: str) -> str:
    return f"say-{language}" + ("-robotic" if voice.split(" (")[0] in ROBOTIC else "")


def voices(accents: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Installed English voices, and the voices of each accent locale. A line
    reads "Eddy (Portuguese (Brazil)) pt_BR    # Olá...": the name ends where
    the locale starts."""
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    en: list[str] = []
    by_accent: dict[str, list[str]] = {a: [] for a in accents}
    for line in out.splitlines():
        m = re.match(r"^(.*?)\s+([a-z]{2,3}_[A-Z0-9]{2,3})\s+#", line)
        if not m or m.group(1).split(" (")[0] in NOVELTY:
            continue
        name, locale = m.groups()
        if locale.startswith("en_") and name not in en:
            en.append(name)
        elif locale in by_accent and name not in by_accent[locale]:
            by_accent[locale].append(name)
    return en, by_accent


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def render(voice: str, text: str, dest: Path, rate: int | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["say", "-v", voice, "-o", str(dest), "--file-format=WAVE", "--data-format=LEI16@16000"]
    if rate:
        cmd += ["-r", str(rate)]
    subprocess.run(cmd + [text], check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--phrases", type=Path, default=HERE / "phrases.yaml")
    ap.add_argument("--only", help="comma-separated model names")
    ap.add_argument("--accent", action="append", default=[], metavar="LOCALE",
                    help="the locale of `say` voices to read the English words with an accent, "
                         "such as pt_BR or de_DE (`say -v ?` lists them); repeat for several")
    args = ap.parse_args()
    for accent in args.accent:
        if not LOCALE.fullmatch(accent):
            sys.exit(f"--accent {accent}: not a locale such as pt_BR")
        if accent.startswith("en_"):
            sys.exit(f"--accent {accent}: every English voice is rendered already")

    spec = yaml.safe_load(args.phrases.read_text())
    en, by_accent = voices(args.accent)
    print(f"voices: {len(en)} English" + "".join(f", {len(v)} {a}" for a, v in by_accent.items()))
    for accent, found in by_accent.items():
        if not found:
            print(f"no {accent} voice is installed: add one in System Settings > Accessibility > "
                  "Spoken Content > System voice > Manage Voices", file=sys.stderr)
    names = args.only.split(",") if args.only else list(spec["models"])
    for name in names:
        m = spec["models"][name]
        pos = args.out / name / "positive"
        n = 0
        for text in m["eval"]:
            for voice in en:
                render(voice, text, pos / group(voice, "en") / f"{slug(voice)}_{slug(text)}.wav")
                n += 1
            for accent, found in by_accent.items():
                for voice in found:
                    where = pos / group(voice, label(accent))
                    render(voice, text, where / f"{slug(voice)}_{slug(text)}.wav")
                    render(voice, text, where / f"{slug(voice)}-slow_{slug(text)}.wav", rate=140)
                    n += 2
        neg = args.out / name / "negative" / "say"
        k = 0
        for text in m.get("eval_negatives", []):
            for voice in en[:3] + [v for found in by_accent.values() for v in found[:3]]:
                render(voice, text, neg / f"{slug(voice)}_{slug(text)}.wav")
                k += 1
        print(f"{name}: {n} positives, {k} near misses")


if __name__ == "__main__":
    main()
