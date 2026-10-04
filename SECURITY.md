# Security

Calliope holds voices, recordings, transcripts and the keys to other services,
so a security problem in it is taken seriously.

## Reporting a vulnerability

Please report it privately, through GitHub's
[private vulnerability reporting](https://github.com/gabrielbelli/calliope/security/advisories/new)
(*Security › Report a vulnerability*). Do not open a public issue for it.

Say what is affected (service and version or commit), how to reproduce it, and
what an attacker gains. You will get an answer within a week, and credit in the
fix's release notes unless you would rather not.

## Supported versions

Only the latest release is supported. Fixes are released as a new version; there
are no backports.

| Version | Supported |
|---|---|
| 0.2.x | Yes |
| < 0.2 | No: 0.1.x answered the API without a key |

## What the design assumes

These are deliberate, and reports that rely only on them are not
vulnerabilities:

- **One published port.** The gateway is the only service a network can reach;
  the others are on internal networks.
- **Everything needs a sign-in or a key**, except `GET /health`, which says only
  `ok` or `degraded`. Satellites authenticate with their own adoption tokens.
- **Secrets are encrypted at rest** (Fernet) with a master key the operator
  provides as a file (`CALLIOPE_MASTER_KEY_FILE`), and every change to them is
  in the audit trail.
- **The Mac app's local API answers this Mac only**, on `127.0.0.1`, and refuses
  any request a browser sends (one carrying an `Origin` header). It holds the
  Calliope server's key so other programs on the Mac do not have to.
- **A bare IP address is trusted without a certificate check** when the Mac app
  is pointed at one, because no certificate can name it; a host name is always
  verified.
