"""Graph-provider recovery and short-interview coverage regressions."""

import os
import json
import tempfile
import unittest
from unittest.mock import patch

import app as application
from backend.adaptive_planner import select_next_untested_skill
from backend.analysis_module import SKILL_GRAPH_SCHEMA, extract_skill_graph
from backend.interview_session import INTERVIEW_STATE, start_interview, submit_answer
from backend.llm_service import InvalidModelResponse, LLMService, LLMServiceUnavailable
from backend.skill_graph import normalize_skill_graph


def graph(*names, edge=None):
    return normalize_skill_graph({
        "skills": [
            {"skill_id": name, "name": name.title(), "category": "technical", "evidence": f"Built a {name} project"}
            for name in names
        ],
        "edges": [edge] if edge else [],
    })


class GeminiSchemaTests(unittest.TestCase):
    def test_wire_schema_omits_max_items_but_local_check_retains_both_bounds(self):
        class Models:
            text = '{"skills":[],"edges":[]}'

            def generate_content(self, **kwargs):
                self.config = kwargs["config"]
                return type("Response", (), {"text": self.text})()

        models = Models()
        service = LLMService(gemini_client=type("Client", (), {"models": models})())
        with self.assertRaises(InvalidModelResponse):
            service.complete_gemini_json("graph", SKILL_GRAPH_SCHEMA)
        self.assertNotIn("maxItems", models.config.response_json_schema["properties"]["skills"])
        self.assertNotIn("maxItems", models.config.response_json_schema["properties"]["edges"])
        self.assertEqual(models.config.response_json_schema["properties"]["skills"]["minItems"], 1)
        self.assertEqual(SKILL_GRAPH_SCHEMA["properties"]["skills"]["minItems"], 1)
        self.assertEqual(SKILL_GRAPH_SCHEMA["properties"]["skills"]["maxItems"], 30)
        models.text = json.dumps({"skills": [
            {"skill_id": str(i), "name": "Python", "category": "technical", "evidence": "Built Python APIs"}
            for i in range(31)], "edges": []})
        with self.assertRaises(InvalidModelResponse):
            service.complete_gemini_json("graph", SKILL_GRAPH_SCHEMA)

    def test_groq_only_recovery_never_retries_gemini(self):
        class Completions:
            def create(self, **_kwargs):
                raise type("RateLimitError", (Exception,), {"status_code": 429})("limited")

        class Models:
            def generate_content(self, **_kwargs):
                raise AssertionError("Gemini was retried")

        groq = type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
        gemini = type("Client", (), {"models": Models()})()
        with self.assertRaises(LLMServiceUnavailable) as caught:
            LLMService(groq_client=groq, gemini_client=gemini).complete_groq_json("graph", SKILL_GRAPH_SCHEMA)
        self.assertEqual(caught.exception.category, "rate_limit")


class GraphRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.old_state = INTERVIEW_STATE.copy()
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.temp.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.temp.cleanup()
        INTERVIEW_STATE.clear()
        INTERVIEW_STATE.update(self.old_state)

    def test_groq_graph_recovers_from_gemini_failure_and_is_reported(self):
        expected = graph("sql", "api", edge={"source": "sql", "target": "api", "relation": "prerequisite", "weight": .8})
        providers = []

        def extract(_index, _jd, _context, *, provider="gemini"):
            providers.append(provider)
            if provider == "gemini":
                raise LLMServiceUnavailable("gemini_initialization_failure")
            return expected

        with patch("backend.interview_session.build_rag_index", return_value="index"), \
             patch("backend.interview_session.extract_skill_graph", side_effect=extract):
            result = start_interview("Backend engineer", mode="quick", job_description_override="SQL and APIs")
        self.assertEqual(providers, ["gemini", "groq"])
        self.assertEqual(result["graph_source"], "groq")
        self.assertTrue(result["graph_initialized"])
        self.assertEqual(len(result["skill_graph"]["edges"]), 1)
        self.assertEqual(application.app.test_client().get("/api/log").json["graph_source"], "groq")

    def test_degraded_question_fallback_is_reported(self):
        questions = [{"question": "How do you use SQL?", "skill_id": "sql", "focus": "SQL", "basis": "jd_requirement"}]
        with patch("backend.interview_session.build_rag_index", return_value="index"), \
             patch("backend.interview_session.extract_skill_graph", side_effect=LLMServiceUnavailable("failure")), \
             patch("backend.interview_session.generate_questions_from_resume", return_value=questions):
            result = start_interview("Backend engineer", mode="quick", job_description_override="SQL")
        self.assertEqual(result["graph_source"], "question_fallback")
        self.assertFalse(result["graph_initialized"])
        self.assertEqual(result["skill_graph"]["edges"], [])

    def test_extraction_rejects_generic_evidence(self):
        with patch("backend.analysis_module.retrieve_context", return_value="Python API project"), \
             patch("backend.analysis_module.complete_gemini_json", return_value={
                 "skills": [{"skill_id": "python", "name": "Python", "category": "technical", "evidence": "resume"}],
                 "edges": [],
             }):
            with self.assertRaises(InvalidModelResponse):
                extract_skill_graph("index")


class BreadthTests(unittest.TestCase):
    def setUp(self):
        self.old_state = INTERVIEW_STATE.copy()
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.temp.name)

    def tearDown(self):
        os.chdir(self.cwd)
        self.temp.cleanup()
        INTERVIEW_STATE.clear()
        INTERVIEW_STATE.update(self.old_state)

    def test_prerequisite_is_tested_once_before_dependent(self):
        candidate = graph("api", "sql", "tests", edge={"source": "sql", "target": "api", "relation": "prerequisite", "weight": .8})
        self.assertEqual(select_next_untested_skill(candidate)[0], "sql")
        candidate["skills"]["sql"]["evidence_count"] = 1
        candidate["skills"]["sql"]["mastery"] = .2
        self.assertEqual(select_next_untested_skill(candidate)[0], "api")

    def run_quick(self, candidate, scores, *, answers=None, repeated_followup=False):
        question_number = 0
        evaluations = iter(enumerate(scores, 1))

        def model(_prompt, schema, _options):
            nonlocal question_number
            if set(schema["properties"]) == {"question"}:
                question_number += 1
                return {"question": f"Technical question {question_number}?"}
            turn, score = next(evaluations)
            return {"reaction": "You gave a concrete answer.", "score": score,
                    "evaluation": "The explanation lacked one important detail.",
                    "next_question": "Technical question 1?" if repeated_followup else f"Can you explain the missing detail from answer {turn}?"}

        with patch("backend.interview_session.build_rag_index", return_value="index"), \
             patch("backend.interview_session.extract_skill_graph", return_value=candidate), \
             patch("backend.interview_session.complete_json", side_effect=model), \
             patch("backend.interview_session.complete_text", return_value="Final report."), \
             patch("backend.interview_session.retrieve_evaluation_context", return_value="Relevant project"):
            start_interview("Backend engineer", mode="quick", job_description_override="APIs, SQL, testing, monitoring")
            submit_answer("I am a backend engineer with several production projects.")
            for answer in answers or ["I designed the feature, tested failure cases, and monitored its behavior in production."] * len(scores):
                submit_answer(answer)
        return INTERVIEW_STATE

    def test_quick_spans_four_skills_with_answer_specific_followup(self):
        candidate = graph("api", "sql", "tests", "monitoring", edge={"source": "sql", "target": "api", "relation": "prerequisite", "weight": .8})
        state = self.run_quick(candidate, [2, 7, 7, 7, 7])
        self.assertEqual([entry["skill_id"] for entry in state["log"]], ["sql", "sql", "api", "tests", "monitoring"])
        self.assertEqual(state["log"][1]["question"], "Can you explain the missing detail from answer 1?")
        self.assertEqual(state["questions_asked"], 5)
        self.assertEqual(state["main_questions_used"], 4)
        self.assertEqual(state["followups_used"], 1)
        self.assertEqual(state["execution_phase"], "completed")
        self.assertEqual({name: node["evidence_count"] for name, node in state["skill_graph"]["skills"].items()},
                         {"api": 1, "sql": 2, "tests": 1, "monitoring": 1})

    def test_quick_allows_two_followups_but_never_chains_them(self):
        state = self.run_quick(graph("api", "sql", "tests", "monitoring", "cache"), [2, 2, 5, 2, 7, 7])
        self.assertEqual([entry["is_followup"] for entry in state["log"]], [False, True, False, True, False, False])
        self.assertEqual(state["main_questions_used"], 4)
        self.assertEqual(state["followups_used"], 2)
        self.assertEqual(state["questions_asked"], 6)
        self.assertEqual(state["execution_phase"], "completed")

    def test_quick_ends_after_four_good_main_answers(self):
        state = self.run_quick(graph("api", "sql", "tests", "monitoring", "cache"), [7, 7, 7, 7])
        self.assertEqual(state["main_questions_used"], 4)
        self.assertEqual(state["followups_used"], 0)
        self.assertEqual(state["questions_asked"], 4)
        self.assertEqual(state["execution_phase"], "completed")

    def test_quick_followup_can_follow_fourth_main_answer(self):
        state = self.run_quick(graph("api", "sql", "tests", "monitoring", "cache"), [7, 7, 7, 5, 7])
        self.assertTrue(state["log"][-1]["is_followup"])
        self.assertEqual(state["main_questions_used"], 4)
        self.assertEqual(state["followups_used"], 1)
        self.assertEqual(state["execution_phase"], "completed")

    def test_quick_short_answer_triggers_followup_even_with_high_score(self):
        state = self.run_quick(graph("api", "sql", "tests", "monitoring"), [8], answers=["Brief answer."])
        self.assertTrue(state["next_questions"][0]["is_followup"])
        self.assertEqual(state["next_questions"][0]["skill_id"], state["log"][0]["skill_id"])

    def test_quick_never_exceeds_two_followups_for_all_weak_answers(self):
        state = self.run_quick(graph("api", "sql", "tests", "monitoring", "cache"), [2] * 6)
        self.assertEqual(state["followups_used"], 2)
        self.assertEqual(state["main_questions_used"], 4)
        self.assertEqual(state["questions_asked"], 6)
        self.assertEqual(state["execution_phase"], "completed")

    def test_quick_allows_only_one_followup_when_retesting_the_same_skill(self):
        state = self.run_quick(graph("api"), [2] * 5)
        self.assertEqual(state["main_questions_used"], 4)
        self.assertEqual(state["followups_used"], 1)
        self.assertEqual(state["questions_asked"], 5)
        self.assertEqual(state["execution_phase"], "completed")

    def test_quick_regenerates_a_repeated_evaluator_followup(self):
        state = self.run_quick(graph("api", "sql", "tests", "monitoring"), [2], repeated_followup=True)
        self.assertTrue(state["next_questions"][0]["is_followup"])
        self.assertEqual(state["next_questions"][0]["question"], "Technical question 2?")

    def test_quick_session_reports_both_limits(self):
        mode = application.app.test_client().get("/api/roles").json["modes"]["quick"]
        self.assertEqual(mode["main_question_budget"], 4)
        self.assertEqual(mode["max_followups_total"], 2)
        self.assertEqual(mode["adaptive_turn_budget"], 6)


if __name__ == "__main__":
    unittest.main()
