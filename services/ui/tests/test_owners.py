"""A link answers only the person who resolved it (D36), and each person's jobs stay small.

The token every ingest route takes is the URL itself, which anybody can guess.
Jobs are keyed by the person and the URL together, so a second person using
the first one's token finds nothing at all: the same 404 a link nobody pasted
gets, before anything runs. Two people pasting one link get a download each,
and neither can see, stop or play the other's.
"""

from __future__ import annotations

import pytest

from conftest import ALICE, BOB, fetched, wait_for

URL = "https://media.example/instant-talk"

TOKEN_ROUTES = [
    ("POST", "/ui/commit", {"json": {"token": URL}}),
    ("POST", "/ui/abandon", {"json": {"token": URL}}),
    ("GET", "/ui/progress", {"params": {"token": URL}}),
    ("POST", "/ui/fetch", {"json": {"token": URL}}),
    ("POST", "/ui/captions", {"json": {"token": URL}}),
    ("GET", "/ui/media", {"params": {"token": URL}}),
]


@pytest.fixture
def alice_fetched(client, sign):
    """Alice has resolved, committed and finished URL; Bob is signed in as a second user."""
    api, gateway, fetches = client()
    fetched(api, URL)
    return api, gateway, fetches, sign(sub=BOB)


@pytest.mark.parametrize("method,path,arguments", TOKEN_ROUTES)
def test_someone_else_s_link_is_a_404_and_starts_nothing(alice_fetched, method, path,
                                                         arguments):
    api, gateway, fetches, bob = alice_fetched
    response = api.request(method, path, headers=bob, **arguments)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "unknown_token"
    assert len(fetches.calls()) == 1, "a stranger's token started a download"
    assert not gateway.seen, "a stranger's token reached the gateway"
    assert api.get("/ui/progress", params={"token": URL}).json()["ready"] is True, \
        "a stranger's request changed the download"


@pytest.mark.parametrize("method,path,arguments", TOKEN_ROUTES)
def test_the_person_who_resolved_a_link_can_use_it(alice_fetched, method, path, arguments):
    api, _, _, _ = alice_fetched
    assert api.request(method, path, **arguments).status_code != 404


@pytest.mark.parametrize("method,path,arguments", TOKEN_ROUTES)
def test_a_token_padded_with_whitespace_is_nobody_s_link(alice_fetched, method, path,
                                                        arguments):
    api, gateway, fetches, _ = alice_fetched
    padded = {key: ({**value, "token": " " + URL}) for key, value in arguments.items()}
    assert api.request(method, path, **padded).status_code == 404
    assert len(fetches.calls()) == 1 and not gateway.seen


def test_two_people_with_one_link_get_a_download_each(alice_fetched):
    api, _, fetches, bob = alice_fetched
    state = fetched(api, URL, headers=bob)
    assert state["ready"] is True
    calls = fetches.calls()
    assert len(calls) == 2, "Bob was handed Alice's download"
    assert calls[0]["cwd"] != calls[1]["cwd"]
    from app import downloads
    alice, bobs = downloads.JOBS[(ALICE, URL)], downloads.JOBS[(BOB, URL)]
    assert alice.path != bobs.path, "two people share one cache entry"


def test_abandoning_drops_only_your_own(alice_fetched):
    api, _, _, bob = alice_fetched
    fetched(api, URL, headers=bob)
    api.post("/ui/abandon", json={"token": URL}, headers=bob)
    assert api.get("/ui/progress", params={"token": URL}, headers=bob).status_code == 404
    assert api.get("/ui/progress", params={"token": URL}).json()["ready"] is True


def test_a_person_s_65th_link_drops_their_oldest_and_nobody_else_s(client, sign):
    api, _, _ = client(UI_RESOLVE_PER_MINUTE="1000")
    bob = sign(sub=BOB)
    api.post("/ui/resolve", json={"url": URL}, headers=bob)
    from app import downloads
    for n in range(downloads.JOBS_PER_PERSON + 1):
        assert api.post("/ui/resolve", json={"url": f"{URL}-{n}"}).status_code == 200
    assert api.get("/ui/progress", params={"token": f"{URL}-0"}).status_code == 404
    assert api.get("/ui/progress", params={"token": f"{URL}-1"}).status_code == 200
    assert api.get("/ui/progress", params={"token": URL}, headers=bob).status_code == 200
    assert sum(1 for sub, _ in downloads.JOBS if sub == ALICE) == downloads.JOBS_PER_PERSON


def test_a_running_download_is_never_the_one_dropped(client):
    api, _, _ = client(UI_RESOLVE_PER_MINUTE="1000")
    running = "https://media.example/hang"
    api.post("/ui/resolve", json={"url": running})
    api.post("/ui/commit", json={"token": running})
    from app import downloads
    for n in range(downloads.JOBS_PER_PERSON + 2):
        api.post("/ui/resolve", json={"url": f"{URL}-{n}"})
    assert wait_for(api, running, seconds=0.5)["status"] in ("pending", "downloading")


def test_the_resolve_allowance_is_per_person(client, sign):
    """D36: one person on two devices is one allowance, and a household behind
    one address is several."""
    api, _, _ = client(UI_RESOLVE_PER_MINUTE="2")
    for n in range(2):
        api.post("/ui/resolve", json={"url": f"{URL}{n}"})
    assert api.post("/ui/resolve", json={"url": f"{URL}x"}).status_code == 429
    assert api.post("/ui/resolve", json={"url": f"{URL}y"},
                    headers=sign(sub=BOB)).status_code == 200
