"""A wake word as a whole behaviour, run rather than read.

Since 2026-09-25 a wake word says what it does: Command, Conversation or
Trigger, an optional language hint, and (for the first two) an action. The
Satellites tab's Wake words disclosure is the one place to set all of it, and
these drive the page's own Satellites section through test_satellites_writes.py's
harness, in Node, against its fake hub. Nothing starts a server and nothing
reaches the network; without `node` on PATH these skip.

What they prevent:

  * a word saved without the fields its mode needs, which the hub refuses
    with a 422 the page could have named first, or saved with fields another
    mode owns (a trigger with an action);
  * an edit to one field that resets the others, because a field sent
    replaces the saved one whole;
  * a pasted token saved where a variable's name belongs;
  * push-to-talk offered as a trigger, or sent when nobody changed it;
  * a custom model deleted while a word still uses it;
  * a conversation, a turn or a trigger that the log prints as its bare type
    and that leaves the satellite's row saying Listening.
"""

from pathlib import Path

from test_satellites_writes import pytestmark, run  # noqa: F401

# A hub from after modes: every word has its behaviour, push-to-talk has its
# own, and one custom model is on the volume.
MODERN = """
hub.available = ["alexa", "hey_jarvis", "hey_mycroft", "lumos"];
hub.custom = ["lumos"];
hub.env = { SATELLITES_HA_TOKEN: true };
hub.words = [
  { name: "hey_jarvis", threshold: 0.5, satellites: ["*"], mode: "command", language: null,
    action: { destination: { type: "ha_assist", url: "https://ha.local:8123",
                             token_env: "SATELLITES_HA_TOKEN", pipeline: null, timeout: 20 },
              reply_to: "same", voice: null, fallback: null },
    silence_ms: 800, conversation: { follow_up_s: 8, silence_ms: 600, end_phrases: null },
    trigger: { feedback: "earcon", cooldown_s: 3, ends_conversation: false },
    state: "ready", error: null }];
hub.ptt = { mode: "command", language: null,
            action: { destination: { type: "echo" }, reply_to: "same", voice: null, fallback: null },
            silence_ms: 800, conversation: { follow_up_s: 8, silence_ms: 600, end_phrases: null },
            trigger: { feedback: "earcon", cooldown_s: 3, ends_conversation: false } };
const sent = name => hub.puts[hub.puts.length - 1].find(w => w.name === name);
const last = () => hub.bodies[hub.bodies.length - 1];
"""


def test_a_command_word_is_saved_with_its_home_assistant_action(tmp_path):
    """Add, and the new word is a command on every satellite at 0.5 with the
    action the hub already uses for Home Assistant, so its address is not
    typed twice. Switched to the conversation agent, the address and the
    token's name carry over."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      const w = wakeAdd("alexa");
      const staged = JSON.parse(JSON.stringify(w));
      wakeEdit("alexa", w => wakeField(w, "dest", "ha_conversation"));
      wakeEdit("alexa", w => wakeField(w, "dest", "ha_assist"));
      wakeEdit("alexa", w => wakeField(w, "d.pipeline", "01kitchen"));
      await wakeSave();
      console.log(JSON.stringify({ staged, body: sent("alexa"), clean: WAKE.draft === null,
                                   keys: Object.keys(last()) }));
    """)
    staged, body = got["staged"], got["body"]
    assert staged["mode"] == "command" and staged["threshold"] == 0.5
    assert staged["satellites"] == ["*"]
    assert staged["action"]["destination"] == {"type": "ha_assist", "url": "https://ha.local:8123",
                                               "token_env": "SATELLITES_HA_TOKEN"}, staged
    assert body["mode"] == "command" and body["language"] is None
    assert body["action"]["destination"] == {"type": "ha_assist", "url": "https://ha.local:8123",
                                             "token_env": "SATELLITES_HA_TOKEN",
                                             "pipeline": "01kitchen"}, body
    assert body["action"]["reply_to"] == "same"
    assert got["clean"], got
    assert got["keys"] == ["words"], "push-to-talk was sent although nobody changed it"


def test_the_assist_pipeline_is_chosen_from_home_assistants_own_list(tmp_path):
    """Home Assistant is asked once for an address and token variable, the
    preferred pipeline comes first by name, the line under the select says
    how the chosen one hears and speaks, and a saved id Home Assistant no
    longer has stays choosable, said as such."""
    got = run(tmp_path, MODERN + """
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      await satellitesRefresh();
      await sleep(30);
      const d = () => (WAKE.draft || WAKE.server.words).find(w => w.name === "hey_jarvis").action.destination;
      const entry = () => WAKE_PIPES.get(wakePipesKey(d()));
      const out = { calls: hub.pipeCalls.slice(), options: wakePipeOptions(d(), entry()),
                    preferred: wakePipeHint(d(), entry()) };
      wakeEdit("hey_jarvis", w => wakeField(w, "d.pipeline", "01alexa"));
      out.chosen = wakePipeHint(d(), entry());
      wakeEdit("hey_jarvis", w => wakeField(w, "d.pipeline", "01gone"));
      out.gone = wakePipeOptions(d(), entry());
      await satellitesRefresh();
      await sleep(30);
      out.asked = hub.pipeCalls.length;
      console.log(JSON.stringify(out));
    """)
    assert got["calls"] == [{"url": "https://ha.local:8123", "token_env": "SATELLITES_HA_TOKEN"}]
    assert got["options"] == [["", "Home Assistant's preferred (Home Assistant Cloud)"],
                              ["01cloud", "Home Assistant Cloud"], ["01alexa", "Alexa"]]
    assert got["preferred"] == "Hears with home_assistant_cloud (en-GB), speaks with piper."
    assert got["chosen"] == "Hears with calliope_parakeet (pt), speaks with calliope_kokoro as pf_dora."
    assert got["gone"][-1] == ["01gone", "01gone (not in Home Assistant)"]
    assert got["asked"] == 1, "a repaint asked Home Assistant again"


def test_a_pipeline_list_that_failed_says_why_and_a_new_address_is_asked_once_committed(tmp_path):
    """The hub's sentence is shown as it is (it never carries the token), the
    saved choice stays as it was, and an address typed letter by letter is
    never asked for, however long the reader pauses: asking sends the token
    to the address, and a pause after "https://ha.example.co" sent it to
    that host. Committed (the field left, or Enter), it is one request."""
    got = run(tmp_path, MODERN + """
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      hub.words[0].action.destination.pipeline = "01alexa";
      hub.pipesFail = "502 Home Assistant refused the token in SATELLITES_HA_TOKEN";
      await satellitesRefresh();
      await sleep(30);
      const d = () => (WAKE.draft || WAKE.server.words).find(w => w.name === "hey_jarvis").action.destination;
      const entry = () => WAKE_PIPES.get(wakePipesKey(d()));
      const out = { failed: wakePipeHint(d(), entry()), kept: wakePipeOptions(d(), entry()) };
      hub.pipesFail = "";
      for (const url of ["https://h", "https://ha.example.co", "https://ha.example.com"]) {
        wakeEdit("hey_jarvis", w => wakeField(w, "d.url", url));
        await sleep(700);
      }
      out.typing = hub.pipeCalls.length;
      out.waiting = wakePipeHint(d(), entry(), true);
      wakePipesCommit(d());
      wakeRender();
      await sleep(30);
      wakeRender();
      await sleep(30);
      out.asked = hub.pipeCalls.map(c => c.url);
      out.ask = wakePipeHint({ type: "ha_assist", url: "https://ha.local:8123", token_env: "" }, undefined);
      console.log(JSON.stringify(out));
    """)
    assert got["failed"] == ("Could not list the pipelines: "
                             "502 Home Assistant refused the token in SATELLITES_HA_TOKEN")
    assert got["kept"] == [["", "Home Assistant's preferred"], ["01alexa", "01alexa"]]
    assert got["typing"] == 1, "a request went out while the address was still being typed"
    assert got["waiting"] == "List pipelines sends the token in SATELLITES_HA_TOKEN to ha.example.com."
    assert got["asked"] == ["https://ha.local:8123", "https://ha.example.com"]
    assert got["ask"] == "Fill in the address and token variable to list the pipelines set up in Home Assistant."


def test_a_pipeline_list_is_never_asked_while_its_address_has_the_focus(tmp_path):
    """Even a pair the reader asked for before waits while the address is
    being typed: the render that follows each keystroke asks nothing."""
    got = run(tmp_path, MODERN + """
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      await satellitesRefresh();
      await sleep(30);
      const row = WAKE.rows.get("hey_jarvis");
      // The stand-ins hand out a new element a call; the row keeps one per
      // selector here, so the address field can hold the focus.
      const els = new Map(), find = row.querySelector;
      row.querySelector = sel => { if (!els.has(sel)) els.set(sel, find(sel)); return els.get(sel); };
      const d = () => (WAKE.draft || WAKE.server.words).find(w => w.name === "hey_jarvis").action.destination;
      wakeEdit("hey_jarvis", w => wakeField(w, "d.url", "https://ha.example.com"));
      wakePipesCommit(d());
      WAKE_PIPES.delete(wakePipesKey(d()));
      document.activeElement = row.querySelector('[data-f="d.url"]');
      wakeRender();
      await sleep(30);
      const out = { typing: hub.pipeCalls.length };
      document.activeElement = null;
      wakeRender();
      await sleep(30);
      out.left = hub.pipeCalls.map(c => c.url);
      console.log(JSON.stringify(out));
    """)
    assert got["typing"] == 1, "asked while the address had the focus"
    assert got["left"] == ["https://ha.local:8123", "https://ha.example.com"]


def test_a_pipeline_list_that_failed_is_asked_again_from_its_row(tmp_path):
    """A failure was kept for the page's life, and the only retry was to shut
    the word and open it again, which nothing said. Ask again forgets it and
    the row asks at once; a list that did not fail is never forgotten."""
    got = run(tmp_path, MODERN + """
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      hub.pipesFail = "502 SATELLITES_HA_TOKEN is not set";
      await satellitesRefresh();
      await sleep(30);
      const row = WAKE.rows.get("hey_jarvis");
      const d = () => WAKE.server.words.find(w => w.name === "hey_jarvis").action.destination;
      const state = () => WAKE_PIPES.get(wakePipesKey(d())).state;
      const out = { failed: state() };
      hub.pipesFail = "";
      out.forgot = wakePipesForget(row);
      wakeRender();
      await sleep(30);
      out.again = state();
      out.calls = hub.pipeCalls.length;
      out.kept = wakePipesForget(row);
      console.log(JSON.stringify(out));
    """)
    assert got["failed"] == "failed" and got["forgot"] is True, got
    assert got["again"] == "ready" and got["calls"] == 2, got
    assert got["kept"] is False, "a list that loaded was forgotten"


def test_home_assistants_two_actions_share_the_address_the_token_and_the_timeout(tmp_path):
    """Assist to the conversation agent kept the address and the token's
    name and dropped a saved timeout, so 20 s went back to the default 15."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      wakeEdit("hey_jarvis", w => wakeField(w, "dest", "ha_conversation"));
      await wakeSave();
      console.log(JSON.stringify({ d: sent("hey_jarvis").action.destination }))
    """)
    assert got["d"] == {"type": "ha_conversation", "url": "https://ha.local:8123",
                        "token_env": "SATELLITES_HA_TOKEN", "timeout": 20}, got


def test_an_edit_sends_the_rest_of_the_entry_back_as_it_was(tmp_path):
    """A field sent replaces the saved one whole: an action sent without its
    timeout would reset it to the default. Only the field edited changes."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      wakeEdit("hey_jarvis", w => wakeField(w, "d.url", "https://ha.home:8123"));
      await wakeSave();
      console.log(JSON.stringify({ body: sent("hey_jarvis") }));
    """)
    body = got["body"]
    assert body["action"]["destination"]["url"] == "https://ha.home:8123"
    assert body["action"]["destination"]["timeout"] == 20, "the saved timeout was dropped"
    assert body["conversation"] == {"follow_up_s": 8, "silence_ms": 600, "end_phrases": None}
    assert body["silence_ms"] == 800
    assert "state" not in body and "error" not in body


def test_the_pause_that_ends_a_command_is_typed_in_seconds_and_sent_in_milliseconds(tmp_path):
    """Hesitating mid-command used to end it at the hub's fixed 0.8 s. The
    pause is typed in seconds, sent as the hub's silence_ms, and a value it
    would refuse, or none, is named before Save."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      const word = () => WAKE.draft.find(w => w.name === "hey_jarvis");
      wakeEdit("hey_jarvis", w => wakeField(w, "silence_ms", "5"));
      const tooLong = wakeProblem(word(), wakeEffective());
      wakeEdit("hey_jarvis", w => wakeField(w, "silence_ms", ""));
      const empty = wakeProblem(word(), wakeEffective());
      wakeEdit("hey_jarvis", w => wakeField(w, "silence_ms", "1.5"));
      wakeEdit("hey_jarvis", w => wakeSetMode(w, "conversation", "hey_jarvis"));
      wakeEdit("hey_jarvis", w => wakeField(w, "c.silence_ms", "0.1"));
      const follow = wakeProblem(word(), wakeEffective());
      wakeEdit("hey_jarvis", w => wakeField(w, "c.silence_ms", "1.2"));
      await wakeSave();
      console.log(JSON.stringify({ tooLong, empty, follow, body: sent("hey_jarvis") }));
    """)
    assert got["tooLong"] == got["empty"] == "Pause for 0.2 to 3 seconds before the command ends."
    assert got["follow"] == "Pause for 0.2 to 3 seconds before a follow-up ends."
    assert got["body"]["silence_ms"] == 1500
    assert got["body"]["conversation"]["silence_ms"] == 1200


def test_a_word_takes_its_own_ring_colour_and_can_go_back_to_the_default(tmp_path):
    """The picker's colour is sent as the hub's `colour`; Use the default
    sends null; a colour the hub would refuse is named before Save."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      const word = () => WAKE.draft.find(w => w.name === "hey_jarvis");
      wakeEdit("hey_jarvis", w => wakeField(w, "colour", "red"));
      const bad = wakeProblem(word(), wakeEffective());
      wakeEdit("hey_jarvis", w => wakeField(w, "colour", "#ff4400"));
      await wakeSave();
      const picked = sent("hey_jarvis");
      wakeEdit("hey_jarvis", w => { w.colour = null; });
      await wakeSave();
      console.log(JSON.stringify({ bad, picked, cleared: sent("hey_jarvis") }));
    """)
    assert got["bad"] == "Pick the ring colour with the colour picker."
    assert got["picked"]["colour"] == "#ff4400"
    assert got["cleared"]["colour"] is None


def test_a_trigger_words_ring_colour_is_shown_checked_and_can_go_back_to_the_default(tmp_path):
    """A trigger flashes its colour when heard, but the picker and Use the
    default were written with the action's fields, which a trigger has none
    of: its picker kept the browser's black and the button never showed.
    And a trigger's colour was not checked before the hub saw it."""
    got = run(tmp_path, MODERN + """
      hub.words.push({ name: "lumos", threshold: 0.7, satellites: ["*"], mode: "trigger", language: null,
                       action: null, silence_ms: 800, colour: "#ff4400",
                       conversation: { follow_up_s: 8, silence_ms: 600, end_phrases: null },
                       trigger: { feedback: "earcon", cooldown_s: 3, ends_conversation: false },
                       state: "ready", error: null });
      await satellitesRefresh();
      const row = WAKE.rows.get("lumos");
      const els = new Map(), find = row.querySelector;
      row.querySelector = sel => { if (!els.has(sel)) els.set(sel, find(sel)); return els.get(sel); };
      wakeRender();
      const shown = { picker: row.querySelector('[data-f="colour"]').value,
                      reset: !row.querySelector('[data-ww="colourdefault"]').hidden };
      wakeEdit("lumos", w => wakeField(w, "colour", "red"));
      const bad = wakeProblem(WAKE.draft.find(w => w.name === "lumos"), wakeEffective());
      wakeEdit("lumos", w => { w.colour = null; });
      const reset = { picker: row.querySelector('[data-f="colour"]').value,
                      hidden: row.querySelector('[data-ww="colourdefault"]').hidden };
      console.log(JSON.stringify({ shown, bad, reset }));
    """)
    assert got["shown"] == {"picker": "#ff4400", "reset": True}, got
    assert got["bad"] == "Pick the ring colour with the colour picker.", got
    assert got["reset"] == {"picker": "#286eff", "hidden": True}, got


def test_more_end_phrases_than_the_hub_takes_are_named_before_save(tmp_path):
    """The box takes 2000 characters and the hub 64 phrases: seventy short
    ones passed the page and came back as a 422."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      wakeEdit("hey_jarvis", w => wakeSetMode(w, "conversation", "hey_jarvis"));
      const say = text => { wakeEdit("hey_jarvis", w => wakeField(w, "c.end_phrases", text));
                            return wakeProblem(WAKE.draft.find(w => w.name === "hey_jarvis"), wakeEffective()); };
      const many = n => Array.from({ length: n }, (_, i) => "stop " + i).join(", ");
      console.log(JSON.stringify({ seventy: say(many(70)), sixty_four: say(many(64)), long: say("x".repeat(65)) }));
    """)
    fix = "Keep to 64 end phrases, each of 64 characters at most."
    assert got == {"seventy": fix, "sixty_four": "", "long": fix}, got


def test_a_value_the_new_mode_hides_is_never_one_the_hub_would_refuse(tmp_path):
    """Save sends every field in every mode and the hub checks them all, but
    the page named only what the mode shows. A pause of 5 s typed in Command
    and then Trigger turned Save on, and the hub answered 422 about a field
    nobody could see. Each such value goes back to the saved one as its
    field is hidden, or, on a word never saved, is left to the hub."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      // The draft, or the hub's copy once an edit is back where it started.
      const jarvis = () => (WAKE.draft || WAKE.server.words).find(w => w.name === "hey_jarvis");
      const out = {};
      const turn = (edit, mode) => {
        wakeEdit("hey_jarvis", edit);
        wakeEdit("hey_jarvis", w => wakeSetMode(w, mode, "hey_jarvis"));
        return { problem: wakeProblem(jarvis(), wakeEffective()), off: $("wwsave").disabled };
      };
      out.pause = turn(w => wakeField(w, "silence_ms", "5"), "trigger");
      out.tag = turn(w => { wakeSetMode(w, "command", "hey_jarvis"); wakeField(w, "tag", "Deutsch"); }, "trigger");
      out.follow = turn(w => { wakeSetMode(w, "conversation", "hey_jarvis"); wakeField(w, "c.follow_up_s", "");
                               wakeField(w, "c.silence_ms", "0.1"); }, "command");
      out.cool = turn(w => { wakeSetMode(w, "trigger", "hey_jarvis"); wakeField(w, "t.cooldown_s", "900"); }, "command");
      out.clean = WAKE.draft === null;
      wakeEdit("hey_jarvis", w => { w.threshold = 0.6; });
      await wakeSave();
      out.sent = sent("hey_jarvis");
      // A word never saved has nothing to go back to: left out, the hub's default.
      wakeAdd("alexa");
      wakeEdit("alexa", w => wakeField(w, "silence_ms", "5"));
      wakeEdit("alexa", w => wakeSetMode(w, "trigger", "alexa"));
      out.fresh = "silence_ms" in WAKE.draft.find(w => w.name === "alexa");
      // Push-to-talk leaves a conversation as a word does.
      wakeEdit("ptt", w => wakeSetMode(w, "conversation", "ptt"));
      wakeEdit("ptt", w => wakeField(w, "c.follow_up_s", "0"));
      wakeEdit("ptt", w => wakeSetMode(w, "command", "ptt"));
      out.ptt = WAKE.draftPtt.conversation.follow_up_s;
      console.log(JSON.stringify(out));
    """)
    for case in ("pause", "tag"):
        assert got[case] == {"problem": "", "off": False}, (case, got[case])
    # Back to Command with every hidden value put back is no edit at all.
    for case in ("follow", "cool"):
        assert got[case] == {"problem": "", "off": True}, (case, got[case])
    assert got["clean"], got
    body = got["sent"]
    assert body["mode"] == "command" and body["silence_ms"] == 800 and body["language"] is None, body
    assert body["conversation"] == {"follow_up_s": 8, "silence_ms": 600, "end_phrases": None}, body
    assert body["trigger"]["cooldown_s"] == 3, body
    assert got["fresh"] is False, got
    assert got["ptt"] == 8, got


def test_a_word_whose_model_has_gone_from_the_hub_is_named_before_save(tmp_path):
    """A custom .onnx gone from the volume makes the hub refuse the whole
    list, so every Save of another word, and Try again, was a 422 that named
    the word only afterwards. It is named on its row beforehand, with no
    field to mark, and Save waits until it is removed."""
    got = run(tmp_path, MODERN + """
      hub.available = hub.available.filter(n => n !== "hey_jarvis");
      await satellitesRefresh();
      const jarvis = () => (WAKE.draft || WAKE.server.words).find(w => w.name === "hey_jarvis");
      const problem = wakeProblem(jarvis(), WAKE.server.words);
      const at = wakeProblemAt(jarvis(), problem);
      wakeAdd("alexa");
      const held = { off: $("wwsave").disabled, said: $("wwdirty").textContent };
      wakeRemove("hey_jarvis", stand());
      console.log(JSON.stringify({ problem, at, held, removed: $("wwsave").disabled }));
    """)
    assert got["problem"] == ("Its model is no longer on the hub: upload it again under Custom models, "
                              "or remove this word."), got
    assert got["at"] == {"f": "", "empty": False}, got
    assert got["held"] == {"off": True, "said": "hey jarvis needs a fix before the wake words can be saved."}
    assert got["removed"] is False, got


def test_mores_note_says_first_that_a_fix_is_in_it(tmp_path):
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      console.log(JSON.stringify({ plain: wakeMoreNote({ reply_to: "none" }, true, false),
                                   fix: wakeMoreNote({ reply_to: "none" }, true, true),
                                   only: wakeMoreNote({ reply_to: "same" }, true, true) }));
    """)
    assert got == {"plain": "replies nowhere", "fix": "needs a fix, replies nowhere", "only": "needs a fix"}


def test_a_conversation_word_keeps_listening_and_hands_over_to_nobody(tmp_path):
    """A conversation's follow-up and end phrases are sent; a fallback is a
    command's, and switching to conversation takes it off."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      wakeAdd("alexa");
      wakeEdit("alexa", w => { w.action.fallback = "hey_jarvis"; });
      wakeEdit("alexa", w => wakeSetMode(w, "conversation", "alexa"));
      wakeEdit("alexa", w => wakeField(w, "c.follow_up_s", "12"));
      wakeEdit("alexa", w => wakeField(w, "c.end_phrases", "thanks, that's all ,  "));
      wakeEdit("alexa", w => wakeField(w, "dest", "llm"));
      const needs = wakeProblem(WAKE.draft.find(w => w.name === "alexa"), wakeEffective());
      wakeEdit("alexa", w => { wakeField(w, "d.base_url", "https://llm.example.com/v1");
                               wakeField(w, "d.model", "vendor/test-model"); wakeField(w, "d.env", ""); });
      await wakeSave();
      console.log(JSON.stringify({ needs, body: sent("alexa") }));
    """)
    body = got["body"]
    assert got["needs"] == "Write the address in full, starting with https or http.", got
    assert body["mode"] == "conversation"
    assert body["conversation"] == {"follow_up_s": 12, "end_phrases": ["thanks", "that's all"]}
    assert body["action"]["fallback"] is None
    assert body["action"]["destination"] == {"type": "llm", "base_url": "https://llm.example.com/v1",
                                             "model": "vendor/test-model", "api_key_env": None,
                                             "tools": []}, body


def test_a_trigger_word_sends_no_action_and_starts_stricter(tmp_path):
    """The word is the command: no action is sent (the hub refuses one), the
    threshold moves from 0.5 to 0.7 unless somebody set it, and its feedback
    and cooldown go with it. Back to a command, the action it had returns."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      wakeAdd("lumos");
      wakeEdit("lumos", w => wakeSetMode(w, "trigger", "lumos"));
      const threshold = WAKE.draft.find(w => w.name === "lumos").threshold;
      wakeEdit("lumos", w => { wakeField(w, "t.feedback", "none"); wakeField(w, "t.cooldown_s", "5"); });
      // Ends a conversation it is heard in: the box, as its change listener sets it.
      wakeEdit("lumos", w => { w.trigger = { ...(w.trigger || {}), ends_conversation: true }; });
      wakeEdit("hey_jarvis", w => { w.threshold = 0.65; });
      wakeEdit("hey_jarvis", w => wakeSetMode(w, "trigger", "hey_jarvis"));
      const set_by_hand = WAKE.draft.find(w => w.name === "hey_jarvis").threshold;
      wakeEdit("hey_jarvis", w => wakeSetMode(w, "command", "hey_jarvis"));
      const back = WAKE.draft.find(w => w.name === "hey_jarvis").action.destination.url;
      await wakeSave();
      console.log(JSON.stringify({ threshold, set_by_hand, back, body: sent("lumos"),
                                   line: wakeLine(WAKE.server.words.find(w => w.name === "lumos")) }));
    """)
    body = got["body"]
    assert got["threshold"] == 0.7, got
    assert got["set_by_hand"] == 0.65, "a threshold somebody set was moved"
    assert got["back"] == "https://ha.local:8123", "Trigger and back lost the action"
    assert body["mode"] == "trigger" and "action" not in body, body
    assert body["trigger"] == {"feedback": "none", "cooldown_s": 5, "ends_conversation": True}
    assert body["threshold"] == 0.7
    assert got["line"] == "Trigger · every satellite · Home Assistant decides", got


def test_a_language_hint_is_a_tag_or_nothing(tmp_path):
    """Auto is no hint (null); the short list sends its tag; Other takes any
    BCP 47 tag the hub's own pattern accepts, and anything else is named as
    wrong before Save rather than refused after it."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      // Read from the draft, or from the hub's copy once an edit is back
      // where it started: the page drops a draft that matches the hub.
      const now = () => (WAKE.draft || WAKE.server.words).find(w => w.name === "hey_jarvis");
      const say = (value, key) => { wakeEdit("hey_jarvis", w => wakeField(w, key || "lang", value));
                                    return now().language; };
      const out = { br: say("pt-BR"), auto: say(""), other_first: (say("pt-BR"), say("other")),
                    de: say("de", "tag"), bad: say("Deutsch", "tag") };
      out.bad_problem = wakeProblem(now(), wakeEffective());
      out.save_off = $("wwsave").disabled;
      say("nl-BE", "tag");
      await wakeSave();
      out.sent = sent("hey_jarvis").language;
      const saved = WAKE.server.words.find(w => w.name === "hey_jarvis");
      out.line = wakeLine(saved);
      out.line_agent = wakeLine({ ...saved, action: { ...saved.action,
                                  destination: { ...saved.action.destination, type: "ha_conversation" } } });
      // A tag the hub would refuse, typed under another action, then Assist,
      // which hides the only field that could fix it.
      wakeEdit("hey_jarvis", w => { wakeField(w, "dest", "webhook"); wakeField(w, "tag", "Deutsch"); });
      wakeEdit("hey_jarvis", w => wakeField(w, "dest", "ha_assist"));
      out.hidden_bad = WAKE.draft.find(w => w.name === "hey_jarvis").language;
      console.log(JSON.stringify(out));
    """)
    assert got["br"] == "pt-BR" and got["auto"] is None
    assert got["other_first"] == "pt-BR", "choosing Other threw the language away before a tag was typed"
    assert got["de"] == "de"
    assert got["bad"] == "Deutsch"
    assert got["bad_problem"] == "Write the language as a tag, for example de or nl-BE.", got
    assert got["save_off"] is True
    assert got["sent"] == "nl-BE"
    # An Assist word's pipeline sets its language, so its closed line names
    # none; the conversation agent reads the hint, so its line does.
    assert got["line"] == "Command · every satellite · Home Assistant Assist", got
    assert got["line_agent"] == "Command · every satellite · Home Assistant · nl-BE", got
    assert got["hidden_bad"] is None, "a refused tag stays behind a hidden field and holds Save off"


def test_what_the_hub_would_refuse_is_named_and_save_waits(tmp_path):
    """Each of these is a 422 from the hub. The page names it on the word's
    row and beside Save, Save is off, and pressing it anyway sends nothing.
    A token pasted where its variable's name belongs is caught before it can
    be stored in wake_words.json. What the hub refuses anyway stays on
    screen with the edit kept."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      const jarvis = () => WAKE.draft.find(w => w.name === "hey_jarvis");
      const problem = () => wakeProblem(jarvis(), wakeEffective());
      const out = {};
      wakeEdit("hey_jarvis", w => wakeField(w, "d.url", "ha.local:8123"));
      out.scheme = problem();
      out.dirty = $("wwdirty").textContent;
      out.off = $("wwsave").disabled;
      await wakeSave();
      out.puts = hub.puts.length;
      wakeEdit("hey_jarvis", w => wakeField(w, "d.url", "https://me:pw@ha.local:8123"));
      out.userinfo = problem();
      wakeEdit("hey_jarvis", w => { wakeField(w, "d.url", "https://ha.local:8123");
                                    wakeField(w, "d.env", "eyJhbGciOiJIUzI1NiJ9.secret"); });
      out.token = problem();
      wakeEdit("hey_jarvis", w => wakeField(w, "d.env", ""));
      out.no_token = problem();
      wakeEdit("hey_jarvis", w => wakeField(w, "d.env", "SATELLITES_HA_TOKEN_KITCHEN"));
      out.env_hint = wakeEnvHint("SATELLITES_HA_TOKEN_KITCHEN");
      out.env_set = wakeEnvHint("SATELLITES_HA_TOKEN");
      wakeEdit("hey_jarvis", w => { w.action.fallback = "alexa"; });
      out.fallback = problem();
      wakeEdit("hey_jarvis", w => { w.action.fallback = null; });
      out.fixed = problem();
      hub.refuse = 1;
      await wakeSave();
      out.kept = !!WAKE.draft && jarvis().action.destination.token_env === "SATELLITES_HA_TOKEN_KITCHEN";
      out.notes = notes;
      console.log(JSON.stringify(out));
    """)
    assert got["scheme"] == "Write the address in full, starting with https or http.", got
    assert got["dirty"] == "hey jarvis needs a fix before the wake words can be saved.", got
    assert got["off"] is True and got["puts"] == 0, got
    assert got["userinfo"].startswith("Leave the user and password out"), got
    assert got["token"] == ("Write the name of the variable that holds the token, "
                            "never the token itself."), got
    assert got["no_token"] == "Name the variable that holds Home Assistant's token.", got
    assert got["env_hint"] == "Save, and the hub says whether SATELLITES_HA_TOKEN_KITCHEN is set."
    assert got["env_set"] == "SATELLITES_HA_TOKEN is set on the hub."
    assert got["fallback"] == "alexa is not a conversation word, so it cannot take over.", got
    assert got["fixed"] == "", got
    assert got["kept"], "a refused save lost the edit"
    assert ["bad", "422 the threshold is out of range"] in got["notes"], got


def test_a_word_being_set_up_is_unfinished_until_a_value_is_wrong(tmp_path):
    """A new webhook or language model action starts with its address (and
    model) empty. Save waits either way, but empty is a word being set up,
    said as such beside Save; a value the hub would refuse is a fix."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      const out = {};
      wakeEdit("hey_jarvis", w => wakeField(w, "dest", "webhook"));
      const jarvis = () => WAKE.draft.find(w => w.name === "hey_jarvis");
      const at = () => wakeProblemAt(jarvis(), wakeProblem(jarvis(), wakeEffective()));
      out.empty = at();
      out.empty_line = $("wwdirty").textContent;
      out.empty_off = $("wwsave").disabled;
      wakeEdit("hey_jarvis", w => wakeField(w, "d.url", "hooks.example.com"));
      out.wrong = at();
      out.wrong_line = $("wwdirty").textContent;
      wakeEdit("hey_jarvis", w => { wakeField(w, "dest", "llm");
                                    wakeField(w, "d.base_url", "https://llm.example.com/v1"); });
      out.model = at();
      console.log(JSON.stringify(out));
    """)
    assert got["empty"] == {"f": "d.url", "empty": True}, got
    assert got["empty_line"] == "Finish setting up hey jarvis to save the wake words.", got
    assert got["empty_off"] is True, got
    assert got["wrong"] == {"f": "d.url", "empty": False}, got
    assert got["wrong_line"] == "hey jarvis needs a fix before the wake words can be saved.", got
    assert got["model"] == {"f": "d.model", "empty": True}, got


def test_a_closed_word_says_which_one_a_save_will_change(tmp_path):
    """Save sends the whole set, and "Unsaved changes." beside it did not say
    which word. Each row is told whether it differs from the hub's copy, and
    a word marked for removal or changed says so closed."""
    got = run(tmp_path, MODERN + """
      const seen = {};
      const paint = wakeRowUpdate;
      wakeRowUpdate = (row, w, live, removed, words, ptt, changed) => {
        seen[ptt ? "ptt" : w.name] = { removed, changed };
        return paint(row, w, live, removed, words, ptt, changed);
      };
      await satellitesRefresh();
      const calm = JSON.parse(JSON.stringify(seen));
      wakeEdit("hey_jarvis", w => { w.threshold = 0.65; });
      const edited = JSON.parse(JSON.stringify(seen));
      wakeEdit("hey_jarvis", w => { w.threshold = 0.5; });
      const undone = JSON.parse(JSON.stringify(seen));
      wakeEdit("ptt", w => wakeSetMode(w, "conversation", "ptt"));
      const ptt = JSON.parse(JSON.stringify(seen));
      console.log(JSON.stringify({ calm, edited, undone, ptt }));
    """)
    assert got["calm"] == {"hey_jarvis": {"removed": False, "changed": False},
                           "ptt": {"removed": False, "changed": False}}, got
    assert got["edited"]["hey_jarvis"]["changed"] is True, got
    assert got["edited"]["ptt"]["changed"] is False, got
    assert got["undone"]["hey_jarvis"]["changed"] is False, "an edit taken back still reads Changed"
    assert got["ptt"]["ptt"]["changed"] is True and got["ptt"]["hey_jarvis"]["changed"] is False, got


def test_push_to_talk_is_edited_like_a_word_and_is_never_a_trigger(tmp_path):
    """What a button set to Talk does is the hub's `ptt` block: a command or a
    conversation, sent only when it changed."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      wakeEdit("ptt", w => wakeSetMode(w, "trigger", "ptt"));
      const refused = (WAKE.draftPtt || WAKE.server.ptt).mode;
      const still_clean = WAKE.draft === null;
      wakeEdit("ptt", w => wakeSetMode(w, "conversation", "ptt"));
      wakeEdit("ptt", w => wakeField(w, "dest", "ha_conversation"));
      wakeEdit("ptt", w => wakeField(w, "lang", "pt-BR"));
      const line = wakeLine(WAKE.draftPtt, true);
      await wakeSave();
      console.log(JSON.stringify({ refused, still_clean, line, ptt: last().ptt, keys: Object.keys(last()),
                                   words: last().words.map(w => w.name) }));
    """)
    assert got["refused"] == "command", "push-to-talk became a trigger"
    assert got["still_clean"], "a refused mode left an unsaved change behind"
    assert got["line"] == "Conversation · Home Assistant · pt-BR", got
    ptt = got["ptt"]
    assert ptt["mode"] == "conversation" and ptt["language"] == "pt-BR"
    assert ptt["action"]["destination"] == {"type": "ha_conversation", "url": "https://ha.local:8123",
                                            "token_env": "SATELLITES_HA_TOKEN"}, ptt
    assert "name" not in ptt and "threshold" not in ptt and "satellites" not in ptt
    assert got["keys"] == ["words", "ptt"] and got["words"] == ["hey_jarvis"], got


def test_a_custom_model_is_uploaded_and_deleted_only_when_no_word_uses_it(tmp_path):
    """The .onnx is the body and its name the query, taken from the file when
    none is typed. A built-in's name is refused before the upload. A model a
    word uses, saved or staged, is not deleted: the hub would answer 409, or
    the next Save a 422."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      $("wwmodelname").value = "";
      $("wwfile").files = [{ name: "Computer.onnx" }];
      await wakeModelUpload();
      const uploaded = { calls: hub.calls.slice(), custom: WAKE.server.custom,
                         offered: WAKE.server.available.includes("Computer") };
      $("wwmodelname").value = "alexa";
      $("wwfile").files = [{ name: "x.onnx" }];
      await wakeModelUpload();
      const builtin = notes[notes.length - 1];
      $("wwfile").files = [];
      await wakeModelUpload();
      const nofile = notes[notes.length - 1];
      wakeAdd("Computer");
      const staged_in_use = wakeModelInUse("Computer");
      hub.calls.length = 0;
      await wakeModelDelete("Computer", stand());
      const refused = hub.calls.length;
      WAKE.draft = null;
      await wakeModelDelete("Computer", stand());
      console.log(JSON.stringify({ uploaded, builtin, nofile, staged_in_use, refused,
                                   deleted: hub.calls, custom: WAKE.server.custom,
                                   saved_in_use: wakeModelInUse("hey_jarvis") }));
    """)
    assert got["uploaded"]["calls"] == [["POST", "/satellites/wake-words/models?name=Computer",
                                         "application/octet-stream"]], got
    assert got["uploaded"]["custom"] == ["lumos", "Computer"] and got["uploaded"]["offered"]
    assert got["builtin"] == ["bad", "That is a built-in wake word's name, so give yours another."]
    assert got["nofile"] == ["warn", "Choose an .onnx file first."]
    assert got["staged_in_use"] is True and got["refused"] == 0, got
    assert got["deleted"] == [["DELETE", "/satellites/wake-words/models/Computer"]], got
    assert got["custom"] == ["lumos"], got
    assert got["saved_in_use"] is True


def test_try_a_word_sends_the_saved_word_and_prints_the_reply(tmp_path):
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      $("routesay").value = "  what time is it ";
      $("routeword").value = "hey_jarvis";
      await wakeTry();
      console.log(JSON.stringify({ call: hub.calls[0],
        line: satelliteRouted({ rule_id: "hey_jarvis", transcript: "what time is it",
                                reply_text: "It is four.", timings_ms: { total: 812 } }) }));
    """)
    assert got["call"] == ["POST", "/satellites/routing/test",
                           {"satellite": "any", "wake_word": "hey_jarvis", "text": "what time is it"}]
    assert got["line"] == 'hey jarvis: "what time is it" answered "It is four." in 812 ms', got


def test_conversations_and_triggers_are_logged_and_end_the_wait(tmp_path):
    """Each new event is a line in words, and a turn, the end of a
    conversation or a trigger clears Listening the way a routed reply did. A
    conversation in progress is its own state on the row, with its word."""
    got = run(tmp_path, MODERN + """
      let source = null;
      globalThis.EventSource = window.EventSource = class { constructor() { source = this; } };
      await satellitesRefresh();
      const id = "aaaaaaaaaaaa";
      const send = ev => source.onmessage({ data: JSON.stringify({ at: 1000, satellite: id, ...ev }) });
      const woke = () => SATELLITES.woke.has(id);
      const out = { lines: {}, cleared: {} };
      for (const [kind, ev] of [
          ["turn", { type: "turn", turn: 2, rule_id: "hey_jarvis", transcript: "and tomorrow",
                     language: "en", reply_text: "Rain.", timeline_ms: { first_audio: 912.4 } }],
          ["conversation_ended", { type: "conversation_ended", rule_id: "hey_jarvis", turns: 3,
                                   reason: "phrase" }],
          ["triggered", { type: "triggered", wake_word: "lumos", score: 0.91 }]]) {
        send({ type: "wake", wake_word: "hey_jarvis", score: 0.8 });
        const before = woke();
        send(ev);
        out.cleared[kind] = before && !woke();
        out.lines[kind] = satEventWhat(ev);
      }
      out.lines.started = satEventWhat({ type: "conversation_started", rule_id: "hey_jarvis",
                                         wake_word: "alexa", reason: "fallback", from_rule: "alexa" });
      out.lines.ended_odd = satEventWhat({ type: "conversation_ended", turns: 1, reason: "listening failed" });
      out.lines.ended_turn = satEventWhat({ type: "turn", turn: 3, rule_id: "hey_jarvis",
                                            transcript: "thanks", ended: true });
      out.heard = WAKE.heard.get("lumos").score;
      out.bad = [{ type: "routed", rule_id: "hey_jarvis", error: "Home Assistant refused" },
                 { type: "ota", state: "failed", error: "bad signature" },
                 { type: "conversation_ended", turns: 1, reason: "error" },
                 { type: "conversation_ended", turns: 1, reason: "silence" },
                 { type: "ota", state: "verified" }, { type: "button", button: "play", action: "press" }]
        .map(satEventBad);
      const sat = { id, name: "Kitchen", adopted: true, online: true, config: {}, status: {},
                    listening: { state: "listening", conversation: "replying",
                                 session: { rule_id: "hey_jarvis", turns: 2 } },
                    latency: { turns: 12, p50_first_audio_ms: 1284 } };
      const s = satState(sat, satMem());
      out.state = [s.word, s.kind, s.line];
      out.fresh = satState({ ...sat, listening: { session: { rule_id: "hey_jarvis", turns: 0 } } },
                           satMem()).line;
      out.latency = satLatency(sat);
      out.none = satLatency({ latency: null });
      console.log(JSON.stringify(out));
    """)
    assert got["cleared"] == {"turn": True, "conversation_ended": True, "triggered": True}, got
    lines = got["lines"]
    assert lines["turn"] == 'hey jarvis, turn 2: "and tomorrow" (en) answered "Rain.", first sound after 912 ms'
    assert lines["conversation_ended"] == "conversation ended after 3 turns: an ending phrase"
    assert lines["triggered"] == "triggered lumos (0.91)"
    assert lines["started"] == "conversation with hey jarvis started, taking over from alexa"
    assert lines["ended_odd"] == "conversation ended after 1 turn: listening failed"
    assert lines["ended_turn"] == 'hey jarvis, turn 3: "thanks" ended it'
    assert got["heard"] == 0.91, "a trigger's score does not reach its row's Last heard"
    assert got["bad"] == [True, True, True, False, False, False], "a failure is not marked in the log"
    assert got["state"] == ["In conversation", "running", "Conversation with hey jarvis · 2 turns"], got
    assert got["fresh"] == "Conversation with hey jarvis", got
    assert got["latency"] == "1.3 s to first sound, median of 12 replies", got
    assert got["none"] == ""


def test_a_token_is_any_name_the_secret_store_can_hold(tmp_path):
    """Names resolve in the gateway's secret store now, never in the hub's
    environment (D47), so the hub no longer refuses its own settings' names
    and neither does the page: any name in the store's shape is a name. One
    that is not in that shape is still a fix before Save."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      const words = () => WAKE.draft || WAKE.server.words;
      const jarvis = () => words().find(w => w.name === "hey_jarvis");
      const out = {};
      for (const name of ["SATELLITES_MQTT_URL", "SATELLITES_HA_TOKEN_KITCHEN", "satellites_ha_token"]) {
        wakeEdit("hey_jarvis", w => wakeField(w, "d.env", name));
        out[name] = wakeProblem(jarvis(), words());
      }
      console.log(JSON.stringify(out));
    """)
    assert got["SATELLITES_MQTT_URL"] in ("", None), got
    assert got["SATELLITES_HA_TOKEN_KITCHEN"] in ("", None), got
    assert got["satellites_ha_token"], got


def test_a_words_voice_picker_offers_its_own_language_by_name(tmp_path):
    """A word's Voice lists only the voices of its language, as a name and a
    sex under the language's accent, rather than every Kokoro id; a word that
    detects its language gets every voice, grouped by language. A voice saved
    before the language changed stays chosen, and says it no longer fits."""
    got = run(tmp_path, """
      VOICES = { kokoro: { voices: ["af_heart", "am_adam", "bf_emma", "ef_dora", "pf_dora",
                                    "pm_alex", "zf_xiaobei"] }, clones: [] };
      console.log(JSON.stringify({
        pt: wakeVoiceOptions("pt-BR", null),
        en: wakeVoiceOptions("en", null),
        auto: wakeVoiceOptions(null, null).map(o => o[2] || ""),
        other: wakeVoiceOptions("de", null),
        kept: wakeVoiceOptions("pt-BR", "am_adam").slice(-1)[0],
        gone: wakeVoiceOptions("pt-BR", "xx_old").slice(-1)[0],
        note: wakeMoreNote({ reply_to: "same", voice: "pf_dora" }, true, null),
      }));
    """)
    assert got["pt"] == [["", "The language's own voice"],
                         ["pm_alex", "Alex · male", "Portuguese (Brazil)"],
                         ["pf_dora", "Dora · female", "Portuguese (Brazil)"]], got["pt"]
    assert got["en"] == [["", "The language's own voice"],
                         ["am_adam", "Adam · male", "English (US)"],
                         ["af_heart", "Heart · female", "English (US)"],
                         ["bf_emma", "Emma · female", "English (UK)"]], got["en"]
    assert got["auto"] == ["", "English (US)", "English (US)", "English (UK)", "Portuguese (Brazil)",
                           "Portuguese (Brazil)", "Spanish", "Mandarin"], got["auto"]
    assert got["other"] == [["", "The language's own voice"]], got["other"]
    assert got["kept"] == ["am_adam", "Adam · male · English (US), not this word's language"]
    assert got["gone"] == ["xx_old", "xx_old, not a voice the stack lists"]
    assert "Dora · female" in got["note"] and "pf_dora" not in got["note"], got["note"]
    page = (Path(__file__).resolve().parents[1] / "app" / "static" / "ui.html").read_text()
    assert "satFillSelect(q('[data-f=\"a.voice\"]'), wakeVoiceOptions(w.language, a.voice))" in page


def test_the_double_check_is_chosen_per_word_and_round_trips_through_the_put_body(tmp_path):
    """Double-check and Also accept sit with a word's other settings, in
    every mode: shown as the hub keeps them (Record only for a hub that
    says nothing), sent back whole with a change, and a list longer than
    the hub takes is named before Save. Push-to-talk is never offered it."""
    got = run(tmp_path, MODERN + """
      hub.words[0].verify = { mode: "log", spellings: ["alexia"] };
      hub.words.push({ name: "lumos", threshold: 0.7, satellites: ["*"], mode: "trigger", language: null,
                       action: null, silence_ms: 800, colour: null,
                       conversation: { follow_up_s: 8, silence_ms: 600, end_phrases: null },
                       trigger: { feedback: "earcon", cooldown_s: 3, ends_conversation: false },
                       state: "ready", error: null });
      await satellitesRefresh();
      const shown = name => {
        const row = name === "ptt" ? WAKE.pttRow : WAKE.rows.get(name);
        const els = new Map(), find = row.querySelector;
        row.querySelector = sel => { if (!els.has(sel)) els.set(sel, find(sel)); return els.get(sel); };
        return () => ({ mode: row.querySelector('[data-f="v.mode"]').value,
                        spellings: row.querySelector('[data-f="v.spellings"]').value,
                        hidden: row.querySelector(".ww-verify").hidden === true });
      };
      const jarvis = shown("hey_jarvis"), lumos = shown("lumos"), ptt = shown("ptt");
      wakeRender();
      const before = { jarvis: jarvis(), lumos: lumos(), ptt: ptt().hidden };
      wakeEdit("hey_jarvis", w => wakeField(w, "v.mode", "on"));
      wakeEdit("lumos", w => wakeField(w, "v.spellings", "lumus, loo moss , "));
      const after = { jarvis: jarvis(), lumos: lumos() };
      await wakeSave();
      const body = { jarvis: sent("hey_jarvis").verify, lumos: sent("lumos").verify };
      const many = Array.from({ length: 13 }, (_, i) => "alexa" + i).join(", ");
      const say = text => { wakeEdit("lumos", w => wakeField(w, "v.spellings", text));
                            return wakeProblem(WAKE.draft.find(w => w.name === "lumos"), wakeEffective()); };
      const fixes = { thirteen: say(many), long: say("x".repeat(41)), twelve: say(many.split(", ").slice(1).join(", ")) };
      const at = wakeProblemAt(WAKE.draft.find(w => w.name === "lumos"), say(many)).f;
      console.log(JSON.stringify({ before, after, body, fixes, at }));
    """)
    assert got["before"] == {"jarvis": {"mode": "log", "spellings": "alexia", "hidden": False},
                             "lumos": {"mode": "log", "spellings": "", "hidden": False},
                             "ptt": True}, got["before"]
    assert got["after"]["jarvis"]["mode"] == "on", got["after"]
    assert got["after"]["lumos"]["spellings"] == "lumus, loo moss", got["after"]
    assert got["body"] == {"jarvis": {"mode": "on", "spellings": ["alexia"]},
                           "lumos": {"spellings": ["lumus", "loo moss"]}}, got["body"]
    fix = "Keep to 12 other spellings, each of 40 characters at most."
    assert got["fixes"] == {"thirteen": fix, "long": fix, "twelve": ""}, got["fixes"]
    assert got["at"] == "v.spellings"
    # Every mode's, as the ring colour is: data-when shows it for all three.
    page = (Path(__file__).resolve().parents[1] / "app" / "static" / "ui.html").read_text()
    assert '<div class="grid2 ww-verify" data-when="command conversation trigger">' in page


def test_a_wake_word_the_double_check_did_not_hear_is_logged_in_words(tmp_path):
    """The hub's wake_rejected: a wake word STT did not hear in the audio
    that held it. On, the wake was dropped; Record only, it went ahead and
    the line says what On would have done. Neither is a failure, and
    neither asks the hub for its lists again."""
    got = run(tmp_path, MODERN + """
      let source = null;
      globalThis.EventSource = window.EventSource = class { constructor() { source = this; } };
      await satellitesRefresh();
      const asked = hub.calls.length;
      const ev = mode => ({ type: "wake_rejected", at: 1000, satellite: "aaaaaaaaaaaa", word: "hey_jarvis",
                            score: 0.93, heard: "Obrigado.", mode });
      source.onmessage({ data: JSON.stringify(ev("on")) });
      console.log(JSON.stringify({ on: satEventWhat(ev("on")), log: satEventWhat(ev("log")),
                                   bad: satEventBad(ev("on")), asked: hub.calls.length - asked }));
    """)
    assert got["on"] == "hey jarvis ignored: heard “Obrigado.”", got
    assert got["log"] == "hey jarvis would have been ignored: heard “Obrigado.”", got
    assert got["bad"] is False and got["asked"] == 0, got
