# tts-runner: tts-long on an NVIDIA GPU

tts-long speaks long-form audio on its own CPU, at about a quarter of realtime.
tts-runner lends it a GPU. It is a separate container that runs tts-long's own
Chatterbox code on CUDA and answers the same runner protocol as offpeak, so
tts-long needs no new code, only a few settings.

It is always on. It does not watch for games or users and never hands the card
back mid-job. If you want a desktop that lends its GPU only while idle, use
offpeak. You can use both: tts-long drives every runner it is given at once,
and sends each job to the free one that will finish it first, with its own CPU
as the last fallback.

## What you need

- Linux on amd64, with an NVIDIA GPU from Pascal (GTX 10-series) to Ada or Hopper.
  RTX 50-series (Blackwell) is not supported by the torch build this image uses.
- An NVIDIA driver for CUDA 12 (525 or newer). 580 is the last branch for Pascal.
- Docker with the NVIDIA Container Toolkit. Check it first:

  ```bash
  docker run --rm --gpus all debian:trixie-slim nvidia-smi
  ```

  In an LXC container, the toolkit usually needs `no-cgroups = true` in
  `/etc/nvidia-container-runtime/config.toml`.
- Enough free VRAM for the larger engine. `smoke` prints each engine's peak;
  expect 4–5.5 GB. About 7 GB of disk for the weights and 7 GB for the image.

## Install

1. **Make a key** for this runner alone, readable only by the container's user
   (uid 1000). Do not reuse offpeak's key.

   ```bash
   (umask 077 && openssl rand -hex 32 > runner-key)
   sudo chown 1000:1000 runner-key
   ```

   Read it back later with `sudo cat runner-key`.

2. **Save the compose file** below as `compose.yaml` next to `runner-key`.
   Replace `<gpu-host-lan-ipv4>` with this host's LAN address and `vX.Y.Z`
   with the release you run.

   ```yaml
   name: tts-runner
   services:
     tts-runner:
       image: ghcr.io/gabrielbelli/calliope-tts-runner:vX.Y.Z   # a version tag, never :latest
       restart: unless-stopped
       init: true
       stop_grace_period: 30s
       user: "1000:1000"
       cap_drop: [ALL]
       security_opt: ["no-new-privileges:true"]
       read_only: true
       tmpfs: [/tmp, /home/tts]
       mem_limit: 8g
       pids_limit: 512
       ports:
         - "<gpu-host-lan-ipv4>:47600:47600"   # one IPv4 address: no [::] listener
       volumes:
         - models:/models
         - state:/state
       secrets:
         - runner-key            # appears as /run/secrets/runner-key
       deploy:
         resources:
           reservations:
             devices:
               - driver: nvidia
                 count: 1
                 capabilities: [gpu]
       healthcheck:
         test: ["CMD", "python", "-c", "import socket; socket.create_connection(('127.0.0.1', 47600), 3)"]
         interval: 60s
         timeout: 10s
         retries: 3
         start_period: 60s
   secrets:
     runner-key:
       file: ./runner-key
   volumes:
     models:
     state:
   ```

   The same thing as a single `docker run`. `--stop-timeout 30` matters:
   Docker's default of 10 s stops the runner before it has cancelled its job.

   ```bash
   docker run -d --name tts-runner --init --restart unless-stopped --gpus all \
     --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges \
     --read-only --tmpfs /tmp --tmpfs /home/tts --memory 8g --pids-limit 512 \
     --stop-timeout 30 -p <gpu-host-lan-ipv4>:47600:47600 \
     -v tts-runner-models:/models -v tts-runner-state:/state \
     -v "$PWD/runner-key:/run/secrets/runner-key:ro" \
     ghcr.io/gabrielbelli/calliope-tts-runner:vX.Y.Z
   ```

3. **Download the weights and measure the card**, about 7 GB the first time:

   ```bash
   docker compose run --rm tts-runner python -m app.runner smoke
   ```

   The image runs offline, and `smoke` alone turns that off to download. It
   prints the GPU, then for each engine the load time, the realtime factor and
   the peak VRAM. Keep the two realtime factors for step 6. To measure with a
   voice you use, put its clip in the `state` volume and name it:
   `python -m app.runner smoke /state/clip.wav`.

4. **Start it** and read the certificate fingerprint:

   ```bash
   docker compose up -d
   docker compose exec tts-runner python -m app.runner fingerprint
   ```

5. **Firewall the port** so only the Calliope host can reach it. Docker's
   published ports bypass `ufw`, so use the `DOCKER-USER` chain:

   ```bash
   sudo iptables -I DOCKER-USER -p tcp --dport 47600 ! -s <calliope-host> -j DROP
   ```

   Make the rule persistent in whatever way your distribution does. Then check
   that the port listens on the IPv4 address only, and that a third machine
   on the LAN cannot connect:

   ```bash
   ss -ltn | grep 47600                 # one line, the LAN IPv4 address, no [::]
   nc -vz -w 3 <runner-host> 47600      # from a third machine: must fail
   ```

6. **Point tts-long at it** and restart tts-long. If tts-long has no runner
   yet, use the first set of settings. If offpeak is already `TTS_RUNNER_*`,
   add this one as the second, `TTS_RUNNER2_*`, and keep both.

   ```yaml
   # the only runner
   TTS_RUNNER_HOST: "<runner-host>"
   TTS_RUNNER_PORT: "47600"
   TTS_RUNNER_FINGERPRINT: "<from step 4>"
   TTS_REALTIME_FACTOR_RUNNER: "<baseline rate from step 3>"
   TTS_REALTIME_FACTOR_RUNNER_CHATTERBOX_TURBO: "<turbo rate from step 3>"

   # or beside offpeak, as the second runner
   TTS_RUNNER2_HOST: "<runner-host>"
   TTS_RUNNER2_PORT: "47600"
   TTS_RUNNER2_FINGERPRINT: "<from step 4>"
   TTS_RUNNER2_LABEL: "Linux GPU"
   TTS_REALTIME_FACTOR_RUNNER2: "<baseline rate from step 3>"
   TTS_REALTIME_FACTOR_RUNNER2_CHATTERBOX_TURBO: "<turbo rate from step 3>"
   ```

   In Admin › Secrets, store the contents of `runner-key` under the secret
   that matches: `TTS_RUNNER_API_KEY` for the only runner, `TTS_RUNNER2_API_KEY`
   for the second. Make tts-long its consumer and add
   `https://<runner-host>:47600` to its allowed hosts. Each runner has its own
   secret, and the key goes only to the host its secret names.

   Before trusting the fingerprint, check it from the Calliope host:

   ```bash
   openssl s_client -connect <runner-host>:47600 </dev/null 2>/dev/null | openssl x509 -outform der | sha256sum
   ```

7. **Check it.** The Jobs tab in Calliope has one card per runner. This one
   reads "GPU runner: ready" (or its label, "Linux GPU: ready"), with
   "always-on" at its right edge. Submit a long-form job. The card reads
   "busy · working", the job's row names the runner, and `nvidia-smi` on the
   GPU host shows a `python` process while it speaks.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `RUNNER_API_KEY_FILE` | `/run/secrets/runner-key` | The bearer key, at least 32 characters. Required |
| `RUNNER_DEVICE` | `cuda` | `cpu` only for tests |
| `RUNNER_IDLE_SECONDS` | `600` | After this long without a job, the model is unloaded and all VRAM is freed |
| `RUNNER_MIN_FREE_MIB` | `4608` | With no model loaded, refuse work while less VRAM than this is free. Set it to the larger engine's peak from `smoke` plus 256. `0` turns it off |
| `TTS_ENGINES` | `chatterbox,chatterbox-turbo` | Which engines this runner offers |
| `RUNNER_LOG_LEVEL` | `INFO` | The log level |

## How it behaves

- **One job at a time**, in the order received. The first job after an idle
  unload pays the cold load: about 20 s for baseline and about a minute for Turbo.
- **One engine in VRAM at a time.** A job for the other engine unloads the first.
- **Beside offpeak, the free and faster one gets the job.** tts-long weighs
  each runner by its own measured rate, so a job goes to this runner when it
  is the quicker of the free ones, and to the other runner when this one is
  busy. Its CPU takes the job when no runner is free or none is clearly faster.
- **A restart forgets jobs.** tts-long notices and speaks the job on another
  runner or its CPU. Streamed jobs always run on tts-long's CPU and never come
  here.
- **A job tts-long stops asking about** is cancelled after a minute, so it
  cannot hold the GPU for nobody.
- **If the GPU is missing, broken or full,** the runner says so, and tts-long
  sends its jobs elsewhere.
- **Voice clips** that tts-long uploads are kept under `/state/assets` so they
  cross the network once. They are never served back. To forget them, delete
  that directory and restart tts-long.
- **The certificate** lives in `/state/tls`. Deleting the `state` volume mints
  a new one, and tts-long then refuses the runner until you update the fingerprint.

## Upgrading

Each Calliope release publishes a runner tag, and each new tag is a full pull of
about 3.5 GB. The protocol does not change between releases, so move your tag
only when the runner changed. This prints nothing when it did not:

```bash
git diff --stat vOLD vNEW -- packages/common services/tts-long/Containerfile.runner \
  services/tts-long/requirements.txt services/tts-long/app/__init__.py \
  services/tts-long/app/engines.py services/tts-long/app/synth.py services/tts-long/app/runner
```

After an upgrade, `docker image prune` frees the old image.

## Troubleshooting

| What you see | Cause | Fix |
|---|---|---|
| The card says "not answering" | Wrong host or port, firewall, or the container is down | `docker compose ps`; check the firewall rule and the address in `ports` |
| tts-long logs `certificate fingerprint mismatch` | The certificate was reminted | Copy the new fingerprint (step 4) |
| tts-long logs `401` and "not allowed for" | The origin is missing from the secret's allowed hosts | Admin › Secrets |
| tts-long logs `401` with the origin allowed | The secret holds another runner's key | Admin › Secrets: set this runner's key under its own secret |
| tts-long refuses to start: "both name" | Two runner settings point at the same host and port | One machine is one runner: remove one of them |
| The runner logs `no usable GPU` | The container started without a GPU, or the card is not supported | `--gpus all` or the compose reservation; read the probe line |
| Jobs fail with a Hugging Face offline error | The weights were never downloaded | Step 3 |
| The card says "Busy with something else." | Something else is using the card | Wait, or lower `RUNNER_MIN_FREE_MIB` |
| Baseline jobs still run on the CPU | The GPU is not fast enough for baseline on this card, and tts-long chose correctly | Nothing to fix. Compare the two rates |

## Switching between this and offpeak

To use only one of the two, give tts-long only that runner's settings as
`TTS_RUNNER_*` and its key as `TTS_RUNNER_API_KEY`, with its origin in the
secret's allowed hosts, and restart tts-long. The two runners keep separate
keys, so a compromised machine cannot call the other one.
