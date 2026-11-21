import io
import unittest
from unittest.mock import patch

import app as application
from backend.llm_service import LLMServiceUnavailable


class ApiTests(unittest.TestCase):
    def setUp(self):
        application.app.config.update(TESTING=True)
        self.client = application.app.test_client()

    @patch("app.start_interview", side_effect=LLMServiceUnavailable("dual_provider_failure"))
    def test_start_returns_consistent_503(self, _start):
        response = self.client.post("/api/start", json={})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["code"], "AI_SERVICE_UNAVAILABLE")

    @patch("app.submit_answer", side_effect=LLMServiceUnavailable("timeout"))
    def test_both_answer_interfaces_return_consistent_503(self, _answer):
        first = self.client.post("/api/answer", json={"answer": "test"})
        second = self.client.post("/api/interview", json={"content": "test"})
        self.assertEqual((first.status_code, second.status_code), (503, 503))
        self.assertEqual(first.json["code"], second.json["code"])

    @patch.object(application.tts_service, "synthesize", return_value=b"RIFFwav")
    def test_tts_returns_wav(self, _synthesize):
        response = self.client.post("/api/tts", json={"text": "Hello"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "audio/wav")

    def test_transcribe_rejects_non_wav(self):
        response = self.client.post("/api/transcribe", data={"audio": (io.BytesIO(b"bad"), "bad.wav")})
        self.assertEqual(response.status_code, 400)

    def test_role_library_and_session_configuration(self):
        roles = self.client.get("/api/roles")
        self.assertEqual(roles.status_code, 200)
        self.assertTrue(any(role["id"] == "software-engineer" for role in roles.json["roles"]))

        config = self.client.post("/api/session/config", json={
            "role_id": "software-engineer",
            "mode": "standard",
            "job_description": "Build reliable APIs for our customers.",
            "job_description_source": "pasted",
        })
        self.assertEqual(config.status_code, 200)
        self.assertEqual(config.json["role_name"], "Software Engineer")
        self.assertEqual(config.json["job_description_length"], 38)
        self.assertFalse(config.json["launched"])
        self.client.post("/api/session/reset")

    def test_custom_role_validation_and_session(self):
        self.client.post("/api/session/reset")
        for payload in (
            {"role_id": "custom"},
            {"role_id": "custom", "role_name": "   "},
            {"role_id": "custom", "role_name": "x" * 81},
            {"role_id": "custom", "role_name": 42},
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/api/session/config", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("custom role name", response.json["error"])

        response = self.client.post("/api/session/config", json={
            "role_id": "custom", "role_name": "  Cybersecurity Analyst  ",
            "context": "Ignore the custom role", "mode": "quick",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["role_name"], "Cybersecurity Analyst")
        self.assertEqual(response.json["category"], "Custom")
        self.assertEqual(response.json["context"], "Cybersecurity Analyst")
        self.assertEqual(response.json["mode"], "quick")
        self.assertEqual(self.client.post("/api/session/launch").json["session"]["role_name"], "Cybersecurity Analyst")
        self.assertFalse(any(role["id"] == "custom" for role in self.client.get("/api/roles").json["roles"]))
        reset = self.client.post("/api/session/reset")
        self.assertIsNone(reset.json["role_name"])

        preset = self.client.post("/api/session/config", json={"role_id": "software-engineer", "role_name": "Ignored"})
        self.assertEqual(preset.json["role_name"], "Software Engineer")
        self.client.post("/api/session/reset")

    @patch("app.readiness_report", return_value={"status": "ready", "checks": [], "missing": [], "live_models_required": True})
    def test_health_reports_ready(self, _readiness):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ready")

    @patch("app.readiness_report", return_value={"status": "not_ready", "checks": [], "missing": ["Set GROQ_API_KEY in .env"], "live_models_required": True})
    def test_health_reports_missing_demo_configuration(self, _readiness):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["missing"], ["Set GROQ_API_KEY in .env"])

    def test_job_description_upload_extracts_text(self):
        response = self.client.post(
            "/api/session/config/upload",
            data={"job_description": (io.BytesIO(b"Senior engineer\nBuild APIs."), "role.md")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["filename"], "role.md")
        self.assertIn("Build APIs", response.json["content"])

    def test_frontend_and_local_vad_assets_are_served(self):
        for path in (
            "/",
            "/script.js",
            "/audioRecorder.js",
            "/vendor/ort.wasm.min.js",
            "/vendor/ort-wasm-simd-threaded.mjs",
            "/vendor/ort-wasm-simd-threaded.wasm",
            "/vendor/ort-wasm-simd-threaded.jsep.mjs",
            "/vendor/ort-wasm-simd-threaded.jsep.wasm",
            "/vendor/vad.bundle.min.js",
            "/vendor/vad.worklet.bundle.min.js",
            "/vendor/silero_vad_v5.onnx",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                try:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
                finally:
                    response.close()

        module_response = self.client.get("/vendor/ort-wasm-simd-threaded.mjs")
        wasm_response = self.client.get("/vendor/ort-wasm-simd-threaded.wasm")
        try:
            self.assertEqual(module_response.mimetype, "text/javascript")
            self.assertEqual(wasm_response.mimetype, "application/wasm")
        finally:
            module_response.close()
            wasm_response.close()


if __name__ == "__main__":
    unittest.main()
