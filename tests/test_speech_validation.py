import io
import unittest
import wave
from unittest.mock import patch

from backend.speech_service import FasterWhisperService, PiperSpeechService, SpeechInputError


def wav_bytes(seconds=1.0, rate=16000):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
        wav.writeframes(b"\0\0" * int(seconds * rate))
    return output.getvalue()


class SpeechValidationTests(unittest.TestCase):
    def test_accepts_fixed_browser_format(self):
        self.assertEqual(FasterWhisperService.validate_wav(wav_bytes()), 1.0)

    def test_rejects_invalid_empty_short_wrong_rate_and_long(self):
        for content in (b"", b"not wav", wav_bytes(0.2), wav_bytes(1, 48000), wav_bytes(121)):
            with self.subTest(size=len(content)):
                with self.assertRaises(SpeechInputError): FasterWhisperService.validate_wav(content)

    def test_piper_load_error_is_not_masked_by_wav_cleanup(self):
        service = PiperSpeechService()
        with patch.object(service, "_load", side_effect=RuntimeError("voice missing")):
            with self.assertRaisesRegex(RuntimeError, "voice missing"):
                service.synthesize("Hello")


if __name__ == "__main__":
    unittest.main()
