"""The processor rung died in the dispatcher and went on being configured.

`runner_cpu` was the third rung of a ladder. It was removed as a LANE when the
dispatcher was rebuilt on two -- measured, the runner's Ryzen 7 5700X3D ran
Chatterbox at 0.271x realtime against 0.230x on this container's eight Xeon
threads, five per cent, and the agent on that machine has only ever registered
`echo` and `chatterbox`, so every offer to `chatterbox-cpu` was
`no_such_service` for the whole life of the rung.

WHAT SURVIVED THE DELETION IS WHAT THIS FILE IS ABOUT. Three environment keys
went on being parsed for two releases after the thing they configured could not
run a job:

  * `TTS_RUNNER_CPU_SERVICE` reached `RunnerConfig.cpu_service`, which
    `for_cpu()` turned into a SECOND `RunnerClient` built in `lifespan` at every
    startup -- a TLS context and a pinned certificate for a service that does
    not exist;
  * `TTS_RUNNER_CPU_MAX_WAIT` reached `RunnerConfig.cpu_max_wait`, read by that
    client and by nothing else;
  * `TTS_REALTIME_FACTOR_RUNNER_CPU` seeded a rate under a lane key the
    dispatcher cannot build, and `/health` published the number.

And `RunnerClient.snapshot()` put `cpu_service: "chatterbox-cpu"` on `/health`
beside the service that is real, so a reader was handed the name of a service
the runner has never served, from this host's own configuration rather than
from the machine.

ADR 0007 recorded this deletion as done and it was not; compose.yaml and the
tts-long README both then said in as many words that it was still owed. The
tests that existed asserted the OPPOSITE -- `for_cpu()` copies the pin, an
empty `TTS_RUNNER_CPU_SERVICE` is different from an unset one -- so the suite
was green precisely because the dead knobs were still there.

THE SHAPE OF THESE TESTS IS THE POINT. Asserting "the attribute is gone" is a
test of a line of code. All but one of the tests below assert that SETTING THE
KEY CHANGES NOTHING A CALLER CAN OBSERVE, which is the property that was false
and which stays true however the rung is reintroduced.

AND ONE OF THEM IS A SOURCE SCAN, SAID OUT LOUD RATHER THAN DRESSED UP.
`test_no_processor_rung_key_is_spelled_anywhere_in_the_service` greps the
modules for the five key names, which is exactly the "a line of code is gone"
shape the paragraph above disclaims. It is kept because it catches the one
member of the family the observable tests cannot:
`TTS_REALTIME_FACTOR_RUNNER_CPU` never reached `RunnerConfig` at all, it was
read at IMPORT into a module global, and a module global that is read and
routed nowhere has no caller-visible effect to assert the absence of. A
docstring that called that test a property test would be the same kind of
rounding-up this whole file exists to punish.

NOTHING HERE OPENS A SOCKET. `RunnerConfig.from_env` is pure, and the /health
assertions run against the app conftest builds with no runner attached.
"""

from __future__ import annotations

from dataclasses import asdict

from app.remote import RunnerConfig

# EVERY KEY THAT NAMED THE RUNG, including the two that were genuinely deleted
# earlier. They are here together because "be exact about which of them died"
# is what compose.yaml got wrong once already, and a reader of this list should
# not have to hold two categories in their head: NONE of them does anything.
DEAD_KEYS = {
    "TTS_RUNNER_CPU_SERVICE": "chatterbox-cpu",
    "TTS_RUNNER_CPU_MAX_WAIT": "60",
    "TTS_RUNNER_CPU_MIN_PCT": "50",
    "TTS_RUNNER_CPU_WHEN_BACKLOG_S": "120",
    "TTS_REALTIME_FACTOR_RUNNER_CPU": "0.24",
}


def test_no_processor_rung_key_changes_the_runner_this_service_builds():
    """Set every one of them and the configuration is byte for byte the same.

    THE DEFECT THIS WOULD HAVE CAUGHT: `cpu_service` and `cpu_max_wait` were
    fields on this dataclass, so setting either produced a DIFFERENT config --
    and the difference was a whole second client, built at startup, pointed at
    a service id the runner does not answer for. Compared as a dict rather than
    field by field, so a rung reintroduced under a new field name fails here
    without anybody remembering to add an assertion.
    """
    plain = {"TTS_RUNNER_HOST": "box", "TTS_RUNNER_PORT": "47600"}
    assert asdict(RunnerConfig.from_env(plain)) \
        == asdict(RunnerConfig.from_env(plain | DEAD_KEYS)), (
        "a TTS_RUNNER_CPU_* key still reaches a field on RunnerConfig; the "
        "rung it names has not been a lane since the dispatcher was rebuilt "
        "on two, so whatever reads that field cannot route work to it")


def test_no_processor_rung_key_is_spelled_anywhere_in_the_service():
    """A key nothing reads is one thing; a key READ and routed nowhere is the
    trap this rung was for months.

    Searched over the source rather than over the config object, because
    `TTS_REALTIME_FACTOR_RUNNER_CPU` never touched RunnerConfig at all: it was
    read in app/main.py into `RTF_SEED_RUNNER_CPU` and seeded a rate under a
    lane key `_build_dispatch` cannot construct. One test that can only pass
    when all three are gone from every module.
    """
    from pathlib import Path

    import app.main as main

    app_dir = Path(main.__file__).parent
    offenders = []
    for source in sorted(app_dir.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        # Only where the key is a STRING the code looks up. The comments in
        # these files name the dead keys on purpose -- deleting a feature
        # without saying so is how it gets reinvented -- and a comment cannot
        # configure anything.
        for key in DEAD_KEYS:
            if f'"{key}"' in text or f"'{key}'" in text:
                offenders.append(f"{source.name}: {key}")
    assert not offenders, (
        "read by code and routed nowhere: " + "; ".join(offenders))


def test_the_snapshot_names_only_services_the_runner_itself_reported():
    """`/health` published `runner.cpu_service` out of this host's settings.

    The runner's own answer to "what do you sell" is `services`, read off its
    /v1/services and carried through `_services_of`. `cpu_service` sat beside
    it carrying `chatterbox-cpu` -- a name this end invented, for a service the
    agent on spring has never registered -- and nothing on the page or in the
    gateway ever read it, so the only consumer was a person, being told
    something untrue.

    DRIVEN THROUGH A REAL `snapshot()` AGAINST A FAKE TRANSPORT, because that
    is where the key was added and /health with no runner attached would have
    passed either way. The fake answers exactly what the runner really answers:
    one service, `chatterbox`.

    Asserted as "every service id in this document was reported by the runner"
    rather than as "the key `cpu_service` is absent", so a configured id
    reaching the snapshot under any name fails here.
    """
    import json

    from app.remote import RunnerClient

    docs = {
        "/v1/status": {"state": "available",
                       "services": [{"id": "chatterbox", "running": True}]},
        "/v1/services": {"gpu_available": True,
                         "services": [{"id": "chatterbox", "device": "gpu",
                                       "available": True}]},
    }
    client = RunnerClient(RunnerConfig(host="runner.invalid",
                                       service="chatterbox",
                                       fingerprint="ab" * 32))
    client._request = (                     # type: ignore[method-assign]
        lambda method, p, body=None, headers=None, timeout=None:
        (200, {}, json.dumps(docs.get(p, {})).encode()))

    snap = client.snapshot(max_age=0.0)
    reported = {row["id"] for row in (snap.get("services") or [])}
    assert reported == {"chatterbox"}, snap
    named = {v for v in snap.values() if isinstance(v, str)}
    invented = {v for v in named if v.startswith("chatterbox")} - reported
    assert not invented, (
        "the snapshot names " + repr(sorted(invented)) + ", which this host "
        "configured and the runner never reported")


def lanes_the_dispatcher_can_build() -> set[str]:
    """Every name `_build_dispatch` has an `add_lane` for, read out of it.

    NOT `dispatch.lanes`, AND THE DIFFERENCE IS A TEST THAT FAILS ON A SHIPPED
    DEPLOYMENT. `dispatch.lanes` is the lanes this process TURNED ON, which
    TTS_BACKEND_ORDER decides -- and that variable is a membership test, so
    `TTS_BACKEND_ORDER: "local"` is a documented, supported configuration that
    compose.yaml, app/main.py and test_remote.py all name in as many words.
    Under it `dispatch.lanes` is `{"local"}` while `_LANE_SEEDS` still holds
    `runner`, which is correct -- the seed is a constant of the code and the
    lane is a choice of the deployment -- and comparing the two called it
    "seeded but unreachable: ['runner']". Measured: this file's first version
    passed with no TTS_BACKEND_ORDER set and failed the moment one was.

    The question the rung actually poses is whether a seed names a lane the
    dispatcher has NO BRANCH FOR AT ALL, which is what `runner_cpu` was, and
    that set is a property of the source rather than of anybody's environment.
    Read with `ast` rather than written down here, because a list restated in a
    test is a third copy of the contract, free to drift from both.
    """
    import ast
    from pathlib import Path

    import app.main as main

    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_dispatch":
            names = {call.args[0].value
                     for call in ast.walk(node)
                     if isinstance(call, ast.Call)
                     and isinstance(call.func, ast.Attribute)
                     and call.func.attr == "add_lane"
                     and call.args
                     and isinstance(call.args[0], ast.Constant)}
            assert names, "no add_lane call in _build_dispatch; this reader is stale"
            return names
    raise AssertionError("app/main.py has no _build_dispatch to read")


def test_the_rate_table_holds_only_lanes_the_dispatcher_can_build(speech):
    """A seed for a lane no job can be sent to is a figure describing no
    machine this deployment will ever use.

    `_LANE_SEEDS` carried `runner_cpu: 0.24` and `/health` printed it in
    `realtime_factor_by_backend`, next to two numbers that are real, with
    nothing saying which was which. `backend_observations` reported 0 beside
    it for ever, because nothing could ever observe it.
    """
    import app.main as main

    buildable = lanes_the_dispatcher_can_build()
    assert set(main.dispatch.lanes) <= buildable, (
        "this process turned on a lane the dispatcher cannot build: "
        + repr(sorted(set(main.dispatch.lanes) - buildable)))
    assert set(main._LANE_SEEDS) <= buildable, (
        "seeded but unreachable: " + repr(sorted(set(main._LANE_SEEDS) - buildable)))
    assert set(main._LANE_ENV) <= buildable, (
        "an environment key names a lane that cannot be built: "
        + repr(sorted(set(main._LANE_ENV) - buildable)))

    doc = speech.get("/health").json()
    published = set(doc["realtime_factor_by_backend"]) | set(doc["backend_observations"])
    assert published <= buildable, (
        "/health publishes a rate for " + repr(sorted(published - buildable)))


def test_the_rate_table_is_still_honest_on_a_local_only_deployment(build):
    """THE DEFECT THIS PREVENTS IS IN THE TEST ABOVE, NOT IN THE SERVICE.

    `TTS_BACKEND_ORDER: "local"` switches the runner lane off. It is the
    supported way to run this service on one machine, it is written out in
    compose.yaml, and it is the setting somebody reaches for the evening spring
    is being played on -- which is to say the evening they are most likely to
    run the suite. The first version of the test above bounded `_LANE_SEEDS` by
    `dispatch.lanes`, so under that order it reported `runner` as "seeded but
    unreachable" and failed. A guard that cries on a shipped configuration is a
    guard somebody deletes, and what they delete is the pin on `runner_cpu`.

    So the same four assertions, driven on the order that broke them. This is
    also the harder direction for the thing the file is really about: with the
    runner switched off, `runner_cpu` in `_LANE_SEEDS` is the ONLY name that
    can be over the line, because `runner` no longer is.
    """
    from starlette.testclient import TestClient

    built = build(TTS_BACKEND_ORDER="local")
    import app.main as main

    buildable = lanes_the_dispatcher_can_build()
    assert set(main.dispatch.lanes) == {"local"}, (
        "TTS_BACKEND_ORDER=local did not switch the runner lane off, so this "
        "test is not exercising the configuration it names")
    assert set(main._LANE_SEEDS) <= buildable, (
        "seeded but unreachable: " + repr(sorted(set(main._LANE_SEEDS) - buildable)))
    assert set(main._LANE_ENV) <= buildable, (
        "an environment key names a lane that cannot be built: "
        + repr(sorted(set(main._LANE_ENV) - buildable)))

    with TestClient(built) as client:
        doc = client.get("/health").json()
    published = set(doc["realtime_factor_by_backend"]) | set(doc["backend_observations"])
    assert published <= buildable, (
        "/health publishes a rate for " + repr(sorted(published - buildable)))
