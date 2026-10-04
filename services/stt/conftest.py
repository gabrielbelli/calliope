"""Puts the repository root on sys.path, and stands in for the gateway.

tests/test_conformance.py imports `app.main`, which is a package in this
directory rather than an installed distribution. Under pytest's default import
mode the rootdir is added to sys.path only when a conftest.py lives there, so
this file is the reason that import resolves.

Every route but /health now needs the gateway's signed assertion (D52), so the
suites need something that signs one. `gateway` is voice-common's own
`calliope_gateway` fixture under a shorter name: a FakeGateway that writes
identity.pub into a temporary directory, points CALLIOPE_RUN_DIR at it, and
signs with the matching key, so the app built at import verifies it exactly as
it verifies the real gateway's. Imported rather than redefined, so the format
and the fixture live in voice_common alone.
"""

from voice_common.conformance import calliope_gateway as gateway  # noqa: F401
