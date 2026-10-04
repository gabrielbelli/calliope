# ADR 0016 — Several speech-to-text engines side by side, picked by `model`

**Status:** accepted
**Date:** 2026-09-28

## Context

stt-stack loaded one recogniser per deployment, chosen by `STT_MODEL`, and
`model` on a request chose nothing. The stack's README gave the reason as a
deviation: Parakeet needs 1.4 GB resident and Whisper large-v3 2.9 GB, both
did not fit the memory the stack was deployed under, and a cold load takes
minutes. `whisper-1` was accepted and answered by whatever was loaded, and
`x-stt-engine` said which engine ran.

Satellite commands in Brazilian Portuguese changed the cost of that rule.
Parakeet v3 detects its language, takes no hint, and on short commands
through a cheap microphone it did badly. A Brazilian Portuguese fine-tune of
the same model exists in ONNX
(`alefiury/parakeet-tdt-0.6b-v3-ptBR-TAGARELA-onnx`), but it is useless for
English. A household that speaks both needs both engines at once, and a
request has to be able to say which one it wants.

Measured on the project's CPU host on 28 Sep 2026, word error rate on
Brazilian Portuguese, the fine-tune against Parakeet v3:

| Audio | `parakeet-pt-br` | `parakeet` |
|---|---|---|
| Spoken commands | 0.224 | 0.436 |
| The same commands through a cheap microphone | 0.195 | 0.523 |
| CORAA, spontaneous speech | 0.110 | 0.219 |
| Commands after an English-sounding wake word | 0.129 | 0.313 |
| Median time per command | 355 ms | 410 ms |

Parakeet heard up to 9% of those clips as English, and the fine-tune heard
none. On English the fine-tune's word error rate was 0.89.

## Decision

`STT_MODELS` names the engines one process loads, in order, from a fixed
table in `services/stt/app/asr.py` (`ENGINES`): `parakeet`, `parakeet-pt-br`
and `whisper`. The first is the default. A request's `model` picks an engine
when it names one that is loaded. Any other value, `whisper-1` included, gets
the default. `/transcribe` has no `model` field and always gets the default.
With `STT_MODELS` unset, nothing changes: one engine, chosen by `STT_MODEL`.

Every `/v1` response carries `x-stt-engine`, the family that ran (`parakeet`
or `whisper`, which decides what a request may send), and `x-stt-model`, the
id of the engine. `GET /health` lists every loaded engine under `models`,
with its id, family, whether it is the default, the languages it hears, and
whether it takes `language`, `boost`, translation and streaming. The
top-level `translations` and `streaming` are true when any loaded engine can.
A refusal names the engine that would accept the field.

## Why `whisper-1` stays the default engine

An OpenAI client that nobody configured sends `whisper-1`. Sending it to
Whisper on a deployment that loaded Whisper as a second engine would give
that client the slowest engine, by an order of magnitude, because of a
default it did not choose. The default engine is the operator's choice, and
`model=whisper` is one field away.

## Canary 1B v2 was measured and rejected

onnx-asr loads Canary 1B v2, and Canary takes a language, which Parakeet v3
does not. On the same host it ran at 3.2× realtime against Parakeet's 9.4×,
it was no more accurate on Brazilian Portuguese commands, and on unclear
speech its autoregressive decoder looped ("falei, falei, …") for 48 s on a
3 s command. A voice assistant cannot wait for that, so it is not in
`ENGINES`.

## What it costs

- **Memory is the sum of the engines.** `parakeet,whisper` is about 4.3 GB
  before a clip, and the fine-tune's weights alone are 2.4 GB in fp32. Its
  resident size has not been measured. All three together do not fit the
  6 GB limit `compose.yaml` gives stt-stack.
- **A second model to download** into the models volume on first start. The
  fine-tune is CC BY 4.0, like Parakeet v3 (THIRD-PARTY-NOTICES.md).
- **English is unusable on the fine-tune.** It is offered for Portuguese
  alone, and the stack does not stop an operator who makes it the default of
  a household that also speaks English.
- **`STT_MODEL_ID` and `STT_QUANTISATION` do not apply under `STT_MODELS`.**
  Each engine in the table has its own checkpoint and quantisation.
  `STT_LANGUAGE` still applies to a Whisper loaded this way.

## Consequences for the other components

- **The gateway** routes every transcription to stt-stack whatever `model`
  says. Its `GET /v1/models` is a static table: it lists `parakeet` and
  `whisper-1`, not `parakeet-pt-br` or `whisper`. A client that needs to know
  which engines a deployment loaded reads `GET /health`.
- **The Home Assistant integration** builds one speech-to-text entity from
  each entry of `models`, and keeps each entity on its engine when the
  default changes.
- **The satellite hub** reads `models` before its first command, and again
  every 10 minutes (every 30 s while the stack does not answer). A wake word
  whose language hint is the one language an engine was loaded for goes to
  that engine by its id, so with `STT_MODELS=parakeet,parakeet-pt-br` a `pt`
  word is heard by the fine-tune. Every other command gets the default.
- The deviation "`model` does not choose an engine", in the stack's README
  and in the root README, is replaced by this rule.

## Rejected

- **Refusing a `model` the deployment did not load.** It would reject every
  unconfigured OpenAI client, which is the reason `whisper-1` was accepted in
  the first place.
