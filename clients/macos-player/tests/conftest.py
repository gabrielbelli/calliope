import os
import pathlib
import subprocess

import pytest

PLAYER_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def defaults_harness(tmp_path_factory):
    """tests/harness/main.swift built against the real player/defaults.swift."""
    binary = tmp_path_factory.mktemp("harness") / "harness"
    subprocess.run(
        ["swiftc", "-swift-version", "5",
         str(PLAYER_ROOT / "tests/harness/main.swift"),
         str(PLAYER_ROOT / "player/defaults.swift"),
         "-o", str(binary)],
        check=True, capture_output=True, text=True,
    )

    def run(*arguments):
        # Each call is its own process, as the real first run is: a carry that only works
        # because the writing process still has the values cached would pass in-process and
        # fail on the user's machine.
        result = subprocess.run([str(binary), *arguments], check=True, capture_output=True, text=True)
        return result.stdout.strip()

    return run


@pytest.fixture
def suites(defaults_harness):
    """A throwaway pair of suite names, removed from the user's real preferences afterwards."""
    tag = f"com.gabrielbelli.calliope-player-test.{os.getpid()}"
    names = (f"{tag}.old", f"{tag}.new")
    yield names
    for name in names:
        # Deleting the domain leaves an empty plist behind in the user's real Preferences, and
        # one per test per run adds up. cfprefsd has already flushed by the time defaults
        # returns, so the file can go too.
        subprocess.run(["defaults", "delete", name], capture_output=True)
        (pathlib.Path.home() / "Library/Preferences" / f"{name}.plist").unlink(missing_ok=True)
