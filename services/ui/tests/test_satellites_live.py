"""The Satellites tab between polls: what the hub's events change by themselves,
and how often the tab still asks.

The tab asked the hub for three lists every 3 s for as long as it was open,
hidden page or not, and threw away the status every satellite sends every ten
seconds, which is the same answer. It applies those now, asks every 30 s while
the stream is open and nothing on the list is moving, and clears the chips
that run out on a timer of their own. These run the page's own Satellites
section in Node, through test_satellites_writes.py's harness and fake hub.

What they prevent:

  * a row that waits up to 30 s for a change its status event already said;
  * a 30 s poll while something only a poll can report is moving, or while
    there is no stream to report anything at all;
  * a Listening chip that stays lit until the next poll;
  * a background tab that keeps asking;
  * a stream that closed for good while the tab was shut, never reopened;
  * a telemetry read that left before a change and lands after it, putting
    the old setting back on screen.
"""

from test_satellites_writes import pytestmark, run  # noqa: F401

# A stream the page can open, and every GET of the satellite list counted.
# `send` delivers one event as the hub would; `delays` records each next poll
# the tab schedules.
STREAM = """
  let source = null;
  globalThis.EventSource = window.EventSource =
    class { constructor() { source = this; this.readyState = 1; } close() { this.readyState = 2; } };
  const send = ev => source.onmessage({ data: JSON.stringify({ at: Date.now() / 1000, ...ev }) });
  let lists = 0;
  const answer = json;
  json = (path, options) => {
    if (path === "/satellites" && !(options && options.method)) lists++;
    return answer(path, options);
  };
  const delays = [];
  const schedule = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...args) => {
    if (fn === satellitesRefresh) delays.push(ms);
    return schedule(fn, ms, ...args);
  };
  const KITCHEN = "aaaaaaaaaaaa";
  const key = () => satState(SATELLITES.rows.get(KITCHEN)._n, satMem()).key;
"""


def test_a_status_event_updates_the_row_without_asking_the_hub(tmp_path):
    """The kitchen is muted on the device. The status it sends says so, and
    the row says so, with no request."""
    got = run(tmp_path, STREAM + """
      await satellitesRefresh();
      const before = lists, was = key();
      send({ type: "status", satellite: KITCHEN, status: { muted: true, volume: 30 } });
      const listed = SATELLITES.list.find(n => n.id === KITCHEN);
      console.log(JSON.stringify({ was, now: key(), asked: lists - before,
                                   listed: listed.status.muted, volume: SATELLITES.rows.get(KITCHEN)._n.status.volume }));
    """)
    assert got == {"was": "online", "now": "muted", "asked": 0, "listed": True, "volume": 30}, got


def test_the_tab_polls_every_thirty_seconds_while_the_stream_is_open_and_nothing_moves(tmp_path):
    got = run(tmp_path, STREAM + """
      $("tab-satellites").hidden = false;
      await satellitesRefresh();
      console.log(JSON.stringify(delays));
    """)
    assert got == [30000], got


def test_the_tab_polls_every_three_seconds_without_a_stream_or_while_a_row_is_listening(tmp_path):
    """No stream means the poll is all there is. With one, a row listening for
    a command is a state whose end no event may report (a wake word nothing
    answered), so it is watched at 3 s, from the wake word on rather than
    from the next calm poll; once that has run out, 30 s again."""
    got = run(tmp_path, STREAM + """
      $("tab-satellites").hidden = false;
      const EventSourceWas = window.EventSource;
      window.EventSource = undefined;
      await satellitesRefresh();
      const without = delays.slice();
      window.EventSource = EventSourceWas;
      await satellitesRefresh();
      const calm = delays.at(-1);
      send({ type: "wake", satellite: KITCHEN, wake_word: "alexa", score: 0.91 });
      const woken = delays.at(-1);
      await satellitesRefresh();
      const listening = delays.at(-1);
      SATELLITES.woke.set(KITCHEN, Date.now() - 16000);
      await satellitesRefresh();
      console.log(JSON.stringify({ without, calm, woken, listening, after: delays.at(-1) }));
    """)
    assert got == {"without": [3000], "calm": 30000, "woken": 3000, "listening": 3000,
                   "after": 30000}, got


def test_a_hidden_page_schedules_no_satellite_poll(tmp_path):
    got = run(tmp_path, STREAM + """
      document.hidden = true;
      $("tab-satellites").hidden = false;
      await satellitesRefresh();
      console.log(JSON.stringify(delays));
    """)
    assert got == [], got


def test_a_config_event_asks_the_hub_again_while_the_tab_is_open(tmp_path):
    """A setting changed in Home Assistant arrives as a config event. With the
    tab polling every 30 s it is asked for at once, while somebody can see it,
    and not otherwise."""
    got = run(tmp_path, STREAM + """
      await satellitesRefresh();
      const settle = () => new Promise(r => setTimeout(r, 30));
      const before = lists;
      send({ type: "config", satellite: KITCHEN, changed: ["volume"] });
      await settle();
      const shut = lists - before;
      $("tab-satellites").hidden = false;
      send({ type: "config", satellite: KITCHEN, changed: ["volume"] });
      await settle();
      const open = lists - before;
      document.hidden = true;
      send({ type: "firmware", action: "added", sha256: "e".repeat(64), model: "esp32-s3", version: "v1" });
      await settle();
      console.log(JSON.stringify({ shut, open, hidden: lists - before - open }));
    """)
    assert got == {"shut": 0, "open": 1, "hidden": 0}, got


def test_a_stream_that_closes_for_good_tells_the_live_layer(tmp_path):
    """A reconnect the browser makes by itself is not news; a stream the
    browser has given up on is, because with the tab shut nothing else would
    open another."""
    got = run(tmp_path, STREAM + """
      const told = [];
      SATELLITES.hooks.live = what => told.push(what);
      await satellitesRefresh();
      source.readyState = 0;
      source.onerror();
      const reconnecting = told.slice();
      source.readyState = 2;
      source.onerror();
      console.log(JSON.stringify({ reconnecting, closed: told, events: SATELLITES.events }));
    """)
    assert got == {"reconnecting": [], "closed": ["closed"], "events": None}, got


def test_the_listening_chip_is_repainted_when_it_expires_without_a_poll(tmp_path):
    """Listening lasts 15 s after a wake word. It used to be cleared by the
    next 3 s poll that repainted the row; at 30 s it would have stayed lit
    for half a minute. Its own timer clears it, and asks the hub nothing."""
    got = run(tmp_path, STREAM + """
      $("tab-satellites").hidden = false;
      await satellitesRefresh();
      const timers = [];
      const keep = globalThis.setTimeout;
      globalThis.setTimeout = (fn, ms, ...args) => {
        if (fn !== satellitesRefresh) timers.push({ fn, ms });
        return keep(fn, ms, ...args);
      };
      send({ type: "wake", satellite: KITCHEN, wake_word: "alexa", score: 0.91 });
      const lit = key(), expiry = timers.at(-1), before = lists;
      const real = Date.now;
      Date.now = () => real() + expiry.ms;
      expiry.fn();
      console.log(JSON.stringify({ lit, after: key(), ms: expiry.ms, asked: lists - before }));
    """)
    assert got["lit"] == "listening" and got["after"] == "online", got
    assert 15000 <= got["ms"] <= 15100, got
    assert got["asked"] == 0, got


def test_satellites_count_says_whether_the_hub_answered(tmp_path):
    got = run(tmp_path, """
      const up = await satellitesCount();
      hub.down = true;
      const down = await satellitesCount();
      console.log(JSON.stringify({ up, down }));
    """)
    assert got == {"up": True, "down": False}, got


def test_a_telemetry_read_that_a_change_overtook_is_dropped(tmp_path):
    """Telemetry is read every 30 s while its section is open. A read that
    left before the switch was turned on, and landed after the switch's own
    answer, put Off back on screen."""
    got = run(tmp_path, """
      await satellitesRefresh();
      await new Promise(r => setTimeout(r, 30));
      const answer = json;
      let land = null;
      json = (path, options) => {
        if (path === "/satellites/telemetry" && !(options && options.method)) {
          const before = JSON.parse(JSON.stringify(hub.telemetry));
          return new Promise(r => { land = () => r(before); });
        }
        return answer(path, options);
      };
      const reading = tmLoad();
      await tmSend("PUT", { enabled: true });
      land();
      await reading;
      console.log(JSON.stringify({ enabled: TELEMETRY.state.enabled, hub: hub.telemetry.enabled }));
    """)
    assert got == {"enabled": True, "hub": True}, got
