# Third-party notices

This repository is BSD-2-Clause (see `LICENSE`). This file records what came
from elsewhere, under what licence, and in what form — because "we learned it
from X" and "we copied X" carry different obligations, and the difference is
easy to lose once the code is in the tree.

Three categories, deliberately kept apart:

| Form | What it means | What is owed |
|---|---|---|
| **Depended on** | Called over the network or installed by pip. Not a derivative work. | Nothing beyond honouring the dependency's own terms |
| **Copied** | Their source, verbatim or lightly edited, in our files | Their copyright and permission notice must travel with it |
| **Learned from** | An idea, a layout, a parameter range, a sequence of steps | Nothing legally. Credited here anyway |

---

## Depended on

### MeTube — `alexta69/metube` — **AGPL-3.0**

Used for URL ingestion. **`services/ui/app/metube.py` is an HTTP client and
contains none of MeTube's code**, and that is a deliberate licence decision
rather than a style one.

AGPL-3.0's §13 obliges an operator who *modifies* the program and lets users
interact with it over a network to offer them the modified source. Calling a
separate program's network API is not a derivative work, and the operator here
runs the published image unmodified on his own machine. Forking or vendoring
MeTube would have been a materially different answer, and would have pulled
this repository's UI service into AGPL territory.

**If anyone later vendors, patches or embeds MeTube, that reasoning stops
holding.** Re-read this section first.

### pip dependencies

| Package | Version | Licence |
|---|---|---|
| yt-dlp | 2026.8.19 | Unlicense (public domain) |
| fastapi | 0.121.2 | MIT |
| uvicorn[standard] | 0.38.0 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| websockets | 17.1 | BSD-3-Clause |
| numpy | 2.3.4 | BSD-3-Clause |
| python-multipart | 0.0.20 | Apache-2.0 |

yt-dlp is used **as a metadata probe only** — `extract_info(download=False)`,
to resolve a pasted link to a title, duration and size so the user can confirm
before anything is fetched. The fetching itself is MeTube's job.

### pip dependencies of the satellite hub (`services/satellites`)

In addition to fastapi, uvicorn, httpx and numpy above. Pinned in
`services/satellites/requirements.txt`, except openwakeword, which is in
`requirements-nodeps.txt` and installed with `--no-deps` (its metadata still
asks for tflite-runtime, which has no wheel for Python 3.13).

| Package | Version | Licence | What for |
|---|---|---|---|
| openwakeword | 0.6.0 | Apache-2.0 (the code; its models are not, see below) | wake words |
| onnxruntime | 1.30.0 | MIT | runs the wake word models |
| scipy | 1.18.1 | BSD-3-Clause | imported by openwakeword |
| scikit-learn | 1.9.1 | BSD-3-Clause | imported by openwakeword |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | imported by openwakeword |
| requests | 2.34.2 | Apache-2.0 | imported by openwakeword |
| webrtcvad-wheels | 2.0.14 | MIT (the wrapper); compiles in WebRTC's VAD, BSD-3-Clause, Copyright The WebRTC project authors | the endpointer |
| aiomqtt | 2.5.1 | BSD-3-Clause | Home Assistant over MQTT |
| paho-mqtt | 2.1.0 | EPL-2.0 OR BSD-3-Clause, taken as BSD-3-Clause | under aiomqtt |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | firmware signatures |
| py3langid | 0.4.0 | BSD-3-Clause, its model included | the language of a transcript |
| websockets | 17.1 | BSD-3-Clause | Home Assistant's Assist pipelines (`ha_assist`) |

MPL-2.0 (tqdm) is file-level copyleft: it binds changes to tqdm's own files,
and nothing here changes them.

**onnxruntime reports usage to Microsoft on Linux unless told not to.** Its
Linux wheel carries Microsoft's 1DS telemetry client, which posts to
`mobile.events.data.microsoft.com` and keeps a device id under
`~/.cache/Microsoft`. Measured in the satellites image with that host pointed
at the container's own loopback: two connections within 15 s of a session and
the id written, and with `ORT_DISABLE_TELEMETRY=1` neither. The Containerfile
sets it and `app/wakeword.py` sets it before anything imports onnxruntime. This
is a term of use rather than a licence one, and it is recorded here because a
hub that listens in a house must not phone home by default.

### openWakeWord's pre-trained models — **CC BY-NC-SA 4.0**, David Scripka

Not the code's licence. openWakeWord's README: *"All of the included
pre-trained models are licensed under the Creative Commons
Attribution-NonCommercial-ShareAlike 4.0 International license due to the
inclusion of datasets with unknown or restrictive licensing as part of the
training data."* That covers every model in its v0.5.1 release, the two shared
feature models (`melspectrogram.onnx`, `embedding_model.onnx`) included; the
embedding model reimplements Google's `speech_embedding`, which Google
published under Apache-2.0.

**The satellites image carries `hey_jarvis` and the two feature models**,
fetched at build time and checked against the SHA-256 pinned in
`app/wakeword.py`, so that a first start needs no network. The attribution
travels beside them as `/srv/models/NOTICE.md` (from
`services/satellites/MODELS-NOTICE.md`). What that means for anyone handling
the image:

- **Non-commercial only.** Running it at home is what it is for. Anyone who
  wants to use or ship the image commercially must build it without the models
  (`--build-arg WAKE_WORDS=` leaves `/srv/models` out entirely) and use models
  licensed for that.
- **Attribution** is the notice file; do not strip it from a derived image.
- **No changes are made to the files.** ShareAlike binds adaptations, and none
  is made; a model trained here from them would be one.

Other built-in names are fetched at start-up into `SATELLITES_MODEL_DIR` on the
data volume, from the same release and under the same licence.

---

### Satellite firmware (`clients/korvo-satellite`)

Fetched by PlatformIO at build time and linked into the firmware image; none of
it is in this tree. The two LGPL libraries are linked unmodified, and the whole
firmware is published here as source, so anyone can rebuild it against their
own copy of either.

| Library | Version | Licence |
|---|---|---|
| Arduino core for the ESP32 (`framework-arduinoespressif32`) | 2.0.17 | LGPL-2.1 |
| `tzapu/WiFiManager` | 2.0.17 | MIT |
| `links2004/WebSockets` | 2.6.1 | LGPL-2.1 |
| `bblanchon/ArduinoJson` | 7.4.2 | MIT |
| `adafruit/Adafruit NeoPixel` | 1.15.1 | LGPL-3.0 |

`src/ca.h` holds the ISRG Root X1 and X2 certificates, exported from the macOS
system roots; they are public trust anchors and carry no licence terms.

## Copied

Their code is in our tree, so their notice travels with it.

### `hasib41/meniscus-liquid-nav` — MIT — Copyright (c) 2026 Hasib

The dock — the bottom navigation whose top edge is a liquid surface, and whose
selected tab is a bead that surface dips beneath — is **ported from Meniscus**.
The geometry, the spring constants and the drag response are theirs and are
deliberately not reinterpreted. The four icons, the accents and the two themes
are this page's. Found through its listing at
`vibing.inc/library/meniscus-vg-2134`; upstream is
`github.com/hasib41/meniscus-liquid-nav`, with a live demo at
`hasib41.github.io/meniscus-liquid-nav/`.

All of it lives in `services/ui/app/static/ui.html`, in three places, each
already carrying a pointer back to the original:

| Where | What came from Meniscus |
|---|---|
| the dock CSS (`.rail`, `.dock`, the socket custom properties) | the plate, the rim and the bead |
| the `<div class="rail">` markup and its SVG `<defs>` | the one closed path the socket is cut into |
| `dockMeasure`, `dockTrough`, `meniscusTo` and the spring loop | the geometry, the spring and the drag |

It is one component in vanilla JS and SVG with zero dependencies, which is why
it could be taken at all — there was no framework attached to it, and nothing
to reconcile with a page that has no build step.

The notice below is reproduced in full at the top of `ui.html` as well, because
that is the file the code is actually in.

```
MIT License

Copyright (c) 2026 Hasib

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

### `xiph/speexdsp` `mdf.c` — BSD-3-Clause — Copyright (C) 2003-2008 Jean-Marc Valin

The echo canceller in `services/satellites/app/frontend.py` is ported from
Speex's MDF echo canceller (`libspeexdsp/mdf.c`): the foreground/background
two-path logic and its thresholds (`VAR1_UPDATE`, `VAR2_UPDATE`,
`VAR_BACKTRACK`), the leak estimate and its rates (`spec_average`, `beta0`,
`beta_max`, `MIN_LEAK`), and the learning-rate formulas from Valin's paper as
`mdf.c` implements them. Changed from the original: vectorised over microphones
in numpy, floating point only, one reference channel, and no DC notch or
pre-emphasis. BSD-3-Clause is compatible with BSD-2-Clause; mdf.c's own notice
is at the top of `frontend.py`, and in full here:

```
Copyright (C) 2003-2008 Jean-Marc Valin

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

1. Redistributions of source code must retain the above copyright notice,
this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
notice, this list of conditions and the following disclaimer in the
documentation and/or other materials provided with the distribution.

3. The name of the author may not be used to endorse or promote products
derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT,
INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```

---

### `espressif/esp-adf` `esp_codec_dev` — Apache-2.0 — Copyright 2023 Espressif Systems (Shanghai) CO LTD

The ES7210 and ES8311 register sequences in
`clients/korvo-satellite/src/codec.cpp` are ported from
`components/esp_codec_dev/device/es7210/es7210.c` and `es8311/es8311.c`,
specialised to the Korvo's one configuration. Apache-2.0 is compatible with
BSD-2-Clause for this use; the notice and a statement of what changed are at
the top of that file. The Korvo's pin assignments were read from Espressif's
published schematics and from esp-skainet's board header, which are facts
rather than code.

## Learned from

No code from these was copied verbatim. Credited because the ideas were load
bearing, and because a reader deserves to know where to look for the original.

### The satellite hub's front-end: papers, not code

`services/satellites/app/frontend.py` implements, from their publications:
J.-M. Valin, "On adjusting the learning rate in frequency domain echo
cancellation with double-talk" (2007), whose Speex implementation is under
**Copied** above; T. Gerkmann and R. C. Hendriks, "Unbiased MMSE-based noise
power estimation with low complexity and low tracking delay" (2012), for the
noise tracker; and the MVDR beamformer and decision-directed Wiener gain, which
are textbook. Silero, which openWakeWord can run as a VAD, was measured for
the endpointer and not used; `app/wakeword.py` says why.

### `devnen/Chatterbox-TTS-Server` — MIT — Copyright (c) 2025 devnen

The reference-audio upload **sequence** was transplanted in shape: sanitise the
filename, enforce an extension allowlist, write, validate the duration, unlink
on failure, return the refreshed list with per-file errors. Their
`server.py:670-753` is the original. Worth reading before changing ours.

### `resemble-ai/chatterbox` — MIT — Copyright (c) 2025 Resemble AI

Their Gradio demo supplied the **parameter table**, not code: exaggeration,
`cfg_weight`, temperature, `min_p`, `top_p`, `repetition_penalty` and their
ranges, plus the advice to set `cfg_weight` to 0 for cross-language transfer.

Note that our `services/tts-long` clamps exaggeration and `cfg_weight` to
`0.0–1.0`, so the demo's `0.25–2.0` exaggeration range is **rejected by our own
backend**. The ranges had to be reconciled, not copied.

### `speaches-ai/speaches` — MIT — Copyright (c) 2024 Fedir Zadniprovskyi

The closest prior art to this whole stack, and the reason `Calliope` is scoped
the way it is: it already does OpenAI-compatible STT and TTS over
faster-whisper and Kokoro. What it does *not* do — Parakeet, post-decode
glossary repair, a long-form job queue — is what justifies this repository
existing at all.

### `jamiepine/voicebox` — MIT — Copyright (c) 2026 Voicebox Contributors

A local-first voice studio: seven TTS engines, cloning, global dictation. It
hosts its own models and its OpenAI-compatible API is planned rather than
shipped, so it is an alternative to this stack rather than a client of it — but
its UX is worth studying, and it is MIT, so its code may be copied into this
repository provided the notice above travels with it.

Its `RESPONSIBLE_USE.md` is a policy document, not an additional licence
condition. It does not restrict the MIT grant. It is still the right position
on cloning a voice you do not own, and this project offers voice cloning.

---

## If you copy code in

MIT and BSD-2-Clause are compatible: both are permissive and both ask only that
the notice be preserved. So copying is allowed — silently dropping the notice
is not.

1. Put the origin's copyright line and permission notice at the top of the file
   holding the copied code, or in a clearly-labelled block around it.
2. Move that project from **Learned from** to **Copied** in this file, naming
   the file and the upstream path.
3. Say in the commit message what was taken and from where.
