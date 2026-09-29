"""More of the Satellites tab against a hub that answers out of order.

The harness is test_satellites_writes.py's: the page's own Satellites section,
unchanged, in Node, against a fake hub. Nothing starts a server and nothing
reaches the network; without `node` on PATH these skip.

What they prevent:

  * a wake word event that left the hub before a save and arrives after it:
    it carries the list from before the save, and taken whole it was the copy
    the next edit was built from, so that edit's Save put the old assignment
    back;
  * a poll, or the hub coming back after a missed one, writing the hub's
    copy over a word somebody is in the middle of editing;
  * the rows moving on a poll, which takes the row under a finger away;
  * a /satellites answer read before an adopt and landing after it, which
    rebuilt the adopted row as pending, closed, until the next poll.
"""

# The harness, and the skip when node is not on PATH: pytest reads pytestmark
# from this module, so it is imported rather than restated.
from test_satellites_writes import pytestmark, run  # noqa: F401


def test_a_wake_word_event_from_before_a_save_cannot_undo_it(tmp_path):
    """The event is on the stream and the save's answer on its own request, so
    nothing orders them. The event still brings the word's state."""
    got = run(tmp_path, """
      let source = null;
      globalThis.EventSource = window.EventSource = class { constructor() { source = this; } };
      await satellitesRefresh();                        // opens the stream
      const before = answer().words;                    // what the hub held before the save
      tick("alexa", "aaaaaaaaaaaa");
      await wakeSave();
      source.onmessage({ data: JSON.stringify({ type: "wake_words", at: 0,
        words: before.map(w => ({ ...w, state: "error", error: "no route" })) }) });
      const state = WAKE.server.words[0].state;
      tick("alexa", "bbbbbbbbbbbb");
      await wakeSave();
      console.log(JSON.stringify({ kitchen: on("alexa", "aaaaaaaaaaaa"),
                                   bedroom: on("alexa", "bbbbbbbbbbbb"), state }));
    """)
    assert got["kitchen"] is True, got
    assert got["bedroom"] is True, got
    assert got["state"] == "error", got


def test_an_address_being_typed_survives_the_polls_and_the_hub_going_away(tmp_path):
    """What Routing's box promised, now on a word: a poll every 3 s, and a
    hub that restarts in the middle, leave an edit where it is. The draft is
    the list, and a poll only refreshes the hub's copy under it."""
    got = run(tmp_path, """
      await satellitesRefresh();
      wakeEdit("alexa", w => { wakeSetMode(w, "command", "alexa"); wakeSetDest(w, "ha_assist"); });
      wakeEdit("alexa", w => wakeField(w, "d.url", "https://homeassistant.lo"));   // half-typed
      await satellitesRefresh();
      hub.down = true;  await satellitesRefresh();
      hub.down = false; await satellitesRefresh();
      const w = WAKE.draft && WAKE.draft.find(x => x.name === "alexa");
      console.log(JSON.stringify({ url: w && w.action.destination.url, puts: hub.puts.length }));
    """)
    assert got["url"] == "https://homeassistant.lo", got
    assert got["puts"] == 0, "a poll sent an edit nobody saved"


def test_a_poll_never_reorders_the_rows_and_a_rename_does(tmp_path):
    """Rows are placed when they are added, adopted or renamed, and a poll
    leaves them where they are. The list is read back through the order each
    row was placed in, since the stand-in elements hold no DOM."""
    got = run(tmp_path, """
      const order = [];
      const was = satPlace;
      satPlace = (li, n) => { order.push(n.name); return was(li, n); };
      await satellitesRefresh();
      const first = order.length;
      await satellitesRefresh();                        // an ordinary poll
      const moved_on_poll = order.length !== first;
      hub.satellites[0].name = "zed";                   // renamed somewhere else
      await satellitesRefresh();
      console.log(JSON.stringify({ first, moved_on_poll, moved_on_rename: order.slice(first) }));
    """)
    assert got["first"] == 2, got
    assert got["moved_on_poll"] is False, got
    assert got["moved_on_rename"] == ["zed"], got


def test_activity_opens_its_stream_again_after_the_hub_restarts(tmp_path):
    """A reconnect that gets the proxy's 503 closes an EventSource for good,
    and nothing looked: after a hub restart Activity was silent until a
    reload, with the empty log still saying to press a button. A closed
    stream is let go and the next poll that reaches the hub opens another;
    meanwhile Activity and its summary say it is not connected."""
    got = run(tmp_path, """
      const sources = [];
      globalThis.EventSource = window.EventSource = class {
        constructor(url) { this.url = url; this.readyState = 0; sources.push(this); } };
      await satellitesRefresh();
      const first = sources[0];
      first.onerror();                                  // reconnecting by itself
      const retrying = { held: SATELLITES.events === first, said: $("evstate").textContent };
      await satellitesRefresh();
      const opened_while_retrying = sources.length;
      first.readyState = 2;                             // CLOSED: a non-200 answer
      first.onerror();
      const lost = { said: $("evstate").textContent, sum: $("ev-sum").textContent,
                     none: $("evnone").hidden };
      await satellitesRefresh();
      const second = sources[1];
      second.onopen();
      console.log(JSON.stringify({ retrying, opened_while_retrying, lost, count: sources.length,
                                   url: second && second.url, current: SATELLITES.events === second,
                                   cleared: $("evstate").textContent, sum: $("ev-sum").textContent }));
    """)
    lost = "Not receiving activity from the hub; it reconnects when the hub answers."
    assert got["retrying"] == {"held": True, "said": lost}, got
    assert got["opened_while_retrying"] == 1, "a second stream was opened beside one still retrying"
    assert got["lost"] == {"said": lost, "sum": "not connected", "none": True}, got
    assert got["count"] == 2 and got["url"] == "/ui/api/satellites/events" and got["current"], got
    assert got["cleared"] == "" and got["sum"] == "", got


def test_activity_marks_the_gap_while_its_stream_was_down(tmp_path):
    """The hub's stream has no replay, so what happened while it was down
    never arrives, and the log ran on as if unbroken. Once it is back, one
    plain line says so; the first connection says nothing."""
    got = run(tmp_path, """
      const sources = [], lines = [];
      globalThis.EventSource = window.EventSource = class {
        constructor(url) { this.url = url; this.readyState = 0; sources.push(this); } };
      const line = satEventLine;
      satEventLine = (li, ev, who, what) => { lines.push([ev.type, who, what, satEventBad(ev)]); return line(li, ev, who, what); };
      await satellitesRefresh();
      sources[0].onopen();
      const first = lines.length;
      sources[0].readyState = 2;
      sources[0].onerror();
      await satellitesRefresh();
      sources[1].onopen();
      sources[1].onopen();                              // said once per gap
      console.log(JSON.stringify({ first, lines }));
    """)
    assert got["first"] == 0, "the first connection was logged as a gap"
    assert got["lines"] == [["reconnected", "Hub", "reconnected; anything in between is not in this log",
                             False]], got


def test_an_update_is_one_line_in_activity_and_asks_the_hub_nothing_per_ten_per_cent(tmp_path):
    """The firmware reports progress every 10%, and each report was a line
    of its own (about fifteen of the fifty kept per update) and a refresh of
    three lists while the hub was sending the image. Progress rewrites the
    update's line and the row; the other steps keep lines of their own, in
    the words the row uses."""
    got = run(tmp_path, """
      let source = null;
      globalThis.EventSource = window.EventSource = class { constructor() { source = this; } };
      await satellitesRefresh();
      $("tab-satellites").hidden = false;               // open, so an event may refresh
      let added = 0, refreshed = 0;
      const add = satEventAdd;
      satEventAdd = li => { added++; return add(li); };
      satellitesRefresh = () => { refreshed++; };
      const id = "aaaaaaaaaaaa";
      const row = SATELLITES.rows.get(id);
      row._n = { ...row._n, ota: { state: "started", version: "v0.3.1" } };
      const send = ev => source.onmessage({ data: JSON.stringify({ at: 1000, satellite: id, type: "ota", ...ev }) });
      send({ state: "started", version: "v0.3.1" });
      const after_start = { added, refreshed };
      for (let pct = 10; pct <= 100; pct += 10) send({ state: "progress", pct, version: null });
      const progress = { added, refreshed, pct: row._n.ota.pct, state: row._n.ota.state };
      send({ state: "rebooting", version: "v0.3.1" });
      send({ state: "verified", version: "v0.3.1" });
      console.log(JSON.stringify({ after_start, progress, end: { added, refreshed },
        said: ["started", "progress", "rebooting", "verified", "failed"].map(state =>
          satEventWhat({ type: "ota", state, pct: 40, error: state === "failed" ? "bad signature" : null },
                       "v0.3.1")),
        button: satEventWhat({ type: "button", button: "vol_up", action: "release", held_ms: 820 }),
        online: satEventWhat({ type: "online", name: "kitchen", firmware: "v0.3.1" }),
        offline: satEventWhat({ type: "offline" }),
        pending: satEventWhat({ type: "pending", satellite: "a1b2c3d4e5f6" }),
        volume: satSettingsSaid({ volume: 58 }) }));
    """)
    assert got["after_start"] == {"added": 1, "refreshed": 1}, got
    assert got["progress"] == {"added": 1, "refreshed": 1, "pct": 100, "state": "progress"}, \
        "progress wrote lines of its own, or asked the hub again"
    assert got["end"] == {"added": 3, "refreshed": 3}, got
    assert got["said"] == ["update to v0.3.1 started", "updating to v0.3.1: 40%", "restarting into v0.3.1",
                           "now on v0.3.1", "update to v0.3.1 failed: bad signature"], got
    assert got["button"] == "Vol + released after 820 ms", got
    assert got["online"] == "connected, on v0.3.1" and got["offline"] == "went offline", got
    # Its line is "New satellite waiting to be adopted (ID …)", not "New
    # satellite new, waiting …".
    assert got["pending"] == "waiting to be adopted (ID a1b2c3d4e5f6)", got
    # The slider's steps, where Activity said a percent seen nowhere else.
    assert got["volume"] == "volume 7 of 12", got


def test_an_update_line_keeps_the_time_it_started_when_progress_rewrites_it(tmp_path):
    """Progress rewrites an update's line where it sits, and the rewrite took
    the report's time too. Activity is newest first, so a press logged after
    the update started sat above a line with a later time than its own."""
    got = run(tmp_path, """
      // A line as the page builds one: <time>, then <span><b>who</b> what.
      function line() {
        let built = false;
        const time = { textContent: "", dateTime: "" }, b = { textContent: "" };
        const span = { childNodes: [b, { textContent: " " }], querySelector: () => b,
                       get lastChild() { return this.childNodes[this.childNodes.length - 1]; },
                       append(t) { this.childNodes.push({ textContent: t }); } };
        return { time, span, classList: { toggle() {} },
                 set innerHTML(v) { built = true; },
                 querySelector: sel => built ? (sel === "time" ? time : span) : null };
      }
      const li = line();
      const ev = (at, state, pct) => ({ at, satellite: "aaaaaaaaaaaa", type: "ota", state, pct,
                                        version: "v0.3.1" });
      satEventLine(li, ev(1000, "started"), "kitchen", "update to v0.3.1 started");
      const first = li.time.dateTime;
      satEventLine(li, ev(1090, "progress", 40), "kitchen", "updating to v0.3.1: 40%");
      console.log(JSON.stringify({ first, after: li.time.dateTime,
                                   what: li.span.lastChild.textContent }));
    """)
    assert got["first"] == "1970-01-01T00:16:40.000Z", got
    assert got["after"] == got["first"], "a rewrite moved the line's time past the lines above it"
    assert got["what"] == "updating to v0.3.1: 40%", got


def test_a_poll_read_before_an_adopt_does_not_undo_it(tmp_path):
    """A timer poll reads /satellites, Adopt is pressed and answered, the
    adopt's own poll lands, and then the old poll does, saying the satellite
    is pending. It rebuilt the row as pending, closed and without its
    "adopted" line; the next poll rebuilt it adopted but shut."""
    got = run(tmp_path, r"""
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      hub.satellites.push({ id: "cccccccccccc", name: "", adopted: false, online: true,
                            config: {}, status: {}, wake_words: [] });
      let holdOnce = false, held = null;
      const real = json;
      json = async (path, options) => {
        const method = (options && options.method) || "GET";
        if (method === "GET" && path === "/satellites" && holdOnce) {
          holdOnce = false;
          const then = { satellites: JSON.parse(JSON.stringify(hub.satellites)) };
          return new Promise(r => { held = () => r(then); });
        }
        if (method === "POST" && path === "/satellites/cccccccccccc/adopt") {
          const s = hub.satellites.find(x => x.id === "cccccccccccc");
          s.adopted = true; s.name = "study";
          await sleep(5);
          return JSON.parse(JSON.stringify(s));
        }
        return real(path, options);
      };
      const builds = [];
      const build = satelliteBuild;
      satelliteBuild = (li, n) => { builds.push(!!n.adopted); return build(li, n); };
      await satellitesRefresh();
      const li = SATELLITES.rows.get("cccccccccccc");
      builds.length = 0;
      holdOnce = true;
      const old = satellitesRefresh();                  // reads before the adopt
      await sleep(10);
      await satelliteAct(li, "adopt", stand());
      await sleep(30);                                  // the adopt's own poll
      held();                                           // the old one lands
      await old;
      await sleep(10);
      console.log(JSON.stringify({ builds, adopted: li.dataset.adopted,
                                   listed: SATELLITES.list.find(n => n.id === "cccccccccccc").adopted }));
    """)
    assert got["builds"] == [True], got
    assert got["adopted"] == "1" and got["listed"] is True, got
