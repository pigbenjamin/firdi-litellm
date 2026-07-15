#!/bin/bash
# Runs embed-vllm and marker-service as two processes in one container so
# they share the single nvidia.com/gpu:1 allocation for this pod (this
# cluster's device plugin hands out whole GPUs per container that requests
# one — two separate pods each requesting nvidia.com/gpu:1 would land on two
# different physical GPUs, not share one). If either process exits, the
# container exits and Kubernetes (strategy: Recreate) restarts the pod.

EMBED_MODEL="${EMBED_MODEL:-google/embeddinggemma-300m}"
EMBED_GPU_MEM_UTIL="${EMBED_GPU_MEM_UTIL:-0.15}"
EMBED_MAX_NUM_SEQS="${EMBED_MAX_NUM_SEQS:-512}"
EMBED_PORT="${EMBED_PORT:-8000}"
MARKER_PORT="${MARKER_PORT:-8766}"

# Observed in practice: right after container start, pod networking (DNS/route)
# can take a moment to settle. vLLM fetches the (gated) HF config immediately
# on launch with no retry, so it fatally crashes if that first request loses
# the race — and the crash doesn't reliably bring the container down either
# (see below), so it can sit "Running" but broken for the full startup probe
# budget. Wait for HF connectivity before starting vLLM at all.
echo "[entrypoint] waiting for network before starting embed-vllm..."
for i in $(seq 1 30); do
    if curl -sf -m 3 https://huggingface.co >/dev/null 2>&1; then
        echo "[entrypoint] network ready after ${i}s."
        break
    fi
    sleep 1
done

echo "[entrypoint] starting embed-vllm (${EMBED_MODEL}) on port ${EMBED_PORT}..."
python3 -m vllm.entrypoints.openai.api_server \
    --model="${EMBED_MODEL}" \
    --runner=pooling \
    --convert=embed \
    --gpu-memory-utilization="${EMBED_GPU_MEM_UTIL}" \
    --max-num-seqs="${EMBED_MAX_NUM_SEQS}" \
    --served-model-name="${EMBED_MODEL}" \
    --host=0.0.0.0 \
    --port="${EMBED_PORT}" \
    --disable-custom-all-reduce &
EMBED_PID=$!

echo "[entrypoint] waiting for embed-vllm on port ${EMBED_PORT}..."
for i in $(seq 1 120); do
    if curl -sf "http://localhost:${EMBED_PORT}/health" >/dev/null 2>&1; then
        echo "[entrypoint] embed-vllm ready after $((i * 5))s."
        break
    fi
    sleep 5
done

echo "[entrypoint] starting marker-service on port ${MARKER_PORT}..."
cd /app && /opt/marker-venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${MARKER_PORT}" &
MARKER_PID=$!

wait -n "$EMBED_PID" "$MARKER_PID"
exit_code=$?
echo "[entrypoint] one of embed-vllm/marker-service exited (code ${exit_code}), stopping container"
exit "$exit_code"
