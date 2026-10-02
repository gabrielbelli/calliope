# ADR 0003 — Profiles are managed over the API, and writability follows the volume

**Status:** accepted
**Date:** 2026-09-03
**Refines:** [ADR 0002](0002-glossary-profiles.md)

## Decision

Profiles are CRUD resources on the STT service. Useful ones ship in the image
read-only; anyone can add their own at run time over HTTP, without a restart
and without rebuilding anything.

```
GET    /glossaries            every profile: name, source, term count
GET    /glossaries/{name}     its terms
PUT    /glossaries/{name}     create or replace a custom profile
DELETE /glossaries/{name}     remove one
```

## Four decisions inside that, and the reasoning for each

### These are native routes, not `/v1`

OpenAI has no concept of a glossary profile, so per [ADR 0001](0001-openai-api-compatibility.md)
there is nothing to be 1:1 with. Inventing `/v1/glossaries` would be claiming
spec territory that does not exist and would collide the day OpenAI uses that
path. Native routes are explicitly out of ADR 0001's scope.

**Selection** stays exactly as ADR 0002 has it — `prompt` for one-off terms
because the specification already defines it, and `glossary: "tech,mine"` as an
allowlisted extension beside `keywords` and `languages`.

### Built-in profiles are read-only; custom ones live in a volume

Two directories, and the distinction is visible in the API rather than implied:

| | path | writable |
|---|---|---|
| built-in | `/etc/calliope/glossaries/` (in the image) | no |
| custom | `/glossaries` (a volume) | yes |

`PUT` or `DELETE` on a built-in name is a **409**, not a silent shadow. A
profile whose contents depend on which directory won is a profile nobody can
reason about, and "why is `tech` different on that box" is not a question worth
creating.

Built-ins ship as `dictation` and `tech`, and per ADR 0002 contain **no
personal vocabulary**.

### Writability follows from whether a volume is mounted

If `/glossaries` is absent or not writable, the write routes answer **503**
naming the reason, and the service still serves and applies the built-ins.

This is not a permission system, and calling it one would be dishonest. It is
the same reasoning as the UI's `clips.writable()`: a deployment that mounted
nowhere to persist has said, by omission, that it does not want run-time
profiles, and accepting a `PUT` that evaporates on restart would be worse than
refusing it.

**Authentication is `STT_API_KEYS`, unchanged.** But note plainly what that
means today: on this deployment the keys are unset, so **a write API is an
unauthenticated write API**. That is a reason to set them before mounting the
volume, and it belongs in the README rather than in a comment nobody reads.

**Since 2026-09-06 the volume is mounted.** `compose.yaml` had no glossary
volume of any kind for as long as this ADR had been accepted, so `/glossaries`
did not exist in the container and every `PUT` and `DELETE` answered the 503
above. The four routes were complete, tested and unusable on the only
deployment that runs them. `stt-glossaries:/glossaries` is a named volume like
the other five in that file, and `VOICE_CHOWN_DIRS` in the image already listed
`/glossaries`, so the entrypoint takes ownership for uid 1000 on every start.
The warning in the paragraph above is now live rather than hypothetical: the
keys are still unset, and the write routes now accept writes.

### Writes are validated, because a bad rule corrupts silently

The shipped `glossary.txt` already argues this in its own header:

> `"Belli"` is heard as `"belly"`, but a `belly = Belli` rule would corrupt any
> sentence that genuinely says belly. Biasing the decoder is safe; rewriting is
> not.

A `PUT` therefore reports what it rejected instead of accepting everything:

- a replacement whose left-hand side is an ordinary word of the language is
  **refused by default**, and takes an explicit `force` to accept — that is the
  `belly` case, and it is the one failure mode that damages sentences the
  glossary was never meant to touch
- two rules with the same left-hand side are a conflict, not a last-one-wins
- a size ceiling, since these are matched against every word of every transcript
- the response says how many terms were accepted and lists every line that was
  not, with the reason

A `PUT` that half-succeeded silently is the failure this stack has already been
bitten by three times in other forms.

## What has to change first

`pipeline.py:108` loads the glossary **once at startup** and compiles it into
`state["rules"]`. Per-request selection is meaningless while the set is frozen
at boot, so that becomes a registry keyed by profile name, with compiled rules
cached and invalidated on write. These are small text files matched with
compiled regexes; the cost is in the regex compilation, not the read.

## Consequences

- The gateway gains four proxied routes. It is the only published port, so a
  route absent there does not exist for any real client — the same omission
  that left `DELETE /jobs/{id}` unreachable while `tts-long` had implemented it
  all along.
- A request naming an unknown profile is a **400 naming it**. Silent omission
  would mean a caller believing their vocabulary applied when it did not.
- The UI can later grow an editor over these four routes. It is not required:
  `curl` is a fine client for a text file, and the routes are the product.
- Selecting several profiles at once stays discouraged in the documentation,
  for ADR 0002's measured reason: terms that do not occur cost **+12% WER on
  Parakeet, +28% on Whisper**.

## Later

A profile that a program writes and other programs name, rather than one a
person writes, came with the Home Assistant integration:
[ADR 0017](0017-home-assistant-vocabulary.md).

**2026-10-02: profiles have owners, and the write API is behind a login**
([ADR 0022](0022-everything-behind-a-login.md)). The two warnings above about
unset keys no longer apply: stt-stack refuses any request the gateway did not
sign, and `STT_API_KEYS` is a removed variable that is logged and ignored.
The volume is now namespaced, with no file moved:

| | path | who reads and writes it |
|---|---|---|
| built-in | `/etc/calliope/glossaries/` (in the image) | everyone reads; nobody writes |
| system | `/glossaries/*.txt`, every profile written before this release | services, and people holding `glossaries:read:all` or `glossaries:write:all` |
| a person's | `/glossaries/users/<user id>/*.txt` | that person, and `:all` holders by naming `?owner=<user id>` |

A person resolves names among their own and the built-ins; another person's
profile is a 404, and "unknown profile" lists only the names the caller can
use. A built-in name still cannot be shadowed in any namespace. The compiled
cache is keyed by owner and name, so one person's profile is never applied to
another's request. `home-assistant` stays in the system namespace and is
reserved (ADR 0017). `/health` lists only system and built-in profiles, and no
response names a path any more. One person may keep at most 50 profiles, and
one request may select at most 16 distinct ones.
