# ADR 0022 — Everything is behind a login, and the gateway is the only place that checks one

**Status:** accepted
**Date:** 2026-10-02
**Amends:** [ADR 0013](0013-satellites-one-door.md), whose device socket
keeps its adoption tokens and gains a relay assertion;
[ADR 0003](0003-glossary-profile-api.md) and
[ADR 0017](0017-home-assistant-vocabulary.md), whose profiles gain owners.
**With:** [ADR 0023](0023-one-secret-store.md), the secret store, which this
record's identity model is what makes safe to build.

## Context

Before this record the stack had one optional credential. `GATEWAY_API_KEYS`
was unset on the deployment, so every route was open to anyone who could reach
port 30080. Set, it took the web page offline, because a browser has nowhere
to present a Bearer key, and every key it accepted was an administrator's: a
key issued for transcription could also adopt satellites, open their
microphones and point a wake word's action, with its token, at any address.
The backends ran open behind the gateway, so the only boundary was the
network, and the page's own container held a key that made anyone who could
reach it authenticated by it.

The owner asked for three things: every route behind a login, admins and
"only STT and TTS" users, and several API keys per person with scopes.

## Decision

**The gateway owns identity.** Users, sessions, API keys, the services' own
keys and the audit live in one SQLite database on a new volume,
`gateway-data`. Roles, scopes and key presets are code constants in
`packages/common/voice_common/scopes.py`, reviewed with the code and read by
the gateway and every backend from one copy. Admin › Roles shows them
read-only.

**The gateway is the only place a credential is checked.** One pure ASGI
middleware covers HTTP and WebSocket alike, sits outermost, and finds each
route's requirement with the router's own match, so a parameter route can
never stand in for a fixed one declared before it. A route without a
requirement cannot be registered: binding the table raises. The public rows
are exactly `GET /health` (liveness only), `GET /login`, `POST /auth/login`,
`GET /` and the two device sockets.

**Identity reaches each service as a signed assertion.** The gateway signs
`X-Calliope-Identity` with Ed25519 for one audience (`stt`, `tts`,
`tts-long`, `satellites`, `ui`), valid for 60 seconds, carrying the subject,
whether it is a person or a service, the effective scopes and the credential
used. There is no role in it. Each service verifies it with the public key
alone (`voice_common.identity.install`), refuses everything without a valid
one except its own `/health`, removes the header before any handler runs so
nothing forwards it onward, and switches off `/docs` and `/openapi.json`.
Neither voice-ui, which runs yt-dlp on addresses people paste, nor the hub,
which calls addresses people configure, can mint an identity.

**Services decide nothing about access; they partition data.** A job, a run
record, a vocabulary profile and a cloned voice belong to the person who made
them. A service filters by the assertion's subject, widens only for an `:all`
scope, and answers 404 for anything else, so a refusal does not say the
object exists. Records from before this release, and everything a service
does on its own account, belong to the system and are visible only to
`:all` holders. `home-assistant` is a reserved system profile, written only
with `glossaries:ha` or `glossaries:write:all`. A person speaks only in their
own cloned voices or the built-in ones, an admin included.

**Services call each other through a second listener, `:8081`.** It is plain
HTTP on the compose network, never published, and takes only the services'
own keys (`calliope_svc_…`), which the public listener refuses. It serves the
same route table with the same scopes, plus `/runs` and the secret store. The
gateway writes each service's key and its own public key onto that service's
volume, `calliope-svc-<name>`, which the service mounts read-only at
`/run/calliope`, so there is no operator step and no first-use race.

**People sign in; clients use keys.**

| | |
|---|---|
| Sessions | An opaque ID in `__Host-calliope_session` (`Secure`, `HttpOnly`, `SameSite=Lax`), only its SHA-256 stored. A year absolute, 30 days idle, so everyday use never meets a login. Logout, a password change, a role change or a disabled account ends them on the next request |
| Passwords | Argon2id from `cryptography` (t=3, 64 MiB, p=1), two at a time. 15 to 128 characters after NFKC, no composition rules, not one of 10,000 common passwords, not the username |
| Keys | `calliope_` and 36 base62 characters with a CRC, shown once, stored as a hash. Several per person, each with its own scopes, chosen from presets, and an expiry. A key's scopes are recomputed on every request against its owner's current role |
| Step-up | The password again, valid 10 minutes, before any user or role change, any secret write and any key holding an admin-only scope |
| Session-only | `users:manage`, `secrets:manage` and both `keys:manage` scopes are never granted to a key, and every step-up route refuses a key with 403 `session_required`, so a leaked key can mint nothing that outlives its own revocation |
| Must change | Any account with a temporary password (the first admin, a new user, a reset) signs in to a restricted session of 15 minutes that can only change the password |

Three roles, `admin`, `user` and `user-jobs`. A `user` has fast
transcription and fast speech, link ingestion, their own vocabulary profiles
and their own keys. A `user-jobs` user also has the GPU lane: long-form
speech and voice cloning, which run as jobs, and their own jobs and voices.
Compute is the only reason to hold a person back, so jobs are the only
difference between the two, and the page does not show a `user` any control
that needs them. `user-jobs` was first called `speech`, a name that said
nothing about what the role could not do; gateway migration 0002 renamed it
and every key preset of that name, with the same scopes. Key presets narrow a
key further: `user` and `user-jobs`, each everything its role may give a key,
`transcribe-only`, `speak-only`, `read-only`, and for admins `monitor`,
`home-assistant` and `firmware-release`. A person is offered only the presets
that fit their role, so a `user` sees `user` and `transcribe-only`. A key holding a scope in `EXPIRY_CAPPED` (satellite admin,
listen and firmware, audit, full health, and every `:all`) lives at most 90
days. The `home-assistant` preset is outside that set and may live a year or
never expire, so Assist does not stop every quarter.

**The first admin comes from a variable that works until it is replaced.**
With an empty database and `CALLIOPE_ADMIN_PASSWORD` set, the gateway creates
`admin` with no password. That value signs in to a restricted session, in
which the admin can only choose a password of their own, and it keeps
signing in until they have: a tab closed before then leaves it live. From
then on it is ignored. A marker on the gateway's key volume records that it
was used, so losing the database alone does not reopen it.
`docker exec -u 1000:1000 voice-gateway python -m app.admin reset-password <user>`
is the way back in, and needs only host access.

**Cross-site requests are refused by Fetch Metadata, not tokens.** Every
unsafe request that is not Bearer, login and logout included, must come from
the same origin, and every cookie request must be `same-origin` except a
top-level navigation to a page address. `same-site` gets no exception: the
NAS serves other things on the same host name. Login accepts JSON only, issues
a fresh session every time, and works only on the host of
`CALLIOPE_PUBLIC_ORIGIN`, which must be a host name that serves Calliope and
nothing else on any port. A session cookie presented on any other host is
ignored. The gateway never sends a CORS header.

**A configuration fault locks the gateway; it never stops it.** A missing or
plain-HTTP public origin, a removed key variable still set, a weak or missing
first-access password, a database lost with the marker present, a failed
Argon2 self-test, a development cookie on a network bind and an unreadable
secret-store key each put it in locked mode: `/health` answers `degraded`,
the device socket is relayed, `:8081` serves the services, and every other
route on 30080 answers 503 `locked` with the reason and the variable to fix.
The hub reaches stt and tts only through `:8081` and its devices only through
the gateway, so an exit would take every satellite in the house down over one
line of configuration. A backend that sees a removed variable logs an ERROR
every minute, ignores it and reports it in `/health`.

**The satellites' socket stays on adoption tokens** ([ADR
0013](0013-satellites-one-door.md)). The gateway relays it for anyone, adds a
relay assertion (`svc:gateway-relay`), and refuses an upgrade that carries an
`Origin`, except `file://`, which the Korvo's WebSocket library sends and no
web page can. The hub refuses any other caller on the socket, keeps at most
32 devices waiting for adoption (the oldest goes), and limits hellos without
a valid token to 10 a minute per address. A device cannot sign in, and a
key in firmware would be in every flash dump.

**Everything that can be revoked ends what it opened.** Event streams and
long downloads are registered per session and key, and closed on logout,
revoke, disable or role change; an event stream is also closed after 15
minutes so the browser reconnects and is checked again. Link transcription,
where voice-ui sends a download to stt on a person's behalf, uses a
delegation token bound to one call: it travels in its own header, is spent at
most twice, and on every use the gateway checks again that the session or key
behind it still works.

**`/health` has three tiers, each built from a list of named fields.**
Anonymous gets `ok` or `degraded`. `health:read` adds each service's status,
engines, voices and queue depth. `health:detail` adds the runner, MQTT, the
satellites' topology and the variables a backend ignores. A field a backend
adds later appears in neither tier until someone puts it in one. Probes are
cached for 5 seconds, so anonymous polling cannot multiply into the backends.

**The client address is believed only from a proxy the operator names.**
`CALLIOPE_TRUSTED_PROXIES` is the only source of trust. Behind HAProxy in TCP
passthrough, `CALLIOPE_PROXY_PROTOCOL=1` reads the PROXY header before TLS;
otherwise the rightmost `X-Forwarded-For` entry that is not a trusted proxy
is the client. The login throttle is per account and address (from the fifth
failure, 1 s doubling to 15 minutes), per account across all addresses
(addresses that signed in before are exempt), and 20 attempts per address in
10 minutes. Every map is bounded at 100,000 entries.

**The audit has two tiers.** Security events (sign-ins, keys, secrets, a
microphone opened, a firmware upload, an `:all` read) are kept for a year and
never dropped to make room; the tier stops at a million rows with one
`audit_overflow` event and a banner. Refusals anyone on the internet can
produce at will are counted per minute into the noise tier, capped at 100,000
rows. No row holds a value, a body or a token. Each row is also one
`audit {...}` line on stdout, the same format the backends use for the
assertions they refuse.

**Deleting a user is a soft delete.** The account is disabled, its sessions
and keys revoked, and the name stays reserved; its jobs, profiles and clips
stay on disk, owned by that ID and visible only to `:all` holders.

**The identity key rotates without downtime.**
`docker exec -u 1000:1000 voice-gateway python -m app.admin rotate-identity-key`
publishes the new public key before signing with it and keeps the old one
in `identity.pub` for two minutes. The command line runs as uid 1000, the
gateway's own. A plain `docker exec` is root, and a key written as root is
one the gateway cannot read: it would keep the old key, then fail to start at
its next restart.

**The MQTT broker is an authentication boundary this design does not cover.**
The hub's MQTT bridge takes commands from the broker, and those set control
fields (the microphone switch among them) with no Calliope credential at all.
Whoever may publish to the hub's command topics can do what
`satellites:control` does. The broker's ACL must let only Home Assistant
publish there, and Mosquitto as Home Assistant's add-on lets every broker
user publish to every topic until an ACL is added. The hub's README has an
example ACL.

## What it costs

- **Home Assistant stops until it has a key.** The upgrade answers its next
  request with 401, and Assist speech pauses until someone creates a
  `home-assistant` key and reauthenticates the integration. The upgrade path
  in `services/gateway/README.md` puts that step straight after the first
  sign-in.
- **A host name of its own.** Cookie sign-in works only on the public
  origin's host. The NAS's own address on port 30080 still serves API keys,
  but the page there sends you to the public origin. A deployment that cannot
  give Calliope a host name of its own cannot use the page from a browser.
- **One gateway worker, by design.** The login throttle, the delegation
  counts and the stream registry live in the process's memory. A restart
  forgets throttle delays and fails any link transcription in flight; a
  second worker would split all three.
- **A hop for every service call.** The hub's speech calls and the run log
  now pass through the gateway. The gateway hop measured 0.85 ms on a laptop;
  the round trip on the deployed hardware has not been measured yet.
- **An admin-only key lasts at most 90 days.** Scripts that upload firmware
  need a new key each quarter.
- **The broker is outside it.** See the last decision above: the ACL is the
  operator's job, and nothing here can check it.
- **Link ingestion is checked at the first hop only.** voice-ui refuses an
  address that resolves to a private, loopback or link-local range before it
  starts yt-dlp, but redirects inside yt-dlp, DNS rebinding and MeTube's own
  fetches are not covered. *Superseded by
  [ADR 0024](0024-links-fetched-in-voice-ui.md): voice-ui fetches links
  itself, and every connection its downloader makes is checked.*

## Rejected

- **A shared secret between the gateway and the services.** Any holder of it
  can mint an identity, and voice-ui and the hub are exactly the two
  processes that act on input from outside.
- **A `role` claim in the assertion.** A key an admin narrowed to `read-only`
  would be widened again by any service that tested `role == "admin"`.
  Services decide from `:all` scopes only.
- **CSRF tokens.** The page is one file of nearly 18,000 lines; Fetch Metadata needs no
  plumbing in it, and every supported browser sends it.
- **Roles and presets as database rows.** They would need a migration for
  every new scope, and the backends would need a second copy.
- **Refusing to start on a bad configuration.** It takes the satellites down
  with it. Locked mode keeps them up and tells a person what to fix.
- **SSO, OIDC, MFA, password-reset email and invitation links.** Out of scope
  for this release; the CLI reset covers a forgotten password.
- **Per-user CPU quotas** beyond four live long-form jobs per person and the
  existing upload limits.
