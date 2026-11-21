import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.demo_readiness import readiness_report


class DemoReadinessTests(unittest.TestCase):
    def test_missing_live_credentials_are_reported_without_loading_models(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "GROQ_API_KEY": "",
            "GEMINI_API_KEY": "",
            "WHISPER_MODEL_DIR": str(Path(directory) / "missing-whisper"),
            "PIPER_MODEL_PATH": str(Path(directory) / "missing.onnx"),
            "PIPER_CONFIG_PATH": str(Path(directory) / "missing.json"),
        }, clear=False):
            report = readiness_report()
        self.assertEqual(report["status"], "not_ready")
        self.assertIn("Set GROQ_API_KEY in .env", report["missing"])
        self.assertIn("Set GEMINI_API_KEY in .env", report["missing"])
        self.assertTrue(report["live_models_required"])


if __name__ == "__main__":
    unittest.main()
