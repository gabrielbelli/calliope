# ADR 0015 — The satellite hub may hold an API key, by name, and never shows it

**Status:** superseded by [ADR 0023](0023-one-secret-store.md)
**Date:** 2026-09-28
**Superseded on 2026-10-02.** Every key now lives in the gateway's encrypted
secret store, which wins over the environment, and only the hub reads the
hub's keys. `secrets.json` and `PUT /satellites/secrets` are gone. What stands
from this record: an action names its key and never holds it, no route
answers a value, and a 422 never echoes one. The text below is the record of
what was decided on 2026-09-28.

## Context

A satellite hub destination that needs a credential names it and does not
hold it: `token_env` and `api_key_env` are variable names, and the value was
read from the container's environment at call time. The reason is in
`services/satellites/app/destinations.py`: `wake_words.json` is answered
whole by `GET /satellites/wake-words`, so a key saved in it would be one
request away from anyone with an API key.

The language model destination now targets any OpenAI-compatible provider,
and a hosted provider always needs a key. With keys in the environment only,
each key meant editing the deployment and restarting the hub, and a second
provider meant a second variable set by hand. The page could name a key, but
not supply one.

## Decision

The hub may hold a key. An action still names it, and `api_key_env` is
unchanged, so `wake_words.json`, its merge rules and every `GET` are as they
were. The value lives in one of two places:

- **The process environment**, as before. It wins.
- **`secrets.json`** in `SATELLITES_DATA_DIR`, as `{"version": 1, "secrets":
  {NAME: value}}`, which `app/secret_store.py` manages. It sets mode 0600
  before writing the first byte, tightens a file it finds wider at start and
  logs a warning, and removes the file when the last key is cleared.

`destinations._secret(name)` reads the environment, then the store, on every
request. A stored or cleared key applies from the next request, with no
restart and no re-save. `router.env_status` goes through the same function,
so `env[name]` means "the action will find a value".

The page writes with `PUT /satellites/secrets` and `{"name", "value"}`, and
`"value": null` clears. Both fields are in the body, never the path, because
the gateway and voice-ui log paths and not bodies. No route answers a value.
`GET /satellites/wake-words` gains `secrets`, a map from each name to
`"environment"` or `"hub"`. The hub refuses a name the environment already
sets, with 409 `set_in_environment`, because a value the environment shadows
would look stored and never be sent. `router.quiet_validation` cuts every
`/satellites` 422 down to `{type, loc, msg}`: FastAPI's own 422 repeats the
rejected value, so a key sent to the wrong field would come back in the
answer. The log names a key and never its value.

## What it costs

- **The data volume's backups hold stored keys, in plain text.** Anyone who
  can read the volume or a backup of it has them. The environment remains,
  and wins, for anyone who does not accept that. The README says so where
  the file and the variable are described.
- **The file is not encrypted.** The key to decrypt it would sit on the same
  volume or in the same environment, which protects nothing the file mode
  does not.
- **A stored key can be sent to any address an action names.** This was
  already true of a key in the environment: whoever can save an action can
  point it anywhere and press Try a word, and a picker (the model list, the
  Assist pipelines) sends it without a save. The trust boundary is still who
  may write the configuration (destinations.py). This ADR first said those
  are the same callers who can reflash every satellite. With firmware signing
  on they are not, since a key holder cannot install firmware the board
  refuses. So the boundary is narrower, and stated where it holds: behind the
  gateway every client key can do this (`GATEWAY_API_KEYS` has one tier), and
  the hub's own settings are never sent. A name under its prefix with no
  TOKEN, KEY, SECRET or PASSWORD in it (`SATELLITES_MQTT_URL`, which carries
  the broker's password, or `SATELLITES_API_KEYS`) is refused where an action
  or a picker names it and resolves to nothing anywhere else
  (destinations.hub_setting, 2026-09-28).

## Rejected

- **Keys in `wake_words.json`**, because `GET` answers it whole.
- **A resolution path for the language model destination only.** It would
  need a second code path, and `env` would say "unset" for a key that works.
- **Reading a key back to the page**, even masked. A prefix, a suffix or a
  length is a start on the secret. The page shows where a value lives and
  offers Replace and Clear.
