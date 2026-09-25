# wakeword-train

Trains custom openWakeWord models for the satellites hub
(`services/satellites`), one `<name>.onnx` per wake word. The hub loads them
with openwakeword 0.6.0 on ONNX Runtime and scores 16 kHz audio in 80 ms
frames, the same way it runs the built-in `hey_jarvis`.

The training is openWakeWord's own automatic pipeline
(`openwakeword/train.py` driven by a YAML config, as in its
`notebooks/automatic_model_training.ipynb`), packaged as a Docker image with
every version pinned. It runs on a GPU with 4 GB of memory.

| File | What it is |
|---|---|
| `Dockerfile` | CUDA 12.2 base, Python 3.10, the pinned stack, the upstream code at pinned commits |
| `requirements.txt` | the top-level pins, with the reasons for them |
| `requirements.lock.txt` | every package in the image, from `pip freeze`; the Dockerfile installs this |
| `patches/openwakeword-train.patch` | three small fixes to upstream `train.py` (see below) |
| `patches/piper-sample-generator.patch` | more voice variety in the generated clips (see below) |
| `oww_train.py` | runs upstream `train.py` with three more fixes that need no edit to it |
| `train.py` | the driver: `prepare` the data, `train` models in order, make `heldout` clips |
| `evaluate.py` | loads each `.onnx` as the hub does and measures it |
| `make_say_clips.py` | makes held-out test clips with macOS `say` (Mac only) |
| `phrases.yaml` | the wake words: spellings, confusable phrases, test phrases |

## Quick start

On the GPU host, as a user who owns the work directory:

```bash
sudo mkdir -p /srv/wakeword-train && sudo chown "$USER": /srv/wakeword-train
sudo docker build -t wakeword-train:1 tools/wakeword-train

run() {
  sudo docker run --rm --gpus all --cpus 4 --memory 10g --memory-swap 12g --shm-size 2g \
    --user "$(id -u):$(id -g)" -v /srv/wakeword-train:/srv/wakeword-train wakeword-train:1 "$@"
}
run prepare                                   # about 20 GB, once
run train --profile smoke --only hey_claude   # prove the pipeline, about 10 min
```

On the Mac, held-out clips from `say` (it writes files and plays nothing):

```bash
uv run tools/wakeword-train/make_say_clips.py --out /tmp/heldout
rsync -a /tmp/heldout/ holocron.gabrielbelli.com:/srv/wakeword-train/data/heldout/
```

Then the full run, detached:

```bash
sudo docker run -d --name wakeword-train --restart=no --gpus all --cpus 4 \
  --memory 10g --memory-swap 12g --shm-size 2g --user "$(id -u):$(id -g)" \
  -v /srv/wakeword-train:/srv/wakeword-train wakeword-train:1 train --profile full
```

`--shm-size` matters: the training data loader passes batches between worker
processes through `/dev/shm`, and Docker's default of 64 MB is too small.

## Watching a run

```bash
tail -f /srv/wakeword-train/logs/run.log                   # one line per stage
# run.log is shared by every run; this shows the latest full run only
awk '/profile=full models/ {buf = ""} {buf = buf $0 "\n"} END {printf "%s", buf}' \
  /srv/wakeword-train/logs/run.log | grep -E 'START|DONE|FAILED|FINISHED'
tail -f /srv/wakeword-train/logs/full-hey_claude.log       # everything one model prints
cat /srv/wakeword-train/reports/full-hey_claude.txt        # its evaluation
ls -l /srv/wakeword-train/models/
```

`run.log` has `START <name>`, a line per finished stage, `MODEL <name>` when
the `.onnx` is written, the evaluation summary, then `DONE <name>`. A model
that fails is logged as `FAILED <name>` and the queue moves on. `FINISHED`
ends the run. A model whose `.onnx` exists is skipped, so starting the same
command again resumes; the clips and features of a model that was cut short
are reused. `--force` trains again over an existing `.onnx` but still reuses
the clips and features: after changing a phrase or a sample count, delete
`work/<profile>/<name>` so they are made again.

## What a run does, per model

1. **Config.** `examples/custom_model.yml` from upstream, with the model's
   phrases, the data paths, the profile and the model's own overrides, written
   to `work/<profile>/<name>/config.yaml`.
2. **`--generate_clips`.** piper-sample-generator's LibriTTS-R generator
   (speakers 0 to 699 of 904, blended in random pairs) says the target
   phrases `n_samples` times for training and `n_samples_val` times for
   validation. The same number of adversarial negatives is generated:
   openWakeWord's phonetically similar phrases plus the `negatives` in
   `phrases.yaml`.
3. **`--augment_clips`.** Each clip is padded to a fixed length, then EQ,
   distortion, pitch shift, band-stop, coloured noise, AudioSet and FMA
   background at -10 to 15 dB SNR, and MIT room impulse responses are applied
   at random. openWakeWord's melspectrogram and embedding models turn the
   result into features.
4. **`--train_model`.** A small fully connected network is trained on those
   features against about 2,000 hours of ACAV100M features (speech, music,
   noise), in three sequences with a rising weight on negatives. The false
   positive rate on the validation set steers checkpoint selection. The best
   checkpoints are averaged and exported to ONNX.
5. **Held-out clips and evaluation**, below.

## Profiles

| | `smoke` | `full` |
|---|---|---|
| `n_samples` (positives, and as many adversarial negatives) | 2,000 | 50,000 |
| `n_samples_val` | 500 | 5,000 |
| `steps` (sequence 1; sequences 2 and 3 add a tenth each) | 10,000 | 50,000 |
| `tts_batch_size` | 50 | 50 |
| Time on holocron (GTX 1050 Ti, 4 CPUs) | 10 min | about 85 min per model |

Measured on holocron: the generator makes about 80 positive and 35 to 60
negative clips a second (negatives run at a seventh of the batch size, and
batch 50 fills 3.8 of the GPU's 4 GB); augmentation and features run at about
60 clips a second; training runs at about 78 steps a second. So `full` spends
roughly 35 minutes making clips, 30 augmenting them, 15 training and 3
evaluating, and nine models take about 13 hours.

Do not cut `steps` below about 10,000. The weight on negatives rises from 1 to
`max_negative_weight` over the steps; in a 3,000-step smoke run it overtook
the positives before the model had learnt anything, and the model collapsed
to one constant output for every input.
The same data at 20,000 steps gave recall 0.51 at 0.44 false positives an
hour on upstream's own validation.

The single-word models (`claude`, `gemini`, `grok`, `gpt`, `lumos`) also get a
64-wide layer instead of 32 and 100 adversarial negatives per batch instead of
50. A single word is short, so ordinary speech contains near matches more
often, and these models false-trigger more than the two-word ones. Their
false-accept rates are reported as measured.

## Evaluation

`evaluate.py` loads each model with
`openwakeword.model.Model(wakeword_models=[path], inference_framework="onnx")`
and feeds it 16-bit 16 kHz audio in 1,280-sample frames, as
`services/satellites/app/wakeword.py` does. It measures:

- **Held-out positives**: clips of the wake word that no model trained on.
  `libritts` blends LibriTTS-R speakers 700 to 903, which training never
  uses, at other noise settings and another seed. The macOS `say` clips are
  in four groups: `say-en`, 8 natural English voices (American, British,
  Australian, Irish, Indian, South African); `say-en-robotic`, 21 Eloquence
  and MacinTalk voices; `say-ptbr`, Luciana, the natural Brazilian voice,
  reading the English words; `say-ptbr-robotic`, 8 Eloquence Brazilian
  voices. The Brazilian voices are the nearest available test of the user's
  accent. Each clip is scored clean, and again over an AudioSet background at
  10 dB SNR. Recall at 0.5 is the share of clips that reach 0.5.
- **Near misses**: `eval_negatives` from `phrases.yaml`, confusable phrases
  kept out of training, in the same voices. The share that reaches 0.5 is a
  false-accept rate on near misses.
- **False activations per hour** on openWakeWord's validation set (10.7 hours
  of speech, music and noise features), counted the way the hub counts a
  detection: once per crossing of the threshold, then quiet for 1.5 s.

Results go to `reports/<profile>-<name>.json` (with every clip's score) and
`.txt`, and the summary into `run.log`.

## Known limitation: accents

Every positive clip comes from LibriTTS-R (American audiobook readers) through
espeak-ng's `en-us` pronunciation. How far a model carries to other voices
depends on the word:

| Full model | Held-out LibriTTS-R | 8 natural macOS English voices | Luciana (Brazilian) | False activations/hour |
|---|---|---|---|---|
| `hey_claude` | 98% | 12% (only Samantha, the American voice) | 0% | 0 |
| `hey_gemini` | 100% | 88% | 0% | 0 |

Recall at 0.5, clean. "Claude" is one syllable whose vowel differs from
accent to accent. A check with "Hey Clawed", "Hey Clawd" and "Hey Clode" in
the same voices showed that the voices do say "clawd" (the first two score
exactly as "Claude" does); the British, Australian, Irish, Indian and South
African versions of that vowel are what the model rejects. Both models are
also very conservative: no false activation in 10.7 hours even at 0.3. So
expect a Brazilian accent to be missed more often than the LibriTTS-R numbers
suggest, try the hub's threshold at 0.3 first, and see below for adding
accents to training.

## Brazilian Portuguese voices

Training uses English voices only. The wake words are English, so Brazilian
Portuguese Piper voices were left out of the positive clips. The accent is
covered by the evaluation instead: the macOS Brazilian Portuguese voices read
the English wake words, and their scores sit in the `say` results beside the
English voices (the per-clip scores in the JSON name the voice).

Every Brazilian voice scored about 0.00 on `hey_claude`, in the smoke run and
the full one. The Eloquence voices apply Portuguese spelling rules
("KLAU-dji"), which makes them a harsh proxy, but Luciana, a natural voice,
scored 0.00 too. If the models miss the user's own voice, the way to add the
accent, and other accents, to training is to render the phrases with other
voices: the pt_BR Piper voices
(`rhasspy/piper-voices`: faber, cadu and jeff are CC0, edresson is CC BY 4.0)
or English Piper voices with other accents, written into `positive_train/`
before `--generate_clips` runs. Upstream `train.py` only tops that directory
up to `n_samples`, so pre-seeded clips become a share of the total.

## Fixes to upstream

`patches/openwakeword-train.patch`, applied to `train.py` at the pinned commit:

- **The negative weight doubles only when false positives are over target.**
  `auto_train` means to double it in sequences 2 and 3 when the false-positive
  rate is above `target_false_positives_per_hour`, but it tests
  `best_val_fp`, which starts at 1000 and is never updated, so it always
  doubled, to four times `max_negative_weight`. It now tests the rate measured
  at the end of the previous sequence, as the comment above it says it
  should. On `hey_claude` the difference was within run-to-run variation:
  three trainings on the same features, one with the bug and two without,
  gave held-out macOS recall of 9%, 15% and 6%, and no false activations.
- **The 11-hour false-positive validation set is fed in chunks of 50,000
  windows.** Upstream puts all of it on the GPU as one batch: about 3 GB for a
  2 s window and over 5 GB for a longer one, which does not fit in 4 GB.
  Every consumer sums over batches, so the numbers are unchanged.
- **`--convert_to_tflite` is honoured.** Its default is the string `"False"`,
  which is truthy, so upstream always attempted the TFLite export and failed
  on the missing TensorFlow after writing the ONNX file. The hub needs only
  ONNX.

`patches/piper-sample-generator.patch`, applied to `generate_samples.py` at
v2.0.0:

- **Speaker pairs in random order.** The generator walks its pairs as (0,0),
  (0,1), (0,2) and so on, so the main voice of every blend is one of the first
  3 speakers in a 2,000-clip run and of the first 56 in a 50,000-clip run.
  The pairs are now shuffled, seeded by the output directory, so a rerun is
  repeatable and the training and test sets differ.
- **Only speakers 0 to 699.** `PSG_MAX_SPEAKERS=700` (set in the image) fills
  the `max_speakers` argument that `train.py` never passes. The generator's
  own README warns that the late LibriTTS-R speakers had little data and can
  produce artefacts; they also make a held-out voice set for `evaluate.py`.

`oww_train.py`, around upstream `train.py` without changing it:

- **Words missing from CMUdict.** The adversarial phrase generator looks each
  target word up in CMUdict and otherwise downloads a DeepPhonemizer model.
  That URL returns 403, so "grok" or "lumos" crashed clip generation. The
  `pronunciations` in `phrases.yaml` are added to the dictionary in memory.
- **Homophones among the adversarial negatives.** The generator means to drop
  words that sound exactly like the target, but compares a string with a list,
  so it drops none: "clawed" became a negative for "claude", "gee pea tea" for
  "g p t". Phrases whose pronunciation equals a target's are now dropped, and
  the log says how many.
- **Logging.** `train.py` configures none, and piper-sample-generator then sets
  DEBUG for everything. INFO is set first.

## Versions

| Component | Version |
|---|---|
| Base image | `nvidia/cuda:12.2.2-base-ubuntu22.04`, Python 3.10 |
| PyTorch, torchaudio | 2.4.1+cu121 (CUDA 12.1, cuDNN 9.1) |
| ONNX Runtime | onnxruntime-gpu 1.19.2 |
| openwakeword (library, as in the hub) | 0.6.0 from PyPI |
| openWakeWord `train.py`, example config | commit `368c037` (30 Dec 2025, still versioned 0.6.0) |
| piper-sample-generator | v2.0.0, commit `195e3bd`, with the patch above |
| LibriTTS-R generator | `en_US-libritts_r-medium.pt` from its v2.0.0 release, SHA-256 `e95ee537...` |
| Feature models | `melspectrogram.onnx`, `embedding_model.onnx` from openWakeWord v0.5.1, SHA-256 as pinned in the hub |
| Training extras | as the upstream notebook pins them: speechbrain 0.5.14, audiomentations 0.33.0, torch-audiomentations 0.11.0, acoustics 0.2.6, torchmetrics 1.2.0, torchinfo 1.8.0, mutagen 1.47.0, pronouncing 0.2.0 |
| ONNX export | onnx 1.17.0 (`torch.onnx.export` needs it; upstream never lists it) |
| Everything else | `requirements.lock.txt` |

Why the `train.py` from `main` and not the 0.6.0 tag: the tag sizes the model
input from a fixed 2 s, which breaks phrases whose clips run longer
(`hey chat g p t`); `main` fixes it and changes nothing else that matters. The
library is still the PyPI 0.6.0 release, the one the hub runs.

To regenerate the lock after changing `requirements.txt`, install it in the
same base image and keep the output of `pip freeze --all` minus `pip`,
`setuptools`, `wheel` and `openwakeword`.

## Data

`train.py prepare` fetches all of this into `$WW_ROOT/data` once, pinned by
revision and checked by size and SHA-256 where the source publishes one.

| Data | Source | Size | Licence |
|---|---|---|---|
| ACAV100M features, 2,000 h, training negatives | `davidscripka/openwakeword_features` @ `985bf1b` | 17.3 GB | CC BY-NC-SA 4.0 |
| Validation features, ~11 h | same | 185 MB | CC BY-NC-SA 4.0 |
| Room impulse responses, 270 | `davidscripka/MIT_environmental_impulse_responses` @ `b824a1e` (Traer and McDermott, 2016) | 8 MB | none stated |
| AudioSet balanced train, shards 00 to 02, 1,500 clips of 10 s | `agkphysics/AudioSet` @ `0c609e8` | 2.1 GB | labels CC BY 4.0; the audio is from YouTube and belongs to its uploaders |
| Free Music Archive, 240 tracks of 30 s from `fma_small` | `os.unil.cloud.switch.ch/fma/fma_small.zip`, read by range requests | ~240 MB read of a 7.7 GB archive | each track under its artist's licence, mostly Creative Commons; metadata CC BY 4.0 |
| LibriTTS-R generator (in the image) | piper-sample-generator v2.0.0 release | 204 MB | trained on LibriTTS-R, CC BY 4.0; code MIT |
| Feature models (in the image) | openWakeWord v0.5.1 release | 2.4 MB | CC BY-NC-SA 4.0 |

The image also carries espeak-ng inside `piper-phonemize` (GPL-3.0).

**The trained models inherit these terms.** They are trained on
CC BY-NC-SA 4.0 features and only work in front of the CC BY-NC-SA 4.0
feature models, so treat them as CC BY-NC-SA 4.0, like openWakeWord's own
pre-trained models: non-commercial use, with attribution, shared on the same
terms. `THIRD-PARTY-NOTICES.md` at the repository root covers the hub's side.

## Using a model in the hub

Copy `models/<name>.onnx` into the hub's wake word directory; the hub loads a
custom model by name as `<model_dir>/<name>.onnx`. The feature models it
already has are the same files, by hash, as the ones used here.
