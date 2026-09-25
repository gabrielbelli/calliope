"""The wake words: one shape, owned by two services.

GET and PUT /satellites/wake-words are answered by voice-satellites and read by
the Satellites tab. The two halves were written at the same time by two people
against one written contract, which is how test_gap_counts_contract.py's bug
happened: each side tested its own half against its own idea of the names, and
both suites passed over a page that printed nothing.

So nothing here restates the contract. Each assertion reads the names out of
both files and compares them: what the page sends is what the hub's body model
takes, what the page reads is what the hub emits, and the states the page
colours are the states the hub reports. The hub is read as source through
`ast`, never imported, for the reason the sibling file gives: its import pulls
in openWakeWord, numpy and its own configuration, and the thing under test is a
set of names.
"""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "ui.html").read_text()
HUB = REPO / "services" / "satellites" / "app"
HUB_MAIN = ast.parse((HUB / "main.py").read_text())
HUB_WORDS = ast.parse((HUB / "wakewords_config.py").read_text())

# Comments quote the names they are about, so they are removed before any name
# is collected from the page. A colon before // is a URL, not a comment.
CODE = re.sub(r"/\*.*?\*/|<!--.*?-->|(?<!:)//[^\n]*", "", PAGE, flags=re.S)


def hub_class(name: str, tree: ast.Module = HUB_MAIN) -> ast.ClassDef:
    found = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]
    assert found, f"voice-satellites has no class {name}; the contract moved"
    return found[0]


def method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    found = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert found, f"{cls.name}.{name} is gone from voice-satellites"
    return found[0]


def dict_keys(node: ast.AST) -> set[str]:
    """Every string key of every dict literal under `node`."""
    return {k.value for d in ast.walk(node) if isinstance(d, ast.Dict)
            for k in d.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def page_function(name: str) -> str:
    found = re.search(r"\n(?:async )?function " + re.escape(name) + r"\(", CODE)
    assert found, f"{name}() is gone from the page"
    return CODE[found.start():CODE.index("\n}\n", found.start()) + 2]


VOICE = hub_class("Voice")
WORD_FIELDS = dict_keys(method(VOICE, "views"))
ANSWER_FIELDS = dict_keys(method(VOICE, "describe"))


def test_the_page_sends_exactly_the_fields_the_hub_takes_for_a_word():
    """A field the page sent that the hub does not take would be dropped
    without a word (pydantic ignores extras), and one the hub needs that the
    page left out would be its default: every satellite at 0.5."""
    copy = page_function("wakeCopy")
    sent = set(re.findall(r"(\w+):", copy[copy.index("return {"):]))
    body = hub_class("WakeWordBody")
    taken = {a.target.id for a in body.body if isinstance(a, ast.AnnAssign)}
    assert sent == taken


def test_every_field_the_page_reads_from_the_answer_is_one_the_hub_sends():
    """A misspelt field reads as undefined in the browser, which the page shows
    as a word with no state or a card with no add list, and no test fails.
    `live` is the hub's copy of one word in the card's code (and, in
    wakeRender, a Map of them, whose methods are not fields); the name means
    other things elsewhere on the page, so only the card is searched."""
    card = CODE[CODE.index("const WAKE = {"):CODE.index("\nfunction firmwareRender(")]
    read_answer = set(re.findall(r"WAKE\.server\.(\w+)", CODE))
    assert read_answer and read_answer <= ANSWER_FIELDS, read_answer - ANSWER_FIELDS
    read_word = set(re.findall(r"\blive\.(\w+)\b(?!\()", card))
    assert read_word and read_word <= WORD_FIELDS, read_word - WORD_FIELDS


def test_the_states_the_page_colours_are_the_states_the_hub_reports():
    """A state the hub reports that the page has no colour for shows as plain
    text; one the page colours that the hub never sends is dead code that
    looks like a feature."""
    returned = set()
    for r in ast.walk(method(VOICE, "word_state")):
        if isinstance(r, ast.Return) and isinstance(r.value, ast.Tuple):
            first = r.value.elts[0]
            if isinstance(first, ast.Constant):
                returned.add(first.value)
    table = re.search(r"const WAKE_STATE = \{([^}]*)\}", CODE)
    assert table, "WAKE_STATE is gone from the page"
    assert set(re.findall(r"(\w+):", table.group(1))) == returned


def test_the_wake_word_event_the_page_listens_for_is_the_one_the_hub_publishes():
    """The page learns that a download finished from this event and not from a
    poll. Named differently on the two sides, a word would sit at
    "downloading" until the next three-second poll, and with the tab in the
    background, forever."""
    published = [d for d in ast.walk(HUB_MAIN) if isinstance(d, ast.Dict)
                 and any(isinstance(k, ast.Constant) and k.value == "type" and
                         isinstance(v, ast.Constant) and v.value == "wake_words"
                         for k, v in zip(d.keys, d.values))]
    assert len(published) == 1, "voice-satellites publishes no wake_words event"
    assert 'ev.type === "wake_words"' in CODE
    assert "words" in dict_keys(published[0]) and "ev.words" in CODE


def test_every_satellite_is_spelt_the_same_on_both_sides():
    """"*" is the one word that reaches a satellite adopted later. The page
    writes it and tests for it; the hub decides with it."""
    every = [n.value.value for n in HUB_WORDS.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "EVERY" for t in n.targets)]
    assert every == ["*"]
    assert 'satellites.includes("*")' in page_function("wakeHas")


# ---- what a word does: modes, destinations, the hub's own patterns ----------
#
# The page restates what router.Behaviour and destinations.py accept, so it
# can say what is wrong before a Save rather than show a 422 after one. Each
# restatement is read against the hub's source here, so a field renamed or a
# pattern tightened on either side fails a test instead of a Save.

HUB_ROUTER = ast.parse((HUB / "router.py").read_text())
HUB_DESTINATIONS = ast.parse((HUB / "destinations.py").read_text())
HUB_WAKEWORD = ast.parse((HUB / "wakeword.py").read_text())
MARKUP = re.sub(r"<!--.*?-->", "", PAGE, flags=re.S)
WORD_MARKUP = page_function("wakeRowMarkup")


def module_string(tree: ast.Module, name: str) -> str:
    """The string a module-level NAME = r"..." (or re.compile(r"...")) holds."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name
                                                for t in node.targets):
            value = node.value.args[0] if isinstance(node.value, ast.Call) else node.value
            assert isinstance(value, ast.Constant), f"{name} is not a literal any more"
            return value.value
    raise AssertionError(f"voice-satellites has no {name}; the contract moved")


def page_regex(name: str) -> str:
    """The source of a page `const NAME = /.../;`, as Python would write it."""
    found = re.search(r"const " + name + r" = /(.*)/;\n", CODE)
    assert found, f"{name} is gone from the page"
    return found.group(1).replace("\\/", "/")


def fields(cls: ast.ClassDef) -> set[str]:
    return {a.target.id for a in cls.body if isinstance(a, ast.AnnAssign)
            and isinstance(a.target, ast.Name)}


def default(cls: ast.ClassDef, name: str):
    for a in cls.body:
        if isinstance(a, ast.AnnAssign) and a.target.id == name:
            call = a.value
            if isinstance(call, ast.Call):
                return next(k.value.value for k in call.keywords if k.arg == "default")
            return call.value
    raise AssertionError(f"{cls.name}.{name} is gone")


def destination_types() -> dict[str, ast.ClassDef]:
    """type literal -> the destination class that owns it."""
    out = {}
    for node in HUB_DESTINATIONS.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for a in node.body:
            if (isinstance(a, ast.AnnAssign) and a.target.id == "type"
                    and isinstance(a.annotation, ast.Subscript)):
                out[a.annotation.slice.value] = node
    assert len(out) >= 5, "the destination reader found too few types"
    return out


def test_the_three_modes_are_the_hubs():
    mode = next(n for n in HUB_ROUTER.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "Mode" for t in n.targets))
    hub = {e.value for e in mode.value.slice.elts}
    assert hub == {"command", "conversation", "trigger"}
    assert set(re.findall(r'data-mode="(\w+)"', WORD_MARKUP)) == hub
    table = re.search(r"const WAKE_MODE_WORD = \{([^}]*)\}", CODE).group(1)
    assert set(re.findall(r"(\w+):", table)) == hub


def test_every_destination_the_page_offers_is_one_the_hub_takes_and_all_are_offered():
    hub = set(destination_types())
    select = WORD_MARKUP[WORD_MARKUP.index('data-f="dest"'):]
    select = select[:select.index("</select>")]
    assert set(re.findall(r'<option value="(\w+)"', select)) == hub
    table = re.search(r"const WAKE_DEST_WORD = \{([^}]*)\}", CODE).group(1)
    assert set(re.findall(r"(\w+):", table)) == hub


def test_every_destination_field_the_page_writes_is_one_that_type_has():
    """destinations.py forbids unknown fields: a field sent to the wrong type
    is a 422, and one misspelt is too. data-dest says which types show a
    field; "env" is the NAME of the secret's variable, token_env or
    api_key_env by type."""
    types = destination_types()
    shown = re.findall(r'data-dest="([^"]+)">.*?data-f="d\.(\w+)"', WORD_MARKUP, re.S)
    assert len(shown) >= 7, "the field reader stopped finding fields"
    for owners, field in shown:
        for kind in owners.split():
            name = field if field != "env" else "api_key_env" if kind == "llm" else "token_env"
            assert name in fields(types[kind]), f"{kind} has no {name}, which the page sends it"
    # And what wakeSetDest writes when it starts a type from nothing.
    for field in ("url", "token_env", "base_url", "model", "api_key_env"):
        assert f"{field}" in page_function("wakeSetDest")


def test_the_page_checks_what_the_hub_checks_with_the_hubs_own_patterns():
    assert page_regex("WAKE_LANG") == module_string(HUB_ROUTER, "LANGUAGE")
    assert page_regex("WAKE_ENV") == module_string(HUB_DESTINATIONS, "ENV_NAME")
    assert page_regex("WAKE_URL") == module_string(HUB_DESTINATIONS, "HTTP_URL")
    assert page_regex("WAKE_NAME") == module_string(HUB_WAKEWORD, "NAME")
    # The secret goes in as a name, and the hub's defaults are the page's.
    types = destination_types()
    for kind in ("ha_assist", "ha_conversation"):
        assert default(types[kind], "token_env") in page_function("wakeSetDest")
    assert default(types["llm"], "api_key_env") in page_function("wakeSetDest")


def test_what_the_page_shows_for_an_unset_setting_is_the_hubs_default():
    """A new word sends no conversation or trigger block, so the hub's
    defaults apply; the page shows those numbers, and they must be the same."""
    router = {n.name: n for n in HUB_ROUTER.body if isinstance(n, ast.ClassDef)}
    shown = re.search(r"const WAKE_SHOWN = \{([^}]*)\}", CODE).group(1)
    shown = dict(re.findall(r'(\w+): "?([\w.]+)"?', shown))
    assert float(shown["follow_up_s"]) == default(router["ConversationSettings"], "follow_up_s")
    assert float(shown["cooldown_s"]) == default(router["TriggerSettings"], "cooldown_s")
    assert shown["feedback"] == default(router["TriggerSettings"], "feedback")


def test_try_a_word_sends_exactly_what_the_routing_test_takes():
    body = hub_class("TryBody", HUB_ROUTER)
    taken = {a.target.id for a in body.body if isinstance(a, ast.AnnAssign)}
    call = page_function("wakeTry")
    call = call[call.index('json("/satellites/routing/test"'):]
    literal = call[call.index("JSON.stringify({") + len("JSON.stringify({"):call.index("})")]
    # `key: value` and the shorthand `key`, which is a field all the same.
    sent = set(re.findall(r"(\w+)\s*:", literal))
    sent |= set(re.findall(r"(?:^|,)\s*(\w+)\s*(?=,|$)", literal.strip()))
    assert sent == taken


def published(kind: str) -> set[str]:
    """Every key of every dict literal the hub publishes as `kind`, with the
    `| {...}` a turn or a routed reply is built from followed."""
    keys: set[str] = set()
    for d in ast.walk(HUB_MAIN):
        if isinstance(d, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "type" and isinstance(v, ast.Constant)
                and v.value == kind for k, v in zip(d.keys, d.values)):
            keys |= {k.value for k in d.keys if isinstance(k, ast.Constant)}
    assert keys, f"voice-satellites publishes no {kind} event"
    return keys


def test_the_events_the_page_logs_are_the_ones_the_hub_publishes():
    """Named differently on the two sides, a conversation would be logged as
    its bare type, and a row would stay Listening after a turn."""
    for kind, reads in (("conversation_started", ("rule_id", "wake_word", "reason", "from_rule")),
                        ("conversation_ended", ("reason", "turns")),
                        ("triggered", ("wake_word", "score", "satellite")),
                        ("turn", ("turn", "ended", "handed_over_to"))):
        assert f'"{kind}"' in CODE, f"the page does not handle {kind}"
        missing = set(reads) - published(kind)
        assert not missing, f"the page reads {missing} from {kind}, which the hub never sends"
    # A turn and a routed reply share their fields (`common`), which carry the
    # timeline the log reads.
    common = next(n for n in ast.walk(HUB_MAIN) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "common" for t in n.targets))
    assert {"rule_id", "transcript", "language", "timeline_ms", "timings_ms"} <= dict_keys(common)


def test_every_end_the_page_names_is_one_the_hub_gives():
    """A conversation's reason is a word the hub picks in several places; the
    page's names for them must not be for reasons that never come."""
    reasons = {"cancelled"}
    for node in ast.walk(HUB_MAIN):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "cancel" and node.args
                and isinstance(node.args[0], ast.Constant)):
            reasons.add(node.args[0].value)
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "reason"
                                                 for t in node.targets)):
            reasons |= {c.value for c in ast.walk(node.value)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    table = re.search(r"const SAT_ENDED = \{([^}]*)\}", CODE).group(1)
    named = set(re.findall(r"(\w+):", table))
    assert named <= reasons, named - reasons
    assert {"silence", "phrase", "stop"} <= named


def test_a_rows_conversation_and_latency_are_read_by_the_names_the_hub_uses():
    hub = hub_class("Hub")
    assert {"turns", "p50_first_audio_ms"} <= dict_keys(method(hub, "latency_summary"))
    assert '"latency": self.latency_summary(nid)' in (HUB / "main.py").read_text()
    conversation = hub_class("Conversation")
    assert {"rule_id", "turns"} <= dict_keys(method(conversation, "session_view"))
    assert '"session": conv.session_view()' in (HUB / "main.py").read_text()
    assert "l.p50_first_audio_ms" in page_function("satLatency")
    state = page_function("satState")
    assert "ear.session" in state and "talk.rule_id" in state and "talk.turns" in state
