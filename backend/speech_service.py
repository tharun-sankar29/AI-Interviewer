"""Lazy, singleton CPU speech services."""

from __future__ import annotations

import io
import os
import tempfile
import threading
import wave


class SpeechInputError(ValueError):
    pass


class FasterWhisperService:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._model = None
        return cls._instance

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                os.getenv("WHISPER_MODEL", "distil-small.en"),
                device="cpu",
                compute_type="int8",
                cpu_threads=8,
                num_workers=1,
                download_root=os.getenv("WHISPER_MODEL_DIR", "models/faster-whisper"),
            )
        return self._model

    @staticmethod
    def validate_wav(content: bytes) -> float:
        if not content:
            raise SpeechInputError("The audio file is empty.")
        max_bytes = int(os.getenv("MAX_AUDIO_BYTES", str(20 * 1024 * 1024)))
        if len(content) > max_bytes:
            raise SpeechInputError("The audio file is too large.")
        try:
            with wave.open(io.BytesIO(content), "rb") as wav:
                if wav.getcomptype() != "NONE" or wav.getsampwidth() != 2:
                    raise SpeechInputError("Audio must be uncompressed 16-bit WAV.")
                if wav.getnchannels() != 1:
                    raise SpeechInputError("Audio must contain one channel.")
                if wav.getframerate() != 16000:
                    raise SpeechInputError("Audio must use a 16 kHz sample rate.")
                duration = wav.getnframes() / wav.getframerate()
        except (wave.Error, EOFError) as exc:
            raise SpeechInputError("A valid WAV file is required.") from exc
        if duration < 0.4:
            raise SpeechInputError("No usable speech was detected.")
        if duration > 120:
            raise SpeechInputError("Answers may be at most 120 seconds long.")
        return duration

    def transcribe(self, content: bytes) -> str:
        self.validate_wav(content)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as audio_file:
                audio_file.write(content)
                temp_path = audio_file.name
            segments, _ = self._load().transcribe(
                temp_path,
                language="en",
                beam_size=5,
                temperature=0.0,
                condition_on_previous_text=False,
                vad_filter=False,
            )
            transcript = " ".join(segment.text.strip() for segment in segments).strip()
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        if not transcript:
            raise SpeechInputError("No speech was detected.")
        return transcript


class PiperSpeechService:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._voice = None
        return cls._instance

    def _load(self):
        if self._voice is None:
            from piper import PiperVoice
            model = os.getenv("PIPER_MODEL_PATH", "models/piper/en_US-lessac-medium.onnx")
            config = os.getenv("PIPER_CONFIG_PATH", f"{model}.json")
            if not os.path.isfile(model) or not os.path.isfile(config):
                raise RuntimeError("Piper voice files are not installed. Run scripts/download_models.sh.")
            self._voice = PiperVoice.load(model, config_path=config)
        return self._voice

    def synthesize(self, text: str) -> bytes:
        # Load and validate the voice before opening the WAV writer. If loading
        # fails, an empty writer has no channel/rate metadata and its cleanup
        # raises a misleading ``wave.Error: # channels not specified``.
        voice = self._load()
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        return output.getvalue()


stt_service = FasterWhisperService()
tts_service = PiperSpeechService()
