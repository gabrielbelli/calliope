"""More of the Satellites tab against a hub that answers out of order.

The harness is test_satellites_writes.py's: the page's own Satellites section,
unchanged, in Node, against a fake hub. Nothing starts a server and nothing
reaches the network; without `node` on PATH these skip.

What they prevent:

  * a wake word event that left the hub before a save and arrives after it:
    it carries the list from before the save, and taken whole it was the copy
    the next tick was built from, so that tick put the old assignment back;
  * the Routing card coming back after a missed poll by writing the hub's
    rules over an edit somebody was in the middle of.
"""

# The harness, and the skip when node is not on PATH: pytest reads pytestmark
# from this module, so it is imported rather than restated.
from test_satellites_writes import pytestmark, run  # noqa: F401


def test_a_wake_word_event_from_before_a_tick_cannot_undo_it(tmp_path):
    """The event is on the stream and the save's answer on its own request, so
    nothing orders them. The event still brings the word's state."""
    got = run(tmp_path, """
      let source = null;
      globalThis.EventSource = window.EventSource = class { constructor() { source = this; } };
      await satellitesRefresh();                        // opens the stream
      const before = answer().words;                    // what the hub held before the tick
      const kitchen = tick("alexa");
      await satelliteWordToggle(card("aaaaaaaaaaaa"), kitchen);
      source.onmessage({ data: JSON.stringify({ type: "wake_words", at: 0,
        words: before.map(w => ({ ...w, state: "error", error: "no route" })) }) });
      const state = WAKE.server.words[0].state;
      const bedroom = tick("alexa");
      await satelliteWordToggle(card("bbbbbbbbbbbb"), bedroom);
      console.log(JSON.stringify({
        kitchen_ticked: kitchen.checked, kitchen_on_hub: on("alexa", "aaaaaaaaaaaa"),
        bedroom_ticked: bedroom.checked, bedroom_on_hub: on("alexa", "bbbbbbbbbbbb"), state }));
    """)
    assert got["kitchen_ticked"] == got["kitchen_on_hub"] is True, got
    assert got["bedroom_ticked"] == got["bedroom_on_hub"] is True, got
    assert got["state"] == "error", got


def test_the_routing_card_comes_back_after_a_missed_poll_without_overwriting_an_edit(tmp_path):
    """The card is loaded again after the hub was away, which is what shows
    it. The rules somebody was editing while it was away stay in the box: the
    hub's copy goes in only when the box still holds what it put there."""
    got = run(tmp_path, """
      $("routetext").value = "";                        // the empty box the page starts with
      await satellitesRefresh();
      await new Promise(r => setTimeout(r, 50));        // the first routing load fills it
      const filled = $("routetext").value.includes('"rules"');
      $("routetext").value = '{"version": 1, "rules": [ half-typed';
      hub.down = true;  await satellitesRefresh();
      hub.down = false; await satellitesRefresh();
      await new Promise(r => setTimeout(r, 50));
      console.log(JSON.stringify({ filled, shown: $("routecard").hidden === false,
                                   kept: $("routetext").value.endsWith("half-typed") }));
    """)
    assert got["filled"], got
    assert got["shown"], got
    assert got["kept"], got
