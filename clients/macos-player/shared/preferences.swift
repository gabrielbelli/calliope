// What the daemon's Settings writes and the player reads, said once.
//
// TWO PROCESSES READ THESE AND ONLY ONE OF THEM HAS A WINDOW. The daemon's
// Settings is where a preference is chosen; the one-shot player is where it is
// used, after the window has long been closed. Spelled separately in each, a
// key renamed on one side reads as "the setting saves and does nothing", which
// is the defect shared/paths.swift was written to end for paths. This file is
// compiled into both for the same reason.

import Foundation

/// The suite both halves read. CALLIOPE_TEST_DEFAULTS IS FOR THE TEST HARNESS
/// ALONE, and exists so that rendering the Settings window in a test reads a
/// throwaway suite instead of the owner's real preferences -- the same reason
/// playerDefaults() takes its suite names as arguments. Nothing sets it in use.
let preferencesSuiteName = ProcessInfo.processInfo.environment["CALLIOPE_TEST_DEFAULTS"]
    ?? "com.gabrielbelli.calliope-player"
let preferences = UserDefaults(suiteName: preferencesSuiteName)!

/// A language Kokoro can speak, and what Calliope reads it with by default.
///
/// THE LANGUAGE IS IN THE VOICE'S NAME, and that is not decoration. Kokoro
/// derives its phonemiser from the first letter -- `pf_dora` is Portuguese
/// because of the `p` -- so a voice can only be offered for the language its
/// letter says. Portuguese words run through an English phonemiser are not an
/// accent, they are a different and much worse thing.
struct Language {
    /// The key in `voices`, and what `/voices` reports per voice.
    let code: String
    let name: String
    let badge: String
    /// NLLanguage's raw value, which is not always the code: Chinese is zh-Hans.
    let recognizer: String
    let prefixes: Set<Character>
    /// What a language nobody configured is read with.
    let standardVoice: String
}

/// Every language there is a voice for, in the order Settings lists them.
let languages: [Language] = [
    Language(code: "zh", name: "Chinese", badge: "ZH", recognizer: "zh-Hans",
             prefixes: ["z"], standardVoice: "zf_xiaobei"),
    Language(code: "en", name: "English", badge: "EN", recognizer: "en",
             prefixes: ["a", "b"], standardVoice: "af_heart"),
    Language(code: "fr", name: "French", badge: "FR", recognizer: "fr",
             prefixes: ["f"], standardVoice: "ff_siwis"),
    Language(code: "hi", name: "Hindi", badge: "HI", recognizer: "hi",
             prefixes: ["h"], standardVoice: "hf_alpha"),
    Language(code: "it", name: "Italian", badge: "IT", recognizer: "it",
             prefixes: ["i"], standardVoice: "if_sara"),
    Language(code: "ja", name: "Japanese", badge: "JA", recognizer: "ja",
             prefixes: ["j"], standardVoice: "jf_alpha"),
    Language(code: "pt", name: "Portuguese", badge: "PT", recognizer: "pt",
             prefixes: ["p"], standardVoice: "pf_dora"),
    Language(code: "es", name: "Spanish", badge: "ES", recognizer: "es",
             prefixes: ["e"], standardVoice: "ef_dora"),
]

let english = languages.first { $0.code == "en" }!

func language(code: String) -> Language? { languages.first { $0.code == code } }

func language(ofVoice name: String) -> Language? {
    guard let letter = name.first else { return nil }
    return languages.first { $0.prefixes.contains(letter) }
}

/// Where a voice runs. The user picks a voice; the voice says where it lives.
enum VoiceOrigin: String {
    case mac
    case calliope
}

/// "mac/af_heart" or "calliope/pf_dora": a voice and the side that speaks it.
///
/// THE SAME NAME CAN BE ON BOTH SIDES, and that is why the origin is part of
/// the reference rather than looked up. The Calliope server's fast voices are
/// Kokoro's own presets, so `pf_dora` exists on this Mac and on the server --
/// and which one answers is exactly the choice the Voices pane offers.
struct VoiceRef: Equatable {
    let origin: VoiceOrigin
    let name: String

    init(origin: VoiceOrigin, name: String) {
        self.origin = origin
        self.name = name
    }

    init?(_ ref: String) {
        let parts = ref.split(separator: "/", maxSplits: 1).map(String.init)
        guard parts.count == 2, let origin = VoiceOrigin(rawValue: parts[0]),
              !parts[1].isEmpty else { return nil }
        self.init(origin: origin, name: parts[1])
    }

    var ref: String { "\(origin.rawValue)/\(name)" }

    /// What the proxy is asked for. Kokoro's own names on both sides, so the
    /// prefix is the only thing that sends a request to the Calliope server
    /// rather than to this Mac.
    var model: String { origin == .mac ? "kokoro" : "calliope/kokoro" }
}

enum Preference {
    static let voicesKey = "voices"
    static let legacyVoiceKey = "voice"
    static let macKey = "macOn"
    static let keepLoadedKey = "keepLoaded"
    static let portKey = "proxyPort"
    static let calliopeKey = "calliopeOn"
    static let defaultPort = 47815

    /// The voice chosen per language, for the languages somebody chose one for.
    ///
    /// ONLY THE LANGUAGES THEY LISTED. A form with a row for every language
    /// Kokoro has makes somebody who reads English and Portuguese answer six
    /// questions about languages they never read. An absent language is not an
    /// unanswered question: it uses its standard voice.
    ///
    /// THE SINGLE VOICE OF THE PREVIOUS VERSION IS CARRIED ONCE. It was one
    /// voice for whichever language it spoke, so it becomes that language's
    /// row. Carried only while `voices` has never been written, so removing
    /// every row later is a choice that stays made; `voice` itself is left
    /// alone, as the renamed defaults suite was.
    static var voices: [String: String] {
        get {
            if let saved = preferences.dictionary(forKey: voicesKey) as? [String: String] {
                return saved
            }
            var carried: [String: String] = [:]
            if let legacy = preferences.string(forKey: legacyVoiceKey), !legacy.isEmpty,
               let spoken = language(ofVoice: legacy) {
                carried[spoken.code] = VoiceRef(origin: .mac, name: legacy).ref
                preferences.set(carried, forKey: voicesKey)
            }
            return carried
        }
        set { preferences.set(newValue, forKey: voicesKey) }
    }

    /// This Mac's own engine. ON UNLESS SOMEBODY TURNED IT OFF: absent is the
    /// state of every install before the switch existed, and those read aloud
    /// on this Mac.
    static var macOn: Bool {
        get { preferences.object(forKey: macKey) as? Bool ?? true }
        set { preferences.set(newValue, forKey: macKey) }
    }

    static var keepLoaded: Bool {
        get { preferences.bool(forKey: keepLoadedKey) }
        set { preferences.set(newValue, forKey: keepLoadedKey) }
    }

    /// Whether the Calliope switch is on. The address and key are the daemon's
    /// to know; the player only needs to know whether that side exists.
    static var calliopeOn: Bool { preferences.bool(forKey: calliopeKey) }

    /// The proxy's port on loopback. Below 1024 needs root and above 65535 is
    /// not a port, so anything outside that is the default rather than a
    /// server that cannot start.
    static var port: Int {
        get {
            let saved = preferences.integer(forKey: portKey)
            return (1024...65535).contains(saved) ? saved : defaultPort
        }
        set { preferences.set(newValue, forKey: portKey) }
    }
}

/// THE ONLY ADDRESS EITHER HALF TALKS TO. Loopback, always: the port is the one
/// part a setting may change, and the host is not a setting at all. Anything
/// that reaches another machine goes through the proxy, which is the one
/// process that holds the key.
var localServerURL: String { "http://127.0.0.1:\(Preference.port)" }
