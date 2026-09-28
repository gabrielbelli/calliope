"""The Satellites tab: one card, one list of satellites, each satellite's own
settings folded under its row, and the hub's settings (wake words, activity,
firmware) as three quiet disclosures at the foot. Routing was a fourth until
the hub moved it onto each wake word (2026-09-25).

The tab lists and manages the satellites. Each satellite's own settings
collapse under its row; there are several wake words, and each one chooses
the satellites that hear it; and what is set once or rarely (firmware,
addresses) sits in folded sections, so the page is not overwhelming.

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
    foot of it, and firmware last of all. Routing is not
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
    """Every satellite was a bordered box inside the bordered card, the
    nesting the Jobs tab already dropped. Rows are divided by hairlines, and
    the old box rule is gone."""
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


def test_a_faults_line_wraps_and_a_healthy_one_stays_one_line():
    """An update's failure and why a satellite is not listening end in the
    reason, which an ellipsis cut on a phone and nothing else on the row
    repeats; so do an image's signed flag and date."""
    assert ".sat[data-state=failed] .sat-line,.fws .sat-line{white-space:normal;overflow-wrap:anywhere}" in BARE_CSS
    assert "overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" in BARE_CSS[BARE_CSS.index(".sat-line{"):]


def test_a_hub_summary_that_wraps_keeps_the_left_edge():
    """The satellite rows keep their text clear of the chevron with a grid;
    the hub's summaries are a title and a note in plain inline text, and on
    a phone the note's second line started under the chevron."""
    assert ".sat-hub>details>summary{padding-left:1em;text-indent:-1em}" in BARE_CSS
    assert ".sat-hub>details>summary::before{text-indent:0}" in BARE_CSS


def test_a_segmented_button_shows_its_focus_ring_and_greys_when_off():
    """The shared ring stands 2px outside a button, and .seg{overflow:hidden}
    cut it off: Command, which Add focuses, and Every satellite showed no
    ring. It is drawn inside, in the fill's ink on the pressed one. And a
    word marked for removal kept its pressed segments in the full accent,
    because the pressed rule outranks button:disabled."""
    assert ".seg{display:inline-flex;border:1px solid var(--line);" in BARE_CSS
    assert "overflow:hidden}" in BARE_CSS[BARE_CSS.index(".seg{display:inline-flex"):][:120]
    assert ".seg button:focus-visible{outline-offset:-3px}" in BARE_CSS
    assert ".seg button[aria-pressed=true]:focus-visible{outline-color:var(--accent-ink)}" in BARE_CSS
    assert ".seg button:disabled{background:var(--sunk)}" in BARE_CSS
    assert ".seg button[aria-pressed=true]:disabled{background:var(--sunk);color:var(--ink);" in BARE_CSS
    # After the rules they correct, since they tie on specificity or lose.
    assert (BARE_CSS.index(".seg button:disabled{") > BARE_CSS.index(".seg button{border:none")
            > BARE_CSS.index("button:disabled{cursor:not-allowed"))
    assert ".ww[data-removed] fieldset>legend{opacity:.55}" in BARE_CSS


# ------------------------------------------------------------ the rows --


def test_a_satellites_own_settings_are_folded_under_it_and_start_closed():
    """What people change weekly is one level down (levels, speaker,
    microphone, lights); what makes a noise is in Try it; the mapping in
    Buttons; and the device's facts and every consequential action in
    Device, two levels down."""
    assert '<details class="sat-row">' in ADOPTED, "the row starts open, or is not a disclosure"
    for control in ('data-cfg="volume"', 'data-cfg="mic_gain_db"', 'data-cfg="brightness"',
                    'data-cfg="speaker_enabled"', 'data-cfg="mic_enabled"', 'data-cfg="lights_enabled"',
                    'data-act="wakewords"'):
        assert control in OPEN_ROW, f"{control} is not on the open row"
    for control in ('class="row sat-sayform"', 'data-act="identify"', 'data-act="tone"',
                    'data-act="listen"', 'data-act="stop"', 'data-act="lights"', "<audio"):
        assert control in TRY, f"{control} is not in Try it"
    assert 'class="sat-btns"' in BUTTONS, "the mapping is not in Buttons"
    # No button is set apart from the rest: the volume pair has no switch of
    # its own any more, it is two rows of the grid like the others.
    assert "local_volume_buttons" not in ADOPTED
    for control in ('<dl class="facts">', 'data-act="update"', 'class="row sat-rename"',
                    'data-act="reboot"', 'data-act="move"', 'data-act="forget"',
                    'class="row sat-moveform"'):
        assert control in DEVICE, f"{control} is not in Device"


def test_the_satellite_summary_carries_no_device_facts_and_nothing_pressable():
    """Closed, a row is a name, one state word and one line. Firmware, the
    address and the signal are set once or rarely read, so they stay off the
    main view; and a summary is a button, so nothing pressable may sit inside
    it."""
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
    for name in ("wakeRender", "wakeRowUpdate", "wakeActionUpdate", "wakeLlmUpdate"):
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
                 "wakeActionUpdate", "satellitesHealth", "wakeTryWords", "satFillSelect",
                 "wakeLlmUpdate", "wakeModelsFetch", "wakeModelsHint", "satFillList",
                 "wakeKeyStore", "wakeKeyForget", "wakeLlmTry"):
        assert "innerHTML" not in function(name), f"{name} writes markup"
    # A server's model ids are a third party's words: options made one by
    # one, with the id as the value.
    fill = function("satFillList")
    assert 'document.createElement("option")' in fill and "option.value = v;" in fill
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
    # Its track does not move as the value grows: "10 of 12" is 8ch, twice
    # the shared floor, so a satellite's outputs are pinned at that.
    assert "`${v} of ${steps}`" in function("satLevelSay")
    assert ".sats .slider output{min-width:8ch}" in BARE_CSS


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
    # Set up the ring lights the ring, which a mute paints red: greyed with
    # its reason before the press, not refused after it.
    assert needs["ring"] == "online lights unmuted"
    assert '<div class="hint sat-ringwhy"></div>' in DEVICE
    assert 'li.querySelector(".sat-ringwhy")' in function("satDevice")
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


def test_the_buttons_grid_fits_a_phone_and_names_the_lights_switch():
    """At three columns a phone left each select about 84px of text, and a
    select cuts rather than ellipsing: "Night mode (lights on/off)" read
    "Night mode (l". On a phone each button's name heads its own pair. And
    the action flips lights_enabled, which the row calls Lights."""
    actions = dict(re.findall(r'\["(\w+)", "([^"]+)"\]', CODE[CODE.index("const SAT_ACTIONS = ["):][:600]))
    assert actions["lights"] == "Lights on/off"
    assert "Night mode" not in CODE
    for value, label in actions.items():
        assert len(label) <= 14, f"{value}'s label {label!r} is cut on a phone"
    block = BARE_CSS[BARE_CSS.rindex("@media (max-width:30rem){"):]
    block = block[:block.index("\n}")]
    assert ".sat-btns{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}" in block
    assert ".sat-btns>span:first-child{display:none}" in block
    assert ".sat-btns .bn{grid-column:1/-1;" in block
    # The Side button's caveat is on its own row, not the last of four hints.
    assert "R37" not in BUTTONS and "firmware README" not in BUTTONS
    assert 'if (key === "key1")' in function("satButtons")
    assert "SAT_COPY.sideNeeds" in function("satButtons")
    assert ".sat-buttons .body>.hint+.hint{margin-top:var(--s1)}" in BARE_CSS


def test_a_failed_setting_is_reported_beside_the_control():
    """A refused Volume, Speaker or Lights went to the row's note, which sat
    after Try it, Buttons and Device, a screen away on a phone with Device
    open; and a refused button mapping under four hints below the grid."""
    note = '<div class="sat-note" aria-live="polite"></div>'
    assert OPEN_ROW.count(note) == 1, "the row's own note is not above its disclosures"
    assert OPEN_ROW.index('class="row sat-switches"') < OPEN_ROW.index(note)
    assert BUTTONS.index(note) < BUTTONS.index('<div class="hint">')
    assert BODY.count(note) == 4, "a section lost its note, or has two"
    assert 'scope.querySelector(":scope > .sat-note")' in function("satNoteFor")


def test_the_device_facts_are_written_in_place_and_not_rebuilt_on_a_poll():
    """The Wi-Fi signal moves with most status reports, and rebuilding the
    list for it took away a selection of the Address or ID mid-copy."""
    device = function("satDevice")
    assert 'const sig = facts.map(f => f[0]).join("|");' in device
    assert "satSet(dl.children[i * 2 + 1], value)" in device
    assert "JSON.stringify(facts)" not in device


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
    assert ('const ask = act === "all" ? (say.name ? "askUpdate" : "askUpdateAll")\n'
            '    : act === "back" ? (say.name ? "askRollbackOne" : "askRollback") : "askDelete";') in fw
    assert "if (!confirm(satText(ask, say))) return;" in fw
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


def test_setting_up_the_ring_keeps_the_focus_with_its_steps():
    """Each step hid the button just pressed, so the focus fell to the page
    and a keyboard began again at the top of the document. And the panel is
    opened by a button, which said nothing of being open."""
    show = function("satRingShow")
    assert 'open.setAttribute("aria-expanded", String(step !== null));' in show
    assert 'const lost = !!held && box.contains(held) && !!held.closest("[hidden]");' in show
    assert "held === open && step === \"top\"" in show
    assert "to.focus({ preventScroll: true });" in show
    for goes_to in ("'[data-act=\"ring-cw\"]'", "'[data-act=\"ring-next\"]'"):
        assert goes_to in show
    assert 'aria-expanded="false" aria-controls="${u}-ringset">Set up the ring' in DEVICE
    assert '<div class="sat-ringset" id="${u}-ringset" hidden>' in DEVICE
    # Every step goes through satRingShow, which is what moves the focus.
    setup = function("satRingSetup")
    assert setup.count("satRingShow(li, null);") == 3 and "satRingShow(li, li._ring.step);" in setup
    # The box names what the wizard asks, and the panel's rows do not touch.
    assert "> LEDs run anticlockwise</label>" in DEVICE and "Upside down" not in DEVICE
    assert ".sat-ringsay{margin:0}" in BARE_CSS and ".sat-ringset>*+*{margin-top:var(--s2)}" in BARE_CSS


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
           "s": "1.3", "output": "Jack (aux)", "hears": "home_assistant_cloud (en-GB)",
           "speaks": "calliope_kokoro as pf_dora", "first": "0.4",
           "reply": "Hello! How can I help you today?", "buttons": "Rec or Mode",
           "model": "vendor/test-model-large-instruct"}


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


def test_an_assist_word_takes_its_pipeline_from_a_list_and_its_voice_from_home_assistant():
    """An Assist word hears, understands and speaks through a pipeline set up
    in Home Assistant, so the pipeline is picked from Home Assistant's own list
    rather than typed as an id, and the language and voice the page would
    otherwise ask for are hidden: the pipeline has its own."""
    pick = WORD[WORD.index('data-f="d.pipeline"') - 200:WORD.index('data-f="d.pipeline"') + 40]
    assert '<select id="${u}-pipe" data-f="d.pipeline"></select>' in pick
    assert 'data-dest="ha_assist"' in pick
    assert WORD.count('data-f="d.pipeline"') == 1, "the old typed pipeline field is back"
    for field in ('data-f="lang"', 'data-f="a.voice"'):
        owner = WORD[:WORD.index(field)]
        owner = owner[owner.rindex("<div"):]
        assert 'data-nodest="ha_assist"' in owner, f"{field} is still asked of an Assist word"
    # A failed list is asked again from the row; the line that restated what
    # the ready hint says (hears with, speaks with) is gone.
    assert '<button class="link" type="button" data-ww="pipes" hidden>Ask again</button>' in pick + WORD
    assert "It hears, understands and speaks as set in Home Assistant." not in WORD
    assert 'b.dataset.ww === "pipes"' in function("wakeRow")
    assert "let again = wakePipesForget(row);" in function("wakeRow")
    update = function("wakeActionUpdate")
    assert "ask.hidden = !waiting && (!entry || entry.state !== \"failed\");" in update
    assert "satSet(ask, waiting ? SAT_COPY.pipeList : SAT_COPY.pipeAgain);" in update
    assert 'row.querySelectorAll("[data-nodest]")' in update
    assert 'q(".ww-tag").hidden = !other || q(".ww-lang").hidden;' in update
    assert CODE.count('json("/satellites/ha/pipelines", { method: "POST"') == 1


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
    # Said once, as the summary's state word; the row hint beside Keep said
    # it again in other words ("Removed when you save.").
    assert '["Removed on save", ""]' in update
    assert "wwRemoved" not in CODE and 'satSet(q(".ww-rowhint"), removed ? ""' in update
    assert 'rm.classList.toggle("danger", !removed);' in update, "Keep is drawn as a destructive button"
    assert "button.focus();" in remove, "focus is lost when Remove becomes Keep"


def test_save_is_off_with_its_reason_when_there_is_nothing_to_save():
    """Off when nothing changed, when a word is left with nobody to hear it,
    and when an entry would be refused: the hub's 422, said before the press,
    with the word named beside Save and the reason on the word's own row."""
    render = function("wakeRender")
    assert "const nobody = dirty ? wakeEffective().find(w => !w.satellites.length" in render
    assert 'const bad = dirty ? wakeFirstBad() : null, fix = bad ? bad.name : "";' in render
    assert '$("wwsave").disabled = !dirty || !!nobody || !!fix;' in render
    assert ('nobody ? satText("wwNone", { word: wwLabel(nobody.name) })\n'
            '    : fix ? satText(bad.empty ? "wwUnfinished" : "wwFix", { word: fix })\n'
            "    : dirty ? SAT_COPY.wwDirty : SAT_COPY.wwClean") in render
    assert 'satNoteOnce(q(".ww-fix"), quiet ? "hint" : "warn", problem);' in function("wakeRowUpdate")
    assert "if (wakeFirstProblem()) { wakeRender(); return; }" in function("wakeSave")


def test_a_closed_word_says_what_a_save_will_do_to_it():
    """A word marked for removal, or edited, said Ready closed, and the only
    sign of a change was "Unsaved changes." beside Save. Dim words, no chip:
    healthy is quiet, and a change the reader made is not a fault."""
    update = function("wakeRowUpdate")
    order = [update.index(word) for word in ('"Needs a fix"', '"Removed on save"',
                                             'live.state !== "ready"', '"Changed"', '"Not saved yet"')]
    assert order == sorted(order), "the state words are chosen in another order"
    assert '["Removed on save", ""]' in update and '["Changed", ""]' in update
    render = function("wakeRender")
    assert "const changed = !!WAKE.draft && !!was && wakeKey([w]) !== wakeKey([was]);" in render
    assert "wakeKey([], ptt) !== wakeKey([], WAKE.server.ptt)" in render


def test_a_word_being_filled_in_is_incomplete_and_its_field_is_marked():
    """A new Language model or Webhook action starts with its required
    fields empty, and the row said Needs a fix in amber, with a warning box
    several rows from the field, before a key was pressed. Empty, or being
    typed in, is Incomplete in dim text with the reason as a plain line; a
    value the hub would refuse is still Needs a fix. Either way the field is
    described by the reason, which nothing named before; it is aria-invalid,
    with the amber edge that follows, only at Needs a fix. Marked while
    Incomplete, it was "invalid entry" read out, and drawn amber, on the
    render that chose Webhook."""
    update = function("wakeRowUpdate")
    assert "const at = problem ? wakeProblemAt(w, problem) : null;" in update
    assert "(at.empty || (!!culprit && culprit === document.activeElement))" in update
    assert '(quiet ? ["Incomplete", ""] : ["Needs a fix", "warn"])' in update
    assert "const about = f === culprit, bad = about && !quiet;" in update
    assert 'if (bad) f.setAttribute("aria-invalid", "true");' in update
    assert 'if (about) f.setAttribute("aria-describedby", q(".ww-fix").id);' in update
    assert '<div class="ww-fix" id="${u}-fix"></div>' in WORD
    assert ".ww [aria-invalid=true]{border-color:var(--warn)}" in BARE_CSS
    # A field left is read again: while it had the focus it was only typed.
    assert 'row.addEventListener("focusout"' in function("wakeRow")
    # Every sentence wakeProblem can say has its field, bar the fallback's,
    # which carries the word's name and is the default.
    at = function("wakeProblemAt")
    for key in re.findall(r"SAT_COPY\.(fix\w+)", function("wakeProblem")):
        assert f"[SAT_COPY.{key}]" in at, f"{key} names no field"
    assert '|| ["a.fallback"]' in at


def test_a_fix_line_turning_to_a_warning_does_not_move_what_is_under_it():
    """Leaving a field that holds a wrong value turns the plain fix line into
    the .note.warn box, on focusout, between a mouse press and its release.
    The box's padding made it 16px taller, so Remove dropped under the
    pointer and the press landed on nothing. The plain line has the note's
    box, so only the ink and the fill change."""
    note = re.search(r"\.note\{[^}]*\}", BARE_CSS).group(0)
    assert "padding:var(--s2) var(--s3)" in note and "margin-top:var(--s2)" in note
    assert ".ww-fix>.hint{padding:var(--s2) var(--s3)}" in BARE_CSS
    assert re.search(r"\.hint\{[^}]*margin-top:var\(--s2\)", BARE_CSS)
    assert 'satNoteOnce(q(".ww-fix"), quiet ? "hint" : "warn", problem);' in function("wakeRowUpdate")


def test_a_removed_word_offers_no_list_to_ask_for_and_a_list_asked_for_keeps_the_focus():
    """Ask again and List models hide themselves once pressed and hand the
    focus to the field they fill. On a removed word that field is greyed, so
    Ask again, live on a struck-through row, left the focus on the page; and
    List models handed it to nothing at all."""
    update = function("wakeRowUpdate")
    hide = ("if (removed) for (const b of row.querySelectorAll('[data-ww=\"pipes\"], "
            "[data-ww=\"models\"]')) b.hidden = true;")
    assert hide in update
    # After the render that shows them, or it would be undone at once.
    assert update.index("wakeActionUpdate(row, w, words, moreFix)") < update.index(hide)
    row = function("wakeRow")
    assert "if (b.hidden) row.querySelector('[data-f=\"d.model\"]').focus();" in row
    assert "row.querySelector('[data-f=\"d.pipeline\"]').focus();" in row


def test_the_ring_colour_is_every_modes_and_its_reset_keeps_the_focus():
    """A trigger flashes the colour, so the picker is written for every mode
    and not with the action's fields; a word with no mode yet has none, so
    picking one cannot be what gives it an echo action. Use the default
    hides itself, so the picker it reset takes the focus."""
    assert '<div class="grid2 ww-colour" data-when="command conversation trigger">' in WORD
    update = function("wakeRowUpdate")
    assert "wakeValue(q('[data-f=\"colour\"]'), w.colour || WAKE_SHOWN.colour);" in update
    assert "data-f=\"colour\"" not in function("wakeActionUpdate")
    assert ("wakeEdit(name, w => { w.colour = null; });\n"
            "      row.querySelector('[data-f=\"colour\"]').focus();") in function("wakeRow")


def test_a_fix_under_the_closed_more_is_said_on_its_summary():
    """The reply limit and the end phrases are under More. Closed, the row
    said Needs a fix with its reason while the field it marked was out of
    sight; More's own note says so, first, without opening it."""
    update = function("wakeRowUpdate")
    assert 'const moreFix = !!culprit && !quiet && !!culprit.closest(".ww-more");' in update
    assert "wakeMoreNote(a, !q('[data-f=\"a.voice\"]').parentElement.hidden, moreFix)" in function("wakeActionUpdate")
    assert "const said = fix ? [SAT_COPY.moreFix] : [];" in function("wakeMoreNote")
    for field in ('data-f="d.max_tokens"', 'data-f="c.end_phrases"'):
        more = WORD[WORD.index('<details class="sub ww-more"'):WORD.index("</details>", WORD.index("ww-more"))]
        assert field in more, field


def test_a_removed_word_greys_its_key_test_and_colour_buttons():
    """The removal loop greys fields and the mode buttons. Clear key, which
    clears a key other words share, Store key, Test and Use the default
    stayed pressable on a struck-through row."""
    update = function("wakeRowUpdate")
    for ww in ("keyclear", "colourdefault", "key", "llmtest"):
        assert f'[data-ww=\"{ww}\"]' in update, ww
    assert 'if (b.dataset.ww === "keyclear" || b.dataset.ww === "colourdefault") b.disabled = removed;' in update
    assert "else if (removed) b.disabled = true;" in update
    # After the render that sets Store key and Test from the form.
    assert update.index("wakeActionUpdate(row, w, words, moreFix)") < update.index("else if (removed) b.disabled")


def test_the_add_list_offers_only_what_the_hub_can_load_and_is_not_listed():
    render = function("wakeRender")
    assert "(WAKE.server.available || []).filter(a => !keep.has(a))" in render
    assert 'pick.disabled = $("wwaddgo").disabled = !left.length;' in render
    assert "left.length ? SAT_COPY.wwAddWhy : SAT_COPY.wwExhausted" in render


def test_the_add_hint_and_last_heard_say_what_happens():
    """A new word starts on every satellite ("*"), and the hint said it woke
    only the ones you choose. Last heard put the score where a time goes
    ("at 0.83") and the clock last, so the one number to set against the
    Threshold was the hardest to read. Push-to-talk said Play button, but
    any button can be set to Talk (and Play need not be)."""
    assert 'satellites: ["*"]' in function("wakeAdd")
    said = sat_copy()
    assert said["wwAddWhy"] == "It downloads when you save, and wakes every satellite until you choose some."
    assert f'<div class="hint" id="wwaddwhy">{said["wwAddWhy"]}</div>' in PANEL
    assert said["wwLast"] == "Last heard on {name} at {clock}, scoring {score}."
    update = function("wakeRowUpdate")
    assert '"Play button"' not in update and 'ptt ? ["Talk buttons", ""]' in update
    assert "ptt ? SAT_COPY.wwPttWhere" in update


def test_an_open_word_reads_as_mode_who_hears_it_and_what_it_does():
    """Every block in a word stood the same --s3 apart as two fields of one
    grid, so an open command word was one even block of eight to ten fields."""
    assert WORD.count('class="grid2 ww-does"') == 2, "the action or the trigger grid lost its group"
    assert ("details.sat-row>.body>.ww-listen,details.sat-row>.body>.ww-does{margin-top:var(--s5)}"
            in BARE_CSS)
    # It has to outrank the owl rule it corrects.
    assert "details.sat-row>.body>*+*," in BARE_CSS


def test_a_control_off_for_its_own_reason_stays_off_on_every_render():
    """The fallback opened to one choice on every command word of a first
    setup, and the removal loop set every control's disabled from `removed`
    alone, which switched the key box back on after Store had turned it off."""
    update = function("wakeRowUpdate")
    assert 'c.disabled = removed || c.hasAttribute("data-off");' in update
    assert update.index("wakeActionUpdate(row, w, words, moreFix)") < update.index("c.disabled = removed")
    assert ("q('[data-f=\"a.fallback\"]').toggleAttribute(\"data-off\", !talk.length && !a.fallback);"
            in function("wakeActionUpdate"))
    assert ("q('input[type=\"password\"]').toggleAttribute(\"data-off\", !keys.canStore);"
            in function("wakeLlmUpdate"))


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
    assert CODE.count('json("/satellites/llm/models", { method: "POST"') == 1
    assert CODE.count('json("/satellites/llm/test", { method: "POST"') == 1
    assert CODE.count('json("/satellites/secrets", { method: "PUT"') == 1


# ------------------------------------------------ a language model word --


def test_the_key_box_is_write_only():
    """The page stores a key and is never given one back. The box has no
    data-f, so the row's input handler never copies it into the draft or a
    Save; a browser and a password manager are told it is not a password to
    fill or to make up; and the box is emptied before the request goes, so a
    failed one leaves no copy."""
    box = WORD[WORD.index('id="${u}-key"') - 30:]
    box = box[:box.index(">") + 1]
    assert '<input type="password" id="${u}-key"' in box
    assert 'spellcheck="false"' in box
    # The attribute itself: data-form-type is Dashlane's, read as formType.
    assert not re.search(r"\sdata-f=", box), "the key box feeds the draft"
    assert 'maxlength="4096"' in box
    store = function("wakeKeyStore")
    assert store.index('box.value = "";') < store.index("await "), \
        "the box still holds the key while the request is out"
    for name in ("wakeKeyStore", "wakeKeyPut", "wakeKeyForget", "wakeKeyClear"):
        body = function(name)
        for leak in ("store.set", "console.", "localStorage", "WAKE.draft", "WAKE.server ="):
            assert leak not in body, f"{name} keeps the key somewhere: {leak}"
    # Clearing asks first, and nothing asks by prompt().
    forget = function("wakeKeyForget")
    assert forget.index('confirm(satText("askKeyClear"') < forget.index("await ")


def test_the_key_box_asks_no_password_manager_for_a_generated_password():
    """autocomplete="new-password" is the hint for password generation:
    Chrome's password manager, 1Password and Bitwarden offered a strong
    password on focus, and one click stored a random string as the
    provider's key, so every turn answered 401. Each manager's own opt-out
    is on the box too."""
    box = WORD[WORD.index('id="${u}-key"') - 30:]
    box = box[:box.index(">") + 1]
    assert "new-password" not in box and 'autocomplete="off"' in box
    for word in ("data-1p-ignore", 'data-lpignore="true"', "data-bwignore", 'data-form-type="other"'):
        assert word in box, f"the key box is missing {word}"
    # Store and the box go off together (test_satellites_llm.py drives it).
    assert ("wakeKeyBox(q('input[type=\"password\"]'), q(\".ww-keynote\"), keys.canStore);"
            in function("wakeLlmUpdate"))


def test_the_language_model_fields_name_no_one_server():
    """A language model word takes any OpenAI-compatible API, so no
    placeholder or generated sentence names one server's address or one
    model; the Base URL's example is a hosted provider's, and the hint beside
    it names self-hosted servers only as some among several."""
    placeholders = re.findall(r'placeholder="([^"]*)"', WORD)
    for text in placeholders + list(sat_copy().values()):
        assert "ollama" not in text.lower() and "llama3" not in text.lower(), text
    assert 'data-f="d.base_url" maxlength="500"\n                 placeholder="https://api.openai.com/v1"' in WORD
    assert '<label for="${u}-base">Base URL</label>' in WORD
    assert 'placeholder="Pick one, or type its id"' in WORD
    # The model is typed or picked: the list only suggests.
    assert '<input type="text" id="${u}-model" list="${u}-models" data-f="d.model"' in WORD
    assert '<datalist id="${u}-models"></datalist>' in WORD
    # The shared variable field is a "Key name" on a language model word,
    # and optional on a webhook, whose examples are a webhook's and not Home
    # Assistant's.
    assert '<label for="${u}-env" class="ww-envlabel">Token variable</label>' in WORD
    update = function("wakeActionUpdate")
    assert ('satSet(q(".ww-envlabel"), llm ? "Key name" : hook ? "Token variable (optional)" '
            ': "Token variable");') in update
    assert '"e.g. SATELLITES_WEBHOOK_TOKEN"' in update
    assert 'hook ? "https://hooks.example.com/…" : "https://homeassistant.local:8123"' in update


def test_the_model_list_is_asked_only_for_an_address_nobody_is_typing():
    """Asking 600 ms after the last keystroke, as the pipeline picker once
    did, sends a key to a half-typed host that happens to resolve. The model
    list is asked on a row's first paint, a provider picked, or a committed
    field, and never while the Base URL or the key's name has the focus.
    Whether it is asked without a press at all is wakeModelsFree's, which
    every caller reaches with the word's saved destination
    (test_satellites_llm.py). The pipeline picker follows the same rule
    (test_satellites_modes.py)."""
    want = function("wakeModelsWant")
    assert "setTimeout" not in want and "typing ||" in want and "!wakeModelsFree(d, saved)" in want
    update = function("wakeLlmUpdate")
    assert ("wakeModelsWant(d, [q('[data-f=\"d.base_url\"]'), q('[data-f=\"d.env\"]')]"
            ".includes(document.activeElement), saved);") in update
    assert "const saved = wakeSavedDest(w.name || WAKE_PTT);" in update
    # Shown to ask for the first time, or again after a failure.
    assert "go.hidden = !waiting && !failed;" in update
    row = function("wakeRow")
    assert 't.dataset.f === "d.base_url" || t.dataset.f === "d.url" || t.dataset.f === "d.env"' in row
    assert "wakePipesCommit(d);" in row
    pipes = function("wakePipesWant")
    assert "setTimeout" not in pipes and "typing ||" in pipes and "!wakePipesFree(d)" in pipes
    assert "wakeModelsWant(d, false, wakeSavedDest(name));" in row
    assert 'b.dataset.ww === "models"' in row and "wakeModelsList(d);" in row
    assert ('<button class="small tight ww-modelsgo" type="button" data-ww="models" hidden>'
            'List models</button>') in WORD


def test_test_waits_for_a_form_the_hub_would_take_and_says_why_beside_it():
    """Test sends the form as it stands; one the hub would refuse is a 422
    the row already names in its fix line, so Test is greyed until then."""
    assert '<button class="small" type="button" data-ww="llmtest">Test</button>' in WORD
    assert "test.disabled = !!wakeProblem({ mode: \"command\", action: { destination: d } }, words);" \
        in function("wakeLlmUpdate")
    assert '<div class="ww-llmresult" aria-live="polite"></div>' in WORD


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


def test_activity_is_a_log_a_screen_reader_hears_and_a_keyboard_can_scroll():
    """Activity exists to show an event arrive when a button is pressed, and
    it was a plain list: silent to a screen reader, and a scroll box Safari
    never lets a keyboard reach. role="log" cannot go on an <ol>."""
    activity = PANEL[PANEL.index('<details id="sat-activity">'):PANEL.index('<details id="sat-firmware">')]
    assert ('<div class="sat-log" role="log" aria-label="Satellite activity" aria-relevant="additions">'
            in activity)
    assert '<ol class="satelliteevents" id="satelliteevents" role="list"></ol>' in activity
    assert "list.parentElement.tabIndex = 0;" in function("satEventAdd")
    assert ".sat-log{max-height:14rem;overflow:auto}" in BARE_CSS
    assert ".sat-log:focus-visible{outline:2px solid var(--accent);" in BARE_CSS
    # A line is a time column and the rest, and a failure is marked.
    line = function("satEventLine")
    assert '"<time></time><span><b></b> </span>"' in line
    assert 'hour: "2-digit", minute: "2-digit", second: "2-digit"' in line
    assert 'li.classList.toggle("bad", satEventBad(ev));' in line
    assert "grid-template-columns:auto minmax(0,1fr)" in BARE_CSS[BARE_CSS.index(".satelliteevents li{"):][:120]
    assert ".satelliteevents li.bad{color:var(--bad);border-left:2px solid var(--bad)" in BARE_CSS


def test_an_event_names_a_button_as_the_page_does():
    """Activity and Try it said "vol_up press", the firmware's id, beside a
    Buttons grid that calls it Vol +."""
    what = function("satEventWhat")
    assert "${satButtonName(ev.button)} ${SAT_EDGE_SAID[ev.action] || ev.action}" in what
    assert "${ev.button} ${ev.action}" not in what
    assert 'const SAT_EDGE_SAID = { press: "pressed", release: "released" };' in CODE


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
    # Greyed, with the reason beside it, when no satellite would change: the
    # hub's "all" skips none already on the image, and with every satellite
    # offline it answered 404 "no satellite matches 'all'".
    assert '<span class="hint fw-why"></span>' in row and "row._fw = fw;" in row
    assert "go.disabled = !due.length;" in render and 'row.querySelector(".fw-why")' in render
    due = function("firmwareDue")
    assert "n.adopted && n.online && n.model === fw.model" in due
    assert "satFirmware(n) !== fw.version && !satGoingTo(n)" in due
    act = function("firmwareAct")
    assert 'satellite: "all"' not in act and "for (const n of due)" in act
    assert 'const due = act === "delete" ? [] : firmwareDue(fw);' in act
    assert "JSON.stringify({ satellite: n.id, sha256: fw.sha256 })" in act


def test_a_satellite_that_has_gone_is_a_word_a_reason_and_forget():
    """A seen satellite that went offline was the heaviest row in the calm
    view: a Name field nothing could use, two greyed buttons and a Forget
    stranded at the left of its own line on a phone."""
    assert 'class="sat-newname"' in PENDING
    assert 'placeholder="e.g. kitchen"' in PENDING, "the placeholder reads as a typed value"
    assert 'class="small tight danger end" type="button" data-act="dismiss" hidden>Forget' in PENDING
    assert "#tab-satellites .row>.end{margin-left:auto}" in BARE_CSS
    assert "named.hidden = !n.online;" in function("satelliteUpdate")


def test_the_firmware_upload_is_two_rows_and_its_signature_has_the_width():
    """Every field was .tight, so each kept an input's own 170px: Signature
    and Upload wrapped ragged, and a 90-character signature could not be
    read back. Its placeholder, "unsigned", also read as a typed value."""
    form = PANEL[PANEL.index("<legend>Upload an image</legend>"):PANEL.index("</fieldset>",
                                                                              PANEL.index("Upload an image"))]
    assert form.count('<div class="row">') == 2
    assert '<div class="grow"><label for="fwsig">Signature</label>' in form
    assert 'placeholder="unsigned"' not in form
    assert 'placeholder="e.g. v0.3.1-4-g1a2b3c4"' in form
    assert '<div class="tight"><label for="fwversion">' not in form
    assert "fieldset.sat-fs>.row+.row{margin-top:var(--s3)}" in BARE_CSS
    assert '$("fwupload").addEventListener("click", firmwareUpload);' in CODE


def test_firmware_is_rebuilt_only_when_the_list_changes():
    render = function("firmwareRender")
    assert "if (box.dataset.sig !== sig)" in render
    assert "model.dataset.touched" in render, "the Model field is overwritten while being typed in"
