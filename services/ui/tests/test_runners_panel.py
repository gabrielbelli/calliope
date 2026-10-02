"""Every GPU runner on the page, and the job's lane read off the right one.

tts-long can drive several runners at once (offpeak's desktop and a Linux GPU
box); /health lists each under `runners`, and every engine's runner row says
which of them are ready. These run the page's own functions in Node -- the
card's name, the lane an estimate is read for, and how a job row names where
it ran -- and the e2e suite draws the cards in a browser. Without `node` on
PATH they skip, with the reason.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "app" / "static" / "ui.html"
HTML = PAGE.read_text(encoding="utf-8")
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH; these drive "
                                "the page's own JavaScript and need a JavaScript runtime")


def function(name: str) -> str:
    found = re.search(r"\n(?:async )?function " + re.escape(name) + r"\(", HTML)
    assert found, f"{name}() is gone from the page"
    return HTML[found.start():HTML.index("\n}\n", found.start()) + 2]


def run(tmp_path: Path, script: str):
    source = "\n".join(function(n) for n in ("readyRunner", "runnerName"))
    (tmp_path / "t.js").write_text(
        "let HEALTH_TTS = {};\nconst ttsLongHealth = () => HEALTH_TTS;\n"
        + source + "\n" + script)
    done = subprocess.run([NODE, str(tmp_path / "t.js")], capture_output=True,
                          text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_node
def test_a_card_is_named_by_its_operator_or_numbered_and_never_by_address(tmp_path):
    got = run(tmp_path, """
console.log(JSON.stringify([
  runnerName({lane: "runner"}, 0, 1),
  runnerName({lane: "runner"}, 0, 2),
  runnerName({lane: "runner2"}, 1, 2),
  runnerName({lane: "runner2", label: "Linux GPU"}, 1, 2),
]));""")
    assert got == ["GPU runner", "GPU runner 1", "GPU runner 2", "Linux GPU"]


@needs_node
def test_the_estimate_reads_the_ready_runner_that_the_dispatcher_would_choose(tmp_path):
    """The faster of two free runners, by its rate for THIS engine."""
    got = run(tmp_path, """
HEALTH_TTS = {realtime_factor_by_engine: {"runner/chatterbox": 0.7, "runner2/chatterbox": 0.4,
                                          "runner/chatterbox-turbo": 1.2, "runner2/chatterbox-turbo": 2.1}};
const both = {ready: true, lanes: {runner: {ready: true}, runner2: {ready: true}}};
const one = {ready: true, lanes: {runner: {ready: false}, runner2: {ready: true}}};
console.log(JSON.stringify([
  readyRunner(both, "chatterbox"), readyRunner(both, "chatterbox-turbo"),
  readyRunner(one, "chatterbox"), readyRunner({ready: true}, "chatterbox"),
]));""")
    assert got == ["runner", "runner2", "runner2", "runner"]


def test_the_panel_draws_every_runner_as_a_copy_of_the_first():
    """The bead dock's page keeps its look: a further runner is the same card."""
    paint = function("paintRunner")
    assert "health.runners" in paint, "only the first runner is drawn"
    assert "runnerCards(box, all.slice(1))" in paint
    cards = function("runnerCards")
    assert "box.cloneNode(true)" in cards, "a second card is styled on its own"
    # The headline rule reaches every copy's suffixed id.
    assert '[id^="runnerstate"]{flex:1 1 auto;min-width:0}' in HTML


def test_a_job_on_any_runner_says_it_ran_on_a_gpu():
    """`backend` is the lane, and the second runner's lane is runner2."""
    ran = function("ranOn")
    assert '/^runner\\d*$/.test(job.backend || "")' in ran
    assert 'job.backend === "runner"' not in ran
