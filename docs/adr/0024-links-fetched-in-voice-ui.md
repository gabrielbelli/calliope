# ADR 0024 — voice-ui fetches pasted links itself, with a small cache

**Status:** accepted
**Date:** 2026-10-02
**Supersedes:** the consequence of [ADR 0022](0022-everything-behind-a-login.md)
that link ingestion is checked at the first hop only.

## Context

A pasted link was fetched by MeTube, a separate TrueNAS app that Calliope
reached by its LAN address. That had four costs:

- **It had no authentication.** Anyone on the LAN could read its history and
  add, start or delete downloads. Calliope's routes were narrower than that,
  but they made it load-bearing.
- **Its queue was shared.** One record per link, whoever pasted it, so
  voice-ui kept a map of who owned which link beside it, with lockouts when
  two people pasted one link, and orphans when either side restarted.
- **Nothing pruned its download folder.** It held every link anyone had ever
  ingested.
- **The probe was unguarded.** voice-ui ran yt-dlp itself to read the length
  and the size, which MeTube resolved and did not report, and nothing checked
  where that yt-dlp connected after the first hop.

The owner asked for the server to be self-contained, and for "a really simple
cache, just to avoid re-downloads; if it's too big you don't cache; don't
overdo it, light not bloated".

## Decision

**voice-ui downloads, in a child process.** `app/fetcher.py` is the only
process that imports `yt_dlp`. voice-ui runs it as `python -I app/fetcher.py`
with four fixed variables, and reads one JSON object per line from it, into
named fields. Before the child imports yt-dlp:

- it installs `app/guard.py`'s rules on every `getaddrinfo` answer and every
  `connect`, `connect_ex` and `sendto`, so redirects, URLs inside pages, DASH
  fragments and DNS rebinding are all checked, for the probe and the download;
- it caps its data at 256 MiB and raises its OOM score to 1000, so a body read
  whole into memory ends in an error line, and the kernel kills a child before
  the server;
- it caps what it may write at the download's cap, and at nothing for a probe.

**No ffmpeg.** One native file over http, https or DASH segments, nothing
merged, converted or trimmed. A site that offers only HLS fails at the probe
with a message that says so, and a live stream is refused.

**Jobs are keyed by person and link.** Two people with one link get two
downloads and two cache entries, and a stranger's token is a 404 before
anything runs. The owner map is gone with the shared queue it existed for.

**The cache is the directory listing.** A finished file is
`/cache/<sha256(person, link, kind)>.<ext>`, last used at its atime. A file of
128 MiB or less is kept a day after its last use, and all of them together
stay under `UI_CACHE_BYTES` (1 GiB). A bigger file is not cached. Nothing runs
on a timer, and nothing but those names and the `jobs/` work directories is
ever deleted.

**stt cuts an excerpt.** Start at and Stop at download the whole audio once
and go to stt as `clip_start` and `clip_end`, which decodes only that window
and returns times on the file's own timeline.

## Consequences

- voice-ui needs nothing on the LAN. An egress rule for it may block RFC 1918,
  ULA and link-local ranges with no exception, and must also block the home's
  WAN address and public IPv6 prefix: a router with NAT loopback hands those to
  the reverse proxy, often with a LAN source address. Proxy access lists must
  not trust a source address alone.
- "Keep the video" works only where the site offers one file with picture and
  sound. YouTube offers none without a JavaScript runtime, so the box is
  greyed there.
- Cloning from a link takes sources of up to ten minutes: the whole recording
  comes down and the browser cuts the clip out of it.
- `mem_limit` for voice-ui goes from 384m to 512m, for up to five children.
- yt-dlp is pinned, and `.github/dependabot.yml` opens a pull request for each
  release; `/health` reports the version that runs. Until now MeTube absorbed
  those updates.
- Jobs live in memory: a restart loses a download in progress, and resolving
  the link again finds the cached file.
- **Accepted risk.** The child runs as uid 1000, like the probe before it. If
  yt-dlp itself is compromised, its code can undo the guard, which is a patch
  in the same interpreter, then read the service key, every person's clips in
  `/voices` and downloads in `/cache`, and send them to a public address. The
  service key opens nothing without a person's delegation; against
  compromised code the only network control is the egress rule above.
- **Trigger to revisit.** Move the fetcher into its own container, with no
  key, no `/voices` and the internet only, before adding ffmpeg, aria2c or
  curl_cffi to the image, or after a yt-dlp vulnerability that runs code.

## If YouTube needs a JavaScript runtime

YouTube audio still resolves without one, through a mode yt-dlp warns is
deprecated. When it stops, add deno and `yt-dlp-ejs` to this image (about
100 MB) and keep `remote_components` unset. Deno is not a way round the guard:
yt-dlp runs it with `--no-remote --no-prompt` and no `--allow-net`, so it has
no network of its own.

## Rejected

- **A fetch sidecar.** A sixth image with no key and no `/voices` would limit
  a *compromised* yt-dlp to the internet, but it costs a CI entry, two
  networks, an HTTP API and a second test suite, and the guard stops SSRF
  either way. It is the next step if the trigger above fires.
- **ffmpeg in the image.** About 430 MB, and C decoders and a native network
  stack beside the service key, for merged video, HLS and trimming at the
  source.
- **Keeping MeTube.** The four costs above, and an app the stack does not
  control in the path of every link.
