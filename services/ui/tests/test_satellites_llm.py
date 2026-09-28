"""A language model word, run rather than read: its provider, its model list,
its key and its Test.

These drive the page's own Satellites section through test_satellites_writes.py's
harness, in Node, against its fake hub. Nothing starts a server and nothing
reaches the network; without `node` on PATH these skip. The harness hands out
a fresh stand-in for every querySelector, so what a row shows cannot be read
back: the logic sits in functions that take plain values (wakeField,
wakeKeyState, wakeKeyPut, wakeLlmTest, wakeModelsWant), and those are what is
driven. A key box is played by a plain object whose value can be read.

What they prevent:

  * the form reading as one server's (an Ollama address, a llama3 model);
  * a preset that changes the key's name, so a key already set is lost;
  * the model list asked on every poll, or while an address is typed, or not
    asked again once a key is stored;
  * a key kept anywhere on the page after it is stored: in the draft, a Save,
    a note or the browser's storage;
  * a key stored under a name the environment sets, where it would never be
    sent, or cleared without a question;
  * a Test that saves, or that tests the saved action instead of the form.
"""

from test_satellites_writes import pytestmark, run  # noqa: F401

KEY = "sk-test-do-not-leak-5e1f"
NAME = "SATELLITES_LLM_API_KEY"

LLM_HUB = """
hub.env = { SATELLITES_LLM_API_KEY: false };
hub.secrets = {};
const llmWord = (name, extra) => ({
  name, threshold: 0.5, satellites: ["*"], mode: "conversation", language: null,
  action: { destination: { type: "llm", base_url: "https://llm.example.com/v1", model: "gpt-test-mini",
                           system: null, api_key_env: "SATELLITES_LLM_API_KEY", max_tokens: 400,
                           timeout: 30, stream: true, ...(extra || {}) },
            reply_to: "same", voice: null, fallback: null },
  silence_ms: 800, conversation: { follow_up_s: 8, silence_ms: 600, end_phrases: null },
  trigger: { feedback: "earcon", cooldown_s: 3, ends_conversation: false }, state: "ready", error: null });
hub.words = [llmWord("hey_jarvis")];
hub.ptt = { mode: "command", language: null,
            action: { destination: { type: "echo" }, reply_to: "same", voice: null, fallback: null },
            silence_ms: 800, conversation: { follow_up_s: 8, silence_ms: 600, end_phrases: null },
            trigger: { feedback: "earcon", cooldown_s: 3, ends_conversation: false } };
const settle = () => new Promise(r => setTimeout(r, 30));
const dest = name => (WAKE.draft || WAKE.server.words).find(w => w.name === name).action.destination;
const sent = name => hub.puts[hub.puts.length - 1].find(w => w.name === name);
const problemOf = name => wakeProblem(WAKE.draft.find(w => w.name === name), wakeEffective());
// A row whose key box can be read back; everything else in it is a stand-in.
const keyRow = box => ({ querySelector: sel => sel.includes("password") ? box : stand() });
"""


def test_a_preset_fills_the_base_url_and_a_typed_address_reads_as_other(tmp_path):
    got = run(tmp_path, LLM_HUB + """
      await satellitesRefresh(); await settle();
      wakeEdit("hey_jarvis", w => wakeField(w, "preset", "https://openrouter.ai/api/v1"));
      const picked = dest("hey_jarvis").base_url, shows = wakePreset(picked);
      const keyName = dest("hey_jarvis").api_key_env;
      wakeEdit("hey_jarvis", w => wakeField(w, "d.base_url", " https://llm.example.com/v1/chat/completions/ "));
      const typed = dest("hey_jarvis").base_url, other = wakePreset(typed);
      wakeEdit("hey_jarvis", w => wakeField(w, "preset", ""));
      console.log(JSON.stringify({ picked, shows, keyName, typed, other,
                                   cleared: dest("hey_jarvis").base_url, needs: problemOf("hey_jarvis") }));
    """)
    assert got["picked"] == got["shows"] == "https://openrouter.ai/api/v1", got
    assert got["keyName"] == NAME, "a preset changed the key's name"
    # The curl example's endpoint, cut back to its base as the hub cuts it.
    assert got["typed"] == "https://llm.example.com/v1" and got["other"] == "", got
    # Other is an address of your own: emptied, and named as the fix.
    assert got["cleared"] == ""
    assert got["needs"] == "Write the address in full, starting with https or http."


def test_the_models_are_asked_once_per_address_and_key_name_and_a_failure_leaves_free_text(tmp_path):
    got = run(tmp_path, LLM_HUB + """
      await satellitesRefresh(); await settle();
      const first = hub.modelCalls.length;
      await satellitesRefresh(); await settle();
      const afterPoll = hub.modelCalls.length;
      const entry = WAKE_MODELS.get(wakeModelsKey("https://llm.example.com/v1", "SATELLITES_LLM_API_KEY"));
      const ready = wakeModelsHint(dest("hey_jarvis"), entry);
      wakeEdit("hey_jarvis", w => wakeField(w, "d.env", "OPENROUTER_API_KEY")); await settle();
      const afterName = hub.modelCalls.length;
      // Typing: nothing is asked, whatever the address looks like so far.
      wakeModelsWant({ base_url: "https://api.half.example.co", api_key_env: null }, true);
      const whileTyping = hub.modelCalls.length;
      hub.modelsFail = "the server answered 404: 404 page not found";
      wakeEdit("hey_jarvis", w => wakeField(w, "d.base_url", "https://other.example.com/v1")); await settle();
      const failed = WAKE_MODELS.get(wakeModelsKey("https://other.example.com/v1", "OPENROUTER_API_KEY"));
      const hint = wakeModelsHint(dest("hey_jarvis"), failed);
      wakeEdit("hey_jarvis", w => wakeField(w, "d.model", "my-own-model"));
      await wakeSave();
      console.log(JSON.stringify({ first, afterPoll, models: entry.models, ready, afterName, whileTyping,
                                   calls: hub.modelCalls, hint, saved: sent("hey_jarvis").action.destination }));
    """)
    assert (got["first"], got["afterPoll"]) == (1, 1), "a poll asked again"
    assert got["models"] == ["gpt-test-mini", "vendor/test-large"]
    assert got["ready"] == "2 models to pick from; type to narrow the list."
    assert got["afterName"] == 2 and got["whileTyping"] == 2, got
    assert got["calls"][:2] == [
        {"base_url": "https://llm.example.com/v1", "api_key_env": NAME},
        {"base_url": "https://llm.example.com/v1", "api_key_env": "OPENROUTER_API_KEY"}]
    assert got["hint"] == ("Could not list the models, so type the id: the server answered 404: "
                           "404 page not found")
    # The list only ever suggests: a typed id is saved as it is.
    assert got["saved"]["model"] == "my-own-model"
    assert got["saved"]["base_url"] == "https://other.example.com/v1"


def test_a_stored_key_is_sent_once_and_is_nowhere_on_the_page_afterwards(tmp_path):
    got = run(tmp_path, LLM_HUB + f"""
      const KEY = "{KEY}";
      await satellitesRefresh(); await settle();
      const before = hub.modelCalls.length;
      const box = {{ value: "  " + KEY + "\\n" }};
      await wakeKeyStore("SATELLITES_LLM_API_KEY", keyRow(box), stand());
      await settle();
      const state = wakeKeyState("SATELLITES_LLM_API_KEY", "hey_jarvis");
      wakeEdit("hey_jarvis", w => wakeField(w, "d.model", "vendor/test-large"));
      await wakeSave();
      const page = JSON.stringify([WAKE, WAKE.server, WAKE.draft, WAKE.draftPtt, [...WAKE.stash],
                                   [...WAKE_MODELS], [...saved], notes, asked, hub.bodies]);
      console.log(JSON.stringify({{ sent: hub.secretCalls, emptied: box.value, onPage: page.includes(KEY),
                                   state, askedAgain: hub.modelCalls.length - before, notes }}));
    """)
    # Trimmed, sent once, in the body the hub takes.
    assert got["sent"] == [{"name": NAME, "value": KEY}], got
    assert got["emptied"] == "", "the box still holds the key"
    assert got["onPage"] is False, "the key is somewhere on the page"
    assert got["state"] == {"hint": f"A key is stored on the hub as {NAME}.", "canStore": True,
                            "canClear": True}
    # A model list asked without the key may have been refused: asked again.
    assert got["askedAgain"] == 1, got
    assert got["notes"] == []


def test_a_key_that_is_empty_or_malformed_is_never_sent(tmp_path):
    got = run(tmp_path, LLM_HUB + """
      await satellitesRefresh(); await settle();
      const empty = { value: "   " }, spaced = { value: "sk-test two words" };
      await wakeKeyStore("SATELLITES_LLM_API_KEY", keyRow(empty), stand());
      await wakeKeyStore("SATELLITES_LLM_API_KEY", keyRow(spaced), stand());
      console.log(JSON.stringify({ sent: hub.secretCalls, notes, emptied: spaced.value }));
    """)
    assert got["sent"] == []
    assert got["notes"] == [["bad", "A key has no spaces or line breaks; check what was pasted."]]
    assert got["emptied"] == ""


def test_a_key_the_environment_sets_cannot_be_replaced_from_the_page(tmp_path):
    got = run(tmp_path, LLM_HUB + """
      hub.secrets = { SATELLITES_LLM_API_KEY: "environment" };
      hub.env = { SATELLITES_LLM_API_KEY: true };
      hub.environment = ["SATELLITES_LLM_API_KEY", "OPENROUTER_API_KEY"];
      await satellitesRefresh(); await settle();
      const state = wakeKeyState("SATELLITES_LLM_API_KEY", "hey_jarvis");
      // A name no saved action reads yet, which the environment also sets:
      // the page cannot know, so the hub says.
      const unknown = wakeKeyState("OPENROUTER_API_KEY", "hey_jarvis");
      const box = { value: "sk-test-another-one" };
      await wakeKeyStore("OPENROUTER_API_KEY", keyRow(box), stand());
      console.log(JSON.stringify({ state, unknown, noName: wakeKeyState(null), notes, emptied: box.value }));
    """)
    assert got["state"] == {"hint": f"{NAME} is set in the hub's environment, which a stored key "
                                    "cannot replace.", "canStore": False, "canClear": False}
    assert got["unknown"]["hint"] == ("Store a key as OPENROUTER_API_KEY, or save to learn whether "
                                      "the environment sets it.")
    assert got["notes"] == [["bad", "409 OPENROUTER_API_KEY is set in the hub's environment"]]
    assert got["emptied"] == ""
    assert got["noName"] == {"hint": "Name the key to send one; with no name, no key is sent.",
                             "canStore": False, "canClear": False}


def test_clearing_a_key_asks_first_and_sends_null(tmp_path):
    got = run(tmp_path, LLM_HUB + """
      hub.words = [llmWord("hey_jarvis"), { ...llmWord("alexa"), mode: "command" }];
      hub.secrets = { SATELLITES_LLM_API_KEY: "hub" };
      hub.env = { SATELLITES_LLM_API_KEY: true };
      await satellitesRefresh(); await settle();
      const shared = wakeKeyState("SATELLITES_LLM_API_KEY", "hey_jarvis");
      confirming = false;
      await wakeKeyForget("SATELLITES_LLM_API_KEY", { querySelector: () => stand() }, stand());
      const declined = hub.secretCalls.length;
      confirming = true;
      await wakeKeyForget("SATELLITES_LLM_API_KEY", { querySelector: () => stand() }, stand());
      const after = wakeKeyState("SATELLITES_LLM_API_KEY", "hey_jarvis");
      console.log(JSON.stringify({ shared, asked, declined, sent: hub.secretCalls, after }));
    """)
    assert got["shared"]["hint"] == f"A key is stored on the hub as {NAME}. Also used by alexa."
    question = f"Clear the key stored as {NAME}? Every action that names it stops sending it."
    assert got["asked"] == [question, question]
    assert got["declined"] == 0, "a declined question still cleared the key"
    assert got["sent"] == [{"name": NAME, "value": None}]
    assert got["after"]["canClear"] is False and got["after"]["canStore"] is True
    assert got["after"]["hint"].startswith(f"No key is stored as {NAME}; a server of your own")


def test_the_test_sends_the_draft_destination_and_says_how_long_or_what_the_provider_said(tmp_path):
    got = run(tmp_path, LLM_HUB + """
      await satellitesRefresh(); await settle();
      wakeEdit("hey_jarvis", w => wakeField(w, "d.model", "vendor/test-large"));
      const ok = await wakeLlmTest(wakeClone(dest("hey_jarvis")));
      hub.testFail = "the LLM refused the key in SATELLITES_LLM_API_KEY (401): Incorrect API key provided: [key hidden]";
      const bad = await wakeLlmTest(wakeClone(dest("hey_jarvis")));
      await wakeLlmTry("hey_jarvis", { querySelector: () => stand() }, stand());
      // Push-to-talk echoes: it has no Test to run.
      await wakeLlmTry("ptt", { querySelector: () => stand() }, stand());
      console.log(JSON.stringify({ ok, bad, calls: hub.testCalls, puts: hub.puts.length,
                                   draft: !!WAKE.draft, notes }));
    """)
    assert got["calls"][0] == {"type": "llm", "base_url": "https://llm.example.com/v1",
                               "model": "vendor/test-large", "system": None, "api_key_env": NAME,
                               "max_tokens": 400, "timeout": 30, "stream": True}
    assert len(got["calls"]) == 3, "push-to-talk's echo was tested"
    assert got["ok"] == {"ok": True, "text": "Answered in 1.3 s, the first words in 0.4 s: Hello there."}
    said = ("The model did not answer: the LLM refused the key in SATELLITES_LLM_API_KEY (401): "
            "Incorrect API key provided: [key hidden]")
    assert got["bad"] == {"ok": False, "text": said}
    assert got["notes"] == [["bad", said]]
    # A Test saves nothing, and the edit is still the reader's.
    assert got["puts"] == 0 and got["draft"] is True


def test_a_reply_limit_is_a_whole_number_or_left_to_the_hub(tmp_path):
    got = run(tmp_path, LLM_HUB + """
      await satellitesRefresh(); await settle();
      const problem = v => { wakeEdit("hey_jarvis", w => wakeField(w, "d.max_tokens", v));
                             return problemOf("hey_jarvis"); };
      const said = {};
      for (const v of ["0", "8193", "2.5", "lots", "1200"]) said[v] = problem(v);
      await wakeSave();
      const set = sent("hey_jarvis").action.destination.max_tokens;
      wakeEdit("hey_jarvis", w => wakeField(w, "d.max_tokens", ""));
      await wakeSave();
      console.log(JSON.stringify({ said, set, left: "max_tokens" in sent("hey_jarvis").action.destination }));
    """)
    fix = "Make the reply limit a whole number from 1 to 8192."
    assert got["said"] == {"0": fix, "8193": fix, "2.5": fix, "lots": fix, "1200": ""}, got
    assert got["set"] == 1200, "the limit went as text"
    assert got["left"] is False, "an emptied limit was sent instead of left to the hub"
