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
  * a button mapping sent that the hub refuses, or one that maps Rec.
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
    assert got["muted"]["kind"] == got["mic_off"]["kind"] == "neutral"
    assert got["pending"]["word"] == "New" and got["pending"]["kind"] == "warn"
    assert got["pending"]["line"] == "Waiting to be adopted · ID aaaaaaaaaaaa"
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
    still coming. With no word, the satellite still answers its PLAY button.
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
    or no scheme, and refuses anything on Rec. "none" is the default and is
    left out."""
    got = run(tmp_path, SAT + """
      const e = (button, edge, action, url) => ({ button, edge, action, url: url || "" });
      console.log(JSON.stringify({
        good: satMapping([e("play", "press", "ptt"), e("play", "release", "none"),
                          e("set", "press", "stop"), e("mode", "press", "webhook", "https://ha.local/hook"),
                          e("rec", "press", "ptt")]),
        userinfo: satMapping([e("mode", "press", "webhook", "https://me:pw@ha.local/hook")]),
        scheme: satMapping([e("mode", "press", "webhook", "ha.local/hook")]),
        empty: satMapping([e("mode", "press", "webhook", "")]),
        note: satButtonsNote({ play: { press: "ptt" }, set: { press: "stop" },
                               mode: { press: "webhook:https://x.local" } }, ["play", "set", "mode"]),
        note_none: satButtonsNote({}, ["play", "set"]),
        keys_local: satButtonKeys(sat({ caps: { buttons: ["vol_up", "vol_down", "set", "play", "mode", "rec"] },
                                        config: { local_volume_buttons: true } })),
        keys_free: satButtonKeys(sat({ caps: { buttons: ["vol_up", "vol_down", "set", "play", "mode", "rec"] },
                                       config: { local_volume_buttons: false } })),
        keys_offline: satButtonKeys(sat({ caps: {}, config: { buttons: { custom: { press: "ptt" } } } })) }));
    """)
    assert got["good"] == {"mapping": {"play": {"press": "ptt"}, "set": {"press": "stop"},
                                       "mode": {"press": "webhook:https://ha.local/hook"}}}, got
    for refused in ("userinfo", "scheme", "empty"):
        assert "error" in got[refused], (refused, got[refused])
    assert got["note"] == "Play talks, Set stops, Mode calls a webhook", got
    assert got["note_none"] == "nothing mapped", got
    assert got["keys_local"] == ["play", "set", "mode"], got
    assert got["keys_free"] == ["play", "set", "mode", "vol_up", "vol_down"], got
    assert got["keys_offline"] == ["play", "set", "mode", "custom"], got


def test_the_health_line_counts_faults_and_not_choices(tmp_path):
    got = run(tmp_path, SAT + """
      SATELLITES.list = [sat({ id: "a1", online: false }), sat({ id: "a2", online: false }),
                         sat({ id: "a3", ota: { state: "failed", version: "v1" } }),
                         sat({ id: "a4", status: { muted: true } }),
                         sat({ id: "a5", config: { mic_enabled: false } }),
                         sat({ id: "a6", adopted: false, online: false })];
      WAKE.server = { words: [{ name: "alexa", satellites: ["*"], state: "error" }] };
      SATELLITES.routing = { load_error: "rules.json: bad" };
      satellitesHealth();
      const busy = $("sathealth").textContent;
      SATELLITES.list = [sat({})]; WAKE.server = { words: [] }; SATELLITES.routing = null;
      satellitesHealth();
      console.log(JSON.stringify({ busy, calm: $("sathealth").textContent }));
    """)
    assert got["busy"] == ("2 offline · 1 update failed · alexa failed to download · "
                           "routing did not load"), got
    assert got["calm"] == "", "the health line says something when all is well"
