# ADR 0011 — The Mac app is a local gateway, and Calliope is one backend behind it

**Status:** accepted
**Date:** 2026-09-18
**Amends:** GAB-635, which cut the remote server path out of the reader. That
decision stands and this record depends on it: what the reader does is still
local by construction. What is new is a **second surface** on the same app,
which may route.
**Related:** [ADR 0001](0001-openai-api-compatibility.md), whose API shape this
reuses rather than inventing another one.

## Decision

`clients/macos-player` becomes **Calliope.app**, a menu-bar application that
holds the reader and serves an OpenAI-shaped HTTP API on loopback. It speaks
with three tiers of engine, and a Calliope server is the third of them and is
optional.

| Tier | Engine | Voices | Disk | Needs the network |
|---|---|---|---|---|
| 1 | macOS `AVSpeechSynthesizer` | **187 across 51 locales** | **0** | no |
| 2 | Kokoro, ONNX on the CPU | 54 across ~8 languages | already installed | no |
| 3 | Calliope | cloned voices, Chatterbox, Turbo | — | **yes** |

Measured on the owner's machine, 2026-09-18: `say -v '?'` lists 187 voices
across 51 locales, five of them pt-BR, and **zero** Enhanced or Premium among
them. Tier 1 therefore earns its place on **coverage and cost**, not on
quality: it says Kannada, Swedish and fr-CA, which Kokoro cannot, at no
download. It does not replace tier 2 for English.

## The rule that keeps GAB-635 true

There are two layers and the boundary is structural rather than configured.

```text
hotkey · OpenClip · capsule ─────────────┐
                                         ▼
                              ┌──────────────────────┐
  CLI · Claude · HTTP ──► gateway         core       │
      127.0.0.1:47815   │  routes    tiers 1 and 2   │
                        └────┬─────────────────────┬─┘
                             │                     │
                       Calliope, when a        run log,
                       model asks for it       queued offline
```

**The capsule calls the core directly and never its own gateway.** Routed
through it, a server that has stopped answering would hang the read-aloud
hotkey — which is exactly the failure that made the reader local-only in the
first place. Written this way, "the reader works with the network down" is a
property of the shape rather than of a setting somebody can get wrong.

## Four decisions, and what was rejected

**Unreachable means 503, named. Never a substitution.**
A request for `chatterbox` when the server is down answers
`backend_unreachable` and says which backend and since when. It does not quietly
speak in a different voice. A cloned-voice request answering in a stranger's
voice with no signal is the kind of defect that is only noticed after it has
been sent to somebody. A caller who genuinely wants a fallback may ask for one
by name, and an unknown value there is refused by name, per the house rule
already stated in `services/stt/app/openai_api.py`.

**`/v1/models` always lists everything.** Remote models stay in the list when
the server is down, with `owned_by` naming the backend. Clients cache model
lists, and an entry that vanishes reads as a configuration error and sends
somebody digging; a 503 carrying a sentence does not.

**The reachability probe is cached and off the request path.** This is
[GAB-633's D3](0007-two-lanes-not-three-rungs.md) arriving on a second machine:
`/health` asked the runner synchronously, and a runner that had stopped
answering stalled the service and its own healthcheck with it. The same mistake
is available here and is refused in advance.

**Long jobs are proxied, never queued locally.** `POST /jobs`, the poll and the
fetch are forwarded. One run log, one place to debug, no second store to
reconcile. When the server is down a job cannot be submitted — which is honest,
because it could not have run either.

**The API binds loopback and is on by default.** Nothing off the machine can
reach `127.0.0.1`, so there is no key to issue and no firewall rule to add.
Off by default would mean `calliope speak` fails until somebody finds a
checkbox, which is a setup step that exists only because an earlier one was
skipped. One setting turns it off.

**The upstream key lives in the Keychain, and is its own key.** A laptop that
walks away should not hand over the credential the web UI uses. See GAB-632.

## The integration is three things, and engines are the weakest of them

The obvious half is remote models. The other two cost almost nothing because
the protocol already exists and three services already speak it.

1. **Engines.** Cloned voices and Turbo need the card. Proxied.
2. **Configuration.** Calliope serves `/glossaries`, and the vocabulary
   profiles edited once in its UI can boost transcription on the Mac. Cached on
   disk, so they survive the server going away.
3. **History.** `services/stt` and `services/tts` already set `RUNLOG_URL` and
   report every run to `tts-long`. **The app becomes another reporter**, queuing
   records locally and flushing them when the server answers, so the Jobs tab
   holds one history across both machines.

## Simplicity is the constraint, not an aspiration

A KISS pass over the first draft of this design deleted more than it kept.

| First draft | Shipped shape | Why |
|---|---|---|
| Six opt-in toggles | **One** | They were never independent. "Connected to Calliope" decides all of them. |
| A separate daemon, a `launchd` job, an update path | **The app is the process** | A menu-bar application is already long-running and already has a login item. |
| A voice picker listing 241 entries | **No picker in the default path** | It already detects the language and chooses. The list belongs in settings. |
| Speech-to-text in the first version | **Deferred** | Raycast covers dictation on this machine. The endpoint stays in the design, proxied. |

**Shortest path to first success: one action.** Open the app. Select text, press
the hotkey. No server, no key, no configuration file. Connecting to Calliope
adds two: paste a URL, paste a key.

The whole of the remote configuration is one switch:

```text
☐ Connect to a Calliope server
     URL   https://orko.gabrielbelli.com:30080
     Key   ••••••••                         [Test]

     Adds cloned voices and Chatterbox, uses your
     vocabulary profiles, and records runs in
     Calliope's history.
```

Splitting that into separate switches for the proxy, the glossaries and the
history is the change this pass exists to refuse. It can be split the day
somebody wants one without the others, and not before.

**Personal Voice is shown and never prompted for.** macOS requires an
authorisation prompt to use it. If one exists it appears among the voices, and
the prompt fires when it is chosen rather than at launch. It is also the only
cloned voice in this design that works with the network down, since it needs no
GPU.

## A voice is deployment data, not repository data

**The names of cloned voices, and the reference clips behind them, do not go in
this repository.** Not in a README, not in an ADR, not as a test fixture, not as
an example in a docstring. They are a person's own recordings and the names they
chose for them, and a public repository is not where either belongs.

What may be written down is the shape: a voice is a reference clip addressed by
the SHA-256 of its bytes, held in a directory the deployment mounts, and named
by whoever put it there. Preset voices that ship with an engine -- Kokoro's 54,
OpenAI's `alloy` and `fable` -- are catalogue entries and carry no such
restriction.

This matters to the client, which lists what the server offers. It displays
those names; it does not log them anywhere that leaves the machine, and it does
not send them to the run log as anything but the identifier the request already
carried.

## What this does not decide

**Whether to ship a local recogniser.** Tier 1 and tier 2 cover speech out.
Speech in is proxied for now. When it is revisited, macOS 27's
`SpeechTranscriber` is measured **first**: it is managed by the operating system
and costs nothing on disk, against FluidAudio's Parakeet at 461 MB. Neither has
been measured here, and this project does not ship an engine on a vendor's
number.

**Whether tier 1 should ever compete on quality.** No Enhanced or Premium voice
is installed on the machine this was measured on. Downloading them is one panel
in System Settings and changes tier 1 from a coverage answer to a quality one.
That is the owner's choice and not a thing the app should do on their behalf.

## Consequences

- The reader keeps working with no network, and that is now a structural
  property rather than a promise.
- Anything that wants speech on this Mac — the hotkey, OpenClip, the CLI, an
  agent — has **one address** and one protocol, whether the answer comes from
  the operating system, from Kokoro, or from a NAS.
- Calliope gains a client that reports its runs, so the history stops being
  server-only.
- The app is useful before Calliope exists, which is the test of whether tier 1
  and tier 2 were worth having.
