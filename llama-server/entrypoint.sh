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

    if command -v huggingface-cli &> /dev/null; then
        echo "==> Using huggingface-cli"
        huggingface-cli download "${MODEL_REPO}" "${MODEL_FILE}" \
            --local-dir "${MODEL_DIR}" \
            --local-dir-use-symlinks False
    elif command -v curl &> /dev/null; then
        echo "==> Using curl to download from ${DOWNLOAD_URL}"
        curl -L -o "${MODEL_PATH}" "${DOWNLOAD_URL}"
    elif command -v wget &> /dev/null; then
        echo "==> Using wget to download from ${DOWNLOAD_URL}"
        wget -O "${MODEL_PATH}" "${DOWNLOAD_URL}"
    else
        # Last resort: install curl via apt
        echo "==> No download tool found, installing curl..."
        apt-get update -qq && apt-get install -y -qq curl > /dev/null 2>&1
        curl -L -o "${MODEL_PATH}" "${DOWNLOAD_URL}"
    fi
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
