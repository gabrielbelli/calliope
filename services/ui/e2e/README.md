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
browser -> gateway (real) -> page server (real) -> gateway -> stt, tts, tts-long (fakes.py)
                                                           -> hub (real) <- scripted satellites (fakes.py)
                             page server -> MeTube (fakes.py)
```

The browser loads the page from the real gateway, because that is how the page
is deployed. Every call the page makes goes through both allowlists: the
gateway's `/ui` routes and the page server's `PROXIED` table. A path the page
needs and either table lacks is a 404 here, as it would be in production.

The hub (`services/satellites`) is real too. It runs with a temporary
`SATELLITES_DATA_DIR`, no MQTT, and the pinned wake word models for
`hey_jarvis` and `alexa`. The models are copied from
`<venv>/share/calliope-e2e/wakewords`, or from `SATELLITES_TEST_WAKEWORD_DIR`
when that is set. If they are missing, the hub starts with no wake words
instead of downloading them.

The page server runs with `UI_PROBE=0` (no yt-dlp), `UI_METUBE_FORMAT=wav`, and
a clip store that holds one voice, `narrator`.

A stack starts in about 2 seconds, once per session. The fake backends and the
scripted satellites are in one process. Everything is in `stack.py` and
`fakes.py`.

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
| `page` | A Playwright `Page`, 1440 x 900, light, in a new context. |
| `new_page(viewport="desktop", scheme="light", mobile=None, reduced_motion="no-preference", notifications="denied")` | Another page in its own context. `viewport` is `"desktop"`, `"mobile"` (390 x 844, touch, 2x pixels) or `(w, h)`. `notifications="granted"` grants the permission; the default makes `Notification.permission` `"denied"` and `requestPermission()` answer `"denied"`, so queueing a job never waits on a prompt nobody can see. All of them are closed when the test ends. |
| `goto(path="/ui", target=None)` | Loads a path from the page's origin (the gateway) and waits for `load`, not `networkidle`: the Satellites event stream never goes idle. |
| `screenshot(name, viewport=None, scheme=None, full_page=False, target=None)` | Writes `WEBUI/shots/<name>.png` and returns its path. `viewport` resizes the page. `scheme` switches `prefers-color-scheme`. CSS animations are stopped for the shot. |
| `browser_log` | What the test's pages did. `.sent(method, path)` lists the requests that left the page (with the JSON body), and `.responses`, `.failed`, `.console` and `.errors` (uncaught exceptions) hold the rest. `.bad_responses()` returns the 404s, 405s and 5xx responses. `.allow(status, path)` marks one the test caused on purpose (a link to a job that is gone asks for it and gets a 404); `.unexpected()` is the rest. |
| `dialogs(target=None, answer=True)` | Answers every `confirm()` and `alert()` on `target` (the default page) with Yes, No, or `answer(dialog) -> bool`, and records each one's type and message in `dialogs.seen`. Without it Playwright dismisses them, which is a No the test never chose. |
| `fresh_hub` | Restarts the hub before the test with an empty data directory, so Kitchen and Lounge are adopted again and Hallway waits. For every test that changes what the hub holds: an adopt, a forget, a rename, a wake word, firmware, telemetry, a setting. |
| `fake` | The fakes' control API (`stack.FakeControl`, below). Failures a test injects are cleared after it. |
| `stack` | The running stack: `.url` (the gateway), `.ui_direct`, `.hub`, `.restart_hub()`, `.stop_hub()` and `.start_hub()` (the hub down and back, on the same port and with its data), `.describe()`. |

Each context grants the microphone (a synthetic device, never the real one)
and the clipboard. It uses `en-GB`, `Europe/London`, and blocks service workers.

Every test that passes is then checked for three things it does not have to
say: no uncaught exception on the page, no 404, 405 or 5xx it did not `allow`,
and no request that failed outright other than one cut short (`ERR_ABORTED`).

`page.wait_for_function` takes a FUNCTION, `"() => ..."`, never a bare
expression. The page's policy forbids `eval`, and Playwright evaluates a bare
expression with it, so the wait fails with an `EvalError` instead of waiting.
The page's own top-level names (`NAV`, `navPath`, `SATELLITES`) can be read
inside it.

### The control API (`fake`)

| Call | What it does |
|---|---|
| `fake.requests(backend=, method=, path=, since=)` | What reached the fake backends, oldest first: method, path, query, the `x-` headers, status, and the JSON body or form fields (a file as its name, type and size). `backend` is `stt`, `tts`, `tts_long` or `metube`. `path` is a regular expression. |
| `fake.last_seq()`, `fake.clear_requests()` | For "only what happened after this point". |
| `fake.fail(path, status=500, method=, backend=, json_body=, times=, delay=)` | Answers matching requests with an error instead. With `status=None`, it only delays them, which is useful for loading states. |
| `fake.health(backend, **fields)` | Merges fields over a backend's `/health` (`None` removes one). For example, `fake.health("tts_long", runner=None)` hides the GPU runner panel. |
| `fake.transcript(text)` | What every transcription returns from now on. |
| `fake.add_job(**fields)` | A tts-long job. Scripted by default: it is queued for 1 s, then finishes one segment every 1.2 s. Use `scripted=False, status="failed"` for a fixed state. |
| `fake.metube()` | The fake MeTube's downloads. |
| `fake.reset()` | Restores the request log, failures, health, transcript, jobs, glossaries and MeTube to their start state. The hub is not reset. `stack.restart_hub()` gives a fresh one in about a second. |

### The scripted satellites

Three devices connect to the real hub's WebSocket when the stack starts.

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
| `fake.satellite_mic(key, seconds)` | Microphone frames in real time, for the Listen button. |
| `fake.satellite_button(key, button, action)` | A button press on the device. |
| `fake.satellite_drop(key)`, `fake.satellite_start(key)` | Takes it offline, and brings it back. |
| `fake.satellite_send(key, message)` | Any message, as is. |
| `fake.satellites()` | Each device's state, settings, earcons and frame counts. |

## The fake routes

Their wire shapes are copied from the real services.

| Backend | Routes |
|---|---|
| stt-stack | `GET /health`; `POST /v1/audio/transcriptions` (`json`, `text`, `srt`, `vtt`, `verbose_json` with `words` and `segments` spread over the upload's length); `POST /v1/audio/translations` (Parakeet's 400); `POST /transcribe`; `GET /glossaries`; `GET`, `PUT` and `DELETE /glossaries/{name}` (`dictation` and `tech` are built in and answer 409; a line without both sides of `=` is refused with its number) |
| tts-stack | `GET /health`; `GET /voices` (Kokoro's names and the OpenAI aliases); `POST /v1/audio/speech` (`pcm` and `wav` are real; `mp3`, `opus`, `aac` and `flac` are WAV bytes under their own content type; `stream_format=sse` sends half-second deltas); `POST /speak` (with `X-Segment-Offsets`) |
| tts-long | `GET /health` (the engines from `voice_common.engines`, both lanes, an idle GPU runner); `POST /jobs`; `GET /jobs` (with `kind`, `audio`, `status` and `limit`, and the counts); `GET` and `DELETE /jobs/{id}`; `GET` and `DELETE /jobs/{id}/audio`; `POST /v1/audio/speech` |
| MeTube | `POST /add` (a URL containing `unsupported` is refused), `/start`, `/delete`; `GET /history` (a download finishes 3 s after it starts); `GET /audio_download/…` and `/download/…` (one exact path per finished file, with ranges) |

At the start, the Jobs tab has five jobs: a finished clone with audio, one
whose audio was deleted, a failed clone, a Kokoro run and a transcription.

Links are resolved without the network. `launch.py` answers
`example.com`, `example.org` and `example.net` with a public address, so use
those in link tests. Any other name fails, as it would with no network.

## Running the stack without a browser

```bash
cd services/ui/e2e
<venv>/bin/python stack.py
```

This takes the same lock, prints the URLs, and stops on Ctrl-C or after 20
minutes. Use it to explore the stack with `curl` while you write a test.

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
