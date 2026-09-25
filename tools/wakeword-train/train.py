"""Train openWakeWord models for the phrases in phrases.yaml, one after another.

    train.py prepare                        fetch the datasets into $WW_ROOT/data, once
    train.py train --profile smoke --only hey_claude
    train.py train --profile full           every model in phrases.yaml, in order
    train.py heldout --only hey_claude      fresh LibriTTS-R clips for evaluate.py

Everything lives under $WW_ROOT (default /srv/wakeword-train):

    data/       datasets, fetched once and shared by every model
    work/       per profile and model: the YAML config, generated clips, features
    models/     <name>.onnx for the full profile, models/smoke/ for the smoke one
    reports/    evaluate.py's results, <profile>-<name>.json and .txt
    logs/       run.log (one line per stage), <profile>-<name>.log (everything)

Training is openWakeWord's own pipeline: openwakeword/train.py from a pinned
commit, driven by a YAML config built from its examples/custom_model.yml. Each
model runs its three stages (--generate_clips, --augment_clips, --train_model)
as separate processes, so each stage starts with the GPU empty. A model whose
.onnx already exists is skipped, so a stopped run resumes where it stopped;
the clips and features of a half-finished model are reused as well.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("WW_ROOT", "/srv/wakeword-train"))
DATA = ROOT / "data"
OWW_DIR = Path("/opt/openWakeWord")
PSG_DIR = Path("/opt/piper-sample-generator")

log = logging.getLogger("wakeword-train")

# ---------------------------------------------------------------------------
# Profiles. `full` takes about 1.5 h per model on the GTX 1050 Ti (4 GB) on
# holocron; `smoke` proves the pipeline end to end in about 10 min.
# ---------------------------------------------------------------------------
PROFILES = {
    # steps: at 3,000 the rising negative weight collapses the model to a
    # constant output before it learns anything; 10,000 is the notebook's value.
    "smoke": dict(n_samples=2000, n_samples_val=500, steps=10000, tts_batch_size=50,
                  augmentation_batch_size=16, augmentation_rounds=1),
    # upstream's defaults for steps; n_samples well past its "20,000 minimum"
    "full": dict(n_samples=50000, n_samples_val=5000, steps=50000, tts_batch_size=50,
                 augmentation_batch_size=16, augmentation_rounds=1),
}

# ---------------------------------------------------------------------------
# Data, pinned by revision and hash.
# ---------------------------------------------------------------------------
HF = "https://huggingface.co"
FEATURES_REV = "985bf1b47e7f19c07741af82bfe32d5a9dc56096"   # davidscripka/openwakeword_features
ACAV = dict(
    path=DATA / "features" / "openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
    url=f"{HF}/datasets/davidscripka/openwakeword_features/resolve/{FEATURES_REV}/"
        "openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
    size=17280000128,
    sha256="721a66d0682c65a1b5c1da0aa109409cede1d20e28b15235c344b000cbb7654f")
VALIDATION = dict(
    path=DATA / "features" / "validation_set_features.npy",
    url=f"{HF}/datasets/davidscripka/openwakeword_features/resolve/{FEATURES_REV}/"
        "validation_set_features.npy",
    size=184836608,
    sha256="a56a8a0f8e0efb91900acc6de4c0cdf4c564842e8475a7d49b36c039e17a690f")
RIR_REPO, RIR_REV = "davidscripka/MIT_environmental_impulse_responses", "b824a1ef2821f112fda0b9cb26e4278c62b425bb"
AUDIOSET_REV = "0c609e8302cf139307f639c57652032af0a88041"   # agkphysics/AudioSet (parquet)
AUDIOSET_SHARDS = {  # 3 of the 38 balanced-train shards, ~540 ten-second clips each
    "00": (687636067, "b433e7bcf3bbdfb0488791fceae1eb7100711d13093d22e2253f15d2dcabc084"),
    "01": (682091793, "c38eba06b54801c655d61346193a5974722e7b92af3a9e668effc3a41e47a2a1"),
    "02": (704032600, "dad9f493edde823055ba7c1ed417946949fe62f702317f94477ad1ca5d9749c2"),
}
FMA_URL, FMA_SIZE, FMA_CLIPS = "https://os.unil.cloud.switch.ch/fma/fma_small.zip", 7679594875, 240

RIRS = DATA / "rirs"
BACKGROUNDS = [DATA / "background" / "audioset", DATA / "background" / "fma"]
HELDOUT = DATA / "heldout"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, size: int | None = None, sha256: str | None = None) -> Path:
    """Fetch url into dest, resuming a .part file, and rename it into place only
    once size and hash match. A file at dest is taken as already verified."""
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(8):
        have = part.stat().st_size if part.exists() else 0
        if size is not None and have >= size:
            break
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=60) as r:
                if r.status_code == 416:
                    break
                r.raise_for_status()
                if have and r.status_code != 206:
                    have = 0  # the server ignored the range: start over
                with open(part, "ab" if have else "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            if size is None:
                break
        except requests.RequestException as e:
            log.warning("download of %s interrupted (%s), retrying", url, e)
            time.sleep(min(60, 5 * (attempt + 1)))
    got = part.stat().st_size
    if size is not None and got != size:
        raise RuntimeError(f"{url}: {got} bytes, expected {size}")
    if sha256 is not None:
        log.info("checking SHA-256 of %s (%.1f GB)", dest.name, got / 1e9)
        digest = _sha256(part)
        if digest != sha256:
            part.unlink()
            raise RuntimeError(f"{url}: SHA-256 {digest}, expected {sha256}; deleted")
    os.replace(part, dest)
    log.info("downloaded %s (%.1f MB)", dest, got / 1e6)
    return dest


def _to_wav16k(src: bytes | Path, dest: Path) -> bool:
    """Decode anything ffmpeg reads to 16 kHz mono 16-bit WAV."""
    tmp = dest.with_name(dest.name + ".tmp.wav")
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i",
           "pipe:0" if isinstance(src, bytes) else str(src),
           "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(tmp)]
    p = subprocess.run(cmd, input=src if isinstance(src, bytes) else None, capture_output=True)
    if p.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1000:
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, dest)
    return True


class _HttpRange(io.RawIOBase):
    """A seekable read-only file over HTTP range requests, so zipfile can pull a
    few members out of a 7.7 GB archive without downloading all of it."""

    def __init__(self, url: str, size: int):
        self.url, self.size, self.pos = url, size, 0
        self.session = requests.Session()

    def seekable(self): return True
    def readable(self): return True
    def tell(self): return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        self.pos = {io.SEEK_SET: offset, io.SEEK_CUR: self.pos + offset, io.SEEK_END: self.size + offset}[whence]
        return self.pos

    def readinto(self, b):
        if self.pos >= self.size:
            return 0
        end = min(self.size, self.pos + len(b)) - 1
        for attempt in range(5):
            try:
                r = self.session.get(self.url, headers={"Range": f"bytes={self.pos}-{end}"}, timeout=60)
                r.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 4:
                    raise
                time.sleep(5 * (attempt + 1))
        n = len(r.content)
        b[:n] = r.content
        self.pos += n
        return n


def prepare() -> None:
    """Fetch everything training needs. Safe to run again: done parts are skipped."""
    for f in (ACAV, VALIDATION):
        download(f["url"], f["path"], f["size"], f["sha256"])

    # MIT environmental impulse responses (Traer & McDermott 2016), already 16 kHz.
    RIRS.mkdir(parents=True, exist_ok=True)
    tree = requests.get(f"{HF}/api/datasets/{RIR_REPO}/tree/{RIR_REV}/16khz", timeout=60).json()
    files = [t["path"] for t in tree if t["path"].endswith(".wav")]

    def rir(path):
        dest = RIRS / Path(path).name
        if dest.exists():
            return True
        r = requests.get(f"{HF}/datasets/{RIR_REPO}/resolve/{RIR_REV}/{path}", timeout=60)
        r.raise_for_status()
        return _to_wav16k(r.content, dest)

    with ThreadPoolExecutor(8) as ex:
        ok = sum(ex.map(rir, files))
    log.info("room impulse responses: %d of %d in %s", ok, len(files), RIRS)

    # AudioSet, balanced train, 3 shards: the notebook uses one of these.
    import pyarrow.parquet as pq

    out = BACKGROUNDS[0]
    out.mkdir(parents=True, exist_ok=True)
    for shard, (size, sha) in AUDIOSET_SHARDS.items():
        done = out / f".shard-{shard}.done"
        if done.exists():
            continue
        pqf = download(f"{HF}/datasets/agkphysics/AudioSet/resolve/{AUDIOSET_REV}/data/bal_train/{shard}.parquet",
                       DATA / "downloads" / "audioset" / f"bal_train-{shard}.parquet", size, sha)
        rows = []
        for batch in pq.ParquetFile(pqf).iter_batches(columns=["video_id", "audio"], batch_size=64):
            for vid, audio in zip(batch.column("video_id").to_pylist(), batch.column("audio").to_pylist()):
                rows.append((vid, audio["bytes"]))
        with ThreadPoolExecutor(4) as ex:
            ok = sum(ex.map(lambda r: (out / f"{r[0]}.wav").exists() or _to_wav16k(r[1], out / f"{r[0]}.wav"), rows))
        log.info("AudioSet shard %s: %d of %d clips converted", shard, ok, len(rows))
        done.touch()

    # Free Music Archive, fma_small: FMA_CLIPS 30 s tracks spread evenly over the
    # archive (it is ordered by track id), read out of the zip by range requests.
    out = BACKGROUNDS[1]
    out.mkdir(parents=True, exist_ok=True)
    if not (out / ".done").exists():
        zf = zipfile.ZipFile(io.BufferedReader(_HttpRange(FMA_URL, FMA_SIZE), buffer_size=1 << 20))
        mp3s = sorted(n for n in zf.namelist() if n.endswith(".mp3"))
        step = len(mp3s) / FMA_CLIPS
        chosen = [mp3s[int(i * step)] for i in range(FMA_CLIPS)]
        ok = 0
        for name in chosen:
            dest = out / (Path(name).stem + ".wav")
            if dest.exists() or _to_wav16k(zf.read(name), dest):
                ok += 1
            else:
                log.warning("FMA: could not decode %s, skipped", name)
        log.info("FMA: %d of %d clips converted", ok, len(chosen))
        (out / ".done").touch()
    log.info("data ready in %s", DATA)


# ---------------------------------------------------------------------------
# Held-out clips for evaluate.py
# ---------------------------------------------------------------------------
def heldout(names: list[str], spec: dict, n_pos: int = 60, n_neg_each: int = 4) -> None:
    """LibriTTS-R clips no model trained on: blends of two speakers from 700 to
    903, which training never uses (PSG_MAX_SPEAKERS=700), at other noise
    settings and another seed."""
    import numpy as np
    import torch
    import torchaudio

    sys.path.insert(0, str(PSG_DIR))
    import generate_samples as gs  # piper-sample-generator v2.0.0

    model_path = PSG_DIR / "models" / "en_US-libritts_r-medium.pt"
    config = json.load(open(f"{model_path}.json"))
    model = torch.load(model_path)
    model.eval()
    if torch.cuda.is_available():
        model.cuda()
    resampler = torchaudio.transforms.Resample(22050, 16000, lowpass_filter_width=64, rolloff=0.9475937167399596,
                                               resampling_method="kaiser_window", beta=14.769656459379492)
    rng = np.random.default_rng(20260925)
    torch.manual_seed(20260925)

    def render(text: str, dest: Path) -> None:
        a, b = (int(s) for s in rng.integers(int(os.environ.get("PSG_MAX_SPEAKERS", 700)), 904, size=2))
        ids = [gs.get_phonemes(config["espeak"]["voice"], config, text, False)]
        with torch.no_grad():
            audio = gs.generate_audio(model, torch.LongTensor([a]), torch.LongTensor([b]), ids,
                                      float(rng.uniform(0.3, 0.7)), 0.667, 0.8,
                                      float(rng.choice([0.85, 1.0, 1.15])), None)
        pcm = gs.audio_float_to_int16(resampler(audio.cpu()).numpy())[0].flatten()
        pcm = gs.remove_silence(pcm)
        import scipy.io.wavfile
        dest.parent.mkdir(parents=True, exist_ok=True)
        scipy.io.wavfile.write(dest, 16000, pcm)

    for name in names:
        s = spec["models"][name]
        pos = HELDOUT / name / "positive" / "libritts"
        if not pos.exists() or len(list(pos.glob("*.wav"))) < n_pos:
            for i in range(n_pos):
                render(s["phrases"][i % len(s["phrases"])], pos / f"{i:03d}.wav")
        neg = HELDOUT / name / "negative" / "libritts"
        if not neg.exists():
            for text in s.get("eval_negatives", []):
                for i in range(n_neg_each):
                    render(text, neg / f"{text.replace(' ', '_')}_{i}.wav")
        log.info("held-out LibriTTS-R clips for %s in %s", name, HELDOUT / name)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
class StageFailed(RuntimeError):
    pass


def build_config(name: str, spec: dict, profile: str, workdir: Path) -> dict:
    model = spec["models"][name]
    cfg = yaml.safe_load(open(OWW_DIR / "examples" / "custom_model.yml"))
    words = {w for p in model["phrases"] for w in p.split()}
    cfg.update(
        model_name=name,
        target_phrase=list(model["phrases"]),
        custom_negative_phrases=list(model.get("negatives", [])),
        piper_sample_generator_path=str(PSG_DIR),
        output_dir=str(workdir),
        rir_paths=[str(RIRS)],
        background_paths=[str(p) for p in BACKGROUNDS],
        background_paths_duplication_rate=[1] * len(BACKGROUNDS),
        false_positive_validation_data_path=str(VALIDATION["path"]),
        feature_data_files={"ACAV100M_sample": str(ACAV["path"])},
        extra_pronunciations={w: p for w, p in (spec.get("pronunciations") or {}).items() if w in words},
    )
    cfg.update(PROFILES[profile])
    cfg.update(model.get("train") or {})
    return cfg


def _run(cmd: list[str], log_path: Path, label: str) -> None:
    env = dict(os.environ, PYTHONUNBUFFERED="1", TQDM_MININTERVAL="30")
    t0 = time.time()
    with open(log_path, "a") as lf:
        lf.write(f"\n===== {time.strftime('%F %T')} {label}: {' '.join(cmd)}\n")
        lf.flush()
        p = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
    minutes = (time.time() - t0) / 60
    if p.returncode != 0:
        raise StageFailed(f"{label} exited {p.returncode} after {minutes:.1f} min; see {log_path}")
    log.info("%s done in %.1f min", label, minutes)


def train_one(name: str, spec: dict, profile: str, force: bool) -> None:
    models_dir = ROOT / "models" / ("" if profile == "full" else profile)
    final = models_dir / f"{name}.onnx"
    if final.exists() and not force:
        log.info("SKIP %s: %s exists", name, final)
        return
    workdir = ROOT / "work" / profile / name
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = ROOT / "logs" / f"{profile}-{name}.log"
    cfg_path = workdir / "config.yaml"
    yaml.safe_dump(build_config(name, spec, profile, workdir), open(cfg_path, "w"), sort_keys=False)
    log.info("START %s (%s): %s", name, profile, cfg_path)
    t0 = time.time()

    launcher = [sys.executable, str(HERE / "oww_train.py"), "--training_config", str(cfg_path)]
    for stage in ("generate_clips", "augment_clips", "train_model"):
        marker = workdir / f".{stage}.done"
        if stage != "train_model" and marker.exists():
            log.info("%s %s already done", name, stage)
            continue
        extra = []
        if stage == "augment_clips" and (workdir / name / "positive_features_train.npy").exists():
            extra = ["--overwrite"]  # features from an interrupted run: recompute them
        _run([*launcher, f"--{stage}", *extra], log_path, f"{name} {stage}")
        marker.touch()

    trained = workdir / f"{name}.onnx"
    if not trained.exists():
        raise StageFailed(f"train.py finished but wrote no {trained}")
    models_dir.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f".{final.name}.tmp")
    shutil.copyfile(trained, tmp)
    os.replace(tmp, final)
    log.info("MODEL %s -> %s (%.0f min)", name, final, (time.time() - t0) / 60)

    # Held-out clips on the GPU in a process of their own, then the evaluation.
    _run([sys.executable, str(HERE / "train.py"), "heldout", "--only", name], log_path, f"{name} heldout clips")
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    _run([sys.executable, str(HERE / "evaluate.py"), str(final),
          "--json", str(reports / f"{profile}-{name}.json"),
          "--text", str(reports / f"{profile}-{name}.txt"),
          "--train-log", str(log_path)], log_path, f"{name} evaluate")
    for line in (reports / f"{profile}-{name}.txt").read_text().splitlines():
        log.info("  %s", line)
    log.info("DONE %s in %.0f min", name, (time.time() - t0) / 60)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["prepare", "train", "heldout"])
    ap.add_argument("--profile", choices=sorted(PROFILES), default="full")
    ap.add_argument("--only", help="comma-separated model names (default: all, in phrases.yaml order)")
    ap.add_argument("--phrases", default=str(HERE / "phrases.yaml"))
    ap.add_argument("--force", action="store_true",
                    help="train again even if the .onnx exists; the clips and features are reused "
                         "(delete work/<profile>/<name> to make new ones)")
    args = ap.parse_args()

    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler()]
    if args.command != "heldout":  # heldout runs inside a train, whose log already has it
        handlers.append(logging.FileHandler(ROOT / "logs" / "run.log"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    spec = yaml.safe_load(open(args.phrases))
    names = args.only.split(",") if args.only else list(spec["models"])
    unknown = [n for n in names if n not in spec["models"]]
    if unknown:
        sys.exit(f"not in {args.phrases}: {unknown}")

    if args.command == "prepare":
        prepare()
    elif args.command == "heldout":
        heldout(names, spec)
    else:
        import onnxruntime
        import torch
        log.info("run: profile=%s models=%s torch=%s cuda=%s ort=%s providers=%s", args.profile, names,
                 torch.__version__, torch.cuda.is_available(), onnxruntime.__version__,
                 onnxruntime.get_available_providers())
        failed = []
        for name in names:
            try:
                train_one(name, spec, args.profile, args.force)
            except Exception as e:  # one bad model must not stop the queue
                log.error("FAILED %s: %s", name, e)
                failed.append(name)
        log.info("FINISHED profile=%s; failed: %s", args.profile, failed or "none")
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
