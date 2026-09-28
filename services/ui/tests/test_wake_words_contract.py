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


def destination_fields() -> list[tuple[str, str]]:
    """(owners, field) for each d.* field in a word's row, read from the
    markup between its data-dest and the next one. The reader used to match
    `data-dest=... .*? data-f="d.x"` lazily across elements, so a block with
    no d.* field of its own (the key box, the Test button, the provider
    picker) was read as owning the next block's field: an llm block placed
    before the Assist pipeline would have read as "llm has pipeline"."""
    marks = [(m.start(), m.group(1)) for m in re.finditer(r'data-dest="([^"]+)"', WORD_MARKUP)]
    out = []
    for i, (at, owners) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(WORD_MARKUP)
        out += [(owners, f) for f in re.findall(r'data-f="d\.(\w+)"', WORD_MARKUP[at:end])]
    # Every field is inside some data-dest, or it would be read as nobody's.
    assert len(out) == WORD_MARKUP.count('data-f="d.'), "a d.* field sits outside every data-dest"
    return out


def test_every_destination_field_the_page_writes_is_one_that_type_has():
    """destinations.py forbids unknown fields: a field sent to the wrong type
    is a 422, and one misspelt is too. data-dest says which types show a
    field; "env" is the NAME of the secret's variable, token_env or
    api_key_env by type."""
    types = destination_types()
    shown = destination_fields()
    assert len(shown) >= 8, "the field reader stopped finding fields"
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
    assert float(shown["silence_ms"]) == default(router["Behaviour"], "silence_ms")
    assert float(shown["follow_silence_ms"]) == default(router["ConversationSettings"], "silence_ms")


def test_the_pauses_the_page_allows_are_the_hubs():
    """The pause that ends a command and the one that ends a follow-up: the
    page's range is the hub's Field bounds for both."""
    router = {n.name: n for n in HUB_ROUTER.body if isinstance(n, ast.ClassDef)}
    pause = dict(re.findall(r"(low|high): (\d+)", re.search(r"const WAKE_PAUSE = \{([^}]*)\}", CODE).group(1)))

    def bounds(cls: ast.ClassDef) -> tuple[int, int]:
        for a in cls.body:
            if isinstance(a, ast.AnnAssign) and a.target.id == "silence_ms":
                kw = {k.arg: k.value.value for k in a.value.keywords}
                return kw["ge"], kw["le"]
        raise AssertionError(f"{cls.name}.silence_ms is gone")

    for cls in ("Behaviour", "ConversationSettings"):
        assert bounds(router[cls]) == (int(pause["low"]), int(pause["high"])), cls


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


# ---- a language model word: its key, its model list, its Test -----------------------

HUB_SECRETS = ast.parse((HUB / "secret_store.py").read_text())


def hub_function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    found = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == name]
    assert found, f"voice-satellites has no {name}; the contract moved"
    return found[0]


def sent_keys(function: str, call: str) -> set[str]:
    """The keys of the object literal a page function sends in `call`'s
    JSON.stringify({...}), shorthand ones included."""
    body = page_function(function)
    body = body[body.index(call):]
    literal = body[body.index("JSON.stringify({") + len("JSON.stringify({"):body.index("})")]
    keys = set(re.findall(r"(\w+)\s*:", literal))
    keys |= set(re.findall(r"(?:^|,)\s*(\w+)\s*(?=,|$)", literal.strip()))
    return keys


def field_call(cls: ast.ClassDef, name: str) -> dict:
    """The keywords of a Field(...) (or a StringConstraints inside an
    Annotated) that declares `name`, as Python values."""
    for a in cls.body:
        if isinstance(a, ast.AnnAssign) and a.target.id == name:
            calls = [c for c in ast.walk(a) if isinstance(c, ast.Call)]
            return {k.arg: ast.literal_eval(k.value) for c in calls for k in c.keywords
                    if isinstance(k.value, ast.Constant)}
    raise AssertionError(f"{cls.name}.{name} is gone")


def test_the_key_the_page_stores_is_shaped_as_the_hub_takes_it():
    """PUT /satellites/secrets refuses a field it does not know and a value
    it would not send; the page checks a pasted key with the hub's own
    pattern before it goes, and sends exactly the two fields."""
    assert sent_keys("wakeKeyPut", 'json("/satellites/secrets"') == fields(hub_class("SecretBody"))
    assert page_regex("WAKE_SECRET") == module_string(HUB_SECRETS, "SECRET_VALUE")
    assert '@app.put("/satellites/secrets")' in (HUB / "main.py").read_text()
    assert "WAKE_SECRET.test(value)" in page_function("wakeKeyStore")


def test_the_model_list_request_is_what_the_hub_takes():
    body = hub_class("LlmModelsBody", HUB_ROUTER)
    assert sent_keys("wakeModelsFetch", 'json("/satellites/llm/models"') == fields(body)
    # The name a picker asks with by default is the one a saved action sends.
    assert default(body, "api_key_env") == default(destination_types()["llm"], "api_key_env")
    # And the page reads what the route answers.
    answered = dict_keys(hub_function(HUB_ROUTER, "llm_models"))
    assert set(re.findall(r"\br\.(\w+)", page_function("wakeModelsFetch"))) <= answered


def test_the_test_sends_a_destination_the_hub_validates_as_llm():
    """POST /satellites/llm/test takes the destination itself, validated as
    the Llm model a Save validates it as; the page sends the draft
    destination whole, and only an llm one."""
    route = hub_function(HUB_ROUTER, "llm_test")
    annotation = next(a.annotation for a in route.args.args if a.arg == "body")
    assert isinstance(annotation, ast.Name) and annotation.id == "Llm"
    assert 'body: JSON.stringify(destination) })' in page_function("wakeLlmTest")
    try_it = page_function("wakeLlmTry")
    assert 'if (!d || d.type !== "llm") return;' in try_it and "wakeLlmTest(wakeClone(d))" in try_it
    answered = dict_keys(route)
    read = set(re.findall(r"\br\.(\w+)", page_function("wakeLlmTest")))
    assert read and read <= answered, read - answered


def test_every_preset_is_an_address_the_hub_takes():
    table = CODE[CODE.index("const LLM_PRESETS = ["):]
    table = table[:table.index("];")]
    presets = re.findall(r'\["([^"]*)", "([^"]+)"\]', table)
    assert len(presets) >= 6, "the preset reader stopped finding presets"
    urls = [url for url, _ in presets if url]
    assert len(urls) == len(presets) - 1, "there is not exactly one Other"
    for url in urls:
        assert re.fullmatch(module_string(HUB_DESTINATIONS, "HTTP_URL"), url), url
        # Saved as it is shown: the hub strips a trailing slash and a
        # /chat/completions, so a preset with either would never read as chosen.
        assert not url.endswith("/") and not url.endswith("/chat/completions"), url


def test_the_reply_limit_bounds_are_the_hubs():
    hub = field_call(destination_types()["llm"], "max_tokens")
    box = WORD_MARKUP[WORD_MARKUP.index('data-f="d.max_tokens"'):]
    box = box[:box.index(">")]
    assert f'min="{hub["ge"]}" max="{hub["le"]}" step="1" placeholder="{hub["default"]}"' in box
    assert f'wakeWithin(d.max_tokens, {hub["ge"]}, {hub["le"]})' in page_function("wakeProblem")


def test_the_key_sources_the_page_names_are_the_ones_the_hub_reports():
    """GET /satellites/wake-words says where each key lives. A word spelt
    differently on the two sides would read as "no key stored", and offer
    Store for a key the environment holds."""
    assert "secrets" in ANSWER_FIELDS and "WAKE.server.secrets" in CODE
    reported = {c.value for c in ast.walk(hub_function(HUB_ROUTER, "secret_sources"))
                if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    named = set(re.findall(r'where === "(\w+)"', page_function("wakeKeyState")))
    assert named == {"environment", "hub"} and named <= reported
