#!/bin/bash
set -e

MODEL_REPO="${MODEL_REPO:-EryriLabs/dutybot-GGUF}"
MODEL_FILE="${MODEL_FILE:-domain_adapted-Q4_K_M.gguf}"
MODEL_DIR="/models"
MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"
N_GPU_LAYERS="${N_GPU_LAYERS:-999}"
CTX_SIZE="${CTX_SIZE:-4096}"
HOST="0.0.0.0"
PORT="8080"

mkdir -p "${MODEL_DIR}"

# Download model if not cached
if [ ! -f "${MODEL_PATH}" ]; then
    echo "==> Model not found at ${MODEL_PATH}, downloading..."
    DOWNLOAD_URL="https://huggingface.co/${MODEL_REPO}/resolve/main/${MODEL_FILE}"

    # Install huggingface_hub (handles HF's Xet storage properly)
    if ! command -v python3 &> /dev/null; then
        echo "==> Installing python3 + pip..."
        apt-get update -qq && apt-get install -y -qq python3 python3-pip > /dev/null 2>&1
    fi
    echo "==> Installing huggingface_hub..."
    python3 -m pip install --quiet huggingface_hub hf_xet 2>/dev/null || \
        python3 -m pip install --quiet --break-system-packages huggingface_hub hf_xet

    echo "==> Downloading ${MODEL_REPO}/${MODEL_FILE} via huggingface_hub..."
    python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    repo_id='${MODEL_REPO}',
    filename='${MODEL_FILE}',
    local_dir='${MODEL_DIR}',
    local_dir_use_symlinks=False,
)
print('Download complete.')
"
    echo "==> Download complete."
else
    echo "==> Model already cached at ${MODEL_PATH}"
fi

# Verify file exists
if [ ! -f "${MODEL_PATH}" ]; then
    echo "ERROR: Model file not found at ${MODEL_PATH} after download attempt."
    exit 1
fi

echo "==> Starting llama-server with ${N_GPU_LAYERS} GPU layers, ctx_size=${CTX_SIZE}"

# Find llama-server binary
LLAMA_BIN=""
for p in /app/llama-server /usr/local/bin/llama-server /usr/bin/llama-server llama-server; do
    if [ -x "$p" ] || command -v "$p" > /dev/null 2>&1; then
        LLAMA_BIN="$p"
        break
    fi
done
if [ -z "$LLAMA_BIN" ]; then
    echo "ERROR: llama-server binary not found"
    exit 1
fi
echo "==> Using binary: ${LLAMA_BIN}"

exec "${LLAMA_BIN}" \
    --model "${MODEL_PATH}" \
    --host "${HOST}" \
    --port "${PORT}" \
    --ctx-size "${CTX_SIZE}" \
    --n-gpu-layers "${N_GPU_LAYERS}" \
    --chat-template chatml
