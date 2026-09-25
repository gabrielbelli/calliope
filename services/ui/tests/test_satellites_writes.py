"""The Satellites tab against a hub that answers in the order a network does.

test_satellites.py reads the page as text, which is enough for what it asks:
which controls sit in which disclosure, what a poll may write. What it cannot
see is ordering. Save and Try again each PUT the whole wake word list, built
from the page's copy of the hub's list; the tab also polls that list every
3 s. Neither the hub nor the page carries a version, so the last write wins,
and a write built from an old copy puts the old copy back.

So these run the page's own Satellites section, the source between
`const SATELLITES = {` and `async function loadGlossaries(`, unchanged, in
Node, with every element a permissive stand-in and `json()` a fake hub whose
answers can be held back. Nothing starts a server and nothing reaches the
network. Without `node` on PATH they skip, with the reason.

What they prevent:

  * two writes whose PUTs overlap: both built from the same copy, so the
    second takes back what the first saved;
  * a poll that left before a save and answers after it: the page's copy goes
    back to the list before the save, and the next edit's Save PUTs that;
  * one missed poll (the hub restarting): Routing is hidden, and it was only
    ever shown again by the first load, which has already happened.

The same harness drives test_satellites_ordering.py and
test_satellites_states.py, which import `run` from here.
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
# json() is the fake hub. The section and the scenario are one eval, so the
# scenario can call the section's functions by name.
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
const document = { activeElement: null, createElement: () => stand(), getElementById: $ };
const window = {};
class Option {}
const paintRange = () => {}, note = () => {}, confirm = () => true;
const busy = () => () => {};
const reason = (p, fallback) => fallback;
const saved = new Map();
const store = { get: (k, d) => saved.has(k) ? saved.get(k) : d, set: (k, v) => saved.set(k, v) };
const MENISCUS = { calm: { matches: true } };

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
  words: [{ name: "alexa", threshold: 0.5, satellites: [], state: "ready", error: null },
          { name: "hey_jarvis", threshold: 0.5, satellites: ["*"], state: "ready", error: null }],
  puts: [],
  down: false,
  hold: false,
  slow: 0,
  refuse: 0,
};
const answer = () => JSON.parse(JSON.stringify({ available: ["alexa", "hey_jarvis"], words: hub.words,
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
    if (hub.refuse) {
      hub.refuse--;
      await later();
      const e = new Error("422 the threshold is out of range"); e.status = 422; throw e;
    }
    const body = JSON.parse(options.body);
    hub.puts.push(body.words);
    // One slow write: the next one must not be built, or sent, until this
    // one has answered. Only the first is slow, so an unqueued second write
    // would land first and the first would put the old list back.
    if (hub.slow) { const ms = hub.slow; hub.slow = 0; await new Promise(r => setTimeout(r, ms)); }
    hub.words = body.words.map(w => ({ ...w, state: "ready", error: null }));
    const now = answer();
    await later(); return now;
  }
  throw new Error("the fake hub has no " + method + " " + path);
}
const api = json;

const word = name => hub.words.find(w => w.name === name);
const on = (name, id) => word(name).satellites.includes(id);
// One satellite chosen for one word, the way a tick in its row does it.
function tick(name, id) {
  wakeEdit(name, w => {
    w.satellites = w.satellites.filter(s => s !== id && s !== "*");
    w.satellites.push(id);
  });
}

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


def test_two_wake_word_writes_at_once_both_reach_the_hub(tmp_path):
    """Try again on a failed word, and Save pressed while it is still out.
    Each PUT is the whole list. Built from the same copy, one would take back
    what the other sent; so they are taken in turn, and each is built when
    its turn comes, from the answer to the one before."""
    got = run(tmp_path, """
      await satellitesRefresh();
      hub.slow = 30;
      tick("alexa", "aaaaaaaaaaaa");                    // the kitchen, staged
      await Promise.all([wakeRetry(stand()), wakeSave()]);
      console.log(JSON.stringify({ puts: hub.puts.length, kitchen: on("alexa", "aaaaaaaaaaaa"),
        last_has_it: hub.puts[1][0].satellites.includes("aaaaaaaaaaaa"), clean: WAKE.draft === null }));
    """)
    assert got["puts"] == 2, got
    assert got["kitchen"] and got["last_has_it"], got
    assert got["clean"], got


def test_a_poll_answer_that_arrives_after_a_save_cannot_undo_it(tmp_path):
    """A poll leaves, the kitchen is given alexa and saved, and then the poll's
    answer lands, holding the list from before the save. Taken as the hub's
    copy, it is what the next edit's draft is built from, and that Save takes
    the kitchen off the word again."""
    got = run(tmp_path, """
      await satellitesRefresh();
      hub.hold = true;
      const poll = satellitesRefresh();                 // leaves; answered now
      hub.hold = false;
      tick("alexa", "aaaaaaaaaaaa");
      await wakeSave();
      release(); await poll;                            // lands after the save
      tick("alexa", "bbbbbbbbbbbb");
      await wakeSave();
      console.log(JSON.stringify({ kitchen: on("alexa", "aaaaaaaaaaaa"),
                                   bedroom: on("alexa", "bbbbbbbbbbbb") }));
    """)
    assert got["kitchen"] is True, got
    assert got["bedroom"] is True, got


def test_a_refused_save_keeps_the_edit_for_the_reader_to_fix(tmp_path):
    """A 422 names the field. The edit it refused stays on screen, and the
    next Save sends it again rather than the hub's old copy."""
    got = run(tmp_path, """
      await satellitesRefresh();
      tick("alexa", "aaaaaaaaaaaa");
      hub.refuse = 1;
      await wakeSave();
      const kept = !!WAKE.draft
        && WAKE.draft.find(w => w.name === "alexa").satellites.includes("aaaaaaaaaaaa");
      await wakeSave();
      console.log(JSON.stringify({ kept, kitchen: on("alexa", "aaaaaaaaaaaa") }));
    """)
    assert got["kept"], got
    assert got["kitchen"], got


def test_a_word_marked_for_removal_is_left_out_of_the_save_and_keep_undoes_it(tmp_path):
    """Remove is staged and sends nothing: the PUT is what Save sends. Keep
    takes it back, and a word marked again is the one word left out."""
    got = run(tmp_path, """
      await satellitesRefresh();
      const button = stand();
      wakeRemove("alexa", button);
      const staged = WAKE.removed.has("alexa") && hub.puts.length === 0;
      wakeRemove("alexa", button);                      // Keep
      const kept = !WAKE.removed.has("alexa") && WAKE.draft === null;
      wakeRemove("alexa", button);
      await wakeSave();
      console.log(JSON.stringify({ staged, kept, sent: hub.puts[0].map(w => w.name) }));
    """)
    assert got["staged"], got
    assert got["kept"], got
    assert got["sent"] == ["hey_jarvis"], got


def test_one_missed_poll_does_not_hide_routing_until_the_page_is_reloaded(tmp_path):
    """The hub restarts, one poll gets 503 and the tab hides everything the hub
    answers. The next poll shows it again, but Routing is shown only by a
    routing load, and the first one has already happened."""
    got = run(tmp_path, """
      await satellitesRefresh();
      await new Promise(r => setTimeout(r, 50));        // the first routing load, not awaited
      const before = $("sat-routing").hidden;
      hub.down = true;  await satellitesRefresh();
      hub.down = false; await satellitesRefresh();
      await new Promise(r => setTimeout(r, 50));        // a routing load, if one started
      console.log(JSON.stringify({ shown_at_first: before === false,
                                   shown_after_the_hub_is_back: $("sat-routing").hidden === false }));
    """)
    assert got["shown_at_first"], got
    assert got["shown_after_the_hub_is_back"], got
