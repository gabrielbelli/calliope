# ADR 0024 — An always-on Linux GPU runner, beside offpeak rather than instead of it

**Status:** accepted
**Date:** 2026-10-02
**Builds on:** [ADR 0007](0007-two-lanes-not-three-rungs.md), whose lanes this
adds to: one lane per runner, each one job wide, chosen per job by arithmetic.

## Context

tts-long speaks on the NAS CPU at 0.230x realtime (baseline Chatterbox, measured)
and borrows offpeak's desktop RTX 3070 when nobody is using it. A GTX 1060 6 GB
on another Linux host is free all the time. offpeak is Windows-only and built
to yield; nothing ran tts-long's engines on an always-on Linux card.

## Decision

A separate image, `calliope-tts-runner`, built from `services/tts-long`
(`Containerfile.runner`, `app/runner/`), answers offpeak's runner protocol and
runs tts-long's own `Synth` on CUDA. tts-long's client is unchanged.

| # | Question | Decision |
|---|---|---|
| 1 | Where the code lives | In tts-long, because it runs the same `Synth` the CPU lane runs, given `device="cuda"` |
| 2 | Protocol | offpeak's, limited to the seven routes tts-long calls plus `/v1/status` |
| 3 | Process model | A server that never imports torch, and one worker process per loaded engine. The dispatcher thread is the only thing that stops it: idle, engine switch, CUDA error or shutdown. Process exit is the only way to free all of the VRAM |
| 4 | Concurrency | One job at a time, at most four queued |
| 5 | TLS | Always on, TLS 1.3 only, a self-signed P-256 certificate minted into `/state/tls` and pinned by tts-long |
| 6 | Key | The runner's own, from a file, at least 32 characters, compared in constant time by a pure ASGI guard before any body is read |
| 7 | Network | The host firewall, and a port published on one IPv4 address |
| 8 | Image | `python:3.12-slim-trixie` with PyPI's `torch==2.6.0`, which on amd64 is the CUDA 12.4 build with Pascal's sm_60 kernels. amd64 only. No CUDA base image |
| 9 | Engines | `chatterbox` and `chatterbox-turbo`, one in VRAM at a time. Not Voxtral |
| 10 | Durability | Jobs in memory, wiped at restart; clips kept under their digest |
| 11 | Orphans | A job nobody has polled for 60 s is cancelled |
| 12 | Free-memory gate | With nothing loaded, refuse work while the card has less free than the larger engine's measured peak plus 256 MiB |

**Both runners, and the free one gets the job (the owner's decision, 2 October
2026).** The first plan pointed tts-long at one runner at a time, which would
have given up the 3070 to gain the 1060. Instead tts-long takes one or more
runners: `TTS_RUNNER_*` for the first, `TTS_RUNNER2_*` for the next, each with
its own host, port, fingerprint, label, rate seeds and secret
(`TTS_RUNNER2_API_KEY`, with its own allowed hosts). Each is a lane. A job goes
to the free lane that would finish it first by its own measured rate, so the
faster runner wins while both are free, the other takes it while one is busy or
gated, and the NAS CPU takes it when neither is free or neither is clearly
better. A runner that goes away mid-job hands the job back under the rules one
runner always followed, per runner. `/health` lists every runner under
`runners`, and the page draws one card each.

## The measurement this decision waits on

The go rule, per engine, is `r_gpu ≥ 1.3 × r_cpu`: while the NAS lane is idle,
the dispatcher sends a job to a runner only if `(W / r_gpu + 8) × 1.25 < W /
r_cpu`, which for 60–300 s of speech needs about 1.27–1.35 times the CPU's
rate. With today's NAS figures that is about 0.57–0.61x for Turbo and
0.26–0.30x for baseline.

| Engine | NAS CPU | RTX 3070 (offpeak) | GTX 1060 (this runner) | Device peak on the 1060 |
|---|---|---|---|---|
| `chatterbox` | 0.230x, measured | 0.70x | not measured yet | not measured yet |
| `chatterbox-turbo` | 0.45x, a seed never measured | 1.54x | not measured yet | not measured yet |

The operator measures the card with `python -m app.runner smoke` (see
`services/tts-long/RUNNER.md`, step 3) and the NAS with the same five segments,
writes the four rates and both device peaks here, sets
`TTS_REALTIME_FACTOR_RUNNER2*` to the card's rates, and sets
`RUNNER_MIN_FREE_MIB` from the larger peak. The pass mark for VRAM is 5.4 GiB.

## Consequences

- With both runners configured nothing is given up: the 3070 is still used
  whenever it is free and faster, and the 1060 takes what it would otherwise
  have queued behind or sent to the CPU.
- Baseline may not clear the go rule on Pascal. Then baseline uses the 1060
  only while the NAS lane is busy, which is the arithmetic working.
- One more image: about a 3.5 GB pull and 6.5–7 GB on disk, built only when
  the files it contains change, amd64 only.
- Each runner has its own key, so a compromise of one machine cannot call the
  other, and offpeak's clip store stays offpeak's.
