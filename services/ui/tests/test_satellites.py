"""The Satellites tab: one card, one list of satellites, each satellite's own
settings folded under its row, and the hub's settings (wake words, activity,
firmware) as three quiet disclosures at the foot. Routing was a fourth until
the hub moved it onto each wake word (2026-09-25).

The owner's words were "the main page should list and manage the satellites,
the satellite specific settings should be a collapsible on the satellite",
"wake words should be multiple and you choose the satellite that will have
that wake words", and "settings like firmware etc dont need to be present on
the main menu, can be hidden under sub menus so its less overwhelming".

Static, for the reason test_interface.py gives: what these assert is a property
of the bytes in ui.html -- which controls sit in which disclosure, what a poll
may write, which copy of the wake words a PUT is built from. What needs the
page to run (ordering against a hub, the state table) is in
test_satellites_writes.py, test_satellites_ordering.py and
test_satellites_states.py. The page was also driven headless against a fake
hub, with fetch mocked and nothing on the network, when this was written.

The failures these prevent are the quiet ones:

  * a poll that rebuilds a row, which shuts the disclosure somebody opened
    and drops what they were typing, every three seconds;
  * firmware, addresses and reboots creeping back onto the list;
  * a satellite's name, which it chooses itself in its hello and which anyone
    on the network can send, reaching innerHTML;
  * a control offered for something the hub refuses or the board swallows,
    or one that vanishes instead of greying with its reason beside it;
  * generated copy that no hint fence can see growing into paragraphs.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PAGE = Path(__file__).resolve().parents[1] / "app" / "static" / "ui.html"
HTML = PAGE.read_text()
CSS = HTML[HTML.index("<style>"):HTML.index("</style>")]
SCRIPT = HTML[HTML.index("<script>"):HTML.index("</script>", HTML.index("<script>"))]
HUB_WAKE_WORDS = REPO / "services" / "satellites" / "app" / "wakewords_config.py"


def bare(source: str) -> str:
    """The source without its comments, which quote what they forbid. A colon
    before // is a URL in a string (https://, wss://), not a comment."""
    return re.sub(r"/\*.*?\*/|(?<!:)//[^\n]*", "", source, flags=re.S)


CODE = bare(SCRIPT)
BARE_CSS = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
PANEL = re.sub(r"<!--.*?-->", "", HTML[HTML.index('<section id="tab-satellites"'):
                                       HTML.index("</section>", HTML.index('<section id="tab-satellites"'))],
               flags=re.S)


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


def returned(name: str) -> str:
    """The template literal a markup function returns."""
    body = function(name)
    start = body.index("return `") + len("return `")
    return body[start:body.index("`;", start)]


def between(markup: str, start: str, end: str) -> str:
    at = markup.index(start)
    return markup[at:markup.index(end, at)]


ADOPTED = returned("satAdoptedMarkup")
PENDING = returned("satPendingMarkup")
WORD = returned("wakeRowMarkup")
SUMMARY = between(ADOPTED, "<summary>", "</summary>")
BODY = ADOPTED[ADOPTED.index("</summary>"):]
TRY = between(ADOPTED, '<details class="sub sat-try">', "</details>")
BUTTONS = between(ADOPTED, '<details class="sub sat-buttons">', "</details>")
DEVICE = between(ADOPTED, '<details class="sub sat-device">', "</details>")
OPEN_ROW = BODY[:BODY.index('<details class="sub')]


# ------------------------------------------------------------- one card --


def test_the_satellites_tab_is_one_card():
    """It was five cards at one weight, which read as a settings dump. The
    list and the hub's disclosures are one card now, the hub sections at the
    foot of it, and the owner's "firmware etc." last of all. Routing is not
    one of them any more: a wake word says what it does, and the hub answers
    PUT /satellites/routing 409."""
    assert PANEL.count('class="card"') == 1, "the tab is a stack of cards again"
    order = [PANEL.index(f'id="{i}"') for i in ("satellitelist", "sat-wakewords", "sat-activity",
                                                  "sat-firmware")]
    assert order == sorted(order), "the list is not first, or Firmware is not last"
    hub = PANEL[PANEL.index('<div class="sat-hub"'):]
    assert re.findall(r'<details id="(sat-[a-z]+)"', hub) == [
        "sat-wakewords", "sat-activity", "sat-firmware"], "the hub's disclosures changed"
    # Everything the hub answers is inside #satellitesman, so a deployment with
    # no hub shows one sentence and no empty headings.
    man = PANEL[PANEL.index('id="satellitesman"'):]
    assert 'id="sat-firmware"' in man and 'id="satellitelist"' in man


def test_the_satellites_panel_has_no_tab_strip_of_its_own():
    """The dock finds its tabs with a document-wide [role=tab] sweep (TABS,
    DOCKTABS), so a sub-navigation in this panel would join the dock. And no
    exclusive accordion: two rows, or Wake words and Activity, open at once
    is a real task, and closing the row above moves the new one away from the
    pointer."""
    for markup in (PANEL, ADOPTED, PENDING, WORD):
        assert 'role="tab"' not in markup and 'role="tablist"' not in markup
        assert "<details name=" not in markup, "a disclosure here closes its neighbours"


def test_a_satellite_row_is_a_hairline_not_a_box():
    """Every satellite was a bordered box inside the bordered card: the
    nesting the owner rejected on the Jobs tab. Rows are divided by hairlines,
    and the old box rule is gone."""
    assert ".satellite{" not in BARE_CSS and ".satellite " not in BARE_CSS
    rows = BARE_CSS[BARE_CSS.index(".sats>li,.wws>li,.fws>li{"):]
    assert "border-top:1px solid var(--line)" in rows[:rows.index("}")]
    assert "border-radius" not in rows[:rows.index("}")]


def test_contrast_mode_does_not_box_a_satellite_row():
    """prefers-contrast:more boxes every <details> on the page, which here
    would draw a box per satellite and a box per sub-disclosure inside it.
    The rows keep their hairlines; the hub's disclosures stay boxed, as the
    Expert panels are."""
    block = BARE_CSS[BARE_CSS.index("@media (prefers-contrast:more){"):]
    block = block[:block.index("\n}")]
    assert "details.sat-row{border:0;border-radius:0;padding:0;margin-inline:0}" in block
    assert "details.sub{border:0;border-top:1px solid var(--line)" in block


def test_a_summary_is_a_thumb_high_on_a_phone():
    block = CSS[CSS.index("@media (pointer:coarse)"):]
    block = block[:block.index("\n}\n")]
    assert "#tab-satellites details>summary{min-height:44px}" in block


# ------------------------------------------------------------ the rows --


def test_a_satellites_own_settings_are_folded_under_it_and_start_closed():
    """What people change weekly is one level down (levels, speaker,
    microphone, lights); what makes a noise is in Try it; the mapping in
    Buttons; and the device's facts and every consequential action in
    Device, two levels down."""
    assert '<details class="sat-row">' in ADOPTED, "the row starts open, or is not a disclosure"
    for control in ('data-cfg="volume"', 'data-cfg="mic_gain_db"', 'data-cfg="speaker_enabled"',
                    'data-cfg="mic_enabled"', 'data-cfg="lights_enabled"', 'data-act="wakewords"'):
        assert control in OPEN_ROW, f"{control} is not on the open row"
    for control in ('class="row sat-sayform"', 'data-act="identify"', 'data-act="tone"',
                    'data-act="listen"', 'data-act="stop"', 'data-act="lights"', "<audio"):
        assert control in TRY, f"{control} is not in Try it"
    for control in ('data-cfg="local_volume_buttons"', 'class="sat-btns"'):
        assert control in BUTTONS, f"{control} is not in Buttons"
    for control in ('<dl class="facts">', 'data-act="update"', 'class="row sat-rename"',
                    'data-act="reboot"', 'data-act="move"', 'data-act="forget"',
                    'class="row sat-moveform"'):
        assert control in DEVICE, f"{control} is not in Device"


def test_the_satellite_summary_carries_no_device_facts_and_nothing_pressable():
    """Closed, a row is a name, one state word and one line. Firmware, the
    address and the signal are the "firmware etc." the owner asked to have off
    the main view; and a summary is a button, so nothing pressable may sit
    inside it."""
    for fact in ("firmware", "address", "rssi", "facts", "dBm"):
        assert fact not in SUMMARY, f"{fact} is on the closed row"
    for control in ("<button", "<input", "<select", "<a "):
        assert control not in SUMMARY, f"{control} is inside a <summary>"
    for fact in ("firmware", "rssi", "address", "connected_at"):
        assert fact not in OPEN_ROW, f"{fact} is on the open row rather than in Device"
    device = function("satDevice")
    assert '["Firmware", fw]' in device and '["Address", n.address]' in device


def test_a_poll_never_rebuilds_a_row_so_an_open_disclosure_stays_open():
    """The tab polls every 3 s. A row rebuilt by the poll is a new <details>,
    closed, with every box in it emptied. A row's markup is written when it
    is created and when it is adopted or forgotten, and at no other time."""
    render = function("satellitesRender")
    assert "if (!li) {" in render and "li = satelliteRow(n);" in render
    assert '(li.dataset.adopted === "1") !== !!n.adopted' in render
    assert render.count("satelliteBuild(") == 1
    for name in ("satelliteUpdate", "satNeeds", "satButtons", "satDevice"):
        body = function(name)
        assert ".open" not in body, f"{name} decides whether a disclosure is open"
        assert "innerHTML" not in body, f"{name} rewrites markup"
    # The four places the page opens a disclosure: restoring a row the viewer
    # left open (or the only satellite), the moment a satellite is adopted,
    # Change wake words, and a wake word just added. Each is a press or a
    # first render; the word list is kept across polls like the satellites.
    assert CODE.count(".open = true") == 4
    assert 'li.querySelector("details.sat-row").open = true;' in render
    assert "row.open = true;" in function("satelliteAct")
    assert "box.open = true;" in function("satGoWakeWords")
    assert 'row.querySelector("details.sat-row").open = true;' in listener("wwaddgo")
    for name in ("wakeRender", "wakeRowUpdate", "wakeActionUpdate"):
        assert ".open" not in function(name), f"{name} decides whether a disclosure is open"
    assert "if (!row) { row = wakeRow(w.name); WAKE.rows.set(w.name, row); }" in function("wakeRender")


def test_open_rows_are_remembered_per_viewer():
    row = function("satelliteRow")
    assert 'addEventListener("toggle"' in row and "}, true);" in row, \
        "toggle does not bubble; without capture the listener never fires"
    assert 'store.set("sat.open"' in row
    assert 'store.get("sat.open", null)' in function("satellitesRender")


def test_a_poll_never_writes_into_a_control_somebody_is_holding():
    """A slider being dragged, a box being typed in, a select being chosen
    from: the poll leaves each alone. The guard is per control; the whole
    row used to be skipped while anything in it had focus, so one press of
    Blink froze every value in the row."""
    assert "input === document.activeElement) return;" in function("satLevel")
    update = function("satelliteUpdate")
    assert "const idle = el => el !== document.activeElement;" in update
    assert "if (idle(box)) box.checked" in update
    device = function("satDevice")
    assert "field !== document.activeElement && !field.dataset.edited" in device
    buttons = function("satButtons")
    assert "!pick.dataset.pending && pick !== document.activeElement" in buttons
    assert "!grid.contains(document.activeElement)" in buttons


def test_a_value_set_in_code_repaints_its_slider():
    """paintRange paints the filled half, and a value set without an input
    event leaves the fill where it was: every satellite's sliders were drawn
    half full whatever their value."""
    assert "paintRange(input);" in function("satLevel")
    assert "paintRange(range);" in function("wakeRowUpdate")


def test_nothing_a_satellite_says_about_itself_reaches_innerhtml():
    """A satellite's name and id come from its own hello, and the socket is
    open to anyone on the network until adoption, so a pending satellite can
    call itself `<img src=x onerror=...>`. Every row is built from markup with
    one hole, `u`, a counter the page owns; every name is written as text."""
    for name, markup in (("satAdoptedMarkup", ADOPTED), ("satPendingMarkup", PENDING),
                         ("wakeRowMarkup", WORD)):
        holes = set(re.findall(r"\$\{[^}]*\}", markup))
        assert holes == {"${u}"}, f"{name} interpolates {holes - {'${u}'}} into markup"
    firmware = function("firmwareRow")
    fw = firmware[firmware.index("row.innerHTML = `"):]
    assert "${" not in fw[:fw.index("`;")], "firmwareRow interpolates into markup"
    for name in ("satelliteUpdate", "satButtons", "satDevice", "wakeRowUpdate", "wakeRender",
                 "wakeActionUpdate", "satellitesHealth", "wakeTryWords", "satFillSelect"):
        assert "innerHTML" not in function(name), f"{name} writes markup"
    # A custom model's name is the hub's, and anyone who can upload chooses
    # it: its row is markup with no hole at all, and the name goes in as text.
    models = function("wakeModels")
    literal = models[models.index("row.innerHTML = `"):]
    assert "${" not in literal[:literal.index("`;")], "wakeModels interpolates into markup"
    assert 'row.querySelector(".sat-name").textContent = wwLabel(name);' in models
    assert 'const u = "sat" + (++SATELLITES.seq);' in function("satelliteBuild")
    assert 'const u = "wake" + (++SATELLITES.seq);' in function("wakeRow")


def test_every_label_on_a_row_names_its_control():
    """A label with no `for` is announced as nothing, and a slider under it as
    "slider, 60" and no name at all."""
    for markup in (ADOPTED, PENDING, WORD):
        targets = re.findall(r'<label for="([^"]+)"', markup)
        assert targets
        for target in targets:
            assert f'id="{target}"' in markup, f"<label for={target!r}> points at nothing"


def test_every_satellite_slider_says_what_it_is():
    assert '<label for="${u}-vol">Volume</label>' in ADOPTED
    assert '<label for="${u}-gain">Mic gain</label>' in ADOPTED
    assert '<label for="${u}-t">Threshold</label>' in WORD
    # And its value in words, which "60" alone is not.
    assert 'input.setAttribute("aria-valuetext"' in function("satLevelSay")


def test_what_needs_the_satellite_is_greyed_with_its_reason_and_never_hidden():
    """The standing rule: a control the satellite cannot take now is greyed,
    and the reason sits beside it. data-needs names the conditions; the hub
    refuses Show with the lights off and Listen while muted (409), and a
    silent satellite is sent no sound."""
    needs = dict(re.findall(r'data-act="(\w+)" data-needs="([^"]+)"', ADOPTED))
    assert needs["lights"] == "online lights"
    assert needs["tone"] == "online speaker"
    assert needs["listen"] == "online mic unmuted"
    assert needs["reboot"] == needs["move"] == needs["update"] == "online idle"
    assert 'type="submit" data-needs="online speaker">Say' in ADOPTED
    body = function("satNeeds")
    assert "lights: cfg.lights_enabled !== false" in body and "unmuted: !st.muted" in body
    assert "b.disabled = " in body and ".hidden" not in body
    # Settings are not in data-needs at all: offline, they are saved and sent
    # when the satellite reconnects.
    for setting in ("volume", "mic_gain_db", "speaker_enabled", "mic_enabled", "lights_enabled"):
        at = ADOPTED.index(f'data-cfg="{setting}"')
        tag = ADOPTED[ADOPTED.rindex("<", 0, at):ADOPTED.index(">", at)]
        assert "data-needs" not in tag, f"{setting} is greyed offline"
    for reason in ('q(".sat-tryhint")', 'q(".sat-why")'):
        assert reason in function("satelliteUpdate")
    assert 'li.querySelector(".sat-devhint")' in function("satDevice")
    # Forget on a seen satellite and the Move row are modes, not greyed controls.
    assert "Forget</button>" in PENDING


def test_the_one_filled_button_on_the_list_is_adopt():
    """Settings apply on change, so an adopted row has no go button; the
    destructive ones are outlined and last."""
    assert 'class="primary small tight" type="submit" data-needs="online">Adopt' in PENDING
    assert 'class="primary' not in ADOPTED
    assert 'class="small danger end" type="button" data-act="forget">Forget' in DEVICE
    assert DEVICE.index('data-act="forget"') > DEVICE.index('data-act="move"')


def test_every_consequential_action_asks_first_and_nothing_prompts():
    """Reboot, Forget, Move, Update, Update every satellite, Roll back every
    satellite and Delete each ask, and the question comes before anything is
    greyed or sent. Rename is an inline field now, so the page has no
    prompt() at all. (Roll back is new: Update every satellite used to sit on
    every image, older ones included, and downgraded the house without
    saying so.)"""
    act = function("satelliteAct")
    ask = act.index("if (ask && !confirm(ask)) return;")
    for key in ("askReboot", "askForget", "askMove", "askUpdate"):
        assert f'satText("{key}"' in act[:ask], f"{key} is not asked"
    assert ask < act.index("busy(button") < act.index("await json(`/satellites/${id}/set-hub`")
    fw = function("firmwareAct")
    assert ('const ask = act === "all" ? "askUpdateAll" : act === "back" ? "askRollback" : "askDelete";'
            in fw)
    assert "if (!confirm(satText(ask, { v: fw.version }))) return;" in fw
    assert fw.index("confirm(") < fw.index("await json(")
    assert "prompt(" not in CODE


def test_the_move_address_is_checked_as_the_hub_checks_it():
    """HubBody takes ^wss?://[^/\\s]+$ and up to 120 characters; a webhook,
    the hub's ButtonAction pattern. Anything else would be a 422 the page
    could have prevented."""
    hub = (REPO / "services" / "satellites" / "app" / "main.py").read_text()
    assert 'pattern=r"^wss?://[^/\\s]+$", max_length=120' in hub
    assert "/^wss?:\\/\\/[^/\\s]+$/.test(url) || url.length > 120" in function("satelliteAct")
    assert "webhook:https?://[^\\s/?#@]+(/\\S*)?" in hub
    assert "const SAT_WEBHOOK = /^https?:\\/\\/[^\\s/?#@]+(\\/\\S*)?$/;" in CODE


def test_a_forgotten_row_hands_focus_on():
    body = function("satForgotten")
    assert "rows[at + 1] || rows[at - 1]" in body
    assert '(target || $("sat-h")).focus();' in body
    assert 'id="sat-h" tabindex="-1"' in PANEL


def test_the_update_bar_moves_a_transform_and_is_hidden_from_a_reader():
    """The chip already says the percentage; the bar is for eyes."""
    assert '<span class="bar-track" aria-hidden="true" hidden>' in SUMMARY
    assert '.style.transform = `scaleX(' in function("satelliteUpdate")
    assert "aria-live" not in SUMMARY, "a ticking figure is in a live region"


def test_the_health_line_announces_once_and_only_when_it_changes():
    assert '<span class="hint tight" id="sathealth" aria-live="polite"></span>' in PANEL
    assert 'satSet($("sathealth"), parts.join(" · "));' in function("satellitesHealth")
    assert "if (el.textContent !== text) el.textContent = text;" in function("satSet")
    # And a standing failure is written once, not on every poll.
    assert 'if (host.dataset.said === said) return;' in function("satNoteOnce")


# -------------------------------------------------------- generated copy --


SAMPLES = {"name": "Kitchen", "old": "Kitchen", "new": "Bedroom", "id": "a1b2c3d4e5f6",
           "v": "v0.3.1", "fw": "v0.3.1", "clock": "24 Sep, 14:02", "time": "12:04:31",
           "error": "bad signature", "why": "offline or not adopted", "url": "wss://hub.local:8443",
           "what": "heard hey jarvis (0.82) from 40°", "words": "hey jarvis, alexa",
           "list": "Speaker, Microphone and Lights", "message": "503 Service Unavailable",
           "n": "3 satellites", "k": "2 warnings", "score": "0.82", "b": "Vol −",
           "how": "strong", "word": "hey mycroft", "var": "SATELLITES_HA_TOKEN_KITCHEN",
           "s": "1.3", "output": "Headphones"}


def sat_copy() -> dict[str, str]:
    block = SCRIPT[SCRIPT.index("const SAT_COPY = {"):]
    block = block[:block.index("\n};\n")]
    pairs = re.findall(r'^\s+(\w+): "((?:[^"\\]|\\.)*)",?$', block, re.M)
    assert len(pairs) > 60, "the SAT_COPY scan stopped finding strings"
    return {k: re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), v)
            for k, v in pairs}


def test_every_generated_satellite_string_stays_one_sentence():
    """The page's hint fence measures the markup and cannot see a row built
    in script, so this is the same fence for SAT_COPY: at most 100
    characters with every hole filled by a realistic value, and no direction
    that is only true on the writer's screen."""
    copy = sat_copy()
    for key, text in copy.items():
        filled = re.sub(r"\{(\w+)\}", lambda m: SAMPLES[m.group(1)], text)
        assert len(filled) <= 100, f"{key} is {len(filled)} characters: {filled!r}"
        for direction in (r"\babove\b", r"\bbelow\b", r"[Cc]lick here"):
            assert not re.search(direction, filled), f"{key} points: {filled!r}"
        assert "—" not in filled, f"{key} carries an em dash"
    # Every string is used, so none is a stale promise.
    for key in copy:
        assert (f'"{key}"' in CODE or f"SAT_COPY.{key}" in CODE), f"SAT_COPY.{key} is never used"


def test_the_hints_inside_the_row_templates_stay_one_sentence():
    """The static hints the templates carry are invisible to the markup scan
    too."""
    hints = [re.sub(r"\s+", " ", h).strip() for markup in (ADOPTED, PENDING, WORD)
             for h in re.findall(r'<div class="hint">([^<]+)</div>', markup)]
    assert len(hints) >= 3
    for hint in hints:
        assert len(hint) <= 100, hint


# ------------------------------------------------------- the wake words --


def test_the_wake_word_is_the_one_editor_of_its_satellites():
    """"You choose the satellite that will have that wake word": the choice is
    made on the word. A satellite's row only says what it listens for, read
    from the saved set, with a link to the one editor."""
    assert "data-word" not in HTML and "satelliteWordToggle" not in CODE, \
        "a second editor of the assignment is back on the satellite rows"
    assert '<button class="link" type="button" data-act="wakewords">Change wake words</button>' in OPEN_ROW
    assert 'link.setAttribute("aria-label", `Change wake words for ${name}`);' in function("satelliteUpdate")
    assert 'data-scope="all">Every satellite' in WORD and 'data-scope="chosen">Chosen' in WORD


def test_satellite_rows_read_the_saved_words_and_never_the_draft():
    for name in ("satListens", "satMem", "satState"):
        assert "WAKE.draft" not in function(name), f"{name} reads an unsaved edit"
    assert "words: WAKE.server ? WAKE.server.words : null" in function("satMem")


def test_change_wake_words_opens_the_editor_and_moves_focus_to_it():
    body = function("satGoWakeWords")
    assert 'MENISCUS.calm.matches ? "auto" : "smooth"' in body, \
        "the scroll ignores reduced motion"
    assert '.focus({ preventScroll: true })' in body


def test_a_save_sends_what_put_takes_and_nothing_the_hub_reports():
    """GET answers each word with its state and error; PUT takes the entry.
    A body that carried the state back would be the hub's own report sent to
    it as an instruction. (Which fields PUT takes is read from the hub itself
    in test_wake_words_contract.py.) A trigger sends no action: it has none."""
    copy = function("wakeCopy")
    body = copy[copy.index("return {"):]
    assert "state" not in body and "error" not in body
    assert 'action: w.mode === "trigger" ? undefined : w.action' in body
    assert "wakePut(wakeEffective().map(wakeCopy), ptt)" in function("wakeSave")
    assert "body: JSON.stringify(ptt ? { words, ptt } : { words })" in function("wakePut")
    # Push-to-talk is sent only when it changed; left out, the hub keeps it.
    assert "wakeKey([], WAKE.draftPtt) !== wakeKey([], WAKE.server.ptt)" in function("wakeSave")


def test_a_refused_save_keeps_the_edit_and_says_why_beside_it():
    save = function("wakeSave")
    assert save.index("await wakePut(") < save.index("WAKE.draft = WAKE.draftPtt = null;")
    assert 'note($("wwnote"), "bad", e.message);' in save
    assert '<div id="wwnote" aria-live="polite"></div>' in PANEL


def test_a_poll_cannot_put_back_an_edit_nobody_saved():
    """The poll replaces the hub's copy every three seconds, which is how a
    word's state moves from downloading to ready. The list shows the draft
    when there is one, so that refresh cannot reset a threshold mid-edit."""
    take = function("wakeTake")
    assert "WAKE.server = answer;" in take
    assert "WAKE.draft" not in take.replace("WAKE.server = WAKE.draft = null;", ""), \
        "a poll's answer touches the draft"
    assert "const words = WAKE.draft || WAKE.server.words;" in function("wakeRender")
    assert "if (range !== document.activeElement)" in function("wakeRowUpdate")


def test_removing_a_word_is_staged_and_can_be_kept():
    """THIS REPLACES "removing a word asks first". Remove used to take effect
    on Save behind a confirm(); it is staged now, the row says so in words
    and in a line through the name, and the same button says Keep and takes
    it back. Nothing leaves the hub until Save, so nothing needs asking."""
    remove = function("wakeRemove")
    assert "WAKE.removed.delete(name); else WAKE.removed.add(name);" in remove
    assert "confirm(" not in remove and "confirm(" not in function("wakeSave")
    update = function("wakeRowUpdate")
    assert 'satSet(rm, removed ? "Keep" : "Remove");' in update
    assert "SAT_COPY.wwRemoved" in update
    assert 'rm.classList.toggle("danger", !removed);' in update, "Keep is drawn as a destructive button"
    assert "button.focus();" in remove, "focus is lost when Remove becomes Keep"


def test_save_is_off_with_its_reason_when_there_is_nothing_to_save():
    """Off when nothing changed, when a word is left with nobody to hear it,
    and when an entry would be refused: the hub's 422, said before the press,
    with the word named beside Save and the reason on the word's own row."""
    render = function("wakeRender")
    assert "const nobody = dirty && wakeEffective().some(w => !w.satellites.length);" in render
    assert "const fix = dirty ? wakeFirstProblem() : \"\";" in render
    assert '$("wwsave").disabled = !dirty || nobody || !!fix;' in render
    assert ('nobody ? SAT_COPY.wwNone : fix ? satText("wwFix", { word: fix })\n'
            "    : dirty ? SAT_COPY.wwDirty : SAT_COPY.wwClean") in render
    assert 'satNoteOnce(q(".ww-fix"), "warn", problem);' in function("wakeRowUpdate")
    assert "if (wakeFirstProblem()) { wakeRender(); return; }" in function("wakeSave")


def test_the_add_list_offers_only_what_the_hub_can_load_and_is_not_listed():
    render = function("wakeRender")
    assert "(WAKE.server.available || []).filter(a => !keep.has(a))" in render
    assert 'pick.disabled = $("wwaddgo").disabled = !left.length;' in render
    assert "left.length ? SAT_COPY.wwAddWhy : SAT_COPY.wwExhausted" in render


def test_the_threshold_slider_offers_exactly_the_range_the_hub_accepts():
    """Read from the hub itself, so the two cannot drift apart: the range, and
    where a new word starts, which is higher for a trigger."""
    hub = HUB_WAKE_WORDS.read_text()
    low = re.search(r"^MIN_THRESHOLD = ([\d.]+)$", hub, re.M).group(1)
    high = re.search(r"^MAX_THRESHOLD = ([\d.]+)$", hub, re.M).group(1)
    default = re.search(r"^DEFAULT_THRESHOLD = ([\d.]+)$", hub, re.M).group(1)
    trigger = re.search(r"^DEFAULT_TRIGGER_THRESHOLD = ([\d.]+)$", hub, re.M).group(1)
    assert f'min="{low}" max="{high}"' in WORD
    assert f"const WAKE_THRESHOLD = {{ usual: {default}, trigger: {trigger} }};" in CODE
    assert 'threshold: WAKE_THRESHOLD.usual, satellites: ["*"], mode: "command"' in function("wakeAdd")
    assert "wakeAdd(name);" in listener("wwaddgo")


def test_a_hub_without_wake_word_assignment_costs_the_section_and_not_the_tab():
    """An older hub answers /satellites/wake-words with 404. allSettled, so
    that costs Wake words and the Change wake words links, and a firmware
    list that fails costs Firmware; only the satellite list failing is "the
    hub did not answer"."""
    refresh = function("satellitesRefresh")
    assert "await Promise.allSettled([" in refresh
    assert 'if (n.status === "rejected")' in refresh
    take = function("wakeTake")
    assert "if (answer.status === 404) { WAKE.server = WAKE.draft = null;" in take
    assert '$("sat-wakewords").hidden = !WAKE.server && !WAKE.failed;' in function("wakeRender")
    assert "link.parentElement.hidden = !WAKE.server && !WAKE.failed;" in function("satelliteUpdate")


def test_every_wake_word_request_is_one_the_gateway_fence_can_read():
    """services/gateway/tests reads every api()/json() call out of this page
    and checks that voice-ui's PROXIED and the gateway both route it. It reads
    only a literal path and a literal method."""
    assert CODE.count('json("/satellites/wake-words", { method: "PUT"') == 1
    assert CODE.count('json("/satellites/wake-words")') == 1


def test_a_wake_word_event_updates_the_list_and_is_not_logged_to_nobody():
    """The hub publishes the words when one finishes downloading. It belongs to
    no satellite, and logged as one it would print "undefined wake_words".
    The list takes each word's state from it and nothing else
    (test_satellites_ordering.py drives why)."""
    handler = CODE[CODE.index("function satellitesListen()"):]
    handler = handler[:handler.index("\n}\n")]
    event = handler[handler.index('if (ev.type === "wake_words")'):]
    assert event.index("return;") < event.index("document.createElement"), \
        "the wake word event reaches the log"
    taken = event[:event.index("return;")]
    assert "state: states.get(w.name).state, error: states.get(w.name).error" in taken
    assert "words: ev.words" not in taken, "the event's assignment replaces the page's copy"


def test_the_stream_remembers_what_the_hub_does_not():
    """A wake word lights Listening at once (and a routed reply clears it), a
    rebooting update is remembered so it reads Restarting, and a test clip
    (injected) lights nothing."""
    handler = function("satellitesListen")
    assert 'if (ev.type === "wake" && !ev.injected)' in handler
    assert "if (SAT_WOKE_ENDS.includes(ev.type)) SATELLITES.woke.delete(ev.satellite);" in handler
    assert ('const SAT_WOKE_ENDS = ["routed", "turn", "conversation_ended", "triggered"];'
            in CODE), "a reply that is a turn, or a trigger, leaves the row Listening"
    assert 'ev.type === "ota" && ev.state === "rebooting"' in handler
    assert '$("evnone").hidden = true;' in handler
    state = function("satState")
    assert state.index('"restarting"') < state.index('"offline"'), \
        "a satellite restarting into new firmware is called Offline"
    # An update that broke off mid-transfer is remembered as well: the hub
    # sends `failed` and ends the session, which is the whole of its record.
    assert 'if (ev.type === "ota" && ev.state === "failed")' in handler
    assert "SATELLITES.otaFailed.set(ev.satellite" in handler
    assert state.index('"failed", "Update failed"') < state.index('"offline", "Offline"'), \
        "an update that broke off is called Offline"


def test_an_event_names_a_button_as_the_page_does():
    """Activity and Try it said "vol_up press", the firmware's id, beside a
    Buttons grid that calls it Vol +."""
    assert "${satButtonName(ev.button)} ${ev.action}" in function("satEventWhat")
    assert "${ev.button} ${ev.action}" not in function("satEventWhat")


def test_a_wake_word_that_failed_says_so_once_and_in_a_sentence():
    """The hub's reason is often a Python exception's own words. It is wrapped
    in a sentence on the word's row, it is not repeated on every satellite
    that has a word to hear, and a "Saved." left beside a later failure goes."""
    assert 'satText("wwBroken", { error: live.error' in function("wakeRowUpdate")
    assert "if (broken.length && !ready.length) parts.push(" in function("satListens")
    render = function("wakeRender")
    assert 'broken !== WAKE.broken && $("wwnote").querySelector(":scope > .hint")' in render


# --------------------------------------------------------- routing, firmware --


def test_routing_is_on_the_word_and_its_editor_is_gone():
    """The hub keeps what a word does on the word, and answers PUT
    /satellites/routing 409 routing_per_wake_word. The rules.json editor
    would have been a box whose every Save failed, so it is gone, and only
    its Try box is left, inside Wake words."""
    for gone in ('id="sat-routing"', 'id="routetext"', 'id="routesave"', 'id="routesatellite"'):
        assert gone not in PANEL, f"{gone} is back"
    assert 'json("/satellites/routing", { method: "PUT"' not in CODE
    assert 'json("/satellites/routing")' not in CODE
    words = PANEL[PANEL.index('<details id="sat-wakewords"'):PANEL.index('<details id="sat-activity"')]
    assert 'id="routeform"' in words and '<select id="routeword"></select>' in words


def test_try_a_word_offers_the_saved_words_that_have_an_action():
    """A trigger has no action to run, and the hub runs what it has saved,
    not what the page is editing; push-to-talk is always there."""
    words = function("wakeTryWords")
    assert 'WAKE.server.words.filter(w => w.mode !== "trigger" && w.action)' in words
    assert "WAKE.draft" not in words
    assert '[WAKE_PTT, "Push-to-talk"]' in words
    assert "hey_jarvis" not in PANEL
    assert CODE.count('json("/satellites/routing/test", { method: "POST"') == 1
    assert '$("routeform").addEventListener("submit"' in CODE, "Enter does not send it"


def test_update_every_satellite_is_on_the_newest_image_only():
    """It was on every image at one weight, so an older image's button
    downgraded the house and its question did not say the image was older.
    The newest image per model updates; an older one offers a roll back,
    named as one."""
    render = function("firmwareRender")
    assert "firmwareRow(fw, satNewest(fw.model) === fw)" in render
    row = function("firmwareRow")
    assert "row.querySelector(newest ? '[data-fw=\"back\"]' : '[data-fw=\"all\"]').remove();" in row
    assert 'data-fw="back">Roll back every satellite</button>' in row
    assert "an older image?" in SCRIPT[SCRIPT.index("askRollback:"):][:120]


def test_a_satellite_that_has_gone_is_a_word_a_reason_and_forget():
    """A seen satellite that went offline was the heaviest row in the calm
    view: a Name field nothing could use, two greyed buttons and a Forget
    stranded at the left of its own line on a phone."""
    assert 'class="sat-newname"' in PENDING
    assert 'placeholder="e.g. kitchen"' in PENDING, "the placeholder reads as a typed value"
    assert 'class="small tight danger end" type="button" data-act="dismiss" hidden>Forget' in PENDING
    assert "#tab-satellites .row>.end{margin-left:auto}" in BARE_CSS
    assert "named.hidden = !n.online;" in function("satelliteUpdate")


def test_firmware_is_rebuilt_only_when_the_list_changes():
    render = function("firmwareRender")
    assert "if (box.dataset.sig !== sig)" in render
    assert "model.dataset.touched" in render, "the Model field is overwritten while being typed in"
