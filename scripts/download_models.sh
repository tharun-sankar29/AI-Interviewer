#!/usr/bin/env bash
set -euo pipefail

model_dir="models/piper"
mkdir -p "$model_dir"
base_url="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
curl -L --fail --output "$model_dir/en_US-lessac-medium.onnx" "$base_url/en_US-lessac-medium.onnx"
curl -L --fail --output "$model_dir/en_US-lessac-medium.onnx.json" "$base_url/en_US-lessac-medium.onnx.json"
curl -L --fail --output "$model_dir/en_US-lessac-medium.MODEL_CARD" "$base_url/MODEL_CARD"

# Pre-warm the local faster-whisper model cache.
project_python="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}"
project_python="${project_python:-$(pwd)/.venv/bin/python}"
if [[ ! -x "$project_python" ]]; then
  echo "Active project Python not found at $project_python" >&2
  exit 1
fi
"$project_python" -c "from faster_whisper import WhisperModel; WhisperModel('distil-small.en', device='cpu', compute_type='int8', cpu_threads=8, num_workers=1, download_root='models/faster-whisper')"
"$project_python" -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2', cache_folder='models/huggingface')"
