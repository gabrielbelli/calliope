"""The audit trail: two tiers, aggregation, and what writes a row (§2.1, M6, recheck M-3)."""

from __future__ import annotations

import json

from conftest import (PASSWORD, SAME_ORIGIN, Clock, MockBackend, bearer, gateway, make_key,
                      make_user, sign_in)

from app import audit
from app.db import iso


def rows(db, **where):
    clause = " AND ".join(f"{k} = ?" for k in where) or "1"
    return [dict(r) for r in db.all(f"SELECT * FROM audit WHERE {clause} ORDER BY id",
                                    tuple(where.values()))]


async def test_refusals_are_counted_per_minute_not_written_one_by_one(monkeypatch):
    """401s per IP, 403s per credential: an internet client chooses how many
    it sends, so it must not choose how many rows it writes."""
    clock = Clock(monkeypatch)
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        rt = main.runtime.get()
        for _ in range(7):
            await client.get("/v1/models")
        key = make_key(make_user("ana"), scopes={"models:read"})
        for path in ("/voices", "/jobs", "/voices"):
            await client.get(path, headers=bearer(key))
        clock.advance(61)
        rt.trail.flush(before=int(clock.at // 60))
        refused = rows(rt.db, action="request_refused")

    assert len(refused) == 2
    by_status = {json.loads(r["detail"])["status"]: r for r in refused}
    assert json.loads(by_status[401]["detail"])["count"] == 7
    assert by_status[401]["aggregated"] == 1 and by_status[401]["ip"] == "192.0.2.1"
    forbidden = json.loads(by_status[403]["detail"])
    assert forbidden["count"] == 3 and forbidden["paths"] == ["/voices", "/jobs"]
    assert by_status[403]["auth_method"].startswith("key:k_")


async def test_an_unknown_username_reaches_only_the_noise_tier(monkeypatch):
    """Failures against invented names would otherwise fill the never-dropped
    tier as fast as a botnet can invent them (recheck M-3)."""
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        make_user("ana")
        await sign_in(client, "nobody\r\nforged", "wrong but long enough")
        await sign_in(client, "ana", "wrong but long enough")
        rt = main.runtime.get()
        rt.trail.flush()
        security = rows(rt.db, action="login_failed", aggregated=0)
        noise = rows(rt.db, action="login_failed", aggregated=1)

    assert len(security) == 1 and security[0]["target"].startswith("u_")
    assert {r["target"] for r in noise} == {"<unknown>", "ana"}


async def test_noise_never_evicts_a_security_event_younger_than_a_year(monkeypatch):
    clock = Clock(monkeypatch)
    async with gateway(monkeypatch, authenticate=False) as (_, main):
        rt = main.runtime.get()
        recent = iso(clock.at - 300 * 24 * 3600)
        ancient = iso(clock.at - 400 * 24 * 3600)
        with rt.db.transaction():
            rt.db.executemany(
                "INSERT INTO audit (ts, actor_kind, action, outcome, aggregated) "
                "VALUES (?, 'anonymous', ?, 'denied', ?)",
                [(recent, "kept", 0), (ancient, "expired", 0)]
                + [(iso(clock.at - i), "noise", 1) for i in range(200_000)])
        rt.trail.sweep()
        kept = rows(rt.db, action="kept")
        expired = rows(rt.db, action="expired")
        noise = rt.db.one("SELECT COUNT(*) AS n FROM audit WHERE aggregated = 1")["n"]

    assert len(kept) == 1
    assert not expired
    assert noise == audit.NOISE_CAP


async def test_the_security_tier_stops_at_its_ceiling_and_says_so(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        rt = main.runtime.get()
        rt.trail.security_ceiling = rt.trail._security_rows + 2
        for n in range(4):
            rt.trail.record(action=f"event{n}", outcome="ok")
        overflow = rows(rt.db, action="audit_overflow")
        stored = [r["action"] for r in rows(rt.db) if r["action"].startswith("event")]
        monitor = make_key(make_user("ana"), scopes={"audit:read"})
        page = (await client.get("/admin/audit", headers=bearer(monitor))).json()

    assert stored == ["event0", "event1"]
    assert len(overflow) == 1
    assert page["overflow"] is True


async def test_opening_a_microphone_and_adopting_a_satellite_write_security_rows(
        monkeypatch):
    hub = MockBackend("voice-satellites")
    async with gateway(monkeypatch, satellites=hub) as (client, main):
        await client.post("/satellites/020000000001/listen?seconds=12")
        await client.post("/satellites/020000000001/inject", content=b"RIFF")
        await client.post("/satellites/020000000002/adopt", json={})
        await client.get("/satellites/telemetry/clips/wake-1.wav")
        events = {r["action"]: r for r in rows(main.runtime.get().db, aggregated=0)}

    listen = events["listen"]
    assert listen["target"] == "020000000001"
    assert json.loads(listen["detail"]) == {"status": 200, "seconds": "12"}
    assert listen["actor_kind"] == "api_key"
    assert {"inject", "adopt", "telemetry_clip_downloaded"} <= set(events)


async def test_step_up_and_reading_someone_else_s_jobs_are_on_the_record(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        make_user("ana")
        await sign_in(client)
        client.headers.update(SAME_ORIGIN)
        await client.post("/auth/step-up", json={"password": "not it, at all"})
        await client.post("/auth/step-up", json={"password": PASSWORD})
        await client.get("/jobs?owner=all")
        await client.get("/jobs?owner=all")
        await client.get("/jobs?owner=me")
        rt = main.runtime.get()
        rt.trail.flush()
        step_ups = [r["outcome"] for r in rows(rt.db, action="step_up")]
        event = rows(rt.db, action="read_all", aggregated=0)
        counted = rows(rt.db, action="read_all", aggregated=1)

    assert step_ups == ["denied", "ok"]
    # The first in the minute is an event at once; both are in the count. An
    # allowed read is never recorded as a refusal.
    assert [(r["target"], r["outcome"]) for r in event] == [("all", "ok")]
    assert [(r["outcome"], json.loads(r["detail"])["count"]) for r in counted] == [("ok", 2)]


async def test_no_password_or_key_ever_reaches_the_audit(monkeypatch):
    async with gateway(monkeypatch, authenticate=False) as (client, main):
        make_user("ana")
        await sign_in(client, "ana", "a wrong but memorable phrase")
        await sign_in(client)
        client.headers.update(SAME_ORIGIN)
        await client.post("/auth/step-up", json={"password": PASSWORD})
        created = (await client.post("/auth/keys", json={
            "name": "x", "preset": "read-only"})).json()
        rt = main.runtime.get()
        rt.trail.flush()
        everything = json.dumps(rows(rt.db))

    for secret in (PASSWORD, "a wrong but memorable phrase", created["plaintext"]):
        assert secret not in everything
