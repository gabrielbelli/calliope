# Browser tests for the page

End-to-end tests for `services/ui/app/static/ui.html`: a headless browser loads
the page from a local stack and clicks through it. They run on a developer's
machine only. CI and the default `pytest` run in `services/ui` never collect
this directory, because `services/ui/pytest.ini` names `testpaths = tests`.

## Run them

```bash
cd <repository root>
PY=<venv>/bin/python                     # the venv described under "The environment"

$PY -m pytest -q services/ui/e2e -p no:cacheprovider                  # everything
$PY -m pytest -q services/ui/e2e/test_smoke.py -p no:cacheprovider    # one file
$PY -m pytest -q services/ui/e2e -k "mobile and dark" -p no:cacheprovider
```

Send the output to a file rather than piping it into `head` or `tail`. A
closed pipe can end the run before its clean-up has finished.

At the end the session prints a report: the browser's version, peak memory,
leaked processes (it should say `none`), outbound connections the stack tried
and was refused, and where the logs and screenshots are.

## What protects the machine

| Rule | How |
|---|---|
| One browser at a time, machine-wide | `fcntl.flock` on `WEBUI/browser.lock`, taken before the stack starts and released after the browser and the stack are down. A second session from any agent waits up to 25 minutes. |
| Never visible | Playwright's `chromium-headless-shell` (a bare binary, no `.app`), launched by path with `headless=True`. `PWDEBUG` is removed from the driver's environment, because it opens a visible browser. |
| Never heard | `--mute-audio`. |
| Never the network | The browser has `--proxy-server` set to a dead loopback port and a resolver that knows no names. A request to anything but `127.0.0.1` fails the test that made it. Every stack process runs behind `launch.py`, which refuses any connection outside loopback and logs the attempt. |
| Nothing left behind | Each stack process is in its own process group with `--marker calliope-e2e-<pid>-…` on its command line. The browser's profile is `WEBUI/profiles/<pid>/…`, and its helper processes share its group. Teardown kills the groups and fails the session if anything had to be force-killed. The next session sweeps an earlier run's leftovers while it holds the lock. Processes are matched on the shape of their command, not on a substring, so a `grep` for the marker is never killed. |
| Bounded | 60 s per test (`pytest-timeout`, signal method), 20 minutes per session (a watchdog that kills everything). Each stack process exits by itself if pytest dies, and at the deadline. |

`WEBUI` is `~/.cache/calliope-e2e`.
Set `CALLIOPE_E2E_DIR` to move it. Everything a run writes goes there:

```text
browser.lock          the machine-wide lock; says which pid holds it
runs/<pid>/           each service's log, network-violations.log, session.json
profiles/<pid>/       the browser's profile, removed at the end of the session
shots/<name>.png      screenshots
```

The last five runs are kept.

To confirm that nothing is left after a run:

```bash
ps -axo pid=,ppid=,command= | awk '$3 ~ /chrome-headless-shell|launch\.py|playwright\/driver/'
```

## The stack

```text
browser -> gateway :8080 (real) -> page server, hub (real); stt, tts, tts-long (fakes.py)
           hub, page server -> gateway :8081 (internal, real) -> stt, tts (fakes.py)
           page server -> its downloader child (../tests/fake_fetcher.py)
           scripted satellites (fakes.py) -> gateway device socket -> hub
```

The browser loads the page from the real gateway, because that is how the page
is deployed. Every call the page makes crosses the gateway's route table, and
a path the page needs and the table lacks is a 404 here, as it would be at
home.

### Signed in, as the deployment is

The gateway runs as a developer's machine may run it: bound to loopback, with
`CALLIOPE_DEV_INSECURE_COOKIE=1` (so the session cookie is `calliope_session`
and works over plain http), `CALLIOPE_PUBLIC_ORIGIN` set to its own address,
and its database, keys and service volumes in the run's directory, new every
session. Its internal listener, `:8081`, is on another free loopback port.

The session's first act is the deployment's first sign-in, through `/login`:
`admin` with the `CALLIOPE_ADMIN_PASSWORD` the stack made up, then a password
of the session's own on the forced change. What that sign-in showed and sent is
kept for `test_auth.py`, because it can happen only once per gateway. Then the
harness, with the admin's session, mints an admin key for itself (the password
again first, as any admin key needs), and the admin creates the user-jobs
user `sam`, who signs in for the first time through `/login` too. `una`, a
user (the role without jobs), and `robin`, a second user-jobs user, are
created and signed in the same way the first time a test asks for them. Every page a test
opens starts from the cookie of one of those sign-ins and nothing else.

Every password and key is made up per session, and the gateway's data, keys
and service volumes are deleted when the session ends.

> The gateway allows twenty sign-in attempts per address per ten minutes, and
> every test comes from 127.0.0.1. A test signs in itself only when it has
> to: to sign out, to meet the step-up prompt, or as a person nobody else is
> using. It never signs out or changes the password of a shared session.

The harness counts every attempt the gateway counts: each sign-in and step-up
from a page or from `stack`, a wrong password included, and not a request
refused before the password is read (CSRF, wrong host). Its budget is 16 in
any ten minutes, four under the gateway's limit
(`stack.SIGN_IN_BUDGET`). The test that spends the seventeenth fails and says
so, and so does a test that meets a 429. A full run spends about a dozen in
its first ten minutes: the first sign-ins of `admin`, `sam`, `robin` and `una`, the
harness's step-up, and the tests in `test_admin.py` and `test_auth.py` that
sign in or are asked for the password again.

### Nothing believes anything but the gateway

The fake stt, tts and tts-long install `voice_common.identity` for their
audience, with the public key the gateway wrote to their service volume, so a
request that did not come through the gateway is a 401 there as it is at home.
What each one keeps is partitioned as the real service partitions it:
tts-long's jobs by owner, stt's profiles by namespace.

So a test never calls a backend or the hub directly. What another device does
goes through the gateway with a key (`stack.api`, `stack.client`), and what a
test needs to know or change behind the page's back goes through the fakes'
control port (`fake.jobs()`, `fake.job(id)`, `fake.backend_health(...)`).

The hub and the page server reach the gateway's internal listener at
`http://voice-gateway:8081`, a constant in `voice_common.identity` and not a
setting. `launch.py --route voice-gateway:8081=<port>` answers that name for
them with this session's port, so they run with the deployment's own address.

### The services

The hub (`services/satellites`) is real too. It runs with a temporary
`SATELLITES_DATA_DIR`, no MQTT, and the pinned wake word models for
`hey_jarvis` and `alexa`. The models are copied from
`<venv>/share/calliope-e2e/wakewords`, or from `SATELLITES_TEST_WAKEWORD_DIR`
when that is set. If they are missing, the hub starts with no wake words
instead of downloading them. Its secrets are in the gateway's store, which
outlives a hub restart and every test: `stack.store_secret` puts one there,
and `conftest.py` clears the store after any test that stored one, that way or
through the page.

The page server runs with a clip store that holds one voice, `narrator`, which
is the admin's own, and a resolve limit of 600 a minute instead of 12, because
a file of link tests runs as the one admin. Its downloader is the stand-in
`services/ui/tests/fake_fetcher.py` (`UI_FETCHER`), which speaks the real
child's protocol, writes into the run's own `cache/` and opens no socket.
`stack.py` refuses to start the page server without it: the real yt-dlp would
reach for the network from a child process, and `launch.py` walls in only the
page server's own.

A stack starts and signs in in about 10 seconds, once per session. The fake
backends and the scripted satellites are in one process. Everything is in
`stack.py`, `fakes.py` and `conftest.py`.

## Writing a test

```python
def test_a_new_glossary_is_saved_and_listed(page, goto, fake, browser_log, screenshot):
    goto("/ui")
    page.get_by_role("tab", name="Vocabulary").click()
    ...
    put = fake.requests(backend="stt", method="PUT", path=r"^/glossaries/")
    assert put[-1]["json"]["text"].startswith("cloud code = Claude Code")
    screenshot("vocab-saved")
```

### Fixtures

| Fixture | What it is |
|---|---|
| `page`, `admin_page` | A Playwright `Page`, 1440 x 900, light, in a new context, signed in as the admin. |
| `user_jobs_page` | The same, signed in as `sam`, a person with the user-jobs role (`conftest.USER_JOBS`). |
| `user_page` | The same, signed in as `una`, a person with the user role, which has no jobs (`conftest.USER`). |
| `new_page(viewport="desktop", scheme="light", mobile=None, reduced_motion="no-preference", notifications="denied", user="admin")` | Another page in its own context. `viewport` is `"desktop"`, `"mobile"` (390 x 844, touch, 2x pixels) or `(w, h)`. `user` is `"admin"`, `USER_JOBS`, `USER`, `OTHER` (a second user-jobs user, `robin`), any other username (created as a user-jobs user and signed in on first use), or `None` for nobody. `notifications="granted"` makes `Notification.permission` read `"granted"` (the headless shell itself answers `"denied"` whatever the context grants); the default makes it `"denied"` and `requestPermission()` answer `"denied"`, so queueing a job never waits on a prompt nobody can see. All of them are closed when the test ends. |
| `people(username)` | That person's `stack.Account`: username, role, ID, the password they chose and their signed-in cookie. |
| `api_key(preset, user="admin")` | A key of that preset, made by that person with their own session, as the Account tab makes one; once per session. |
| `first_sign_in` | What the session's first sign-in showed and sent: every answer's text, the markup and every field's value at the forced change and once signed in, and the bootstrap value to search them for. |
| `goto(path="/ui", target=None)` | Loads a path from the page's origin (the gateway) and waits for `load`, not `networkidle`: the Satellites event stream never goes idle. |
| `screenshot(name, viewport=None, scheme=None, full_page=False, target=None)` | Writes `WEBUI/shots/<name>.png` and returns its path. `viewport` resizes the page. `scheme` switches `prefers-color-scheme`. CSS animations are stopped for the shot. |
| `browser_log` | What the test's pages did. `.sent(method, path)` lists the requests that left the page (with the JSON body), and `.responses`, `.failed`, `.console` and `.errors` (uncaught exceptions) hold the rest. `.bad_responses()` returns the 404s, 405s and 5xx responses. `.allow(status, path)` marks one the test caused on purpose (a link to a job that is gone asks for it and gets a 404); `.unexpected()` is the rest. |
| `dialogs(target=None, answer=True)` | Answers every `confirm()` and `alert()` on `target` (the default page) with Yes, No, or `answer(dialog) -> bool`, and records each one's type and message in `dialogs.seen`. Without it Playwright dismisses them, which is a No the test never chose. |
| `fresh_hub` | Restarts the hub before the test with an empty data directory, so Kitchen and Lounge are adopted again and Hallway waits. The scripted satellites start over too, as they were when the stack started (a volume, a mute, a forget and a skipped track are gone). For every test that changes what the hub holds: an adopt, a forget, a rename, a wake word, firmware, telemetry, a setting. |
| `fake` | The fakes' control API (`stack.FakeControl`, below). Failures a test injects are cleared after it. |
| `stack` | The running stack: `.url` (the gateway), `.admin`, `.api` (the gateway with the harness's admin key), `.client(key=None)` (a new client of the gateway with a key, the admin's by default), `.person(account)` (the gateway as that person's page calls it), `.mint_key(account, preset)`, `.store_secret(name, value, hosts=[...])`, `.clear_secrets()`, `.restart_hub()`, `.stop_hub()` and `.start_hub()` (the hub down and back, on the same port and with its data), `.inject(key, wav_bytes, **params)` (a clip through a satellite's listening path, `POST /satellites/{id}/inject` with `play=0`), `.describe()`. |

Each context grants the microphone (a synthetic device, never the real one)
and the clipboard. It uses `en-GB`, `Europe/London`, and blocks service workers.

Every test that passes is then checked for three things it does not have to
say: no uncaught exception on the page, no 404, 405 or 5xx it did not `allow`,
and no request that failed outright other than one cut short (`ERR_ABORTED`).

Two helpers in `conftest.py` are for what signing in changed:

| Helper | What it is for |
|---|---|
| `password_if_asked(page, password, done)` | Waits for `done`, entering the password first if the page asks for it again. The shared admin session entered it at the start of the session, and a step-up lasts ten minutes, so whether the prompt comes depends on when the test runs. |
| `fetch_as_page(route)` | `route.fetch()` with the Fetch Metadata the page's own `fetch()` carries. Playwright sends it from outside the page, and the gateway refuses a cookie request without it. |

A route pattern for one of the gateway's paths is anchored to the host
(`re.compile(r"//[^/]+/satellites$")`): `/satellites$` alone also matches the
page's own address, `/ui/satellites`, and holds the page itself.

`page.wait_for_function` takes a FUNCTION, `"() => ..."`, never a bare
expression. The page's policy forbids `eval`, and Playwright evaluates a bare
expression with it, so the wait fails with an `EvalError` instead of waiting.
The page's own top-level names (`NAV`, `navPath`, `SATELLITES`) can be read
inside it.

### The control API (`fake`)

| Call | What it does |
|---|---|
| `fake.requests(backend=, method=, path=, since=)` | What reached the fake backends, oldest first: method, path, query, the `x-` headers, status, and the JSON body or form fields (a file as its name, type and size). `identity` is who the gateway said asked (`sub`, `kind`, `cred`, `scopes`), or None. An `X-Calliope-*` header is recorded by name only, under `calliope_headers`, and a cookie or an `Authorization` header only as `cookie: true` and `authorization: true`; only `ha`, `llm` and `hook`, which are handed tokens a test made up, keep the `Authorization` value. `began` and `seq` are when the request arrived and when it was answered, on one clock. `backend` is `stt`, `tts`, `tts_long`, or `ha`, `llm` and `hook` for what the hub sent a wake word's action or a button's webhook. `path` is a regular expression. |
| `fake.last_seq()`, `fake.clear_requests()` | For "only what happened after this point": the request log's clock now, and a log emptied. |
| `fake.fail(path, status=500, method=, backend=, json_body=, times=, delay=, headers=, cut_after=)` | Answers matching requests with an error instead. With `status=None`, it only delays them, which is useful for loading states; with `status=None` and `cut_after=n`, a streamed `/v1/audio/speech` sends n deltas and then tts-stack's in-band error frame, with `headers` on its response (tts-long's `X-Job-Id`, say). |
| `fake.health(backend, **fields)` | Merges fields over a backend's `/health` (`None` removes one), and returns once the gateway has probed the backends again: it keeps a probe for 5 s. For example, `fake.health("tts_long", runner=None)` hides the GPU runner panel. A field the gateway's allowlist does not name never reaches the page. |
| `fake.fresh_health()` | Until the gateway's `/health` comes from a probe that arrived after now, for a change a backend's health shows (a job queued elsewhere). A probe already under way carries the old health, even when it is logged after. |
| `fake.backend_health(backend)` | What a backend's `/health` answers now, overrides included. |
| `fake.transcript(text)` | What every transcription returns from now on. |
| `fake.glossaries(writable=True, reason=None, strict=False)` | How profile writes are treated until `reset()`. `writable=False` lists `writable: false` (with the server's `reason` when one is given) and answers a `PUT` or `DELETE` with 503, as a deployment with no volume does. `strict=True` refuses a one-word left-hand side (`belly = Belli`) unless the `PUT` sends `force`, with a reason that says "send force", as the real service does. |
| `fake.add_job(**fields)` | A tts-long job, the admin's unless `owner` says whose (`None` is a system record). Scripted by default: it is queued for 1 s, then finishes one segment every 1.2 s. Use `scripted=False, status="failed"` for a fixed state. |
| `fake.jobs(**params)`, `fake.job(id)` | tts-long's listing over every job whoever owns it (the same filters and counts), and one job's whole record (`None` once it is gone). |
| `fake.elsewhere(to, username, password, host="127.0.0.1")` | The address of another site's page that submits a sign-in form to `to` by itself: on `127.0.0.1` it is the gateway's site with another origin, on `localhost` another site. |
| `stack.fetches()` | Every download the stand-in fetcher ran, oldest first: its argv (`fetch KIND CAP LANG -- URL`), environment, cwd and pid. A cache hit runs none. |
| `fake.reset()` | Restores the request log, failures, health, transcript, jobs and glossaries to their start state. The hub is not reset. `stack.restart_hub()` gives a fresh one in about a second. |

### The scripted satellites

Three devices connect to the gateway's device socket, which relays them to the
real hub, once the session has signed in; the two adopted ones are adopted
through the gateway with the harness's admin key.

| Key | Id | Device | State |
|---|---|---|---|
| `kitchen` | `020000000001` | ESP32-Korvo (caps as the firmware sends them) | adopted as Kitchen |
| `lounge` | `020000000002` | Raspberry Pi with AirPlay, two outputs (one with a jack), health readings | adopted as Lounge, playing a track with a cover |
| `hallway` | `020000000003` | ESP32-Korvo | waiting to be adopted |

They send a status every 10 s. They apply a `config` and report it back at
once. They answer `airplay_command` (play, pause, next and the rest change what
the Pi reports), store earcons, reboot, and come back pending after `forget`.

They take an OTA image chunk by chunk and come back on the new version. Like
the real firmware, they report a signing key (`caps.ota_key`), so the hub
skips an unsigned image for them and says why. To see the whole update, upload
the image with `signature=stack.UNCHECKED_SIGNATURE`. The hub has no public key
set, so it checks only that the signature is well formed. The image's first
byte must be `0xE9`, as in an ESP32 application image.

| Call | What it does |
|---|---|
| `fake.satellite_status(key, cause=None, **fields)` | Changes what the device reports and sends a status now. `cause="local"` is a change made on the device, such as a phone's AirPlay slider. `status_every=` changes the clock. |
| `fake.satellite_received(key, type=None, since=0)` | The JSON messages the hub sent it. |
| `fake.satellite_airplay(key, state=, on_command=)` | `state` is `playing`, `paused` or `idle`. `on_command` is `answer`, `refuse` (403 from the phone) or `ignore` (the hub answers 504). |
| `fake.satellite_mic(key, seconds, clip=None)` | Microphone frames in real time, for the Listen button: a quiet tone, or `clip`, one of `services/satellites/tests/fixtures` by file name (`hey_jarvis_en_gb.wav`), as the room heard it. A clip is a wake word heard live, so the hub's double-check runs on it, which it never does on `/inject`. |
| `fake.satellite_caps(key, **caps)` | Changes what a satellite says it is (merged into its caps; `None` takes one out) and connects it again. A Korvo whose `mic` reports 48 kHz is one the hub will not listen to. |
| `fake.ha_url`, `fake.llm_url`, `fake.hook_url(name)` | Where a wake word's action or a button's webhook can point: Home Assistant (`POST /api/conversation/process`, and the websocket's auth and `assist_pipeline/pipeline/list`), an OpenAI-compatible server (`GET /models` in two pages, `POST /chat/completions` streamed), and a receiver that answers 200. All three are on the control port. `fake.fail(path, backend="llm", status=401)` breaks one. |
| `fake.satellite_button(key, button, action)` | A button press on the device. |
| `fake.satellite_drop(key)`, `fake.satellite_start(key)` | Takes it offline, and brings it back. |
| `fake.satellite_send(key, message)` | Any message, as is. |
| `fake.satellites()` | Each device's state, settings, earcons and frame counts. |

## The fake routes

Their wire shapes are copied from the real services.

| Backend | Routes |
|---|---|
| stt-stack | `GET /health`; `POST /v1/audio/transcriptions` (`json`, `text`, `srt`, `vtt`, `verbose_json` with `words` and `segments` spread over the upload's length, or over the `clip_start` to `clip_end` window of it, with the times on the whole file's timeline); `POST /v1/audio/translations` (Parakeet's 400); `POST /transcribe`. Both transcription routes apply the profiles named in a `glossary` field, as the caller may name them, to the transcript, and report the terms that changed it: `/transcribe` as `repaired`, `/v1` as the `x-glossary-repaired` header. A name the caller cannot see, another person's or `home-assistant` without its scope, is refused with 400 as an unknown profile, listing the names they can, as the service refuses it; `GET /glossaries` (with `?owner=`); `GET`, `PUT` and `DELETE /glossaries/{name}` (`dictation` and `tech` are built in and answer 409; a line without both sides of `=` is refused with its number). A person's profiles are theirs; the admin, who holds `glossaries:read:all`, reads and writes the system's unless `?owner=` names someone, and `home-assistant` is reserved. |
| tts-stack | `GET /health`; `GET /voices` (Kokoro's names and the OpenAI aliases); `POST /v1/audio/speech` (`pcm` and `wav` are real; `mp3`, `opus`, `aac` and `flac` are WAV bytes under their own content type; `stream_format=sse` sends half-second deltas); `POST /speak` (with `X-Segment-Offsets`) |
| tts-long | `GET /health` (the engines from `voice_common.engines`, both lanes, an idle GPU runner); `POST /jobs` (owned by whoever the gateway says asked); `GET /jobs` (the caller's own, or `?owner=all`, `system` or a user ID with `jobs:read:all`, filtered before the counts and the limit; with `kind`, `audio`, `status` and `limit`); `GET` and `DELETE /jobs/{id}`; `GET` and `DELETE /jobs/{id}/audio` (somebody else's job answers 404 unless `?owner=` covers it); `POST /v1/audio/speech` |

At the start, the admin's Jobs tab has five jobs: a finished clone with audio,
one whose audio was deleted, a failed clone, a Kokoro run and a transcription.
A sixth, a satellite's transcription, is the hub's (`svc:satellites`), so it
is on nobody's own list and under The system's for the admin.

Links are resolved without the network. `launch.py` answers
`example.com`, `example.org` and `example.net` with a public address, so use
those in link tests. Any other name fails, as it would with no network.

The stand-in fetcher answers by a word in the link, so each branch of the
confirm card and of a download can be reached. A download takes three seconds
and writes a 12 s WAV. The whole list is in `fake_fetcher.py`'s docstring.

| Word in the link | What it does |
|---|---|
| `unprobed` | Sleeps past the two-second probe limit: the card has no length or size |
| `live`, `playlist` | Refused at resolve, with the reason |
| `private`, `unsupported` | The guard's refusal, an extractor's failure |
| `long` | 7200 s, 55 MB |
| `subs` | Subtitles a person wrote, in English |
| `video` | One file with picture and sound, so "Keep the video" is offered |
| `broken` | Resolves, then fails to download with a 403 |
| anything else | 180 s and 1.4 MB of audio, no subtitles, no single file |

## Running the stack without a browser

```bash
cd services/ui/e2e
<venv>/bin/python stack.py
```

This takes the same lock, signs the admin in through the API, prints the URLs,
and stops on Ctrl-C or after 20 minutes. The admin's password and an admin key
are in a file only you can read, `runs/<pid>/tmp/access.json`, deleted when the
stack stops. Use them to explore the stack with `curl` while you write a test.

## The environment

The venv lives outside the repository (`<venv>` above). It was built from the
repository root on 2026-09-30:

```bash
python3.13 -m venv <venv>
V=<venv>/bin/python
$V -m pip install -r services/ui/requirements.txt -r services/satellites/requirements.txt \
    -r services/gateway/requirements.txt pytest==8.4.2 pytest-timeout playwright==1.63.0
$V -m pip install --no-deps -r services/satellites/requirements-nodeps.txt
$V -m pip install --no-deps -e ./packages/common
```

| Package | Version |
|---|---|
| Python | 3.13.15 |
| playwright | 1.63.0, which drives `chromium-headless-shell` revision 1243 (Chrome 153.0.8010.12) from `~/Library/Caches/ms-playwright`. Nothing was downloaded. |
| pytest, pytest-timeout | 8.4.2, 2.4.0 |
| httpx, fastapi, starlette, uvicorn, websockets | 0.28.1, 0.121.2, 0.49.3, 0.38.0, 17.1 |
| numpy, onnxruntime, openwakeword (no deps) | 2.3.4, 1.30.0, 0.6.0 |
| voice-common | editable, from this worktree's `packages/common` |

If the headless shell is ever missing, the session stops and prints the only
install command it accepts:
`python -m playwright install chromium-headless-shell`. It never downloads the
full browser.
