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
  * the rows moving on a poll, which takes the row under a finger away.
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
