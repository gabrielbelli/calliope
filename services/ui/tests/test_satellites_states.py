"""What a satellite row says about itself, run rather than read.

satState() is the one place a row's state word, chip and line come from, and
it is pure, so the whole table is walked here: the page's own Satellites
section in Node, through test_satellites_writes.py's harness, with no DOM and
no network. Without `node` on PATH these skip.

What they prevent:

  * a satellite rebooting into new firmware reported as Offline, a warning
    on every successful update;
  * a healthy satellite drawn as a chip, which makes the one row that is
    wrong no easier to find than the rows that are fine;
  * a row that claims a wake word nobody has saved, or hides that a word it
    is assigned is still downloading;
  * a button mapping sent that the hub refuses, or one with no mute left in it.
"""

from test_satellites_writes import pytestmark, run  # noqa: F401

SAT = """
const base = { id: "aaaaaaaaaaaa", name: "Kitchen", adopted: true, online: true,
               config: {}, status: {}, ota: null, listening: { state: "idle", conversation: null } };
const sat = over => ({ ...base, ...over });
const mem = over => ({ now: 1000000, restarting: new Map(), woke: new Map(), online: new Map(),
                       words: [], ...over });
const pick = s => ({ key: s.key, word: s.word, kind: s.kind, state: s.dataState, line: s.line });
"""


def test_the_state_table_first_match_wins(tmp_path):
    got = run(tmp_path, SAT + """
      const words = [{ name: "hey_jarvis", satellites: ["*"], state: "ready" }];
      const out = {
        healthy: pick(satState(sat({}), mem({ words }))),
        updating: pick(satState(sat({ ota: { state: "progress", pct: 42, version: "v0.3.1" } }), mem())),
        updating_no_pct: satState(sat({ ota: { state: "requested", version: "v0.3.1" } }), mem()).word,
        failed: pick(satState(sat({ ota: { state: "failed", version: "v0.3.1", error: "bad signature" } }), mem())),
        offline: pick(satState(sat({ online: false }), mem())),
        deaf: pick(satState(sat({ listening: { state: "off", error: "4 channels expected" } }), mem())),
        listening: satState(sat({ listening: { state: "listening", conversation: "listening" } }), mem()).word,
        woke: satState(sat({}), mem({ woke: new Map([["aaaaaaaaaaaa", 999000]]) })).word,
        woke_long_ago: satState(sat({}), mem({ woke: new Map([["aaaaaaaaaaaa", 900000]]) })).word,
        answering: satState(sat({ listening: { state: "busy", conversation: "replying" } }), mem()).word,
        updated: pick(satState(sat({ ota: { state: "verified", version: "v0.3.1" } }), mem())),
        muted: pick(satState(sat({ status: { muted: true } }), mem())),
        mic_off: pick(satState(sat({ config: { mic_enabled: false } }), mem())),
        speaker_off: pick(satState(sat({ config: { speaker_enabled: false } }), mem())),
        muted_updated: satState(sat({ ota: { state: "verified", version: "v0.3.1" }, status: { muted: true } }),
                                mem()).word,
        mic_off_updated: satState(sat({ ota: { state: "verified", version: "v0.3.1" },
                                        config: { mic_enabled: false } }), mem()).word,
        pending: pick(satState(sat({ adopted: false, name: "" }), mem())),
        seen: pick(satState(sat({ adopted: false, online: false, last_seen: 999 }), mem())),
      };
      console.log(JSON.stringify(out));
    """)
    # Healthy is quiet: a word, no chip.
    assert got["healthy"] == {"key": "online", "word": "Online", "kind": "", "state": "online",
                              "line": "Listens for hey jarvis"}, got
    assert got["updating"]["word"] == "Updating 42%" and got["updating"]["kind"] == "running"
    assert got["updating"]["line"] == "Updating to v0.3.1. It reboots when the transfer ends."
    assert got["updating_no_pct"] == "Updating"
    assert got["failed"]["word"] == "Update failed" and got["failed"]["kind"] == "failed"
    assert got["failed"]["line"] == "Update to v0.3.1 failed: bad signature"
    assert got["offline"]["word"] == "Offline" and got["offline"]["kind"] == "warn"
    assert got["deaf"]["word"] == "Not listening" and got["deaf"]["kind"] == "failed"
    assert got["listening"] == got["woke"] == "Listening"
    assert got["woke_long_ago"] == "Online", "a wake word heard 100 s ago still reads Listening"
    assert got["answering"] == "Answering"
    assert got["updated"]["kind"] == "done" and got["updated"]["line"] == "Now on v0.3.1"
    # Muted and Mic off are choices: a chip with no colour, not a warning.
    assert got["muted"]["kind"] == got["mic_off"]["kind"] == got["speaker_off"]["kind"] == "neutral"
    assert got["speaker_off"]["word"] == "Speaker off", got
    # A standing condition outranks the ten minutes an update's result is kept.
    assert (got["muted_updated"], got["mic_off_updated"]) == ("Muted", "Mic off"), got
    assert got["pending"]["word"] == "New" and got["pending"]["kind"] == "warn"
    assert got["pending"]["line"] == "Waiting to be adopted"
    assert got["seen"]["word"] == "Seen" and got["seen"]["kind"] == "neutral"


def test_a_rebooting_update_is_not_reported_as_offline(tmp_path):
    """The hub drops `ota` with the session, so the only trace of an update
    that is restarting the satellite is the `rebooting` event the page
    remembered. For two minutes that is Restarting; after it, Offline."""
    got = run(tmp_path, SAT + """
      const off = sat({ online: false });
      const back = new Map([["aaaaaaaaaaaa", { v: "v0.3.1", at: 1000000 - 30000 }]]);
      const late = new Map([["aaaaaaaaaaaa", { v: "v0.3.1", at: 1000000 - 130000 }]]);
      console.log(JSON.stringify({
        restarting: pick(satState(off, mem({ restarting: back }))),
        later: satState(off, mem({ restarting: late })).word,
        online_rebooting: satState(sat({ ota: { state: "rebooting", version: "v0.3.1" } }), mem()).word }));
    """)
    assert got["restarting"]["word"] == "Restarting", got
    assert got["restarting"]["state"] == "updating", "Reboot and Move are offered mid-update"
    assert got["restarting"]["line"] == "Installing v0.3.1. It comes back on its own."
    assert got["later"] == "Offline", got
    assert got["online_rebooting"] == "Restarting", got


def test_a_reboot_pressed_here_is_not_reported_as_offline(tmp_path):
    """The hub answers /reboot at once and remembers nothing, so ten seconds
    after a Reboot that had just been confirmed, the row went amber Offline
    and the health line said "1 offline". The page remembers the press."""
    got = run(tmp_path, SAT + """
      const li = { dataset: { id: "aaaaaaaaaaaa" }, _n: sat({}), querySelector: () => stand() };
      const real = json;                                // /reboot is the hub's 204
      json = async (path, o) => path.endsWith("/reboot") ? null : real(path, o);
      await satelliteAct(li, "reboot", stand());
      const off = sat({ online: false });
      SATELLITES.list = [off];
      satellitesHealth();
      console.log(JSON.stringify({ asked, remembered: SATELLITES.restarting.get("aaaaaaaaaaaa"),
        state: pick(satState(off, satMem())), health: $("sathealth").textContent,
        later: satState(off, mem({ restarting: new Map([["aaaaaaaaaaaa",
          { v: null, reboot: true, at: 1000000 - 130000 }]]) })).word }));
    """)
    assert got["asked"] == ["Reboot Kitchen? It is back in about ten seconds."], got
    assert got["remembered"]["reboot"] is True and got["remembered"]["v"] is None, got
    assert got["state"]["word"] == "Restarting", got
    assert got["state"]["line"] == "Rebooting. It comes back on its own.", "a reboot read as new firmware"
    assert got["health"] == "", "a reboot is counted as offline"
    assert got["later"] == "Offline", got


def test_a_restart_is_over_once_the_satellite_has_gone_and_come_back(tmp_path):
    """A Reboot (or an update's rebooting) is remembered for two minutes, and
    it was kept for all of them: a satellite back after ten seconds that then
    really dropped read Restarting, "It comes back on its own", and was not
    counted offline. It ends when the satellite is seen back after being
    seen gone, by a poll or by the stream's offline event; a poll that still
    finds it online just after the press is not the end."""
    got = run(tmp_path, SAT + """
      let source = null;
      globalThis.EventSource = window.EventSource = class { constructor() { source = this; } };
      const id = "aaaaaaaaaaaa";
      const real = json;                                // /reboot is the hub's 204
      json = async (path, o) => path.endsWith("/reboot") ? null : real(path, o);
      await satellitesRefresh();
      const li = SATELLITES.rows.get(id);
      await satelliteAct(li, "reboot", stand());
      await satellitesRefresh();                        // not gone yet
      const before = SATELLITES.restarting.has(id);
      hub.satellites[0].online = false;
      await satellitesRefresh();
      const away = satState(SATELLITES.list[0], satMem()).word;
      hub.satellites[0].online = true;
      await satellitesRefresh();
      const back = SATELLITES.restarting.has(id);
      hub.satellites[0].online = false;                 // unplugged, a minute in
      await satellitesRefresh();
      const dropped = satState(SATELLITES.list[0], satMem()).word;
      // A tab that polled nothing while it was away: the stream said so.
      hub.satellites[0].online = true;
      await satellitesRefresh();
      await satelliteAct(li, "reboot", stand());
      source.onmessage({ data: JSON.stringify({ at: 1000, satellite: id, type: "offline" }) });
      await satellitesRefresh();
      console.log(JSON.stringify({ before, away, back, dropped, streamed: SATELLITES.restarting.has(id) }));
    """)
    assert got["before"] is True, "a poll from before the satellite left ended the restart"
    assert got["away"] == "Restarting", got
    assert got["back"] is False, "the restart was remembered after the satellite came back"
    assert got["dropped"] == "Offline", "a satellite that dropped after its reboot read Restarting"
    assert got["streamed"] is False, got


def test_an_update_that_broke_off_is_not_reported_as_offline(tmp_path):
    """The hub keeps an update's state on the connection, and a transfer that
    breaks off ends the connection: the only trace is the `failed` event,
    sent with no version. Remembered, it reads Update failed for as long as
    the hub keeps a result (600 s), and the health line counts it; after
    that, Offline."""
    got = run(tmp_path, SAT + """
      const off = sat({ id: "a1", online: false });
      const lost = new Map([["a1", { v: "v0.3.1", error: "disconnected mid-transfer", at: 1000000 - 60000 }]]);
      const old = new Map([["a1", { v: "v0.3.1", error: "disconnected mid-transfer", at: 1000000 - 601000 }]]);
      SATELLITES.list = [off];
      SATELLITES.otaFailed.set("a1", { v: "v0.3.1", error: "disconnected mid-transfer", at: Date.now() });
      satellitesHealth();
      console.log(JSON.stringify({
        failed: pick(satState(off, mem({ failed: lost }))),
        later: satState(off, mem({ failed: old })).word,
        online_is_the_hubs: satState(sat({ id: "a1" }), mem({ failed: lost })).word,
        health: $("sathealth").textContent }));
    """)
    assert got["failed"]["word"] == "Update failed" and got["failed"]["kind"] == "failed", got
    assert got["failed"]["line"] == "Update to v0.3.1 failed: disconnected mid-transfer", got
    assert got["later"] == "Offline", got
    assert got["online_is_the_hubs"] == "Online", "a satellite that came back still reads failed"
    assert got["health"] == "1 update failed", got


def test_last_seen_is_the_hubs_before_the_pages(tmp_path):
    """The hub records when an adopted satellite left. The page's own memory
    is the last poll that found it online, hours stale if the tab was closed
    at the time, so it stands in only when the hub has nothing (a hub
    restart)."""
    got = run(tmp_path, SAT + """
      const seen = new Map([["aaaaaaaaaaaa", 1000]]);
      console.log(JSON.stringify({
        hub: satLastSeen(sat({ online: false, last_seen: 5000 }), seen),
        page: satLastSeen(sat({ online: false }), seen),
        none: satLastSeen(sat({ online: false }), new Map()) }));
    """)
    assert got == {"hub": 5000, "page": 1000, "none": None}, got


def test_a_satellite_is_not_offered_the_update_it_is_taking(tmp_path):
    """Mid-update, Hall's Device offered "Update to v0.3.1" (greyed) and
    Firmware counted it among the satellites the image was ready for."""
    got = run(tmp_path, SAT + """
      SATELLITES.firmware = [{ model: "m", version: "v0.2.0", uploaded_at: 1, sha256: "a" },
                             { model: "m", version: "v0.3.1", uploaded_at: 2, sha256: "b" }];
      const on = over => sat({ model: "m", firmware: "v0.2.0", ...over });
      const v = img => img ? img.version : null;
      SATELLITES.restarting.set("r1", { v: "v0.3.1", at: Date.now() });
      console.log(JSON.stringify({
        idle: v(satImageFor(on({}))),
        taking: v(satImageFor(on({ ota: { state: "progress", pct: 42, version: "v0.3.1" } }))),
        rebooting: v(satImageFor(on({ ota: { state: "rebooting", version: "v0.3.1" } }))),
        restarting: v(satImageFor(on({ id: "r1", online: false }))),
        other: v(satImageFor(on({ ota: { state: "progress", version: "v0.2.5" } }))),
        failed: v(satImageFor(on({ ota: { state: "failed", version: "v0.3.1" } }))),
        current: v(satImageFor(on({ firmware: "v0.3.1" }))) }));
    """)
    assert got["idle"] == "v0.3.1", got
    assert got["taking"] is None and got["rebooting"] is None and got["restarting"] is None, got
    assert got["other"] == "v0.3.1", "an update to another version hides the newest image"
    assert got["failed"] == "v0.3.1", "a failed update is not offered again"
    assert got["current"] is None, got


def test_a_row_says_what_it_listens_for_from_the_saved_words(tmp_path):
    """A word is heard only once it has downloaded, so the line names one
    still coming. With no word, the satellite still answers a button set to Talk.
    On a hub with no assignment at all the line is empty rather than a guess.
    A word that failed is named only on a row it leaves with nothing to hear:
    the health line and the Wake words summary say it once, and it used to be
    repeated on every row (THIS REPLACES the expectation that every row names
    it)."""
    got = run(tmp_path, SAT + """
      const words = [{ name: "hey_jarvis", satellites: ["*"], state: "ready" },
                     { name: "alexa", satellites: ["aaaaaaaaaaaa"], state: "downloading" },
                     { name: "weather", satellites: ["bbbbbbbbbbbb"], state: "ready" },
                     { name: "hey_mycroft", satellites: ["aaaaaaaaaaaa"], state: "error" }];
      console.log(JSON.stringify({
        mixed: satListens(sat({}), words),
        none: satListens(sat({}), [{ name: "weather", satellites: ["bbbbbbbbbbbb"], state: "ready" }]),
        coming: satListens(sat({}), [{ name: "alexa", satellites: ["*"], state: "downloading" }]),
        old_hub: satListens(sat({}), null),
        from_row: satListens(sat({ wake_words: ["hey_jarvis"] }), null),
        only_broken: satListens(sat({}), [{ name: "hey_mycroft", satellites: ["*"], state: "error" }]),
        draft_ignored: (() => { WAKE.server = { words }; WAKE.draft = [{ name: "weather", threshold: 0.5,
          satellites: ["*"] }]; return satState(sat({}), satMem()).line; })() }));
    """)
    assert got["mixed"] == "Listens for hey jarvis; alexa is downloading", got
    assert got["only_broken"] == "Push-to-talk only; hey mycroft failed to download", got
    assert got["none"] == "Push-to-talk only", got
    assert got["coming"] == "Push-to-talk only; alexa is downloading", got
    assert got["old_hub"] == "", got
    assert got["from_row"] == "Listens for hey jarvis", got
    assert "weather" not in got["draft_ignored"], "a row shows a wake word nobody has saved"


def test_a_button_mapping_is_one_the_hub_accepts(tmp_path):
    """The hub replaces the whole mapping, refuses a webhook with a user in it
    or no scheme, and refuses a mapping with no mute left in it, since only a
    button undoes the mute. A mute on Side alone does not count: a stock board
    does not wire it, and the firmware made Rec the mute while the grid showed
    Rec as Talk. Any button may do anything else, Rec included, and Side may
    mute beside another. "none" is the default and is left out."""
    got = run(tmp_path, SAT + """
      const e = (button, edge, action, url) => ({ button, edge, action, url: url || "" });
      const mute = e("rec", "press", "mute");
      console.log(JSON.stringify({
        good: satMapping([e("play", "press", "ptt"), e("play", "release", "none"),
                          e("set", "press", "stop"), e("mode", "press", "webhook", "https://ha.local/hook"),
                          e("key1", "press", "lights"), e("rec", "press", "lights"),
                          e("vol_up", "release", "brighter"), e("vol_down", "release", "mute")]),
        no_mute: satMapping([e("play", "press", "ptt"), e("rec", "press", "none")]),
        side_only: satMapping([e("key1", "press", "mute"), e("rec", "press", "ptt")]),
        side_too: satMapping([e("key1", "release", "mute"), e("mode", "press", "mute")]),
        userinfo: satMapping([mute, e("mode", "press", "webhook", "https://me:pw@ha.local/hook")]),
        scheme: satMapping([mute, e("mode", "press", "webhook", "ha.local/hook")]),
        empty: satMapping([mute, e("mode", "press", "webhook", "")]),
        note: satButtonsNote({ play: { press: "ptt" }, set: { press: "stop" },
                               mode: { press: "webhook:https://x.local" } }, ["play", "set", "mode"]),
        note_device: satButtonsNote({ rec: { press: "mute" }, vol_up: { press: "volume_up" },
                                      key1: { press: "lights" }, mode: { press: "dimmer" } },
                                    ["rec", "mode", "vol_up", "key1"]),
        note_none: satButtonsNote({}, ["play", "set"]),
        note_default: satButtonsNote({ rec: { press: "mute" }, vol_up: { press: "volume_up" },
                                       vol_down: { press: "volume_down" }, play: { press: "ptt" },
                                       set: { press: "stop" } },
                                     ["rec", "mode", "play", "set", "vol_down", "vol_up"]),
        note_only_namesakes: satButtonsNote({ rec: { press: "mute" } }, ["rec", "play"]),
        keys_six: satButtonKeys(sat({ caps: { buttons: ["vol_up", "vol_down", "set", "play", "mode", "rec"] } })),
        keys_seven: satButtonKeys(sat({ caps: { buttons: ["vol_up", "vol_down", "set", "play", "mode", "rec", "key1"] } })),
        keys_offline: satButtonKeys(sat({ caps: {}, config: { buttons: { custom: { press: "ptt" } } } })) }));
    """)
    assert got["good"] == {"mapping": {
        "play": {"press": "ptt"}, "set": {"press": "stop"}, "mode": {"press": "webhook:https://ha.local/hook"},
        "key1": {"press": "lights"}, "rec": {"press": "lights"}, "vol_up": {"release": "brighter"},
        "vol_down": {"release": "mute"}}}, got
    for refused in ("no_mute", "side_only", "userinfo", "scheme", "empty"):
        assert "error" in got[refused], (refused, got[refused])
    assert "muted" in got["no_mute"]["error"]
    assert "other than Side" in got["side_only"]["error"], got["side_only"]
    assert got["side_too"] == {"mapping": {"key1": {"release": "mute"}, "mode": {"press": "mute"}}}, got
    assert got["note"] == "Play talks, Set stops, Mode calls a webhook", got
    # A key doing what it is printed with goes unsaid, unless that is all.
    assert got["note_device"] == "Mode dims, Side switches the lights", got
    assert got["note_default"] == "Play talks, Set stops", got
    assert got["note_only_namesakes"] == "Rec mutes", got
    assert got["note_none"] == "nothing mapped", got
    # All of them, none special, in the order they sit on the board.
    assert got["keys_six"] == ["rec", "mode", "play", "set", "vol_down", "vol_up"], got
    assert got["keys_seven"] == ["rec", "mode", "play", "set", "vol_down", "vol_up", "key1"], got
    assert got["keys_offline"] == ["rec", "mode", "play", "set", "vol_down", "vol_up", "custom"], got


def test_a_muted_satellite_names_the_button_that_unmutes_it(tmp_path):
    """Mute can be on any button since buttons became freely mapped, and Rec
    can be set to something else. The row sent people to Rec whatever it
    now does, and a press on the device is the only way out of a mute."""
    got = run(tmp_path, SAT + """
      const muted = buttons => sat({ status: { muted: true }, config: { buttons } });
      console.log(JSON.stringify({
        mode: satWhy(muted({ rec: { press: "ptt" }, mode: { press: "mute" } })),
        two: satWhy(muted({ rec: { press: "mute" }, key1: { release: "mute" } })),
        none: satWhy(muted({})),
        unmuted: satWhy(sat({ config: { buttons: { rec: { press: "mute" } } } })),
        offline: satWhy(sat({ online: false, status: { muted: true } })) }));
    """)
    assert got["mode"] == ("It is muted on the device, and only its Mode button turns the "
                           "microphones back on."), got
    assert "Rec" not in got["mode"], got
    assert "its Rec or Side button" in got["two"], got
    assert got["none"] == "It is muted on the device, and only a button on it turns the microphones back on."
    assert got["unmuted"] == "", got
    assert got["offline"].startswith("It is offline"), got


def test_the_health_line_counts_faults_and_not_choices(tmp_path):
    got = run(tmp_path, SAT + """
      SATELLITES.list = [sat({ id: "a1", online: false }), sat({ id: "a2", online: false }),
                         sat({ id: "a3", ota: { state: "failed", version: "v1" } }),
                         sat({ id: "a4", status: { muted: true } }),
                         sat({ id: "a5", config: { mic_enabled: false } }),
                         sat({ id: "a7", config: { speaker_enabled: false } }),
                         sat({ id: "a6", adopted: false, online: false })];
      WAKE.server = { words: [{ name: "alexa", satellites: ["*"], state: "error" }],
                      load_error: "wake_words.json could not be loaded" };
      satellitesHealth();
      const busy = $("sathealth").textContent;
      SATELLITES.list = [sat({})]; WAKE.server = { words: [], load_error: null };
      satellitesHealth();
      console.log(JSON.stringify({ busy, calm: $("sathealth").textContent }));
    """)
    assert got["busy"] == ("2 offline · 1 update failed · alexa failed to download · "
                           "wake words did not load"), got
    assert got["calm"] == "", "the health line says something when all is well"


def test_two_ring_presses_at_once_leave_one_idle_timer_and_draw_in_turn(tmp_path):
    """The idle cancel was cleared before the draw and set after it, so two
    presses whose draws overlapped each cleared the same old timer and each
    set one. The first, which nothing could clear any more, cancelled a
    later set-up when it ran. And the draws went out together, in either
    order."""
    got = run(tmp_path, r"""
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const idle = new Set(), realSet = setTimeout, realClear = clearTimeout;
      globalThis.setTimeout = (fn, ms, ...rest) => {
        const t = realSet(fn, ms, ...rest);
        if (ms === RING_IDLE_MS) idle.add(t);
        return t;
      };
      globalThis.clearTimeout = t => { idle.delete(t); return realClear(t); };
      const lights = [];
      let out = 0, most = 0;
      const real = json;
      json = async (path, options) => {
        const method = (options && options.method) || "GET";
        if (method === "POST" && path === "/satellites/aaaaaaaaaaaa/lights") {
          const b = JSON.parse(options.body);
          out++; most = Math.max(most, out);
          lights.push(b.mode === "pixels" ? b.pixels.findIndex(p => p[0] === 255) : b.mode);
          await sleep(15);
          out--;
          return null;
        }
        return real(path, options);
      };
      await satellitesRefresh();
      const li = SATELLITES.rows.get("aaaaaaaaaaaa");
      li._ring = null;                                  // a stand-in has every property; a row starts with none
      const b = stand();
      await satRingSetup(li, "ring", b);
      await Promise.all([satRingSetup(li, "ring-next", b), satRingSetup(li, "ring-next", b)]);
      const during = idle.size;
      await satRingSetup(li, "ring-cancel", b);
      console.log(JSON.stringify({ during, after: idle.size, most, lights }));
    """)
    assert got["during"] == 1 and got["after"] == 0, got
    assert got["most"] == 1, "two lights requests were out at once"
    assert got["lights"][-1] == "off" and got["lights"][-2] == 2, got


def test_the_ring_answers_are_saved_even_when_putting_the_ring_out_fails(tmp_path):
    """The ring was put out before the answers were saved, so a satellite
    that dropped, or Lights switched off meanwhile (409), threw both answers
    away. And Set up the ring pressed again, which says the panel is open,
    started again from the top rather than shutting it."""
    got = run(tmp_path, r"""
      const patches = [], lights = [];
      const real = json;
      json = async (path, options) => {
        const method = (options && options.method) || "GET";
        if (method === "POST" && path === "/satellites/aaaaaaaaaaaa/lights") {
          const b = JSON.parse(options.body);
          lights.push(b.mode);
          if (b.mode === "off") { const e = new Error("409 its lights are off"); e.status = 409; throw e; }
          return null;
        }
        if (method === "PATCH") { patches.push(JSON.parse(options.body)); return null; }
        return real(path, options);
      };
      await satellitesRefresh();
      const li = SATELLITES.rows.get("aaaaaaaaaaaa");
      li._ring = null;                                  // a stand-in has every property; a row starts with none
      const b = stand();
      await satRingSetup(li, "ring", b);
      await satRingSetup(li, "ring-next", b);
      await satRingSetup(li, "ring-top", b);
      await satRingSetup(li, "ring-ccw", b);
      await new Promise(r => setTimeout(r, 10));
      const saved = patches.slice();
      await satRingSetup(li, "ring", b);
      const open = !!li._ring;
      await satRingSetup(li, "ring", b);
      console.log(JSON.stringify({ saved, open, shut: li._ring === null, lights }));
    """)
    assert got["saved"] == [{"ring_top": 1, "ring_upside_down": True}], got
    assert got["open"] is True and got["shut"] is True, "a second press did not shut the set-up"
    assert got["lights"][-1] == "off", got


def test_what_lights_the_ring_says_why_it_cannot(tmp_path):
    """Lights off leaves the ring dark and a mute paints it red, over Blink
    and Show; a pending satellite reports both in its own status."""
    got = run(tmp_path, SAT + """
      const updating = { dataState: "updating" }, calm = { dataState: "online" };
      console.log(JSON.stringify({
        dark: satTryHint(sat({ config: { lights_enabled: false } }), calm),
        muted: satTryHint(sat({ status: { muted: true } }), calm),
        muted_deaf: satTryHint(sat({ status: { muted: true }, config: { mic_enabled: false } }), calm),
        updating: satTryHint(sat({}), updating),
      }));
    """)
    assert got["dark"] == "Turn Lights on to use Blink and Show.", got
    assert got["muted"] == "Muted on the device, so the ring shows red and Listen 5 s hears nothing.", got
    assert got["muted_deaf"] == "Turn Microphone on to use Listen 5 s. Muted on the device, so the ring shows red."
    assert got["updating"] == "Blink and Show wait until this update finishes.", got


def test_a_focused_slider_takes_the_satellites_own_change(tmp_path):
    """A click leaves the focus on a slider until the next click elsewhere,
    and the poll skipped a focused one: Vol+ pressed three times on the
    device left it at 6 of 12, and the next arrow key sent 7, turning the
    satellite down from 9. Only a drag, or a save still out, holds it."""
    got = run(tmp_path, SAT + """
      const slider = () => ({ dataset: { cfg: "volume", steps: "12" }, value: "6", setAttribute() {},
                              parentElement: { querySelector: () => stand() } });
      const focused = slider();
      document.activeElement = focused;
      satLevel(focused, 75);
      const dragged = slider();
      dragged.dataset.held = "drag";
      satLevel(dragged, 75);
      console.log(JSON.stringify({ focused: focused.value, dragged: dragged.value }));
    """)
    assert got == {"focused": 9, "dragged": "6"}, got


def test_an_older_image_is_never_offered_as_an_update(tmp_path):
    """The newest image was the last one uploaded. A known-good older build
    uploaded again after a newer one, or the only one left once the newer
    was deleted, was offered to every satellite on the newer build as
    "Update to", with Update every satellite beside it and a question that
    did not say it was a downgrade."""
    got = run(tmp_path, SAT + """
      const img = (version, at) => ({ model: "m", version, uploaded_at: at, sha256: version });
      const on = over => sat({ model: "m", firmware: "v0.3.1", ...over });
      const v = x => x ? x.version : null;
      SATELLITES.list = [on({ id: "a1" }), on({ id: "b2" })];
      SATELLITES.firmware = [img("v0.3.1", 1), img("v0.3.0", 2)];
      const again = { newest: v(satNewest("m")), offered: v(satImageFor(on({}))),
                      back: firmwareDue(SATELLITES.firmware[1]).length, summary: firmwareSummary() };
      SATELLITES.firmware = [img("v0.3.0", 1)];
      const deleted = { offered: v(satImageFor(on({}))), due: firmwareDue(SATELLITES.firmware[0]).length,
                        why: firmwareIdle(SATELLITES.firmware[0]), summary: firmwareSummary() };
      // A later build, with commits since its tag, is newer; labels that are
      // not stamped versions keep the upload order.
      SATELLITES.firmware = [img("v0.3.1-4-g1a2b3c4", 1), img("v0.3.1", 2)];
      const commits = v(satNewest("m"));
      SATELLITES.firmware = [img("nightly", 1), img("custom", 2)];
      const labels = v(satNewest("m"));
      console.log(JSON.stringify({ again, deleted, commits, labels }));
    """)
    assert got["again"] == {"newest": "v0.3.1", "offered": None, "back": 2,
                            "summary": "every satellite is up to date"}, got
    assert got["deleted"]["offered"] is None and got["deleted"]["due"] == 0, got
    assert got["deleted"]["why"] == "Every satellite of this model that is online runs it or a newer build."
    assert got["deleted"]["summary"] == "1 image", got
    assert got["commits"] == "v0.3.1-4-g1a2b3c4" and got["labels"] == "custom", got

