# ADR 0023 — Every secret the stack holds is in one encrypted store in the gateway

**Status:** accepted
**Date:** 2026-10-02
**Supersedes:** [ADR 0015](0015-the-hub-may-hold-a-key.md), in the three
places it has stopped being true: its keys are no longer kept in plain text,
the environment no longer wins, and a key holder no longer reaches every
secret. Its first rule stands: an action names its secret and never holds
it, and no route answers a value.
**Builds on:** [ADR 0022](0022-everything-behind-a-login.md), whose scopes,
step-up and internal listener this store is reached through.

## Context

Before this record the stack's secrets were in four places. The hub read Home
Assistant's token, language model keys and webhook tokens from its
environment, or from `secrets.json` on its data volume in plain text. The
MQTT password was inside `SATELLITES_MQTT_URL`. A button's webhook was a raw
URL in the satellite's config, and a Home Assistant webhook URL is itself a
bearer secret: `GET /satellites` returned it to every key that could read the
satellites, and the hub published it over MQTT. tts-long read the GPU
runner's key from a file at start, so rotating it meant restarting a
container that holds 6.5 GB of model.

ADR 0015 accepted the plain-text file because a key to decrypt it would have
sat on the same volume. That stops being an argument once a process other
than the consumer holds the key.

## Decision

**One `secrets` table in the gateway's database, encrypted.** Each value is
stored as `{"n": name, "v": value}` under MultiFernet, so a ciphertext copied
from one row into another decrypts to the wrong name and is refused, not
served. The table holds Home Assistant's token, language model keys, webhook
tokens, webhook URLs that are secrets themselves (kind `secret_url`, for
wake-word webhooks and button webhooks alike), the MQTT password and the GPU
runner's key. Names keep the variable pattern (`SATELLITES_HA_TOKEN`,
`OPENROUTER_API_KEY`), so `api_key_env` and `token_env` in `wake_words.json`
needed no migration.

**The key is never beside the ciphertext.** It comes from
`CALLIOPE_MASTER_KEY_FILE`, a host file with one key per line, the first in
use. Without one the gateway generates `master.keys` on `calliope-keys`, a
volume only the gateway mounts and never the database's, and says so at every
start and on Admin › Secrets. **The source is sticky:** once a file has been
used, a file that is missing, empty or unreadable is locked mode
(`keyring_unreadable`), never a newly generated key that would encrypt new
secrets under something nobody holds. A generated keyring may be replaced by
a file later; the next start moves every row under the file's key.

**Write-only.** Admin › Secrets sets, replaces and clears a value, and lists
each row's name, kind, description, consumers, allowed hosts, who changed it
and when, who last read it, and whether it still decrypts. Nothing returns the
value, or a prefix, a suffix, a length or a hash of it. Every write needs a
session and a step-up; no key can do any of it. The Satellites tab's key
boxes write through the same route, and `PUT /satellites/secrets` is gone.

**Served to named consumers only.** A service asks
`GET /internal/secrets/{name}` on the internal listener with its own key and
`secrets:fetch`, and must be listed in the row's `consumers`. The answer is
`{value, version, kind, allowed_hosts, max_age: 60}`, never cached by a
proxy. A consumer keeps a value in memory for a minute, never caches a miss,
and keeps its last value while the gateway cannot answer or answers 503, so a
gateway restart or a locked gateway does not take Home Assistant away from
the satellites. A 404 is a cleared secret and stops at once. Pickers, Test and
a provider's 401 ask afresh.

**A value goes only to the hosts its row names.** Each entry in
`allowed_hosts` and each target are reduced to `scheme://host:port` (IDNA
2008, lower case, no trailing dot, no user, the default port written out) and
compared exactly; an entry with no scheme means `https` only. The schemes are
`http`, `https`, `ws`, `wss`, `mqtt` and `mqtts`, so the MQTT password is
bound to its broker like any other value. The store and both consumers use
one function for this, `voice_common.origins`. A request that carries a
secret never follows a redirect. The hub answers 403
`host_not_allowed`, naming the secret and the host, and the page links to the
row. An empty list allows nothing.

**Button webhooks are secrets.** A button's action is
`webhook:secret:<NAME>`, naming a `secret_url` row; a raw
`webhook:https://…` is refused with 422 `use_secret`, and a name whose row is
not a `secret_url` with 422 `not_a_secret_url`. The satellites' listing and
its events omit `config.buttons` for anyone without `satellites:admin`, and
MQTT never carries them.

**The store wins; the environment is imported once.** At start each consumer
posts what it held to `POST /internal/secrets/import`: the hub its
`secrets.json`, the variables its actions name, the MQTT password from its
URL and each button's raw webhook URL; tts-long its runner key file. An import
never overwrites a row that exists, cleared ones included. A variable still
set afterwards is ignored with a WARNING that names it, and Admin › Secrets
says "imported from env `X` on `Y`: remove it". The hub overwrites
`secrets.json` with zeros and removes it once the store holds every name, and
rewrites each button to `webhook:secret:<NAME>`.

**The import is a window, not a door.** Each service may import only the
names its allowlist in `scopes.py` covers, and only until it posts a batch
with `"final": true`; after that the route answers 410 until an operator runs
`docker exec -u 1000:1000 voice-gateway python -m app.admin reopen-import <service>`.
The hosts an imported row may go to are never wider than those the service's
own configuration sends that name to. Every imported row is marked unreviewed,
with a banner, until an admin confirms it. The import scope is removed in
the next release.

**These stay out of the store**, shown on Admin › Secrets as read-only status
rows: the TLS certificate and key (TrueNAS renews them on disk; the row shows
the expiry), the firmware signing key (the row shows its public key's ID; it
must not be anywhere the hub could sign with it), devices' Wi-Fi credentials,
and adoption tokens, which are hashes.

**Rotation logs nobody out.** Passwords, keys and sessions are hashes; only
these rows are encrypted. Put a new key in front of the keyring file and
restart, and the gateway re-encrypts every row under it in one transaction;
then remove the old line. For a generated keyring, Admin › Secrets › Rotate
master key does all three steps.

## What it costs

- **A backup of its own.** The keyring file sits outside every app dataset,
  so it is in no app backup unless someone puts it in one. A generated
  keyring is on `calliope-keys`, which must be backed up apart from
  `gateway-data`. Losing the key loses every stored secret: they have to be
  entered again.
- **Rotating the master key does not protect old backups.** A database
  backup taken before the rotation was encrypted under the old key. After a
  suspected leak of the key, rotate the secret values themselves too.
- **Rollback means typing the keys again.** `secrets.json` is gone after the
  import, and the previous release reads only the environment and that file.
- **Every fetch is not audited.** Each secret's last read is on its row, but
  a fetch becomes an audit event once an hour per secret, version and
  consumer, and so does a value that cannot be decrypted. At one cache miss a
  minute, an event per miss would fill the year-long security tier in about
  70 days.
- **The allowlist is the hub's own word.** "Every name in `secrets.json`" and
  "the names its own configuration refers to" are what the hub says they
  are. What actually limits a compromised hub is the window, which closes at
  its first start, and the unreviewed flag. Nobody should rely on the
  allowlist alone.
- **A row the gateway cannot decrypt answers 503, not 404**, so its consumers
  keep their last good value while an admin stores it again. A cleared secret
  is the only answer that stops a consumer at once.
- **A ceiling of 1,000 secrets.** A service may import any name under its
  button prefix during its window, so the table needed a bound.

## Rejected

- **Per-secret data keys.** MultiFernet with the name inside the ciphertext
  already detects a swap, and rotation comes free.
- **Vault or OpenBao as a provider.** One more service to run, back up and
  unseal, for one household's dozen secrets. The table's shape would not
  change if one were added later.
- **The environment winning, as ADR 0015 had it.** A value the environment
  shadows looks stored and is never sent, and the environment is visible in
  `docker inspect` and the TrueNAS app config.
- **Keeping `secrets.json.imported` as a rollback copy.** A plain-text copy
  on the hub's volume would defeat the encryption, and it would restore stale
  values anyway.
- **Reading a value back to the page, even masked.** A prefix, a suffix or a
  length is a start on the secret.
