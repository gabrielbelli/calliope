"""Is there a usable GPU, and how full is it: the probe and the sampler.

NEITHER RUNS ON THE REQUEST PATH. The probe launches real kernels in a child
process at start (torch is never imported by the server), and the sampler asks
`nvidia-smi` every five seconds on a thread of its own. Handlers only ever
read what they left behind: the first `nvidia-smi` on a host without
persistence mode can take more than a second, and tts-long's offer timeout is
three.

NEITHER SAYS ANYTHING PRIVATE. The GPU's model name goes to this process's log
and nowhere else; /v1/status carries load figures and a verdict, because
tts-long's gateway inlines that document into an unauthenticated /health.
"""

from __future__ import annotations

import logging
import multiprocessing
import shutil
import subprocess
import threading

log = logging.getLogger("tts-runner.gpu")

# A cold CUDA context on an old card can take a minute; three is the bound on
# a probe that is wedged rather than slow.
PROBE_TIMEOUT_S = 180.0
# How soon a failed probe asks again: a driver that comes up after the
# container, or a card another process was holding.
PROBE_RETRY_S = 60.0
SAMPLE_EVERY_S = 5.0
SAMPLE_TIMEOUT_S = 3.0

CHECKING, OK, FAILED = "checking", "ok", "failed"

QUERY = ["nvidia-smi",
         "--query-gpu=utilization.gpu,memory.used,memory.free,power.draw,pstate",
         "--format=csv,noheader,nounits", "-i", "0"]


def _probe_child(conn) -> None:  # noqa: ANN001 - a multiprocessing Connection
    """The deciding test, in a process of its own: launch real kernels.

    `is_available()` alone says a driver answered. What fails on an unsupported
    card is the first kernel ("no kernel image is available for execution on
    the device" on an RTX 50-series against this wheel), so a convolution, which
    goes through cuDNN, and a matrix product are run and read back.
    """
    try:
        import torch

        if not torch.cuda.is_available():
            conn.send({"ok": False, "reason": "no CUDA device is visible; start "
                       "the container with --gpus all"})
            return
        name = torch.cuda.get_device_name(0)
        major, minor = torch.cuda.get_device_capability(0)
        kernels = " ".join(torch.cuda.get_arch_list())
        _free, total = torch.cuda.mem_get_info()
        x = torch.randn(1, 1, 256, device="cuda")
        torch.nn.functional.conv1d(x, torch.randn(1, 1, 3, device="cuda")).sum().item()
        (torch.randn(64, 64, device="cuda")
         @ torch.randn(64, 64, device="cuda")).sum().item()
        conn.send({"ok": True, "summary": (
            f"GPU: {name}, compute {major}.{minor}, {total >> 20} MiB, torch "
            f"{torch.__version__}, CUDA {torch.version.cuda}, kernels {kernels}: "
            f"usable")})
    except Exception as exc:  # noqa: BLE001 - torch's own sentence is the answer
        conn.send({"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:300]}"})


def probe_now(device: str) -> tuple[bool, str]:
    """One probe, synchronously: (usable, the sentence to log)."""
    if not device.startswith("cuda"):
        return True, f"RUNNER_DEVICE={device}: no GPU is used (tests only)"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_probe_child, args=(child,), daemon=True,
                              name="gpu-probe")
    process.start()
    child.close()
    try:
        if not parent.poll(PROBE_TIMEOUT_S):
            return False, f"the GPU probe did not answer in {PROBE_TIMEOUT_S:.0f} s"
        answer = parent.recv()
    except (EOFError, OSError):
        return False, f"the GPU probe exited with code {process.exitcode}"
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)
        parent.close()
    if answer.get("ok"):
        return True, answer.get("summary", "usable")
    return False, answer.get("reason") or "the GPU probe failed"


class Probe:
    """The probe's verdict, refreshed off the request path.

    `state` is CHECKING until the first answer, then OK or FAILED. It runs once
    at start, again PROBE_RETRY_S after a failure, and again whenever `again()`
    is called, which the dispatcher does after a worker dies.
    """

    def __init__(self, device: str) -> None:
        self.device = device
        self.state = CHECKING
        self.reason = ""
        self._again = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="gpu-probe")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._again.set()

    def again(self) -> None:
        self._again.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.once()
            self._again.wait(PROBE_RETRY_S if self.state == FAILED else None)
            self._again.clear()

    def once(self) -> None:
        # NOT "checking" ON THE CPU. The answer there is immediate, and saying
        # "checking" for an instant after every worker crash would refuse a
        # submit that arrived in that instant for no reason.
        if self.device.startswith("cuda"):
            self.state = CHECKING
        ok, said = probe_now(self.device)
        if ok:
            log.info("%s", said)
            self.state, self.reason = OK, ""
        else:
            log.warning("no usable GPU: %s", said)
            self.state, self.reason = FAILED, said


def _number(field: str) -> float | None:
    try:
        return float(field)
    except ValueError:
        # "[N/A]" on a card or driver that does not report the field.
        return None


def parse(line: str) -> dict | None:
    """One line of the query above, as the figures /v1/status publishes."""
    fields = [f.strip() for f in line.strip().split(",")]
    if len(fields) != 5:
        return None
    util, used, free, power, pstate = fields
    as_int = (lambda v: int(v) if v is not None else None)
    return {"utilisation_pct": as_int(_number(util)),
            "memory_used_mib": as_int(_number(used)),
            "memory_free_mib": as_int(_number(free)),
            "power_watts": _number(power),
            "pstate": pstate if pstate and not pstate.startswith("[") else None}


class Sampler:
    """nvidia-smi every few seconds, cached. Handlers read; they never ask."""

    def __init__(self) -> None:
        self._latest: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def latest(self) -> dict | None:
        return self._latest

    def start(self) -> None:
        if self._thread is not None:
            return
        if shutil.which(QUERY[0]) is None:
            # THE GATE STAYS OPEN. With no reading there is nothing to refuse
            # work on, and the load figures are simply absent from /v1/status.
            log.warning("nvidia-smi is not on PATH: no GPU load figures, and "
                        "the free-memory gate is off")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="gpu-sampler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._latest = self.sample()
            self._stop.wait(SAMPLE_EVERY_S)

    @staticmethod
    def sample() -> dict | None:
        try:
            done = subprocess.run(QUERY, capture_output=True, text=True,
                                  timeout=SAMPLE_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode != 0 or not done.stdout.strip():
            return None
        return parse(done.stdout.splitlines()[0])


class NoSampler:
    """What a runner with RUNNER_DEVICE=cpu has: no card, so no figures."""

    def latest(self) -> dict | None:
        return None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass
