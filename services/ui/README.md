# voice-ui

One page in front of the gateway, and the three things a browser cannot do for
itself.

```
  https://calliope.example.com/ui   (the gateway, or a proxy in front of it)
        |
        |  every XHR, same origin, no credential in the browser
        v
  voice-ui:8090  ──── + Authorization: Bearer <UI_GATEWAY_API_KEY> ────►
                                       voice-gateway:8080 ──► stt-stack / tts-stack / tts-long
        │
        ├─ /ui/resolve /commit /abandon /progress /fetch /captions ──► MeTube (by host address)
        └─ /ui/clips                                     ──► the shared `voices` volume
```

Five tabs, **Transcribe**, **Speak**, **Jobs**, **Vocabulary** and
**Satellites**. The speech tabs have an easy mode that needs no manual, and an
*Expert* `<details>` panel at the foot holding the real knobs beneath the
controls already on screen. Opening one does not swap pages or lose what you
typed. The Satellites tab is the satellite hub's page, and needs the hub
([The Satellites tab](#the-satellites-tab)).

> There used to be an **Expert** checkbox in the header as well, gating those
> same panels. It was a second, global control over one thing, so a setting
> could be out of sight for two unrelated reasons at once. The panels are
> collapsed `<details>` titled *Expert — …*; a disclosure triangle already
> means "hidden until you want it".

> There used to be an **API key box** in the header too. The key now lives on
> the container as `UI_GATEWAY_API_KEY` and this service adds the header
> itself. **That moves the trust boundary from :30080 to :30081** — see below.

---

## The short version

| | |
|---|---|
| **Reached at** | `http://<host>:30081/ui` — 30081, next to the gateway's 30080 |
| **Talks to** | the gateway, and MeTube. Never `:8000`, `:8001` or `:8002` |
| **Auth** | the gateway's; this service holds no key list and compares no token, but it now *presents* `UI_GATEWAY_API_KEY`. **That makes :30081 the trust boundary** |
| **Image** | 320 MB, measured. The gateway is 286 MB on the same machine and `python:3.13-slim-trixie` is 215 MB |
| **Build step** | none. One HTML file, inline CSS and JS, no framework, no `node_modules`, no CDN |
| **Degrades** | MeTube down or unset → link box hidden or disabled, uploads and TTS unaffected. Gateway down → the page still loads and says so |

---

## Why this is its own container, and what it costs

The framework survey argued for serving the page from the gateway itself, and
the argument was good: the page is static, the gateway is already the only
published port, and this estate is organised around not adding containers.

Two things decided otherwise.

The gateway's Containerfile makes a specific promise — **283 MB**, described in
its own comment as *"the cheap check on this file — an audio or model library
arriving by accident moves it by gigabytes, not megabytes"*. Ingestion needs
`yt-dlp`. Putting `yt-dlp` in the gateway would make the process that holds
every API key also the process that spawns a subprocess on a URL a browser
chose. Separate images keep that blast radius where it is.

And the brief asked for `services/ui` with a Containerfile, a compose entry, a
workflow matrix row and a README like its siblings.

**What it costs is a second `ports` entry**, and `compose.yaml` says at the top
that one appearing means the file has stopped doing its job. That sentence was
written about 8000, 8001 and 8002 — three backends reachable *directly*,
skipping the only process in the stack that checks a token. Those three are
still closed. This one is a browser origin, not a bypass:

- it reaches the gateway and nothing else;
- it holds no key *list* — it asks the gateway whether the credential in play
  is good, using `GET /v1/models`, the cheapest authenticated call in the stack
  (a static table, no backend contacted), cached for 60 s;
- it can answer nothing the gateway would not have answered.

If a future edit gives this container a URL for `stt-stack`, `tts-stack` or
`tts-long`, that is the moment the three points above stop being true.

One line of that list **is** no longer true and was removed: "it forwards the
caller's `Authorization` header untouched". With `UI_GATEWAY_API_KEY` set it
presents its own instead — see the next section. Unset, it is the passthrough
it always was.

### Why the page's XHRs come back here rather than going straight to :30080

One origin means no CORS on the gateway, no preflight on every upload, and no
second base URL for someone to get wrong. The forwarding table in `app/main.py`
is a **fixed allowlist** — no wildcard, no catch-all, for the reason the
gateway has none: a wildcard would proxy `/docs` and `/openapi.json` to
services that deliberately do not publish them.

---

## Authentication, and the boundary that moved

The page has no API key box. It used to: the browser kept a key in
`localStorage`, put it on every XHR, and this service forwarded it untouched.
The user's decision is that this is not a bring-your-own-key tool, so the
credential moved into the container as **`UI_GATEWAY_API_KEY`**, and
`app/main.py` adds `Authorization: Bearer …` on the way past — on proxied
routes, on the `GET /v1/models` key probe, and on the ingest hand-off, all
through one function (`config.gateway_authorization`) so the three cannot
drift apart. An inbound header is *replaced*, not joined: HTTP lets a field
name repeat, and two `Authorization` headers on the wire would let a caller
choose which key the gateway read.

**The consequence, stated rather than discovered: the trust boundary is now
:30081.** Anyone who can reach that port is authenticated by this service,
because it signs their requests for them. On a LAN behind a firewall, for a
tool one person uses, that is a reasonable trade. It is not one anywhere else,
and **publishing 30081 somewhere 30080 is not already reachable from now grants
more access, not less.** `voice-ui` logs a `WARNING` at startup whenever
`UI_GATEWAY_API_KEY` is set, saying exactly this.

What did *not* change: this service still compares no token and holds no key
list. The gateway is the only thing that decides whether a credential is good.

`UI_GATEWAY_API_KEY` is **unset by default**, like `GATEWAY_API_KEYS`, and for
the same reason — a key invented in a deployed file is how a placeholder
becomes production credentials. Unset means no header is added and an inbound
one is forwarded as before, so a stack with `GATEWAY_API_KEYS` also unset
behaves precisely as it did. If `GATEWAY_API_KEYS` **is** set and this is not,
every route in the page is a 401 and there is no box to fix it in; the key
probe turns that into a `503 misconfigured_api_key` naming the variable rather
than passing the gateway's "Incorrect API key provided" through to a page with
nowhere to type one.

---

## Security: read this before setting `UI_METUBE_URL`

**MeTube has no authentication of any kind.** Its configuration has no `auth`,
`user`, `password` or `token` key; `/add`, `/start`, `/delete`, `/retry` and
`/history` are all open, and an unauthenticated `GET /history` from off-NAS
answers 200. That is true today, with or without this service.

This service does not widen it — our ingestion routes are key-checked, so we
are a strictly narrower client of something already open to the LAN. Note that
with `UI_GATEWAY_API_KEY` set, "key-checked" means *this container's* key: the
gate on ingestion is reaching :30081, not knowing a secret.

But **shipping a UI that makes MeTube load-bearing is the moment to close it**:
after this deploys, an outage or an abuse of port 30097 becomes an Calliope
outage. The fix is not in this code. Unpublish 30097, or
firewall it to the NAS, and point `UI_METUBE_URL` at the LAN IP.

### The SSRF story, in three layers

`app/guard.py` carries the full reasoning; the short form:

1. **Our stdlib pre-filter.** http/https only, no userinfo, ports 80 and 443
   only, then `getaddrinfo` and a check of **every** address the name resolves
   to — loopback, RFC 1918, ULA, link-local (which includes
   `169.254.169.254`), CGNAT, multicast, reserved, unspecified, and IPv4-mapped
   IPv6 of any of those. `localhost*`, `*.local`, `*.internal` and
   `metadata.google.internal` are refused by name before resolution is even
   attempted.
2. **MeTube's own `url_guard.validate_url`,** which runs inside its `POST /add`
   and is better than ours: ingress validation *plus* a connect-time
   `getaddrinfo` hook installed in the download subprocess, so it covers
   redirects and DNS rebinding during the download itself.
3. **Our probe, third,** and only on a URL both guards have already accepted.

That ordering is deliberate and it has a cost: because `POST /add` runs before
the probe, every link the user declines has left a pending record in MeTube. So
`/ui/abandon` is not a nicety, it is the other half of `/ui/resolve` — and it
**verifies**, because MeTube's `/delete` answers `{"status":"ok"}` when it
deletes nothing at all.

**Still open, stated rather than hidden.** yt-dlp's extraction follows
redirects, and neither guard covers a redirect *during* extraction to an
internal host; MeTube documents that exact limitation in its own docstring.
The impact is *blind* SSRF — the probe's output is parsed into five scalars,
nothing is written to disk, and no response body is ever returned to a caller.
**The real backstop is network isolation:** this container has no business
reaching the NAS's other services, and an egress rule on the Calliope app is
the fix. Write it down; do not assume it.

`UI_PROBE=0` removes the probe entirely, at the cost of a title-only confirm
card.

---

## Ingestion: what MeTube can and cannot do

`auto_start:false` on `POST /add` **genuinely resolves without downloading** —
verified against MeTube's source and live. `POST /start {ids}` commits;
`POST /delete {ids, where}` abandons. `download_type:"audio"` means a two-hour
4K video never has its video stream pulled: about 1 MB a minute at opus, so
~131 MB for a 2h14m podcast rather than tens of gigabytes.

**But there is no duration and no size in what MeTube exposes.** Its
`DownloadInfo` has no duration field anywhere in its source, `size` stays
`null` until the download finishes, and the full yt-dlp info-dict is stored and
then deliberately stripped (`_PUBLIC_EXCLUDED_FIELDS`). MeTube resolves
duration and throws it away.

So the confirm dialog needs a **metadata probe** of our own —
`yt-dlp -J --skip-download --no-config --no-cache-dir`, in a subprocess with a
20 s hard kill and a `cwd` it cannot write, whose output is reduced to five
scalars. That is not embedding a downloader: there is no output template, no
writable path, no format selection, no post-processing, no cookies, no
concurrency and no disk. Its failure mode is a missing estimate, never a
blocked fetch.

### Sharp edges, all verified

| Fact | Consequence |
|---|---|
| `ids` in `/start` and `/delete` are **URLs**, not the short `id` field | A wrong id returns `{"status":"ok"}` and silently does nothing |
| An `auto_start:false` item lands in **`pending`**, not `queue` | `/history` returns three lists; reading `queue` finds nothing |
| `auto_start` defaults to **true** when the field is `None` | It is always sent explicitly |
| `filename` is `OUTPUT_TEMPLATE` sanitised and byte-trimmed | **Never predicted** — read back from `/history` and percent-encoded |
| Terminal success is `status == "finished"` | Anything else in `done` means the status was rewritten to `error` and `filename` nulled |
| A *rejected* add still creates a record, in `done` | Abandon clears both queues |
| `DELETE_FILE_ON_TRASHCAN` defaults false and is unset here | `/delete where=done` clears the record and **leaves the file** — see cleanup below |
| `AUDIO_DOWNLOAD_DIR` defaults to `%%DOWNLOAD_DIR`, unset here | `/download/` and `/audio_download/` are the same directory, which is why `UI_METUBE_FOLDER` is mandatory in effect |
| `download_type:"captions"` sets yt-dlp's `skip_download` | What finishes is a `.vtt` or `.srt` and **no media**. The `/history` entry differs from an audio one in the filename and nothing else, which is why the suffix is what tells them apart |
| `CORS_ALLOWED_ORIGINS` is empty | A browser **cannot** call MeTube at all. Every call is server-side from this container, which is the right shape anyway |

**Cleanup is the one thing delegation does not solve.** Files accumulate in
`stt-ingest/` and nothing removes them: we have no shared volume to delete
through, and setting `DELETE_FILE_ON_TRASHCAN=true` is global and would make
the user's own trashcan button delete their music. Start with a TrueNAS cron
pruning that directory by mtime. Move to a second, dedicated MeTube instance
with its own dataset if ingest volume ever gets real. Do not silently pick the
global flag.

---

## Subtitles instead of a transcription

A video with **real, human-written subtitles already has a transcript**. The
confirm card offers to take it, and `POST /ui/commit {captions:true}` asks
MeTube for `download_type:"captions"` — yt-dlp then sets `skip_download`,
fetches the subtitle track alone and writes a `.vtt` or `.srt`. About two
seconds, no media, no Parakeet, and a better transcript than this stack would
produce from the audio.

**That path was broken, and this is what was wrong with it.** `/ui/fetch`
streams whatever `filename` MeTube reported into
`/v1/audio/transcriptions`, and there was no branch for a subtitle file. So the
`.vtt` was handed to `stt-stack`, which passes its bytes to libav, which was
being asked to decode a text file as media. The button on the card could not
work as written, and the failure arrived two services away as a decode error
about a file the user never saw.

`POST /ui/captions` is the fix and the sibling of `/ui/fetch`: that route moves
media it must never keep, this one returns a file that is already the answer,
and it **calls nothing**. Both ends now refuse the other's input — `/ui/fetch`
answers `409 not_media` for a `.vtt`/`.srt` filename and `/ui/captions` answers
`409 not_captions` for media — so a page regression cannot put a subtitle file
back on the wire to the transcriber.

Three details worth having written down:

- **The parsing happens in the browser.** The page already has a SubRip/WebVTT
  parser for the karaoke highlight (`CUE_LINE`, `parseSubtitles`), and a second
  one in Python would be two implementations that must agree about what a cue
  is, in two languages, with only one of them tested. The route reads bytes and
  decides nothing about them.
- **Two static directories, and only accidentally one.** MeTube serves
  `DOWNLOAD_DIR` at `/download/` and `AUDIO_DOWNLOAD_DIR` at
  `/audio_download/`; the latter defaults to `%%DOWNLOAD_DIR` and is unset
  here, so today both resolve to the same place. A captions download is the one
  file that would not follow if they were ever set apart — `skip_download`
  writes it beside the *video* — so `/ui/captions` tries the audio route first
  and falls back to the video one, one extra request only on the path that has
  already 404'd.
- **The format asked for is the format written.** yt-dlp writes WebVTT unless
  told otherwise, so someone who chose SubRip would otherwise get a file named
  `.srt` with WebVTT inside it, and someone who chose Text — the default, and
  why most people press that button — would get timecodes. The pane, the
  Download button and the two sidecar buttons are all rendered from one parse,
  so they cannot disagree about a cue.

---

## Uploads, including video

Video files work and always did: `services/stt/app/audio.py` opens the bytes
with PyAV, takes `container.streams.audio[0]` and ignores the video entirely.
There is no ffmpeg package and no shell-out anywhere in this feature.

Two things the page handles because the backend does not.

**There is no size ceiling in the stack.** `services/stt/app/main.py:138` is a
bare `file.file.read()` on an `UploadFile` — no `Content-Length` check, no cap,
no streaming — so a 4 GB MKV is buffered whole into a container limited to
6 GB, and the failure is an OOM kill rather than a message. This service
rejects on `Content-Length` above `UI_MAX_UPLOAD_BYTES` (2 GiB by default)
before a byte is forwarded.

**The browser extracts the audio first.** `decodeAudioData` →
`OfflineAudioContext` at 16 kHz mono → a hand-written WAV header turns a 2 GB
MKV into about 15 MB before it crosses the network. That also unlocks the
native `/transcribe` route, which calls `decode(allow_resample=False)` and 400s
with *"expected 16000 Hz, got 44100 Hz"* — video audio is always 44.1 or
48 kHz, so without this the richer response and its `repaired` glossary field
are unreachable for every video anyone owns. Above ~500 MB the file is uploaded
raw with a warning, because `decodeAudioData` needs the whole thing resident.

The same fifty lines serve the reference-clip path at 24 kHz. **One transcoder,
two features, zero packages added to any image.**

---

## Voice cloning, and the two small changes it needed

On this stack a voice **is a file**: `TTS_VOICE_DIR/gabriel.wav` is the voice
`gabriel`, resolved by tts-long's registry and handed to Chatterbox as
`audio_prompt_path`. There is no per-request reference field on the wire at any
layer.

So "use my own voice" is: record or drop a clip, the browser transcodes it to
24 kHz mono WAV, this service writes it into a volume shared with tts-long, and
the voice is selectable. In the picker it is one more row, and each row carries
its own speed tag.

**Picking the voice picks the *backend*. Whether it also picks the engine
depends on what kind of voice it is**, and that is the distinction the picker is
built on:

* **A clip does not carry an engine.** `gabriel.wav` is read by `chatterbox` and
  by `chatterbox-turbo` equally, so putting the engine on the voice list would
  double every row and mean registering a second clip to change one parameter.
  For a clip the engine is a **request field**, and it belongs with the request
  controls.
* **A preset voice does carry one.** `bm_george` is a Kokoro voice and nothing
  else; `pt_male` is a tensor inside the `voxtral` checkpoint — an engine this
  deployment has **retired**, kept here as the example because it is the one
  that makes the distinction visible. There is nothing to choose: the voice
  *is* the engine, and the same name in another engine's list would be a
  different thing entirely.

So an option's value is `engine:name` — `kokoro:bm_george`, `chatterbox:gabriel`,
`voxtral:pt_male` — and every group in the picker is one engine's voices. The
older one-letter `k:` and `c:` values still parse, so a selection remembered from
before the change survives the deploy.

**This deployment offers no long-form preset voices**, because `voxtral` is
retired from it —
[ADR 0010](../../docs/adr/0010-the-third-engine-was-measured-and-retired.md).
The `voxtral:` rows above are what a deployment that enables it gets; the page
needs no edit either way, because the groups come from `/health.engines`.

**What decides the button, the estimate and the missing Listen is where a job
runs and how long it takes, never where its voice came from.** Those are separate
questions and the page had been answering them with the same test. Voxtral was
what made the difference visible: a *preset* voice like Kokoro's and a
*three-minute job* like Chatterbox's, so any gate that reads "preset means
instant" gets both wrong at once. **The separation stays now that it is
retired.** Re-joining the two questions because every preset voice on this
deployment happens to be an instant one again is how the page would be wrong
the day a preset engine comes back.

The rules the picker has to keep are in
[ADR 0008](../../docs/adr/0008-two-engines-and-both-stay-jobs.md) and
[ADR 0009](../../docs/adr/0009-a-third-engine-that-cannot-run-here.md), and one
of them is worth repeating here: **an engine that is enabled but unavailable is
shown disabled with the reason on the line, never hidden** — a group renders
greyed out with the runner's own reason in its labels. Hiding a control the
reader could have had is how the deleted `chatterbox-cpu` rung stayed invisible
for its whole life.

**An engine a deployment has not enabled is a different case and is absent, not
greyed out.** It is not something the reader could have had by waiting: it is
not on `/health.engines` at all, and a disabled row for it would invite a
request the gateway answers 404. Both engines on this deployment run on the
container's own processor, so neither can be unavailable for want of a gaming
PC.

**No engine id is written as a string anywhere in the page.** The groups, the
languages each voice speaks, the controls each engine takes and the rate each one
runs at all come from `/health.engines`, which is why a fourth engine is a
catalogue row and not an edit here. The page holds no voice list of its own.

Two changes elsewhere made that possible:

- **`compose.yaml` gained a named `voices:` volume.** The deployed compose
  mounted none, so tts-long's registry was `["default"]` only and all thirteen
  OpenAI names aliased to the built-in speaker. Without a *named* volume every
  cloned voice would die on the next restart, which is worse than not shipping
  the feature. This service is the only writer; tts-long mounts it read-only.
- **`services/tts-long/app/voices.py` rescans on mtime** instead of once at
  startup. The original argument — no directory listing per request — is kept:
  it is one `stat()`, and a listing happens only on the request after something
  in the directory changed. What it removes is the restart, which on a
  container holding 6.5 GB of Chatterbox costs a ~60 s model load on its next
  job. This is a strict improvement to tts-long on its own terms: a clip copied
  in over SMB now works too.

The write sequence is devnen/Chatterbox-TTS-Server's (MIT, `server.py:670-753`)
transplanted in shape — sanitise, extension allowlist, write, validate duration,
unlink on failure, return the refreshed list — with three things added that
upstream lacks and that matter behind a gateway: a size cap, a collision
policy, and a name built from a `[a-z0-9_-]` **whitelist** rather than from a
sanitiser applied to the uploaded filename. Upstream's `sanitize_filename` is
the only thing between a caller and path traversal; "the only thing" is not
where a write path belongs.

Only `.wav` is accepted, because the browser always sends one. That is what
lets the server-side validation be the stdlib `wave` module rather than
librosa.

---

## Long jobs

Chatterbox runs at about **0.138× realtime**, so a five-minute voice note is
about thirty-six minutes of compute. The whole answer is that the user closes
the tab.

- Submission is **`POST /jobs`**, never `/v1/audio/speech`. `/jobs` answers 202
  every time — no 200-vs-202-vs-SSE to branch on — and hands back `chunks` and
  its own `estimated_seconds`, which the page prefers over its own arithmetic.
  It costs the format field (`/jobs` always produces WAV) and that is the right
  trade: an SSE stream dies when a laptop lid closes, and **closing the stream
  cancels the job**.
- The list is rebuilt from `GET /jobs` **and** reconciled with the ids this
  browser remembers, so it survives a reload, a browser restart and a different
  device, and it covers the window where a 202 landed before the server's list
  caught up.
- Polling is a backoff ladder (2 s → 10 s → 30 s) **layered on
  `document.visibilityState`**, so a background tab does not hammer a NAS that
  is simultaneously running CPU-only Parakeet.
- **The progress bar is honest.** `GET /jobs/{id}` carries `chunks` but no
  per-chunk counter, so the only bar available is time against
  `estimated_seconds`. It is capped at 95 % until the job says `done`, and it
  relabels itself to *"still going"* when it overruns. A bar that reaches the
  end and sits there teaches people to distrust every bar you ever show them.
  The real fix is a completed-chunk counter in tts-long's worker — a handful of
  lines, and it would turn this estimate into a fact.
- **Cancel exists now.** The button reads *"Stop and keep what's done"*,
  because `/jobs/{id}/audio` serves a `cancelled` job as happily as a `done`
  one. tts-long has had `DELETE /jobs/{job_id}` all along; the gateway simply
  never routed it, and this change adds those three lines.
- Before submission, anything over ten minutes of compute gets a strip that
  **offers the cheap path as a button** — *"a Kokoro voice would do it in
  about 30 seconds — [Use it instead] [Queue it anyway]"* — and the ETA is in
  the button label itself. Nobody presses *"Generate — about 36 minutes"* by
  accident. Kokoro's button just says *Speak*; the asymmetry is the message.

STT gets none of this ceremony, deliberately. Parakeet is fast enough that a
spinner is the correct UI.

---

## Playback speed, and following along

Both are entirely client-side. No route changed, nothing was added to the
`PROXIED` allowlist, and no request carries a new field except one that the
expert panel could already send by hand.

### Two things called speed, and how they stopped colliding

The Speak tab already had a **Speed** slider. It is Kokoro's *synthesis* rate:
a request field, sent to the server, baked into the samples, changing the file
the Download button writes, and a 400 on Chatterbox. The new control is
`HTMLMediaElement.playbackRate`: browser-side, applied to any audio, and
invisible to every service in the stack.

They are told apart by **name and by shape**. The slider is now labelled
*Synthesis speed*; the new one is *Playback speed* and is a `<select>` of
discrete rates rather than a second range. Two sliders both saying "speed", one
of which is sent to the server, is how somebody concludes that the download
will come back faster.

`preservesPitch` is left **true**, and set explicitly so the decision is
written where the rate is. Resampling rather than time-stretching moves the
formants, and formants are what intelligibility rides on — so the one thing the
control exists for, getting through a recording faster while still following
it, is exactly what dropping it would destroy. `webkitPreservesPitch` is set
alongside for Safari before 17.

One rate is shared by all three players and remembered in `localStorage`.
Chatterbox's *Synthesis speed* hint now names playback speed as the way out,
because "fixed at 1.0" on its own reads as a dead end when there is a working
answer directly below it.

**The Jobs tab had no player at all** — a finished job could only be
downloaded — so adding a speed control there meant adding the player. It sits
*outside* `#joblist`, which matters: `renderJobs()` assigns `innerHTML` on a
two-second tick while a job is running, and an `<audio>` inside that markup is
destroyed and recreated every tick, restarting from zero. The audio is fetched
with `api()` and turned into a blob URL rather than pointed at directly,
because `GET /jobs/{id}/audio` needs the `Authorization` header and an
`<audio src>` carries none.

### Where the karaoke timings come from, per path

Every timing on screen is the recogniser's own. There is no estimate anywhere
on this path.

| Path | Audio in the browser? | Timings | Highlight |
|---|---|---|---|
| Transcribe — **file upload** | Yes: the `File`, or the 16 kHz WAV the page decoded from it | `verbose_json` `words[]`, falling back to `segments[]` | **Word by word** |
| Transcribe — upload, **SRT/VTT** output | Yes | The cue times in the file itself | **Cue by cue** |
| Transcribe — **link** | No | *(available, unused)* | None, and the page says why |
| Transcribe — **captions** | No | The cue times in the subtitle file | None to follow, but the sidecar writes them |
| Transcribe — **video upload** | Yes, the file itself | `words[]` for the pane, `segments[]` for the band | **Word by word, and a caption over the picture** |
| Speak — Kokoro | Yes | **None exist** | None |
| Jobs — Chatterbox | Yes | **None exist** | None |

The link path is the interesting refusal. `/ui/fetch` streams MeTube's file
straight into the gateway server-side and the browser never receives a byte —
that is the design, and it is what makes a two-hour podcast cost the laptop a
transcript rather than 131 MB. A player would mean a new route serving the
media down to the browser, which is the one thing that architecture exists to
avoid. The result card says so in a line rather than showing a dead control.

On the upload path, a transcript asked for as **Text** is requested as
`verbose_json` instead. Nothing is lost: `openai_api.py`'s `_body()` returns
`result.text` for `response_format=text` and puts that identical string in
`verbose_json`'s `text`, so the pane and the downloaded `.txt` are
byte-identical either way. Both granularities are requested — `word` is what
the highlight follows, `segment` is the fallback when a word cannot be placed
in the transcript exactly, which happens for real when a glossary rule spans
two words and so fires in the segment text and in neither word. The swap does
not happen when there is no audio to follow along with, because timestamps are
a second decoder pass per segment on Whisper and about 5 % on Parakeet
(`asr.py`: 5.34 s against 5.07 s on a 14.2 s clip); and an explicit
`response_format` in the expert panel is never overridden.

Timings line up with the file because `pipeline.py` maps every start and end
back through `speech.original()`, so the silence the VAD removed is added back
in. Playback rate needs no compensation at all — the highlight reads
`currentTime`, so 2× stays in sync for free.

### Nothing is highlighted in the speech direction, and that is the finding

`/speak` answers audio and a usage count. `/v1/audio/speech` the same.
`tts-long`'s `_public()` strips `segments` from every job it reports, leaving
`chunks`, `audio_seconds` and `compute_seconds` — a count, not boundaries. The
only construct available is `duration × (chars so far / chars total)`, and it
is wrong from the first sentence: Chatterbox inserts per-segment pauses,
`chunk_text()` splits where the server decides rather than where the characters
fall, and speech rate moves with punctuation. A highlight that drifts is worse
than none — it is read as a fact about the audio, and it teaches people to stop
trusting the ones that are right. The Speak tab says this in one line, under
the player, where somebody would go looking for the feature.

### The two properties this must not lose

**Escaping.** A transcript is remote data, it is the largest piece of remote
data this page renders, and per-word spans are the only place it is rendered as
hundreds of elements — precisely the change that would reintroduce the hole
fixed two commits ago. It is built with `createElement` and `textContent` and
touches `innerHTML` nowhere, so there is no string for an injection to live in
at all. That is a stronger property than "`esc()` was remembered on every one
of them", and `tests/test_playback.py` asserts it stays true.

Cues are **ranges of the displayed string** — an offset and a length — never
copies of the text. The pane is painted from the response's own bytes, so the
highlighted transcript cannot disagree with the one the Download button writes,
and a timing that cannot be placed exactly is dropped rather than
approximately placed.

**Accessibility.** The highlight carries three redundant channels — background,
weight and an underline — because colour alone is gone for a red-green
deficiency, gone on a badly set projector, and gone in forced-colours mode
where the system palette overrides `background` and only the underline
survives. `--mark-bg` and `--mark-ink` are defined in both the light and the
dark token blocks. Under `prefers-reduced-motion` the transition and the
follow-scroll are removed and the highlight is not: it is information, not
decoration, so switching it off would remove the feature rather than calm it.
The query is read at call time, so changing the setting mid-session takes
effect. Clicking a word seeks to it; words are deliberately not focusable,
because several thousand tab stops between the player and the Download button
is a worse keyboard experience than not having the shortcut, and the audio
element's own controls already reach any point in the file.

### The video player, and the sidecar that stands in for burn-in

A file dropped in with a picture in it plays in a `<video>` rather than an
`<audio>`, with the transcript over it. Two scales of one cue list:

| | Source | Where it is drawn |
|---|---|---|
| **Highlight** | `verbose_json` `words[]` | Word by word in the transcript pane |
| **Caption band** | the same response's `segments[]` | A line over the bottom of the picture |

They are two scales rather than two sources, so the band costs **nothing extra
on the wire** — the upload path already asks for both granularities, because
`segment` is the fallback when a glossary rule spanning two words means the
words stop reconstructing the line. One word at a time over a picture is
unreadable and a subtitle is the unit a viewer's eye is trained on, which is
the whole reason the band is not simply the highlight moved upwards.

The element is given the **original file, never `prepared`** — that is the
16 kHz mono WAV the page decoded for the upload and it has no picture in it.
The timings still line up because `toWav` is called from `pick()` with no
`maxSeconds` and no `startAt`, so both are a whole-file decode on one timeline.
`canPlayType` decides, not the MIME prefix: `decodeAudioData` reads Matroska
that the same browser will not render, and when it is wrong anyway the video's
error handler falls back to the audio element and keeps the transcript, the
highlight and the sound. Only the picture and the band are lost, and they are
what could not work.

The band's colours are **fixed white-on-black in both themes**, which is the one
place on this page that ignores the tokens. It sits over a picture, so the
page's background says nothing about what it needs to be readable against; the
plate is opaque for the same reason.

**Nothing is burnt in, and that was chosen rather than deferred.** Burn-in
means ffmpeg in this image — the Containerfile says twice that there is none —
and a full re-encode of the media to produce a caption track every player
already reads, plus a second copy of the file. The **`.srt` and `.vtt` buttons
beside Download** are the other half of that trade: a sidecar is loaded by VLC,
mpv, QuickTime, every television and every upload form, it stays editable, and
it costs about thirty lines. They are hidden rather than disabled when the
response carried no timings, because on the native route and on a plain `json`
response there are none and there is nothing the user could do about it.

The sidecar and the parser are the two halves of one round trip — a file this
page writes and could not read back would break the highlight for anyone who
saved a transcript and dropped it in again — and `tests/test_playback.py`
asserts a written cue line still matches `CUE_LINE`.

### Links get a player too, and that took three separate fixes

**Links used to get no player at all**, and every screenshot this feature was
built from is a pasted link. Three things were wrong and none of them was the
player:

1. **No timings came back.** The cues come from `timedFromJson()`, which needs
   `verbose_json`. `formatForUpload()` asked for it — on the upload path only.
   A link went through `transcribeToken()`, which used `chosenFormat()`, so it
   was transcribed as plain text and there was nothing to draw with.
2. **`/ui/fetch` could not carry the ask.** It forwarded `model` and
   `response_format` and nothing else, so requesting granularities would not
   have reached stt even if the page had asked.
3. **The media never reached the browser.** That one is deliberate and stays
   the default: `/ui/fetch` streams MeTube → gateway → stt server-side, which
   is what makes a two-hour podcast cost a transcript rather than 131 MB.

`GET /ui/media` is the way back and it is narrow. It serves the file MeTube has
**already** downloaded, only for a token this page resolved, only for a
filename that is media, and only below `UI_MAX_MEDIA_BYTES`. It **relays** byte
ranges rather than parsing them: MeTube's static route already answers `206`
with `Content-Range` and `Accept-Ranges` — verified live — so `Range` and
`If-Range` go up untouched and the answer comes back untouched. Ranges are not
a nicety: without them a `<video>` plays from the start and ignores every
scrub.

Both elements are `preload="metadata"`, so a transcript that is read and never
played still costs nothing; the bytes come off the NAS when someone presses
play. The sidecar buttons work on that path as they always did.

**Keeping the video is opt-in, per link, and off by default.** `download_type:
"audio"` never pulls the video stream, which is the entire reason a link is
affordable, so the tick sits on the confirm card next to the row it changes:
the Download line stops saying "131 MB of audio only" and starts saying
"video — gigabytes, not the 131 MB of audio". With it off, an audio-only link
still plays and the transcript still follows along — a karaoke highlight needs
a clock, not a picture; only the caption band needs the frame.

**The gateway needs `("GET", "/ui/media")` in `UI_PATHS`.** Without it the page
404s on playback when it is served from the published port, which is how
`DELETE /jobs/{id}` stayed unreachable while tts-long had implemented it all
along. The page says so honestly when it happens rather than blaming the
browser for a file it never received.

---

## The estimate, and the number this repository contradicts itself about

The confirm dialog quotes a transcription time, and the rate behind it is
stated three different ways in this repository:

| Source | Claim |
|---|---|
| root `README.md:95` | 47–63× realtime |
| `services/gateway/app/main.py:117-123` | **8.5–10.4×** — and the 900 s `GATEWAY_STT_TIMEOUT` and the 504 help text are built on this one |
| `services/stt/README.md:590` | about 5× on four cores |

A factor of twelve apart. At 47× the brief's flagship example — a 2h14m podcast
— is about two minutes; at 8.5× it is about sixteen minutes and **946 s of
compute, which exceeds the gateway's own 900 s ceiling**, so the honest dialog
for that file says *"this will not finish in one request — trim it"*.

So: nothing is hardcoded. `UI_STT_RTF` seeds the **conservative** figure, the
page keeps its own EMA in `localStorage` corrected by the `realtime_factor`
every native transcription returns, the number is labelled an estimate, and the
dialog warns whenever `duration / rtf` crosses `UI_STT_BUDGET`.

**Someone must re-measure on orko before this is trusted**, and correct
`main.py:121`'s `timeout_help` in the same change — otherwise this page and the
gateway's own 504 message will disagree in front of the same user.

The two halves are **never blended into one figure**. Download and transcribe
are separate lines, because for long media the download is the slow half and
one merged number hides which half to blame. The download half is shown as a
**size**, not a time: this container has no idea what the source's bandwidth
is, and MeTube reports the real `speed` and `eta` once it is actually
downloading.

---

## The Satellites tab

The fifth tab is the satellite hub's page
([`services/satellites`](../satellites/README.md)). It lists every satellite
the hub has seen, and below the list it has four closed sections for the
hub's own settings: **Wake words**, **Activity**, **Telemetry** and
**Firmware**. A
deployment without the hub shows one sentence instead. The page asks the hub
for the lists every 3 s while the tab is open, and keeps its event stream
open once the tab has been visited.

**A first satellite, start to finish:**

1. Join the Wi-Fi network a new satellite opens, `calliope-sat-XXXX`, and give
   it your Wi-Fi and the hub's address. It turns up in the list as waiting.
2. Type a **Name** and press **Adopt**.
3. Open **Wake words**, add a word or open one, choose its **Mode** and its
   **Action**, and press **Save wake words**.
4. Under **Try a word**, type a sentence and press **Try**. The saved action
   answers it and the reply is made, but nothing plays on any satellite.
5. Say the wake word to the satellite, and open **Activity** to see what
   happened.

Each control writes one field on the hub. The tables name the field and link
to where the hub's README describes it, rather than repeat it.

### A satellite waiting to be adopted

| Control | Hub request | |
|---|---|---|
| **Name**, **Adopt** | `POST /satellites/{id}/adopt` `{"name"}` | [Adoption](../satellites/README.md#adoption) |
| **Blink** | `POST /satellites/{id}/identify` | Five seconds of light, to find which board it is |
| **Forget** | `POST /satellites/{id}/forget` | Shown while it is offline, to clear a satellite that was only seen |

### An adopted satellite

Closed, a row shows the name, one state word and one line. Open, it has:

| Control | Field or request | Range |
|---|---|---|
| **Volume** | `volume` | On a satellite with a ring (the Korvo), 12 steps, one to an LED: step k is k × 100 / 12 %. On one without (a Raspberry Pi), 0 to 100 % |
| **Mic gain** | `mic_gain_db` | 0 to 36 dB, 3 dB a step |
| **Light brightness** | `brightness` | 10 to 100 %, 5 a step |
| **Speaker**, **Microphone**, **Lights** | `speaker_enabled`, `mic_enabled`, `lights_enabled` | |
| **Output** | `audio_sink` or `output_satellite` | Each own output with what it is: "USB DAC · up to 32-bit · 384 kHz", "DAC HAT", "HDMI: the display's own DAC", and the Pi's own jack marked **\*** as "PWM, not a DAC"; the line under it says whether something is plugged into it (where the card can tell), what the chosen one takes and what it is driven at now, and the jack's limits; one with nothing plugged in says so in its option. Its own outputs first (a Linux satellite's devices from its last status and the system's default; the Korvo's speaker), then every other adopted satellite with a speaker. One chosen before and gone now stays chosen and says so. Chosen another satellite, the hint names where it plays, or that it plays on its own speaker while that one is offline |
| **Microphone input** | `audio_source` | A Linux satellite only (caps `audio_devices`) |
| **AirPlay** (its own section) | `airplay_enabled`, `airplay_name` | A satellite with caps `airplay` only. **On**, and **Name on phones** (the satellite's own when empty). **Starting volume** (`airplay_volume`, 70 % unless set): where a phone that starts after a minute of quiet starts. Its summary says playing, paused, waiting, off or not running; under it, while a phone is connected: Status, From (the phone and its model), Now playing, Album (and year), Genre, Position, Original file (the phone's own file: its kind and bit rate), Source (classic AirPlay is always ALAC, lossless, 16-bit at 44.1 kHz), Bit rate (1,411 kb/s), Handed on as (the stream into PipeWire), Played at (what the card is driven at), Path ("Bit-perfect" when the phone's samples reach the card unchanged, or what they were converted from and to), Delay here, and the phone's volume |

A control for hardware a satellite does not have is not shown at all: a
Raspberry Pi with no ring, buttons or microphone has no light brightness,
Lights switch, ring set-up, colours, Buttons, mic gain, Microphone switch,
Listen or wake word link, and its **Blink** is **Chime** (three of its wake
sounds). What it has but has switched off stays, greyed with the reason.
Its closed row says **Playing** and the track while AirPlay plays, and
otherwise that it is a speaker, not that it failed to listen. **Device**
adds its output format (what the card is driven at now), its temperature
and whether its power supply is too low.
| **Change wake words** | | Opens **Wake words** |

Each change is one `PATCH /satellites/{id}`. A slider sends its value when
it is let go. A change made with the satellite's own buttons shows here too.

Under **Try it**, each control makes a noise or lights the ring. A sound
plays where the satellite's **Output** is, on another satellite if it plays
through one:

| Control | Request |
|---|---|
| **Say** | `POST /satellites/{id}/say` `{"text"}`, in the hub's default voice |
| **Blink** | `POST /satellites/{id}/identify`. **Chime** on a satellite without a ring (a Raspberry Pi), which plays its wake sound three times |
| **Play a tone** | `POST /satellites/{id}/tone` |
| **Listen 5 s** | `GET /satellites/{id}/listen?seconds=5`, played back in the page |
| **Stop** | `POST /satellites/{id}/flush`: what the satellite's Set button does |
| **Colour**, **Light pattern** (Solid, Pulse, Spin, Off), **Show** | `POST /satellites/{id}/lights` `{"mode", "color"}` |

**Buttons** is a grid: one row per button the satellite reports (Rec, Mode,
Play, Set, Vol −, Vol +, and Side), and a **When pressed** and a **When
released** choice for each. The choices are Nothing, Talk (`ptt`), Stop,
Mute mic (`mute`), Volume up, Volume down, Lights on/off (`lights`), Dimmer,
Brighter and Webhook, which asks for its address. Every change sends the
whole mapping as `buttons` in one `PATCH`. The hub refuses a mapping with no
mute on a button other than Side, since a stock board does not wire Side, and
the page says so before it sends one
([Buttons](../satellites/README.md#buttons)).

**Device** holds what is set once:

| Control | Field or request |
|---|---|
| **Update** | `POST /satellites/ota` `{"satellite", "sha256"}` with the newest image for its model. Shown only when there is one |
| **Name**, **Rename** | `name` |
| **Top of the ring** | `ring_top`: the LED at 12 o'clock as the board is mounted, where the volume bar starts |
| **LEDs run anticlockwise** | `ring_upside_down`. The hub's name for it is "upside down": the bar then runs the other way, so it still fills clockwise as seen |
| **Set up the ring…** | Lights one LED (`POST …/lights` with `pixels`). Move it with ◀ and ▶ and press **That's the top**, then say which way it went, **Clockwise** or **Anticlockwise**. The answers are saved as `ring_top` and `ring_upside_down` |
| **Reboot** | `POST /satellites/{id}/reboot`. It asks first |
| **Move to another hub**, **New hub address**, **Move** | `POST /satellites/{id}/set-hub` `{"url"}`. The satellite saves the address, reboots and waits to be adopted there. It asks first |
| **Forget** | `POST /satellites/{id}/forget`. It asks first |

### Wake words

The list edits a copy of the hub's set, and **Save wake words** sends the
whole set with `PUT /satellites/wake-words`, because the hub checks the set as
one. A 422 leaves the copy on screen with the reason beside the field.
**Remove** marks a word until the save, and **Keep** takes it back. Every
field is in the hub's
[wake word table](../satellites/README.md#wake-words).

| Control | Field | Range |
|---|---|---|
| **Add a wake word**, **Add** | a new entry's `name` | The names the hub can load |
| **Mode**: Command, Conversation, Trigger | `mode` | |
| **Threshold** | `threshold` | 0.1 to 0.95 |
| **Satellites**: Every satellite, Chosen | `satellites`: `["*"]`, or the ids ticked | |
| **Language**, **Language tag** | `language`: unset for Auto, or a BCP 47 tag | |
| **Action** | `action.destination.type`: `ha_assist`, `ha_conversation`, `llm`, `webhook`, `echo` | |
| **Address** | `action.destination.url` | |
| **Token variable**; **Key name** for a language model | `token_env`, or `api_key_env` | A variable's name, never a value. Optional for a webhook |
| **Assist pipeline**, **Ask again** | `pipeline`, listed by `POST /satellites/ha/pipelines` | |
| **Conversation agent** (under More) | `agent_id` | |
| **If not understood, continue as a conversation with** | `action.fallback` | A conversation word |
| **Keep listening after a reply, seconds** | `conversation.follow_up_s` | 1 to 60 |
| **Pause that ends the command, seconds** | `silence_ms`, sent in milliseconds | 0.2 to 3 s |
| **Pause that ends a follow-up, seconds** | `conversation.silence_ms` | 0.2 to 3 s |
| **Ring colour**, **Use the default** | `colour` (`#rrggbb`), or none for the listening blue | |
| **When heard**: Chime and flash, Nothing | `trigger.feedback`: `earcon`, `none` | |
| **Cooldown, seconds** | `trigger.cooldown_s` | 0 to 600 |
| **Ends a conversation it is heard in** | `trigger.ends_conversation` | |
| **Reply on** (under More) | `action.reply_to` | The same satellite, none, or another |
| **Voice** (under More) | `action.voice` | The language's own voice, or a Kokoro voice of the word's language (every language, grouped, on auto-detect), listed by name and sex |
| **End phrases** (under More) | `conversation.end_phrases`, comma-separated | Up to 64 |

A language model word ([its fields](../satellites/README.md#language-model-destination))
adds:

| Control | Field or request | Range |
|---|---|---|
| **Provider** | Fills **Base URL** for a known provider | |
| **Base URL** | `base_url` | |
| **Model**, **List models** | `model`, from `POST /satellites/llm/models` | |
| **API key**, **Store key**, **Clear key** | `PUT /satellites/secrets` `{"name", "value"}`, under the key variable's name. The page is never sent a key back ([Keys](../satellites/README.md#keys)) | |
| **Tools**: Web search, Weather | `tools` ([Tools and the date](../satellites/README.md#tools-and-the-date)) | |
| **Reply limit, tokens** (under More) | `max_tokens` | 1 to 8192 |
| **System prompt** (under More) | `system` | Up to 8000 characters |
| **Test** | `POST /satellites/llm/test`, with the form as it stands | |

The page asks a server for its model list by itself only for the address and
key a word was saved with, because asking sends the key. After a change,
press **List models**.

The last row, after the words, is push-to-talk's own entry: what a button set
to Talk does. It has a mode and an action, and cannot be a trigger.

**Try a word** (`POST /satellites/routing/test`) runs a typed sentence
through a word's saved action and makes the reply, and plays nothing.
**Custom models** uploads an openWakeWord `.onnx` under a name
(`POST /satellites/wake-words/models`). The word is then offered under
**Add a wake word**. **Delete** removes one no word uses.

### Activity, Telemetry and Firmware

**Activity** is the hub's event stream (`GET /satellites/events`) as a log a
screen reader hears: buttons, wake words, conversations, triggers, updates,
and satellites coming and going. It marks a gap while the stream was down.

**Telemetry** is off until turned on ([the hub's
Telemetry](../satellites/README.md#telemetry)). Its summary says whether the
hub is recording, read once with the first list of satellites and not on
every poll.

| Control | Field or request |
|---|---|
| **Record telemetry** | `PUT /satellites/telemetry {"enabled"}` |
| **Keep** | `level`: **Everything, with what was said** (`full`) or **Timings only, no words** (`timings`) |
| **Days kept** | `retention_days`, 1 to 365 |
| **Download** | `GET /satellites/telemetry/records?limit=20000`, as `telemetry.json`. Shown once something is recorded |
| **Delete all** | `DELETE /satellites/telemetry`, after a question. The settings stay |

**Firmware** lists the uploaded images and uploads one:

| Control | Field or request |
|---|---|
| **Image (.bin)**, **Version**, **Model**, **Signature**, **Upload** | `POST /satellites/firmware?model=&version=&signature=`. Type **Version** exactly as the build stamped it (`git describe`): the page calls a satellite up to date only when its reported firmware matches. A satellite built with a signing key refuses an image with no **Signature** ([korvo-satellite](../../clients/korvo-satellite/README.md#updates-over-the-air)) |
| **Update every satellite** | On the newest image for a model. One `POST /satellites/ota` for each satellite that would change, after a question that counts them |
| **Roll back every satellite** | On an older image, in place of Update. The question says the image is older |
| **Delete** | `DELETE /satellites/firmware/{sha256}` |

### The routes behind it

`app/main.py` forwards these to the gateway, and nothing else under
`/satellites`:

- `GET /satellites`, `GET /satellites/events`, and `GET`, `PATCH /satellites/{id}`
- `POST /satellites/{id}/` `adopt`, `forget`, `identify`, `reboot`,
  `lights`, `tone`, `say`, `flush`, `set-hub`, and `GET /satellites/{id}/listen`
- `GET` and `PUT /satellites/wake-words`, `POST /satellites/wake-words/models`
  and `DELETE /satellites/wake-words/models/{name}`
- `GET` and `PUT /satellites/routing`, and `POST /satellites/routing/test`
- `POST /satellites/ha/pipelines`, `POST /satellites/llm/models`,
  `POST /satellites/llm/test`, `PUT /satellites/secrets`
- `GET` and `POST /satellites/firmware`, `DELETE /satellites/firmware/{sha256}`
  and `POST /satellites/ota`
- `GET`, `PUT` and `DELETE /satellites/telemetry`, `GET /satellites/telemetry/records`
  and `GET /satellites/telemetry/summary`

Two hub routes are left out on purpose. `POST /satellites/{id}/inject` runs a
recorded clip through a satellite's real actions, which is a script's job: a
button for it would be one press from Home Assistant acting on a clip.
`POST /satellites/{id}/ptt` is Home Assistant's way to start listening, and
the page has the satellites' own Talk buttons. The device socket
`/satellites/ws` is not here either, because a browser never opens it. A
firmware image or a wake word model is held whole by the hub before it can
refuse it, so this service refuses either upload over 8 MB before it
forwards a byte.

---

## What the page hides, and why each one is right

- **`language`, in either mode.** It is a 400 `unsupported_parameter` on `/v1`
  under Parakeet and silently ignored on `/transcribe`
  (`accepts_language = False`). `STT_LANGUAGE` is deliberately unset on this
  deployment because pinning it breaks the English/Portuguese code-switching
  the user actually does — agreement collapsed to 0.017 when the service
  translated instead of transcribing. A control that does nothing, or does
  harm, is worse than no control.
- **A denoise toggle.** There is nothing to toggle: the pipeline is
  decode → VAD → ASR → glossary, with no preprocessing stage anywhere.
  Denoising measured **+26 % mean WER**, worse in 9 of 13 conditions, one case
  above WER 1.0 from hallucination. Expert mode carries this as a note so
  nobody adds it back.
- **A per-request glossary box.** There is no such field: a request selects
  *named profiles* by name, and `prompt`/`keywords[]` are both 400 on Parakeet.
  Reading and editing those profiles is its own panel, in *Vocabulary
  profiles*. An *irrelevant* glossary cost +12 % WER on Parakeet and +28 % on
  Whisper, which is a finding no slider can express.
- **`model` on STT.** Required by `/v1` validation, and the page offers no
  choice. On `/v1` it sends `model=parakeet`, which reaches Parakeet wherever
  the stack loaded it and the default engine elsewhere. `/transcribe` always
  gets the default engine. `x-stt-engine` and `x-stt-model` say which ran.
- **The TTS language dropdown, on the fast path.** It is *inferred* from the
  voice prefix (`a`→en-us, `b`→en-gb, `p`→pt-br, …). This is the single best
  easy-mode win available: `/speak` defaults to `en-us`, so a UI that simply
  omitted the field would mispronounce every Portuguese request while looking
  entirely correct.

### What the expert panels show

STT `response_format`, `timestamp_granularities[]` (auto-switching to
`verbose_json`), `include[]=logprobs` (auto-switching to `json`), the three
real Silero VAD knobs **at this deployment's defaults** (0.5 / 100 / 300, which
are not OpenAI's), and the route choice — with the native route *disabled with
a stated reason* when the selected file is not 16 kHz.

Kokoro: the `language` override, `format` (noting the field is `format` on
`/speak` and `response_format` on `/v1`, with *different defaults*),
`stream_format`, a segments editor with `pause_after` and per-segment voice,
and the route choice — surfacing `X-Ignored-Parameters` and `X-Speed-Clamped`,
because a field that is accepted and ignored is the worst kind.

Chatterbox: `exaggeration`, `cfg_weight` and `temperature`, each labelled
**"this deployment is calmer than stock"** (0.3 / 0.3 / 0.6 against 0.5 / 0.5 /
0.8), with a *Reset to deployment defaults* button — three sliders at non-stock
values is exactly the state people get lost in. Resemble's demo offers
exaggeration 0.25–2.0; our backend validates `ge=0.0, le=1.0`, so those ranges
are reconciled rather than copied.

**Those three sliders belong to the `chatterbox` engine, not to tts-long.**
`chatterbox-turbo` has no expressive conditioning of any kind — its
`hp.emotion_adv` is `False`, so the layer is never built, and it has no
classifier-free-guidance path — and the backend answers **400** rather than
accepting the values and dropping them. So the panel renders a slider only when
the selected engine declares that control, and when turbo is selected the two
expressive sliders are **removed and replaced by one line saying why**, with
`temperature` left in place. A slider that moves nothing is the same failure as
`X-Ignored-Parameters` one paragraph up, drawn in a nicer widget.

**`voxtral` replaces all three with its own two** where a deployment enables
it, for the same reason and read from the same place: `flow_steps` (1–64,
**32**) and `cfg_alpha` (1.0–3.0, **1.2**). This deployment does not enable it,
so neither slider is reachable here — and that took no edit to the page, which
is the point: the panel builds itself from the controls the selected engine
declares on `/health.engines` and has no list of its own to fall out of date.

Two things about those two are worth knowing before touching them:

* **`flow_steps` is the quality knob and it is also the cost.** 32 was chosen by
  ear against 16, 8 and 4. It is the difference between roughly one minute and
  roughly three minutes of somebody's graphics card per twenty seconds of
  speech, and the estimate on the page moves with it.
* **`cfg_alpha` is not `cfg_weight`.** Different engine, different scale,
  different solver. Sending one where the other belongs is a **400 that names
  the right field**, not a silent reinterpretation — and Retry copies whichever
  controls the engine actually declares, so a retried job cannot quietly lose
  one.

**The Language control is disabled while a voice that carries its own language
is selected**, with the reason on the line. A Voxtral voice is the long-form
case — `pt_male` is Portuguese because of which tensor it is, so a language
field beside it could only agree with the voice or contradict it. The rule is
read from `language_from_voice` on the engine row and not from a name, so it
applies to the next such engine without an edit. Kokoro's rows are filtered by
language too; Chatterbox's clips are not, because language is a parameter those
engines take.

### Vocabulary profiles: reading them, and changing them

The **Vocabulary** row on the Transcribe tab chooses which named profiles a
request applies. That is one half. The **Vocabulary profiles** panel under it
is the other: it reads a profile's file, creates one, replaces one and deletes
one, against `/glossaries` on the gateway.

Before it existed the page could list the names and nothing else. What a
profile contains, and every change to one, went through `curl`. The terms a
transcript depends on were invisible to the person depending on them, on the
one control that only they can set.

**The name box is the address.** Every control in the panel is decided by what
the typed name resolves to in the listing, never by which name was opened. Open
`tech`, type `mine` over it, and the same keystroke turns the panel into a new
profile: the source says so, the text stops being read-only, Save creates it
and Delete goes off. That is also the documented way out of a built-in, which
cannot be written and can be copied.

**Three refusals are pre-empted and one is not.** A built-in's name (409), a
name that is not a usable filename (400) and a deployment with nothing mounted
(503) are all decidable from the listing the page already holds, so Save and
Delete grey *with the reason beside them* rather than offering a write the
service is certain to refuse. That is the rule the route control follows. The
fourth is not decidable here and must not be: which lines the parser accepts is
the service's business and changes with the service, so the body is sent and
the refusal is rendered line by line, with the service's own wording. Nothing
is written when any line is refused, so the text stays in the editor.

`force` is offered only for the one refusal it can fix. It switches off the
single-word left-hand-side rule and nothing else, and the service marks the
forceable rejections in the reason it prints, so the panel reads that rather
than keeping a second copy of the rule.

`GET /glossaries` and `GET /glossaries/{name}` both carry `replacements` and
`hotwords`, and they are **integer counts on the first and the full object and
array on the second**. Nothing in the page reads either field: the counts it
shows come from `terms`, which is an integer on both.

The four routes are on `app/main.py`'s allowlist. Three of them are writes, and
what that changes is stated in the table: anybody who can reach this service
could already start and cancel work on the stack, and can now also write a
glossary file. The ceiling is the service's own: 64 KB, a validated name, 500
terms, and a 409 on a built-in.

**What they still do not show**, because an expert panel is not every
environment variable: `STT_VAD`, `STT_HOTWORDS`, `STT_THREADS`,
`STT_MAX_CONCURRENT`, `STT_QUANTISATION`, `TTS_THREADS`, `TTS_MAX_QUEUE`,
`TTS_JOB_TTL`, every `GATEWAY_*` timeout, and every `/v1` field that is an
unconditional 400 on this stack. Startup configuration is startup
configuration. A control that cannot change the outcome is a lie with a slider
on it.

---

## Configuration

Every variable is optional and every default degrades rather than fails.

| Variable | Default | What it does |
|---|---|---|
| `UI_GATEWAY_URL` | `http://voice-gateway:8080` | The only speech address this service knows |
| `UI_GATEWAY_API_KEY` | *(unset)* | The key this container presents. **Setting it moves the trust boundary to :30081** — read the section above |
| `UI_METUBE_URL` | *(unset)* | MeTube, **by host address**. Unset hides the link box entirely |
| `UI_METUBE_FOLDER` | `stt-ingest` | Mandatory in effect — see the table above |
| `UI_METUBE_FORMAT` | `opus` | ~1 MB a minute. MeTube 400s on any `quality` but `best` for it |
| `UI_METUBE_VIDEO_FORMAT` | `mp4` | Only when "keep the video" is ticked. mp4 because it is remuxed, not re-encoded, and a browser will actually render it |
| `UI_PROBE` | on | `0` removes yt-dlp from the running system; the card degrades to a title |
| `UI_PROBE_TIMEOUT` | `20` | Hard kill, not a suggestion |
| `UI_MAX_UPLOAD_BYTES` | 2 GiB | Checked on `Content-Length` before a byte is forwarded |
| `UI_MAX_CAPTION_BYTES` | 8 MiB | `/ui/captions` buffers rather than streams. An hour of dialogue is ~100 KB; this catches a file that is not subtitles |
| `UI_MAX_MEDIA_BYTES` | 4 GiB | The ceiling on `/ui/media` playback. **Its own setting**: `UI_MAX_UPLOAD_BYTES` bounds what stt reads into memory, this bounds what a laptop pulls down a domestic line |
| `UI_CONFIRM_SECONDS` | `600` | Below this **and** the size threshold, no dialog |
| `UI_CONFIRM_BYTES` | 50 MiB | The second gate, not an alternative |
| `UI_STT_RTF` | `8.5` | The conservative seed. The page measures its own |
| `UI_STT_BUDGET` | `900` | `GATEWAY_STT_TIMEOUT`. Crossing it warns |
| `UI_VOICE_DIR` | `/voices` | The reference-clip store, shared with tts-long |
| `UI_MAX_CLIP_BYTES` | 25 MiB | |
| `UI_MAX_CLIP_SECONDS` | `30` | Trimmed client-side, enforced server-side |
| `UI_RESOLVE_PER_MINUTE` | `12` | So `/ui/resolve` is not a free scanner |
| `UI_VOLUMES` | `/voices` | What the entrypoint takes ownership of before dropping to uid 1000 |

### Why the confirm thresholds are those numbers

Ten minutes of audio is ~10 MB at opus and, at the conservative 8.5×, about
71 s of transcription — an order of magnitude inside the gateway's 900 s
ceiling and well inside anyone's patience. A dialog there is pure friction, and
**a dialog that fires on everything is a dialog people dismiss without
reading** — which is exactly how the three-hour stream gets through. The size
threshold is a second gate rather than an alternative, because a short video
with an enormous audio stream is still a real download. An *unknown* duration
always confirms: not knowing is the case the dialog exists for.

---

## Running it

```bash
# Build, from the repository root — the context is the root for every service
docker build -f services/ui/Containerfile -t calliope-ui .

# Run, on the network the gateway shares
docker run -p 30081:8090 \
  -e UI_GATEWAY_URL=http://voice-gateway:8080 \
  -e UI_METUBE_URL=http://192.0.2.10:30097 \
  -v voices:/voices \
  calliope-ui
```

`compose.yaml` at the repository root wires all of it, including the healthcheck
— which is **not** in the Containerfile, and that is not an oversight:
`HEALTHCHECK` is not a field in the OCI image spec, so an OCI-format build
drops it silently, and OCI is buildah's default format, which is what CI runs.
The sibling images have none for the same reason.

### Tests

```bash
pip install -r services/ui/requirements-dev.txt   # from the repository root
cd services/ui && pytest -q
```

**No test starts a server, and none may.** The suite is
`fastapi.testclient.TestClient` over an httpx `MockTransport` standing in for
both the gateway and MeTube, so the whole resolve → confirm → fetch flow, the
forwarding table, the upload ceiling and the clip store run in-process with no
socket anywhere. `yt-dlp` is never spawned — `app.probe.run` is replaced.

`tests/test_escaping.py` and `tests/test_playback.py` are static and
parser-based: what they assert about `ui.html` — which value reaches
`innerHTML`, which element gets a rate control, which response format is asked
for, whether a highlight is carried by colour alone — is a property of the
bytes in that file, and a headless browser would add a dependency to the one
service whose whole claim is that it has none. The inline script's syntax is
checked separately with `node --check` over the extracted `<script>` block.

The Satellites tab has its own suites:

| File | What it checks |
|---|---|
| `tests/test_satellites.py` | The tab read as text: which control sits in which section, and what a poll may write |
| `tests/test_satellites_writes.py` | The page's own Satellites script, run in Node against a fake hub that answers in the order a network does. The other three below use its harness |
| `tests/test_satellites_ordering.py` | The same, with events and polls that arrive before or after a save |
| `tests/test_satellites_modes.py` | A wake word's mode, language hint and action, set and saved |
| `tests/test_satellites_llm.py` | A language model word: its provider, model list, key and Test |
| `tests/test_satellites_states.py` | The state word, chip and line each satellite row shows, over every case |
| `tests/test_wake_words_contract.py` | The wake word fields the page sends against the names the hub's own code reads |

The Node suites skip without `node` on PATH. None starts a server or reaches
the network.

---

## Layout

| File | |
|---|---|
| `app/static/ui.html` | The whole UI, the Satellites tab included. Inline CSS and JS, no build step, no external request of any kind — it works on a NAS with no internet |
| `app/main.py` | The forwarding allowlist, the `/satellites` routes among it, the key check, the upload ceilings, the clip routes |
| `app/ingest.py` | Resolve, commit, abandon, progress, fetch |
| `app/metube.py` | A narrow client, with every verified trap written down |
| `app/probe.py` | Five scalars out of a URL, and not one byte of media |
| `app/guard.py` | What a pasted URL has to survive. **Read this before relaxing anything in it** |
| `app/clips.py` | The reference-clip store |
| `app/config.py` | Every knob, with the measurement behind each default |

## What we reused rather than wrote

- **MeTube** (AGPL, called over HTTP so no licence reach) — the entire
  downloader: extractors, cookies, retries, concurrency, format selection,
  audio-only extraction, an SSRF guard better than ours, and file serving with
  Range support. Delegating means no extractor rot to chase and, because
  `/audio_download/` is HTTP, **no shared volume between two TrueNAS apps**.
- **devnen/Chatterbox-TTS-Server** (MIT, `server.py:670-753`) — the
  reference-audio upload sequence, transplanted in shape.
- **resemble-ai/chatterbox** (MIT) **as a specification only** — the parameter
  ranges and the four-visible-plus-accordion layout. Its ranges are reconciled
  against ours, not copied.
- **speaches-ai/speaches** (MIT, `src/speaches/ui/app.py:14-88`) — the
  `localStorage` API-key box with show/hide, lifted as a pattern into vanilla
  JS. The rest of that fork carries imports into `speaches.config` and two
  dropdown wirings that are wrong for our API.
- **PyAV**, already in the stt image — video upload already worked; nothing was
  added for it.
- **tts-long's job queue, chunking and ETA arithmetic** — the page reads them,
  it does not reimplement them.
- **`<dialog>.showModal()`, `OfflineAudioContext`, `Notification`** — the
  modal, the transcoder and the ping. Zero dependencies for all three.
