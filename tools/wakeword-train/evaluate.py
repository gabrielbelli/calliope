"""Evaluate trained wake word models the way the hub runs them.

    evaluate.py /srv/wakeword-train/models/hey_claude.onnx [more.onnx ...]
        [--json out.json] [--text out.txt] [--train-log logs/full-hey_claude.log]
        [--false-accept data/falseaccept/pt-fleurs ...] [--heldout-set NAME]

Each model is loaded with openwakeword 0.6.0's Model(wakeword_models=[path],
inference_framework="onnx"), as services/satellites/app/wakeword.py loads it,
and fed 16 kHz int16 audio in 80 ms frames. Three measurements, and a fourth
on request:

1. HELD-OUT POSITIVES. Clips of the wake word no model trained on, from
   $WW_ROOT/data/heldout/<name>/positive/<source>/*.wav, where <name> is the
   model file's stem, or --heldout-set to measure a model on another model's
   clips (the stock alexa_v0.1.onnx on alexa_ptbr's, say):
     libritts          LibriTTS-R blends of speakers 700-903, never used in training
     say-en            natural macOS English voices (make_say_clips.py makes the say-* sets)
     say-en-robotic    Eloquence and MacinTalk English voices
     say-<accent>      the natural voices of a make_say_clips.py --accent locale,
                       reading the English words (say-ptbr for pt_BR)
     say-<accent>-robotic  its Eloquence voices, likewise
   Every directory there is reported, whatever its name.
   Each clip is played after 3 s of a -60 dBFS noise floor and followed by 1 s
   of it; its score is the highest frame score. Clean, and again with an
   AudioSet background under it at 10 dB SNR. Recall at a threshold is the
   share of clips whose score reaches it.

2. HELD-OUT NEAR MISSES. The same, for .../negative/<source>/*.wav: phrases
   that sound close to the wake word and were kept out of training. The share
   that reaches the threshold is a false-accept rate on near misses.

3. FALSE ACTIVATIONS PER HOUR on openWakeWord's validation set (~10.7 h of
   precomputed features: speech, music, noise). Counted as the hub counts a
   detection: a frame at or above the threshold fires once, then the model
   stays quiet until its score drops below the threshold and 1.5 s have passed.
   The raw count of frames above the threshold, which is what train.py
   reports, is given as well; it is always higher.

4. FALSE ACTIVATIONS PER HOUR ON REAL RECORDINGS, for each --false-accept
   directory of WAV files at any rate, made 16 kHz mono here. The validation
   set is mostly English, so this is where speech in the language of the
   house gets measured. The files play back to back, in name order, as one
   stream through Model.predict, 1,280 samples at a time, as the hub feeds a
   satellite: one reset at the start, the warm-up not scored, then the same
   count as in 3. The audio goes through openWakeWord's own streaming feature
   code, not features computed in advance, and the detections match the
   hub's WakeWords.feed frame for frame. Reported at 0.5, 0.7, 0.8, 0.85, 0.9
   and 0.95, with the 20 highest-scoring detections at 0.5 (file, seconds
   into it, peak score) to listen to. One hour of audio takes about 1.6
   minutes of one core of a Ryzen 5 5600G. Nothing in these directories may
   ever reach training.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

import numpy as np
import scipy.io.wavfile
import scipy.signal

os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

RATE = 16000
FRAME = 1280
REFRACTORY_FRAMES = math.ceil(1.5 * RATE / FRAME)
ROOT = Path(os.environ.get("WW_ROOT", "/srv/wakeword-train"))
THRESHOLDS = (0.3, 0.5, 0.7, 0.9)
# Finer at the top: a model that false-triggers on real speech is run at a
# high threshold, and the question is how high it has to go.
FA_THRESHOLDS = (0.5, 0.7, 0.8, 0.85, 0.9, 0.95)
FA_LOUDEST = 20  # detections listed per --false-accept directory


def load_wav(path: Path) -> np.ndarray:
    sr, x = scipy.io.wavfile.read(path)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768
    elif x.dtype == np.int32:
        x = x.astype(np.float32) / 2**31
    else:
        x = x.astype(np.float32)
    if sr != RATE:
        g = math.gcd(sr, RATE)
        x = scipy.signal.resample_poly(x, RATE // g, sr // g)
    return x  # float in [-1, 1]


def _active_rms(x: np.ndarray) -> float:
    """RMS over the 20 ms frames within 30 dB of the loudest: the speech, not
    the silence a TTS engine leaves around it."""
    n = len(x) // 320 * 320
    frames = x[:n].reshape(-1, 320) if n else x[None]
    e = (frames ** 2).mean(axis=1) + 1e-12
    return float(np.sqrt(e[e >= e.max() / 1000].mean()))


class Scorer:
    def __init__(self, model_path: Path):
        from openwakeword.model import Model

        self.path = Path(model_path)
        self.model = Model(wakeword_models=[str(self.path)], inference_framework="onnx")
        self.key = self.path.stem
        if self.model.model_outputs[self.key] != 1:
            raise ValueError(f"{self.path} has {self.model.model_outputs[self.key]} outputs; the hub needs 1")
        self.n_frames = self.model.model_inputs[self.key]
        # openWakeWord fills its window with random embeddings on reset; scores
        # are meaningless until real audio has pushed them all out (see the hub).
        self.warmup = (self.n_frames - 1) * FRAME

    def max_score(self, speech: np.ndarray, background: np.ndarray | None = None, snr_db: float = 10.0,
                  seed: int = 0) -> float:
        rng = np.random.default_rng(seed)
        lead, tail = 3 * RATE, 1 * RATE
        x = np.concatenate([np.zeros(lead), speech, np.zeros(tail)])
        x += rng.normal(0, 10 ** (-60 / 20), len(x))  # -60 dBFS floor: no exact zeros
        if background is not None:
            start = int(rng.integers(0, max(1, len(background) - len(x))))
            bg = np.resize(background[start:start + len(x)], len(x))
            gain = _active_rms(speech) / (np.sqrt((bg ** 2).mean()) + 1e-9) / 10 ** (snr_db / 20)
            x += bg * gain
        peak = np.abs(x).max()
        if peak > 1:
            x /= peak
        pcm = (x * 32767).astype(np.int16)

        np.random.seed(seed)  # the reset's random window, made repeatable
        self.model.reset()
        best, fed = 0.0, 0
        for off in range(0, len(pcm) - FRAME + 1, FRAME):
            score = float(self.model.predict(pcm[off:off + FRAME])[self.key])
            fed += FRAME
            if fed > self.warmup:
                best = max(best, score)
        return best

    def stream(self, clips: list[Path]) -> tuple[np.ndarray, np.ndarray]:
        """The score of every 80 ms frame of `clips` played back to back as one
        stream, as WakeWords.feed scores a satellite's audio, and where each
        clip starts in it, in samples.

        Frame i of the result ends at stream sample warmup + (i + 1) * FRAME:
        the warm-up frames are fed but not scored, as in the hub. A clip's
        last partial frame carries over into the next clip, so the stream has
        no gaps and no padding."""
        np.random.seed(0)  # the reset's random window, made repeatable
        self.model.reset()
        scores: list[float] = []
        starts, fed, total = [], 0, 0
        pending = np.empty(0, dtype=np.int16)
        for clip in clips:
            starts.append(total)
            pcm = (np.clip(load_wav(clip), -1, 1) * 32767).astype(np.int16)
            total += len(pcm)
            x = np.concatenate((pending, pcm))
            whole = len(x) - len(x) % FRAME
            for off in range(0, whole, FRAME):
                score = float(self.model.predict(x[off:off + FRAME])[self.key])
                fed += FRAME
                if fed > self.warmup:
                    scores.append(score)
            pending = x[whole:]
        return np.array(scores, dtype=np.float32), np.array(starts, dtype=np.int64)

    def feature_scores(self, feats: np.ndarray) -> np.ndarray:
        """The model's score for every 80 ms step of a stream of embeddings,
        run on the ONNX session openWakeWord loaded."""
        sess = self.model.models[self.key]
        name = sess.get_inputs()[0].name
        n = self.n_frames
        windows = np.lib.stride_tricks.sliding_window_view(feats, (n, feats.shape[1]))[:, 0]
        out = np.empty(len(windows), dtype=np.float32)
        try:  # the export fixes the batch at 1, but try a batch in case a model allows it
            sess.run(None, {name: windows[:2].astype(np.float32)})
            for i in range(0, len(windows), 4096):
                out[i:i + 4096] = sess.run(None, {name: windows[i:i + 4096].astype(np.float32)})[0].ravel()
        except Exception:
            for i in range(len(windows)):
                out[i] = sess.run(None, {name: windows[i:i + 1].astype(np.float32)})[0][0, 0]
        return out


def detections(scores: np.ndarray, threshold: float) -> list[int]:
    """The frames the hub's WakeWords.feed would fire on."""
    fired, armed, quiet_until = [], True, 0
    for i, s in enumerate(scores):
        if s < threshold:
            armed = True
        elif armed and i >= quiet_until:
            fired.append(i)
            armed = False
            quiet_until = i + REFRACTORY_FRAMES
    return fired


def activations(scores: np.ndarray, threshold: float) -> int:
    """Detections as the hub's WakeWords.feed counts them."""
    return len(detections(scores, threshold))


def false_accepts(sc: Scorer, directory: Path) -> dict:
    """Measurement 4 for one directory of recordings. A detection's "at_s" is
    where the hub would fire, which is about 0.8 s after the word that fired
    it, so a detection early in a file may belong to the end of the one
    before."""
    clips = sorted(directory.rglob("*.wav"))
    if not clips:
        raise FileNotFoundError(f"--false-accept {directory}: no .wav files")
    scores, starts = sc.stream(clips)
    if not len(scores):
        raise ValueError(f"--false-accept {directory}: shorter than the model's warm-up")
    hours = len(scores) * FRAME / RATE / 3600
    t0 = FA_THRESHOLDS[0]
    loudest = []
    for i in detections(scores, t0):
        # The peak of the rise that fired, not the frame that crossed: a
        # 0.99 is a word the model is sure of, a 0.51 a brush with the line.
        peak = float(scores[i:i + REFRACTORY_FRAMES].max())
        end = sc.warmup + (i + 1) * FRAME
        k = int(np.searchsorted(starts, end, side="right")) - 1
        loudest.append({"file": str(clips[k].relative_to(directory)),
                        "at_s": round((end - int(starts[k])) / RATE, 2), "score": round(peak, 4)})
    loudest.sort(key=lambda d: -d["score"])
    return {
        "dir": str(directory), "files": len(clips), "hours": round(hours, 3),
        "activations_per_hour": {str(t): round(activations(scores, t) / hours, 3) for t in FA_THRESHOLDS},
        "activations": {str(t): activations(scores, t) for t in FA_THRESHOLDS},
        "max_score": round(float(scores.max()), 4),
        f"loudest_at_{t0}": loudest[:FA_LOUDEST],
    }


def _train_metrics(train_log: Path | None) -> dict:
    """The last 'Final Model ...' block train.py logged, if any."""
    if not train_log or not train_log.exists():
        return {}
    text = train_log.read_text(errors="replace")
    out = {}
    for key, label in (("accuracy", "Accuracy"), ("recall", "Recall"),
                       ("fp_per_hour_frames", "False Positives per Hour")):
        found = re.findall(rf"Final Model {label}: ([0-9.eE+-]+)", text)
        if found:
            out[key] = float(found[-1])
    shapes = re.findall(r"Best model from training step.*|Increasing weight.*", text)
    if shapes:
        out["notes"] = sorted(set(shapes))
    return out


def evaluate(model_path: Path, heldout: Path, val_features: Path, background_dir: Path,
             train_log: Path | None, false_accept_dirs: list[Path] | None = None,
             heldout_set: str | None = None) -> dict:
    sc = Scorer(model_path)
    name = heldout_set or sc.key
    report: dict = {
        "model": sc.key, "file": str(model_path),
        "sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "input_frames": sc.n_frames, "window_s": sc.n_frames * FRAME / RATE,
        "heldout_set": name,
        "thresholds": list(THRESHOLDS), "positives": {}, "near_misses": {},
        "train_log_metrics": _train_metrics(train_log),
    }

    bgs = sorted(background_dir.glob("*.wav"))[:400]
    rng = np.random.default_rng(7)

    def bg_for(i: int):
        return load_wav(bgs[int(rng.integers(len(bgs)))]) if bgs else None

    for kind, sub in (("positives", "positive"), ("near_misses", "negative")):
        for src_dir in sorted((heldout / name / sub).glob("*")):
            clips = sorted(src_dir.glob("*.wav"))
            if not clips:
                continue
            clean, noisy, per_clip = [], [], {}
            for i, clip in enumerate(clips):
                x = load_wav(clip)
                c = sc.max_score(x, seed=i)
                n = sc.max_score(x, background=bg_for(i), seed=i)
                clean.append(c)
                noisy.append(n)
                per_clip[clip.name] = [round(c, 4), round(n, 4)]
            clean, noisy = np.array(clean), np.array(noisy)
            report[kind][src_dir.name] = {
                "clips": len(clips),
                "median_score_clean": round(float(np.median(clean)), 4),
                "median_score_noisy_10dB": round(float(np.median(noisy)), 4),
                "rate_clean": {str(t): round(float((clean >= t).mean()), 4) for t in THRESHOLDS},
                "rate_noisy_10dB": {str(t): round(float((noisy >= t).mean()), 4) for t in THRESHOLDS},
                "per_clip_clean_noisy": per_clip,
            }

    feats = np.load(val_features).astype(np.float32)
    hours = len(feats) * FRAME / RATE / 3600
    scores = sc.feature_scores(feats)
    report["validation"] = {
        "hours": round(hours, 2),
        "activations_per_hour": {str(t): round(activations(scores, t) / hours, 3) for t in THRESHOLDS},
        "activations": {str(t): activations(scores, t) for t in THRESHOLDS},
        "frames_above_per_hour": {str(t): round(float((scores >= t).sum()) / hours, 3) for t in THRESHOLDS},
        "max_score": round(float(scores.max()), 4),
    }

    report["false_accept"] = {}
    for d in false_accept_dirs or []:
        # Keyed by the directory's name, which says what it holds (pt-fleurs);
        # the full path is in "dir".
        key = d.name if d.name not in report["false_accept"] else str(d)
        report["false_accept"][key] = false_accepts(sc, d)
    return report


def summary(r: dict) -> str:
    t = "0.5"
    lines = [f"{r['model']}: window {r['window_s']:.2f} s ({r['input_frames']} frames), sha256 {r['sha256'][:12]}"
             + (f", held-out set {r['heldout_set']}" if r["heldout_set"] != r["model"] else "")]
    for src, v in r["positives"].items():
        lines.append(f"  held-out positives [{src}] n={v['clips']}: recall@0.5 clean {v['rate_clean'][t]:.0%}, "
                     f"noisy {v['rate_noisy_10dB'][t]:.0%}; median score {v['median_score_clean']:.2f} / "
                     f"{v['median_score_noisy_10dB']:.2f}")
    for src, v in r["near_misses"].items():
        lines.append(f"  near misses [{src}] n={v['clips']}: accepted@0.5 clean {v['rate_clean'][t]:.0%}, "
                     f"noisy {v['rate_noisy_10dB'][t]:.0%}")
    val = r["validation"]
    fa = ", ".join(f"@{k} {v:g}" for k, v in val["activations_per_hour"].items())
    lines.append(f"  false activations/hour on {val['hours']} h validation set: {fa}")
    for src, v in r["false_accept"].items():
        fa = ", ".join(f"@{k} {n:g}" for k, n in v["activations_per_hour"].items())
        lines.append(f"  false activations/hour on {src} ({v['hours']:.2f} h, {v['files']} files): {fa}; "
                     f"max score {v['max_score']:.2f}")
    if r["train_log_metrics"]:
        m = r["train_log_metrics"]
        lines.append("  train.py final: " + ", ".join(f"{k} {v}" for k, v in m.items() if k != "notes"))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models", nargs="+", type=Path)
    ap.add_argument("--heldout", type=Path, default=ROOT / "data" / "heldout")
    ap.add_argument("--val-features", type=Path, default=ROOT / "data" / "features" / "validation_set_features.npy")
    ap.add_argument("--background", type=Path, default=ROOT / "data" / "background" / "audioset")
    ap.add_argument("--train-log", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--text", type=Path)
    ap.add_argument("--false-accept", type=Path, action="append", default=[], metavar="DIR",
                    help="a directory of long recordings with no wake word in them, streamed through "
                         "each model for false activations per hour (measurement 4); repeat for several")
    ap.add_argument("--heldout-set", metavar="NAME",
                    help="read held-out clips from <heldout>/NAME instead of the model's own name")
    args = ap.parse_args()
    for d in args.false_accept:
        if not d.is_dir():
            sys.exit(f"--false-accept {d}: not a directory")

    reports = [evaluate(m, args.heldout, args.val_features, args.background,
                        args.train_log if len(args.models) == 1 else None,
                        args.false_accept, args.heldout_set) for m in args.models]
    text = "\n".join(summary(r) for r in reports)
    print(text)
    if args.json:
        args.json.write_text(json.dumps(reports[0] if len(reports) == 1 else reports, indent=1))
    if args.text:
        args.text.write_text(text + "\n")


if __name__ == "__main__":
    sys.exit(main())
