# voice-ui

One page behind the gateway, and the three things a browser cannot do for
itself.

```
  https://calliope.example/ui     the gateway: sign-in, then every page address
        |
        |  the page itself calls the gateway's own paths, same origin, with
        |  its session cookie: /v1, /jobs, /glossaries, /satellites, /health
        v
  voice-gateway:8080 ──── /ui/* + X-Calliope-Identity ────►  voice-ui:8090
                                                                 │
        /ui/resolve /commit /abandon /progress /media /captions ─┼──► a yt-dlp child ──► the internet, :80 and :443
                                                                 ├──► the `ui-cache` volume
        /ui/clips                                                ├──► the shared `voices` volume
        /ui/fetch ─── its own key + the person's delegation ─────┴──► voice-gateway:8081 ──► stt-stack
```

Seven tabs: **Transcribe**, **Speak**, **Jobs**, **Vocabulary**,
**Satellites**, **Account** and **Admin**. A `user-jobs` user sees the first
four and Account, a `user` the same without Jobs, and an admin all seven. The speech tabs have an easy mode that
needs no manual, and an *Expert* `<details>` panel at the foot holding the
real knobs beneath the controls already on screen. Opening one does not swap
pages or lose what you typed. The Satellites tab is the satellite hub's page,
and needs the hub ([The Satellites tab](#the-satellites-tab)). Account and
Admin are the gateway's ([Account and Admin](#account-and-admin)).

> There used to be an **Expert** checkbox in the header as well, gating those
> same panels. It was a second, global control over one thing, so a setting
> could be out of sight for two unrelated reasons at once. The panels are
> collapsed `<details>` titled *Expert — …*; a disclosure triangle already
> means "hidden until you want it".

> There used to be an **API key box** in the header, and later a key held by
> this container on every reader's behalf. Both are gone. A person signs in,
> and the page holds no key of any kind
> ([Who is asking](#who-is-asking)).

---

## The short version

| | |
|---|---|
| **Reached at** | `https://<host>/ui`, through the gateway, after signing in. This service publishes no port |
| **Talks to** | The gateway's internal listener for one thing, transcribing a download, and through its downloader child to public addresses on ports 80 and 443. Nothing on the LAN. Never `:8000`, `:8001` or `:8002`, and it shares no network with them |
| **Auth** | the gateway's. Every request arrives with the gateway's signed assertion of who is asking, and anything without one is refused. This service decides whose data a request touches, never whether it may be made |
| **Image** | 320 MB, measured. The gateway is 286 MB on the same machine and `python:3.13-slim-trixie` is 215 MB |
| **Build step** | none. One HTML file, inline CSS and JS, no framework, no `node_modules`, no CDN |
| **Degrades** | `UI_LINKS=0` or a `/cache` it cannot write → link box hidden, uploads and TTS unaffected. A probe that does not answer → a card with no length or size, and the fetch still works. A backend down → the page loads and its health pills say which |

---

## Addresses

Every tab has an address, and so does every place inside one. The page reads
its own location when it loads and opens what the path names, so a reload
comes back to the same place and a link can point at one satellite, one job or
one profile.

| URL | Opens | Canonical |
|---|---|---|
| `/ui` | Transcribe | yes |
| `/ui/transcribe` | Transcribe | replaced with `/ui` |
| `/ui/transcribe/expert` | the Expert panel, open | yes |
| `/ui/speak` | Speak | yes |
| `/ui/speak?voice=<option value>` | Speak with that voice selected | yes |
| `/ui/speak/clone` | Clone a new voice, with its sheet open | yes |
| `/ui/speak/expert` | whichever Expert panel belongs to the voice | yes |
| `/ui/jobs[?show=<filter>&kind=<kind>]` | Jobs with those filters | `show` omitted when `all`, `kind` omitted when `all` |
| `/ui/jobs/<id>` | that job: marked, scrolled to, focused, its text opened | yes (query kept) |
| `/ui/vocabulary` | Vocabulary | yes |
| `/ui/vocabulary/<name>` | that profile, open in the editor | lower case |
| `/ui/satellites` | the list | yes |
| `/ui/satellites/<sat>` | that satellite's row, open (a satellite waiting to be adopted is scrolled to and its name box focused) | its name as a slug (`Sala de Estar` is `sala-de-estar`), or its ID when the name is empty, shared with another satellite, a section's name or shaped like an ID |
| `/ui/satellites/<sat>/<part>` | the row and its `airplay`, `try`, `buttons` or `device` section | yes |
| `/ui/satellites/wake-words` | Wake words | reserved |
| `/ui/satellites/wake-words/<word>` | that word's row; `ptt` is push-to-talk | yes |
| `/ui/satellites/wake-words/<word>/more` | the row and its More | yes |
| `/ui/satellites/try-a-word` | Wake words and Try a word | reserved |
| `/ui/satellites/custom-models` | Wake words and Custom models | reserved |
| `/ui/satellites/activity`, `/telemetry`, `/firmware` | that section | reserved |
| `/ui/account` | Account: password, sessions, API keys | yes |
| `/ui/admin` | Admin, on its Users section | yes |
| `/ui/admin/<section>` | `users`, `keys`, `roles`, `secrets` or `audit` | yes |

The tab's slug is `vocabulary`; its `data-tab` in the markup is still `vocab`.
A path the tab has no place for is cut back to the part it has, and an unknown
tab (`/ui/nope`) is a JSON 404 from this service and from the gateway. Nothing
that does something or needs the page's memory has an address: the
transcript, the link dialog, the job player, the ring's set-up, the move form,
a new profile, and every button.

Choosing a tab, or opening a place (a row, a section, a word, a job's text, an
Expert panel, a profile), adds a step that Back undoes. Opening a section
inside a place, changing a filter or the voice, and what the page does itself
(adopt, rename, add a word, forget, delete a profile) rewrite the entry they
are on. Back restores the scroll and the focus the entry was left with, closes
what the entry being left opened, and answers the link dialog No if it is open.

A link to something that has gone waits for its list to answer first, then
says so in that tab's own note, replaces the address with the nearest parent
that exists, and shows the parent. A job that exists but is filtered out gets
a Show everything button; one that is only older than the fifty runs the
service lists is said to be. A satellite's address found by its ID survives a
rename made anywhere.

A deep link loads in every case `/ui` loads. The page server answers each
path under the seven tab names with the same file and the same headers. The
gateway lists each tab as one pair of routes (`/ui/<tab>` and
`/ui/<tab>/{rest:path}`), each needing a session and the tab's own scope: a
navigation without a session goes to `/login` and comes back, and one to a
tab the role does not have lands on `/ui`. A link with a query string changes
nothing until somebody presses something.

## What updates by itself

The page keeps itself current without a reload, and asks for nothing while
nobody can see it. The rule: ask when the answer can be seen, and catch up
once when it can be seen again. Returning to the page fires `focus` and
`visibilitychange` together; the two count as one return.

| What | Source | When it is read | Paused while hidden |
|---|---|---|---|
| Hub events | `EventSource /satellites/events` | Opened at load, not on the first visit to Satellites. Closed 60 s after the page is hidden and opened again on return, with a gap line in Activity. A stream closed for good is tried again at 2, 5, 15 and 30 s, then every 30 s. Never tried again once health says the deployment has no hub. | yes, after 60 s |
| Health, `GET /health` | poll | Every 30 s, each read scheduled when the last one answered. At once on return or focus, if the last answer is older than 10 s. | yes |
| Satellites (the three lists) | poll and events | While the tab is open and the page visible: every 3 s while the stream is down, or while a row reads Updating, Restarting, Listening, Answering or In conversation; every 30 s otherwise. A wake word brings the next read forward to 3 s. Also on entering the tab, on return, and on the hub's `config`, `firmware`, `online`, `offline`, `pending`, `ota` and `conversation_*` events. | yes |
| A satellite's status | the `status` event | Applied to its row at once. No request. | n/a |
| Chips that run out | local | One timer, to the soonest end: Listening 15 s, Restarting 120 s, Update failed 600 s. | not needed |
| The Satellites count in the dock | the satellite list | At load, on `pending`, `online` and `offline` events while the tab is closed, and on each health answer while the stream is down. | yes |
| Jobs | the polling ladder | Only while Jobs is open and the page visible, while a job is live, or after health shows tts-long's `queued + running` changed (a job queued or finished on another device). | yes, except every 30 s while a job is live |
| Vocabulary profiles | on demand | On entering Vocabulary or Transcribe, if older than 5 s. On return or focus, if older than 30 s. After this page's own writes. | n/a |
| Voices and cloned voices | on demand | On entering Speak, if older than 5 s. On return or focus, if older than 60 s. After this page's own writes. A change in an engine's readiness redraws the voice groups. | n/a |
| Telemetry | poll | Every 30 s while its section is open, the tab is open and the page is visible. | yes |
| A link's download progress | poll, started by the reader | Every 2 s, as before. | no |

Overlapping reads do not undo each other. A listing of the jobs that was
asked for before a newer one was drawn, before this page wrote to a job, or
under a filter that is no longer chosen is dropped. The same holds for a
telemetry read that a change overtook.

A session that ends while the page is open (signed out elsewhere, expired,
the account disabled) is noticed by the next request, or by a stream or an
image that fails: the page asks `GET /auth/me`, and on a `401` goes to
`/login` with the way back, once.

What is not live, by decision:

* **Wake words and vocabulary profiles are last-write-wins across devices.**
  Wake words saved on another device appear with the next poll of the
  Satellites tab, and an unsaved edit here is kept over them. The vocabulary
  editor warns when the open profile was changed or deleted elsewhere, and
  asks before Save puts this version over a newer one. The wake word editor
  does not warn.
* **Selecting text inside an open transcript on the Jobs tab is lost** on
  each redraw while a job is live, every 2 s. The focus on a button or a
  row's summary is kept.

---

## Why this is its own container, and what it costs

The framework survey argued for serving the page from the gateway itself, and
the argument was good: the page is static, the gateway is already the only
published port, and this estate is organised around not adding containers.

Two things decided otherwise.

The gateway's Containerfile made a specific promise — **283 MB**, described in
its own comment as *"the cheap check on this file — an audio or model library
arriving by accident moves it by gigabytes, not megabytes"*. Ingestion needs
`yt-dlp`. Putting `yt-dlp` in the gateway would make the process that checks
every credential also the process that spawns a subprocess on a URL a browser
chose. Separate images keep that blast radius where it is.

And the brief asked for `services/ui` with a Containerfile, a compose entry, a
workflow matrix row and a README like its siblings.

**What it costs is nothing at the door.** This service publishes no port, and
`compose.yaml` puts it on the `edge` network alone, so the gateway is the only
Calliope service it can reach. If a future edit gives this container a URL
for `stt-stack`, `tts-stack` or `tts-long`, or a network they are on, that is
the moment the container that runs yt-dlp can reach them directly.

### Why the page calls the gateway's paths directly

The page and the gateway are one origin, so the session cookie goes with
every call and there is no CORS, no preflight on an upload, and no second
base URL for someone to get wrong. The page asks for `/v1/...`, `/jobs`,
`/glossaries`, `/satellites/...` and `/health` exactly as an API client
would, and the gateway checks each against the person's scopes. This service
used to forward those calls through `/ui/api/*` with a key of its own
attached; that table, the mount and the key are gone.

---

## Who is asking

**People sign in; this service never sees a password or a key.** The gateway
forwards each `/ui/*` request with `X-Calliope-Identity`, a signed assertion
of who is asking and what they may do, valid for 60 s and for this service
only. `voice_common.identity` verifies it with the public key on this
container's own volume (`/run/calliope`), refuses everything without a valid
one except `/health`, and removes the header before any handler runs, so
nothing this service sends onward can carry it. `/docs` and `/openapi.json`
are off.

**The gateway decides whether a request may be made; this service decides
whose data it touches.**

- **Voice clips.** A clip is saved into the caller's own directory,
  `/voices/users/<user id>/`. Listing and deleting show the caller's own; a
  holder of `voices:write:all` may list and delete anyone's with `?owner=`
  (`me`, `all`, `system` or a user ID; anything else is a `400`). The top
  level of `/voices` is the system's. Nobody can speak in another person's
  voice, an admin included: tts-long resolves `voice` among the caller's own.
- **Links.** A link is one person's job: jobs are keyed by the person and the
  link together, so every route that takes a link answers `404` to anyone
  else, before anything runs. Two people pasting one link get a download each
  and a cached file each, and neither can see, stop or play the other's. The
  resolve allowance is per person, and the jobs are bounded (64 per person,
  4,096 in all, forgotten after 24 hours unused; a running download is never
  the one dropped).
- **`/ui/fetch`**, which sends a finished download to be transcribed, is the
  one call this service makes with a credential. It sends the file to the
  gateway's internal listener, `http://voice-gateway:8081`, with this
  service's own key and the delegation token the gateway gave for that one
  request. The gateway accepts the token twice at most and checks again that
  the person is still signed in, so the transcript is recorded as theirs. A
  key the gateway refuses is a `503 service_key_refused`, not a `401`: the
  person is still signed in, and the fault is the deployment's.

Every outbound request to the gateway is built from named headers and never
from the inbound request's. The downloader child is told a link, a kind, a cap
and a language, and nothing about who is asking.

## What the downloader may reach

**This container fetches pasted links itself.** `app/fetcher.py` is the only
process that imports `yt_dlp`: a child of the server, run as
`python -I app/fetcher.py` with four fixed variables, in a session of its own.
The server reads one JSON line at a time from it, at most 64 KiB each, into
named fields, and never imports yt-dlp itself.

### The SSRF story, in two layers

`app/guard.py` carries the full reasoning; the short form:

1. **A stdlib pre-filter in the server,** on `/ui/resolve` and `/ui/commit`.
   http/https only, no userinfo, ports 80 and 443 only, then `getaddrinfo`
   and a check of **every** address the name resolves to — loopback, RFC 1918,
   ULA, link-local (which includes `169.254.169.254`), CGNAT, multicast,
   reserved, unspecified, and IPv4-mapped or NAT64 IPv6 of any of those.
   `localhost`, `*.localhost`, `*.local`, `*.internal` and `metadata.google.internal` are
   refused by name before resolution is even attempted.
2. **The same rules at every connection, inside the child.** Before it
   imports yt-dlp it replaces `socket.getaddrinfo` and `socket.socket`'s
   `connect`, `connect_ex` and `sendto`, so every answer a name resolves to and
   every peer a socket opens is checked. That covers redirects, URLs found
   inside a page, DASH fragments and DNS rebinding, for the probe and the
   download alike. A refusal comes back as `400 destination_not_allowed`.

**Still open, stated rather than hidden.** A native network stack — ffmpeg,
aria2c, curl_cffi — connects in C and never passes the child's check, so the
image carries none and its build fails if one arrives. And the check is a
patch inside the child's own interpreter: if yt-dlp itself were compromised,
its code could undo it, then read this service's key, every person's clips and
downloads, and send them out. The key opens nothing without a person's
delegation; against compromised code **the backstop is the network.** This
container needs nothing on the LAN, so an egress rule for it may block RFC 1918,
ULA and link-local ranges with no exception. It must also block the home's own
WAN address and public IPv6 prefix: a router with NAT loopback hands those to
the reverse proxy, often with a LAN source address, so a proxy's access list
must not trust a source address alone. Write the rule down; do not assume it
([ADR 0024](../../docs/adr/0024-links-fetched-in-voice-ui.md)).

**The child is held to its share.** Its data is capped at 256 MiB and its OOM
score raised to 1000, so a gzip bomb or a huge page ends in an error line and
the kernel kills a child before the server. It may write at most the cap of
its kind (500 MiB, 64 MiB for a clip, 8 MiB for subtitles, nothing at all for a probe), enforced
three ways. A probe has 20 s, the wait for a slot included; a download 30
minutes, then its process group is killed. At most three downloads and two
probes run at once, two downloads and one probe per person.

---

## Ingestion without ffmpeg

**Nothing is downloaded before the user says so.** `/ui/resolve` runs a probe
that writes nothing and leaves a pending job; `/ui/commit` starts the download,
or finds it in the cache; `/ui/abandon` drops the job, kills a running
download and deletes a file too big to cache.

**One native file per link, nothing merged, converted or trimmed.** There is
no ffmpeg in the image, so:

| What | How |
|---|---|
| Audio | Opus at 96 kbit/s or less first (YouTube's itag 250, about 0.5 MB a minute), then any audio-only stream, then the smallest file with picture and sound, for its sound |
| Keep the video | Only where the site offers one file with picture and sound, 720p or less preferred. **YouTube offers none** without a JavaScript runtime, so the box is greyed there, with "(not offered for this link)" |
| Start at, Stop at | The whole audio comes down once, and stt transcribes only the window (`clip_start`, `clip_end`). The player opens on it |
| A clip for cloning | AAC first, sources up to ten minutes and 64 MiB; the browser cuts the clip out |
| HLS-only sites | Refused at resolve: *This site offers no stream this server can fetch without ffmpeg.* Without ffmpeg an HLS download is MPEG-TS in an `.mp4` no browser plays |
| Live and upcoming streams | Refused at resolve, with the reason |
| Playlists and channels | Refused at resolve: paste the link of one video |

A finished file is checked before it is kept: exactly one regular file,
called `media.` with an allowed suffix, more than 0 bytes and within the cap.
An audio-only `.webm` or `.mp4` is renamed `.weba` or `.m4a`, so the page picks
the `<audio>` element.

### The cache

It only avoids downloading the same thing twice. A finished file is
`/cache/<sha256 of person, link and kind>.<ext>` in the `ui-cache` volume, and
the directory listing is the index: no database, no timer, no state anywhere
else. Last use is the file's atime, moved on a cache hit, a transcription, a
subtitle read and a playback; mtime never changes, so the player's ETag stays
the same.

| Rule | Value |
|---|---|
| A file of 128 MiB or less | Kept a day after its last use. All of them together stay under `UI_CACHE_BYTES` (1 GiB), least recently used out first |
| A bigger file, or one bigger than `UI_CACHE_BYTES` | Not cached. Deleted on abandon, when its job goes, when the same person finishes another big file, or an hour after its last use |
| `UI_CACHE_BYTES=0` | The cache is off: every file is big |
| Before a download | Free space minus what running downloads may still write must leave the cap and 64 MiB, evicting small files first; otherwise the download fails and says so |
| What is deleted | Only names the cache wrote, and the `jobs/` work directories. A `UI_CACHE_DIR` pointed at the wrong directory loses nothing |

Entries are per person, so a hit tells nobody what anyone else fetched.
Deleting the volume costs re-downloads and nothing else. Jobs are in memory: a
restart loses a download in progress, and resolving the link again finds the
cached file.

### Keeping yt-dlp current

YouTube breaks yt-dlp every few weeks. `yt-dlp` is pinned in
`yt-dlp/requirements.txt`, which `requirements.txt` installs.
`.github/dependabot.yml` opens a pull request for each release, and `/health`
reports the version that runs. When links start failing with an extractor
error, merge the bump, tag a release, and update compose.

The pin has a directory of its own because Dependabot reads every requirements
file in the directory it watches. The `./packages/common` path in
`requirements.txt` is relative to the repository root, so Dependabot would not
find it, and its run would fail with no pull request.

---

## Subtitles instead of a transcription

A video with **real, human-written subtitles already has a transcript**. The
confirm card offers to take it, and `POST /ui/commit {captions:true}` starts a
captions download — yt-dlp skips the media, fetches the subtitle track alone in
the language the probe found, and writes a `.vtt` or `.srt`. About two
seconds, no media, no Parakeet, and a better transcript than this stack would
produce from the audio.

**That path was broken, and this is what was wrong with it.** `/ui/fetch`
streamed whatever file had finished into
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

Two details worth having written down:

- **The parsing happens in the browser.** The page already has a SubRip/WebVTT
  parser for the karaoke highlight (`CUE_LINE`, `parseSubtitles`), and a second
  one in Python would be two implementations that must agree about what a cue
  is, in two languages, with only one of them tested. The route reads bytes and
  decides nothing about them.
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

**stt-stack has no size ceiling of its own.** `services/stt/app/main.py:201`
is a bare `file.file.read()` on an `UploadFile` — no `Content-Length` check, no
cap, no streaming — so a 4 GB MKV would be buffered whole into a container
limited to 6 GB, and the failure would be an OOM kill rather than a message.
The gateway refuses an upload over `GATEWAY_UPLOAD_MAX_BYTES` (512 MiB) before
it reaches stt, and the page refuses a file over `UI_MAX_UPLOAD_BYTES` (2 GiB
by default, read from `/ui/config`) before a byte is sent.

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

On this stack a voice **is a file**: `TTS_VOICE_DIR/narrator.wav` is the voice
`narrator`, resolved by tts-long's registry and handed to Chatterbox as
`audio_prompt_path`. There is no per-request reference field on the wire at any
layer.

So "use my own voice" is: record or drop a clip, the browser transcodes it to
24 kHz mono WAV, this service writes it into a volume shared with tts-long, and
the voice is selectable. In the picker it is one more row, and each row carries
its own speed tag.

**Picking the voice picks the *backend*. Whether it also picks the engine
depends on what kind of voice it is**, and that is the distinction the picker is
built on:

* **A clip does not carry an engine.** `narrator.wav` is read by `chatterbox` and
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

So an option's value is `engine:name` — `kokoro:bm_george`, `chatterbox:narrator`,
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

**From a link, the browser cuts the clip.** The sheet resolves the link,
commits it for a clip and waits up to two minutes; then the recording comes
down from `/ui/media`, and the same code that cuts an upload cuts this one, from
Start at for Take seconds, to a 24 kHz WAV with a preview and **Save voice**.
There is no ffmpeg on the server to trim with, so the whole recording comes
down, and the server takes sources of up to **ten minutes** for this (`400
too_long_for_clip` otherwise, with the reason): the browser has to hold the
recording to cut it. The length is the site's word, so the download is also
capped at 64 MiB, and a bigger one fails with the reason. A recording the
browser cannot decode is said to and never sent as it is.

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

Both are entirely client-side. No route changed, and no request carries a
new field except one that the expert panel could already send by hand.

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
with `api()` and turned into a blob URL rather than pointed at directly, so a
`409` (not ready yet) or a `404` (swept) is said in words rather than left as
a silent player.

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

The link path is the interesting refusal. `/ui/fetch` streams the downloaded
file straight into the gateway server-side and the browser never receives a byte —
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
   the default: `/ui/fetch` streams the cached file → gateway → stt
   server-side, which is what makes a two-hour podcast cost a transcript
   rather than 131 MB.

`GET /ui/media` is the way back and it is narrow. It serves a file this
service has **already** downloaded, only for the caller's own finished job,
and only a file that is media. It is Starlette's `FileResponse` over the
cache: `Range`, `If-Range` and `416` are answered properly, with
`X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox` and
`Cache-Control: private, no-store`, because the bytes are a stranger's. Ranges
are not a nicety: without them a `<video>` plays from the start and ignores
every scrub. A trimmed link opens the player on its window with `#t=`.

Both elements are `preload="metadata"`, so a transcript that is read and never
played still costs nothing; the bytes come off the NAS when someone presses
play. The sidecar buttons work on that path as they always did.

**Keeping the video is opt-in, per link, and off by default.** Audio-only
never pulls the picture, which is the entire reason a link is affordable, so
the tick sits on the confirm card next to the row it changes: the Download line
stops saying "131 MB of audio only" and starts saying "more than the 131 MB of
audio". Where the site has no single file with picture and sound, the tick is
greyed and says so. With it off, an audio-only link
still plays and the transcript still follows along — a karaoke highlight needs
a clock, not a picture; only the caption band needs the frame.

**The gateway needs `("GET", "/ui/media")` in `UI_PATHS`.** Without it the page
404s on playback when it is served from the published port, which is how
`DELETE /jobs/{id}` stayed unreachable while tts-long had implemented it all
along. The page says so honestly when it happens rather than blaming the
browser for a file it never received.

---

## The estimate, and the number this repository contradicts itself about

The confirm dialog quotes a transcription time, and when the page was built
the rate behind it was stated three different ways in this repository:

| Source | Claim |
|---|---|
| root `README.md` | 47–63× realtime. It now quotes the gateway's 8.5–10.4× |
| `services/gateway/app/main.py`, the `STT` backend | **8.5–10.4×** — and the 900 s `GATEWAY_STT_TIMEOUT` and the 504 help text are built on this one |
| `services/stt/README.md` | about 5× on four cores |

A factor of twelve apart. At 47× the brief's flagship example — a 2h14m podcast
— is about two minutes; at 8.5× it is about sixteen minutes and **946 s of
compute, which exceeds the gateway's own 900 s ceiling**, so the honest dialog
for that file says *"this will not finish in one request — trim it"*.

So: nothing is hardcoded. `UI_STT_RTF` seeds the **conservative** figure, the
page keeps its own EMA in `localStorage` corrected by the `realtime_factor`
every native transcription returns, the number is labelled an estimate, and the
dialog warns whenever `duration / rtf` crosses `UI_STT_BUDGET`.

**Someone must re-measure on the NAS before this is trusted**, and correct
`services/gateway/app/main.py:213`'s `timeout_help` in the same change — otherwise this page and the
gateway's own 504 message will disagree in front of the same user.

The two halves are **never blended into one figure**. Download and transcribe
are separate lines, because for long media the download is the slow half and
one merged number hides which half to blame. The download half is shown as a
**size**, not a time: this container has no idea what the source's bandwidth
is, and the downloader reports the real `speed` and `eta` once it is actually
downloading.

---

## The Satellites tab

The fifth tab is the satellite hub's page
([`services/satellites`](../satellites/README.md)). It lists every satellite
the hub has seen, and below the list it has four closed sections for the
hub's own settings: **Wake words**, **Activity**, **Telemetry** and
**Firmware**. A
deployment without the hub shows one sentence instead. While the tab is
open, the page asks the hub for the lists every 3 s while something is
moving, every 30 s otherwise, and at once on the hub's events. The event
stream opens when the page loads, so the dock's count and Activity are
current before the tab is visited ([What updates by
itself](#what-updates-by-itself)).

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
| **Output** | `audio_sink` or `output_satellite` | Each own output with what it is: "USB DAC · up to 32-bit · 384 kHz", "DAC HAT", "HDMI: the display's own DAC", and the Pi's own jack marked **\*** as "PWM, not a DAC"; the line under it says whether something is plugged into it (where the card can tell), what the chosen one takes and what it is driven at now, and the jack's limits; each option says "plugged in" or "nothing plugged in" where the card can tell. Its own outputs first (a Linux satellite's devices from its last status and the system's default; the Korvo's speaker), then every other adopted satellite with a speaker. One chosen before and gone now stays chosen and says so. Chosen another satellite, the hint names where it plays, or that it plays on its own speaker while that one is offline |
| **Microphone input** | `audio_source` | A Linux satellite only (caps `audio_devices`) |
| **AirPlay** (its own section) | `airplay_enabled`, `airplay_name` | A satellite with caps `airplay` only. **On**, and **Name on phones** (the satellite's own when empty). Its summary says playing, paused, waiting, off or not running; under it, while a phone is connected: the cover, whole at its own shape (a square album, a video's 16:9; from `/satellites/{id}/airplay/artwork`, addressed by its SHA-256), Status, From (the phone and its model), Now playing, Album (and year), Genre, Position, Original file (the phone's own file: its kind and bit rate), Source (classic AirPlay is always ALAC, lossless, 16-bit at 44.1 kHz), Bit rate (1,411 kb/s), Handed on as (the stream into PipeWire), Played at (what the card is driven at), Path ("Bit-perfect" when the phone's 16-bit samples reach the card unchanged: the same rate and channels, and a card driven at 16 bits or wider in whole numbers; otherwise what they were converted from and to), Delay here, and the phone's volume. Under them, **Previous**, **Pause** or **Play**, **Next** and **Disconnect**: `POST /satellites/{id}/airplay/{command}`, shown while a phone is connected to a satellite whose agent reports remote control (`status.airplay.remote`), and each one greyed unless the phone takes that command now. A phone that has not handed over remote control takes only Disconnect, and the hint says to use the phone. **Pause** reads **Play** while the phone is paused. Disconnect asks first and names the phone. The phone decides what it does with a command, so it can answer and then do nothing; the hub then says `confirmed: false` (the player did not change within 1.5 s), and the page says "The phone took the command but did not act on it." |
| **Change wake words** | | Opens **Wake words** |

A control for hardware a satellite does not have is not shown at all: a
Raspberry Pi with no ring, buttons or microphone has no light brightness,
Lights switch, ring set-up, colours, Buttons, mic gain, Microphone switch,
Listen or wake word link, and its **Blink** is **Chime** (three of its wake
sounds). What it has but has switched off stays, greyed with the reason.
Its closed row says **Playing** and the track while AirPlay plays, and
otherwise that it is a speaker, not that it failed to listen. **Device**
adds its output format (what the card is driven at now), its temperature
and whether its power supply is too low.

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
| **Listen 5 s** | `POST /satellites/{id}/listen?seconds=5`, played back in the page. A POST because opening a microphone is a side effect; it needs `satellites:listen` and is audited |
| **Stop** | `POST /satellites/{id}/flush`: what the satellite's Set button does. It also stops music Home Assistant is playing on it |
| **Colour**, **Light pattern** (Solid, Pulse, Spin, Off), **Show** | `POST /satellites/{id}/lights` `{"mode", "color"}` |

**Buttons** is a grid: one row per button the satellite reports (Rec, Mode,
Play, Set, Vol −, Vol +, and Side), and a **When pressed** and a **When
released** choice for each. The choices are Nothing, Talk (`ptt`), Stop,
Mute mic (`mute`), Volume up, Volume down, Lights on/off (`lights`), Dimmer,
Brighter and Webhook. A webhook's address is a secret (a Home Assistant
webhook ID is a bearer credential), so the choice names a `secret_url` secret
from the gateway's store, `webhook:secret:<NAME>`, and never holds the
address. **Store the address** stores a new one under that name through
`PUT /admin/secrets/{name}`, bound to the address's host. Every change sends
the whole mapping as `buttons` in one `PATCH`; the hub refuses a raw address
with `422 use_secret`, and the page moves the focus to the name box. The hub refuses a mapping with no
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
| **Token variable**; **Key name** for a language model | `token_env`, or `api_key_env` | A secret's name in the gateway's store, never a value. Optional for a webhook |
| **Assist pipeline**, **Ask again** | `pipeline`, listed by `POST /satellites/ha/pipelines` | |
| **Conversation agent** (under More) | `agent_id` | |
| **If not understood, continue as a conversation with** | `action.fallback` | A conversation word |
| **Keep listening after a reply, seconds** | `conversation.follow_up_s` | 1 to 60 |
| **Pause that ends the command, seconds** | `silence_ms`, sent in milliseconds | 0.2 to 3 s |
| **Pause that ends a follow-up, seconds** | `conversation.silence_ms` | 0.2 to 3 s |
| **Ring colour**, **Use the default** | `colour` (`#rrggbb`), or none for the listening blue | |
| **Double-check**: Off, Record only, On | `verify.mode`: `off`, `log`, `on`. Every mode's, and Record only for a word the hub has said nothing about ([Double-checking a wake word](../satellites/README.md#double-checking-a-wake-word)) | |
| **Also accept** | `verify.spellings`, comma-separated | Up to 12, each up to 40 characters |
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
| **API key**, **Store key**, **Clear key** | `PUT` and `DELETE /admin/secrets/{name}` on the gateway, under the key variable's name, after the password again. A new secret may go only to the word's host; storing into an existing one sends only the value and keeps its hosts. The page is never sent a key back ([Keys](../satellites/README.md#keys)) | |
| **Tools**: Web search, Weather | `tools` ([Tools and the date](../satellites/README.md#tools-and-the-date)) | |
| **Reply limit, tokens** (under More) | `max_tokens` | 1 to 8192 |
| **System prompt** (under More) | `system` | Up to 8000 characters |
| **Test** | `POST /satellites/llm/test`, with the form as it stands | |

The page asks a server for its model list by itself only for the address and
key a word was saved with, because asking sends the key. After a change,
press **List models**.

The last row, after the words, is push-to-talk's own entry: what a button set
to Talk does. It has a mode and an action, and cannot be a trigger. It has no
**Double-check**: the hub never checks a button, and its `verify` goes back
as the hub gave it.

**Double-check** is what stops a TV or another device in the room waking a
satellite with something that only sounds like the word: the hub transcribes
the wake word before it answers. **Record only**, every word's default,
answers as before and logs in Activity what **On** would have ignored, so a
word is turned **On** once that log shows it would have ignored the right
things. **Also accept** adds the ways speech-to-text writes a word that the
hub does not know, such as a custom model's name.

**Try a word** (`POST /satellites/routing/test`) runs a typed sentence
through a word's saved action and makes the reply, and plays nothing.
**Custom models** uploads an openWakeWord `.onnx` under a name
(`POST /satellites/wake-words/models`). The word is then offered under
**Add a wake word**. **Delete** removes one no word uses.

### Activity, Telemetry and Firmware

**Activity** is the hub's event stream (`GET /satellites/events`) as a log a
screen reader hears: buttons, wake words, conversations, triggers, updates,
settings set on the device itself, and satellites coming and going. It marks
a gap while the stream was down. A wake word the hub's double-check did not
hear (`wake_rejected`) is a line of its own: "alexa ignored: heard
“Obrigado.”", or, under Record only, "alexa would have been ignored: heard
“Obrigado.”". It leaves out the events the hub publishes
for Home Assistant alone: media streams, AirPlay commands, the names of the
settings changed in the hub's record (`config`, after a `PATCH` or a
satellite's report), and firmware images uploaded or deleted. A `config` or
`firmware` event still asks for the lists at once while the tab is open, so
a change made in Home Assistant is seen without waiting for the 30 s poll.

**Telemetry** is off until turned on ([the hub's
Telemetry](../satellites/README.md#telemetry)). Its summary says whether the
hub is recording, read once with the first list of satellites, and again
every 30 s while the section is open.

| Control | Field or request |
|---|---|
| **Record telemetry** | `PUT /satellites/telemetry {"enabled"}` |
| **Keep** | `level`: **Everything, with what was said** (`full`) or **Timings only, no words** (`timings`) |
| **Days kept** | `retention_days`, 1 to 365 |
| **Download** | `GET /satellites/telemetry/records?limit=20000`, as `telemetry.json`. Shown once something is recorded |
| **Delete all** | `DELETE /satellites/telemetry`, after a question: the records and the clips the double-check kept. The settings stay |

**Firmware** lists the uploaded images and uploads one:

| Control | Field or request |
|---|---|
| **Image (.bin)**, **Version**, **Model**, **Signature**, **Upload** | `POST /satellites/firmware?model=&version=&signature=`. Type **Version** exactly as the build stamped it (`git describe`): the page calls a satellite up to date only when its reported firmware matches. A satellite built with a signing key refuses an image with no **Signature** ([korvo-satellite](../../clients/korvo-satellite/README.md#updates-over-the-air)) |
| **Update every satellite** | On the newest image for a model. One `POST /satellites/ota` for each satellite that would change, after a question that counts them |
| **Roll back every satellite** | On an older image, in place of Update. The question says the image is older |
| **Delete** | `DELETE /satellites/firmware/{sha256}` |

### The routes behind it

The page calls the hub's routes on the gateway itself, at `/satellites/...`,
with the person's session; nothing goes through this service. Each needs its
scope ([the gateway's route table](../gateway/README.md#what-each-route-needs)):
`satellites:read` to see the tab at all, `satellites:control` for the
controls, `satellites:listen` for **Listen**, `satellites:firmware` and
`satellites:update` for **Firmware**, and `satellites:admin` for adoption,
wake words, telemetry and a satellite's name and buttons. Without
`satellites:admin` the hub leaves the button mapping out of its answers
altogether. The secrets an action names are on Admin › Secrets.

Five hub routes are not used by the page, on purpose. `POST /satellites/{id}/inject` runs a
recorded clip through a satellite's real actions, which is a script's job: a
button for it would be one press from Home Assistant acting on a clip.
`POST /satellites/{id}/ptt` is Home Assistant's way to start listening, and
the page has the satellites' own Talk buttons. `POST /satellites/{id}/media`
and `POST /satellites/{id}/media/stop` are Home Assistant's too: its media
player and its announcements upload audio it has already converted with its
own ffmpeg. The page has Say for speech, and its Stop (`/flush`) stops that
music as well. `GET /satellites/telemetry/clips/{name}` serves the audio of a
wake word the double-check did not hear, which is for tuning and retraining
the word's model from the records, not for the page. The device socket
`/satellites/ws` is not here either, because a browser never opens it, and
the gateway refuses one that tries.

## Account and Admin

Both tabs are the gateway's own routes, drawn by this page. They need a
session: no API key can reach them.

**Account** (`/ui/account`) is every person's:

| Control | Request |
|---|---|
| **Change password**: current, new, again | `POST /auth/password`. 15 to 128 characters, not a common password, not the username. Every other session ends |
| **Sessions**, **Sign out**, **Sign out other sessions** | `GET`, `DELETE /auth/sessions[/{ref}]` |
| **API keys**: name, preset, scopes, expiry, **Create** | `POST /auth/keys`. The preset only fills the scope boxes, and only presets within the person's role are offered. The boxes never offer a session-only scope. A scope that caps a key at 90 days takes "a year" and "never" off the expiry list; any other admin-only scope takes "never" off it. A key holding an admin-only scope asks for the password first |
| The new key | Shown once, with **Copy**, and never again: not after a reload, not to an admin |
| **Revoke** | `DELETE /auth/keys/{id}`. The key is refused from its next request, and anything it had open is closed |

**Admin** (`/ui/admin/<section>`) is an admin's:

| Section | What it does |
|---|---|
| **Users** | Create a user (a username and a role, `admin`, `user` or `user-jobs`, with `user` chosen to start with; the temporary password is shown once, and they choose their own at first sign-in), change a role (a change to one that holds less asks first, and says what goes), disable or enable, reset a password ("Also revoke this user's API keys" is ticked by default), delete. Nobody can delete, demote or disable themselves, and the last admin cannot be either |
| **Keys** | Everyone's keys, by user, with when each was last used and from where; revoke. An admin never sees a key |
| **Roles** | The roles and presets against every scope, read-only, with the session-only scopes marked. They are code, not settings |
| **Secrets** | The secret store ([ADR 0023](../../docs/adr/0023-one-secret-store.md)): each secret's name, kind, description, consumers, allowed hosts, who changed it and who last read it. Set or replace a value (a password box, never filled in), clear it, edit its consumers and hosts, **Confirm** an imported one. **Rotate master key** for a generated keyring. Read-only rows for the TLS certificate's expiry, the GPU runner's key file and the firmware signing key |
| **Audit** | What people and services did, newest first, with filters for who, what and the outcome, and a switch for the per-minute counts |

Banners at the top say what needs somebody: keys that expire within 14 days,
imported secrets not yet confirmed, secrets that cannot be decrypted, a
keyring generated on the gateway's volume (back it up separately), variables
a service imported from its environment and that should now be removed, a
removed variable a service is ignoring, and an audit that has reached its
ceiling.

**Every change to a user, every key with an admin-only scope and every secret
write asks for the password again**, valid for 10 minutes, and sends the
same request once it is given. Cancelling sends nothing.

**Every row is built as DOM nodes, never as markup.** Usernames, key names,
audit targets, a session's user agent and a device's hello are all text
someone else chose.

### When the gateway says no

One wrapper handles every refusal the same way, wherever it comes from:

| Answer | What the page does |
|---|---|
| `401` | Asks `GET /auth/me` once. If the session has ended, goes to `/login` with the way back; if not, a service behind the gateway refused, and the page says so and stays |
| `403 step_up_required` | Asks for the password, then sends the same request again |
| `403 insufficient_scope` | Says which scope the account lacks, and switches off the control that asked, for the rest of the visit |
| `403 csrf`, `403 session_required` | Asks the reader to reload, or to sign in again |
| `429` | Says how long to wait |
| `503 locked` | Covers the page with the reason and what to fix |
| no answer at all at load | Covers the page with "Calliope is not answering" and tries again, 2 s at first and doubling to 30 s |

---

## What the page hides, and why each one is right

- **Jobs, for a `user`.** The `user` role holds no `speech:long`, no job
  scope and no `voices:write:own`: each of those runs on the GPU or follows
  something that did ([ADR 0022](../../docs/adr/0022-everything-behind-a-login.md)).
  For that account the page draws no Jobs tab, no Chatterbox or other
  long-form voice in the picker, no **+ Clone a new voice**, and no language
  only a long-form engine speaks. It never asks for `/jobs` or `/ui/clips`,
  and a stream that ends early says what was kept without pointing at a tab
  the account does not have. The gateway refuses every one of those routes
  to the role anyway; the page only stops offering a press it knows will be
  refused. The key form offers it the `user` and `transcribe-only` presets.

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
  profiles*. An *irrelevant* glossary cost +28 % WER on Whisper and nothing
  measurable on Parakeet, which is a finding no slider can express.
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
request applies. That is one half. The **Vocabulary** tab is the other: it
reads a profile's file, creates one, replaces one and deletes one, against
`/glossaries` on the gateway.

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

The page calls `/glossaries` on the gateway with the person's session.
**Each person's profiles are their own**: a `user` or `user-jobs` account
lists and edits only theirs and reads the built-ins. An admin sees every profile grouped by owner
(System first) and edits another person's with `?owner=<user id>`.
`home-assistant` belongs to the system and is listed only for an account
holding `glossaries:ha` or a glossaries `:all` scope. The ceiling is stt's own: 64 KB, a validated name, 500 terms,
a 409 on a built-in, and 50 profiles per person.

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
| `CALLIOPE_RUN_DIR` | `/run/calliope` | Where this service's key and the gateway's public key are: `calliope-svc-ui`, mounted read-only. `/health` says `not_ready` until both exist |
| `UI_GATEWAY_INTERNAL_URL` | `http://voice-gateway:8081` | The gateway's internal listener, the one address `/ui/fetch` sends to. Only that address or a loopback `http://` one (for tests on one machine) is accepted; anything else falls back to the default, with an ERROR that names the variable and not its value |
| `UI_LINKS` | on | `0` hides the link box. File upload and TTS are unaffected |
| `UI_CACHE_DIR` | `/cache` | Finished downloads and downloads in progress: the `ui-cache` volume. Not writable means links are off, and the log says so |
| `UI_MAX_DOWNLOAD_BYTES` | 500 MiB | The most one audio or video download may write, and a clip's 64 MiB is never above it. Keep it at or below the gateway's `GATEWAY_UPLOAD_MAX_BYTES`, which leaves room for the multipart framing |
| `UI_CACHE_BYTES` | 1 GiB | Every cached file of 128 MiB or less, together. A file bigger than this value on its own is not cached, as if it were over 128 MiB. `0` turns the cache off |
| `UI_PROBE_TIMEOUT` | `20` | The probe's time limit, the wait for a slot included. Past it the card has no length or size |
| `UI_FETCHER` | *(unset)* | **Tests only.** A script run in place of `app/fetcher.py`; the browser harness points it at `tests/fake_fetcher.py`. Set, the log warns |
| `UI_MAX_UPLOAD_BYTES` | 2 GiB | Read by the page from `/ui/config`: a bigger file is refused before a byte is sent. The gateway's `GATEWAY_UPLOAD_MAX_BYTES` still applies |
| `UI_MAX_CAPTION_BYTES` | 8 MiB | The cap for a subtitles download. `/ui/captions` buffers rather than streams, and an hour of dialogue is ~100 KB |
| `UI_CONFIRM_SECONDS` | `600` | Below this **and** the size threshold, no dialog |
| `UI_CONFIRM_BYTES` | 50 MiB | The second gate, not an alternative |
| `UI_STT_RTF` | `8.5` | The conservative seed. The page measures its own |
| `UI_STT_BUDGET` | `900` | `GATEWAY_STT_TIMEOUT`. Crossing it warns |
| `UI_VOICE_DIR` | `/voices` | The reference-clip store, shared with tts-long |
| `UI_MAX_CLIP_BYTES` | 25 MiB | |
| `UI_MAX_CLIP_SECONDS` | `30` | Trimmed client-side, enforced server-side |
| `UI_RESOLVE_PER_MINUTE` | `12` | Per person, so `/ui/resolve` is not a free scanner |
| `VOICE_CHOWN_DIRS` | `/voices /cache` | What the entrypoint takes ownership of before dropping to uid 1000. Never `/run/calliope`, which is read-only |

A link setting of an earlier release that this one no longer reads is named
in one warning at start-up if it is still set, never with its value.

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

# Run, on a network the gateway shares and no backend is on. No port: every
# request must come through the gateway, which signs it.
docker run --network edge \
  -v calliope-svc-ui:/run/calliope:ro \
  -v voices:/voices \
  -v ui-cache:/cache \
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
`fastapi.testclient.TestClient` over an httpx transport (`Router` in
`tests/conftest.py`) standing in for the gateway's internal listener, so the whole resolve → confirm → fetch flow,
the upload ceiling and the clip store run in-process. The downloader is a real
child process and a stand-in one: `UI_FETCHER` points at
`tests/fake_fetcher.py`, which speaks `app/fetcher.py`'s protocol, chooses what
to do by a word in the link and opens no socket, so the spawning, the time
limits, the one-file rule and the cache all run for real
(`tests/test_downloads.py`). The real `app/fetcher.py` is tested on its own
(`tests/test_fetcher.py`): its guard on refusals that happen before a packet
leaves, its formats on synthetic lists, and one real download from a loopback
server. Every request is signed as the gateway would sign it, with
`voice_common.conformance`'s test keys.

The identity rules have their own files: `tests/test_conformance.py` (the
shared suite every service runs: no assertion, a wrong audience, an expired
or forged one), `tests/test_owners.py` (a link answers only the person who
resolved it), `tests/test_clips.py` (clip namespaces) and
`tests/test_delegation.py` (`/ui/fetch` sends the delegation and this
service's key and nothing else, and the downloader is told nothing about who
is asking).
`tests/test_account.py` runs the page's session layer in Node: the sign-in
redirect, the step-up prompt, a missing scope, locked mode, and the key form,
the roles Admin › Users offers and the scopes the page asks before drawing a
job against `voice_common.scopes`.

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
| `tests/test_satellites_writes.py` | The page's own Satellites script, run in Node against a fake hub that answers in the order a network does. The five below use its harness |
| `tests/test_satellites_ordering.py` | The same, with events and polls that arrive before or after a save |
| `tests/test_satellites_modes.py` | A wake word's mode, language hint and action, set and saved |
| `tests/test_satellites_llm.py` | A language model word: its provider, model list, key and Test |
| `tests/test_satellites_states.py` | The state word, chip and line each satellite row shows, over every case |
| `tests/test_satellites_live.py` | Status events applied without a request, the 3 s and 30 s cadence, the chips' own expiry timer, a closed stream reported to the live layer |
| `tests/test_wake_words_contract.py` | The wake word fields the page sends against the names the hub's own code reads |

The page's addresses have one of each: `tests/test_navigation.py` runs the
router's pure half in Node (every address read, cut to its shape and written
back), and `e2e/test_routes.py` opens each address in a headless browser
against a local stack (see `e2e/README.md`, which is also how it is run).

What the page reads by itself has two more: `tests/test_live_data.py` runs
the live section in Node on a fake clock (the stream, health, the catch-up on
return, the quiet while hidden), and `tests/test_jobs_live.py` the jobs
listing's rule for which answer is drawn. `e2e/test_live.py` checks the same
in a headless browser.

The Node suites skip without `node` on PATH. None starts a server or reaches
the network.

---

## Layout

| File | |
|---|---|
| `app/static/ui.html` | The whole UI, the Satellites tab included. Inline CSS and JS, no build step, no external request of any kind — it works on a NAS with no internet |
| `app/main.py` | The page and its addresses, its CSP, `/ui/config`, the clip routes, and `identity.install` |
| `app/ingest.py` | Resolve, commit, abandon, progress, fetch, captions and media, and the delegation `/ui/fetch` sends |
| `app/downloads.py` | Each person's jobs, the child runs and their limits, and the cache |
| `app/fetcher.py` | The child: the only process that imports yt-dlp, with the guard installed first |
| `app/guard.py` | What a pasted URL has to survive. **Read this before relaxing anything in it** |
| `app/clips.py` | The reference-clip store |
| `app/config.py` | Every knob, with the measurement behind each default |

## What we reused rather than wrote

- **yt-dlp** (Unlicense), as a library in a child process — every extractor,
  format selection and the download itself. The child adds the guard, the
  limits and the one-file rule around it, and nothing else.
- **Starlette's `FileResponse`** — byte ranges, `If-Range` and `416` for
  playback, rather than a range parser of our own.
- **devnen/Chatterbox-TTS-Server** (MIT, `server.py:670-753`) — the
  reference-audio upload sequence, transplanted in shape.
- **resemble-ai/chatterbox** (MIT) **as a specification only** — the parameter
  ranges and the four-visible-plus-accordion layout. Its ranges are reconciled
  against ours, not copied.
- **PyAV**, already in the stt image — video upload already worked; nothing was
  added for it.
- **tts-long's job queue, chunking and ETA arithmetic** — the page reads them,
  it does not reimplement them.
- **`<dialog>.showModal()`, `OfflineAudioContext`, `Notification`** — the
  modal, the transcoder and the ping. Zero dependencies for all three.
