# macos-player

Select text in any app, click **Speak** in [OpenClip](https://github.com/ganeshmshetty/openclip),
and a floating Liquid Glass capsule reads it aloud with Kokoro.

```text
OpenClip "Speak"  ──text file──►  calliope-player (Swift)        ──POST /v1/audio/speech──►  server/server.py :47815
                                  detects the language                 response_format pcm          Kokoro-82M, ONNX, CPU
                                  splits sentences                                                   started on demand,
                                  ◀◀ ❙❙ ▶▶  − speed +  ✕                                             exits after 15 min idle
```

It is a client of the same contract as `services/tts`, not a second implementation of it:
the player sends OpenAI's body with `response_format: "pcm"` and plays headerless 24 kHz
16-bit mono. It speaks that contract only to `127.0.0.1`. The Calliope stack is not an option
and is not meant to be one: the model runs faster than realtime on this Mac's own CPU, so a
server on the network would add a URL, a key and an outage for nothing.

## Layout

| Path | What |
|---|---|
| `player/main.swift` | The player: language detection, sentence splitting, playback, the capsule |
| `player/defaults.swift` | The two saved settings, and the carry from the pre-rename suite |
| `server/server.py` | A local server answering the subset of `services/tts` the player uses |
| `openclip/` | The OpenClip extension: one **Speak** action |
| `install.sh` | Builds and installs all three |

Built files and the model live outside the repository, in `~/.local/share/calliope`
(Python 3.12 venv, `kokoro-v1.0.onnx`, `voices-v1.0.bin`, `calliope-player`, `server.py`).

## Install

```bash
clients/macos-player/install.sh
```

Needs macOS 26 (the capsule is `NSGlassEffectView`), the Command Line Tools, `uv`, and
OpenClip with Accessibility permission. Re-run it after changing anything here.

It was called Kokoro before. A first install under the new name reuses the model files from
`~/.local/share/kokoro-tts`, then removes that directory and the `kokoro.openclipext`
extension, so the menu keeps one **Speak**. Your speed and reader setting are carried from
`com.gabrielbelli.kokoro-player` into `com.gabrielbelli.calliope-player` on the first run; the
old suite is left alone.

## Behaviour worth knowing

- **Language** comes from `NLLanguageRecognizer` over the whole selection. Below 0.5
  confidence (`ok`, a lone command) it falls back to English. Each language maps to one voice
  (`af_heart`, `pf_dora`, `ef_dora`, `ff_siwis`, `if_sara`, `hf_alpha`, `jf_alpha`, `zf_xiaobei`).
- **Speed** is applied while playing (`AVAudioUnitTimePitch`, 0.75×–3×), not sent to Kokoro,
  so it changes instantly without re-synthesising. The last speed is remembered.
- **First sound**: about 0.3 s for a short sentence with the server warm; about 4 s when the
  server has to start. Sentences over 140 characters are split at clause punctuation so a
  long first sentence does not hold up the start.
- **Reader**: the speech-bubble button grows the capsule upwards into a box showing the whole
  selection as it was selected — paragraphs and line breaks intact, one text size. Words already
  read are bright, the rest dimmed, and an underline sweeps across each word for as long as it
  is spoken. The box scrolls when the reading moves to a new line, keeping it in the middle, and
  clicking a word jumps to its sentence. The controls row keeps its width and screen position
  while the box grows around it, so nothing moves under the pointer. Each chunk keeps the UTF-16
  ranges of its words in the original text, which is how the underline finds them. The local server
  returns `X-Word-Timings` (one `[start, end]` per whitespace-separated word of `input`), built
  from the duration output of `kokoro-v1.0.onnx`: phoneme timings grouped at spaces, with each
  word phonemised on its own when numbers or abbreviations expand ("42" is two spoken words).
  Without a usable header the player estimates from word lengths instead.
  The position comes from the player node's sample time, which counts source frames, so the
  highlight stays aligned at any speed. The panel's state is remembered.
- **Accents**: text is normalised to NFC in the player and again in the server. Selections can
  arrive decomposed (`e` + U+0301), and espeak drops a lone combining mark: `avó` is then read
  as `avô`, `é` as `e`, and `não` loses its nasal vowel. `services/tts` does not normalise.
- **One player at a time**: a new Speak replaces whatever is playing. The server survives that.
- **Measured on an M2 Max**: the full model runs at about 4.9× realtime on the CPU. The `int8`
  model was 3× slower and ONNX Runtime's CoreML provider no faster, so neither is used.
- **Temp directories**: phonemizer copies `libespeak-ng.dylib` into a new temp directory per
  process and removes it only on a normal exit, so the server turns SIGTERM into one.
