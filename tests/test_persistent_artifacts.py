"""Every started session gets a durable JSON record and a distinct final analysis."""

import copy
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as application
from backend.interview_session import (
    INTERVIEW_STATE, SESSION_CONFIG, clear_session_config, configure_session,
    start_interview, stop_interview, submit_answer,
)
from backend.llm_service import LLMServiceUnavailable
from backend.skill_graph import normalize_skill_graph


ANSWER = "I validate requests, use transactions, test rollback, and monitor production errors."


class PersistentArtifactTests(unittest.TestCase):
    def setUp(self):
        self.old_state = copy.deepcopy(INTERVIEW_STATE)
        self.old_config = copy.deepcopy(SESSION_CONFIG)
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.directory.name)
        clear_session_config()
        self.serial = 0
        candidate = normalize_skill_graph({"skills": [
            {"skill_id": name, "name": name.title(), "category": "technical", "evidence": "Production project"}
            for name in ("api", "sql")], "edges": []})
        for name, options in {
            "build_rag_index": {"return_value": "index"},
            "extract_skill_graph": {"side_effect": lambda *args, **kwargs: copy.deepcopy(candidate)},
            "retrieve_evaluation_context": {"return_value": "Production project"},
            "complete_json": {"side_effect": self.model},
            "complete_text": {"return_value": "Evidence-based final report."},
        }.items():
            patcher = patch("backend.interview_session." + name, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        os.chdir(self.cwd)
        self.directory.cleanup()
        INTERVIEW_STATE.clear()
        INTERVIEW_STATE.update(self.old_state)
        SESSION_CONFIG.clear()
        SESSION_CONFIG.update(self.old_config)

    def model(self, _prompt, schema, _options):
        if set(schema["properties"]) == {"question"}:
            self.serial += 1
            return {"question": f"How would you implement scenario {self.serial}?"}
        return {"reaction": "You explained the approach.", "score": 8,
                "evaluation": "Accurate and specific.", "next_question": "How would you handle a failed write?"}

    def start(self, mode="quick"):
        start_interview("Backend engineer", mode=mode, job_description_override="APIs and SQL")
        return Path(INTERVIEW_STATE["log_file"])

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def video_request(self, path, number=1, *, content=b"\x1a\x45\xdf\xa3recorded-answer"):
        return application.app.test_client().post("/api/session/recordings", data={
            "interview_id": path.parent.name, "session_id": SESSION_CONFIG["session_id"],
            "question_number": str(number), "video": (io.BytesIO(content), "answer.webm", "video/webm"),
        })

    def wait_for_video(self, path, number=1, expected="saved"):
        client = application.app.test_client()
        for _ in range(100):
            response = client.get("/api/session/recordings/status", query_string={
                "interview_id": path.parent.name, "session_id": SESSION_CONFIG["session_id"],
                "question_number": number,
            })
            if response.json["status"] == expected:
                return
            time.sleep(.01)
        self.fail(f"Video status did not become {expected}: {response.json}")

    def test_start_intro_and_each_answer_are_saved(self):
        path = self.start()
        started = self.read(path)
        self.assertEqual(started["status"], "in_progress")
        self.assertEqual(started["entries"], [])
        self.assertTrue(started["current_question"]["is_intro"])
        self.assertEqual((started["role_name"], started["mode"], started["schema_version"]),
                         ("Backend engineer", "quick", 1))
        submit_answer("I build reliable backend systems and maintain APIs.")
        introduced = self.read(path)
        self.assertEqual(len(introduced["entries"]), 1)
        self.assertTrue(introduced["entries"][0]["is_intro"])
        self.assertIsNone(introduced["entries"][0]["score"])
        submit_answer(ANSWER)
        active = self.read(path)
        self.assertEqual(len(active["entries"]), 2)
        self.assertEqual(active["entries"][1]["answer"], ANSWER)
        self.assertEqual(active["skill_graph"]["skills"][active["entries"][1]["skill_id"]]["evidence_count"], 1)
        self.assertIsNone(active["analysis_file"])
        self.assertFalse((path.parent / "analysis.md").exists())

    def test_quick_completion_writes_matching_report_and_preserves_next_session(self):
        first = self.start()
        client = application.app.test_client()
        self.assertEqual(client.get("/api/session/report").status_code, 404)
        self.assertFalse(client.get("/api/log").json["report_available"])
        submit_answer("I build reliable backend systems and maintain APIs.")
        for _ in range(4):
            result = submit_answer(ANSWER)
        self.assertEqual(result["status"], "completed")
        saved = self.read(first)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["stopping_reason"], "mode_safety_limit")
        self.assertEqual(len(saved["entries"]), 5)
        self.assertEqual(sum(not entry["is_intro"] and not entry["is_followup"] for entry in saved["entries"]), 4)
        report = Path(saved["analysis_file"])
        self.assertEqual(report, first.parent / "analysis.md")
        content = report.read_text(encoding="utf-8")
        for section in ("# Final Interview Analysis", "## Summary", "## Skill-wise mastery",
                        "## Recommendations", "## Narrative assessment", "## Question-by-question analysis"):
            self.assertIn(section, content)
        self.assertIn("- Overall score: 8.0/10", content)
        self.assertIn("**Score:** 8/10", content)
        self.assertIn("**Evaluation:** Accurate and specific.", content)
        self.assertIn("Evidence-based final report.", content)
        served = client.get("/api/session/report")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.get_data(as_text=True), content)
        self.assertEqual(served.mimetype, "text/markdown")
        self.assertNotIn("attachment", served.headers["Content-Disposition"])
        self.assertEqual(served.headers["Cache-Control"], "no-store")
        served.close()
        downloaded = client.get("/api/session/report?download=1")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.mimetype, "text/markdown")
        self.assertEqual(downloaded.get_data(as_text=True), content)
        self.assertIn("attachment", downloaded.headers["Content-Disposition"])
        self.assertIn(report.name, downloaded.headers["Content-Disposition"])
        downloaded.close()
        self.assertTrue(client.get("/api/log").json["report_available"])
        self.assertEqual(self.video_request(first, 5).status_code, 202)
        self.wait_for_video(first, 5)
        saved = self.read(first)
        self.assertEqual(saved["entries"][4]["video_file"], "recordings/question_005.webm")
        self.assertEqual(report.read_text(encoding="utf-8"), content)
        self.assertFalse(Path("interview_logs/log.txt").exists())
        self.assertFalse(Path("interview_logs/analysis.txt").exists())
        client.post("/api/session/reset")
        self.assertEqual(client.get("/api/session/report?download=1").status_code, 404)
        self.assertFalse(client.get("/api/log").json["report_available"])
        self.assertTrue(report.exists())
        second = self.start()
        self.assertEqual(client.get("/api/session/report").status_code, 404)
        self.assertFalse(client.get("/api/log").json["report_available"])
        self.assertNotEqual(first, second)
        self.assertEqual(self.read(first), saved)
        self.assertTrue(report.exists())
        self.assertEqual(self.read(second)["status"], "in_progress")

    def test_manual_stop_without_scored_answers_saves_analysis(self):
        path = self.start("adaptive")
        stopped = stop_interview()
        self.assertEqual(stopped["status"], "completed")
        saved = self.read(path)
        self.assertEqual(saved["stopping_reason"], "manual_stop")
        self.assertEqual(saved["entries"], [])
        content = Path(saved["analysis_file"]).read_text(encoding="utf-8")
        self.assertIn("- Overall score: 0.0/10", content)
        self.assertIn("No scored questions were answered.", content)
        self.assertIn("Evidence-based final report.", content)
        self.assertTrue(application.app.test_client().get("/api/log").json["report_available"])
        served = application.app.test_client().get("/api/session/report")
        self.assertEqual(served.status_code, 200)
        served.close()

    def test_automatic_completion_without_scored_answers_saves_markdown(self):
        path = self.start("adaptive")
        with patch("backend.interview_session._adaptive_next_spec", return_value=None):
            completed = submit_answer("I build reliable backend systems and maintain APIs.")
        self.assertEqual(completed["status"], "completed")
        saved = self.read(path)
        self.assertEqual(saved["stopping_reason"], "all_skills_mastered")
        self.assertEqual(len(saved["entries"]), 1)
        self.assertTrue(saved["entries"][0]["is_intro"])
        self.assertEqual(saved["analysis_file"], str(path.parent / "analysis.md"))
        self.assertEqual(self.video_request(path).status_code, 202)
        self.wait_for_video(path)
        self.assertEqual(self.read(path)["entries"][0]["video_file"], "recordings/question_001.webm")
        content = Path(saved["analysis_file"]).read_text(encoding="utf-8")
        self.assertIn("- Overall score: 0.0/10", content)
        self.assertIn("No scored questions were answered.", content)
        self.assertEqual(application.app.test_client().get("/api/session/report").get_data(as_text=True), content)

    def test_reset_and_reconfigure_mark_active_records_interrupted(self):
        first = self.start()
        application.app.test_client().post("/api/session/reset")
        self.assertEqual((self.read(first)["status"], self.read(first)["stopping_reason"]), ("interrupted", "reset"))
        self.assertIsNone(self.read(first)["analysis_file"])
        self.assertIsNone(INTERVIEW_STATE["session_record"])
        second = self.start()
        configure_session(role_id="backend-engineer", mode="standard")
        self.assertEqual((self.read(second)["status"], self.read(second)["stopping_reason"]), ("interrupted", "reconfigured"))
        self.assertFalse((second.parent / "analysis.md").exists())

    def test_replacement_start_interrupts_previous_record(self):
        first = self.start()
        second = self.start()
        self.assertNotEqual(first, second)
        self.assertEqual(self.read(first)["stopping_reason"], "replaced")
        self.assertEqual(self.read(second)["status"], "in_progress")

    def test_provider_and_disk_failures_leave_last_answer_retryable(self):
        path = self.start()
        submit_answer("I build reliable backend systems and maintain APIs.")
        for _ in range(3):
            submit_answer(ANSWER)
        before = copy.deepcopy(INTERVIEW_STATE)
        original = path.read_bytes()
        report = path.parent / "analysis.md"
        with patch("backend.interview_session.complete_text", side_effect=LLMServiceUnavailable("timeout")):
            with self.assertRaises(LLMServiceUnavailable):
                submit_answer(ANSWER)
        self.assertEqual(INTERVIEW_STATE, before)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(report.exists())
        with patch("backend.interview_session._save_record", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                submit_answer(ANSWER)
        self.assertEqual(INTERVIEW_STATE, before)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(report.exists())
        with patch("backend.interview_session._atomic_write", side_effect=OSError("disk full")):
            response = application.app.test_client().post("/api/answer", json={"answer": ANSWER})
        self.assertEqual((response.status_code, response.json["code"]), (503, "PERSISTENCE_UNAVAILABLE"))
        self.assertEqual(INTERVIEW_STATE, before)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(submit_answer(ANSWER)["status"], "completed")
        self.assertTrue(report.exists())

    def test_intro_write_failure_keeps_introduction_pending(self):
        path = self.start()
        original = path.read_bytes()
        before = copy.deepcopy(INTERVIEW_STATE)
        with patch("backend.interview_session._save_record", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                submit_answer("I build reliable backend systems and maintain APIs.")
        self.assertEqual(INTERVIEW_STATE, before)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.read(path)["entries"], [])

    def test_recordings_follow_accepted_answers_and_survive_later_turns_and_reset(self):
        path = self.start()
        self.assertEqual(self.video_request(path).status_code, 400)
        submit_answer("I build reliable backend systems and maintain APIs.")
        queued = self.video_request(path)
        self.assertIn(queued.status_code, (200, 202))
        self.wait_for_video(path)
        clip = path.parent / "recordings/question_001.webm"
        self.assertEqual(clip.read_bytes(), b"\x1a\x45\xdf\xa3recorded-answer")
        self.assertEqual(self.read(path)["entries"][0]["video_file"], "recordings/question_001.webm")
        self.assertEqual(self.video_request(path).json["status"], "saved")
        submit_answer(ANSWER)
        self.assertEqual(self.read(path)["entries"][0]["video_file"], "recordings/question_001.webm")
        self.assertEqual(self.video_request(path, 2).status_code, 202)
        self.wait_for_video(path, 2)
        client = application.app.test_client()
        client.post("/api/session/reset")
        self.assertTrue(clip.is_file())
        self.assertEqual(self.read(path)["entries"][1]["video_file"], "recordings/question_002.webm")
        self.assertFalse(client.get("/api/log").json["report_available"])

    def test_failed_video_metadata_save_can_retry_without_affecting_answer(self):
        path = self.start()
        submit_answer("I build reliable backend systems and maintain APIs.")
        with patch("backend.interview_session._save_record", side_effect=OSError("disk full")):
            self.assertEqual(self.video_request(path).status_code, 202)
            self.wait_for_video(path, expected="failed")
        self.assertNotIn("video_file", self.read(path)["entries"][0])
        self.assertFalse((path.parent / "recordings/question_001.webm").exists())
        self.assertEqual(self.video_request(path).status_code, 202)
        self.wait_for_video(path)
        self.assertEqual(self.read(path)["entries"][0]["video_file"], "recordings/question_001.webm")


if __name__ == "__main__":
    unittest.main()
