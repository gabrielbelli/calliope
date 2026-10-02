"""tts-runner: tts-long's own Chatterbox code on an NVIDIA GPU, behind offpeak's protocol.

WHAT IT IS. A separate image (Containerfile.runner, `calliope-tts-runner`) for a
Linux host with an NVIDIA card. It answers the routes tts-long's RunnerClient
already calls on offpeak, so tts-long needs configuration and no new client.
It is always on: no idle detection, no yielding, no presence.

WHY IT LIVES INSIDE tts-long. The GPU path runs the same `Synth` the CPU lane
runs -- the float32 guard, the watermark stub, the checkpoint assertion, the
catalogue-driven generate() arguments and the token count -- given
`device="cuda"`. A separate service would have to import another service's
`app/` package or copy it.

THE SHAPE. `serve` is a server process that never imports torch, plus at most
one worker process holding one loaded engine. The dispatcher thread is the only
thing that starts or stops the worker, because process exit is the only way to
free all of the VRAM, the CUDA context included.

    __main__.py   the CLI: serve | fingerprint | smoke
    api.py        the pre-auth guard and the routes
    jobs.py       the store, the jobs and the dispatcher thread
    worker.py     the child process that speaks
    gpu.py        the start-up probe and the nvidia-smi sampler
    tls.py        the self-signed certificate tts-long pins
    server.py     uvicorn, TLS 1.3 only, and the request-head deadline

services/tts-long/RUNNER.md is the operator's guide.
"""
