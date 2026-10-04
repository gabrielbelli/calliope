# Changelog

What changed in each release, for people running Calliope. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Every release is a `v*` tag on
`main`; its images are published to `ghcr.io/gabrielbelli/calliope-*` under the
same version.

## [0.2.0] — 2026-10-04

The release that makes Calliope something to leave running: it needs a sign-in,
it reaches rooms through satellites, it fetches links itself, and it can borrow
a GPU.

### Upgrading from 0.1.x

- **Everything needs a sign-in now.** The first visit asks for the admin
  password set in `CALLIOPE_ADMIN_PASSWORD`, and that sign-in replaces it. Every
  client that called the API without a key — Home Assistant, the Mac app,
  scripts — needs an API key from *Account › API keys*.
- **MeTube is gone.** The web page fetches pasted links itself. Remove the
  MeTube service and its `UI_METUBE_*` settings from your compose file.
- **Image tags** in `compose.yaml` move to `v0.2.0`.

### Added

- **Sign-in, users and roles.** Accounts with the roles `admin`, `user` and
  `user-jobs` (`user-jobs` may also queue long-form jobs); sessions that last up
  to a year, or 30 days unused; a forced password change on first sign-in.
- **API keys with scopes.** Several keys per person, each from a preset
  (`user`, `user-jobs`, `speak-only`, `transcribe-only`, `read-only`,
  `monitor`, `home-assistant`, `firmware-release`, `admin`), revocable and
  listed with when each was last used.
- **A secret manager** in *Admin › Secrets* for the stack's own credentials —
  the Home Assistant token, external model keys — instead of environment
  variables, with an audit trail of who changed what.
- **Satellites.** A hub service and two nodes — a Raspberry Pi and an ESP32-S3
  (Korvo-2) — with wake words, a double-check transcription before a command
  word acts, AirPlay on the Pi, optional web search through SearXNG (bundled,
  external, or off), and a Home Assistant integration (0.3.1) that exposes them
  as entities and speakers.
- **Links fetched by the page itself**, with yt-dlp and a small cache (files up
  to 128 MiB kept for a day; a clip taken from a link is capped at 64 MiB):
  transcribe a video's audio, or use its own captions.
- **A GPU runner.** `calliope-tts-runner` runs the long-form engines on an
  always-on Linux card beside the desktop runner; each job goes to whichever
  free runner is faster ([ADR 0025](docs/adr/0025-a-linux-gpu-runner.md)).
- **Speech to text clips and vocabulary**: transcribe one window of a file
  (`clip_start`, `clip_end`), per-request terms, saved glossaries and decoder
  boost.
- **The Mac app, rebuilt.**
  - Settings in the macOS 26/27 style: General, Voices, Engines, Local API,
    Integrations.
  - A preferred voice for each language you add, from this Mac or your
    Calliope server.
  - Two engines behind switches, *This Mac* and *Calliope server*, with a
    connection test.
  - The Kokoro model loads when needed and is released when idle; Calliope
    holds about 75 MB at rest instead of about 500 MB.
  - A local API other apps and scripts can use, holding the server key in one
    place.
  - The `calliope` command: speak, save, transcribe files and links, translate,
    fetch, glossaries, jobs, voice clips, voices, status.
  - A generic agent skill (`calliope-voice`) for spoken explanations, and a
    *Reinstall* button for OpenClip's Speak action.

### Changed

- The web page keeps its bead-in-socket dock and lamp identity through the new
  Account and Admin pages; Admin uses the full width.
- `/health` tells an anonymous caller only `ok` or `degraded`; the per-backend
  detail needs a key.

### Removed

- Keyless API access.
- MeTube, and the gateway's clone-from-link route.

### Fixed

- A wake word was let through unchecked when speech to text was slow to list
  its engines; the hub no longer waits for that list, and a command word that
  cannot be double-checked is ignored rather than trusted.
- OpenClip showed no icon for Speak: the icon's SVG was not well-formed.

## [0.1.2] — 2026-09-19

### Fixed

- tts-long writes a job's record before announcing that it finished.
- The Mac installer reports the version it installs.

### Changed

- The README is a page about the project rather than a transcript.

## [0.1.1] — 2026-09-19

### Added

- The Mac player became an application with a menu bar item, a hotkey, a voice
  you can choose and a Calliope server switch.

### Fixed

- `/health` and the page no longer tell strangers about the deployment.
- A ceiling on uploads.
- Kokoro's phonemiser runs one request at a time.

## [0.1.0] — 2026-09-19

The first release: speech to text, speech, long-form cloned speech and an
OpenAI-compatible gateway, with a web page and a Mac player.

[0.2.0]: https://github.com/gabrielbelli/calliope/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/gabrielbelli/calliope/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/gabrielbelli/calliope/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gabrielbelli/calliope/releases/tag/v0.1.0
