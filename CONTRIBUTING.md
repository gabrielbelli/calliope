# Contributing

Thank you for wanting to help. Calliope is one repository holding six services,
four clients and a shared package; this page is how to find your way around it,
run the tests, and send a change.

## Where things are

| Path | What |
|---|---|
| `services/gateway` | The one published port: sign-in, keys, the OpenAI-compatible API, routing |
| `services/stt` | Speech to text (Parakeet, Whisper) |
| `services/tts` | Fast speech (Kokoro) |
| `services/tts-long` | Long-form and cloned speech (Chatterbox), the job queue, GPU runners |
| `services/ui` | The web page, link downloads |
| `services/satellites` | The hub the satellite nodes connect to |
| `packages/common` | What the services share: scopes, logging, the run log |
| `clients/macos-player` | The Mac app and the `calliope` command |
| `clients/home-assistant` | The Home Assistant integration |
| `clients/pi-satellite`, `clients/korvo-satellite` | The satellite nodes |
| `docs/architecture.md`, `docs/adr/` | How it fits together, and why it is this way |

Each directory has a README that says what it does and how to run it on its own.

## Running the tests

Every service is tested on its own, from its own directory, as CI does
(`.github/workflows/build.yml`):

```bash
cd services/gateway
pip install -r requirements-dev.txt
pytest -q
```

The Mac app's tests run on macOS with the Command Line Tools and draw its
Settings window off screen; nothing appears on screen and nothing plays:

```bash
python3 -m pytest clients/macos-player/tests -q
```

The web page's browser tests run one headless browser at a time behind a lock,
with stand-in backends; see `services/ui/e2e/README.md`.

## Sending a change

- **One change per pull request**, against `main`. Pull requests are merged as a
  single squashed commit, so the title and description are what the history
  will say: write them for somebody reading it in a year.
- **Tests with the change.** A fix comes with the test that would have caught
  it; a feature with the tests that say what it does.
- **Comments say why**, not what: the decision, the alternative that was turned
  down, and the measurement or incident behind it.
- **British English** in prose, comments and the interface.
- **No personal data in the repository**: example host names
  (`calliope.example.com`), documentation addresses (`192.0.2.0/24`), no real
  keys, paths or names.
- **Signed commits**, please.

## Reporting bugs

Open an issue with what you did, what you expected, what happened, and the
version (`calliope status`, the image tag, or the page's footer). For anything
that could be a security problem, follow [SECURITY.md](SECURITY.md) instead.
