# Combo image for the light-models pod (embed-vllm + marker-service): extends
# the vLLM image (already has the correct CUDA/torch build) instead of
# starting from a plain Python base, so embed-vllm and marker-service can run
# as two processes in one container sharing a single GPU allocation.
# See k8s/vllm/light-models/deployment.yaml.
ARG VLLM_BASE_IMAGE=vllm/vllm-openai:v0.23.0
FROM ${VLLM_BASE_IMAGE}

WORKDIR /app

# marker-pdf (PDF→Markdown) system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    poppler-utils \
    curl \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# marker-service runs in its own venv, isolated from vLLM's site-packages.
# Confirmed empirically: installing marker-pdf directly into the base image's
# global python downgrades huggingface_hub/transformers/opencv-python-headless/
# openai/anthropic below the floors vllm and its lmcache dependency declare
# (`pip check` fails), which risks breaking the vLLM server at import time.
# A separate venv sidesteps the conflict entirely instead of fighting pins.
RUN python3 -m venv /opt/marker-venv
COPY marker-service/requirements.txt .
RUN /opt/marker-venv/bin/pip install --no-cache-dir -r requirements.txt \
    && /opt/marker-venv/bin/pip check

COPY marker-service/app/ ./app/
COPY marker-service/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000 8766

ENTRYPOINT ["/entrypoint.sh"]
