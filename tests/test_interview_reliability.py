"""Failures must leave retryable turns, and completion must preserve feedback."""

import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from backend.interview_session import INTERVIEW_STATE, start_interview, stop_interview, submit_answer
from backend.llm_service import LLMServiceUnavailable
from backend.skill_graph import normalize_skill_graph


ANSWER = "I validate incoming requests and use transactions with rollback for failed writes."
RESULT = {"score": 8, "reaction": "You explained the rollback clearly.",
          "evaluation": "Accurate transaction handling.", "next_question": "How would you handle concurrent writes?"}


class InterviewReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.old_state = INTERVIEW_STATE.copy()
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.directory.name)
        self.serial = 0
        self.prompts = []
        graph = normalize_skill_graph({"skills": [
            {"skill_id": name, "name": name.title(), "category": "technical", "evidence": "Built production APIs and SQL"}
            for name in ("api", "sql")], "edges": []})
        for name, options in {
            "build_rag_index": {"return_value": "index"},
            "extract_skill_graph": {"side_effect": lambda *args, **kwargs: copy.deepcopy(graph)},
            "retrieve_evaluation_context": {"return_value": "Relevant project"},
            "complete_json": {"side_effect": self.model},
            "complete_text": {"return_value": "Final report."},
        }.items():
            patcher = patch("backend.interview_session." + name, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        os.chdir(self.cwd)
        self.directory.cleanup()
        INTERVIEW_STATE.clear()
        INTERVIEW_STATE.update(self.old_state)

    def model(self, prompt, schema, options):
        self.prompts.append(prompt)
        self.serial += 1
        if set(schema["properties"]) == {"question"}:
            return {"question": f"Explain transaction scenario {self.serial}?"}
        return {**RESULT, "next_question": f"Explain the missing detail from turn {self.serial}?"}

    def start(self, mode="quick"):
        start_interview("Backend engineer", mode=mode, job_description_override="Python APIs and SQL")
        submit_answer("I build production backend services and maintain their databases.")

    def test_counters_and_evaluator_followups_work_in_every_mode(self):
        for mode in ("quick", "standard", "thorough", "adaptive"):
            with self.subTest(mode=mode):
                self.start(mode)
                with patch("backend.interview_session._evaluate", return_value={**RESULT, "score": 2}):
                    first = submit_answer(ANSWER)
                self.assertTrue(first["current"]["is_followup"])
                self.assertEqual(first["next_question"], RESULT["next_question"])
                submit_answer(ANSWER)
                submit_answer(ANSWER)
                self.assertEqual(INTERVIEW_STATE["followups_used"], 1)
                self.assertEqual(INTERVIEW_STATE["questions_asked"], 3)
                self.assertEqual(INTERVIEW_STATE["main_questions_used"], 2)

    def test_final_report_failure_restores_state_and_retry_commits_once(self):
        self.start()
        for _ in range(3):
            submit_answer(ANSWER)
        before = copy.deepcopy(INTERVIEW_STATE)
        with patch("backend.interview_session.complete_text", side_effect=LLMServiceUnavailable("timeout")):
            with self.assertRaises(LLMServiceUnavailable):
                submit_answer(ANSWER)
        self.assertEqual(INTERVIEW_STATE, before)
        final = submit_answer(ANSWER)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(len(INTERVIEW_STATE["log"]), 4)
        self.assertEqual(final["score"], 8)
        self.assertEqual(final["analysis_score"], 8)
        self.assertEqual(final["evaluation"], RESULT["evaluation"])
        self.assertEqual(final["reaction"], RESULT["reaction"])
        self.assertIn("complete", final["message"])

    def test_failed_question_generation_restores_state(self):
        self.start()
        before = copy.deepcopy(INTERVIEW_STATE)
        with patch("backend.interview_session._generate_adaptive_question", side_effect=LLMServiceUnavailable("timeout")):
            with self.assertRaises(LLMServiceUnavailable):
                submit_answer(ANSWER)
        self.assertEqual(INTERVIEW_STATE, before)

    def test_question_generation_uses_updated_mastery_and_actual_prior_question(self):
        self.start("adaptive")
        previous_question = INTERVIEW_STATE["current_question"]["question"]
        response = submit_answer(ANSWER)
        target = response["current"]["skill_id"]
        node = INTERVIEW_STATE["skill_graph"]["skills"][target]
        self.assertIn(f"Mastery: {node['mastery']}", self.prompts[-1])
        self.assertIn(previous_question, self.prompts[-1])
        self.assertNotIn("fingerprints", self.prompts[-1])

    def test_repeated_followup_regenerates_with_answer_and_selected_skill(self):
        self.start("standard")
        original = INTERVIEW_STATE["current_question"]["question"]
        with patch("backend.interview_session._evaluate", return_value={**RESULT, "score": 2, "next_question": original}):
            response = submit_answer(ANSWER)
        self.assertTrue(response["current"]["is_followup"])
        self.assertNotEqual(response["next_question"], original)
        self.assertIn(ANSWER, self.prompts[-1])
        self.assertIn(RESULT["evaluation"], self.prompts[-1])
        self.assertIn("Selected skill: Api", self.prompts[-1])

    def test_manual_stop_does_not_invent_an_answer_score(self):
        self.start()
        stopped = stop_interview()
        self.assertIsNone(stopped["score"])
        self.assertIsNone(stopped["analysis_score"])
        self.assertEqual(stopped["status"], "completed")
