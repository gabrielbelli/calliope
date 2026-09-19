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
16-bit mono, and it speaks that contract only to `127.0.0.1`.

That address can answer for more than this Mac. Left alone it is Kokoro and nothing else --
no URL, no key, no outage, because the model runs faster than realtime on this CPU and a
server on the network would buy nothing. Given a Calliope address in Settings, the same
address also answers for the engines this Mac has no business running, and the player never
learns the difference.

## Layout

| Path | What |
|---|---|
| `daemon/main.swift` | The daemon: the menu bar item, the hotkey, the warm server, Settings |
| `player/main.swift` | The player: language detection, sentence splitting, playback, the capsule |
| `player/defaults.swift` | The two saved settings, and the carry from the pre-rename suite |
| `server/server.py` | A local server answering the subset of `services/tts` the player uses |
| `openclip/` | The OpenClip extension: one **Speak** action |
| `install.sh` | Builds and installs all four |

Built files and the model live outside the repository, in `~/.local/share/calliope`
(Python 3.12 venv, `kokoro-v1.0.onnx`, `voices-v1.0.bin`, `calliope-player`, `calliope-daemon`, `server.py`).

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

## The Calliope server (optional)

Menu bar icon → **Settings…** → *Calliope server*. An address and a key; empty means
everything stays here.

Filled in, `server.py` becomes a proxy: `kokoro`, `tts-1` and `tts-1-hd` are answered on this
Mac as before, and anything else is forwarded to the Calliope gateway. `GET /v1/models` lists
both, with `owned_by` saying which side each comes from, so one address covers every engine
and nothing that talks to `127.0.0.1:47815` needs to know where a voice actually ran.

Three things are deliberate:

- **The key is in the Keychain**, not in the preferences plist, which rides in every backup.
  The daemon is the only process holding both halves and passes them to `server.py` in its
  environment — so a server started by the one-shot player has no remote at all.
- **The certificate is verified** whenever the address has a name in it. An address typed as a
  bare IP cannot be verified by any certificate, so that one case is trusted on the strength
  of being your own network.
- **An unknown model is refused here**, with both lists in the error, rather than forwarded.
  A gateway answers a name it does not know with a default voice, which turns a typo into
  audio nobody chose.

**Connect** saves, restarts the server and then asks the proxy what it can reach, so the
answer on screen is the round trip rather than a claim about it. An address that is down is
still saved — it may be up later.
