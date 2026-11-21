import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.interview_session import INTRO_QUESTION, INTERVIEW_STATE, _prepare_main_questions, start_interview, submit_answer
from backend.llm_service import LLMServiceUnavailable


def seeded_state(log_file, questions=2):
    mains = [
        {
            "question": f"Question {i}?",
            "focus": f"Competency {i}",
            "basis": "jd_requirement",
            "is_followup": False,
            "is_intro": False,
        }
        for i in range(1, questions + 1)
    ]
    INTERVIEW_STATE.update({
        "context": "Backend role", "log": [], "next_questions": [mains[0]], "rag_index": None,
        "mode": "quick", "log_file": str(log_file), "main_questions": mains,
        "current_main_index": 0, "followups_for_current": 0,
        "job_description": "", "job_description_files": [],
        "adaptive": False, "execution_phase": "waiting_for_answer",
    })


RESULT = {"reaction": "Good explanation.", "score": 8, "evaluation": "Accurate and clear.", "next_question": "Can you add detail?"}


class AtomicTurnTests(unittest.TestCase):
    def setUp(self):
        self.old_state = INTERVIEW_STATE.copy()
        self.temp = tempfile.TemporaryDirectory()
        self.log_file = Path(self.temp.name) / "interview.json"

    def tearDown(self):
        self.temp.cleanup()
        INTERVIEW_STATE.clear()
        INTERVIEW_STATE.update(self.old_state)

    def test_introduction_precedes_technical_questions_without_using_a_slot(self):
        prepared = _prepare_main_questions(["What is a Python dictionary?", "Explain HTTP GET."])
        self.assertEqual(len(prepared), 3)
        self.assertEqual(prepared[0]["question"], INTRO_QUESTION)
        self.assertTrue(prepared[0]["is_intro"])
        self.assertFalse(prepared[1]["is_intro"])

    def test_existing_introduction_is_not_duplicated(self):
        prepared = _prepare_main_questions(["Please introduce yourself.", "What is Python?"])
        self.assertEqual(len(prepared), 2)
        self.assertTrue(prepared[0]["is_intro"])

    @patch("backend.interview_session.build_rag_index", return_value="resume-index")
    @patch("backend.interview_session.extract_skill_graph", side_effect=LLMServiceUnavailable("graph_unavailable"))
    @patch("backend.interview_session.load_job_description", return_value={
        "content": "Requires CAD and load analysis",
        "files": ["requirements.md"],
        "truncated": False,
    })
    @patch("backend.interview_session.generate_questions_from_resume", return_value=[{
        "question": "How would you analyze a static load?",
        "focus": "Static load analysis",
        "basis": "jd_resume_match",
    }])
    def test_start_stores_job_description_and_question_metadata(self, generate, _load_job, _extract_graph, _build_index):
        previous_directory = os.getcwd()
        try:
            os.chdir(self.temp.name)
            INTERVIEW_STATE["rag_index"] = None
            response = start_interview("Mechanical engineer", mode="quick")
        finally:
            os.chdir(previous_directory)
        self.assertEqual(response["next_question"], INTRO_QUESTION)
        self.assertEqual(response["welcome_message"], "Welcome! Let's begin.")
        self.assertEqual(INTERVIEW_STATE["job_description_files"], ["requirements.md"])
        self.assertEqual(INTERVIEW_STATE["main_questions"][1]["focus"], "Static load analysis")
        self.assertEqual(INTERVIEW_STATE["main_questions"][1]["basis"], "jd_resume_match")
        self.assertEqual(generate.call_args.kwargs["job_description"], "Requires CAD and load analysis")

    @patch("backend.interview_session.build_rag_index", return_value="resume-index")
    @patch("backend.interview_session.extract_skill_graph", return_value={"version": 1, "skills": {"python": {"skill_id": "python", "name": "Python", "mastery": 0, "confidence": 0}}, "edges": []})
    @patch("backend.interview_session.load_job_description", return_value={"content": "", "files": [], "truncated": False})
    def test_start_personalizes_welcome_from_resume(self, _load_job, _extract_graph, _build_index):
        previous_directory = os.getcwd()
        try:
            os.chdir(self.temp.name)
            response = start_interview("Backend engineer", mode="quick", resume_content="Name: Priya Sharma\npriya@example.com")
        finally:
            os.chdir(previous_directory)
        self.assertEqual(response["welcome_message"], "Welcome, Priya! Let's begin.")
        self.assertEqual(response["next_question"], INTRO_QUESTION)

    @patch("backend.interview_session.retrieve_evaluation_context", return_value="")
    @patch("backend.interview_session.complete_json", side_effect=LLMServiceUnavailable("timeout"))
    def test_failed_evaluation_does_not_mutate_turn(self, complete, _retrieve):
        seeded_state(self.log_file)
        before = copy.deepcopy(INTERVIEW_STATE)
        with self.assertRaises(LLMServiceUnavailable): submit_answer("A complete answer with enough technical detail to evaluate safely.")
        self.assertEqual(INTERVIEW_STATE, before)
        self.assertFalse(self.log_file.exists())

    @patch("backend.interview_session.retrieve_evaluation_context", return_value="")
    @patch("backend.interview_session.complete_json", return_value=RESULT)
    def test_one_evaluation_call_per_ordinary_answer(self, complete, _retrieve):
        seeded_state(self.log_file)
        response = submit_answer("A complete answer with enough technical detail to evaluate safely.")
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(response["score"], 8)
        self.assertEqual(len(INTERVIEW_STATE["log"]), 1)
        self.assertEqual(INTERVIEW_STATE["next_questions"][0]["question"], "Question 2?")

    @patch("backend.interview_session.retrieve_evaluation_context", return_value="")
    @patch("backend.interview_session.complete_json", return_value={
        **RESULT,
        "reaction": "The candidate’s answer is concise and technically sound.",
    })
    def test_reaction_is_addressed_directly_to_interviewee(self, _complete, _retrieve):
        seeded_state(self.log_file)
        response = submit_answer("A complete answer with enough technical detail to evaluate safely.")
        self.assertEqual(response["reaction"], "Your answer is concise and technically sound.")
        self.assertNotIn("candidate", response["reaction"].lower())

    @patch("backend.interview_session.retrieve_evaluation_context", return_value="")
    @patch("backend.interview_session.complete_json", return_value=RESULT)
    def test_response_logs_safe_interview_state(self, _complete, _retrieve):
        seeded_state(self.log_file)
        answer = "A private answer with enough technical detail to evaluate safely."
        with self.assertLogs("backend.interview_session", level="INFO") as captured:
            submit_answer(answer)
        state_log = next(line for line in captured.output if "interview_state" in line)
        self.assertIn('status: "ongoing"', state_log)
        self.assertIn("responses_recorded: 1", state_log)
        self.assertIn("last_score: 8", state_log)
        self.assertIn('reaction: "Good explanation."', state_log)
        self.assertIn("follow_up_required: false", state_log)
        self.assertIn("follow_up_question: null", state_log)
        self.assertGreaterEqual(state_log.count("\n"), 10)
        self.assertNotIn(answer, state_log)

    @patch("backend.interview_session.retrieve_evaluation_context", return_value="")
    @patch("backend.interview_session.complete_json", return_value={
        **RESULT,
        "score": 2,
        "reaction": "Please add more detail.",
        "next_question": "What happens when two keys have the same hash?",
    })
    def test_state_log_includes_generated_follow_up(self, _complete, _retrieve):
        seeded_state(self.log_file)
        with self.assertLogs("backend.interview_session", level="INFO") as captured:
            submit_answer("Too brief.")
        state_log = next(line for line in captured.output if "interview_state" in line)
        self.assertIn("follow_up_required: true", state_log)
        self.assertIn('follow_up_question: "What happens when two keys have the same hash?"', state_log)
        self.assertEqual(INTERVIEW_STATE["next_questions"][0]["focus"], "Competency 1")
        self.assertEqual(INTERVIEW_STATE["next_questions"][0]["basis"], "jd_requirement")

    @patch("backend.interview_session.retrieve_evaluation_context", return_value="")
    @patch("backend.interview_session.complete_json", return_value={
        **RESULT,
        "score": 2,
        "next_question": "Please expand on your introduction.",
    })
    def test_short_introduction_can_trigger_follow_up(self, _complete, _retrieve):
        mains = _prepare_main_questions(["What is Python?"])
        INTERVIEW_STATE.update({
            "context": "Junior role", "log": [], "next_questions": [mains[0]], "rag_index": None,
            "mode": "quick", "log_file": str(self.log_file), "main_questions": mains,
            "current_main_index": 0, "followups_for_current": 0,
            "adaptive": False, "execution_phase": "waiting_for_answer",
        })
        response = submit_answer("I build software.")
        self.assertEqual(response["next_question"], "Please expand on your introduction.")
        self.assertTrue(INTERVIEW_STATE["next_questions"][0]["is_followup"])

    @patch("backend.interview_session.retrieve_evaluation_context", return_value="")
    @patch("backend.interview_session.complete_text", side_effect=LLMServiceUnavailable("dual_provider_failure"))
    @patch("backend.interview_session.complete_json", return_value=RESULT)
    def test_failed_final_report_keeps_last_turn_retryable(self, _complete_json, _complete_text, _retrieve):
        seeded_state(self.log_file, questions=1)
        before = copy.deepcopy(INTERVIEW_STATE)
        with self.assertRaises(LLMServiceUnavailable): submit_answer("A complete answer with enough technical detail to evaluate safely.")
        self.assertEqual(INTERVIEW_STATE, before)

    @patch("backend.interview_session.retrieve_evaluation_context", return_value="")
    @patch("backend.interview_session.complete_text", return_value="Final report.")
    @patch("backend.interview_session.complete_json", return_value=RESULT)
    def test_compatibility_flow_preserves_final_answer_feedback(self, _complete_json, _complete_text, _retrieve):
        seeded_state(self.log_file, questions=1)
        response = submit_answer("A complete answer with enough technical detail to evaluate safely.")
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["score"], RESULT["score"])
        self.assertEqual(response["evaluation"], RESULT["evaluation"])
        self.assertEqual(response["reaction"], RESULT["reaction"])


if __name__ == "__main__":
    unittest.main()
