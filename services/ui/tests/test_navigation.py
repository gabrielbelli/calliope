"""The page's addresses, read and written by the page's own code.

Every tab and every place inside one has a path of its own (/ui/jobs/<id>,
/ui/satellites/kitchen/airplay), and the page reads its location to open it.
The part that turns a path into a place and back is pure: the source between
the router's "addresses" and "page" markers, which touches no element and
calls nothing at the top level. So these run that source unchanged in Node,
with nothing stubbed, and ask it questions. Without `node` on PATH they skip,
with the reason; test_satellites_writes.py does the same.

What they prevent:

  * an address that does not come back to itself: parsed, shaped and written
    again, it must be the same string, or every reload moves the reader;
  * a tail the tab has no place for being kept, or guessed at;
  * a segment that is not a name (a percent sign cut short, a 200-character
    run) reaching the code that opens rows;
  * two satellites with one address, or one satellite that cannot be found by
    the address it was given before a rename;
  * a title that leads with "Calliope", which is all a narrow browser tab
    shows.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "app" / "static" / "ui.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH; these drive the "
                                "page's own JavaScript and need a JavaScript runtime")

# The router's pure half, and the one scenario, in one eval. Nothing is
# stubbed: if the half grows a reference to the DOM, these fail with it.
HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const from = html.indexOf("/* ------------------------------------------------- router: addresses -- */");
const to = html.indexOf("/* ------------------------------------------------------- router: page -- */");
if (from < 0 || to < 0 || to < from) throw new Error("the router's markers moved");
const SECTION = html.slice(from, to);
const SCENARIO = fs.readFileSync(process.argv[3], "utf8");
const round = (path, search) => navPath(navShape(navParse(path, search || "")));
eval(SECTION + "\n;(() => {\n" + SCENARIO + "\n})();");
"""


def run(tmp_path: Path, scenario: str):
    """The scenario's last line prints one JSON value; that value."""
    (tmp_path / "harness.js").write_text(HARNESS)
    (tmp_path / "scenario.js").write_text(scenario)
    done = subprocess.run([NODE, str(tmp_path / "harness.js"), str(PAGE),
                           str(tmp_path / "scenario.js")],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout.strip().splitlines()[-1])


# Every address the page writes, as it writes it (the table in the README).
CANONICAL = [
    "/ui",
    "/ui/transcribe/expert",
    "/ui/speak",
    "/ui/speak?voice=c%3Agabriel",
    "/ui/speak/clone",
    "/ui/speak/expert",
    "/ui/jobs",
    "/ui/jobs?show=failed&kind=clone",
    "/ui/jobs/0123456789abcdef",
    "/ui/jobs/0123456789abcdef?show=failed",
    "/ui/vocabulary",
    "/ui/vocabulary/tech",
    "/ui/satellites",
    "/ui/satellites/kitchen",
    "/ui/satellites/020000000003",
    "/ui/satellites/lounge/airplay",
    "/ui/satellites/kitchen/try",
    "/ui/satellites/kitchen/buttons",
    "/ui/satellites/kitchen/device",
    "/ui/satellites/wake-words",
    "/ui/satellites/wake-words/hey_jarvis",
    "/ui/satellites/wake-words/ptt",
    "/ui/satellites/wake-words/hey_jarvis/more",
    "/ui/satellites/try-a-word",
    "/ui/satellites/custom-models",
    "/ui/satellites/activity",
    "/ui/satellites/telemetry",
    "/ui/satellites/firmware",
]


def test_every_tab_has_an_address_and_transcribe_is_the_bare_page(tmp_path):
    """/ui is what every bookmark made before the addresses points at, so it
    stays Transcribe's, and /ui/transcribe is only another spelling of it.
    The Vocabulary tab's data-tab is `vocab`; its address says the word."""
    got = run(tmp_path, """
      const tabs = ["transcribe", "speak", "jobs", "vocab", "satellites"];
      console.log(JSON.stringify({
        paths: tabs.map(view => navPath({ view, parts: [], query: {} })),
        views: ["/ui", "/ui/transcribe", "/ui/speak", "/ui/jobs", "/ui/vocabulary", "/ui/satellites"]
          .map(p => navParse(p, "").view),
        spelling: round("/ui/transcribe") }));
    """)
    assert got["paths"] == ["/ui", "/ui/speak", "/ui/jobs", "/ui/vocabulary", "/ui/satellites"], got
    assert got["views"] == ["transcribe", "transcribe", "speak", "jobs", "vocab", "satellites"], got
    assert got["spelling"] == "/ui", got


@pytest.mark.parametrize("address", CANONICAL)
def test_an_address_round_trips_through_parse_shape_and_path(tmp_path, address):
    """Read and written again, an address the page wrote is the same string,
    or a reload would move the reader somewhere else."""
    path, _, search = address.partition("?")
    got = run(tmp_path, f"console.log(JSON.stringify(round({json.dumps(path)}, {json.dumps(search)})));")
    assert got == address


def test_an_unknown_tail_is_dropped_and_an_unknown_tab_is_transcribe(tmp_path):
    """A tail the tab has no place for is dropped rather than guessed at, and
    a query key the tab does not read is not carried."""
    got = run(tmp_path, """
      console.log(JSON.stringify({
        transcribe: round("/ui/transcribe/nope/deeper"),
        speak: round("/ui/speak/nope"),
        speakQuery: round("/ui/speak", "voice=k%3Aaf_heart&show=failed"),
        jobs: round("/ui/jobs/abc/def", "show=failed&voice=x"),
        word: round("/ui/satellites/wake-words/alexa/extra"),
        hub: round("/ui/satellites/firmware/extra"),
        part: round("/ui/satellites/kitchen/nope"),
        nope: navParse("/ui/nope/deeper", "").view,
        outside: navParse("/elsewhere/ui/jobs", "").view }));
    """)
    assert got["transcribe"] == "/ui"
    assert got["speak"] == "/ui/speak"
    assert got["speakQuery"] == "/ui/speak?voice=k%3Aaf_heart"
    assert got["jobs"] == "/ui/jobs/abc?show=failed"
    assert got["word"] == "/ui/satellites/wake-words/alexa"
    assert got["hub"] == "/ui/satellites/firmware"
    assert got["part"] == "/ui/satellites/kitchen"
    assert got["nope"] == "transcribe" and got["outside"] == "transcribe"


def test_a_percent_encoded_segment_is_decoded_once_and_a_broken_one_ends_the_path(tmp_path):
    """%2520 is a literal %20 in a name, not a space; a segment that will not
    decode, or is longer than any name here, ends the path there."""
    got = run(tmp_path, """
      console.log(JSON.stringify({
        once: navParse("/ui/vocabulary/a%2520b", "").parts,
        space: navParse("/ui/satellites/sala%20de%20estar", "").parts,
        broken: navParse("/ui/satellites/%E0%A4%A/airplay", "").parts,
        long: navParse("/ui/jobs/" + "a".repeat(129), "").parts,
        fits: navParse("/ui/jobs/" + "a".repeat(128), "").parts.length,
        written: navPath({ view: "vocab", parts: ["a b/c"], query: {} }),
        voice: navParse("/ui/speak", "voice=c:gabriel").query.voice,
        voiceWritten: round("/ui/speak", "voice=c:gabriel") }));
    """)
    assert got["once"] == ["a%20b"]
    assert got["space"] == ["sala de estar"]
    assert got["broken"] == []
    assert got["long"] == [] and got["fits"] == 1
    assert got["written"] == "/ui/vocabulary/a%20b%2Fc"
    # A voice typed with a bare colon is read as it is and written encoded.
    assert got["voice"] == "c:gabriel"
    assert got["voiceWritten"] == "/ui/speak?voice=c%3Agabriel"


def test_a_name_becomes_a_slug_of_lower_case_ascii(tmp_path):
    got = run(tmp_path, """
      console.log(JSON.stringify([satSlug("Pi Edifier"), satSlug("Sala de Estar ç"),
        satSlug("  --Kitchen!!  "), satSlug("x".repeat(70) + " y").length, satSlug("")]));
    """)
    assert got[:3] == ["pi-edifier", "sala-de-estar-c", "kitchen"]
    assert got[3] <= 64
    assert got[4] == ""


def test_a_satellite_is_named_by_its_slug_unless_that_is_empty_reserved_id_shaped_or_shared(tmp_path):
    """The name is the address people can read; the ID is the one that is
    always right. The ID is used whenever the name would be wrong: nothing to
    slug, a hub section's own name, a twelve-hex string that could be another
    satellite's ID, or two satellites that slug the same."""
    got = run(tmp_path, """
      const sat = (id, name, adopted = true) => ({ id, name, adopted });
      const list = [sat("aaaaaaaaaaaa", "Kitchen"), sat("bbbbbbbbbbbb", "Firmware"),
                    sat("cccccccccccc", "!!!"), sat("dddddddddddd", "0123456789ab"),
                    sat("eeeeeeeeeeee", "Sala"), sat("ffffffffffff", "sala"),
                    sat("111111111111", "New", false)];
      console.log(JSON.stringify(list.map(n => navSatSegment(n, list))));
    """)
    assert got == ["kitchen", "bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd",
                   "eeeeeeeeeeee", "ffffffffffff", "111111111111"]


def test_a_segment_finds_a_satellite_by_id_then_slug_then_name(tmp_path):
    got = run(tmp_path, """
      const list = [{ id: "aaaaaaaaaaaa", name: "Sala de Estar", adopted: true },
                    { id: "bbbbbbbbbbbb", name: "Kitchen", adopted: true },
                    { id: "cccccccccccc", name: "Kitchen", adopted: true },
                    { id: "dddddddddddd", name: "Hall", adopted: false }];
      const find = s => { const n = navSatFind(s, list); return n ? n.id : null; };
      console.log(JSON.stringify({ id: find("BBBBBBBBBBBB"), slug: find("sala-de-estar"),
        name: find("Sala de Estar"), shared: find("kitchen"), pending: find("hall"),
        none: find("garage") }));
    """)
    assert got["id"] == "bbbbbbbbbbbb"
    assert got["slug"] == "aaaaaaaaaaaa" and got["name"] == "aaaaaaaaaaaa"
    assert got["shared"] is None, "two satellites share the name, so the name finds neither"
    assert got["pending"] == "dddddddddddd", "a waiting satellite is found by the name it reports"
    assert got["none"] is None


def test_the_title_puts_the_most_specific_part_first(tmp_path):
    got = run(tmp_path, """
      console.log(JSON.stringify([navTitleText(["", "Lounge", "AirPlay", "Satellites"]),
        navTitleText(["3m left", "", "", "Transcribe"]), navTitleText(["", "", "", "Jobs"])]));
    """)
    assert got == ["Lounge · AirPlay · Satellites · Calliope",
                   "3m left · Transcribe · Calliope", "Jobs · Calliope"]


def test_closing_a_word_tool_goes_back_to_wake_words_not_the_list(tmp_path):
    """Try a word and Custom models are drawn inside Wake words, so closing
    either leaves Wake words open, and that is where the address goes."""
    got = run(tmp_path, """
      const up = (view, parts, query) => navPath(navParent({ view, parts, query: query || {} }));
      console.log(JSON.stringify({ tryit: up("satellites", ["try-a-word"]),
        models: up("satellites", ["custom-models"]), activity: up("satellites", ["activity"]),
        more: up("satellites", ["wake-words", "alexa", "more"]),
        part: up("satellites", ["lounge", "airplay"]),
        job: up("jobs", ["abc"], { show: "failed" }),
        expert: up("speak", ["expert"], { voice: "k:af_heart" }) }));
    """)
    assert got["tryit"] == "/ui/satellites/wake-words"
    assert got["models"] == "/ui/satellites/wake-words"
    assert got["activity"] == "/ui/satellites"
    assert got["more"] == "/ui/satellites/wake-words/alexa"
    assert got["part"] == "/ui/satellites/lounge"
    assert got["job"] == "/ui/jobs?show=failed", "the list keeps the filter it was showing"
    assert got["expert"] == "/ui/speak"
