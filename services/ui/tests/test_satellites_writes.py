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
  * one missed poll (the hub restarting): what the hub answered stays on
    screen with the reason beside it, and the reason goes with the next poll.

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
// The Speak tab's language helpers, which a wake word's Voice reuses.
const piece = (start, end) => {
  const at = html.indexOf(start), stop = html.indexOf(end, at);
  if (at < 0 || stop < 0) throw new Error("the Speak tab's " + start + " moved");
  return html.slice(at, stop);
};
const SPEAK = [piece("const PREFIX = {", "const CLONE_DEFAULTS"),
               piece("const KOKORO_LANGS = [", "const CHATTERBOX_LANGS"),
               piece("const KOKORO_SPELLING = {", "/* Script first"),
               piece("function voicesForLanguage(", "\n}\n") + "\n}\n"].join("\n");
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
// note() is recorded, so a scenario can read what the reader was told.
const notes = [];
const paintRange = () => {};
// confirm() is recorded and answered with `confirming`, so a scenario can
// see what was asked and say no.
const asked = [];
let confirming = true;
const confirm = question => { asked.push(String(question)); return confirming; };
const note = (host, kind, text) => { if (text) notes.push([kind, String(text)]); };
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
  // Left undefined, the answer is a hub's from before modes: no ptt, no
  // custom models, no env. A scenario sets them for the hub after them.
  available: ["alexa", "hey_jarvis"],
  ptt: undefined, custom: undefined, env: undefined, warnings: undefined,
  puts: [],
  bodies: [],
  calls: [],
  down: false,
  hold: false,
  slow: 0,
  refuse: 0,
  // Home Assistant's Assist pipelines, as the hub lists them for the picker;
  // pipesFail, when set, is the hub's 502 sentence instead.
  pipes: { preferred: "01cloud", pipelines: [
    { id: "01cloud", name: "Home Assistant Cloud", language: "en", stt_engine: "stt.home_assistant_cloud",
      stt_language: "en-GB", tts_engine: "tts.piper", tts_language: "en", tts_voice: null },
    { id: "01alexa", name: "Alexa", language: "pt", stt_engine: "stt.calliope_parakeet",
      stt_language: "pt", tts_engine: "tts.calliope_kokoro", tts_language: "pt-BR", tts_voice: "pf_dora" }] },
  pipeCalls: [],
  pipesFail: "",
  // A language model server's ids, as the hub lists them for the picker, and
  // the hub's answer to a Test; each *Fail, when set, is its 502 sentence.
  models: ["gpt-test-mini", "vendor/test-large"],
  modelCalls: [],
  modelsFail: "",
  testCalls: [],
  testFail: "",
  // Keys: PUT /satellites/secrets. `secrets` is the hub's name -> "hub" or
  // "environment" map, undefined for a hub from before keys could be
  // stored; `environment` is the names the hub's environment sets.
  secretCalls: [],
  secrets: undefined,
  environment: [],
  // Which tools the hub can run (GET /satellites/wake-words' `tools`),
  // undefined for a hub from before it said.
  tools: undefined,
};
const answer = () => JSON.parse(JSON.stringify({ available: hub.available, words: hub.words,
                                                 ptt: hub.ptt, custom: hub.custom, env: hub.env,
                                                 secrets: hub.secrets, tools: hub.tools,
                                                 warnings: hub.warnings, load_error: null }));
let held = [];
function release() { for (const r of held) r(); held = []; }
async function json(path, options) {
  const method = (options && options.method) || "GET";
  if (hub.down) { const e = new Error("503 Service Unavailable"); e.status = 503; throw e; }
  if (method === "GET" && path === "/satellites") {
    await later(); return { satellites: JSON.parse(JSON.stringify(hub.satellites)) };
  }
  if (method === "GET" && path === "/satellites/firmware") { await later(); return { firmware: [] }; }
  if (path === "/satellites/telemetry") {
    hub.telemetry = hub.telemetry || { enabled: false, level: "full", retention_days: 14, max_mb: 200,
                                       files: [], bytes: 0 };
    hub.tm = hub.tm || [];
    hub.tm.push([method, options && options.body ? JSON.parse(options.body) : null]);
    if (method === "PUT") Object.assign(hub.telemetry, JSON.parse(options.body));
    if (method === "DELETE") Object.assign(hub.telemetry, { files: [], bytes: 0 });
    await later(); return JSON.parse(JSON.stringify(hub.telemetry));
  }
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
    hub.bodies.push(body);
    if (body.ptt) hub.ptt = body.ptt;
    // One slow write: the next one must not be built, or sent, until this
    // one has answered. Only the first is slow, so an unqueued second write
    // would land first and the first would put the old list back.
    if (hub.slow) { const ms = hub.slow; hub.slow = 0; await new Promise(r => setTimeout(r, ms)); }
    hub.words = body.words.map(w => ({ ...w, state: "ready", error: null }));
    const now = answer();
    await later(); return now;
  }
  // A custom model: the .onnx is the body, the name the query.
  if (method === "POST" && path.startsWith("/satellites/wake-words/models?name=")) {
    const name = decodeURIComponent(path.split("=")[1]);
    hub.calls.push([method, path, options.headers && options.headers["Content-Type"]]);
    hub.custom = [...new Set([...(hub.custom || []), name])];
    hub.available = [...new Set([...hub.available, name])].sort();
    await later(); return answer();
  }
  if (method === "DELETE" && path.startsWith("/satellites/wake-words/models/")) {
    const name = decodeURIComponent(path.split("/").pop());
    hub.calls.push([method, path]);
    hub.custom = (hub.custom || []).filter(n => n !== name);
    hub.available = hub.available.filter(n => n !== name);
    await later(); return null;                        // 204
  }
  if (method === "POST" && path === "/satellites/routing/test") {
    const body = JSON.parse(options.body);
    hub.calls.push([method, path, body]);
    await later();
    return { rule_id: body.wake_word, mode: "command", transcript: body.text, reply_text: "It is four.",
             error: null, timings_ms: { total: 812 }, timeline_ms: {} };
  }
  if (method === "POST" && path === "/satellites/ha/pipelines") {
    hub.pipeCalls.push(JSON.parse(options.body));
    await later();
    if (hub.pipesFail) { const e = new Error(hub.pipesFail); e.status = 502; throw e; }
    return JSON.parse(JSON.stringify(hub.pipes));
  }
  if (method === "POST" && path === "/satellites/llm/models") {
    hub.modelCalls.push(JSON.parse(options.body));
    await later();
    if (hub.modelsFail) { const e = new Error(hub.modelsFail); e.status = 502; throw e; }
    return { models: [...hub.models] };
  }
  if (method === "POST" && path === "/satellites/llm/test") {
    hub.testCalls.push(JSON.parse(options.body));
    await later();
    if (hub.testFail) { const e = new Error(hub.testFail); e.status = 502; throw e; }
    return { model: JSON.parse(options.body).model, reply: "Hello there.", first_token_ms: 420,
             total_ms: 1260, token_limit: "max_tokens" };
  }
  if (method === "PUT" && path === "/satellites/secrets") {
    const body = JSON.parse(options.body);
    hub.secretCalls.push(body);
    await later();
    if (body.value !== null && hub.environment.includes(body.name)) {
      const e = new Error("409 " + body.name + " is set in the hub's environment"); e.status = 409; throw e;
    }
    hub.secrets = { ...(hub.secrets || {}) };
    hub.env = { ...(hub.env || {}) };
    if (body.value === null) { delete hub.secrets[body.name]; if (body.name in hub.env) hub.env[body.name] = false; }
    else { hub.secrets[body.name] = "hub"; if (body.name in hub.env) hub.env[body.name] = true; }
    return answer();
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

eval(SPEAK + "\n" + SECTION + "\n;(async () => {\n" + SCENARIO + "\n})().catch(e => { console.error(e); process.exit(2); });");
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


def test_an_edit_made_while_save_is_out_is_kept_for_the_next_save(tmp_path):
    """The rows stay live while a Save's PUT is out. An edit made then was
    dropped with the draft when the answer came, and the page said "No
    changes to save." It stays, and the next Save sends it."""
    got = run(tmp_path, """
      await satellitesRefresh();
      tick("alexa", "aaaaaaaaaaaa");                    // the kitchen, staged
      hub.slow = 40;
      const saving = wakeSave();
      await new Promise(r => setTimeout(r, 10));
      tick("alexa", "bbbbbbbbbbbb");                    // the bedroom, meanwhile
      await saving;
      const kept = !!WAKE.draft && WAKE.draft.find(w => w.name === "alexa").satellites.includes("bbbbbbbbbbbb");
      const first = hub.puts.length;
      await wakeSave();
      console.log(JSON.stringify({ kept, first, puts: hub.puts.length,
        bedroom: on("alexa", "bbbbbbbbbbbb"), kitchen: on("alexa", "aaaaaaaaaaaa"),
        clean: WAKE.draft === null }));
    """)
    assert got["kept"], got
    assert (got["first"], got["puts"]) == (1, 2), got
    assert got["bedroom"] and got["kitchen"] and got["clean"], got


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


def test_one_missed_poll_keeps_the_tab_and_says_so_until_the_hub_is_back(tmp_path):
    """The hub restarts and one poll gets 503. That used to hide the list,
    every open row and the hub's disclosures for three seconds, dropping the
    focus and the scroll, under a sentence saying there was no hub at all.
    Once the hub has answered, the last good copy stays with the reason
    beside it, and the reason goes with the next poll that gets through. A
    hub that has never answered is still one sentence and no empty list."""
    got = run(tmp_path, """
      hub.down = true;  await satellitesRefresh();
      const never = { man: $("satellitesman").hidden, none: $("satellitesnone").textContent };
      hub.down = false; await satellitesRefresh();
      const before = $("sat-wakewords").hidden;
      hub.down = true;  await satellitesRefresh();
      const away = { man: $("satellitesman").hidden, words: $("sat-wakewords").hidden,
                     said: $("sathubfail").dataset.said };
      await satellitesRefresh();                        // still down: said once
      hub.down = false; await satellitesRefresh();
      console.log(JSON.stringify({ never, shown_at_first: before === false, away, notes,
                                   back: $("satellitesman").hidden === false
                                     && $("sat-wakewords").hidden === false,
                                   cleared: $("sathubfail").dataset.said }));
    """)
    assert got["never"]["man"] is True, got
    assert got["never"]["none"].startswith("This deployment has no satellite hub"), got
    assert got["shown_at_first"], got
    assert got["away"]["man"] is False and got["away"]["words"] is False, "a missed poll hid the tab"
    lost = "The hub stopped answering, so this may be out of date: 503 Service Unavailable"
    assert got["away"]["said"] == "bad:" + lost, got
    assert got["notes"].count(["bad", lost]) == 1, "a standing failure was announced at every poll"
    assert got["back"], got
    assert got["cleared"] == "", got


def test_a_firmware_image_is_uploaded_only_with_its_version_and_the_form_is_emptied(tmp_path):
    """A blank Version became the file's name, "firmware" for every PlatformIO
    build, and a Version left in its box went up with the next image. The
    label matched nothing a satellite reports, so one that had just installed
    the image was offered it again for ever."""
    got = run(tmp_path, """
      const uploads = [];
      const real = json;
      json = async (path, o) => path.startsWith("/satellites/firmware?") ? (uploads.push(path), null)
                                                                          : real(path, o);
      $("fwfile").files = [{ name: "firmware.bin" }];
      $("fwversion").value = "  ";
      $("fwmodel").value = "esp32-korvo-v1.1";
      $("fwsig").value = "";
      await firmwareUpload();
      const blank = { uploads: uploads.length, notes: notes.slice() };
      $("fwversion").value = "v0.3.1-4-g1a2b3c4";
      $("fwmodel").value = " ";
      await firmwareUpload();
      const nomodel = { uploads: uploads.length, note: notes[notes.length - 1] };
      $("fwmodel").value = "esp32-korvo-v1.1";
      await firmwareUpload();
      console.log(JSON.stringify({ blank, nomodel, uploads, left: [$("fwfile").value, $("fwversion").value,
                                                                   $("fwsig").value] }));
    """)
    assert got["blank"]["uploads"] == 0, "an image went up with a version made up from its file name"
    # The build, where the version is stamped. Device shows the one each
    # satellite runs NOW, and an image labelled with it was never offered.
    assert got["blank"]["notes"] == [["warn", "Type the version its build stamped: git describe "
                                              "--always --dirty --tags, run where it was built."]], got
    # Nor with no model, which no satellite reports, so it is never offered.
    assert got["nomodel"] == {"uploads": 0, "note": ["warn", "Type the model the satellites report, "
                                                             "as each one's Device shows it."]}, got
    assert got["uploads"] == ["/satellites/firmware?model=esp32-korvo-v1.1&version=v0.3.1-4-g1a2b3c4"], got
    assert got["left"] == ["", "", ""], "the last image's version waits in the box for the next one"


def test_update_every_satellite_updates_only_the_ones_it_would_change(tmp_path):
    """"all" on the hub reflashed every satellite of the model, those already
    on the image included. Only the adopted, online ones of its model that
    run something else are sent it, one request each, and a skip is named."""
    got = run(tmp_path, """
      await satellitesRefresh();
      SATELLITES.list = [
        { id: "a1", name: "kitchen", adopted: true, online: true, model: "m1", firmware: "v2" },
        { id: "b2", name: "bedroom", adopted: true, online: true, model: "m1", firmware: "v1" },
        { id: "c3", name: "hall", adopted: true, online: true, model: "m1", firmware: "v1" },
        { id: "d4", name: "attic", adopted: true, online: false, model: "m1", firmware: "v1" },
        { id: "e5", name: "garage", adopted: true, online: true, model: "m2", firmware: "v1" },
        { id: "f6", name: "", adopted: false, online: true, model: "m1", firmware: "v1" }];
      const posts = [];
      const real = json;
      json = async (path, o) => {
        if (path !== "/satellites/ota") return real(path, o);
        const body = JSON.parse(o.body);
        posts.push(body.satellite);
        return body.satellite === "c3" ? { started: [], skipped: { c3: "already updating" } }
                                       : { started: [body.satellite], skipped: {} };
      };
      const fw = { sha256: "f".repeat(64), version: "v2", model: "m1" };
      const due = firmwareDue(fw).map(n => n.id);
      await firmwareAct(fw, "all", stand());
      const sent = posts.slice();
      await firmwareAct({ ...fw, model: "m3" }, "all", stand());
      console.log(JSON.stringify({ due, sent, after_none: posts.length, notes }));
    """)
    assert got["due"] == ["b2", "c3"], got
    assert got["sent"] == ["b2", "c3"], "a satellite already on the image, or offline, was sent it"
    assert got["after_none"] == 2, "an image no satellite needs was sent anyway"
    assert got["notes"] == [["warn", "Updating 1 satellite. Skipped hall: already updating."]], got


def test_update_every_satellite_asks_about_the_ones_it_will_send_and_says_when_they_are_busy(tmp_path):
    """The question said "every satellite" when only the due ones are sent
    the image, so with one of three due the question said all three would
    reboot. And a satellite taking an update is not due, so on the poll after
    a press the greyed button said every one "already runs it" beside rows
    that said Updating 40%."""
    got = run(tmp_path, """
      await satellitesRefresh();
      const sat = (id, name, over) => ({ id, name, adopted: true, online: true, model: "m1",
                                          firmware: "v1", ...over });
      const fw = { sha256: "f".repeat(64), version: "v2", model: "m1" };
      const old = { sha256: "e".repeat(64), version: "v0", model: "m1" };
      confirming = false;
      SATELLITES.list = [sat("a1", "kitchen", { firmware: "v2" }), sat("b2", "bedroom")];
      await firmwareAct(fw, "all", stand());
      SATELLITES.list = [sat("a1", "kitchen", { firmware: "v0" }), sat("b2", "bedroom")];
      await firmwareAct(old, "back", stand());
      SATELLITES.list = [sat("a1", "kitchen"), sat("b2", "bedroom"), sat("c3", "hall", { online: false })];
      await firmwareAct(fw, "all", stand());
      await firmwareAct(old, "back", stand());
      const why = () => firmwareDue(fw).length ? "" : firmwareIdle(fw);
      // Pressed: both online ones are mid-transfer; the hall is offline.
      SATELLITES.list = [sat("a1", "kitchen", { ota: { state: "progress", pct: 40, version: "v2" } }),
                         sat("b2", "bedroom", { ota: { state: "started", version: "v2" } }),
                         sat("c3", "hall", { online: false })];
      const busy = why();
      // One rebooting into it (offline, remembered), the other already on it.
      SATELLITES.restarting.set("a1", { v: "v2", at: Date.now() });
      SATELLITES.list = [sat("a1", "kitchen", { online: false }), sat("b2", "bedroom", { firmware: "v2" })];
      const rebooting = why();
      SATELLITES.restarting.clear();
      SATELLITES.list = [sat("a1", "kitchen", { firmware: "v2" }), sat("b2", "bedroom", { firmware: "v2" })];
      const current = why();
      SATELLITES.list = [sat("a1", "kitchen", { online: false })];
      const offline = why();
      console.log(JSON.stringify({ asked, busy, rebooting, current, offline }));
    """)
    assert got["asked"] == [
        "Update bedroom to v2? It reboots when the transfer ends.",
        "Roll back bedroom to v0, an older image? It reboots when the transfer ends.",
        "Update 2 satellites to v2? Each one reboots when its transfer ends.",
        "Roll back 2 satellites to v0, an older image? Each one reboots when its transfer ends.",
    ], got
    assert got["busy"] == "Updating 2 satellites now.", "an update under way read as already current"
    assert got["rebooting"] == "Updating 1 satellite now.", got
    assert got["current"] == "Every satellite of this model that is online already runs it.", got
    assert got["offline"] == "No satellite of this model is online.", got


def test_a_word_the_hub_left_with_no_satellite_does_not_hold_every_save(tmp_path):
    """Forget takes a satellite off every word, and a word whose only one it
    was is kept with none, which the hub takes back. Counted as a word left
    with nobody, it turned Save off for any edit, a threshold on another word
    included, with a reason that named no word. Only a word the reader
    emptied holds Save, and it is named."""
    got = run(tmp_path, """
      await satellitesRefresh();
      wakeEdit("hey_jarvis", w => { w.threshold = 0.65; });  // alexa has none, as the hub left it
      const other = { off: $("wwsave").disabled, said: $("wwdirty").textContent };
      wakeEdit("hey_jarvis", w => { w.satellites = []; });
      console.log(JSON.stringify({ other, emptied: { off: $("wwsave").disabled, said: $("wwdirty").textContent } }));
    """)
    assert got["other"] == {"off": False, "said": "Unsaved changes."}, got
    assert got["emptied"] == {"off": True, "said": "Pick at least one satellite for hey jarvis, "
                                                   "or choose Every satellite."}, got


def test_rename_with_an_empty_name_says_so_and_sends_nothing(tmp_path):
    """Clearing the Name and pressing Rename did nothing and said nothing: a
    button that did not work."""
    got = run(tmp_path, """
      const patches = [];
      const real = json;
      json = async (path, o) => ((o && o.method === "PATCH") ? (patches.push(path), {}) : real(path, o));
      const field = { value: "  ", focus() { this.focused = true; } };
      const li = { _n: hub.satellites[0], dataset: { id: "aaaaaaaaaaaa" },
                   querySelector: sel => sel === ".sat-rename input" ? field : stand() };
      await satelliteAct(li, "rename", stand());
      console.log(JSON.stringify({ patches, focused: !!field.focused }));
    """)
    assert got == {"patches": [], "focused": True}, got


def test_refreshes_asked_while_one_is_out_are_one_more_after_it(tmp_path):
    """Every online, offline, update or settings event asked for the three
    lists, so during Update every satellite dozens of refreshes overlapped
    on a hub busy sending an image. One asked while another is out waits
    for it, and everybody who asked meanwhile shares the one after."""
    got = run(tmp_path, """
      await satellitesRefresh();
      let gets = 0;
      const real = json;
      json = async (path, o) => { if (path === "/satellites") gets++; return real(path, o); };
      await Promise.all([satellitesRefresh(), satellitesRefresh(), satellitesRefresh(), satellitesRefresh()]);
      console.log(JSON.stringify({ gets, idle: SATELLITES.polling === null && SATELLITES.next === null }));
    """)
    assert got == {"gets": 2, "idle": True}, got



def test_telemetry_is_read_once_with_the_satellites_and_its_switch_turns_it_on(tmp_path):
    """The summary says whether the hub is recording without being opened:
    read once with the first list of satellites, not on every poll. The
    switch sends only what changed."""
    got = run(tmp_path, """
      await satellitesRefresh();
      await satellitesRefresh();
      await new Promise(r => setTimeout(r, 30));
      const before = $("tm-sum").textContent;
      await tmSend("PUT", { enabled: true });
      hub.telemetry.files = [{ date: "2026-09-29", bytes: 2097152 }];
      hub.telemetry.bytes = 2097152;
      await tmLoad();
      console.log(JSON.stringify({ before, after: $("tm-sum").textContent, calls: hub.tm,
                                   size: $("tmsize").textContent, download: $("tmdownload").hidden }));
    """)
    assert got["before"] == "off" and got["after"] == "recording everything"
    assert got["calls"] == [["GET", None], ["PUT", {"enabled": True}], ["GET", None]]
    assert got["size"] == "2.0 MB kept, over 1 day." and got["download"] is False


def test_the_output_lists_its_own_devices_then_the_other_satellites_that_can_speak(tmp_path):
    """A Linux satellite's own outputs (the system default first, one
    unplugged since kept and said), then every other adopted satellite with a
    speaker; the Korvo's own is its speaker. A satellite it plays through
    that is no longer adopted stays chosen and says so."""
    got = run(tmp_path, """
      const pi = { id: "b827eb121359", adopted: true, online: true, name: "pi-edifier",
                   caps: { speaker: {}, audio_devices: true }, status: { audio: {
                     sinks: [{ name: "alsa_output.builtin", description: "Built-in Audio" }], sources: [] } } };
      const korvo = { id: "020000000001", adopted: true, online: true, name: "korvo",
                      caps: { speaker: {}, mic: {} }, status: {} };
      const mute = { id: "020000000002", adopted: true, online: false, name: "hall",
                     caps: { mic: {} }, status: {} };
      const pending = { id: "020000000003", adopted: false, online: true, caps: { speaker: {} } };
      const list = [pi, korvo, mute, pending];
      console.log(JSON.stringify({
        pi: satOutputOptions(pi, { audio_sink: "alsa_output.usb-dac" }, list),
        korvo: satOutputOptions(korvo, { output_satellite: "b827eb121359" }, list),
        gone: satOutputOptions(korvo, { output_satellite: "0000000000ff" }, list).chosen,
        goneLabel: satOutputOptions(korvo, { output_satellite: "0000000000ff" }, list).options.slice(-1)[0] }));
    """)
    assert got["pi"]["options"] == [
        ["dev:", "The system's default", "On this satellite"],
        ["dev:alsa_output.builtin", "Built-in Audio", "On this satellite"],
        ["dev:alsa_output.usb-dac", "alsa_output.usb-dac (not connected)", "On this satellite"],
        ["sat:020000000001", "korvo", "On another satellite"]]
    assert got["pi"]["chosen"] == "dev:alsa_output.usb-dac"
    assert got["korvo"]["options"] == [
        ["dev:", "Its own speaker", "On this satellite"],
        ["sat:b827eb121359", "pi-edifier", "On another satellite"]]
    assert got["korvo"]["chosen"] == "sat:b827eb121359"
    assert got["gone"] == "sat:0000000000ff"
    assert got["goneLabel"] == ["sat:0000000000ff", "0000000000ff (no longer adopted)", "On another satellite"]


def test_choosing_an_output_sends_either_the_satellite_or_its_own_device(tmp_path):
    got = run(tmp_path, """
      const sent = [];
      satellitePatch = async (li, change) => { sent.push(change); return true; };
      satNoteFor = () => ({});
      const li = { _n: { caps: { audio_devices: true } } };
      satOutputPicked(li, { value: "sat:020000000001", dataset: {} });
      satOutputPicked(li, { value: "dev:alsa_output.builtin", dataset: {} });
      satOutputPicked({ _n: { caps: { mic: {} } } }, { value: "dev:", dataset: {} });
      console.log(JSON.stringify(sent));
    """)
    assert got == [{"output_satellite": "020000000001"},
                   {"output_satellite": "", "audio_sink": "alsa_output.builtin"},
                   {"output_satellite": ""}]


def test_airplay_says_what_plays_from_whom_and_how(tmp_path):
    """Its section's summary is one word; the facts are the track, the
    phone, the stream as PipeWire has it (rate, bits, channels, the PCM bit
    rate) and the phone's own volume; nothing is listed while no phone is
    connected."""
    got = run(tmp_path, """
      const ap = { running: true, session: true, playing: true, client: "Gabriel's iPhone",
                   title: "Clair de Lune", artist: "Debussy", album: "Suite bergamasque", volume: 50,
                   client_info: { client_model: "iPhone15,2" }, progress: { position_s: 83, duration_s: 245 },
                   track: { genre: "Classical", year: 1905, kind: "AAC audio file", bitrate_kbps: 256 },
                   stream: { format: "s32le 2ch 44100Hz", rate: 44100, bits: 32, channels: 2,
                             bitrate_kbps: 2822, latency_ms: 200 } };
      console.log(JSON.stringify({
        facts: satAirPlayFacts(ap, { format: "s16le 2ch 44100Hz", state: "running" }),
        perfect: satAirPlayPath({ format: "s16le 2ch 44100Hz" }, { format: "s16le 2ch 44100Hz" }, { playing: true }),
        converted: satAirPlayPath({ format: "s16le 2ch 44100Hz" }, { format: "s32le 2ch 48000Hz" }, { playing: true }),
        formats: [satFormat("float32le 2ch 96000Hz"), satFormat("s24le 6ch 48000Hz"), satFormat("odd")],
        idleOut: satPlayedAt({ format: "s16le 2ch 48000Hz", state: "idle" }),
        notes: [satAirPlayNote({}, ap), satAirPlayNote({}, { ...ap, playing: false }),
                satAirPlayNote({}, { running: true, session: false }), satAirPlayNote({ airplay_enabled: false }, ap),
                satAirPlayNote({}, { running: false, error: "no PipeWire" })],
        idle: satAirPlayFacts({ running: true, session: false }),
        line: satPlayingWhat(ap) }));
    """)
    assert got["facts"] == [["Status", "Playing"], ["From", "Gabriel's iPhone (iPhone15,2)"],
                            ["Now playing", "Clair de Lune · Debussy"], ["Album", "Suite bergamasque (1905)"],
                            ["Genre", "Classical"], ["Position", "1:23 / 4:05"],
                            ["Original file", "AAC audio file, 256 kb/s"],
                            ["Source", "ALAC, lossless · 44.1 kHz · 16-bit · stereo"],
                            ["Bit rate", "1,411 kb/s"], ["Handed on as", "44.1 kHz · 32-bit · stereo"],
                            ["Played at", "44.1 kHz · 16-bit · stereo"],
                            ["Path", "Converted from 44.1 kHz · 32-bit · stereo to 44.1 kHz · 16-bit · stereo"],
                            ["Delay here", "200 ms"], ["Phone's volume", "50%"]]
    assert got["perfect"] == "Bit-perfect: the phone's samples reach the card unchanged"
    assert got["converted"] == "Converted from 44.1 kHz · 16-bit · stereo to 48 kHz · 32-bit · stereo"
    assert got["formats"] == ["96 kHz · 32-bit float · stereo", "48 kHz · 24-bit · 6 channels", "odd"]
    assert got["idleOut"] == "48 kHz · 16-bit · stereo (idle)"
    assert got["notes"] == ["playing", "paused", "waiting", "off", "not running"]
    assert got["idle"] == []
    assert got["line"] == "Clair de Lune by Debussy, from Gabriel's iPhone"


def test_a_pi_row_shows_only_the_hardware_it_has_and_its_volume_in_percent(tmp_path):
    """A Pi with no ring, buttons or microphone: no light brightness, no
    ring set-up, no Buttons, no mic gain or Listen; volume 0-100 %, and a
    Chime in place of Blink. The Korvo keeps its twelve ring steps."""
    got = run(tmp_path, """
      const pi = { caps: { speaker: {}, audio_devices: true, airplay: { version: 1 } } };
      const korvo = { caps: { mic: {}, speaker: {}, lights: 12, buttons: ["rec", "play"] } };
      const old = { caps: {} };
      const has = n => ["mic", "lights", "buttons", "airplay", "audio_devices"].filter(w => satHas(n, w));
      const el = hw => ({ dataset: hw ? { hw } : {}, hidden: false });
      const vol = { dataset: { cfg: "volume", steps: "12" }, max: "12" };
      const blink = { dataset: { act: "identify", needs: "online lights unmuted idle" }, textContent: "Blink" };
      const parts = [el("mic"), el("lights"), el("buttons"), el("airplay")];
      const li = { querySelectorAll: () => parts,
                   querySelector: sel => sel.includes("volume") ? vol : blink };
      satHardware(li, pi);
      const out = { pi: has(pi), korvo: has(korvo), old: has(old),
                    hidden: parts.map(p => p.hidden), max: vol.max, steps: vol.dataset.steps || null,
                    blink: blink.textContent, needs: blink.dataset.needs };
      satHardware(li, korvo);
      out.korvoHidden = parts.map(p => p.hidden);
      out.korvoMax = vol.max;
      out.korvoBlink = blink.textContent;
      console.log(JSON.stringify(out));
    """)
    assert got["pi"] == ["airplay", "audio_devices"]
    assert got["korvo"] == ["mic", "lights", "buttons"] and got["old"] == ["mic", "lights", "buttons"]
    assert got["hidden"] == [True, True, True, False]
    assert (got["max"], got["steps"]) == ("100", None)
    assert (got["blink"], got["needs"]) == ("Chime", "online speaker idle")
    assert got["korvoHidden"] == [False, False, False, True]
    assert (got["korvoMax"], got["korvoBlink"]) == ("12", "Blink")


def test_a_speaker_with_no_microphone_is_online_or_playing_never_not_listening(tmp_path):
    got = run(tmp_path, """
      const pi = { id: "b827eb121359", adopted: true, online: true, caps: { speaker: {}, audio_devices: true },
                   config: {}, listening: { state: "off", error: "it has no microphone" },
                   status: { airplay: { playing: false } } };
      const idle = satState(pi, satMem());
      const playing = satState({ ...pi, status: { airplay: { playing: true, title: "Clair de Lune",
                                                             client: "Gabriel's iPhone" } } }, satMem());
      console.log(JSON.stringify({ idle: [idle.word, idle.line], playing: [playing.word, playing.line] }));
    """)
    assert got["idle"] == ["Online", "A speaker, with no microphone to listen with"]
    assert got["playing"] == ["Playing", "Clair de Lune, from Gabriel's iPhone"]


def test_each_output_says_its_quality_and_the_pis_own_jack_is_starred_with_its_limits(tmp_path):
    got = run(tmp_path, """
      const realtek = { name: "alsa_output.usb-Realtek", description: "Realtek USB2.0 Audio Analog Stereo",
                        quality: { kind: "usb", dac: true, formats: ["S16_LE", "S24_3LE", "S32_LE"],
                                   bits: [16, 24, 32], rates: [44100, 48000, 96000, 192000, 384000] } };
      const jack = { name: "alsa_output.platform-mailbox", description: "Built-in Audio Stereo",
                     quality: { kind: "pwm", dac: false, bits: [16], rates: [48000] } };
      const hdmi = { name: "alsa_output.hdmi", description: "Built-in Audio Digital Stereo (HDMI)",
                     quality: { kind: "hdmi", dac: null } };
      const pi = { id: "b827eb121359", adopted: true, online: true, caps: { speaker: {}, audio_devices: true },
                   status: { audio: { sinks: [jack, realtek], sources: [], default_sink: realtek.name } } };
      console.log(JSON.stringify({
        labels: [satDeviceLabel(realtek), satDeviceLabel(jack), satDeviceLabel(hdmi),
                 satDeviceLabel({ name: "x", description: "Plain" })],
        options: satOutputOptions(pi, {}, [pi]).options.map(o => o[1]),
        usb: satOutputQuality(realtek, "44.1 kHz · 32-bit · stereo"),
        pwm: satOutputQuality(jack, "") }));
    """)
    assert got["labels"] == ["Realtek USB2.0 Audio Analog Stereo · USB DAC · up to 32-bit · 384 kHz",
                             "Built-in Audio Stereo * · PWM, not a DAC",
                             "Built-in Audio Digital Stereo (HDMI) · HDMI: the display's own DAC", "Plain"]
    assert got["options"] == ["The system's default", "Built-in Audio Stereo * · PWM, not a DAC",
                              "Realtek USB2.0 Audio Analog Stereo · USB DAC · up to 32-bit · 384 kHz"]
    assert got["usb"] == "It takes 16, 24 or 32-bit, 44.1 to 384 kHz. Driven now at 44.1 kHz · 32-bit · stereo."
    assert got["pwm"] == ("* The Pi's own jack is PWM from the processor, not a DAC. It plays 16-bit at 48 kHz "
                          "only, with audible hiss and less detail than a DAC. For music, choose a USB DAC or a "
                          "DAC HAT.")


def test_an_output_says_whether_something_is_plugged_into_it(tmp_path):
    got = run(tmp_path, """
      const dac = { name: "alsa_output.usb-Realtek", description: "Realtek USB2.0 Audio",
                    quality: { kind: "usb", bits: [32], rates: [384000] }, jack: "unplugged" };
      console.log(JSON.stringify({
        label: satDeviceLabel(dac),
        out: satOutputQuality(dac, ""),
        in_: satOutputQuality({ ...dac, jack: "plugged" }, ""),
        event: satEventWhat({ type: "jack", device: "Realtek USB2.0 Audio", plugged: false }),
        back: satEventWhat({ type: "jack", device: "Realtek USB2.0 Audio", plugged: true }) }));
    """)
    assert got["label"] == "Realtek USB2.0 Audio · USB DAC · up to 32-bit · 384 kHz · nothing plugged in"
    assert got["out"].startswith("Nothing is plugged into it, so what it plays reaches no one.")
    assert got["in_"].startswith("Something is plugged into it.")
    assert got["event"] == "Realtek USB2.0 Audio: unplugged" and got["back"] == "Realtek USB2.0 Audio: plugged in"
