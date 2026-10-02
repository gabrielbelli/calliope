"""A link answers only the person who resolved it (D36), and the map that says so stays small.

The token every ingest route takes is the URL itself, which anybody can guess,
and MeTube's queue belongs to nobody. So a second person pasting the same link
is told it is in use, and a second person using the first one's token is told
nothing at all: the same 404 a link nobody pasted gets, before MeTube is asked.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.owners import Owners
from conftest import ADMIN, ALICE, BOB, Router

URL = "https://media.example/watch?v=abcdef"


# ------------------------------------------------------------ the routes --


@pytest.fixture
def alice_resolved(client, sign):
    """Alice has resolved URL; Bob is signed in as a second speech user."""
    api, gateway, tube = client()
    assert api.post("/ui/resolve", json={"url": URL}).status_code == 200
    return api, gateway, tube, sign(sub=BOB)


TOKEN_ROUTES = [
    ("POST", "/ui/commit", {"json": {"token": URL}}),
    ("POST", "/ui/abandon", {"json": {"token": URL}}),
    ("GET", "/ui/progress", {"params": {"token": URL}}),
    ("POST", "/ui/fetch", {"json": {"token": URL}}),
    ("POST", "/ui/captions", {"json": {"token": URL}}),
    ("GET", "/ui/media", {"params": {"token": URL}}),
    ("POST", "/ui/clips/from-link", {"json": {"token": URL, "name": "taken"}}),
]


@pytest.mark.parametrize("method,path,arguments", TOKEN_ROUTES)
def test_someone_else_s_link_is_a_404_and_metube_is_never_asked(
        alice_resolved, method, path, arguments):
    api, gateway, tube, bob = alice_resolved
    tube.finish(URL)
    before = len(tube.requests)
    response = api.request(method, path, headers=bob, **arguments)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "unknown_token"
    assert len(tube.requests) == before, "a stranger's token reached MeTube"
    assert not gateway.seen, "a stranger's token reached the gateway"
    assert URL in tube.done, "a stranger's request changed the download"


@pytest.mark.parametrize("method,path,arguments", TOKEN_ROUTES)
def test_the_person_who_resolved_a_link_can_use_it(alice_resolved, method, path,
                                                   arguments):
    api, _, tube, _ = alice_resolved
    tube.finish(URL)
    assert api.request(method, path, **arguments).status_code != 404


def test_a_link_another_person_has_in_progress_is_a_409(alice_resolved):
    api, _, tube, bob = alice_resolved
    response = api.post("/ui/resolve", json={"url": URL}, headers=bob)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pending_for_another_user"
    assert sum(1 for path, _ in tube.calls if path == "/add") == 1, \
        "the second person's resolve reached MeTube"


def test_a_link_is_free_again_once_its_owner_abandons_it(alice_resolved):
    api, _, _, bob = alice_resolved
    assert api.post("/ui/abandon", json={"token": URL}).json()["reaped"] is True
    assert api.post("/ui/resolve", json={"url": URL}, headers=bob).status_code == 200
    assert api.get("/ui/progress", params={"token": URL}).status_code == 404, \
        "the link changed hands and the first owner can still read it"


def test_a_link_metube_has_forgotten_is_free_for_anyone(alice_resolved):
    """The owner never abandoned it, but there is no download left to protect."""
    api, _, tube, bob = alice_resolved
    tube.pending.clear()
    assert api.post("/ui/resolve", json={"url": URL}, headers=bob).status_code == 200


def test_a_resolve_that_fails_leaves_no_claim_behind(client, sign):
    api, _, tube = client()
    tube.refuse = "Refusing to fetch that"
    assert api.post("/ui/resolve", json={"url": URL}).status_code == 400
    tube.refuse = None
    assert api.post("/ui/resolve", json={"url": URL},
                    headers=sign(sub=BOB)).status_code == 200


def test_a_second_resolve_that_fails_keeps_the_first(alice_resolved):
    """A double click must not cost the owner the download the first click made."""
    api, _, tube, _ = alice_resolved
    tube.refuse = "already in the queue"
    assert api.post("/ui/resolve", json={"url": URL}).status_code == 400
    assert api.get("/ui/progress", params={"token": URL}).status_code == 200


def test_a_link_nobody_owns_is_reachable_only_with_jobs_read_all(client, sign):
    """What MeTube kept across a restart of this service: nobody owns it, so
    nobody but an operator who may read everyone's jobs can touch it."""
    api, _, tube = client()
    tube.pending[URL] = {"url": URL, "title": "kept", "status": "pending"}
    assert api.get("/ui/progress", params={"token": URL}).status_code == 404
    assert api.get("/ui/progress", params={"token": URL},
                   headers=sign(scopes=ADMIN)).status_code == 200


@pytest.mark.parametrize("where", ["pending", "queue", "done"])
def test_a_link_nobody_owns_cannot_be_taken_by_resolving_it(client, where):
    """Kept across a restart, or its claim aged out: it may be somebody's live
    download, and resolving it would make it the caller's to cancel or play."""
    api, _, tube = client()
    record = {"url": URL, "title": "kept", "status": "downloading", "percent": 40}
    getattr(tube, where)[URL] = dict(record)
    response = api.post("/ui/resolve", json={"url": URL})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pending_for_another_user"
    assert not tube.calls, "the refused resolve changed MeTube"
    assert getattr(tube, where)[URL] == record
    assert api.get("/ui/progress", params={"token": URL}).status_code == 404


def test_an_operator_may_resolve_a_link_nobody_owns(client, sign):
    api, _, tube = client()
    tube.finish(URL)
    operator = sign(sub=BOB, scopes=ADMIN)
    assert api.post("/ui/resolve", json={"url": URL}, headers=operator).status_code == 200
    assert api.get("/ui/progress", params={"token": URL}, headers=operator).status_code == 200


def test_two_people_resolving_one_link_together_are_answered_in_turn(
        build, sign, monkeypatch):
    """Alice's resolve is still waiting on MeTube's /add when Bob pastes the
    same link. Bob must not find MeTube's record missing in that gap, decide
    the link is free, and take her download from under her."""
    main, _, tube = build()
    adding, added = asyncio.Event(), asyncio.Event()
    handle = Router.handle_async_request

    async def slow_add(self, request):
        if request.url.path == "/add":
            adding.set()
            await added.wait()
        return await handle(self, request)

    monkeypatch.setattr(Router, "handle_async_request", slow_add)

    async def together():
        async with (main.lifespan(main.app),
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                      base_url="http://ui") as api):
            alice = asyncio.create_task(
                api.post("/ui/resolve", json={"url": URL}, headers=sign()))
            await adding.wait()
            bob = asyncio.create_task(
                api.post("/ui/resolve", json={"url": URL}, headers=sign(sub=BOB)))
            # Long enough for Bob's request to get as far as it can go.
            await asyncio.sleep(0.05)
            added.set()
            first, second = await alice, await bob
            commit = await api.post("/ui/commit", json={"token": URL}, headers=sign())
            return first, second, commit

    first, second, commit = asyncio.run(together())
    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "pending_for_another_user"
    assert commit.status_code == 200, "the first person lost the link they resolved"
    assert main.ingest.OWNERS.owner(URL) == ALICE
    assert sum(1 for path, _ in tube.calls if path == "/add") == 1


def test_a_refused_resolve_at_the_cap_costs_the_person_none_of_their_links(client):
    """A typo or a refused link must not evict a download that is running."""
    api, _, tube = client()
    from app import ingest
    ingest.OWNERS.per_owner = 1
    assert api.post("/ui/resolve", json={"url": URL}).status_code == 200
    tube.refuse = "Refusing to fetch that"
    assert api.post("/ui/resolve", json={"url": f"{URL}2"}).status_code == 400
    assert api.get("/ui/progress", params={"token": URL}).status_code == 200


@pytest.mark.parametrize("method,path,arguments", TOKEN_ROUTES)
def test_a_token_padded_with_whitespace_reaches_nobody_s_record(
        alice_resolved, sign, method, path, arguments):
    """Alice's link with a space in front has no owner, so it passes the owner
    check for a holder of jobs:read:all -- and must then match no record, not
    hers."""
    api, gateway, tube, _ = alice_resolved
    tube.finish(URL)
    record, calls = dict(tube.done[URL]), list(tube.calls)
    operator = sign(sub=BOB, scopes=ADMIN)
    padded = {key: ({**value, "token": " " + URL} if key == "json"
                    else {"token": " " + URL})
              for key, value in arguments.items()}
    if path == "/ui/commit":
        padded["json"]["captions"] = True
    api.request(method, path, headers=operator, **padded)
    assert tube.done[URL] == record, "a padded token changed another person's download"
    assert not [p for p, _ in tube.calls[len(calls):] if p in ("/add", "/start")]
    assert not gateway.seen


def test_the_resolve_allowance_is_per_person(client, sign):
    """D36: one person on two devices is one allowance, and a household behind
    one address is several."""
    api, _, _ = client(UI_RESOLVE_PER_MINUTE="2")
    for n in range(2):
        api.post("/ui/resolve", json={"url": f"{URL}{n}"})
    assert api.post("/ui/resolve", json={"url": f"{URL}x"}).status_code == 429
    assert api.post("/ui/resolve", json={"url": f"{URL}y"},
                    headers=sign(sub=BOB)).status_code == 200


# --------------------------------------------------------------- the map --


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_link_is_claimed_once_and_kept_by_its_owner():
    owners = Owners()
    assert owners.claim("u1", ALICE) is None
    assert owners.claim("u1", ALICE) is None, "claiming your own link again is not a conflict"
    assert owners.claim("u1", BOB) == ALICE
    assert owners.owner("u1") == ALICE


def test_only_the_owner_can_release_a_link():
    owners = Owners()
    owners.claim("u1", ALICE)
    owners.release("u1", BOB)
    assert owners.owner("u1") == ALICE
    owners.release("u1", ALICE)
    assert owners.owner("u1") is None


def test_the_person_who_pastes_the_most_evicts_only_their_own_links():
    owners = Owners(capacity=100, per_owner=3)
    owners.claim("bob's", BOB)
    owners.keep("bob's", BOB)
    for n in range(50):
        owners.claim(f"alice-{n}", ALICE)
        owners.keep(f"alice-{n}", ALICE)
    assert owners.owner("bob's") == BOB
    assert [owners.owner(f"alice-{n}") for n in (46, 47, 48, 49)] == [None, ALICE, ALICE, ALICE]


def test_a_claim_counts_against_its_owner_only_once_it_is_kept():
    owners = Owners(per_owner=1)
    owners.claim("live", ALICE)
    owners.keep("live", ALICE)
    owners.claim("typo", ALICE)
    assert owners.owner("live") == ALICE, "a claim still being resolved evicted a live link"
    owners.release("typo", ALICE)
    owners.claim("next", ALICE)
    owners.keep("next", ALICE)
    assert (owners.owner("live"), owners.owner("next")) == (None, ALICE)


def test_keeping_a_link_someone_else_holds_changes_nothing():
    owners = Owners()
    owners.claim("u1", ALICE)
    owners.keep("u1", BOB)
    owners.keep("gone", BOB)
    assert owners.owner("u1") == ALICE and owners.owner("gone") is None


def test_the_map_never_grows_past_its_capacity():
    """Anyone with ingest:links chooses the keys (recheck M-4)."""
    owners = Owners(capacity=1000, per_owner=10)
    for n in range(20_000):
        url, sub = f"https://media.example/{n}", f"u_{n % 997:016d}"
        owners.claim(url, sub)
        if n % 2:
            owners.keep(url, sub)
    assert len(owners) <= 1000


def test_a_turn_is_taken_one_at_a_time_and_forgotten_when_nobody_waits():
    owners = Owners()
    order: list[str] = []

    async def resolve(name: str) -> None:
        async with owners.turn("u1"):
            order.append(f"{name} in")
            await asyncio.sleep(0)
            order.append(f"{name} out")

    async def both() -> None:
        await asyncio.gather(resolve("first"), resolve("second"))

    asyncio.run(both())
    assert order == ["first in", "first out", "second in", "second out"]
    assert not owners._turns and not owners._waiting, "a finished turn was kept"


def test_a_link_left_alone_for_a_day_is_dropped_and_a_touched_one_is_kept():
    clock = Clock()
    owners = Owners(ttl=100.0, clock=clock)
    owners.claim("idle", ALICE)
    owners.claim("polled", ALICE)
    for _ in range(3):
        clock.now += 60.0
        assert owners.owner("polled") == ALICE
    assert owners.owner("idle") is None
    assert len(owners) == 1
