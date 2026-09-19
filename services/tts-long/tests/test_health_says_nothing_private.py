"""What /health may tell a stranger about a third machine.

MEASURED FROM THE PUBLIC INTERNET, with no credential, during a review of this
branch. The gateway inlines each backend's health body verbatim, and /health is
the one path authentication exempts by design — auth.py says why: the TrueNAS
healthcheck has no key and no way to be given one. So this document is
world-readable whether or not GATEWAY_API_KEYS is set, which is what separates
it from the known no-auth gap: turning keys on does not close it.

It was publishing a private workstation's LAN address and port, and the
runner's own prose, which read "…and <account> is signed in: cannot observe the
user" — an account name and a continuously pollable signal of whether somebody
is sitting at that desk.

The exemption justifies a liveness answer about this stack. It does not justify
a third machine's status document.
"""
import pathlib
import re

REMOTE = (pathlib.Path(__file__).resolve().parent.parent / "app" / "remote.py").read_text()

# The snapshot the gateway inlines. Everything between the dict that starts with
# "reachable": True and its close.
SNAPSHOT = REMOTE.split('"reachable": True,')[1].split("\n                }")[0]


def strip_comments(source: str) -> str:
    """The comments here quote the very keys the assertions forbid."""
    return re.sub(r"^\s*#[^\n]*$", "", source, flags=re.M)


def test_the_runner_snapshot_carries_no_address():
    """Neither told this page's reader anything they did not know — it is their
    own machine — and both told a stranger the shape of the network."""
    code = strip_comments(REMOTE)
    assert 'snap["host"]' not in code, "/health publishes the runner's address again"
    assert 'snap["port"]' not in code, "/health publishes the runner's port again"


def test_the_runner_snapshot_carries_no_free_text_from_the_runner():
    """`reason` and `machine_state_reason` are idlegpu's own words, written in
    another repository, so what they say is not ours to bound. `machine_state`
    is the same answer as an enum: a page can render it and a stranger learns
    nothing."""
    code = strip_comments(SNAPSHOT)
    for field in ('"reason"', '"machine_state_reason"'):
        assert field not in code, f"{field} is back in the published snapshot"
    assert '"machine_state"' in code, \
        "the enum went too, so a page has nothing left to say why it will not run"
