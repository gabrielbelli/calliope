// Where the player keeps the two things it remembers between runs, and the one-off carry that
// stops the rename from throwing them away.
import Foundation

let defaultsSuiteName = "com.gabrielbelli.calliope-player"
let legacyDefaultsSuiteName = "com.gabrielbelli.kokoro-player"
/// Everything the player persists: the speed step and whether the reader was left open.
// "voice" is deliberately NOT carried: it did not exist before the rename, so
// there is nothing under the old name to carry, and an empty value is the
// default behaviour anyway.
let carriedDefaultsKeys = ["speed", "karaoke"]

// THE SUITE NAMES ARE ARGUMENTS SO THAT A TEST CAN RUN THIS EXACT FUNCTION, and for nothing else:
// the player calls it with none. Without them a test can only reach carryDefaults directly, which
// leaves this call — the single line the whole carry hangs on — covered by nothing. Deleting it
// was measured against the suite as it stood: 22 tests, all still green, while every reader on the
// machine drops to 1× with the reader shut on the first run after the rename. The alternative,
// letting a test call this with the real names, writes into the settings the owner is using right
// now and hands the fixture's teardown a domain that is not the test's to delete.
func playerDefaults(suiteName: String = defaultsSuiteName,
                    legacySuiteName: String = legacyDefaultsSuiteName) -> UserDefaults {
    carryDefaults(from: legacySuiteName, to: suiteName, keys: carriedDefaultsKeys)
    return UserDefaults(suiteName: suiteName)!
}

// RENAMING THE DEFAULTS SUITE IS A SILENT FACTORY RESET, AND NOBODY ASKED FOR ONE. The suite
// name is part of the path to every key, so com.gabrielbelli.calliope-player begins life empty:
// someone who reads at 2× with the reader open gets a 1× capsule and a closed reader on the
// first run after the rename, with no message saying why and nothing to undo it. The old values
// are still on disk under the old name, so carry them across on a first run that finds none of
// our keys under the new name and at least one under the old.
//
// The old suite is left exactly as it was. It is a few hundred bytes, and it is the only copy of
// those settings if this rename ever has to be walked back. Deleting it would also make the
// carry unrepeatable if the new suite were lost.
//
// The guard is "the new suite holds none of these keys", not "the new suite is new": once the
// player has written either key, a deliberate choice exists under the new name, and what the old
// suite still remembers must never be allowed to overwrite it.
@discardableResult
func carryDefaults(from legacySuiteName: String, to suiteName: String, keys: [String]) -> Bool {
    let standard = UserDefaults.standard
    // persistentDomain reads that suite's own domain and nothing else. Reading through a
    // UserDefaults(suiteName:) object would also see the global domain, where an unrelated
    // "speed" belonging to some other program would look like a value of ours and block the carry.
    let current = standard.persistentDomain(forName: suiteName) ?? [:]
    guard keys.allSatisfy({ current[$0] == nil }) else { return false }
    let legacy = standard.persistentDomain(forName: legacySuiteName) ?? [:]
    let carried = keys.filter { legacy[$0] != nil }
    guard !carried.isEmpty, let settings = UserDefaults(suiteName: suiteName) else { return false }
    for key in carried { settings.set(legacy[key], forKey: key) }
    return true
}
