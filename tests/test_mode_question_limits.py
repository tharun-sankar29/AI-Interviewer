"""Bounded interviews reserve all main questions and score optional follow-ups."""

import copy
import os
import tempfile
import unittest
from unittest.mock import patch

import app as application
from backend.interview_session import INTERVIEW_MODES, INTERVIEW_STATE, SESSION_CONFIG, configure_session, start_interview, submit_answer
from backend.skill_graph import normalize_skill_graph


ANSWER = "I validate requests, use database transactions, test rollback, and monitor production errors."


def graph(count, *, mastered=False):
    result = normalize_skill_graph({"skills": [
        {"skill_id": f"skill_{index}", "name": f"Skill {index}", "category": "technical", "evidence": "Production project"}
        for index in range(count)], "edges": []})
    if mastered:
        for node in result["skills"].values():
            node.update(mastery=.9, confidence=.9, evidence_count=4)
    return result


class ModeQuestionLimitTests(unittest.TestCase):
    def setUp(self):
        self.old_state = copy.deepcopy(INTERVIEW_STATE)
        self.old_config = copy.deepcopy(SESSION_CONFIG)
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.directory.name)
        self.serial = 0
        self.current_score = 8
        self.candidate = graph(12)
        for name, options in {
            "build_rag_index": {"return_value": "index"},
            "extract_skill_graph": {"side_effect": lambda *args, **kwargs: copy.deepcopy(self.candidate)},
            "retrieve_evaluation_context": {"return_value": "Production project"},
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
        SESSION_CONFIG.clear()
        SESSION_CONFIG.update(self.old_config)

    def model(self, prompt, schema, _options):
        if set(schema["properties"]) == {"question"}:
            self.serial += 1
            if prompt.startswith("Ask exactly one technical follow-up"):
                return {"question": f"How would you verify transaction rollback for scenario {self.serial}?"}
            return {"question": f"How would you implement scenario {self.serial}?"}
        skill = INTERVIEW_STATE["current_question"]["skill_id"]
        return {"reaction": "You explained your approach.", "score": self.current_score,
                "evaluation": "The answer did not explain transaction rollback.",
                "next_question": f"For {skill}, how would you roll back a failed transaction in this case?"}

    def run_mode(self, mode, score_for, *, answer_for=None):
        start_interview("Backend engineer", mode=mode, job_description_override="APIs and data")
        intro = submit_answer("I build production backend systems and improve their reliability.")
        self.assertEqual(intro["status"], "ongoing")
        limit = INTERVIEW_MODES[mode]["adaptive_turn_budget"]
        for _ in range(limit + 1):
            if INTERVIEW_STATE["execution_phase"] == "completed":
                break
            current = INTERVIEW_STATE["current_question"]
            main_number = INTERVIEW_STATE["main_questions_used"] + (0 if current["is_followup"] else 1)
            self.current_score = score_for(current, main_number)
            response = submit_answer(answer_for(current, main_number) if answer_for else ANSWER)
            self.assertEqual(response["score"], self.current_score)
        else:
            self.fail("Interview did not complete within its total answer limit")
        return INTERVIEW_STATE

    def test_good_answers_complete_every_main_target_even_on_small_or_mastered_graphs(self):
        for mode, target in (("quick", 4), ("standard", 8), ("thorough", 12)):
            for candidate in (graph(1), graph(2, mastered=True)):
                with self.subTest(mode=mode, skills=len(candidate["skills"]), mastered=next(iter(candidate["skills"].values()))["mastery"]):
                    self.candidate = candidate
                    state = self.run_mode(mode, lambda _current, _number: 9)
                    self.assertEqual((state["main_questions_used"], state["followups_used"], state["questions_asked"]), (target, 0, target))
                    self.assertEqual(state["stopping_reason"], "mode_safety_limit")

    def test_fully_adaptive_still_stops_when_graph_is_mastered(self):
        self.candidate = graph(2, mastered=True)
        start_interview("Backend engineer", mode="adaptive", job_description_override="APIs")
        result = submit_answer("I build production backend systems and improve their reliability.")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(INTERVIEW_STATE["stopping_reason"], "all_skills_mastered")
        self.assertEqual(INTERVIEW_STATE["questions_asked"], 0)

    def test_weak_first_and_final_main_answers_get_same_skill_followups(self):
        for mode, target in (("quick", 4), ("standard", 8), ("thorough", 12)):
            with self.subTest(mode=mode):
                self.candidate = graph(target)
                state = self.run_mode(mode, lambda current, number: 2 if not current["is_followup"] and number in (1, target) else 8)
                entries = state["log"]
                self.assertEqual((state["main_questions_used"], state["followups_used"]), (target, 2))
                for index, entry in enumerate(entries):
                    if entry["is_followup"]:
                        self.assertEqual(entry["skill_id"], entries[index - 1]["skill_id"])
                        self.assertIn(f"For {entry['skill_id']}, how would you roll back", entry["question"])
                self.assertTrue(entries[-1]["is_followup"])
                for skill_id in (entries[0]["skill_id"], entries[-1]["skill_id"]):
                    self.assertEqual(state["skill_graph"]["skills"][skill_id]["evidence_count"], 2)

    def test_short_high_scoring_answer_triggers_followup_in_each_mode(self):
        for mode in ("quick", "standard", "thorough"):
            with self.subTest(mode=mode):
                self.candidate = graph(12)
                start_interview("Backend engineer", mode=mode, job_description_override="APIs")
                submit_answer("I build production backend systems and improve their reliability.")
                self.current_score = 8
                response = submit_answer("Brief answer.")
                self.assertTrue(response["current"]["is_followup"])
                self.assertEqual(response["current"]["skill_id"], INTERVIEW_STATE["log"][0]["skill_id"])

    def test_mode_specific_weak_score_thresholds(self):
        for mode, expected in (("quick", True), ("standard", False), ("thorough", False)):
            with self.subTest(mode=mode):
                self.candidate = graph(12)
                start_interview("Backend engineer", mode=mode, job_description_override="APIs")
                submit_answer("I build production backend systems and improve their reliability.")
                self.current_score = 5
                response = submit_answer(ANSWER)
                self.assertEqual(response["current"]["is_followup"], expected)

    def test_thorough_can_finish_two_followups_after_final_main_question(self):
        self.candidate = graph(12)
        state = self.run_mode("thorough", lambda current, number: 2 if number == 12 else 8)
        self.assertEqual((state["main_questions_used"], state["followups_used"]), (12, 2))
        self.assertEqual([entry["is_followup"] for entry in state["log"][-3:]], [False, True, True])
        self.assertEqual(len({entry["skill_id"] for entry in state["log"][-3:]}), 1)
        self.assertIn("transaction rollback", state["log"][-1]["question"])

    def test_all_weak_answers_obey_total_and_per_skill_caps(self):
        for mode, main, followups in (("quick", 4, 2), ("standard", 8, 4), ("thorough", 12, 6)):
            with self.subTest(mode=mode):
                self.candidate = graph(12)
                state = self.run_mode(mode, lambda _current, _number: 2)
                self.assertEqual((state["main_questions_used"], state["followups_used"], state["questions_asked"]),
                                 (main, followups, main + followups))
                self.assertTrue(all(node["followup_count"] <= INTERVIEW_MODES[mode]["max_followups_per_skill"]
                                    for node in state["skill_graph"]["skills"].values()))
                self.candidate = graph(1)
                state = self.run_mode(mode, lambda _current, _number: 2)
                self.assertEqual(state["main_questions_used"], main)
                self.assertEqual(state["followups_used"], INTERVIEW_MODES[mode]["max_followups_per_skill"])

    def test_standard_api_workflow_and_mode_metadata(self):
        self.candidate = graph(8)
        client = application.app.test_client()
        configured = configure_session(role_id="backend-engineer", mode="standard", job_description="APIs and data")
        self.assertEqual(configured["mode"], "standard")
        start = client.post("/api/start", json={"context": "Backend engineer", "mode": "standard"})
        self.assertEqual(start.status_code, 200)
        self.assertTrue(start.json["current"]["is_intro"])
        self.assertEqual(client.post("/api/answer", json={"answer": ANSWER}).status_code, 200)
        for _ in range(12):
            current = INTERVIEW_STATE["current_question"]
            if current is None:
                break
            self.current_score = 2 if not current["is_followup"] and INTERVIEW_STATE["main_questions_used"] < 4 else 8
            response = client.post("/api/answer", json={"answer": ANSWER})
            self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(INTERVIEW_STATE["execution_phase"], "completed")
        observed = client.get("/api/log").json
        self.assertEqual((observed["planner_state"]["main_questions_used"], observed["planner_state"]["followups_used"]), (8, 4))
        self.assertEqual((observed["session"]["main_question_budget"], observed["session"]["max_followups_total"], observed["session"]["adaptive_turn_budget"]), (8, 4, 12))
        self.assertEqual(observed["report"]["main_questions"], 8)
        self.assertEqual(observed["report"]["followups"], 4)
        self.assertEqual(len(observed["entries"]), 13)  # One unscored introduction.


if __name__ == "__main__":
    unittest.main()
