"""The live layer: what the page asks for by itself, and when it stops asking.

The page keeps health, the hub's event stream, the jobs and the two on-demand
lists (profiles and voices) true without a reload, and keeps quiet while
nobody can see it. That logic is the source between the "live" and "/live"
markers, which defines functions and nothing else, so these run it unchanged
in Node against a fake clock, a fake document and recording stand-ins for
everything it calls in the rest of the page. Without `node` on PATH they skip,
with the reason; test_satellites_writes.py does the same.

What they prevent:

  * a background tab that goes on asking: health every 30 s all night, and a
    stream holding one of the six connections a browser allows per origin;
  * a return to the page that asks for everything twice, because `focus` and
    `visibilitychange` arrive together;
  * a hub stream that closed for good and is never opened again with the
    Satellites tab shut, or one retried for ever on a deployment with no hub;
  * a job queued from another device that this page does not hear of until a
    reload, and a voice group drawn for a runner that has since gone.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "app" / "static" / "ui.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH; these drive the "
                                "page's own JavaScript and need a JavaScript runtime")

# The section, the stand-ins and one scenario, in one eval. The clock is the
# harness's: setTimeout and clearTimeout queue on it, Date.now reads it, and
# advance(ms) runs whatever falls due, in order, letting each one's promises
# settle before the next. `calls` records every stand-in call with the time.
HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const from = html.indexOf("/* ============================================================== live === */");
const to = html.indexOf("/* ============================================================= /live === */");
if (from < 0 || to < 0 || to < from) throw new Error("the live section's markers moved");
const SECTION = html.slice(from, to);
const SCENARIO = fs.readFileSync(process.argv[3], "utf8");

let now = 1e9;
Date.now = () => now;
let timers = [], serial = 0;
const setTimeout = (fn, ms) => { const id = ++serial; timers.push({ id, at: now + (ms || 0), fn }); return id; };
const clearTimeout = id => { timers = timers.filter(t => t.id !== id); };
const settle = () => new Promise(r => setImmediate(r));
async function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    const due = timers[0];
    if (!due || due.at > end) break;
    timers.shift();
    now = due.at;
    await due.fn();
    await settle();
  }
  now = end;
  await settle();
}

const heard = { document: {}, window: {} };
const listen = where => (type, fn) => { (heard[where][type] = heard[where][type] || []).push(fn); };
const fire = (where, type, ev) => { for (const fn of heard[where][type] || []) fn(ev || {}); };
const document = { hidden: false, visibilityState: "visible", activeElement: null,
                   addEventListener: listen("document") };
const window = { addEventListener: listen("window"), EventSource: function () {} };
function hide() { document.hidden = true; document.visibilityState = "hidden"; fire("document", "visibilitychange"); }
function show() { document.hidden = false; document.visibilityState = "visible"; fire("document", "visibilitychange"); }

const calls = [];
const did = name => (...args) => { calls.push({ name, at: now, args }); };
const count = (name, since = 0) => calls.slice(since).filter(c => c.name === name).length;
const times = name => calls.filter(c => c.name === name).map(c => c.at);

// What the rest of the page answers. A scenario changes these.
const hub = { answers: true, open: false };
let engines = {}, tts = {}, open = "transcribe";
let HEALTH = null;
const LOADED = { glossaries: 0, voices: 0, jobs: 0 };
const SATELLITES = { events: null, evGap: false, hooks: {} };
const TABS = ["transcribe", "speak", "jobs", "vocab", "satellites"].map(tab =>
  ({ dataset: { tab }, getAttribute: () => String(tab === open) }));
// poll() ends in liveHealth(), as the page's does.
async function poll() { calls.push({ name: "poll", at: now }); liveHealth(); }
// One read for every caller while one is out, as the page's satellitesCount does.
let counting = null;
function satellitesCount() {
  if (!counting) {
    calls.push({ name: "count", at: now });
    counting = Promise.resolve(hub.answers).finally(() => { counting = null; });
  }
  return counting;
}
function satellitesListen() {
  calls.push({ name: "listen", at: now });
  SATELLITES.events = { readyState: 1, close() { this.readyState = 2; calls.push({ name: "close", at: now }); } };
}
const satellitesOpen = () => hub.open;
const satellitesRefresh = did("refresh"), tmTick = did("telemetry"), schedule = did("schedule");
const voicesRedraw = did("redraw"), jobsStale = did("stale");
function loadGlossaries() { calls.push({ name: "glossaries", at: now }); LOADED.glossaries = now; }
function loadVoices() { calls.push({ name: "voices", at: now }); LOADED.voices = now; }
const engineIds = () => Object.keys(engines);
const engineOffline = id => engines[id];
const ttsLongHealth = () => tts;
// The gateway's /health as a session holding health:read is answered, with
// or without a hub; and the session's scopes, every one unless a scenario
// takes satellites:read away.
const withHub = reachable => ({ status: "ok", backends: {
  stt: { reachable: true }, satellites: { reachable } } });
const withoutHub = { status: "ok", backends: { stt: { reachable: true } } };
const scopes = new Set(["satellites:read"]);
const holds = scope => scopes.has(scope);

eval(SECTION + "\n;(async () => {\n" + SCENARIO + "\n})().catch(e => { console.error(e); process.exit(2); });");
"""


def run(tmp_path: Path, scenario: str):
    """The scenario's last line prints one JSON value; that value."""
    (tmp_path / "harness.js").write_text(HARNESS)
    (tmp_path / "scenario.js").write_text(scenario)
    done = subprocess.run([NODE, str(tmp_path / "harness.js"), str(PAGE),
                           str(tmp_path / "scenario.js")],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_stream_is_opened_at_start_without_the_satellites_tab(tmp_path):
    """The dock's count of satellites waiting, and Activity, were whatever
    they were at load until somebody visited Satellites, because only that
    tab opened the stream. It opens at start now, after one list read that
    gives the count and says the hub is there."""
    got = run(tmp_path, """
      liveStart();
      await advance(0);
      console.log(JSON.stringify({ count: count("count"), listen: count("listen"),
                                   open: !!SATELLITES.events }));
    """)
    assert got == {"count": 1, "listen": 1, "open": True}


def test_a_session_that_may_not_read_the_satellites_never_asks_the_hub(tmp_path):
    """A speech account holds no satellites:read. The stream and its list
    read would each be a 403 the reader never asked for, at load, after a
    return to the page and on every rung of the retry ladder; health naming
    a reachable hub changes nothing."""
    got = run(tmp_path, """
      scopes.delete("satellites:read");
      HEALTH = withHub(true);
      liveStart();
      await advance(0);
      hide(); show();
      await advance(120000);
      console.log(JSON.stringify({ count: count("count"), listen: count("listen"),
                                   hub: liveHub() }));
    """)
    assert got == {"count": 0, "listen": 0, "hub": False}, got


def test_the_stream_is_left_to_the_poll_when_the_satellites_tab_is_open(tmp_path):
    """With the tab open its own poll reads the lists and opens the stream;
    a second read and a second stream from here would be the same work
    twice."""
    got = run(tmp_path, """
      hub.open = true;
      liveStart();
      await advance(0);
      console.log(JSON.stringify({ count: count("count"), listen: count("listen") }));
    """)
    assert got == {"count": 0, "listen": 0}


def test_a_hidden_page_asks_for_nothing_after_a_minute(tmp_path):
    """Health stops at once; the stream is kept for a minute, so a phone
    switching apps for a moment keeps Activity, and is then closed and
    marked as a gap. Nothing is asked for while the page stays hidden."""
    got = run(tmp_path, """
      HEALTH = withHub(true);
      liveStart();
      await advance(0);
      hide();
      const mark = calls.length;
      await advance(59000);
      const kept = !!SATELLITES.events;
      await advance(2000);
      const quiet = calls.slice(mark).filter(c => c.name !== "close").map(c => c.name);
      await advance(600000);
      console.log(JSON.stringify({ kept, quiet, closed: count("close"), events: SATELLITES.events,
                                   gap: SATELLITES.evGap, later: calls.length - mark - 1 }));
    """)
    assert got["kept"], "the stream went before the minute was up"
    assert got["quiet"] == [], f"a hidden page asked for {got['quiet']}"
    assert got["closed"] == 1 and got["events"] is None and got["gap"] is True, got
    assert got["later"] == 0, "something was asked for in the ten minutes after"


def test_returning_reopens_the_stream_and_catches_up_once_for_focus_and_visibility_together(tmp_path):
    """Coming back to a window fires visibilitychange and focus together.
    Each used to be its own catch-up; the second within 2 s is the first."""
    got = run(tmp_path, """
      HEALTH = withHub(true);
      liveStart();
      await advance(0);
      hide();
      await advance(61000);
      const mark = calls.length;
      show();
      fire("window", "focus");
      await advance(0);
      const back = name => count(name, mark);
      console.log(JSON.stringify({ poll: back("poll"), count: back("count"), listen: back("listen"),
                                   glossaries: back("glossaries"), open: !!SATELLITES.events }));
    """)
    assert got == {"poll": 1, "count": 1, "listen": 1, "glossaries": 1, "open": True}, got


def test_a_stream_closed_for_good_is_retried_on_a_ladder_that_stops_growing_at_thirty_seconds(tmp_path):
    """A reconnect that gets a 503 (the hub restarting) closes the stream for
    good, and with the tab shut no poll came along to open another. The
    Satellites section says so through its hook, and the live layer tries
    at 2, 5, 15 and 30 s, then every 30 s."""
    got = run(tmp_path, """
      liveStart();
      await advance(0);
      SATELLITES.events = null;
      hub.answers = false;
      const from = now;
      SATELLITES.hooks.live("closed");
      await advance(200000);
      const at = [from, ...times("count").filter(t => t > from)];
      console.log(JSON.stringify(at.slice(1).map((t, i) => t - at[i])));
    """)
    assert got[:6] == [2000, 5000, 15000, 30000, 30000, 30000], got


def test_a_deployment_without_a_hub_stops_asking_once_health_says_so(tmp_path):
    """Before health answers, a list that fails is a hub not up yet and is
    tried again. Once health says the gateway has no hub, it is not."""
    got = run(tmp_path, """
      hub.answers = false;
      liveStart();
      await advance(0);
      await advance(2000);
      HEALTH = withoutHub;
      await advance(600000);
      console.log(JSON.stringify({ count: count("count"), listen: count("listen") }));
    """)
    assert got == {"count": 3, "listen": 0}, got


def test_a_change_in_the_queue_seen_by_health_asks_for_the_jobs(tmp_path):
    """tts-long's queued and running counts move when anybody queues or
    finishes a job; a move this page did not see in its own list is somebody
    else's job, so the list is asked for whichever tab is open."""
    got = run(tmp_path, """
      tts = { queued: 0, running: 0 };
      await poll();
      const first = count("stale");
      tts = { queued: 1, running: 0 };
      await poll();
      await poll();
      const moved = count("stale");
      tts = { queued: 0, running: 1 };
      await poll();
      const same = count("stale");
      tts = {};
      await poll();
      console.log(JSON.stringify({ first, moved, same, unknown: count("stale") }));
    """)
    assert got == {"first": 0, "moved": 1, "same": 1, "unknown": 1}, got


def test_the_first_health_answer_sets_the_engine_signature_without_a_redraw(tmp_path):
    """Boot draws the voices itself; the first answer has nothing to differ
    from."""
    got = run(tmp_path, """
      engines = { chatterbox: "", turbo: "" };
      await poll();
      await poll();
      console.log(JSON.stringify(count("redraw")));
    """)
    assert got == 0


def test_a_change_in_engine_readiness_redraws_the_voices(tmp_path):
    """The voice groups are drawn disabled with a runner's reason, so a runner
    going or coming back redraws them. A first answer with no engines at all
    (tts-long down at load) counts as an answer: engines appearing after it
    are a change."""
    got = run(tmp_path, """
      await poll();
      engines = { chatterbox: "" };
      await poll();
      const appeared = count("redraw");
      engines = { chatterbox: "the GPU runner is not answering" };
      await poll();
      await poll();
      console.log(JSON.stringify({ appeared, offline: count("redraw") }));
    """)
    assert got == {"appeared": 1, "offline": 2}, got


def test_entering_vocabulary_refetches_profiles_older_than_five_seconds_and_returning_only_older_than_thirty(tmp_path):
    """A profile saved on the phone appears when the tab is opened here.
    Entering a tab is asking to see it, so five seconds is old; a window
    coming back to the front waits for thirty."""
    got = run(tmp_path, """
      LOADED.glossaries = now;
      await advance(4000);
      liveEnter("vocab", true);
      const young = count("glossaries");
      await advance(2000);
      liveEnter("vocab", true);
      const entered = count("glossaries");
      await advance(20000);
      liveEnter("vocab", false);
      const returned_young = count("glossaries");
      await advance(11000);
      liveEnter("vocab", false);
      console.log(JSON.stringify({ young, entered, returned_young, returned: count("glossaries") }));
    """)
    assert got == {"young": 0, "entered": 1, "returned_young": 1, "returned": 2}, got
