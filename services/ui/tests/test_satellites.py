"""The Satellites tab: a list of satellites, each with its own settings folded
under it, and the hub's wake words, each assigned to the satellites chosen.

Static, for the reason test_interface.py gives: what these assert is a property
of the bytes in ui.html -- which controls sit inside the fold, what a poll may
write, which copy of the wake words a PUT is built from -- and starting a
browser to find it out would add a dependency to a service whose whole claim is
that it has none. The same page was driven headless against a fake hub, with
fetch mocked and nothing on the network, when this was written.

The failures these prevent are the quiet ones:

  * a poll that rebuilds a row, which shuts the fold somebody opened and
    drops what they were typing, every three seconds;
  * a tick on one satellite that PUTs the card's half-finished edit along
    with it, or a poll that puts back an edit nobody saved;
  * unticking a word that every satellite had, and taking it off all of them;
  * a satellite's name, which it chooses itself in its hello and which anyone
    on the network can send, reaching innerHTML;
  * a control offered for something the hub refuses or the board swallows:
    a light pattern on a satellite whose lights are off, a threshold outside
    the hub's range.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PAGE = Path(__file__).resolve().parents[1] / "app" / "static" / "ui.html"
HTML = PAGE.read_text()
SCRIPT = HTML[HTML.index("<script>"):HTML.index("</script>", HTML.index("<script>"))]
HUB_WAKE_WORDS = REPO / "services" / "satellites" / "app" / "wakewords_config.py"


def bare(source: str) -> str:
    """The source without its comments, which quote what they forbid."""
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)


CODE = bare(SCRIPT)


def function(name: str) -> str:
    """One top-level function of the page, from its signature to the brace
    that closes it at column zero."""
    found = re.search(r"\n(?:async )?function " + re.escape(name) + r"\(", CODE)
    assert found, f"{name}() is gone from the page"
    return CODE[found.start():CODE.index("\n}\n", found.start()) + 2]


def listener(element_id: str) -> str:
    """The body of the click listener on one element."""
    start = CODE.index(f'$("{element_id}").addEventListener("click"')
    return CODE[start:CODE.index("\n});\n", start)]


def template(body: str, sink: str) -> str:
    """The template literal a function assigns to `sink`.innerHTML."""
    start = body.index(f"{sink}.innerHTML = `") + len(f"{sink}.innerHTML = `")
    return body[start:body.index("`;", start)]


ROW = template(function("satelliteCard"), "card")
WORD = template(function("wakeRow"), "row")


# ------------------------------------------------------------ the list --


def test_a_satellites_own_settings_are_folded_under_it_and_start_closed():
    """The user's words: the main page lists and manages the satellites, and
    what is specific to one satellite is a collapsible on that satellite.

    Everything that acts on or configures ONE satellite is inside its fold.
    Outside it is only what a list needs: Adopt with its name box, Blink, and
    Forget for a satellite that was never adopted and has no fold to use."""
    fold = ROW[ROW.index('<details class="satellite-settings">'):ROW.index("</details>")]
    assert "<summary>Settings</summary>" in fold
    assert '<details class="satellite-settings" open' not in ROW, "the fold starts open"
    for control in ('class="satellite-volume"', 'class="satellite-micgain"',
                    'class="satellite-mic"', 'class="satellite-spk"',
                    'class="satellite-lights"', 'class="satellite-volbuttons"',
                    'class="satellite-words"', 'class="satellite-buttons"',
                    'data-act="say"', 'data-act="tone"', 'data-act="listen"',
                    'data-act="stop"', 'data-act="lights"', 'data-act="rename"',
                    'data-act="reboot"', 'data-act="move"', 'data-act="forget"'):
        assert control in fold, f"{control} is not in the satellite's fold"
    listed = ROW[:ROW.index("<details")]
    assert set(re.findall(r'data-act="([a-z]+)"', listed)) == {"adopt", "identify", "dismiss"}, \
        "a satellite's own setting is back on the list row"


def test_a_poll_never_rebuilds_a_row_so_an_open_fold_stays_open():
    """The tab polls every 3 s. A row rebuilt by the poll is a new <details>,
    closed, with every box in it emptied: the fold would shut in the reader's
    face three seconds after they opened it."""
    render = function("satellitesRender")
    assert "if (!card) { card = satelliteCard(n.id);" in render, \
        "a row is built on every poll rather than once per satellite"
    update = function("satelliteUpdate")
    assert "innerHTML" not in update, "the poll rewrites a row's markup"
    assert ".open" not in update, "the poll decides whether a fold is open"
    # The one place the page opens a fold is the moment a satellite is
    # adopted, which is a press, not a poll.
    assert CODE.count(".open = true") == 1
    assert 'q(".satellite-settings").open = true;' in function("satelliteAct")


def test_a_poll_never_writes_into_a_control_somebody_is_holding():
    """A slider being dragged, or the Buttons box being typed in, must not be
    reset to the hub's value by a poll that lands mid-gesture. The guard is per
    control: the whole row used to be skipped while anything in it had focus,
    so one press of Blink froze every value in the row."""
    update = function("satelliteUpdate")
    assert "const idle = el => el !== document.activeElement;" in update
    for write in ("cfg.volume != null && idle(vol)", "cfg.mic_gain_db != null && idle(gain)",
                  "if (idle(q(sel))) q(sel).checked = on;",
                  'if (idle(q(".satellite-buttons")))'):
        assert write in update, f"an unguarded write: {write!r} is missing"
    assert "card.contains(document.activeElement)" not in update


def test_nothing_a_satellite_says_about_itself_reaches_innerhtml():
    """A satellite's name and id come from its own hello, and the socket is
    open to anyone on the network until adoption, so a pending satellite can
    call itself `<img src=x onerror=...>`. The row and the wake word row are
    built from markup with one hole, `u`, a counter the page owns; every name
    is written as text afterwards."""
    for name, markup in (("satelliteCard", ROW), ("wakeRow", WORD)):
        holes = set(re.findall(r"\$\{[^}]*\}", markup))
        assert holes == {"${u}"}, f"{name} interpolates {holes - {'${u}'}} into markup"
    for name in ("satelliteUpdate", "satelliteWords", "wakeRowUpdate", "wakeRender"):
        assert "innerHTML" not in function(name), f"{name} writes markup"


def test_every_label_on_a_row_names_its_control():
    """A label with no `for` is announced as nothing, and a slider under it as
    "slider, 60" and no name at all. The ids are per row, so two satellites
    never share one."""
    for markup in (ROW, WORD):
        for target in re.findall(r'<label for="([^"]+)"', markup):
            assert f'id="{target}"' in markup, f"<label for={target!r}> points at nothing"
    assert 'const u = "sat" + (++SATELLITES.seq);' in function("satelliteCard")
    assert 'const u = "wake" + (++SATELLITES.seq);' in function("wakeRow")


def test_a_dark_satellite_is_not_offered_a_light_pattern_and_a_silent_one_no_sound():
    """lights_enabled false means the hub sends no "lights" at all and answers
    409 to Set lights; speaker_enabled false means nothing audible. The page
    greys the control rather than offering what the hub refuses or the board
    swallows, and the Lights and Speaker ticks in the same fold are the
    reason beside it."""
    update = function("satelliteUpdate")
    assert "lit = cfg.lights_enabled !== false" in update
    assert 'act("lights").disabled = !n.online || !lit;' in update
    assert "const speaker = cfg.speaker_enabled !== false" in update
    assert 'act("say").disabled = act("tone").disabled = !n.online || !speaker;' in update


# ------------------------------------------------------- the wake words --


def test_a_tick_on_a_satellite_publishes_the_hubs_list_and_not_the_cards_draft():
    """A satellite's fold and the Wake words card edit the same assignment.
    A tick in the fold is one PUT built from the HUB'S copy; built from the
    card's, it would publish somebody's unsaved threshold with it. The same
    change goes into the card's draft, or its Save would undo the tick."""
    toggle = function("satelliteWordToggle")
    assert "wakeAssign(WAKE.server.words, name, id, on, known)" in toggle
    assert "wakeAssign(WAKE.draft" in toggle and "if (WAKE.draft) WAKE.draft = " in toggle
    assert toggle.index("await json(") < toggle.index("if (WAKE.draft)"), \
        "the draft takes the change before the hub has accepted it"
    # A refusal puts the tick back to what the hub still has.
    assert "tick.checked = !on;" in toggle


def test_unticking_a_word_every_satellite_has_keeps_it_on_the_others():
    """"*" means every satellite. Taking one satellite out of it cannot be
    done by removing "*", which would take the word off all of them: it
    becomes the list of every OTHER satellite the hub knows."""
    assign = function("wakeAssign")
    assert 'const every = w.satellites.includes("*");' in assign
    assert 'every ? (on ? ["*"] : known.filter(k => k !== id))' in assign
    # And ticking a satellite into an explicit list never adds "*" beside it,
    # which the hub refuses.
    assert 'if (on && !sats.includes("*")) sats.push(id);' in assign


def test_a_save_sends_what_put_takes_and_nothing_the_hub_reports():
    """GET answers each word with its state and error; PUT takes name,
    threshold and satellites. A body that carried the state back would be the
    hub's own report sent to it as an instruction."""
    copy = function("wakeCopy")
    assert "return { name: w.name, threshold: w.threshold, satellites: [...w.satellites] };" in copy
    save = listener("wakesave")
    assert "const words = WAKE.draft.map(wakeCopy);" in save
    assert "body: JSON.stringify({ words })" in save


def test_a_refused_save_keeps_the_edit_and_says_why_beside_the_card():
    """A 422 names the field. The edit it refused is what the reader needs in
    front of them to fix it, so the draft is dropped only after the hub has
    accepted it, and the reason is written in the card, where it announces."""
    save = listener("wakesave")
    assert save.index("await json(") < save.index("WAKE.draft = null;")
    assert 'note($("wakenote"), "bad", e.message);' in save
    card = HTML[HTML.index('id="wakecard"'):HTML.index('id="routecard"')]
    assert '<div id="wakenote" aria-live="polite"></div>' in card


def test_a_poll_cannot_put_back_an_edit_nobody_saved():
    """The poll replaces the hub's copy every three seconds, which is how a
    word's state moves from downloading to ready. The card shows the draft
    when there is one, so that refresh cannot reset a threshold mid-edit."""
    take = function("wakeTake")
    assert "WAKE.server = answer;" in take
    assert "WAKE.draft" not in take.replace("WAKE.server = WAKE.draft = null;", ""), \
        "a poll's answer touches the draft"
    assert "const words = WAKE.draft || WAKE.server.words;" in function("wakeRender")
    # And the slider being dragged is not written to.
    assert "if (range !== document.activeElement)" in function("wakeRowUpdate")


def test_removing_a_word_asks_first_because_it_stops_it_everywhere():
    save = listener("wakesave")
    assert "gone.length && !confirm(" in save
    assert save.index("confirm(") < save.index("await json(")


def test_the_add_list_offers_only_what_the_hub_can_load_and_is_not_listed():
    """A name the hub cannot load, or one already in the list, is a 422 the
    reader could not have avoided. The hub says which names it can load."""
    render = function("wakeRender")
    assert "(WAKE.server.available || []).filter(a => !keep.has(a))" in render
    assert 'pick.disabled = $("wakeaddgo").disabled = !left.length;' in render


def test_the_threshold_slider_offers_exactly_the_range_the_hub_accepts():
    """The hub refuses a threshold outside its range with a 422. A slider that
    reaches past it offers a value that can only be refused; one that stops
    short hides values that work. Read from the hub itself, so the two cannot
    drift apart."""
    hub = HUB_WAKE_WORDS.read_text()
    low = re.search(r"^MIN_THRESHOLD = ([\d.]+)$", hub, re.M).group(1)
    high = re.search(r"^MAX_THRESHOLD = ([\d.]+)$", hub, re.M).group(1)
    default = re.search(r"^DEFAULT_THRESHOLD = ([\d.]+)$", hub, re.M).group(1)
    assert f'min="{low}" max="{high}"' in WORD
    # A word added on the page starts where a name alone starts on the hub.
    added = listener("wakeaddgo")
    assert f"threshold: {default}, satellites: [\"*\"]" in added


def test_a_hub_without_wake_word_assignment_costs_the_card_and_not_the_tab():
    """An older hub answers /satellites/wake-words with 404 (it reads the path
    as a satellite id). Inside the same Promise.all as the satellite list, that
    would put "The satellite hub did not answer" over a hub that is fine."""
    refresh = function("satellitesRefresh")
    assert 'json("/satellites/wake-words").catch(e => e)' in refresh
    take = function("wakeTake")
    assert "if (answer.status === 404) { WAKE.server = WAKE.draft = null; return; }" in take
    render = function("wakeRender")
    assert '$("wakecard").hidden = !WAKE.server && !WAKE.failed;' in render
    assert "set.hidden = !WAKE.server;" in function("satelliteWords")


def test_every_wake_word_request_is_one_the_gateway_fence_can_read():
    """services/gateway/tests reads every api()/json() call out of this page
    and checks that voice-ui's PROXIED and the gateway both route it. It reads
    only a literal path and a literal method, so a PUT spelled any other way
    would slip past it as a GET and be a 405 in the browser."""
    assert CODE.count('json("/satellites/wake-words", { method: "PUT"') == 2
    assert CODE.count('json("/satellites/wake-words")') == 1


def test_a_wake_word_event_updates_the_card_and_is_not_logged_to_nobody():
    """The hub publishes the words when one finishes downloading. It belongs to
    no satellite, and logged as one it would print "undefined wake_words".

    The card takes each word's state from it and nothing else. The event
    travels on another connection than a save's answer and can arrive after
    it holding the assignment from before; taken whole, it became the copy
    the next tick was built from (test_satellites_writes.py drives that)."""
    handler = CODE[CODE.index("function satellitesListen()"):]
    handler = handler[:handler.index("\n}\n")]
    event = handler[handler.index('if (ev.type === "wake_words")'):]
    assert event.index("return;") < event.index("document.createElement"), \
        "the wake word event reaches the log"
    taken = event[:event.index("return;")]
    assert "state: states.get(w.name).state, error: states.get(w.name).error" in taken
    assert "words: ev.words" not in taken, "the event's assignment replaces the page's copy"


def test_moving_a_satellite_to_another_hub_asks_first():
    """set-hub reboots the satellite onto the other hub, which sees it as new:
    it is gone from this one until somebody moves it back by hand."""
    act = function("satelliteAct")
    move = act[act.index('act === "move"'):act.index('act === "forget"')]
    asks = "if (!confirm(`Move ${name} to ${url}?"
    assert asks in move, "the answer to the question no longer stops the move"
    assert move.index(asks) < move.index("await json(`/satellites/${id}/set-hub`")


def test_the_tab_still_has_its_routing_firmware_and_events_cards_in_order():
    """The list first, then the wake words (routing rules are keyed by them),
    then the three cards the tab already had."""
    order = [HTML.index(f'id="{card}"') for card in
             ("satellitelist", "wakecard", "routecard", "fwcard", "evcard")]
    assert order == sorted(order)
