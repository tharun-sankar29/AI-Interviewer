import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.analysis_module import (
    active_resume_path,
    build_rag_index,
    extract_candidate_first_name,
    generate_questions_from_resume,
    load_job_description,
)
from backend.llm_service import InvalidModelResponse


def generated(question="Explain static and dynamic loading.", focus="Load analysis", basis="jd_resume_match"):
    return {"questions": [{"question": question, "focus": focus, "basis": basis}]}


class ResumeRetrievalTests(unittest.TestCase):
    def test_configured_resume_path_is_used(self):
        with patch.dict(os.environ, {"RESUME_PATH": "candidate/mechanical.md"}):
            self.assertEqual(active_resume_path(), Path("candidate/mechanical.md"))

    def test_default_index_reads_only_the_configured_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.md"
            resume.write_text("Mechanical engineering and thermodynamics", encoding="utf-8")
            with (
                patch.dict(os.environ, {"RESUME_PATH": str(resume)}),
                patch("backend.analysis_module.get_embed_model", return_value=object()),
                patch("backend.analysis_module.VectorStoreIndex.from_documents", return_value="index") as create,
            ):
                self.assertEqual(build_rag_index(), "index")
            documents = create.call_args.args[0]
            self.assertEqual(len(documents), 1)
            self.assertIn("Mechanical engineering", documents[0].text)


class CandidateNameTests(unittest.TestCase):
    def test_labeled_name_and_all_caps_name(self):
        self.assertEqual(extract_candidate_first_name("Name: Priya Sharma\nExperience\n..."), "Priya")
        self.assertEqual(extract_candidate_first_name("# **ALEX MORGAN**\nalex@example.com\nSkills"), "Alex")
        self.assertEqual(extract_candidate_first_name("# Balaji\n\n## Education\nMechanical Engineering"), "Balaji")

    def test_header_name_next_to_contact_details(self):
        self.assertEqual(extract_candidate_first_name("Jordan Lee | jordan@example.com\nEngineer"), "Jordan")
        self.assertEqual(extract_candidate_first_name("Riley Chen\n+1 (555) 123-4567\nExperience"), "Riley")

    def test_uncertain_header_has_no_name(self):
        for text in (
            None,
            "",
            "Software Engineer\nengineer@example.com\nExperience",
            "Professional Summary\nBuilt reliable APIs.",
            "Projects\nJane Doe built several systems.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(extract_candidate_first_name(text))


class JobDescriptionTests(unittest.TestCase):
    def test_markdown_files_are_combined_alphabetically_and_other_types_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.md").write_text("Second requirement", encoding="utf-8")
            (root / "A.md").write_text("First requirement", encoding="utf-8")
            (root / "ignored.txt").write_text("Django", encoding="utf-8")
            result = load_job_description(root)
        self.assertEqual(result["files"], ["A.md", "b.md"])
        self.assertLess(result["content"].index("First requirement"), result["content"].index("Second requirement"))
        self.assertNotIn("Django", result["content"])

    def test_text_pdf_is_loaded(self):
        reader = Mock(is_encrypted=False, pages=[Mock(extract_text=Mock(return_value="CAD and manufacturing"))])
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "role.pdf"
            pdf.write_bytes(b"placeholder")
            with patch("backend.analysis_module.PdfReader", return_value=reader):
                result = load_job_description(directory)
        self.assertEqual(result["files"], ["role.pdf"])
        self.assertIn("CAD and manufacturing", result["content"])

    def test_encrypted_corrupt_and_image_only_pdfs_are_skipped(self):
        cases = [
            Mock(is_encrypted=True, pages=[]),
            Mock(is_encrypted=False, pages=[Mock(extract_text=Mock(return_value=""))]),
            ValueError("corrupt"),
        ]
        for index, outcome in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                Path(directory, "role.pdf").write_bytes(b"placeholder")
                effect = outcome if isinstance(outcome, Exception) else None
                with patch("backend.analysis_module.PdfReader", return_value=None if effect else outcome, side_effect=effect):
                    result = load_job_description(directory)
                self.assertEqual(result["files"], [])
                self.assertEqual(result["content"], "")

    def test_missing_directory_returns_empty_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            result = load_job_description(Path(directory) / "missing")
        self.assertEqual(result, {"content": "", "files": [], "truncated": False})

    def test_combined_content_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "role.md").write_text("A" * 200, encoding="utf-8")
            with patch("backend.analysis_module.MAX_JOB_DESCRIPTION_CHARS", 50):
                result = load_job_description(directory)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["content"]), 50)


class QuestionGenerationTests(unittest.TestCase):
    @patch("backend.analysis_module.retrieve_context", return_value="B.Tech Mechanical Engineering student")
    @patch("backend.analysis_module.complete_json", return_value=generated())
    def test_prompt_uses_resume_and_job_description_with_jd_priority(self, complete, _retrieve):
        questions = generate_questions_from_resume(
            object(),
            1,
            "Entry-level mechanical engineer",
            job_description="Requires load analysis and CAD fundamentals",
        )
        prompt = complete.call_args.args[0]
        self.assertIn("source of truth", prompt)
        self.assertIn("B.Tech Mechanical Engineering student", prompt)
        self.assertIn("Requires load analysis and CAD fundamentals", prompt)
        self.assertIn("Do not assume the candidate is a software engineer", prompt)
        self.assertEqual(questions[0]["basis"], "jd_resume_match")

    @patch("backend.analysis_module.retrieve_context", return_value="Mechanical engineering student")
    @patch("backend.analysis_module.complete_json", return_value=generated(basis="jd_requirement"))
    def test_without_job_description_forces_resume_only_basis(self, _complete, _retrieve):
        questions = generate_questions_from_resume(object(), 1, "Resume-based interview")
        self.assertEqual(questions[0]["basis"], "resume_only")

    @patch("backend.analysis_module.retrieve_context", return_value="Mechanical engineering student")
    @patch("backend.analysis_module.complete_json", return_value=generated(basis="resume_only"))
    def test_job_description_rejects_resume_only_generation(self, _complete, _retrieve):
        with self.assertRaises(InvalidModelResponse):
            generate_questions_from_resume(object(), 1, "Mechanical role", job_description="Requires CAD")


if __name__ == "__main__":
    unittest.main()
