"""The Satellites tab against a hub that answers in the order a network does.

test_satellites.py reads the page as text, which is enough for what it asks:
which controls sit in the fold, what a poll may write. What it cannot see is
ordering. A tick in a satellite's fold PUTs the whole wake word list, built
from the page's copy of the hub's list; the tab also polls that list every
3 s. Neither the hub nor the page carries a version, so the last write wins,
and a write built from an old copy puts the old copy back.

So these run the page's own Satellites section, the source between
`const SATELLITES = {` and `async function loadGlossaries(`, unchanged, in
Node, with every element a permissive stand-in and `json()` a fake hub whose
answers can be held back. Nothing starts a server and nothing reaches the
network. Without `node` on PATH they skip, with the reason.

What they prevent:

  * two ticks whose saves overlap: both PUTs are built from the same copy, and
    the second takes the first one's satellite back off the word;
  * a poll that left before a tick and answers after it: the page's copy goes
    back to the list before the tick, and the next tick PUTs that;
  * one missed poll (the hub restarting): the Routing card is hidden, and it
    was only ever shown again by the first load, which has already happened.
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

# The harness. SECTION is the page's own source; SCENARIO is one test's steps.
# Everything the section uses from the rest of the page is defined here:
# $ and document hand out stand-ins that take any property and any call, and
# json() is the fake hub. The section and the scenario are one eval, because
# function declarations in a strict eval are local to it.
HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const from = html.indexOf("const SATELLITES = {");
const to = html.indexOf("async function loadGlossaries(");
if (from < 0 || to < 0) throw new Error("the Satellites section's bounds moved");
const SECTION = html.slice(from, to);
const SCENARIO = fs.readFileSync(process.argv[3], "utf8");

function stand() {
  const own = {};
  return new Proxy(function () {}, {
    get(_, k) {
      if (k in own) return own[k];
      if (k === "then") return undefined;             // never a thenable
      if (k === Symbol.toPrimitive) return () => "";
      if (k === Symbol.iterator) return function* () {};
      return (own[k] = stand());
    },
    set(_, k, v) { own[k] = v; return true; },
    apply() { return stand(); },
  });
}
const elements = new Map();
const $ = id => { if (!elements.has(id)) elements.set(id, stand()); return elements.get(id); };
// The tab is not open, so satellitesRefresh schedules no next poll.
$("tab-satellites").hidden = true;
const document = { activeElement: null, createElement: () => stand() };
const window = {};
class Option {}
const paintRange = () => {}, note = () => {}, confirm = () => true;
const busy = () => () => {};
const reason = (p, fallback) => fallback;

// THE FAKE HUB. A request reaches it when json() is called and is answered at
// once, and the answer reaches the page one macrotask later, as over a
// network. A GET of the wake words can be held: answered now, delivered on
// release(), which is a poll that left before a save and lands after it.
const later = () => new Promise(r => setTimeout(r, 5));
const hub = {
  satellites: [{ id: "aaaaaaaaaaaa", name: "kitchen", adopted: true, online: true,
                 config: {}, status: {}, wake_words: [] },
               { id: "bbbbbbbbbbbb", name: "bedroom", adopted: true, online: true,
                 config: {}, status: {}, wake_words: [] }],
  words: [{ name: "alexa", threshold: 0.5, satellites: [], state: "ready", error: null }],
  down: false,
  hold: false,
};
const answer = () => JSON.parse(JSON.stringify({ available: ["alexa"], words: hub.words,
                                                 load_error: null }));
let held = [];
function release() { for (const r of held) r(); held = []; }
async function json(path, options) {
  const method = (options && options.method) || "GET";
  if (hub.down) { const e = new Error("503 Service Unavailable"); e.status = 503; throw e; }
  if (method === "GET" && path === "/satellites") {
    await later(); return { satellites: JSON.parse(JSON.stringify(hub.satellites)) };
  }
  if (method === "GET" && path === "/satellites/firmware") { await later(); return { firmware: [] }; }
  if (method === "GET" && path === "/satellites/routing") {
    await later();
    return { rules: [], env: {}, services: { stt: "http://stt.test" }, warnings: [], load_error: null };
  }
  if (method === "GET" && path === "/satellites/wake-words") {
    const now = answer();
    if (hub.hold) return new Promise(r => held.push(() => r(now)));
    await later(); return now;
  }
  if (method === "PUT" && path === "/satellites/wake-words") {
    hub.words = JSON.parse(options.body).words.map(w => ({ ...w, state: "ready", error: null }));
    const now = answer();
    await later(); return now;
  }
  throw new Error("the fake hub has no " + method + " " + path);
}
const api = json;

function card(id) { const c = stand(); c.dataset = { id }; return c; }
function tick(word) { return { dataset: { word }, checked: true, disabled: false }; }
const on = (word, id) => hub.words.find(w => w.name === word).satellites.includes(id);

eval(SECTION + "\n;(async () => {\n" + SCENARIO + "\n})().catch(e => { console.error(e); process.exit(2); });");
"""


def run(tmp_path: Path, scenario: str) -> dict:
    """The scenario's last line prints one JSON object; that object."""
    (tmp_path / "harness.js").write_text(HARNESS)
    (tmp_path / "scenario.js").write_text(scenario)
    done = subprocess.run([NODE, str(tmp_path / "harness.js"), str(PAGE),
                           str(tmp_path / "scenario.js")],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_two_wake_word_ticks_saved_at_once_both_reach_the_hub(tmp_path):
    """Ticking alexa for the kitchen and then for the bedroom before the first
    save has answered. Each PUT is the whole list built from the same copy, so
    without serialising them the bedroom's save takes the kitchen back off the
    word, and the kitchen's tick stays ticked over a hub that does not have
    it. A page that refuses the second tick while the first is saving is
    fine too, as long as that tick is put back: what must never happen is a
    tick left on that the hub does not hold."""
    got = run(tmp_path, """
      await satellitesRefresh();
      const kitchen = tick("alexa"), bedroom = tick("alexa");
      await Promise.all([satelliteWordToggle(card("aaaaaaaaaaaa"), kitchen),
                         satelliteWordToggle(card("bbbbbbbbbbbb"), bedroom)]);
      console.log(JSON.stringify({
        kitchen_ticked: kitchen.checked, kitchen_on_hub: on("alexa", "aaaaaaaaaaaa"),
        bedroom_ticked: bedroom.checked, bedroom_on_hub: on("alexa", "bbbbbbbbbbbb")}));
    """)
    assert got["kitchen_ticked"] == got["kitchen_on_hub"], got
    assert got["bedroom_ticked"] == got["bedroom_on_hub"], got


def test_a_poll_answer_that_arrives_after_a_tick_cannot_undo_it(tmp_path):
    """A poll leaves, the kitchen is ticked and saved, and then the poll's
    answer lands, holding the list from before the tick. Taken as the hub's
    copy, it is what the bedroom's tick is built from next, and that PUT takes
    the kitchen off the word again."""
    got = run(tmp_path, """
      await satellitesRefresh();
      hub.hold = true;
      const poll = satellitesRefresh();                 // leaves; answered now
      hub.hold = false;
      const kitchen = tick("alexa");
      await satelliteWordToggle(card("aaaaaaaaaaaa"), kitchen);
      release(); await poll;                            // lands after the save
      const bedroom = tick("alexa");
      await satelliteWordToggle(card("bbbbbbbbbbbb"), bedroom);
      console.log(JSON.stringify({
        kitchen_ticked: kitchen.checked, kitchen_on_hub: on("alexa", "aaaaaaaaaaaa"),
        bedroom_ticked: bedroom.checked, bedroom_on_hub: on("alexa", "bbbbbbbbbbbb")}));
    """)
    assert got["kitchen_ticked"] == got["kitchen_on_hub"], got
    assert got["bedroom_ticked"] == got["bedroom_on_hub"], got


def test_one_missed_poll_does_not_hide_the_routing_card_until_the_page_is_reloaded(tmp_path):
    """The hub restarts, one poll gets 503 and the tab hides its cards. The
    next poll shows the list, the wake words, firmware and events again, but
    the Routing card is shown only by the first routing load, which already
    happened, so it stays hidden until someone reloads the page."""
    got = run(tmp_path, """
      await satellitesRefresh();
      await new Promise(r => setTimeout(r, 50));        // the first routing load, not awaited
      const before = $("routecard").hidden;
      hub.down = true;  await satellitesRefresh();
      hub.down = false; await satellitesRefresh();
      await new Promise(r => setTimeout(r, 50));        // a routing load, if one started
      console.log(JSON.stringify({ shown_at_first: before === false,
                                   shown_after_the_hub_is_back: $("routecard").hidden === false }));
    """)
    assert got["shown_at_first"], got
    assert got["shown_after_the_hub_is_back"], got
