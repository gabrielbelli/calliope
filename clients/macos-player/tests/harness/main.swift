// Drives player/defaults.swift from a test, against throwaway suite names. It exists because the
// carry cannot be exercised through the player: the player opens a window and starts speaking.
// Modes: set <suite> <key> <float|bool> <value> | carry <old> <new> | dump <suite>
//        open <old> <new>  — the whole of playerDefaults, the call main.swift makes
import Foundation

let arguments = CommandLine.arguments

switch arguments.count > 1 ? arguments[1] : "" {
case "set":
    let store = UserDefaults(suiteName: arguments[2])!
    if arguments[4] == "float" {
        store.set(Float(arguments[5])!, forKey: arguments[3])
    } else {
        store.set(arguments[5] == "true", forKey: arguments[3])
    }
case "carry":
    print(carryDefaults(from: arguments[2], to: arguments[3], keys: carriedDefaultsKeys) ? "carried" : "kept")
case "open":
    // Through playerDefaults, not carryDefaults: what is under test here is that opening the
    // settings is what performs the carry, and that the store handed back is the new suite.
    let store = playerDefaults(suiteName: arguments[3], legacySuiteName: arguments[2])
    print("speed=\(store.float(forKey: "speed"))")
    print("karaoke=\(store.bool(forKey: "karaoke") ? "1" : "0")")
case "dump":
    let values = UserDefaults.standard.persistentDomain(forName: arguments[2]) ?? [:]
    for key in carriedDefaultsKeys.sorted() { print("\(key)=\(values[key].map { "\($0)" } ?? "-")") }
default:
    FileHandle.standardError.write(Data("usage: harness set|carry|open|dump …\n".utf8))
    exit(2)
}
