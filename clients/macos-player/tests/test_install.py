"""install.sh, run as pieces: the retirement of the old install and the model reuse.

The script itself is never run here — it downloads 330 MB, builds the player and writes into
the user's real ~/.local/share. Each test lifts one block of shell out of the file it ships and
runs that block against a temporary layout, so what is under test is the code that installs.
"""
import pathlib
import re
import subprocess

import pytest

INSTALL = pathlib.Path(__file__).resolve().parent.parent / "install.sh"


def block(first_line, last_line):
    """The lines of install.sh from `first_line` to `last_line`, inclusive."""
    text = INSTALL.read_text()
    start = text.index(first_line)
    end = text.index(last_line, start) + len(last_line)
    return text[start:end]


def run_shell(script, cwd):
    return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def layout(tmp_path):
    """An old install and a new one, side by side, both with something in them."""
    for name in ("old/.venv", "new/.venv", "extensions/kokoro.openclipext", "extensions/calliope.openclipext"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "old/kokoro-v1.0.onnx").write_text("model")
    (tmp_path / "old/voices-v1.0.bin").write_text("voices")
    (tmp_path / "new/calliope-player").write_text("binary")
    (tmp_path / "extensions/kokoro.openclipext/openclip.json").write_text("{}")
    (tmp_path / "extensions/calliope.openclipext/openclip.json").write_text("{}")
    return tmp_path


def retire(paths, runtime="new", old_runtime="old", extension="calliope", old_extension="kokoro"):
    script = f"""
set -euo pipefail
runtime="{paths}/{runtime}"
old_runtime="{paths}/{old_runtime}"
extension="{paths}/extensions/{extension}.openclipext"
old_extension="{paths}/extensions/{old_extension}.openclipext"
{block("retire_old_install() {", "\n}")}
retire_old_install
"""
    return run_shell(script, paths)


def test_removes_the_old_runtime_and_the_old_extension(layout):
    result = retire(layout)

    assert result.returncode == 0, result.stderr
    assert not (layout / "old").exists()
    assert not (layout / "extensions/kokoro.openclipext").exists()


def test_keeps_the_install_it_just_made(layout):
    retire(layout)

    assert (layout / "new/calliope-player").exists()
    assert (layout / "extensions/calliope.openclipext/openclip.json").exists()


def test_never_removes_the_new_install_when_the_names_match(layout):
    """After the rename has been through once, old and new are the same path. Removing then
    would delete the install this very run just built."""
    result = retire(layout, old_runtime="new", old_extension="calliope")

    assert result.returncode == 0, result.stderr
    assert (layout / "new/calliope-player").exists()
    assert (layout / "extensions/calliope.openclipext/openclip.json").exists()


def test_is_quiet_when_there_is_no_old_install(tmp_path):
    (tmp_path / "new").mkdir()
    (tmp_path / "extensions/calliope.openclipext").mkdir(parents=True)

    result = retire(tmp_path)

    assert result.returncode == 0, result.stderr


def test_retires_only_after_the_player_and_the_extension_are_installed(layout):
    """The model files are inside the old runtime. Retiring before they are copied across
    turns a re-run into a 330 MB download, and a failed build into no install at all."""
    text = INSTALL.read_text()
    call = text.index("\nretire_old_install\n")

    assert text.index("==> Model files") < call
    assert text.index("==> Player") < call
    assert text.index("==> OpenClip extension") < call


def test_the_player_builds_from_the_sources_install_sh_lists(tmp_path):
    """The build line is where the player is defined, not player/: the settings carry lives in
    a second file, and a swiftc line that names only main.swift fails at link time and leaves
    the user with no new binary at all. Nothing else here would notice — the sources are never
    compiled, only read — so this compiles exactly what the shipped line compiles. It builds to
    a temp path and the binary is never run: running it opens a window and starts speaking."""
    text = INSTALL.read_text()
    build = re.search(r'^swiftc [^\n]*(?:\\\n[^\n]*)*-o "\$helper/MacOS/[\w-]+"$',
                      text, re.M).group(0)
    sources = [str(INSTALL.parent / name) for name in re.findall(r'"\$here/([\w/.-]+\.swift)"', build)]

    assert sources, build
    # -O is what install.sh uses; dropped here because it doubles the time and cannot change
    # whether the sources are a complete program.
    result = subprocess.run(["swiftc", "-swift-version", "5", *sources, "-o", str(tmp_path / "player")],
                            capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_the_old_server_is_stopped_before_its_directory_goes(layout):
    """The old server holds kokoro-v1.0.onnx open and idles out fifteen minutes after the last
    request. Remove the directory under it and it spends those minutes running against files
    that are gone. The stub also pins the pattern to the old runtime: pointed at $runtime it
    would kill the server belonging to the install this run just made."""
    script = f"""
set -euo pipefail
runtime="{layout}/new"
old_runtime="{layout}/old"
extension="{layout}/extensions/calliope.openclipext"
old_extension="{layout}/extensions/kokoro.openclipext"
pkill() {{ echo "$* dir=$([ -d "$old_runtime" ] && echo present || echo gone)" >> "{layout}/pkill"; }}
{block("retire_old_install() {", "\n}")}
retire_old_install
"""
    result = run_shell(script, layout)

    assert result.returncode == 0, result.stderr
    assert (layout / "pkill").read_text().strip() == f"-TERM -f {layout}/old/server.py dir=present"


def test_reuses_the_model_files_from_the_old_runtime(layout):
    script = f"""
set -euo pipefail
runtime="{layout}/new"
old_runtime="{layout}/old"
models="https://example.invalid/models"
curl() {{ while [ $# -gt 0 ] && [ "$1" != "-o" ]; do shift; done; echo "$2" >> "{layout}/downloaded"; : > "$2"; }}
{block('echo "==> Model files"', "\ndone")}
"""
    result = run_shell(script, layout)

    assert result.returncode == 0, result.stderr
    assert (layout / "new/kokoro-v1.0.onnx").read_text() == "model"
    assert (layout / "new/voices-v1.0.bin").read_text() == "voices"
    assert not (layout / "downloaded").exists()


def test_downloads_what_the_old_runtime_does_not_have(tmp_path):
    (tmp_path / "new").mkdir()
    (tmp_path / "old").mkdir()
    (tmp_path / "old/voices-v1.0.bin").write_text("voices")
    script = f"""
set -euo pipefail
runtime="{tmp_path}/new"
old_runtime="{tmp_path}/old"
models="https://example.invalid/models"
curl() {{ while [ $# -gt 0 ] && [ "$1" != "-o" ]; do shift; done; echo "$2" >> "{tmp_path}/downloaded"; : > "$2"; }}
{block('echo "==> Model files"', "\ndone")}
"""
    result = run_shell(script, tmp_path)

    assert result.returncode == 0, result.stderr
    # Downloaded beside its name and renamed: [ -f ] cannot tell 310 MB from
    # 3 MB, so a Ctrl-C used to leave truncated bytes at the final name and
    # every later run skipped the download.
    assert (tmp_path / "downloaded").read_text().strip().endswith("kokoro-v1.0.onnx.part")
    assert (tmp_path / "new/kokoro-v1.0.onnx").exists(), "the part file was never renamed"
    assert not (tmp_path / "new/kokoro-v1.0.onnx.part").exists(), "the part file is left behind"
    assert (tmp_path / "new/voices-v1.0.bin").read_text() == "voices"
