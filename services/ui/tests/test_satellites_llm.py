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
  * a key sent by the model list to a provider just picked, before the key's
    name could be changed or that provider's key stored;
  * a key kept anywhere on the page after it is stored: in the draft, a Save,
    a note or the browser's storage;
  * a key stored under a name the environment sets, where it would never be
    sent, or cleared without a question, or pasted into a box that nothing
    can store from, where it stayed;
  * a key box that asked a password manager to make up a password;
  * a language model word's fixes in Home Assistant's words, about a token;
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
      // Neither name holds a key, so no key goes and each list is asked by
      // itself (wakeModelsFree); one that holds a key is the next test's.
      hub.env = { SATELLITES_LLM_API_KEY: false, OPENROUTER_API_KEY: false };
      await satellitesRefresh(); await settle();
      const first = hub.modelCalls.length;
      await satellitesRefresh(); await settle();
      const afterPoll = hub.modelCalls.length;
      const entry = WAKE_MODELS.get(wakeModelsKey("https://llm.example.com/v1", "SATELLITES_LLM_API_KEY"));
      const ready = wakeModelsHint(dest("hey_jarvis"), entry);
      wakeEdit("hey_jarvis", w => wakeField(w, "d.env", "OPENROUTER_API_KEY")); await settle();
      const afterName = hub.modelCalls.length;
      // Typing: nothing is asked, whatever the address looks like so far.
      wakeModelsWant({ base_url: "https://llm.exam", api_key_env: null }, true);
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


def test_a_key_goes_by_itself_only_where_the_word_already_sends_it(tmp_path):
    """Measured before: with a key stored under the word's name, picking each
    provider in turn asked all six for their models with it, because a pick
    keeps the name. So an operator moving from one provider to another sent
    the first one's key to the second before they could rename it or store
    the second's, and arrowing through a closed select sent it to all six.
    Now only the saved address and name are asked by themselves; anything
    else waits for List models, and the pair pressed is remembered."""
    got = run(tmp_path, LLM_HUB + """
      hub.env = { SATELLITES_LLM_API_KEY: true };
      hub.secrets = { SATELLITES_LLM_API_KEY: "hub" };
      await satellitesRefresh(); await settle();
      const saved = hub.modelCalls.slice();
      for (const [url] of LLM_PRESETS.filter(([u]) => u)) {
        wakeEdit("hey_jarvis", w => wakeField(w, "preset", url)); await settle();
      }
      const picked = hub.modelCalls.length;
      // Typed and committed, as the Base URL's change event commits it.
      wakeEdit("hey_jarvis", w => wakeField(w, "d.base_url", "https://other.example.com/v1"));
      wakeModelsWant(dest("hey_jarvis"), false, wakeSavedDest("hey_jarvis")); await settle();
      const typed = hub.modelCalls.length;
      const d = dest("hey_jarvis"), was = wakeSavedDest("hey_jarvis");
      const waiting = wakeModelsWaiting(d, was);
      const hint = wakeModelsHint(d, WAKE_MODELS.get(wakeModelsKey(d.base_url, d.api_key_env)), waiting);
      wakeModelsList(d); await settle();
      const pressed = hub.modelCalls.slice(picked);
      const after = wakeModelsWaiting(d, was);
      await satellitesRefresh(); await settle();
      const polled = hub.modelCalls.length;
      // The other way round: the saved address, and a name that may hold
      // another provider's key.
      wakeEdit("hey_jarvis", w => { wakeField(w, "d.base_url", "https://llm.example.com/v1");
                                    wakeField(w, "d.env", "OPENROUTER_API_KEY"); });
      wakeModelsWant(dest("hey_jarvis"), false, wakeSavedDest("hey_jarvis")); await settle();
      console.log(JSON.stringify({ saved, picked, typed, waiting, hint, pressed, after, polled,
                                   renamed: hub.modelCalls.length }));
    """)
    # The saved address and name, at the row's first paint, as before.
    assert got["saved"] == [{"base_url": "https://llm.example.com/v1", "api_key_env": NAME}]
    assert got["picked"] == 1, "a provider picked was sent the key"
    assert got["typed"] == 1, "a typed address was sent the key"
    assert got["waiting"] is True and got["hint"] == (
        f"List models sends the key in {NAME} to other.example.com; check the key is for it.")
    assert got["pressed"] == [{"base_url": "https://other.example.com/v1", "api_key_env": NAME}]
    assert got["after"] is False and got["polled"] == 2, "a poll asked again"
    assert got["renamed"] == 2, "a renamed key was sent to the saved address"


def test_a_key_stored_while_an_address_is_in_the_form_lists_its_models(tmp_path):
    """Storing a key with a provider's address in the form says the key is
    that provider's, so its models are asked with it, without a press."""
    got = run(tmp_path, LLM_HUB + f"""
      hub.env = {{ SATELLITES_LLM_API_KEY: true }};
      hub.secrets = {{ SATELLITES_LLM_API_KEY: "hub" }};
      await satellitesRefresh(); await settle();
      wakeEdit("hey_jarvis", w => wakeField(w, "preset", "https://api.deepseek.com")); await settle();
      const before = hub.modelCalls.length;
      await wakeKeyStore("SATELLITES_LLM_API_KEY", keyRow({{ value: "{KEY}" }}), stand(),
                         dest("hey_jarvis").base_url);
      await settle();
      console.log(JSON.stringify({{ before, asked: hub.modelCalls.slice(before),
                                   waiting: wakeModelsWaiting(dest("hey_jarvis"), wakeSavedDest("hey_jarvis")) }}));
    """)
    assert got["before"] == 1
    assert got["asked"] == [{"base_url": "https://api.deepseek.com", "api_key_env": NAME}], got
    assert got["waiting"] is False


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


def test_the_key_box_is_off_when_store_is_and_a_key_left_in_it_is_emptied(tmp_path):
    """Store was off for a name the environment sets, or none, while the box
    still took a paste; Enter clicked the disabled Store, which did nothing,
    and the key stayed in the box with no word said."""
    got = run(tmp_path, LLM_HUB + f"""
      hub.secrets = {{ SATELLITES_LLM_API_KEY: "environment" }};
      hub.env = {{ SATELLITES_LLM_API_KEY: true }};
      await satellitesRefresh(); await settle();
      const env = wakeKeyState("SATELLITES_LLM_API_KEY", "hey_jarvis").canStore;
      const noName = wakeKeyState(null).canStore;
      const pasted = {{ value: "{KEY}", disabled: false }};
      wakeKeyBox(pasted, stand(), env);
      const empty = {{ value: "", disabled: false }};
      wakeKeyBox(empty, stand(), noName);
      const saidOnce = notes.length;
      // Once Store can take a key again, so can the box.
      const back = {{ value: "", disabled: true }};
      wakeKeyBox(back, stand(), wakeKeyState("OPENROUTER_API_KEY", "hey_jarvis").canStore);
      console.log(JSON.stringify({{ env, noName, pasted, empty, saidOnce, back, notes,
                                   sent: hub.secretCalls }}));
    """)
    assert got["env"] is False and got["noName"] is False
    assert got["pasted"] == {"value": "", "disabled": True}, "a key stayed in a box nothing can store"
    assert got["empty"] == {"value": "", "disabled": True}
    assert got["notes"] == [["bad", "The pasted key was emptied: no key can be stored under this name."]]
    assert got["saidOnce"] == 1, "an empty box was said to be emptied"
    assert got["back"] == {"value": "", "disabled": False}
    assert got["sent"] == []


def test_a_language_model_words_fixes_say_key_where_home_assistants_say_token(tmp_path):
    """The field reads Key name on a language model word; a pasted key or an
    address with a user and password there was answered in Home Assistant's
    words, about a token."""
    got = run(tmp_path, LLM_HUB + f"""
      await satellitesRefresh(); await settle();
      wakeEdit("hey_jarvis", w => wakeField(w, "d.env", "{KEY}"));
      const pasted = problemOf("hey_jarvis");
      wakeEdit("hey_jarvis", w => {{ wakeField(w, "d.env", "SATELLITES_LLM_API_KEY");
                                     wakeField(w, "d.base_url", "https://me:pw@llm.example.com/v1"); }});
      const userinfo = problemOf("hey_jarvis");
      console.log(JSON.stringify({{ pasted, userinfo }}));
    """)
    assert got["pasted"] == ("Write the key's name in capitals, digits and underscores, never the "
                             "key itself.")
    assert got["userinfo"] == ("Leave the user and password out of the address; name a key under "
                               "Key name instead.")
    for said in got.values():
        assert "token" not in said and "variable" not in said, said


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


def test_a_language_model_word_turns_its_tools_on_and_off_in_the_hubs_order(tmp_path):
    """A new language model word starts with no tools, as the hub's default
    does; a box ticked puts one on, and the list goes to the hub in its own
    order."""
    got = run(tmp_path, LLM_HUB + """
      await satellitesRefresh(); await settle();
      const saved = dest("hey_jarvis").tools;
      wakeAdd("alexa");
      wakeEdit("alexa", w => wakeField(w, "dest", "llm"));
      const fresh = dest("alexa").tools;
      wakeEdit("alexa", w => wakeTool(w, "weather", true));
      wakeEdit("alexa", w => wakeTool(w, "web_search", true));
      wakeEdit("alexa", w => wakeTool(w, "weather", false));
      wakeEdit("alexa", w => { wakeField(w, "d.base_url", "https://llm.example.com/v1");
                               wakeField(w, "d.model", "gpt-test-mini"); });
      await wakeSave();
      console.log(JSON.stringify({ saved: saved === undefined ? "absent" : saved, fresh,
                                   sent: sent("alexa").action.destination.tools }));
    """)
    assert got["saved"] == "absent", "a word saved before tools existed was given some"
    assert got["fresh"] == []
    assert got["sent"] == ["web_search"]


def test_web_search_is_greyed_and_said_on_a_hub_without_searxng(tmp_path):
    """Most hubs have no SATELLITES_SEARXNG_URL. The model was still offered
    web search, called it for news-like questions, and was told it was not
    set up, a whole round for nothing. The hub says which tools work; the
    box is greyed, unless the word has it already, and the line says why."""
    got = run(tmp_path, LLM_HUB + """
      hub.tools = { web_search: false, weather: true };
      await satellitesRefresh(); await settle();
      const row = WAKE.rows.get("hey_jarvis");
      const els = new Map(), find = row.querySelector;
      row.querySelector = sel => { if (!els.has(sel)) els.set(sel, find(sel)); return els.get(sel); };
      const search = { dataset: { tool: "web_search" }, checked: false, disabled: false };
      const weather = { dataset: { tool: "weather" }, checked: false, disabled: false };
      row.querySelectorAll = sel => sel === "[data-tool]" ? [search, weather] : [];
      wakeRender();
      const off = { search: search.disabled, weather: weather.disabled,
                    hint: row.querySelector(".ww-toolhint").textContent };
      wakeEdit("hey_jarvis", w => wakeTool(w, "web_search", true));
      console.log(JSON.stringify({ off, kept: search.disabled }));
    """)
    assert got["off"] == {"search": True, "weather": False, "hint": (
        "Web search needs SATELLITES_SEARXNG_URL set on the hub; the weather needs nothing.")}, got
    assert got["kept"] is False, "a word that has web search could not take it off"


def test_an_action_changed_and_changed_back_keeps_what_it_had(tmp_path):
    """Webhook and back to Language model came back with the address and
    the model empty, and the system prompt, the reply limit and the tools,
    under the closed More, were sent as the hub's defaults. Arrowing through
    a closed Action select on Windows or Linux is a change per option, so
    only looking did it. Each destination left is kept per word and type,
    and one that comes back as it was saved leaves nothing to save."""
    got = run(tmp_path, LLM_HUB + """
      hub.words = [llmWord("hey_jarvis", { system: "You are terse.", max_tokens: 900, tools: ["weather"] })];
      await satellitesRefresh(); await settle();
      for (const type of ["webhook", "echo", "ha_assist", "llm"]) wakeEdit("hey_jarvis", w => wakeField(w, "dest", type));
      const back = { draft: WAKE.draft, d: dest("hey_jarvis") };
      // A webhook address typed, then away and back to it: the address too.
      wakeEdit("hey_jarvis", w => wakeField(w, "dest", "webhook"));
      wakeEdit("hey_jarvis", w => wakeField(w, "d.url", "https://hooks.example.com/voice"));
      wakeEdit("hey_jarvis", w => wakeField(w, "dest", "llm"));
      wakeEdit("hey_jarvis", w => wakeField(w, "dest", "webhook"));
      const hook = dest("hey_jarvis").url;
      console.log(JSON.stringify({ back, hook }));
    """)
    assert got["back"]["draft"] is None, "an action changed back to what was saved still read as an edit"
    d = got["back"]["d"]
    assert (d["base_url"], d["model"], d["system"], d["max_tokens"], d["tools"]) == (
        "https://llm.example.com/v1", "gpt-test-mini", "You are terse.", 900, ["weather"]), d
    assert got["hook"] == "https://hooks.example.com/voice"


def test_test_is_greyed_only_by_its_destination_and_its_result_goes_when_the_form_changes(tmp_path):
    """Test sends the destination alone, but any problem on the word greyed
    it, a Language tag included, with nothing beside it saying why. And its
    result stayed under a model changed since, reading as proof that the
    form as it now stands works."""
    got = run(tmp_path, LLM_HUB + """
      await satellitesRefresh(); await settle();
      const row = WAKE.rows.get("hey_jarvis");
      const els = new Map(), find = row.querySelector;
      row.querySelector = sel => { if (!els.has(sel)) els.set(sel, find(sel)); return els.get(sel); };
      const result = { textContent: "", append(line) { this.textContent += line.textContent; } };
      els.set(".ww-llmresult", result);
      const test = () => row.querySelector('[data-ww="llmtest"]').disabled;
      wakeEdit("hey_jarvis", w => wakeField(w, "tag", "Deutsch"));
      const tag = test();
      wakeEdit("hey_jarvis", w => wakeField(w, "d.model", ""));
      const model = test();
      wakeEdit("hey_jarvis", w => { wakeField(w, "tag", ""); wakeField(w, "d.model", "gpt-test-mini"); });
      await wakeLlmTry("hey_jarvis", row, stand());
      const said = result.textContent;
      wakeRender();
      const kept = result.textContent;
      wakeEdit("hey_jarvis", w => wakeField(w, "d.model", "vendor/test-large"));
      console.log(JSON.stringify({ tag, model, said, kept, changed: result.textContent }));
    """)
    assert got["tag"] is False, "a problem outside the destination greyed Test"
    assert got["model"] is True, got
    assert got["said"] == got["kept"] == "Answered in 1.3 s, the first words in 0.4 s: Hello there.", got
    assert got["changed"] == "", "a result stayed under a form it did not test"


def test_a_model_list_that_failed_can_be_asked_again_from_its_row(tmp_path):
    """After "Could not list the models" the row offered no control, and the
    only retry was to shut the word and open it again, which nothing said.
    Ask again shows beside the reason, and a press asks once more."""
    got = run(tmp_path, LLM_HUB + """
      hub.modelsFail = "the server answered 502: connection refused";
      await satellitesRefresh(); await settle();
      const row = WAKE.rows.get("hey_jarvis");
      const els = new Map(), find = row.querySelector;
      row.querySelector = sel => { if (!els.has(sel)) els.set(sel, find(sel)); return els.get(sel); };
      wakeRender();
      const go = row.querySelector('[data-ww="models"]');
      const failed = { hidden: go.hidden, text: go.textContent };
      hub.modelsFail = "";
      wakeModelsList(dest("hey_jarvis")); await settle();
      console.log(JSON.stringify({ failed, asked: hub.modelCalls.length, hidden: go.hidden,
                                   state: WAKE_MODELS.get(wakeModelsKey("https://llm.example.com/v1", "SATELLITES_LLM_API_KEY")).state }));
    """)
    assert got["failed"] == {"hidden": False, "text": "Ask again"}, got
    assert (got["asked"], got["state"], got["hidden"]) == (2, "ready", True), got

