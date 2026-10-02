"""Two GPU runners at once, and the job goes to the free one that is faster.

THE OWNER'S DECISION, AS TESTS. offpeak's desktop and the Linux GPU runner
serve at the same time, each a lane of its own: the faster one wins while both
are free, the other takes the job while one is busy or gated, the CPU takes it
when neither is there, and a runner that goes away mid-job hands the job back
under the same rules one runner always followed -- per runner.

Half of these drive `Dispatcher` with stubs, because choosing is arithmetic;
the other half go through the real app built with TTS_RUNNER2_HOST set, with
test_remote's FakeClient attached to each runner lane, because the losses that
matter are only visible end to end. Nothing opens a socket to a runner.

Every test is named after the mistake it prevents.
"""

from __future__ import annotations

import socket
import time
from contextlib import contextmanager

import pytest
from starlette.testclient import TestClient

from app.dispatch import FINISHED, YIELDED, AnyProbe, Dispatcher
from app.remote import RunnerConfig, runner_lanes

from test_dispatch import _Log, _Probe, _settle
from test_remote import FakeClient, _wait

RATES = {"local": 0.23, "runner": 0.70, "runner2": 1.54}


def _three(*, rates=None, execute=None, jobs=None, ready=None, hop=8.0):
    """A Dispatcher over stubs with the local lane and two runner lanes."""
    rates = rates or RATES
    ready = ready or {}
    jobs = jobs if jobs is not None else {}
    d = Dispatcher(execute=execute or (lambda job, lane: FINISHED),
                   job_of=jobs.get,
                   work_of=lambda job: job.get("work", 1.0),
                   rate_of=lambda name, engine=None: rates[name],
                   finished=lambda job_id: None, log=_Log(), margin=1.25,
                   cooldown=30.0)
    d.add_lane("local")
    d.add_lane("runner", hop=hop, probe=_Probe(ready.get("runner", True)))
    d.add_lane("runner2", hop=hop, probe=_Probe(ready.get("runner2", True)))
    return d


# --------------------------------------------------------- the arithmetic ---


def test_with_both_runners_free_the_faster_one_takes_the_job():
    now = time.monotonic()
    assert _three()._pick({"work": 300.0}, "j", now).name == "runner2"
    swapped = _three(rates={"local": 0.23, "runner": 1.54, "runner2": 0.70})
    assert swapped._pick({"work": 300.0}, "j", now).name == "runner", \
        "the choice followed the lane's name rather than its rate"


def test_a_busy_runner_hands_the_job_to_the_other_one():
    now = time.monotonic()
    occupied = _three()
    occupied.lanes["runner2"].slot = "somebody-elses-job"
    assert occupied._pick({"work": 300.0}, "j", now).name == "runner"
    gated = _three(ready={"runner2": False})
    assert gated._pick({"work": 300.0}, "j", now).name == "runner", \
        "a runner saying busy was still chosen"
    cooling = _three()
    cooling.lanes["runner2"].cooldown_until = now + 30
    assert cooling._pick({"work": 300.0}, "j", now).name == "runner"


def test_with_both_runners_down_the_cpu_takes_it():
    d = _three(ready={"runner": False, "runner2": False})
    assert d._pick({"work": 300.0}, "j", time.monotonic()).name == "local"


def test_the_cpu_still_wins_when_neither_runner_is_clearly_better():
    """The margin applies to each runner on its own, not to the pair."""
    d = _three(rates={"local": 0.23, "runner": 0.25, "runner2": 0.26}, hop=0.0)
    assert d._pick({"work": 300.0}, "j", time.monotonic()).name == "local"


def test_a_runner_that_gives_up_is_not_offered_the_job_again_and_the_other_is():
    """The refused set is per lane, so the walk is runner2, runner, then done."""
    calls: list[str] = []

    def execute(job, lane):
        calls.append(lane)
        return YIELDED if lane == "runner2" else FINISHED

    jobs = {"j": {"work": 300.0}}
    d = _three(execute=execute, jobs=jobs)
    d.start()
    try:
        d.submit("j")
        _settle(lambda: calls == ["runner2", "runner"],
                why=f"the job did not move to the other runner: {calls}")
        assert d.lanes["runner2"].cooldown_until > time.monotonic()
        assert d.lanes["runner"].cooldown_until == 0.0
    finally:
        d.drain(1.0)


def test_any_probe_answers_for_the_runners_together_and_names_each():
    one = AnyProbe({"runner": _Probe(False)})
    assert one.ok_for("chatterbox") is False
    assert one.why_for("chatterbox") == "no_such_service", \
        "one runner must answer exactly as its own probe does"
    two = AnyProbe({"runner": _Probe(False), "runner2": _Probe(True)})
    assert two.ok_for("chatterbox") is True
    both_down = AnyProbe({"runner": _Probe(False), "runner2": _Probe(False)})
    assert both_down.why_for("chatterbox") == \
        "runner: no_such_service; runner2: no_such_service"
    assert AnyProbe({}).why_for("chatterbox") == "no runner lane is configured"


# -------------------------------------------------------- the configuration --


def test_each_numbered_runner_is_a_lane_and_the_first_always_is():
    env = {"TTS_RUNNER2_HOST": "192.0.2.12", "TTS_RUNNER5_HOST": "192.0.2.15",
           "TTS_RUNNER3_HOST": "  ", "TTS_RUNNER_CPU_HOST": "192.0.2.99"}
    assert runner_lanes(env) == [("runner", "TTS_RUNNER"),
                                 ("runner2", "TTS_RUNNER2"),
                                 ("runner5", "TTS_RUNNER5")]
    assert runner_lanes({}) == [("runner", "TTS_RUNNER")]


def test_a_numbered_runner_has_its_own_place_and_shares_the_patience():
    env = {"TTS_RUNNER_HOST": "192.0.2.11", "TTS_RUNNER_FINGERPRINT": "AA:BB",
           "TTS_RUNNER_MAX_WAIT": "120", "TTS_RUNNER_POLL": "1",
           "TTS_RUNNER2_HOST": "192.0.2.12", "TTS_RUNNER2_PORT": "47601",
           "TTS_RUNNER2_FINGERPRINT": "CC:DD", "TTS_RUNNER2_POLL": "3",
           "TTS_RUNNER2_LABEL": "  Linux   box  "}
    first = RunnerConfig.from_env(env)
    second = RunnerConfig.from_env(env, prefix="TTS_RUNNER2")
    assert (second.host, second.port, second.fingerprint) == ("192.0.2.12", 47601, "ccdd")
    assert second.origin == "https://192.0.2.12:47601"
    assert second.max_wait == 120.0, "an unset bound did not fall back to the first's"
    assert second.poll == 3.0, "its own setting lost to the first runner's"
    assert second.label == "Linux box"
    assert (first.fingerprint, first.label) == ("aabb", "")
    assert RunnerConfig.from_env(env, prefix="TTS_RUNNER7") is None


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _second_runner_env(**extra) -> dict[str, str]:
    """A second runner on a port nothing listens on, so its probe fails at once."""
    return {"TTS_RUNNER2_HOST": "127.0.0.1", "TTS_RUNNER2_PORT": str(_closed_port()),
            "TTS_RUNNER2_LABEL": "Linux GPU", "TTS_RUNNER_PROBE_S": "3600",
            "TTS_REALTIME_FACTOR_RUNNER2": "1.4",
            "TTS_REALTIME_FACTOR_RUNNER2_CHATTERBOX_TURBO": "2.9", **extra}


def test_a_second_host_turns_on_a_second_lane_with_its_own_key_and_rates(build):
    app = build(**_second_runner_env())
    import app.main as main

    assert list(main.dispatch.lanes) == ["local", "runner", "runner2"]
    assert main.BACKEND_ORDER == ("runner", "runner2", "local")
    assert main.rate_for("runner2").value == 1.4
    assert main.rate_for("runner2", "chatterbox").value == 1.4
    assert main.rate_for("runner").value == 0.70, "the second card's seed leaked"
    with TestClient(app):
        client = main.state["runner2"]
        assert main.state["runner"] is None
        assert client.key.name == "TTS_RUNNER2_API_KEY", \
            "the second runner was sent the first runner's secret"
        assert client.key.target == client.cfg.origin
        assert client.cfg.label == "Linux GPU"


def test_two_runners_at_one_origin_are_refused_at_start(build):
    with pytest.raises(SystemExit) as refused:
        build(TTS_RUNNER_HOST="192.0.2.20", TTS_RUNNER2_HOST="192.0.2.20")
    assert "TTS_RUNNER2_HOST" in str(refused.value.code)


def test_an_explicit_order_that_leaves_a_runner_out_is_said(build, caplog):
    app = build(**_second_runner_env(TTS_BACKEND_ORDER="runner,local"))
    import app.main as main

    assert "runner2" not in main.dispatch.lanes
    with TestClient(app):
        pass
    assert "TTS_BACKEND_ORDER does not name runner2" in caplog.text


# --------------------------------------------------------------- end to end --


@contextmanager
def runners(**fakes):
    """Attach a fake to each runner lane, and prime its probe by hand.

    As test_remote.runner() does for one, with one more step: the second lane's
    REAL client asked its machine once at start, and that answer has to have
    landed before the fake's is written, or it would overwrite it.
    """
    import app.main as main

    saved = {}
    for lane, fake in fakes.items():
        held = main.dispatch.lanes[lane]
        _settle(lambda held=held: held.probe.at > 0,
                why=f"the {lane} probe never answered at start")
        saved[lane] = (main.state.get(lane), held.hop)
        main.state[lane] = fake
        held.hop = 0.0
        held.probe.once()
    try:
        yield main
    finally:
        for lane, (client, hop) in saved.items():
            main.state[lane] = client
            main.dispatch.lanes[lane].hop = hop


def _speak(client) -> dict:
    created = client.post("/jobs", json={"text": "One short line.",
                                         "voice": "default"}).json()
    return _wait(client, created["id"])


@pytest.fixture
def two(build):
    with TestClient(build(**_second_runner_env())) as client:
        yield client


def test_both_up_the_faster_runner_speaks_it(two):
    slow, fast = FakeClient(segments=1), FakeClient(segments=1)
    with runners(runner=slow, runner2=fast):
        done = _speak(two)
    assert done["status"] == "done"
    assert done["backend"] == "runner2"
    assert len(fast.submitted) == 1 and not slow.submitted


def test_one_busy_the_other_runner_speaks_it(two):
    slow, fast = FakeClient(segments=1), FakeClient(segments=1)
    fast.state = (False, "machine_busy: the GPU has 900 MiB free and an engine "
                         "needs about 4608")
    with runners(runner=slow, runner2=fast):
        done = _speak(two)
    assert done["backend"] == "runner"
    assert not fast.submitted


def test_both_down_the_cpu_speaks_it(two):
    def gone():
        from app.remote import RemoteUnavailable

        raise RemoteUnavailable("GET /v1/services: [Errno 111] Connection refused")

    slow, fast = FakeClient(), FakeClient()
    slow.offer = fast.offer = gone
    with runners(runner=slow, runner2=fast):
        done = _speak(two)
    assert done["status"] == "done" and done["backend"] == "local"


def test_a_runner_that_goes_away_mid_job_hands_it_to_the_other(two):
    """Lost mid-job: back to the head, that runner refused and cooled, and the
    other runner -- not the CPU, because it is still clearly better -- speaks it."""
    slow, fast = FakeClient(segments=3), FakeClient(segments=3)
    polls = {"n": 0}
    healthy = fast.job

    def unplugged(job_id):
        polls["n"] += 1
        if polls["n"] > 1:
            from app.remote import RemoteUnavailable

            raise RemoteUnavailable("GET /v1/jobs: [Errno 113] No route to host")
        return healthy(job_id)

    fast.job = unplugged
    with runners(runner=slow, runner2=fast) as main:
        done = _speak(two)
        cooling = main.dispatch.lanes["runner2"].cooldown_until
    assert done["status"] == "done", done.get("error")
    assert done["backend"] == "runner"
    assert done["fell_back_from"] == "runner2"
    assert "runner2: " in done["fell_back_reason"]
    assert cooling > time.monotonic()
    # NO DELETE REACHES A MACHINE THAT IS GONE, and none is needed: the Linux
    # runner cancels a job nobody polls (app/runner/jobs.py, ORPHAN_SECONDS).
    # THE SAME LOCAL JOB ID ON BOTH, so whichever machine is asked again
    # recognises the job rather than speaking it twice.
    assert fast.submitted[0][1] == slow.submitted[0][1] == done["id"]


def test_a_runner_that_yields_hands_it_to_the_other(two):
    slow, fast = FakeClient(segments=2), FakeClient(segments=2, yield_after=0)
    fast.cfg = RunnerConfig(host="desk.invalid", service="chatterbox", poll=0.0,
                            max_wait=0.0)
    with runners(runner=slow, runner2=fast):
        done = _speak(two)
    assert done["status"] == "done"
    assert done["backend"] == "runner" and done["fell_back_from"] == "runner2"


def test_health_shows_every_runner_and_every_runners_answer(two):
    class Snapshotting(FakeClient):
        def __init__(self, label, state):
            super().__init__()
            self.cfg = RunnerConfig(host="192.0.2.30", label=label)
            self._state_word = state

        def snapshot(self, max_age=5.0):
            return {"reachable": True, "state": self._state_word,
                    "can_run": True, "mode": "always-on", "services": [],
                    "service": "chatterbox"}

    desk, linux = Snapshotting("", "ready"), Snapshotting("Linux GPU", "busy")
    linux.state = (False, "machine_busy")
    with runners(runner=desk, runner2=linux):
        doc = two.get("/health").json()
    assert [r["lane"] for r in doc["runners"]] == ["runner", "runner2"]
    assert [r["state"] for r in doc["runners"]] == ["ready", "busy"]
    assert [r["label"] for r in doc["runners"]] == [None, "Linux GPU"]
    assert doc["runner"]["state"] == "ready", "the one-runner field moved"
    row = doc["engines"]["chatterbox"]["runner"]
    assert row["ready"] is True, "one runner free is a runner free"
    assert row["lanes"] == {"runner": {"ready": True, "why": ""},
                            "runner2": {"ready": False, "why": "machine_busy"}}
    assert set(doc["dispatch"]["lanes"]) == {"local", "runner", "runner2"}
    assert set(doc["realtime_factor_by_backend"]) == {"local", "runner", "runner2"}
