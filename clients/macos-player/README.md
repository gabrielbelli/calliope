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
learns the difference. The `calliope` command and any other program on this Mac use the same
address, so an agent can read an explanation aloud through the voices you chose.

## Layout

| Path | What |
|---|---|
| `bundle/*.plist` | The two identities: `com.gabrielbelli.calliope` and `…calliope.player` |
| `shared/paths.swift` | Where everything is, said once and compiled into all three binaries |
| `shared/preferences.swift` | The settings both halves read: voices per language, the engines, the port |
| `daemon/main.swift` | The daemon: the menu bar item, the hotkey, the proxy it supervises |
| `daemon/settings.swift` | Settings: General, Voices, Engines, Proxy, Integrations |
| `player/main.swift` | The player: language detection, sentence splitting, playback, the capsule |
| `player/defaults.swift` | The carry from the pre-rename suite |
| `server/server.py` | The proxy on loopback, and the Kokoro engine it starts on demand (`--engine`) |
| `cli/` | The `calliope` command: speak, save, transcribe, voices, status |
| `skill/calliope-voice/` | An agent skill that writes an explanation and reads it aloud |
| `openclip/` | The OpenClip extension: one **Speak** action, through `calliope speak` |
| `install.sh` | Builds and installs all of it |

It installs as an application, `/Applications/Calliope.app`:

```text
Calliope.app/Contents/
  MacOS/calliope-daemon                                  the menu bar, the hotkey, Settings
  Helpers/CalliopePlayer.app/…/calliope-player           one process per passage
  Resources/server.py                                    the proxy, and the engine it starts
  Resources/cli/calliope                                 the command, linked to ~/.local/bin
  Resources/skill/calliope-voice/SKILL.md                a skill file, never installed
  Resources/openclip/                                    what Settings reinstalls OpenClip's Speak from
```

Everything that changes stays outside it, in `~/.local/share/calliope`: the Python 3.12 venv,
`kokoro-v1.0.onnx`, `voices-v1.0.bin`, the logs, the queue. **A signed bundle must not be
written to**, and a re-install must not download 310 MB again — so code is inside and state is
outside, and `shared/paths.swift` is the one place that says which is which.

It is an application rather than two loose binaries because it had to become one. Without a
bundle identifier `SMAppService.mainApp` reports `notFound`, `register()` throws, and **Open at
Login is a control that does nothing**. There is also no identity for macOS to grant
Accessibility to and nothing for Gatekeeper to check — which is what shipping it anywhere else
requires.

## Install

```bash
clients/macos-player/install.sh
```

Needs macOS 26 (the capsule is `NSGlassEffectView`), the Command Line Tools, `uv`, and
OpenClip with Accessibility permission. Re-run it after changing anything here.

Two environment variables, both optional:

| Variable | Default | For |
|---|---|---|
| `CALLIOPE_APP_DIR` | `/Applications` | Installing somewhere else; the extension follows |
| `CALLIOPE_SIGN_IDENTITY` | `-` (ad-hoc) | `Developer ID Application: …`, for a notarisable build |

> Ad-hoc is enough to run on the machine that built it and not enough to run anywhere else.
> A Gatekeeper-clean download needs a Developer ID identity and notarisation.

The deployment target is pinned to `arm64-apple-macos26.0`. Without `-target`, `swiftc` stamps
the binary with a minimum inferred from the build host — measured as `minos 28.0` on macOS
27 — and LaunchServices then refuses to open the app at all (`-10825`,
`kLSIncompatibleSystemVersionErr`). Running the binary directly bypasses LaunchServices and
works, so this is invisible until somebody double-clicks it.

It was called Kokoro before. A first install under the new name reuses the model files from
`~/.local/share/kokoro-tts`, then removes that directory and the `kokoro.openclipext`
extension, so the menu keeps one **Speak**. Your speed and reader setting are carried from
`com.gabrielbelli.kokoro-player` into `com.gabrielbelli.calliope-player` on the first run; the
old suite is left alone.

## Behaviour worth knowing

- **Language** comes from `NLLanguageRecognizer` over the whole selection. Below 0.5
  confidence (`ok`, a lone command) it falls back to English.
- **Voices** are chosen per language in Settings › Voices, for the languages you add; any
  other language is read with its standard voice (`af_heart`, `pf_dora`, `ef_dora`,
  `ff_siwis`, `if_sara`, `hf_alpha`, `jf_alpha`, `zf_xiaobei`). A language is offered only the
  voices whose first letter is that language, because Kokoro derives its phonemiser from the
  letter. Each choice names its side, `mac/pf_dora` or `calliope/pf_dora`: the voice decides
  where it is spoken.
- **Speed** is applied while playing (`AVAudioUnitTimePitch`, 0.75×–3×), not sent to Kokoro,
  so it changes instantly without re-synthesising. The last speed is remembered.
- **First sound**: about 0.3 s for a short sentence with the model loaded; about 2 s more when
  it has to load first (see *Engines and footprint*). Sentences over 140 characters are split at clause punctuation so a
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

## Engines and footprint

Settings › Engines has two switches, **This Mac** and **Calliope server**. Each language's voice
comes from either side, and runs there.

| What is running | Memory, measured | When |
|---|---|---|
| The daemon (menu bar, hotkey) | about 56 MB | always |
| The proxy, `server.py` | 17–19 MB | always; standard library only |
| The Kokoro engine, `server.py --engine` | 430–580 MB | only while it is used |

**The model is not resident unless you ask for it.** Measured: freeing Kokoro inside a process
returns nothing to the system (509 MB loaded, 508 MB after `del` and a collection), so the
proxy runs the model in a child process and lets that child exit after 10 minutes without a
word to say. The next word loads it again, in about 1.5–2 seconds. **Keep the voice loaded**
keeps it in memory for an instant start. With This Mac switched off the engine never starts;
with the Calliope server switched off nothing leaves this Mac.

## The proxy

Other programs on this Mac can use Calliope at one OpenAI-compatible address,
`http://127.0.0.1:47815` (Settings › Proxy changes the port):

| Route | Answered by |
|---|---|
| `POST /v1/audio/speech`, model `kokoro`, `tts-1`, `tts-1-hd` | This Mac; `pcm` or `wav` |
| `POST /v1/audio/speech`, model `calliope/<id>` | The Calliope server, as `<id>` |
| `POST /v1/audio/transcriptions`, `/translations` | The Calliope server |
| `/jobs`, `/jobs/{id}`, `/jobs/{id}/audio` | The Calliope server (long-form voices answer with a job) |
| `GET /v1/models`, `GET /voices` | Both sides, each row saying which |
| `GET /status`, `GET /calliope/test` | The proxy: what is loaded, and a fresh round trip |

The key stays in the proxy: a program asks `127.0.0.1` with no credential, and the proxy adds
the key on the way to the server. A request with an `Origin` header -- which every browser
sends cross-site -- is refused on every route but `/health`, so a web page cannot spend your
voices or your key.

## The `calliope` command

```bash
calliope speak --reader -f explanation.md     # read a file aloud, with the reader open
calliope speak "Hello there"                  # or text
calliope save -f notes.md -o notes.wav        # an audio file instead
calliope transcribe meeting.m4a               # needs the Calliope server
calliope voices --language pt                 # what can speak Portuguese, on which side
calliope status --test                        # what is running, and a test of the server
```

Markdown is read as prose: code blocks, link targets, images and markup are left out, and
headings and list items end as sentences. `install.sh` links the command into `~/.local/bin`;
Settings › Integrations can do the same for an app installed another way.

**For agents.** `skill/calliope-voice` is a small skill for Claude Code and similar agents:
asked to say something out loud, the agent writes the explanation for the ear as Markdown,
saves it where you asked (or in the temporary directory) and plays it with `calliope speak`.
It is a `SKILL.md` and nothing installs it: use it with whichever agent you like, however you
keep your own setup. Settings › Integrations › **Show Skill File** shows where it is.

**OpenClip.** Settings › Integrations › **Reinstall Speak Action** puts the extension back from
the copy inside the app, with the app's own location written into it -- the repair for a
missing, stale or hand-edited one. OpenClip trusts an extension by a hash of its files, so it
asks once more afterwards.

## The Calliope server (optional)

Menu bar icon → **Settings…** → **Engines** → **Calliope server**. Off is the resting state;
on reveals an address and a key, and turning it back off leaves both alone. The fields commit
on Return or on leaving them -- there is nothing else to press. **Test Connection** asks the
proxy to check the server afresh, in order, stopping at the first failure: reachable, key
accepted, voices listed, a word spoken. The same test runs after every change that completes
the address.

**The key is required.** A Calliope server answers nothing but its liveness without one. Make
it in Calliope under *Account* → *API keys* → *New key*: the preset `user` covers speech and
transcription, and `user-jobs` adds long documents, which answer with a job to poll. An
address without a key is not used: the settings say a key is required, and `server.py`
started with `CALLIOPE_URL` but no `CALLIOPE_KEY` says so once and forwards nothing.

Filled in, every model the server lists is offered here as `calliope/<id>`. `GET /v1/models`
lists both sides, with `owned_by` saying which each comes from, so one address covers every
engine and nothing that talks to `127.0.0.1` needs to know where a voice actually ran.

Three things are deliberate:

- **The key is in the Keychain**, not in the preferences plist, which rides in every backup.
  The daemon is the only process holding both halves and passes them to `server.py` in its
  environment -- so a server started by the one-shot player has no remote at all, and the
  Kokoro engine the proxy starts is never given the key.
- **The certificate is verified** whenever the address has a name in it. An address typed as a
  bare IP cannot be verified by any certificate, so that one case is trusted on the strength
  of being your own network.
- **An unknown model is refused here**, with both lists in the error, rather than forwarded.
  A gateway answers a name it does not know with a default voice, which turns a typo into
  audio nobody chose.
