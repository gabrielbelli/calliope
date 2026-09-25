# openWakeWord training image: openWakeWord's own automatic training pipeline
# (openwakeword/train.py + a YAML config), with every version pinned.
#
#   docker build -t wakeword-train:1 tools/wakeword-train
#
# CUDA 12.2 is what driver 535 reports as its ceiling (nvidia-smi on holocron).
# The CUDA libraries themselves come from the torch wheels (CUDA 12.1, cuDNN 9);
# LD_LIBRARY_PATH below points onnxruntime-gpu at the same copies.
FROM nvidia/cuda:12.2.2-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ORT_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_TELEMETRY=1

# ffmpeg decodes the AudioSet FLAC and FMA MP3 background clips.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3.10 python3-pip git ca-certificates curl ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/bin/python3.10 /usr/local/bin/python

COPY requirements.lock.txt /tmp/requirements.lock.txt
RUN python -m pip install -r /tmp/requirements.lock.txt \
 && python -m pip install --no-deps openwakeword==0.6.0

ENV LD_LIBRARY_PATH=/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cufft/lib:/usr/local/lib/python3.10/dist-packages/nvidia/curand/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cuda_runtime/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cuda_nvrtc/lib

# openWakeWord's two shared feature models (release v0.5.1), with the SHA-256s
# the hub pins in services/satellites/app/wakeword.py: a model trained on these
# features is only valid with these exact files in front of it.
ARG OWW_RELEASE=https://github.com/dscripka/openWakeWord/releases/download/v0.5.1
RUN d=/usr/local/lib/python3.10/dist-packages/openwakeword/resources/models \
 && mkdir -p "$d" \
 && curl -fsSL -o "$d/melspectrogram.onnx" "$OWW_RELEASE/melspectrogram.onnx" \
 && curl -fsSL -o "$d/embedding_model.onnx" "$OWW_RELEASE/embedding_model.onnx" \
 && echo "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f  $d/melspectrogram.onnx" | sha256sum -c - \
 && echo "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f  $d/embedding_model.onnx" | sha256sum -c -

# The training script and example config come from openWakeWord main at a
# pinned commit (still versioned 0.6.0): it fixes the input shape for phrases
# longer than 2 s and puts the TFLite export behind a flag. The library it
# imports is the PyPI 0.6.0 release installed above, the one the hub runs.
# patches/ holds two small fixes, explained in the README.
ARG OWW_COMMIT=368c03716d1e92591906a84949bc477f3a834455
COPY patches /opt/wakeword-train/patches
RUN git clone -q https://github.com/dscripka/openWakeWord.git /opt/openWakeWord \
 && git -C /opt/openWakeWord checkout -q "$OWW_COMMIT" \
 && git -C /opt/openWakeWord apply /opt/wakeword-train/patches/openwakeword-train.patch

# piper-sample-generator v2.0.0 and its LibriTTS-R generator (904 speakers),
# the combination openWakeWord's notebook uses. Later releases changed the API
# that train.py imports. Its patch draws speaker pairs at random from the first
# PSG_MAX_SPEAKERS; evaluate.py's held-out voices come from the rest.
ARG PSG_COMMIT=195e3bd967d54589c2137c9de2b22ad526ba6b6f
ENV PSG_MAX_SPEAKERS=700
RUN git clone -q https://github.com/rhasspy/piper-sample-generator.git /opt/piper-sample-generator \
 && git -C /opt/piper-sample-generator checkout -q "$PSG_COMMIT" \
 && git -C /opt/piper-sample-generator apply /opt/wakeword-train/patches/piper-sample-generator.patch \
 && curl -fsSL -o /opt/piper-sample-generator/models/en_US-libritts_r-medium.pt \
      https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt \
 && echo "e95ee53770bf598c354a6e6dbfc95ccb259aeeb501d35a86be8a767429ab0ff6  /opt/piper-sample-generator/models/en_US-libritts_r-medium.pt" | sha256sum -c -

# The container runs as the owner of $WW_ROOT (docker run --user), who has no
# home here. numba (via librosa) and matplotlib (via acoustics) write caches.
ENV HOME=/tmp NUMBA_CACHE_DIR=/tmp/numba-cache MPLCONFIGDIR=/tmp/matplotlib

COPY *.py phrases.yaml /opt/wakeword-train/
WORKDIR /opt/wakeword-train
ENV WW_ROOT=/srv/wakeword-train
ENTRYPOINT ["python", "/opt/wakeword-train/train.py"]
CMD ["--help"]
