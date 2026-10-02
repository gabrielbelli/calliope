# ADR 0017 — Home Assistant's names are one glossary profile, written by the integration and named by the hub

**Status:** accepted
**Date:** 2026-09-28

## Context

What Assist can act on is named by the people who live with it: an area
called "Sala", a lamp called "Luz da cama", an alias added to a switch.
Parakeet was never trained on those names, and a name it mishears is a
command Assist cannot match. Two clients send such commands to stt-stack:
the Home Assistant integration, as the speech-to-text of an Assist pipeline,
and the satellite hub, which transcribes a satellite's command itself before
its `ha_conversation` or `ha_assist` action.

stt-stack already had named glossary profiles, chosen per request
([ADR 0002](0002-glossary-profiles.md)), a write API for them
([ADR 0003](0003-glossary-profile-api.md)), and decode-time biasing on
Parakeet ([ADR 0005](0005-parakeet-decode-time-biasing.md)). Those records
cover profiles that a person writes. This one covers a profile that a
machine writes and two other machines read.

## Decision

The integration keeps one profile, named `home-assistant`, on stt-stack. It
writes it with `PUT /glossaries/home-assistant` through the gateway, about
10 s after Home Assistant starts and 10 s after any change to floors, areas,
devices, entities or what is exposed to Assist, and checks it every hour. It
writes only when the text changed. The profile holds, in this order, the
floors and areas with their aliases, the names and aliases of the entities
exposed to Assist, and the command words of each language Assist is set to.

Both clients name the profile on a transcription to an engine that boosts
(Parakeet), with `boost=true` unless the stack has biasing off. Neither sends
it to Whisper.

**The name is a contract between two components.** It is spelled twice:
`GLOSSARY_PROFILE` in `clients/home-assistant/custom_components/calliope/const.py`
and `HA_GLOSSARY` in `services/satellites/app/router.py`. Changing one without
the other leaves the hub transcribing without the names, and nothing fails
loudly.

## The choices inside it

- **Bare terms only.** Every name is a line with no `heard =` side, which the
  stack treats as a hotword. It biases the decoder and fixes capitalisation,
  and it never rewrites another word into a name. The stack measured terms
  absent from the audio at no cost on Parakeet, so a list that names every
  room is safe on a command about one of them.
- **Repairs for Portuguese only.** Spoken quickly, "desliga a luz da cama"
  comes back as "desliga-los da cama". "luz" is too short to boost (the stack
  skips terms under four characters), so the fix is a repair after decoding.
  Each left-hand side has two words, which the stack accepts without `force`.
- **At most 200 phrases.** The stack boosts at most `STT_BOOST_MAX_PHRASES`
  (200) phrases a request, the repairs' corrected sides among them. The
  integration stops the terms at 200 less those, in the order above, and logs
  what it left out, so the stack boosts every phrase it is sent.
- **Typographic punctuation becomes ASCII.** A curly apostrophe in an area
  name had no token sequence in the model's vocabulary, and the stack refused
  every boosted request that named the profile. Symbols and emoji are
  removed for the same reason.
- **Boost only once the engine is known.** Both clients read `GET /health`
  (the engine's `accepts_boost`, and `hotwords`) before they add `boost`.
- **The vocabulary must never cost a transcription.** A refused `boost` is
  sent again with the names and without the boost. On the hub, a 400 to a
  request that named the profile is sent again without it, and the profile
  is left out for 10 minutes, with a warning in the log. The hub also leaves
  the profile out while the stack's `/health` does not list it, which is a
  hub without the integration, and looks again every 10 minutes.

## What it costs

- **A keyless gateway lets anyone on the network rewrite the profile** that
  everyone's commands are transcribed with. The write API has no separate
  permission: with `GATEWAY_API_KEYS` unset, any client that can reach the
  gateway can replace `home-assistant` with names of its choosing. With keys
  set, any key can. The integration does not notice: it compares what it
  builds with what it last wrote, not with what the stack holds, so the
  change stays until Home Assistant's names change or it restarts.
- **The name is reserved.** A profile someone made by hand under that name
  is overwritten.
- **It needs a writable `/glossaries` volume on stt-stack.** Without one the
  stack refuses every write with 503, the integration logs a warning, and
  both clients transcribe without the names.
- **The integration's diagnostics do not show the profile's state** or its
  term count. The stack's `GET /glossaries/home-assistant` does.

## Rejected

- **The names in every request** (`prompt` or `keywords[]`). Each client
  would need the names itself, and the hub can read Home Assistant's
  registries only through an action's token. One stored profile is built by
  the component that can read them, and the other client names it.
- **`STT_GLOSSARY_DEFAULT=home-assistant`.** It would apply the names to
  every request from every client, dictation included, and on Whisper that
  measured a 28% higher word error rate for terms absent from the audio.

## Since sign-in

**2026-10-02:** the first item under *What it costs* is closed
([ADR 0022](0022-everything-behind-a-login.md)). `home-assistant` is
reserved to the system namespace: writing it needs `glossaries:ha` or
`glossaries:write:all`, and a profile a person makes under that name is
never loaded. Naming it on a transcription (`/v1/audio/*`, `/transcribe`)
needs `glossaries:ha`, `glossaries:read:all` or `glossaries:write:all`;
without one it is an unknown profile, and a `user` or `user-jobs` account's
listing never shows it. The `home-assistant` key preset holds `glossaries:ha`, so the integration
writes and names it as before. The hub names it as its own service
principal, `svc:satellites`, so that principal must hold `glossaries:ha` too,
or the hub transcribes without the names, which the hub logs. stt-stack
checks the reserved name itself, lower-cased, rather than trusting the
gateway's route table to have matched its spelling.
