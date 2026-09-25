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
      wakeEdit("alexa", w => { wakeField(w, "d.base_url", "https://ollama.local:11434/v1");
                               wakeField(w, "d.model", "llama3.2"); wakeField(w, "d.env", ""); });
      await wakeSave();
      console.log(JSON.stringify({ needs, body: sent("alexa") }));
    """)
    body = got["body"]
    assert got["needs"] == "Write the address in full, starting with https or http.", got
    assert body["mode"] == "conversation"
    assert body["conversation"] == {"follow_up_s": 12, "end_phrases": ["thanks", "that's all"]}
    assert body["action"]["fallback"] is None
    assert body["action"]["destination"] == {"type": "llm", "base_url": "https://ollama.local:11434/v1",
                                             "model": "llama3.2", "api_key_env": None}, body


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
    assert body["trigger"] == {"feedback": "none", "cooldown_s": 5}
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
      out.line = wakeLine(WAKE.server.words.find(w => w.name === "hey_jarvis"));
      console.log(JSON.stringify(out));
    """)
    assert got["br"] == "pt-BR" and got["auto"] is None
    assert got["other_first"] == "pt-BR", "choosing Other threw the language away before a tag was typed"
    assert got["de"] == "de"
    assert got["bad"] == "Deutsch"
    assert got["bad_problem"] == "Write the language as a tag, for example de or nl-BE.", got
    assert got["save_off"] is True
    assert got["sent"] == "nl-BE"
    assert got["line"] == "Command · every satellite · Home Assistant Assist · nl-BE", got


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


def test_push_to_talk_is_edited_like_a_word_and_is_never_a_trigger(tmp_path):
    """The PLAY button's behaviour is the hub's `ptt` block: a command or a
    conversation, sent only when it changed."""
    got = run(tmp_path, MODERN + """
      await satellitesRefresh();
      wakeEdit("ptt", w => wakeSetMode(w, "trigger", "ptt"));
      const refused = (WAKE.draftPtt || WAKE.server.ptt).mode;
      const still_clean = WAKE.draft === null;
      wakeEdit("ptt", w => wakeSetMode(w, "conversation", "ptt"));
      wakeEdit("ptt", w => wakeField(w, "dest", "ha_assist"));
      wakeEdit("ptt", w => wakeField(w, "lang", "pt-BR"));
      const line = wakeLine(WAKE.draftPtt, true);
      await wakeSave();
      console.log(JSON.stringify({ refused, still_clean, line, ptt: last().ptt, keys: Object.keys(last()),
                                   words: last().words.map(w => w.name) }));
    """)
    assert got["refused"] == "command", "push-to-talk became a trigger"
    assert got["still_clean"], "a refused mode left an unsaved change behind"
    assert got["line"] == "Conversation · Home Assistant Assist · pt-BR", got
    ptt = got["ptt"]
    assert ptt["mode"] == "conversation" and ptt["language"] == "pt-BR"
    assert ptt["action"]["destination"] == {"type": "ha_assist", "url": "https://ha.local:8123",
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
    assert got["state"] == ["In conversation", "running", "Conversation with hey jarvis · 2 turns"], got
    assert got["fresh"] == "Conversation with hey jarvis", got
    assert got["latency"] == "1.3 s to first sound, median of 12 replies", got
    assert got["none"] == ""
