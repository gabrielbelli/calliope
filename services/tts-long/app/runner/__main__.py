"""python -m app.runner serve | fingerprint | smoke [clip]

NOTHING HAPPENS ON IMPORT. Every command runs under the `__main__` guard at the
bottom, so importing this module, as the tests do, starts nothing. The worker,
the probe and `smoke`'s measurements run in processes started with
multiprocessing's `spawn`, and their targets live in worker.py and gpu.py:
spawn does not import a package's `__main__` again, so a function defined here
could not be found in the child.

    serve        the runner: reads the key, mints or loads the certificate,
                 logs its fingerprint and listens on :47600 (TLS 1.3)
    fingerprint  prints the SHA-256 tts-long pins as TTS_RUNNER_FINGERPRINT
    smoke        downloads the weights and measures each engine on this card:
                 the load time, the realtime factor and the device's peak VRAM

Settings are few and documented in RUNNER.md: RUNNER_API_KEY_FILE,
RUNNER_DEVICE, RUNNER_IDLE_SECONDS, RUNNER_MIN_FREE_MIB and TTS_ENGINES.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

STATE_DIR = Path("/state")
KEY_FILE_VARIABLE = "RUNNER_API_KEY_FILE"
KEY_FILE_DEFAULT = "/run/secrets/runner-key"
# Printable ASCII with no space, and long enough not to be guessed: `openssl
# rand -hex 32` gives 64 characters.
KEY_PATTERN = re.compile(r"[\x21-\x7e]{32,}")

# Five segments inside tts-long's chunker target (160) and maximum (280), the
# workload a long-form job really is. The same text as the go/no-go
# measurement in RUNNER.md, so the two can be compared.
SMOKE_TEXT = (
    "The lighthouse keeper climbed the spiral stairs every evening at dusk, "
    "counting the steps out loud so that he would notice at once if his legs "
    "ever began to fail him.",
    "By the time the lamp was lit, the fishing boats had usually turned for "
    "home, and he could follow their small lights across the bay until each "
    "one slipped behind the harbour wall.",
    "Some nights the fog came in so thick that he could not see the rail of his "
    "own gallery, and then he wound the old horn by hand and listened for an "
    "answer that rarely came.",
    "In the morning he wrote everything down in a ledger with a brass lock: the "
    "weather, the ships he had seen, the hours the lamp had burned, and "
    "anything else the sea had told him.",
    "When the inspectors finally arrived to automate the light, they found "
    "forty years of those ledgers in the store room, each one filled to the "
    "last page in the same careful hand.",
)


def fail(message: str) -> None:
    """Stop with exit code 1 and one sentence on stderr. Never a secret in it."""
    raise SystemExit(f"tts-runner: {message}")


def read_key(path: str) -> bytes:
    """The bearer key from its file, or exit naming the file and never its contents.

    THERE IS NO ENVIRONMENT-VARIABLE FORM AND NO UNAUTHENTICATED MODE. A key in
    the environment is readable by anything that can inspect the container,
    and a runner without one would speak for anybody on the LAN.
    """
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        fail(f"the key file {path} does not exist; set {KEY_FILE_VARIABLE} or "
             f"mount the key there (RUNNER.md, step 1)")
    except OSError as exc:
        fail(f"the key file {path} cannot be read ({type(exc).__name__}); it "
             f"must be readable by uid {os.getuid()}")
    try:
        key = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        key = ""
    if KEY_PATTERN.fullmatch(key) is None:
        fail(f"the key in {path} must be at least 32 printable ASCII characters "
             f"with no spaces; make one with `openssl rand -hex 32`")
    return key.encode("ascii")


def hosted(engines: dict, log=None) -> dict:  # noqa: ANN001
    """{runner service id: EngineSpec} for every engine with a local class."""
    services = {}
    for engine_id, spec in engines.items():
        if not spec.local:
            if log is not None:
                log.warning("%s is not hosted: it has no implementation that "
                            "runs in this process", engine_id)
            continue
        services[spec.facts.runner_service] = spec
    return services


def serve() -> None:
    from voice_common.logging import setup

    log = setup("tts-runner", "RUNNER")
    key = read_key(os.environ.get(KEY_FILE_VARIABLE) or KEY_FILE_DEFAULT)
    device = (os.environ.get("RUNNER_DEVICE") or "cuda").strip()

    from app.engines import ENGINES

    from . import api, server, tls
    from .jobs import Dispatcher, Store

    services = hosted(ENGINES, log)
    if not services:
        fail("no engine in TTS_ENGINES can run here, so there is nothing to host")
    cert, private = tls.ensure(STATE_DIR / "tls")
    store = Store(STATE_DIR)
    dispatcher = Dispatcher(store, services={s: spec.id for s, spec in services.items()},
                            device=device)
    app = api.create_app(dispatcher, services=services, key=key)
    config = server.build_config(app, cert, private)
    log.info("hosting %s on %s; listening on :%d (TLS 1.3); certificate sha256 %s",
             ", ".join(services), device, server.PORT, tls.fingerprint(cert))
    server.serve(config)


def fingerprint() -> None:
    from . import tls

    cert = STATE_DIR / "tls" / tls.CERT_NAME
    if not cert.is_file():
        fail(f"there is no certificate at {cert} yet; `serve` mints one on its "
             f"first start")
    print(tls.fingerprint(cert))


def smoke(args: list[str], factory: str = "app.synth:Synth") -> int:
    """Download the weights and measure every hosted engine on this device.

    HF_HUB_OFFLINE was turned off by `main` before anything was imported: the
    image runs offline so a load never asks Hugging Face for a revision, and
    this is the one command that is meant to download. The spawned measuring
    process inherits that environment.

    ONE ENGINE AT A TIME, EACH IN A PROCESS OF ITS OWN, and the next starts
    only once the last has exited (worker.measure says why). `factory` is
    the tests' seam, as it is the worker's.
    """
    import multiprocessing

    from voice_common.logging import setup

    log = setup("tts-runner", "RUNNER")
    device = (os.environ.get("RUNNER_DEVICE") or "cuda").strip()
    clip = args[0] if args else None

    from app.engines import ENGINES

    from . import worker
    from .gpu import probe_now

    ok, said = probe_now(device)
    print(said, flush=True)
    if not ok:
        return 1
    context = multiprocessing.get_context("spawn")
    failed = False
    for service, spec in hosted(ENGINES, log).items():
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=worker.measure,
            args=(child, spec.id, device, SMOKE_TEXT, clip, factory),
            daemon=False, name=f"smoke-{spec.id}")
        process.start()
        child.close()
        try:
            figures = parent.recv()
        except (EOFError, OSError):
            figures = None
        process.join()
        parent.close()
        if figures is None:
            print(f"{service}: the measurement exited with code "
                  f"{process.exitcode}; its error is above", flush=True)
            failed = True
            continue
        wall = figures["wall_seconds"]
        peak = figures["peak_mib"]
        print(f"{service}: load {figures['load_seconds']:.1f} s, realtime factor "
              f"{figures['audio_seconds'] / wall if wall else 0.0:.3f}x"
              + (f", device peak {peak} MiB" if peak is not None else ""),
              flush=True)
    return 1 if failed else 0


USAGE = "usage: python -m app.runner serve | fingerprint | smoke [clip]"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else ""
    if command == "serve":
        serve()
        return 0
    if command == "fingerprint":
        fingerprint()
        return 0
    if command == "smoke":
        os.environ["HF_HUB_OFFLINE"] = "0"
        return smoke(args[1:])
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
