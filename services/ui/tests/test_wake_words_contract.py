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
