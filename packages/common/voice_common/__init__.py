"""Shared runtime for the Calliope services.

Five services, and before this package three hand-vendored copies of the same
auth, the same error envelope, the same health route and the same TLS
entrypoint. The copies drifted — 197, 187 and 170 differing lines between the
three app/auth.py files — and one adversarial review round found three
DIFFERENT defects, one per copy, because each had drifted separately. That is
what this package exists to stop, and the argument is measured rather than
predicted.

What belongs here: the wire contract. What a client sees, what an operator
configures, what a healthcheck probes, and what the gateway asserts to a
backend about who is asking (voice_common.identity, voice_common.scopes). What
does not: anything that is a property of one model file, one wheel, one image
or one service's reason for existing. README.md draws the line in full, with
the list of things that were considered and deliberately left behind.

Nothing is imported here, on purpose: docs/tests reads voice_common.engines
with no `cryptography` installed, and a package import that pulled in
identity would stop it.
"""

from __future__ import annotations

__all__ = ["__version__"]

# 2.0: the key middleware is gone, replaced by the gateway's signed assertion.
__version__ = "2.0.0"
