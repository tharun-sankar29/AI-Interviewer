import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.analysis_module import extract_skill_graph
from backend.interview_session import INTERVIEW_STATE, submit_answer
from backend.skill_graph import extract_behavior_signals, normalize_skill_graph, propagate_mastery, update_mastery


class SkillGraphTests(unittest.TestCase):
    def graph(self):
        return normalize_skill_graph({
            "skills": [
                {"skill_id": "fundamentals", "name": "Fundamentals", "category": "technical", "evidence": "resume"},
                {"skill_id": "cad", "name": "CAD", "category": "design", "evidence": "resume"},
                {"skill_id": "testing", "name": "Testing", "category": "quality", "evidence": "jd"},
            ],
            "edges": [
                {"source": "fundamentals", "target": "cad", "relation": "prerequisite", "weight": 0.8},
                {"source": "cad", "target": "testing", "relation": "related", "weight": 0.5},
            ],
        })

    def test_graph_normalizes_and_rejects_cycles(self):
        graph = self.graph()
        self.assertEqual(graph["skills"]["cad"]["mastery"], 0.5)
        with self.assertRaises(ValueError):
            normalize_skill_graph({
                "skills": [
                    {"skill_id": "a", "name": "A", "category": "x", "evidence": "x"},
                    {"skill_id": "b", "name": "B", "category": "x", "evidence": "x"},
                ],
                "edges": [
                    {"source": "a", "target": "b", "relation": "prerequisite", "weight": 1},
                    {"source": "b", "target": "a", "relation": "prerequisite", "weight": 1},
                ],
            })

    def test_behavior_signals_and_capped_mastery_penalty(self):
        signals = extract_behavior_signals("brief answer", latency_ms=30000, followup_count=3, input_mode="voice")
        self.assertEqual(signals["answer_words"], 2)
        self.assertEqual(signals["input_mode"], "voice")
        node = self.graph()["skills"]["cad"]
        result = update_mastery(node, 10, signals)
        self.assertEqual(result["behavior_penalty"], 0.15)
        self.assertLess(result["fused_score"], 1.0)

    def test_one_hop_weighted_propagation(self):
        graph = self.graph()
        updates = propagate_mastery(graph, "cad", 0.5)
        self.assertEqual(len(updates), 2)
        self.assertAlmostEqual(graph["skills"]["fundamentals"]["mastery"], 0.58)
        self.assertAlmostEqual(graph["skills"]["testing"]["mastery"], 0.525)


class SkillGraphIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        skill_graph = normalize_skill_graph({
            "skills": [
                {"skill_id": "cad", "name": "CAD", "category": "design", "evidence": "resume"},
                {"skill_id": "fundamentals", "name": "Fundamentals", "category": "technical", "evidence": "resume"},
            ],
            "edges": [{"source": "fundamentals", "target": "cad", "relation": "prerequisite", "weight": 0.5}],
        })
        question = {
            "question": "Explain CAD concepts.", "focus": "CAD", "basis": "jd_resume_match",
            "skill_id": "cad", "is_followup": False, "is_intro": False,
        }
        INTERVIEW_STATE.update({
            "context": "Mechanical role", "log": [], "next_questions": [question], "rag_index": None,
            "mode": "quick", "log_file": str(Path(self.temp.name) / "interview.json"),
            "main_questions": [question], "current_main_index": 0, "followups_for_current": 0,
            "job_description": "CAD", "job_description_files": ["jd.md"],
            "skill_graph": skill_graph, "graph_initialized": True, "graph_version": skill_graph["version"],
            "current_skill_id": "cad",
        })

    def tearDown(self):
        self.temp.cleanup()

    @patch("backend.interview_session.retrieve_evaluation_context", return_value="CAD context")
    @patch("backend.interview_session.complete_text", return_value="Final report")
    @patch("backend.interview_session.complete_json", return_value={
        "reaction": "Good explanation.", "score": 8, "evaluation": "Correct.", "next_question": "Add detail.",
    })
    def test_answer_updates_target_and_prerequisite(self, _complete_json, _complete_text, _retrieve):
        response = submit_answer(
            "CAD uses geometry and constraints to create engineering drawings.",
            latency_ms=1000,
            input_mode="voice",
        )
        self.assertEqual(response["skill_id"], "cad")
        self.assertEqual(response["signals"]["input_mode"], "voice")
        self.assertGreater(response["mastery_update"]["new_mastery"], 0.5)
        self.assertGreater(response["skill_graph"]["skills"]["fundamentals"]["mastery"], 0.5)
        self.assertEqual(INTERVIEW_STATE["log"][0]["skill_id"], "cad")


class SkillExtractionTests(unittest.TestCase):
    @patch("backend.analysis_module.retrieve_context", return_value="Mechanical engineering, CAD, and testing")
    @patch("backend.analysis_module.complete_gemini_json", return_value={
        "skills": [{"skill_id": "cad", "name": "CAD", "category": "design", "evidence": "Mechanical engineering CAD project"}],
        "edges": [],
    })
    def test_extraction_uses_strict_gemini_graph_schema(self, complete, _retrieve):
        graph = extract_skill_graph("index", "Requires CAD", "Mechanical role")
        self.assertIn("cad", graph["skills"])
        self.assertEqual(complete.call_args.kwargs, {})
        self.assertIn("JOB DESCRIPTION MATERIAL", complete.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
