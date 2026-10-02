# voice-gateway

One port, one sign-in, three speech services and a satellite hub.

```text
                        :8080  voice-gateway   TLS, sessions and API keys
                          │
  /login /auth/* /admin/* ├─  answered here: sign-in, accounts, keys, secrets, audit
                          │
  /v1/audio/transcriptions├──────────────────────────►  stt-stack:8000
  /v1/audio/translations  │                             Parakeet, 8.5-10.4x
  /transcribe             │
  /v1/chat/completions    │  an input_audio part is transcribed; nothing here
                          │  ever invents an assistant message
                          │
  /v1/audio/speech  model=│kokoro tts-1 tts-1-hd …  ─►  tts-stack:8001
  /speak  /voices         │                             Kokoro, 1.2-1.5x
                          │
  /v1/audio/speech  model=│chatterbox chatterbox-turbo tts-long
  /jobs  /jobs/{id}[/audio]  DELETE /jobs/{id}     ─►  tts-long:8002
                          │                            Chatterbox 0.138x here,
                          │                            turbo 1.54x on the card
                          │
  /satellites/...         │
  WS /satellites/ws       ├──────────────────────────►  voice-satellites:8003
                          │                             the satellite hub, optional;
                          │                             the socket needs no sign-in
                          │
  /ui/...                 ├──────────────────────────►  voice-ui:8090  the page
                          │
  /v1/models              ├─  answered here, from a static table
  /v1/models/{id}         ├─  one row of that same table
  /health                 └─  every backend, fanned out: liveness for anyone,
                              detail with a scope

                        :8081  the internal listener: plain HTTP, never
                               published, the services' own keys only
  the same routes, plus /runs and /internal/secrets/*
```

Sibling of [services/stt](../stt/README.md), [services/tts](../tts/README.md)
and [services/tts-long](../tts-long/README.md), same conventions. No torch
and no model: the image is `fastapi`, `httpx` and `cryptography` on the slim
base. Its state is three kinds of volume: the database, the gateway's own
keys, and one small volume per service holding that service's key.

## Why this exists

Two reasons, and they are the only two.

**One place that decides who is asking.** Every request to the stack passes
through here, and this is the only process that checks a credential: a
session cookie for a person at the page, an API key for a client, a service
key on the internal listener. It finds the route's scope, refuses or
forwards, and signs what it forwards. `X-Calliope-Identity` is an Ed25519
assertion of who is asking and what they may do, for one service and valid
for 60 seconds. The services verify it with a public key and decide nothing
else about access: they keep each person's data apart
([ADR 0022](../../docs/adr/0022-everything-behind-a-login.md)). The three
backends once carried three diverging copies of a key check, and the same two
bugs were found and fixed in only one of them. One place that checks is one
place to get right.

**One health answer.** Knowing whether the stack is up used to mean polling
three ports. `GET /health` fans out and returns them all in one call, and the
satellite hub with them when it is deployed. Without a credential it says only
`ok` or `degraded`; the detail needs a scope.

Routing is a consequence of those two, not the point.

## Status

`compose.yaml` publishes 8080 and nothing else, so the backends are reachable
only through this process. Sign-in arrives with this release. A stack
upgraded from one without it needs the steps in
[Upgrading to sign-in](#upgrading-to-sign-in), in that order, or Home
Assistant and every client stop working until each has a key.

## Run

```bash
docker run -p 8080:8080 \
  -v gateway-data:/data -v calliope-keys:/keys \
  -v calliope-svc-satellites:/svc/satellites -v calliope-svc-ui:/svc/ui \
  -v calliope-svc-stt:/svc/stt -v calliope-svc-tts:/svc/tts \
  -v calliope-svc-tts-long:/svc/tts-long \
  -e VOICE_CHOWN_DIRS="/data /keys /svc/satellites /svc/ui /svc/stt /svc/tts /svc/tts-long" \
  -v <certificate directory>:/certs:ro \
  -e GATEWAY_TLS_CERT=/certs/fullchain.pem -e GATEWAY_TLS_KEY=/certs/privkey.pem \
  -e CALLIOPE_PUBLIC_ORIGIN=https://calliope.example \
  -e CALLIOPE_ADMIN_PASSWORD='<15 or more characters, first start only>' \
  ghcr.io/gabrielbelli/calliope-gateway:<version>
```

Each service mounts its own `calliope-svc-<name>` volume read-only at
`/run/calliope`; `compose.yaml` at the repository root wires all of it.
`VOICE_CHOWN_DIRS` names each service's volume because a volume mounted where
the image has no directory arrives owned by root, and the entrypoint takes
ownership only of the paths it is given.

Open the public origin, sign in as `admin` with that value, and choose a
password of your own. The value is ignored from then on; remove it.

```bash
curl -s https://calliope.example/health
```

```json
{"status": "ok"}
```

That is everything an anonymous caller learns. A key holding `health:read`
gets each backend's state as well:

```bash
curl -s -H "authorization: Bearer $CALLIOPE_KEY" https://calliope.example/health | python3 -m json.tool
```

```json
{
  "status": "ok",
  "backends": {
    "stt": {"reachable": true,
            "health": {"status": "ok", "model": "parakeet", "glossaries": ["dictation", "tech"]}},
    "tts": {"reachable": true,
            "health": {"status": "ok", "voices": 54, "default_voice": "bm_george"}},
    "tts_long": {"reachable": true,
                 "health": {"status": "ok", "model_loaded": false, "queued": 0}},
    "satellites": {"reachable": true, "health": {"status": "ok"}}
  }
}
```

`model_loaded: false` on a cold tts-long and `status: "loading"` on a
starting tts-stack are the answers to the question an operator is about to ask
next, so they are in the tier a dashboard reads. **Each tier is built from a
list of named fields per backend, never by passing a backend's body through**,
so a field a backend adds later appears in neither tier until someone decides
which it belongs to. `health:detail` adds each backend's internal address,
thread counts, the GPU runner, MQTT, the satellites' topology, the variables
a backend ignores, and the gateway's own locked-mode reasons. Probes are
cached for 5 s and shared while in flight, so ten anonymous calls in a second
cost each backend one probe.

## Routes

Everything else is `404` with an OpenAI envelope. There is no catch-all
pass-through: `/docs`, `/redoc` and `/openapi.json` are **not** proxied, and
no service behind this one publishes its own, so a wildcard route here would
have nothing to reach and would be one more way past the route table.

| Route | Backend | Body |
|---|---|---|
| `GET /login`, `POST /auth/login`, `/auth/*` | answered here | Sign-in, and the account's own password, sessions and keys ([Sign-in, keys and scopes](#sign-in-keys-and-scopes)) |
| `/admin/*` | answered here | Users, roles, everyone's keys, the secret store and the audit |
| `GET /` | answered here | `303` to `/ui` with a session, to `/login` without one |
| `POST /v1/audio/transcriptions` | stt-stack | streamed through |
| `POST /v1/audio/translations` | stt-stack | streamed through |
| `POST /transcribe` | stt-stack | streamed through |
| `POST /v1/chat/completions` | stt-stack, or answered here | buffered — see below |
| `POST /v1/audio/speech` | by `model` — see below | buffered, to read `model` |
| `POST /speak` | tts-stack | streamed through |
| `GET /voices` | tts-stack | — |
| `GET /glossaries`, and `GET`, `PUT`, `DELETE` `/glossaries/{name}` | stt-stack | — |
| `POST /jobs` | tts-long | streamed through |
| `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/audio` | tts-long | — |
| `DELETE /jobs/{id}`, `DELETE /jobs/{id}/audio` | tts-long | cancel a queued job, or discard a finished one or its audio |
| `GET /v1/models` | answered here | — |
| `GET /v1/models/{id}` | answered here, indexed off that same list | — |
| `GET /health` | every backend, and voice-satellites when `GATEWAY_SATELLITES_URL` is set | — |
| `GET`, `POST`, `PUT`, `PATCH`, `DELETE` `/satellites/...` | voice-satellites, if deployed | streamed through. Listed one by one in `SATELLITES_PATHS`; the hub's own README has what each does. `POST /satellites/{id}/listen` is a POST: opening a microphone is a side effect, and a GET can be made to happen by a link. `POST /satellites/{id}/media` is Home Assistant's music and announcements, a WAV relayed as it is written and answered when it has played: the read timeout starts when the upload ends and is `GATEWAY_SATELLITES_MEDIA_TIMEOUT`, and a write that stalls for `GATEWAY_SATELLITES_TIMEOUT` ends it |
| `/ui`, `/ui/transcribe[/…]`, `/ui/speak[/…]`, `/ui/jobs[/…]`, `/ui/vocabulary[/…]`, `/ui/satellites[/…]`, `/ui/account[/…]`, `/ui/admin[/…]`, and the `/ui/*` routes in `UI_PATHS` | voice-ui | The page answers every path under the seven tab names with itself. It is listed one pair per tab, not a wildcard, and each tab's address needs the scope that tab is for |
| `WS /satellites/ws`, `WS /nodes/ws` | voice-satellites | relayed frame for frame, with no sign-in: a satellite has none, and its adoption token is its credential ([ADR 0013](../../docs/adr/0013-satellites-one-door.md)). The gateway adds a relay assertion and refuses an upgrade carrying any `Origin` but `file://`. `/nodes/ws` is the path pre-release firmware from before 2026-09-25 dials, kept until no such board is left |

**Native routes mount flat and unprefixed, and nothing is rewritten.** That is
load-bearing rather than cosmetic. tts-long answers a long request with
`Location: /jobs/{id}` and a body field `audio_url: /jobs/{id}/audio`, both
relative to its own root; mounted here at the same paths they stay correct with
zero header rewriting. A prefixed design (`/tts-long/jobs/…`) would need a rule
that rewrites a header *and* a JSON field, and that rule rots the first time the
backend adds a field.

**What is checked is exactly what is forwarded.** A path with `.` or `..`
segments, `//`, a backslash, an encoded `/` or `\`, or anything that decodes to
`?`, `#` or `%` is a `400` before any lookup. The path that was checked is
written back out for the backend, rather than whatever the client's bytes
would decode to there.

Upload bodies stream straight through. An hour of wav is 100 MB+, and buffering
it here would double resident memory on a host that already keeps 6.5 GB of
Chatterbox around. `POST /v1/audio/speech` is the exception — the gateway must
read `model` out of that body to route it, and that body is text measured in
kilobytes. Responses stream in every case.

### What each route needs

Every route carries a requirement, and a route without one cannot be
registered: the table is bound at import, and binding raises for any route it
does not name. The five public rows are `GET /health`, `GET /login`,
`POST /auth/login`, `GET /` and the device socket (both paths).

| Routes | Scope |
|---|---|
| `/v1/audio/transcriptions`, `/v1/audio/translations`, `/transcribe`, `/v1/chat/completions` | `speech:transcribe` |
| `/v1/audio/speech`, `/speak`, `GET /voices` | `speech:speak`, and `speech:long` as well when `model` routes to tts-long |
| `POST /jobs` | `speech:long` |
| `GET /jobs`, `/jobs/{id}`, `/jobs/{id}/audio` | `jobs:read:own`. `jobs:read:all` and `?owner=` widen it |
| `DELETE /jobs/{id}`, `/jobs/{id}/audio` | `jobs:delete:own`, widened the same way |
| `GET /glossaries[/{name}]` | `glossaries:read:own`. `home-assistant` needs `glossaries:ha` or an `:all` scope |
| `PUT`, `DELETE /glossaries/{name}` | `glossaries:write:own`. `home-assistant` needs `glossaries:ha` or `glossaries:write:all` |
| `GET /v1/models[/{id}]` | `models:read` |
| `/ui`, `/ui/<tab>`, `/ui/config` | a session, never a key; each tab also needs its own scope: `speech:transcribe`, `speech:speak`, `jobs:read:own`, `glossaries:read:own`, `satellites:read`, `keys:manage:own`, `users:manage` |
| `/ui/resolve`, `/commit`, `/abandon`, `/progress`, `/captions`, `/media` | `ingest:links` |
| `/ui/fetch` | `ingest:links` and `speech:transcribe`, with a delegation ([Link transcription](#link-transcription)) |
| `/ui/clips` | `voices:read` to list, `voices:write:own` to add and delete |
| `/satellites`, its events, firmware list and artwork | `satellites:read` (a satellite's `config.buttons` only with `satellites:admin`) |
| A satellite's controls: lights, tone, say, flush, ptt, media, AirPlay, identify, and `PATCH` of control fields | `satellites:control` |
| `POST /satellites/{id}/listen`, `/inject` | `satellites:listen`, audited |
| `POST /satellites/ota` | `satellites:update` |
| `POST /satellites/firmware`, `DELETE /satellites/firmware/{sha256}` | `satellites:firmware` |
| Adopt, forget, set-hub, routing, wake words (writes and models), Home Assistant pipelines, language models, telemetry | `satellites:admin` |
| `/auth/keys*`, `/admin/users*`, `/admin/roles`, `/admin/keys*`, `/admin/secrets*` | session-only scopes ([Step-up and session-only routes](#step-up-and-session-only-routes)) |
| `GET /admin/audit` | `audit:read`, which a key may hold |

The rows are in `app/main.py`, `app/routes_auth.py`, `app/routes_admin.py`,
`app/routes_secrets.py` and `app/internal.py`. What each scope covers is in
`packages/common/voice_common/scopes.py`, and Admin › Roles shows it. The
requirement is found by the router's own match, in declaration order, so
`GET /satellites/telemetry` can never be checked as `GET /satellites/{nid}`.

## The chat route, and why it is not a chatbot

`POST /v1/chat/completions` exists because an aggregator probes it before it
will list a provider at all, and a `404` there fails the whole provider check —
so the stack becomes invisible to everything behind that aggregator, including
the routes that work perfectly.

**It transcribes. It never answers a question.** There are exactly two strings
that can reach a caller in an assistant message, and neither is composed at
request time:

| What you send | What comes back |
|---|---|
| a message whose content carries an `input_audio` part | that clip's transcript, from stt-stack |
| plain text, no audio | a fixed sentence saying this is a speech gateway, not a language model, and where the real routes are |

```bash
curl -s https://calliope.example/v1/chat/completions \
  -H "authorization: Bearer $CALLIOPE_KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"whisper-1","messages":[{"role":"user","content":[
        {"type":"input_audio","input_audio":{"data":"<base64 wav>","format":"wav"}}]}]}'
```

The failure this design is built against is not a rude answer. It is a **silent
promotion**: a router, an agent or a summariser added later sees a plausible
reply, concludes this box runs a language model, and starts sending it real
traffic — every request of which comes back wrong with a `200` on it. The NAS
holds 6.5 GB of Chatterbox and 1.4 GB of Parakeet and not one parameter of
anything that could answer a question, so the canned sentence has to be
unmistakable.

**Every field is honoured or refused by name**, the rule
[services/stt](../stt/README.md) states and this estate is built on.
`CreateChatCompletionRequest` is about thirty fields and this route can honour
three — `model`, `messages`, `stream` — so the rest are a `400` naming the
field and the reason in the terms of the thing that is missing. `temperature`
is refused because there is no sampler; accepting it and returning the same
fixed sentence would tell a caller their settings had landed on a model.

`usage` is **omitted** rather than returned as zeroes: nothing in this process
tokenises anything, so any count would be a measurement never taken.
`stream_options.include_usage` says so by name.

`stream: true` is honoured — role, one content delta, `finish_reason`,
`[DONE]`. One delta and not many, deliberately: stt-stack's buffered
transcription arrives whole, and slicing it into timed fragments would invent a
latency profile a client then builds a progress bar on. The genuinely
incremental surface is `stream=true` on `POST /v1/audio/transcriptions`, which
faster-whisper can do and Parakeet cannot.

Responses carry `x-stt-engine` naming the checkpoint that actually produced the
words, exactly as stt-stack's own do. `model` in the body echoes what the caller
sent, because the specification says it is theirs.

**A chat body is capped at 16 MiB, and it is the only body this service holds whole.**
Every other route that carries audio is streamed through and never held, which
is why this container is given 512 MB in `compose.yaml`. A chat body cannot be
streamed — the clip arrives base64 inside a JSON object, and the transcript has
to be wrapped before anything can be sent — and one was measured at **3.8x its
own size resident** while it is read, linear from 5 MB to 32 MB. So the ceiling
is what keeps that 512 MB a budget rather than a lottery. It is counted while
reading rather than read off `Content-Length`, because a chunked request
declares no length at all. Over it:

```json
{"error": {"message": "a chat body is buffered whole here to find the audio inside it, so it is capped at 16 MB and this one is larger. POST /v1/audio/transcriptions takes the clip as a file upload, streams it through this gateway rather than holding it, and has no ceiling here.",
           "type": "invalid_request_error", "param": null, "code": "upload_too_large"}}
```

16 MiB of base64 is about 12 MB of audio: six minutes of 16 kHz mono wav, or
twenty-five of a 64 kbps mp3. Longer recordings belong on
`POST /v1/audio/transcriptions`, which streams through and takes up to
`GATEWAY_UPLOAD_MAX_BYTES`.

**Base64 is read the way tools emit it.** `base64 clip.wav` wraps at 76
columns, `openssl base64` at 64, and a browser that built the part from a
`FileReader` result sends a whole `data:audio/wav;base64,…` URI. The wrapping
and the prefix are stripped; everything else is refused, because
`base64.b64decode` in its lenient mode does not reject a stray character, it
deletes it and closes the gap — which turns four mangled characters into a clip
three bytes short that gets transcribed with a `200` on it.

**A conversation handed back is not an error.** `messages.append(completion.
choices[0].message.model_dump())` is how openai-python holds a conversation,
and the message it appends carries `refusal: null` — a field this route puts in
its own replies. That and `annotations` are read and ignored on any message, as
`name` is. Only an `input_audio` part is ever acted on, wherever it appears, so
a previous turn's assistant text is not an instruction here.

## Which TTS backend a request reaches

**The criterion is the `model` string and nothing else.**

| `model` | Backend |
|---|---|
| `kokoro`, `tts-1`, `tts-1-hd`, `gpt-4o-mini-tts` | tts-stack |
| absent, empty, or **any unrecognised value** | tts-stack |
| every name in `GATEWAY_LONG_MODELS` — here `chatterbox`, `chatterbox-turbo`, `tts-long` | tts-long |
| a catalogue name **tts-long owns** that this deployment has not enabled | **404**, naming `GATEWAY_LONG_MODELS` |
| a catalogue name **another backend owns** | that backend, exactly as before |

`GET /v1/models` returns that table as OpenAI's model list, with the backend in
`owned_by`. It is answered from a static table with no backend call: the names
are a property of the routing contract, not of any backend's state, and a
client most wants to know what it can send while a backend is restarting. The
tts-long rows are generated from `GATEWAY_LONG_MODELS`, so what is advertised
and what is routed cannot disagree.

The two speech-to-text rows, `parakeet` and `whisper-1`, are fixed. Every
transcription goes to stt-stack whatever `model` says, and stt-stack decides
the engine: a `model` that names an engine its `STT_MODELS` loaded
(`parakeet-pt-br`, `whisper`) reaches that engine, and anything else,
`whisper-1` included, reaches its default. Those ids work but are not listed
here, because the list is static and a deployment's engines are not. A client
that needs them reads `backends.stt.health.models` in `GET /health`
([ADR 0016](../../docs/adr/0016-several-stt-engines.md)).

**The 404 row exists because the rule above it became a trap, and this
deployment is now living in exactly the case it was built for.** "Everything
else goes fast, including an unrecognised name" is right for a typo and was
right while there was one long-form engine. With a catalogue of three, somebody
types `voxtral` — which this deployment has **retired**, see
[ADR 0010](../../docs/adr/0010-the-third-engine-was-measured-and-retired.md) —
falls off the end of `GATEWAY_LONG_MODELS`, and **gets Kokoro**: a different
engine, a different voice, no error anywhere. So the gateway knows every name
tts-long could own, from the same catalogue tts-long reads
(`voice_common.engines.CATALOGUE`), and refuses those by name:

```json
{"error": {"message": "model 'chatterbox-turbo' is a long-form model this gateway knows but this deployment has not enabled. Add it to GATEWAY_LONG_MODELS (and to TTS_ENGINES on tts-long). Enabled: chatterbox, tts-long.",
           "type": "invalid_request_error", "param": "model", "code": "model_not_found"}}
```

A name in *neither* set still goes fast, unchanged. **Adding an engine adds no
route**: the path and method allowlists are untouched, because a third model
string is a field value and not an endpoint. Voxtral arrived that way — a
catalogue row, a compose key and a README line, and not one entry in any of the
three tables, and retiring it took the same two keys back out.

**The last row of that table is why the refusal reads `owned_by` and not the
whole catalogue.** `EngineFacts.owned_by` names which service in the stack owns
a checkpoint, and every row written so far says `tts-long` — so "in the
catalogue" and "tts-long's" have been the same set, and the code said the
second by spelling the first. They stop being the same set the moment the fast
path's own `kokoro` gets a row, which is work already scheduled: `model=kokoro`
would then be answered **404**, telling the caller to add the default voice of
the whole stack to `GATEWAY_LONG_MODELS` — which would route it to a backend
that has never held those weights. The gateway filters on the owner instead, so
a row for a checkpoint tts-stack owns keeps going to tts-stack.

**Input length is not a routing key**, and that is the decisive rejection.
Length is a proxy for a quality choice, and the cost of getting it wrong is not
symmetric: a caller sending a perfectly ordinary 400-character request would
have a 17-second call auto-escalated into a ten-minute job, with nothing in the
request that asked for it.

The rejection used to rest on a second, sharper fact that has since expired,
and it is recorded here rather than quietly dropped: tts-long's image carried no
ffmpeg, so its formats were `wav`, `flac` and `pcm` and an mp3 request to it was
a hard `400` — while `mp3` is exactly what OpenAI's `response_format` defaults
to. That image carries ffmpeg now and answers all six formats at the same
bitrates tts-stack uses, so the two backends no longer differ on format. The
timing asymmetry above is what still rules length out on its own.

**A header was rejected too.** `X-TTS-Backend` works, and is invisible in the
surface that matters: Open WebUI and every other OpenAI-shaped client has a
`model` field in its settings and no custom-header field. A routing key nobody
can set is not a routing key.

**An unknown model goes fast rather than `400`.** The two wrong answers are
asymmetric — Kokoro on long-form costs some quality, Chatterbox on an ordinary
request turns 17 seconds into a job nobody asked for. Default to the
recoverable mistake, and do not break a client that sends whatever string its
UI was left holding.

**Length is a guard-rail, never a router.** There is no auto-escalation in
either direction. Long input on the fast path stays there, bounded by the
timeout rather than by an invented cap the backend does not have: 300 s at the
orko-measured 1.2x realtime is ~360 s of speech, about 900 words. Past that,
send `chatterbox` or split the text.

### The deviation, stated plainly

**With any long-form model — `chatterbox`, `chatterbox-turbo` or `tts-long` —
`POST /v1/audio/speech` may answer `202` with JSON instead of audio bytes.**

**That is true of `chatterbox-turbo` too, and deliberately so.** Turbo runs at
1.5426x realtime on the runner's card, measured, which is past the point where a
short request could be answered down the socket — but that card belongs to
somebody who is often using it, so the same request is nine seconds when they are
away and four minutes when they are not. tts-long always answers with a job id
rather than sometimes; see
[ADR 0008](../../docs/adr/0008-two-engines-and-both-stay-jobs.md). Nothing here
should be built to make turbo synchronous.

**`voxtral` is not one of them any more.** It is in the catalogue and switched
off on this deployment, so `model=voxtral` is the 404 above and never reaches
tts-long at all. What it was — 0.104x realtime, no local lane, a **503**
`engine_unavailable` from tts-long whenever the runner was away, forwarded
untouched like every other backend envelope — is recorded in
[ADR 0010](../../docs/adr/0010-the-third-engine-was-measured-and-retired.md)
along with the three measurements that retired it.

The synchronous OpenAI contract cannot be honoured by tts-long. At the
orko-measured 0.138x realtime, a 200-word request is 80 s of speech and 580 s
of compute — 9.7 minutes. tts-long already made this call: input under
`TTS_OPENAI_SYNC_MAX_CHARS` is waited on and returned as audio, anything longer
or any wait that expires returns `202` with `{id, status, queued_ahead,
estimated_seconds, audio_url}` and a `Location` header. The audio is collected
from `GET /jobs/{id}/audio`.

The gateway neither invents this nor re-implements it. Its only job is to not
break it, which is why the long read timeout (240 s) sits **above** tts-long's
own `SYNC_TIMEOUT` (180 s): a gateway that timed out first would return `504`
for a job that is still running and will produce audio, and would throw away
the job id — a lie plus a leak.

The cost, honestly: `openai-python` does not raise on a 2xx, so it hands that
JSON to the caller as if it were audio, and `stream_to_file` will happily write
JSON into a `.wav`. Two things blunt it — the deviation is reachable only
through an opt-in model name, so no unmodified client meets it by accident, and
the `Content-Type` is `application/json` rather than `audio/*`, so a client that
checks can tell.

## Sign-in, keys and scopes

```bash
curl -H "authorization: Bearer $CALLIOPE_KEY" https://calliope.example/voices
```

People sign in at `/login` and the page uses their session cookie. Clients
(an OpenAI SDK, curl, Home Assistant, a script) use an API key that a person
creates on the Account tab. The services use keys this gateway writes onto
their volumes, and only on the internal listener. The design and its
reasons are [ADR 0022](../../docs/adr/0022-everything-behind-a-login.md);
this section is how it behaves.

### People and sessions

- **Three roles, `admin`, `user` and `user-jobs`.** A `user` can transcribe
  and speak on the fast lanes, ingest links, and read and change their own
  vocabulary profiles and keys. They cannot queue a job, use a long-form
  engine, save a voice clip or read a job, because each of those uses the
  GPU. A `user-jobs` user can do all of that as well: run long jobs and
  read and delete their own jobs and voice clips. An admin can do everything
  a person can do, including the Satellites tab and Admin. `user-jobs` is
  the role that was called `speech`, with the same scopes; migration 0002
  renamed every `speech` account and key preset.
- **A session is an opaque cookie**, `__Host-calliope_session`
  (`Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`), of which only the SHA-256
  is stored. It lasts a year, or 30 days unused. Signing out, a password
  change, a role change or a disabled account ends it on the next request.
- **Passwords** are Argon2id (t=3, 64 MiB, p=1) from `cryptography`, two at a
  time so the audio routes never wait. A password is 15 to 128 characters
  after NFKC normalisation, with no composition rules and no expiry, and may
  not be one of 10,000 common passwords, the username or `calliope`.
- **A temporary password signs in to a restricted session**: 15 minutes, in
  which the account can do nothing but choose its own password. That covers
  the first admin, every user an admin creates and every reset.
- **The first admin** comes from `CALLIOPE_ADMIN_PASSWORD`. It signs in, to
  a restricted session, every time it is tried until the admin has chosen a
  password of their own, so a tab closed before that leaves it working
  ([Upgrading to sign-in](#upgrading-to-sign-in)).

### API keys

A key is `calliope_` followed by 36 base62 characters, the last six a CRC
that rejects a mistyped key without a database lookup. It is shown once, with
Copy, and stored as a hash. It is accepted only as `Authorization: Bearer`,
never in a query string. A person may hold several, each with a name, its own
scopes and an expiry (30, 90 or 365 days, or never). What a key may do is
worked out on every request: its scopes, cut to its owner's current role. A
demotion narrows every key at once. Disabling an account revokes its sessions
and keys, so enabling it again does not bring back a stolen cookie or key.

The page offers the presets the person's role can use, which only fill in
the scope boxes. A `user` is offered `user` and `transcribe-only`, because
every other preset holds a job scope:

| Preset | Scopes | For |
|---|---|---|
| `admin` | everything a key may hold; at most 90 days | an admin's own scripts |
| `user` | the `user` role, without keys | a client of the fast lanes |
| `user-jobs` | the `user-jobs` role, without keys | a client of the speech API, long jobs included |
| `transcribe-only` | `models:read`, `speech:transcribe` | a dictation client |
| `speak-only` | `models:read`, `speech:speak`, `speech:long`, `jobs:read:own` | the macOS player; a long request answers `202` and is polled |
| `read-only` | `models:read`, `health:read`, `jobs:read:own`, `glossaries:read:own`, `voices:read` | anyone whose role has jobs |
| `monitor` | `models:read`, `health:read`, `health:detail`, `jobs:read:all`, `glossaries:read:all`, `satellites:read`, `audit:read` | dashboards and alerts (admins) |
| `home-assistant` | `models:read`, `health:read`, `speech:transcribe`, `speech:speak`, `glossaries:ha`, `satellites:read`, `satellites:control`, `satellites:update` | the Home Assistant integration (admins); may last a year, never longer |
| `firmware-release` | `satellites:read`, `satellites:firmware`, `satellites:update` | `upload_via_hub.py` and release scripts (admins) |

A key holding any of `satellites:admin`, `satellites:listen`,
`satellites:firmware`, `audit:read`, `health:detail` or an `:all` scope lives
at most 90 days. A key holding any other admin-only scope (the satellite and
`glossaries:ha` scopes of the `home-assistant` preset) lives at most a year
and cannot be made to never expire. Admin warns 14 days before any key
expires. An admin can
list and revoke anyone's keys but never see one, and cannot create a key for
someone else.

### Step-up and session-only routes

Some things need the password again, valid for 10 minutes: creating or
changing a user, a reset, creating a key that holds an admin-only scope, and
every secret write. **Step-up exists only for a session**, so every route that
needs it refuses a key with `403 session_required`, whatever the key holds.
`users:manage`, `secrets:manage` and both `keys:manage` scopes are never
granted to a key at all (`400 scope_not_grantable`). A key therefore cannot
create keys, so a leaked key cannot leave behind one that outlives its own
revocation.

### Cross-site requests and the public origin

- **Fetch Metadata, not tokens.** An unsafe request that is not Bearer
  (cookie or none at all, sign-in and sign-out included) must say
  `Sec-Fetch-Site: same-origin`, or carry an `Origin` equal to
  `CALLIOPE_PUBLIC_ORIGIN`; otherwise `403 csrf`. A cookie request of any
  method must be `same-origin`, except a top-level navigation to a page
  address. `same-site` gets no exception: the NAS serves other things under
  the same host name.
- **Sign-in takes JSON only** (`415 json_required` otherwise), so a form on
  another site cannot submit it, and it always issues a fresh session.
- **One host name.** Sign-in, and every cookie, work only on the host of
  `CALLIOPE_PUBLIC_ORIGIN` (`403 wrong_host`); a session cookie sent to any
  other host is ignored. A browser ties a cookie to a host and not to a
  port, so that host name must serve Calliope and nothing else on any port.
  The NAS's own address still answers API keys.
- **No CORS.** No route sends an `Access-Control-Allow-*` header, which is
  what lets a Bearer request skip the checks above.
- **A Bearer header wins over a cookie**, and a Bearer that does not
  authenticate is a `401`, never a fall-back to the cookie.

### Sign-in throttling

Every failure answers "Incorrect username or password", and every path costs
one Argon2 verification, so neither the words nor the time say whether an
account exists. Three limits, each a `429` with `Retry-After` that does not
check the password:

| Limit | |
|---|---|
| per account and address | from the 5th failure in a row, 1 s, doubling to 15 minutes |
| per account | more than 50 failures in 10 minutes from everywhere: addresses that have not signed in to it in 30 days get one attempt a minute between them |
| per address | 20 attempts in 10 minutes |

The account and address pair is why someone guessing from the internet
cannot lock the owner out from the owner's own address. A step-up counts
against the same limits. `unlock` on [the command line](#the-command-line)
clears the first two. The state is in memory, bounded at 100,000 entries per map, and a
restart forgets it.

### The client's address

`CALLIOPE_TRUSTED_PROXIES` is the only way a forwarded address is believed.
Without it the peer is the client, whatever the headers say. With it,
`X-Forwarded-For` is read only from a trusted peer, and the client is the
rightmost entry that is not a trusted proxy. **Never include the Docker
bridge's subnet:** every LAN client reaches the container through it, and
each could then name its own address, dodge the per-address limit and write
a false address into the audit.

Behind HAProxy in TCP passthrough, where this container terminates TLS,
every client arrives from HAProxy's address. `CALLIOPE_PROXY_PROTOCOL=1`
then makes 30080 read a PROXY protocol header, v1 or v2, from a trusted peer
before TLS (HAProxy's `send-proxy-v2`), and close a connection from any other
peer that sends one. A peer that sends half a header and stops is closed
within 10 seconds.

### What the services receive

The gateway drops the inbound `cookie`, `authorization`, `forwarded`,
`x-forwarded-*` and `x-calliope-*` headers, sets its own `X-Forwarded-For`,
and adds `X-Calliope-Identity`: who is asking, whether a person or a service,
their effective scopes and the credential used, signed with Ed25519 for that
one service and valid for 60 s. There is no role in it, so a service can
widen access only on an `:all` scope. `set-cookie` is stripped from every
answer.

Each service verifies the assertion with `voice_common.identity` and the
public key on its own volume, refuses anything else with `401` except its own
`/health`, and keeps each person's data apart: another person's job, profile
or clip is a `404`.

### The internal listener

`:8081` is plain HTTP on the compose network and is never published. It takes
the services' own keys (`calliope_svc_…`), which `:8080` refuses, and nothing
else. It serves the same route table with the same scopes, checked against
the service's scopes, plus:

| Route | Who |
|---|---|
| `POST /runs` | stt and tts, recording a finished run on tts-long (`runs:write`) |
| `GET /internal/secrets/{name}` | the hub and tts-long, for a secret that lists them as a consumer (`secrets:fetch`) |
| `POST /internal/secrets/import` | the same two, once ([ADR 0023](../../docs/adr/0023-one-secret-store.md)) |
| `POST /v1/audio/transcriptions` with `X-Calliope-Delegation` | voice-ui, for link transcription |
| `GET /health` | the hub, for stt's engines and glossaries (`health:read`, the same tiers as on 8080) |

| Service | Its principal's scopes |
|---|---|
| `svc:satellites` | `speech:transcribe`, `speech:speak`, `health:read`, `glossaries:ha`, `secrets:fetch`, `secrets:import` |
| `svc:ui` | `speech:delegate` |
| `svc:stt`, `svc:tts` | `runs:write` |
| `svc:tts-long` | `secrets:fetch`, `secrets:import` |

The gateway mints each service's key at start, onto
`/svc/<name>/service.key`, and writes its own public key beside it as
`identity.pub`. A service reads both from `/run/calliope`, its own volume
mounted read-only, reports `not_ready` on `/health` until they exist, and
reads them again after a `401`. Both listeners run in one process, so a key
revoked on one is revoked on both.

### Link transcription

When a person transcribes a link, voice-ui downloads it and has to send it to
stt on that person's behalf. The gateway forwards `POST /ui/fetch` with a
second token, `X-Calliope-Delegation`, for the gateway itself and valid 30
minutes. voice-ui sends the download to `:8081` with its own key and that
token. The gateway accepts each token at most twice (one retry) and, on every
use, checks again that the person's session or key still works and that the
account is active. A person who signs out, has the key revoked or is
disabled stops at once, and the run is recorded as theirs.

### Long streams

Every request runs registered against its session or key, so signing out,
revoking a key, disabling an account or changing a role closes what that
credential opened, event streams and downloads included. An event stream is
also closed after 15 minutes, and the browser's `EventSource` reconnects and
is checked again.

### The audit

Admin › Audit lists what people and services did. Two tiers, so noise cannot
push out evidence:

- **Security events**, kept for a year and never dropped to make room: sign-ins
  and their throttling, sign-outs, step-ups, password changes and resets,
  user, role and key changes, secret writes and imports, secret fetches
  (once an hour per secret and service), a microphone opened (`listen`, `inject`), firmware uploads and updates,
  routing and wake word changes, delegated calls, an `:all` read of someone
  else's data, and locked mode. At a million rows one `audit_overflow` event is
  written, later events go to the log only, and Admin shows a banner.
- **Counts**, one row per minute: `401`s per address, `403`s per credential,
  and failed sign-ins per name and address. Capped at 100,000 rows, oldest
  first.

A name typed at sign-in is recorded only if the account exists, and as
`<unknown>` otherwise. No row holds a password, a key, a token or a body.
Each row is also one `audit {...}` line on stdout, the same format the
services use for the assertions they refuse.

### The command line

Anyone who can run these already controls the host, so they need no
password. Each is audited as the command line.

```bash
docker exec -u 1000:1000 voice-gateway python -m app.admin reset-password <user> [--revoke-keys]
docker exec -u 1000:1000 voice-gateway python -m app.admin unlock <user>
docker exec -u 1000:1000 voice-gateway python -m app.admin rotate-identity-key
docker exec -u 1000:1000 voice-gateway python -m app.admin rotate-service-keys [<service> ...]
docker exec -u 1000:1000 voice-gateway python -m app.admin reopen-import <service>
docker exec -u 1000:1000 voice-gateway python -m app.admin list-users
```

**Always as uid 1000, the gateway's own.** The image has no `USER` line,
because the entrypoint starts as root to take ownership of the volumes, so a
`docker exec` without `-u` runs as root. A key that `rotate-identity-key`
writes as root is one the gateway cannot read: it goes on signing with the
old key, and at its next restart it stops, because an unreadable identity key
is the one fault that stops it ([Locked mode](#locked-mode)). So a command
started as root drops to the owner of `/keys` before it does anything; `-u
1000:1000` says the same thing out loud.

A shell opened from the TrueNAS app page is root as well, and the same drop
applies there. If files on the volumes were ever written as root by hand,
give them back and restart. This works whether the gateway is still up or already failing to
start, which a `docker exec` would not:

```bash
docker run --rm --volumes-from voice-gateway --entrypoint chown \
  ghcr.io/gabrielbelli/calliope-gateway:<version> -R 1000:1000 /data /keys /svc
docker restart voice-gateway
```

| Command | What it does |
|---|---|
| `reset-password` | Prints a one-time password to this terminal, and nowhere else, and ends the user's sessions; `--revoke-keys` revokes their keys too. On an empty database, `reset-password admin` creates the admin |
| `unlock` | Clears the sign-in delays for that account |
| `rotate-identity-key` | Signs with a new key from the next second. The old one stays in `identity.pub` for 2 minutes, so no request in flight fails |
| `rotate-service-keys` | New keys for the named services, or all of them. The old keys stop at once; each service reads its new one after its next `401` |
| `reopen-import` | Lets a service import secrets again until its next final batch ([ADR 0023](../../docs/adr/0023-one-secret-store.md)). The hub also needs `secret-import.done` deleted from its volume |
| `list-users` | Every account, its role, and whether it is disabled, deleted or must change its password |

### What this trades away

- **One worker, by design.** The sign-in throttle, the delegation counts and
  the stream registry live in this process's memory. A second worker would
  split them, so the command is `--workers 1`. A restart forgets every delay
  and fails any link transcription in flight.
- **Every service call takes a hop through here.** The hub's speech requests
  and the run log go through `:8081`. The cost has not been measured on the
  deployed hardware yet; on a laptop the gateway hop is 0.85 ms.
- **A host name of its own**, for the page. Without one, only API keys work.
- **The MQTT broker is outside all of this.** The hub takes commands from its
  broker with no Calliope credential, and those can switch a microphone on.
  The broker's ACL is the boundary there; the hub's README has an example.

## Failures

This service installs `packages/common` for the wire contract:
`voice_common.errors` for the envelope, `identity` for the assertion format,
`scopes` for the scope table and `audit` for the line format, so the gateway
and every service read one copy of each. It used to have a fourth
hand-written copy of the envelope, which built three keys where OpenAI's
schema requires four — `param` is required-but-nullable and was absent from
every error this gateway ever emitted — and whose
`error_response(status, message, type_, code)` took the two strings
*positionally*, which is the exact swap the shared function is keyword-only to
prevent. Health stays local: it fans out to every backend rather than
reporting on itself.

Under `/v1` a 404 and a 405 now read exactly as they do from the three
backends. The **native** routes keep the wording and the `code` values they
had, `method_not_supported` included: `/transcribe`, `/speak`, `/voices` and
`/jobs` have clients that may already branch on them. Their only change is the
`param` key the schema requires.

**Governing rule: if the backend produced an answer, forward it unchanged** —
status, body and all. All three services already emit the OpenAI envelope under
`/v1` with distinct `code` values (`model_loading`, `invalid_value`,
`unsupported_value`, `synthesis_failed`, `missing_required_parameter`), and
re-wrapping destroys the code the client switches on. Note stt-stack answers a
rejected body with `422` where the two TTS services answer `400`: both pass
through as-is. Normalising that difference would make the gateway a second,
lying source of truth.

| Failure | Answer |
|---|---|
| Backend down, restarting, no DNS | `503`, `Retry-After: 30`, `code: backend_unavailable`, **naming the service** |
| Backend still loading (fast path) | its own `503 model_loading`, untouched, plus `Retry-After: 10` |
| Backend still loading (long path) | **not an error** — see below |
| Read timeout | `504`, `code: backend_timeout`, message carries the measured rate *and* the way out |
| Non-JSON body on a 5xx | `502`, `code: backend_error`, first 200 bytes quoted, whole body in the log |
| Malformed JSON on `/v1/audio/speech` | `400`, `code: invalid_value` — the only body validation performed |
| No credential | `401 unauthenticated` + `WWW-Authenticate: Bearer`, before any backend is contacted. A page navigation gets `303` to `/login?next=<path>` instead |
| A key that is malformed, unknown or revoked; or expired | `401 invalid_api_key`; `401 api_key_expired`. Never a fall-back to a cookie sent beside it |
| A scope the credential does not hold | `403 insufficient_scope`, with `WWW-Authenticate: Bearer error="insufficient_scope", scope="…"` naming what would do |
| A key on a session-only route | `403 session_required` |
| The password is needed again | `403 step_up_required` |
| A cross-site request; a sign-in on another host | `403 csrf`; `403 wrong_host` |
| A path with `.` or `..` segments, `//`, or an encoded separator | `400 invalid_path`, before any lookup |
| Too many sign-in attempts | `429 rate_limited`, with `Retry-After` |
| The gateway is locked | `503 locked`, with `reason` and `variable` beside `error` ([Locked mode](#locked-mode)) |
| Client disconnects mid-request | upstream connection closed and logged; a tts-long job is **not** cancelled |
| Anything not in the route table | `404`, `code: unknown_url` |

`503` rather than `502` for an unreachable container, because `502` claims the
upstream answered badly and it did not answer at all — and because
`openai-python` retries 5xx twice by default, which for a container mid-restart
is exactly right and costs nothing, since a refused connection fails in
microseconds. The service is named because with three backends behind one URL,
"upstream failed" is unactionable.

**A cold tts-long is never gated on `model_loaded`.** It reports
`model_loaded: false` for minutes on a cold start — 6.5 GB, lazy, unloaded
after 600 s idle, ~3 GB downloaded on the very first job — and `POST /jobs`
accepts work regardless, because the queue absorbs it and the worker loads the
model. A gateway that read that field and synthesised a `503` would reject the
request that was about to warm the model, for the entire cold-start window,
turning a working design into an outage.

Timeouts are per route, from the two measured rates, not one global number:

| Route | Read timeout | Why that number |
|---|---|---|
| STT | 900 s | 8.5-10.4x realtime: two hours of audio is ~847 s of compute |
| Fast TTS | 300 s | 1.2x realtime: ~360 s of speech, about 900 words |
| Long TTS | 240 s | only to sit above tts-long's own 180 s `SYNC_TIMEOUT` |
| Connect | 2 s | a container on the same host either accepts immediately or is not there |

A client disconnecting mid-request does **not** cancel a tts-long job: the job
runs to completion, the audio lands on disk, and the id was already handed over
in the `202`. Abandoning half-finished 6.5 GB work to save disk would be the
worse trade.

## Logging

One line per request, and that is the entire observability budget:

```text
route=/v1/audio/speech POST backend=tts-stack model=tts-1 status=200 duration=3.872 rtf=0.6
```

Route, backend chosen, the `model` string as the client sent it, upstream
status, the duration this process observed, and the backend's own
`X-Realtime-Factor` where it sends one. `grep` answers every question asked so
far, which is why there is no Prometheus, no OpenTelemetry and no sidecar for
three containers and one user.

A request that never reached a backend still writes its line, with `backend=-`
and a status naming what went wrong instead of a code — `client-disconnect`
when the caller hung up mid-upload (answered `499`, nginx's code for it, which
never leaves this process) and `400-badjson` when the body could not be parsed
to route it. Both are on `POST /v1/audio/speech`, the one route that has to
read the whole body before it can choose. `duration` on those lines is the
time the client actually waited, not zero.

`rtf` is populated from the `X-Realtime-Factor` **header**, which only
tts-stack sends. stt-stack and tts-long report `realtime_factor` in their JSON
bodies, and the gateway does not read it: reading it would mean buffering a
response this service exists not to buffer. Those numbers are still in the body
the client received.

Beside it, every audit row is printed as one `audit {...}` line
([The audit](#the-audit)), so `grep audit` is the security log and the rest is
the traffic.

httpx's own per-request log line is silenced — in a proxy it is exactly one
duplicate per request, carrying neither the model nor the duration. uvicorn's
access line is left alone, except that a `/ui/` path loses its query string
there: the page polls `/ui/progress?token=<the link>` once a second while a
link downloads, and a pasted link is not the log's business. Pass
`--no-access-log` if one line per request is meant literally.

A reverse proxy in front sees each request before this gateway does. HAProxy
in `mode tcp` passthrough sees only TLS and logs no path. A proxy that
terminates TLS logs the request line with its query string, so it logs every
pasted link, unless it is told to log the path alone: in HAProxy, a
`log-format` with `%HM %HP %HV` in place of the `%{+Q}r` that
`option httplog` uses; in nginx, `$uri` in place of `$request`.

## Deploying

The three services, the page, the hub and this gateway are **one TrueNAS
app**, which is the whole reason the single boundary is free: the containers
already share networks.

1. Publish `8080` **only**. 8000, 8001, 8002, the page's 8090, the hub's 8003
   and this gateway's own 8081 stay on the app's networks. This is the step
   that makes the boundary real.
2. Give each service its `calliope-svc-<name>` volume, read-write here at
   `/svc/<name>` and read-only in the service at `/run/calliope`, and give this
   gateway `gateway-data` at `/data` and `calliope-keys` at `/keys`. Nothing
   else mounts those two.
3. **Give Calliope a host name of its own** and set `CALLIOPE_PUBLIC_ORIGIN`
   to it, with `https://`. It must serve nothing but Calliope, on any port:
   a browser keeps a cookie per host name, not per port, so another HTTPS
   service under the same name could hand a reader's browser a session of
   its choosing. A second DNS name for the NAS's own address is not enough:
   every app the NAS publishes answers on that name too. Without the variable, or with a plain `http://` origin on a
   network bind, the gateway starts locked.
4. Set `CALLIOPE_ADMIN_PASSWORD` for the first start only, and remove it once
   the first admin has chosen a password. Leave `STT_API_KEYS`,
   `TTS_API_KEYS` and the other variables of the old key scheme unset:
   a leftover one locks the gateway ([Locked mode](#locked-mode)).
5. Leave each container's own healthcheck pointed at **its own** localhost
   `/health`, not at the gateway's aggregate. A container must not be
   restarted because a sibling is down.

`GET /health` here always answers `200`, even when a backend is unreachable
or the gateway is locked. Read `status`, not the status code. The healthcheck
for *this* container calls this endpoint, and a `503` because tts-long is cold
would have the orchestrator restart the gateway for a sibling's fault.

One backend setting is worth changing at the same time, and it belongs in
tts-long rather than here: **set `TTS_OPENAI_SYNC_MAX_CHARS=180` on the
deployed NAS.** The 300 default was derived from 0.21x realtime, which is the
M2 Max number in tts-long's own README; the NAS measures 0.138x, so 300
characters is ~21 s of speech and ~155 s of compute against a 180 s
`SYNC_TIMEOUT` — no headroom at all, and none whatsoever for a cold start that
loads 6.5 GB. At 180 characters it is ~13 s of speech and ~93 s of compute.
The gateway must not compensate for this in code.

### Behind a reverse proxy

| The proxy | Set |
|---|---|
| terminates TLS and sends `X-Forwarded-For` and `X-Forwarded-Proto` | `CALLIOPE_TRUSTED_PROXIES=<its address>/32` |
| passes TCP through and this gateway terminates TLS (HAProxy `mode tcp`) | the same, plus `CALLIOPE_PROXY_PROTOCOL=1`, and `send-proxy-v2` on HAProxy's `server` line |

Never put the Docker bridge's subnet in `CALLIOPE_TRUSTED_PROXIES`
([The client's address](#the-clients-address)). With
`CALLIOPE_PROXY_PROTOCOL=1`, keep `127.0.0.1` out of it too, or the
container's own healthcheck, which connects from there without a PROXY
header, is refused.

### Locked mode

A configuration fault never stops this container: an exit would take the
device relay and `:8081` with it, and the hub reaches stt and tts only through
`:8081`, so every satellite in the house would go quiet. The gateway starts
**locked** instead. `/health` answers `degraded`, the device socket is
relayed, `:8081` serves the services as normal, `/login` shows what is wrong,
and every other route on `:8080` answers `503 locked` with the reason and the
name of the variable, never its value. An ERROR is logged every minute and the
audit records it.

| Reason | Cause | Fix |
|---|---|---|
| `argon2_selftest` | `cryptography`'s Argon2id failed its self-test (OpenSSL older than 3.2) | Use the published image |
| `keyring_unreadable` | `CALLIOPE_MASTER_KEY_FILE` is set but missing, empty, unreadable or blank; or a file was used before and the variable is gone; or the generated keyring on `calliope-keys` is corrupt | Restore the file or its mount, or `master.keys` from its backup. The gateway never generates a key over a file it has used. `/internal/secrets/*` answers 503 meanwhile, and the services keep their last good values |
| `removed_variable` | `GATEWAY_API_KEYS`, `UI_GATEWAY_API_KEY`, `STT_API_KEYS`, `TTS_API_KEYS`, `SATELLITES_API_KEYS` or `RUNLOG_KEY` is set | Remove it. Each was a credential of the old key scheme |
| `public_origin_required` | `CALLIOPE_PUBLIC_ORIGIN` is unset, not an origin, or `http://` on a network bind | Set it to `https://<host name>` |
| `dev_insecure_cookie` | `CALLIOPE_DEV_INSECURE_COOKIE=1` on a bind that is not loopback | Remove it. It exists for the browser tests on `127.0.0.1` |
| `admin_password_weak` | The first-access password breaks the password rules, while no admin has chosen a password yet | A longer, uncommon value |
| `bootstrap_data_lost` | `gateway-data` is empty but `calliope-keys` says the first-access password was used | `docker exec -u 1000:1000 voice-gateway python -m app.admin reset-password admin`; the gateway notices within a minute |
| `bootstrap_required` | An empty database and no `CALLIOPE_ADMIN_PASSWORD` | Set it and restart |

**Losing both volumes at once re-arms the first-access password.** `docker
compose down -v`, or reinstalling the app, removes `gateway-data` and
`calliope-keys` together, and a `CALLIOPE_ADMIN_PASSWORD` still set then works
again for whoever tries it first. That is the reason to remove it once the
first admin has chosen a password. Losing `gateway-data` alone does not re-arm it: that is
`bootstrap_data_lost`.

**One fault does stop the container**: an unreadable identity signing key
(`/keys/identity.json`). Without it nothing can be signed, the device relay
included, so locked mode could not keep the satellites up anyway. The error
names the file. A `rotate-identity-key` run as root leaves exactly this
behind; [The command line](#the-command-line) has the repair.

### Backups

| What | Holds | Back up |
|---|---|---|
| `gateway-data` | users, sessions and keys (hashes only), the encrypted secrets, the audit | with the app |
| `calliope-keys` | the identity signing key, a generated secret-store key (`master.keys`), the first-access marker | **separately** from `gateway-data`: together they decrypt every stored secret |
| the `CALLIOPE_MASTER_KEY_FILE` host file | the secret store's key | separately, and it is outside every app dataset, so in no app backup |
| `calliope-svc-*` | each service's key and the public key | not needed: missing ones are minted at the next start |

A generated keyring moves to a `CALLIOPE_MASTER_KEY_FILE` by itself: set the
variable, mount the file and restart, and every row is re-encrypted under it.
**After a suspected leak of the secret store's key, rotate the secret values
too**: a backup of `gateway-data` taken earlier was encrypted under the old
key.

**The database upgrades itself when the gateway starts.** Each
`app/migrations/NNNN_*.sql` above the file's `user_version` runs once, in one
transaction. A migration that fails, or that leaves a session or key whose
user is gone, is rolled back whole and the gateway does not start. `0002`
renamed the `speech` role and key preset to `user-jobs`. There is no way
back: an older image does not know `user-jobs`, and its holders would sign in
to nothing. Keep the `gateway-data` backup from before an upgrade.

### Upgrading to sign-in

For a stack that ran before sign-in. Nothing here locks the satellites out:
their socket needs no sign-in, and a locked gateway still relays it. Home
Assistant and every API client stop working between step 4 and the moment
each has a key, so do steps 5 to 7 straight away.

**Before deploying**

1. **Create the secret store's key as a host file**, outside every app
   dataset, and owned by uid 1000, which the gateway reads it as:

   ```bash
   umask 077; openssl rand 32 | base64 | tr '+/' '-_' > <host path>/calliope-master
   chown 1000:1000 <host path>/calliope-master
   ```

   The `chown` matters on a NAS shell, which is root: unlike the TLS key, the
   entrypoint does not copy this file, so a `root:root` one locks the gateway
   at its first start (`keyring_unreadable`). Mount it read-only at `/run/secrets/calliope-master`. Skipping this step
   is allowed: the gateway then generates a key on `calliope-keys` and Admin
   › Secrets says so.

2. **Add to the gateway's configuration:**
   - `CALLIOPE_ADMIN_PASSWORD`: long and random, at least 15 characters. It
     keeps signing in until the admin chooses a password of their own in
     step 5, so a tab closed before then leaves it working;
   - `CALLIOPE_MASTER_KEY_FILE=/run/secrets/calliope-master`, if you made the
     file;
   - `CALLIOPE_PUBLIC_ORIGIN=https://<calliope host name>`;
   - `CALLIOPE_TRUSTED_PROXIES` and `CALLIOPE_PROXY_PROTOCOL` if a reverse
     proxy is in front ([Behind a reverse proxy](#behind-a-reverse-proxy)).

3. **Confirm that none of these is set anywhere:** `GATEWAY_API_KEYS`,
   `UI_GATEWAY_API_KEY`, `STT_API_KEYS`, `TTS_API_KEYS`,
   `SATELLITES_API_KEYS`, `RUNLOG_KEY`. A leftover one locks the gateway, or
   makes a service log an ERROR every minute; the satellites stay up either
   way.

**Deploy**

4. **Deploy the release.** `compose.yaml` adds the new volumes and the
   `edge` and `core` networks, points the hub's speech calls and the run log
   at `http://voice-gateway:8081`, and starts the hub once the gateway is
   healthy. The hub imports its stored keys, its button webhook addresses and
   the secrets in its environment into the secret store; tts-long imports the
   GPU runner's key.

5. **Open `https://<calliope host name>/`**, not the NAS's own address, which
   refuses a cookie sign-in. Sign in as `admin` with the first-access value
   and choose a new password; the page signs you in.

6. **Open Admin › Secrets.** Check that Home Assistant's token, the language
   model keys, the runner key, the button webhooks and the MQTT password (if
   any) are listed as set, with the right allowed hosts, then press
   **Confirm** on each unreviewed row.

7. **Create a key for Home Assistant and reauthenticate it.** Account › API
   keys › New key, preset `home-assistant`, expiry a year; copy it.
   In Home Assistant: Settings › Devices & services › Calliope ›
   Reauthenticate, and paste it.

**Clean up**

8. Create keys for the macOS player (`speak-only`), for firmware uploads
   (`firmware-release`) and for any OpenAI or curl client. Create the
   household's people: `user-jobs` for anyone who runs long jobs or clones
   voices, and `user` for everyone else.

9. Remove `CALLIOPE_ADMIN_PASSWORD`, and every variable the Secrets banners
   name (`SATELLITES_HA_TOKEN`, `SATELLITES_*_API_KEY`, the password in
   `SATELLITES_MQTT_URL`), from the configuration, and redeploy. Keep
   `TTS_RUNNER_API_KEY_FILE` for this release if you want its fallback.

**Check**

```bash
curl -s https://<host>/health                                             # {"status":"ok"} and nothing else
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/v1/models         # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'sec-fetch-mode: navigate' https://<host>/ui/jobs   # 303, to /login
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/satellites/x/listen   # 401; with a key, 405 (POST only)
```

- The satellites show online.
- A Home Assistant Assist command is transcribed, with the `home-assistant`
  vocabulary applied.
- A `user-jobs` user's Jobs tab holds none of the household's satellite speech.
- `GET /satellites` with the Home Assistant key has no `buttons` field.

**If something goes wrong**

| Situation | Action |
|---|---|
| The admin password is forgotten | `docker exec -u 1000:1000 voice-gateway python -m app.admin reset-password admin` |
| The admin is throttled | `docker exec -u 1000:1000 voice-gateway python -m app.admin unlock admin` |
| The gateway is locked | `/login` and the log say why; [Locked mode](#locked-mode) has the fix |
| Rollback | Deploy the previous images. They ignore `gateway-data`, and `secrets.json` no longer exists, so **enter the keys again** in the old configuration |

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `CALLIOPE_PUBLIC_ORIGIN` | unset | **Required** on a network bind: `https://<host name>`, a host that serves Calliope and nothing else on any port. Cookie sign-in works only there. Unset, not an origin, or `http://` on a network bind: locked mode `public_origin_required` |
| `CALLIOPE_ADMIN_PASSWORD` | unset | The first admin's first password, with an empty database. It signs in, to a restricted session, until the admin has chosen a password of their own; from then on it is ignored, with a WARNING at every start until it is removed. Weak while unused: locked mode `admin_password_weak`. Unset with an empty database: `bootstrap_required` |
| `CALLIOPE_MASTER_KEY_FILE` | unset | The secret store's keyring, one Fernet key per line, the first in use. Unset, a keyring is generated on `calliope-keys`. Once a file has been used, a missing, empty or blank one is locked mode `keyring_unreadable`; leave the variable out rather than set it empty |
| `CALLIOPE_TRUSTED_PROXIES` | unset | Comma-separated CIDRs of reverse proxies whose forwarded address is believed. Never the Docker bridge's subnet |
| `CALLIOPE_PROXY_PROTOCOL` | unset | `1` reads a PROXY protocol v1 or v2 header from a trusted proxy before TLS on `:8080` |
| `CALLIOPE_DEV_INSECURE_COOKIE` | unset | `1` drops `__Host-` and `Secure` from the cookie, for the browser tests over plain HTTP. Honoured only on a loopback bind; anywhere else it is locked mode `dev_insecure_cookie` |
| `CALLIOPE_DATA_DIR` | `/data` | Where `calliope.db` is |
| `CALLIOPE_KEYS_DIR` | `/keys` | The identity signing key, a generated keyring and the first-access marker |
| `CALLIOPE_SVC_DIR` | `/svc` | One directory per service, each that service's volume |
| `GATEWAY_BIND` | `0.0.0.0` | The address uvicorn's `--host` binds, which must be the same: the public-origin and development-cookie rules are decided from it, never from a `Host` header. A plain-HTTP origin or the development cookie is honoured only on a request that reached a loopback socket as well, so a wrong value here cannot hand out a session in cleartext |
| `GATEWAY_INTERNAL_PORT` | `8081` | The internal listener. Empty switches it off, and with it every service-to-service call. Never publish it |
| `GATEWAY_INTERNAL_BIND` | `0.0.0.0` | The internal listener's address, on the compose networks |
| `GATEWAY_TLS_CERT`, `GATEWAY_TLS_KEY` | unset | A certificate and its key, handed to uvicorn by the shared entrypoint. Both or neither |
| `GATEWAY_STT_URL` | `http://stt-stack:8000` | |
| `GATEWAY_TTS_URL` | `http://tts-stack:8001` | |
| `GATEWAY_TTS_LONG_URL` | `http://tts-long:8002` | |
| `GATEWAY_UI_URL` | `http://voice-ui:8090` | The page |
| `GATEWAY_SATELLITES_URL` | `http://voice-satellites:8003` | The satellite hub. `""` runs without it: its routes answer `503` and `/health` leaves it out. `GATEWAY_NODES_URL`, its name in pre-release builds before 2026-09-25, is not read |
| `GATEWAY_LONG_MODELS` | `chatterbox,tts-long` | Comma-separated `model` values routed to tts-long, and the set `GET /v1/models` advertises for it. Must agree with tts-long's `TTS_ENGINES` minus the `tts-long` alias — `docs/tests/test_deployment.py` asserts it has not drifted. Adding an engine here without adding it there advertises a name that 400s; the other way round hides an engine the box can run |
| `GATEWAY_STT_TIMEOUT` | `900` | Read timeout, seconds |
| `GATEWAY_TTS_TIMEOUT` | `300` | Read timeout, seconds |
| `GATEWAY_TTS_LONG_TIMEOUT` | `240` | Must stay above tts-long's `TTS_OPENAI_SYNC_TIMEOUT` |
| `GATEWAY_SATELLITES_TIMEOUT` | `120` | Read timeout, seconds. The slowest satellite routes are a `listen` of up to 60 s and a `say` or routing test that waits for TTS and an assistant. Was `GATEWAY_NODES_TIMEOUT` |
| `GATEWAY_SATELLITES_MEDIA_TIMEOUT` | `300` | Read timeout, seconds, of `POST /satellites/{id}/media` alone. An announcement is answered once it has played, after whatever the satellite already had queued, and the hub takes up to 120 s of one: this is two at that cap and a reply before them. Its writes keep `GATEWAY_SATELLITES_TIMEOUT` |
| `GATEWAY_UI_TIMEOUT` | `900` | Read timeout, seconds, for the page's routes. `/ui/fetch` transcribes a whole download inside one request, so it has the transcription ceiling |
| `GATEWAY_CONNECT_TIMEOUT` | `2` | |
| `GATEWAY_HEALTH_TIMEOUT` | `5` | Per backend, fanned out concurrently |
| `GATEWAY_CHAT_MAX_BYTES` | `16777216` | The only body this process holds that can carry audio — see above. Over it is a `413`, counted while reading rather than taken from `Content-Length` |
| `GATEWAY_UPLOAD_MAX_BYTES` | `536870912` | The largest upload passed to stt-stack, 512 MiB, about five hours of 16-bit mono wav. Counted as it streams; over it is a `413`. The gateway holds none of it, but stt-stack reads the whole clip to decode it |

`GATEWAY_API_KEYS` is gone. Set, it locks the gateway
([Locked mode](#locked-mode)).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Every test runs the real application against mock backends wired in through
httpx's own transport layer, so header filtering, streaming, timeout mapping
and the whole sign-in path run as deployed, with only the socket replaced.
The suites for sign-in are named for what they cover: `test_auth.py`
(sign-in, passwords, the first admin, throttling), `test_browser.py`
(Fetch Metadata, the public origin, navigations), `test_keys.py`,
`test_admin.py`, `test_routetable.py` (every route has a requirement on both
listeners, and each is checked against a credential with and without its
scope), `test_internal.py`, `test_clientaddr.py` (forwarded addresses and
PROXY protocol, over real sockets), `test_locked.py`, `test_secrets.py`,
`test_signer.py`, `test_streams.py`, `test_audit.py` and `test_migrations.py`
(each migration from the database the one before it left, and the runner's
rollback).

`tests/test_live.py` runs against a deployed gateway, named by
`GATEWAY_LIVE_URL` with a `user-jobs` key in `GATEWAY_LIVE_KEY`, over real
sockets, and **skips itself** when either is unset or the gateway does not
answer, which from a CI runner it should not. It queues no Chatterbox work: tts-long runs one job at a time on a 6.5 GB
model, so the long path is exercised read-only through `GET /jobs`.

## What is not here

Rejected deliberately, each against the same budget: a 2-second dictation clip
is 190-240 ms of recognition at the measured 8.5-10.4x, and a feature that adds
20 ms to that has taken 10% of the interactive path.

- **Retries.** Both TTS routes are non-idempotent — retrying `POST /jobs`
  enqueues a second 6.5 GB job, retrying `POST /speak` burns a second full
  synthesis on a box that is already CPU-bound — there is nowhere to retry to,
  and `openai-python` already retries 5xx twice, so a gateway budget would
  multiply with the client's rather than replace it.
- **Caching.** The hit rate on arbitrary dictated text is approximately zero,
  and the bodies are audio. If a cache is ever justified it belongs *inside*
  tts-stack, keyed on `(text, voice, language, speed, format)` where those are
  already resolved — at the gateway the key would drift from tts-stack's own
  voice-alias and language-inference tables the first time either changed.
- **Rate limiting of the speech routes.** Sign-in is throttled, and tts-long
  holds each person to four live jobs; beyond that the real limiter is
  physical. tts-long runs one job at a time by design, and the other two
  block on CPU in worker threads. A token bucket would only convert "slow"
  into "rejected".
- **Load balancing.** One instance of each service, one CPU. A second Kokoro
  replica would halve the ONNX thread pool available to each.
- **Circuit breakers**, which would do active harm: a breaker cannot tell
  tts-long's minutes-long cold start from an outage, so it would trip on the
  slow first request and then reject exactly the requests that warm the model.
  The `503` with `Retry-After` does the whole job.
- **A health pre-flight before each request.** It doubles the request count on
  the interactive path, it races, and for the long path `model_loaded: false`
  is a normal state in which `/jobs` must still be accepted.
- **Body rewriting beyond reading `model`.** No voice mapping, no language
  inference, no format translation, no error normalisation. tts-stack already
  maps all thirteen OpenAI voice names onto Kokoro voices, and tts-long keeps
  its own clip registry with a different mechanism again; a second table here
  means two tables and a guaranteed drift. The last time that table changed it
  went from six names to thirteen, which is exactly the drift a copy would have
  missed.
- **A unified job abstraction over both TTS backends.** It means the gateway
  holds job state, and then needs storage, restart survival, its own `/jobs`
  endpoints, and an answer for in-flight jobs when it redeploys. tts-long
  already has all of that, and the flat mount makes its URLs work unchanged.
- **Gateway-side streaming or SSE synthesis.** Both TTS backends stream for
  real now — `stream_format: "sse"` on `/v1/audio/speech` emits
  `speech.audio.delta` events as each chunk is encoded, through ffmpeg on a
  pipe rather than a temporary file — and this service already forwards a
  response body as it arrives, so those events reach the client untouched with
  nothing added here. What stays rejected is a gateway-side *fake*: buffering a
  complete response and dribbling it out to pretend at a capability a backend
  does not have. There is nothing to pretend at, and inventing an SSE frame
  here would put a second copy of the event shape in front of the one the
  backend defines.
- **CORS.** The page is same-origin, and a CORS header on any route would
  undo the reason a Bearer request may skip the cross-site checks.
- **SSO, OIDC, MFA, password-reset email and invitations.** A forgotten
  password is `reset-password` on the command line.
- **A realtime STT socket.** stt-stack is file-in, transcript-out with a VAD
  stage, and there is no partial-hypothesis interface to expose. The one
  WebSocket this service relays is the satellites' device socket
  ([ADR 0013](../../docs/adr/0013-satellites-one-door.md)).

## Honest limitations

- **The hop is not free, though it is close.** Measured on a laptop over
  loopback with a trivial JSON body, 300 requests: 0.32 ms median direct
  against 1.17 ms through the gateway — **0.85 ms added**. That is under 1% of
  a 200 ms dictation turn. It has not been measured on the NAS, whose CPU is
  slower and busier, and it will be larger there. With sign-in the hub's
  speech requests take this hop too, through `:8081`, which is also not
  measured there yet.
- **`rtf` in the log is only ever tts-stack's**, because it is the only backend
  that sends the header. The other two put `realtime_factor` in the body, and
  reading a body to log it is exactly the buffering this service avoids.
- **A `202` from the long path will be written into a `.wav` by an unmodified
  OpenAI client.** Documented above; reachable only through an opt-in model
  name.
- **Client-disconnect handling is asserted by reading, not by a test.** The
  code path closes the upstream connection and logs, and the mocked suite
  cannot cut a connection mid-flight through an in-process ASGI transport.
- **No test covers a backend that starts a response and then stalls**
  mid-body. The code logs it and truncates, because the status line has
  already gone out and there is nothing left to change.
- **What lives in memory is lost on a restart**: sign-in delays, delegation
  use counts and open streams. A link transcription in flight fails and has
  to be started again.
- **voice-ui's downloader checks every connection it makes against the same
  rules**; a native network stack would bypass it, and the image carries
  none (ADR 0024).

## Licence

BSD 2-Clause. See [LICENSE](LICENSE).
