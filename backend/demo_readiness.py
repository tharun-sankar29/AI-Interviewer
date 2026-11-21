"""Fast, non-invasive checks used before a local demo starts."""

from __future__ import annotations

import os
from pathlib import Path


def _check_file(path: str | Path, label: str) -> dict:
    resolved = Path(path)
    return {"name": label, "ok": resolved.is_file(), "detail": str(resolved)}


def _check_model_dir(path: str | Path, label: str) -> dict:
    resolved = Path(path)
    # Cache marker files are not model assets. Require at least one real file.
    files = [
        item for item in resolved.rglob("*")
        if item.is_file() and item.stat().st_size > 0
        and item.name not in {"CACHEDIR.TAG", ".gitignore"}
        and not item.name.endswith(".incomplete")
        and ".locks" not in item.parts
    ]
    return {"name": label, "ok": resolved.is_dir() and bool(files), "detail": str(resolved)}


def readiness_report() -> dict:
    """Return configuration and asset checks without loading heavyweight models."""
    checks = []
    groq_key = os.getenv("GROQ_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    checks.extend([
        {"name": "primary AI credentials", "ok": bool(groq_key), "detail": "GROQ_API_KEY is configured" if groq_key else "Set GROQ_API_KEY in .env"},
        {"name": "fallback AI credentials", "ok": bool(gemini_key), "detail": "GEMINI_API_KEY is configured" if gemini_key else "Set GEMINI_API_KEY in .env"},
    ])
    whisper_dir = Path(os.getenv("WHISPER_MODEL_DIR", "models/faster-whisper"))
    checks.append(_check_model_dir(whisper_dir, "speech recognition model"))
    embedding_dir = Path(os.getenv("EMBEDDING_CACHE_DIR", "models/huggingface"))
    checks.append(_check_model_dir(embedding_dir, "embedding model cache"))
    piper_model = Path(os.getenv("PIPER_MODEL_PATH", "models/piper/en_US-lessac-medium.onnx"))
    piper_config = Path(os.getenv("PIPER_CONFIG_PATH", f"{piper_model}.json"))
    checks.extend([
        _check_file(piper_model, "speech synthesis model"),
        _check_file(piper_config, "speech synthesis config"),
        _check_file("frontend/vendor/vad.bundle.min.js", "browser voice detector"),
        _check_file("frontend/vendor/vad.worklet.bundle.min.js", "browser voice worklet"),
        _check_file("frontend/vendor/ort.wasm.min.js", "browser inference runtime"),
        _check_file("frontend/vendor/ort-wasm-simd-threaded.wasm", "browser inference wasm"),
        _check_file("frontend/vendor/silero_vad_v5.onnx", "browser voice model"),
        _check_file(os.getenv("RESUME_PATH", "interview_logs/sample_resume.md"), "demo resume"),
    ])
    missing = [check["detail"] for check in checks if not check["ok"]]
    return {"status": "ready" if not missing else "not_ready", "checks": checks, "missing": missing, "live_models_required": True}
