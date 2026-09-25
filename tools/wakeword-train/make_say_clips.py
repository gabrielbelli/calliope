# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml==6.0.2"]
# ///
"""Held-out clips from macOS `say`, for evaluate.py. Runs on a Mac only.

    uv run make_say_clips.py --out /tmp/heldout
    rsync -a /tmp/heldout/ holocron.gabrielbelli.com:/srv/wakeword-train/data/heldout/

`say -o` renders to a file and plays nothing. Clips are 16 kHz mono 16-bit
WAV. Positives are each model's `eval` texts in every English voice and every
Brazilian Portuguese voice installed (the Portuguese voices read the English
words with a Brazilian accent), the Portuguese ones also at a slower rate,
in one directory per kind of voice, which evaluate.py reports separately:

    <out>/<model>/positive/say-en/             natural English voices
    <out>/<model>/positive/say-en-robotic/     Eloquence and MacinTalk voices
    <out>/<model>/positive/say-ptbr/           natural Brazilian voices (Luciana)
    <out>/<model>/positive/say-ptbr-robotic/   Eloquence Brazilian voices

The robotic voices are formant or diphone synthesis from the 1990s; they test
something, but not what a person sounds like. Near misses are the
`eval_negatives` in three voices of each language, in <out>/<model>/negative/say/.
The novelty voices (Bells, Zarvox and the like) are left out. No model trains
on any of this: none of the training voices is a macOS voice.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent

# The novelty voices (sound effects, singing, robots) are no test of anything.
NOVELTY = {"Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Good News", "Jester", "Organ",
           "Superstar", "Trinoids", "Whisper", "Wobble", "Zarvox"}
# Eloquence (Eddy ... Shelley, in several languages) and MacinTalk voices.
ROBOTIC = {"Eddy", "Flo", "Grandma", "Grandpa", "Reed", "Rocko", "Sandy", "Shelley",
           "Albert", "Fred", "Junior", "Kathy", "Ralph"}


def group(voice: str, language: str) -> str:
    return f"say-{language}" + ("-robotic" if voice.split(" (")[0] in ROBOTIC else "")


def voices() -> tuple[list[str], list[str]]:
    """Installed English and Brazilian Portuguese voices, by locale. A line
    reads "Eddy (Portuguese (Brazil)) pt_BR    # Olá...": the name ends where
    the locale starts."""
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    en, br = [], []
    for line in out.splitlines():
        m = re.match(r"^(.*?)\s+([a-z]{2,3}_[A-Z0-9]{2,3})\s+#", line)
        if not m or m.group(1).split(" (")[0] in NOVELTY:
            continue
        name, locale = m.groups()
        if locale.startswith("en_") and name not in en:
            en.append(name)
        elif locale == "pt_BR" and name not in br:
            br.append(name)
    return en, br


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
    args = ap.parse_args()

    spec = yaml.safe_load(args.phrases.read_text())
    en, br = voices()
    print(f"voices: {len(en)} English, {len(br)} Brazilian Portuguese")
    names = args.only.split(",") if args.only else list(spec["models"])
    for name in names:
        m = spec["models"][name]
        pos = args.out / name / "positive"
        n = 0
        for text in m["eval"]:
            for voice, language in [(v, "en") for v in en] + [(v, "ptbr") for v in br]:
                render(voice, text, pos / group(voice, language) / f"{slug(voice)}_{slug(text)}.wav")
                n += 1
            for voice in br:
                render(voice, text, pos / group(voice, "ptbr") / f"{slug(voice)}-slow_{slug(text)}.wav", rate=140)
                n += 1
        neg = args.out / name / "negative" / "say"
        k = 0
        for text in m.get("eval_negatives", []):
            for voice in en[:3] + br[:3]:
                render(voice, text, neg / f"{slug(voice)}_{slug(text)}.wav")
                k += 1
        print(f"{name}: {n} positives, {k} near misses")


if __name__ == "__main__":
    main()
