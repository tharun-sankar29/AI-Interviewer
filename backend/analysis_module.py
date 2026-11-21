"""Local resume retrieval and provider-neutral interview prompts."""

from __future__ import annotations

import logging
import os
import re
from io import BytesIO
from pathlib import Path

# Model assets are provisioned by the setup script; runtime should not block
# on remote metadata checks when operating from that local cache.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from llama_index.core import Document, Settings, VectorStoreIndex
from llama_index.core.retrievers import VectorIndexRetriever
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from pypdf import PdfReader

from backend.llm_service import InvalidModelResponse, complete_gemini_json, complete_groq_json, complete_json

logger = logging.getLogger(__name__)
_embed_model = None
JOB_DESCRIPTION_DIR = Path("job_description")
MAX_JOB_DESCRIPTION_CHARS = 100_000
SUPPORTED_JOB_DESCRIPTION_SUFFIXES = {".md", ".txt", ".pdf"}
SUPPORTED_RESUME_SUFFIXES = {".md", ".txt", ".pdf"}
MAX_RESUME_CHARS = 100_000
MAX_RESUME_BYTES = 20 * 1024 * 1024
_NAME_WORD = re.compile(r"[^\W\d_]{2,30}(?:[-'’][^\W\d_]{2,30})?", re.UNICODE)
_NAME_LABEL = re.compile(r"^(?:full\s+name|candidate\s+name|name)\s*[:\-]\s*(.+)$", re.IGNORECASE)
_CONTACT = re.compile(r"@|linkedin\.com|github\.com|https?://|www\.|\+?\d[\d\s().-]{7,}\d", re.IGNORECASE)
_NON_NAME_WORDS = {
    "resume", "curriculum", "vitae", "profile", "summary", "professional", "objective",
    "experience", "education", "skills", "projects", "contact", "details", "career",
    "software", "frontend", "backend", "engineer", "developer", "analyst", "manager",
    "designer", "consultant", "specialist", "intern", "student", "director", "lead",
    "senior", "junior", "product", "data", "business", "marketing", "sales",
}


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        cache_dir = os.getenv("EMBEDDING_CACHE_DIR", "models/huggingface")
        # Readiness verifies the cache before the candidate can start. Avoid
        # Hugging Face HEAD/retry delays during the live interview when the
        # machine has no network access.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        _embed_model = HuggingFaceEmbedding("sentence-transformers/all-MiniLM-L6-v2", cache_folder=cache_dir)
        Settings.embed_model = _embed_model
    return _embed_model


def active_resume_path() -> Path:
    return Path(os.getenv("RESUME_PATH", "interview_logs/sample_resume.md"))


def parse_job_description_upload(filename: str, raw: bytes) -> dict:
    """Extract a user-supplied Markdown, text, or PDF job description."""
    name = Path(filename or "job-description").name
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_JOB_DESCRIPTION_SUFFIXES:
        raise ValueError("Job descriptions must be .pdf, .md, or .txt files.")
    if not raw:
        raise ValueError("The uploaded job description is empty.")
    try:
        if suffix == ".pdf":
            reader = PdfReader(BytesIO(raw))
            if reader.is_encrypted:
                raise ValueError("Encrypted PDFs are not supported.")
            content = "\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
        else:
            content = raw.decode("utf-8").strip()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The uploaded job description could not be read.") from exc
    if not content:
        raise ValueError("No readable text was found in the uploaded job description.")
    truncated = len(content) > MAX_JOB_DESCRIPTION_CHARS
    return {
        "content": content[:MAX_JOB_DESCRIPTION_CHARS],
        "files": [name],
        "truncated": truncated,
    }


def parse_resume_upload(filename: str, raw: bytes) -> dict:
    """Extract a bounded, readable resume without writing it to disk."""
    name = Path(filename or "resume").name
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_RESUME_SUFFIXES:
        raise ValueError("Resumes must be .pdf, .md, or .txt files.")
    if not raw:
        raise ValueError("The uploaded resume is empty.")
    if len(raw) > MAX_RESUME_BYTES:
        raise ValueError("The resume is too large. Keep it under 20 MB.")
    try:
        if suffix == ".pdf":
            reader = PdfReader(BytesIO(raw))
            if reader.is_encrypted:
                raise ValueError("Encrypted PDFs are not supported.")
            content = "\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
        else:
            content = raw.decode("utf-8").strip()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The uploaded resume could not be read.") from exc
    if not content:
        raise ValueError("No readable text was found in the uploaded resume.")
    return {"filename": name, "text": content[:MAX_RESUME_CHARS], "truncated": len(content) > MAX_RESUME_CHARS}


def extract_candidate_first_name(resume_text: str | None) -> str | None:
    """Return a confident first name from the resume header, or None."""
    if not isinstance(resume_text, str) or not resume_text.strip():
        return None

    header_lines = [line.strip() for line in resume_text.splitlines() if line.strip()][:12]
    lines = [line.strip("#*_ ") for line in header_lines]

    def first_name(value: str) -> str | None:
        words = value.split("|", 1)[0].strip().split()
        if not 1 <= len(words) <= 4:
            return None
        if any(not _NAME_WORD.fullmatch(word) or word.casefold() in _NON_NAME_WORDS for word in words):
            return None
        name = words[0]
        return name.title() if name.isupper() else name

    if header_lines and (heading := re.fullmatch(r"#\s+(.+)", header_lines[0])):
        heading_name = first_name(heading.group(1).strip("*_ "))
        if heading_name:
            return heading_name

    for line in lines[:8]:
        match = _NAME_LABEL.fullmatch(line)
        if match:
            return first_name(match.group(1))

    for index, line in enumerate(lines[:8]):
        if _NAME_LABEL.fullmatch(line):
            continue
        name = first_name(line)
        if name and (_CONTACT.search(line) or any(_CONTACT.search(next_line) for next_line in lines[index + 1:index + 5])):
            return name
    return None


def load_job_description(directory=None) -> dict:
    """Load top-level Markdown and text-based PDF files as one JD corpus."""
    source_dir = Path(directory) if directory else JOB_DESCRIPTION_DIR
    if not source_dir.is_dir():
        logger.warning("job_description_directory_not_found path=%s", source_dir)
        return {"content": "", "files": [], "truncated": False}

    sections = []
    loaded_files = []
    candidates = sorted(
        (path for path in source_dir.iterdir() if path.is_file() and path.suffix.lower() in {".md", ".pdf"}),
        key=lambda path: path.name.lower(),
    )
    for path in candidates:
        try:
            if path.suffix.lower() == ".md":
                content = path.read_text(encoding="utf-8").strip()
            else:
                reader = PdfReader(str(path))
                if reader.is_encrypted:
                    logger.warning("job_description_file_skipped file=%s reason=encrypted_pdf", path.name)
                    continue
                content = "\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
        except Exception as exc:
            logger.warning("job_description_file_skipped file=%s reason=%s", path.name, type(exc).__name__)
            continue
        if not content:
            logger.warning("job_description_file_skipped file=%s reason=no_extractable_text", path.name)
            continue
        loaded_files.append(path.name)
        sections.append(f"--- FILE: {path.name} ---\n{content}")

    combined = "\n\n".join(sections)
    truncated = len(combined) > MAX_JOB_DESCRIPTION_CHARS
    if truncated:
        logger.warning(
            "job_description_truncated files=%d original_chars=%d limit=%d",
            len(loaded_files),
            len(combined),
            MAX_JOB_DESCRIPTION_CHARS,
        )
        combined = combined[:MAX_JOB_DESCRIPTION_CHARS]
    return {"content": combined, "files": loaded_files, "truncated": truncated}


def build_rag_index(data_dir=None, knowledge_content="", resume_content=None, resume_filename="uploaded_resume"):
    try:
        source = Path(data_dir) if data_dir else active_resume_path()
        if resume_content is not None:
            if isinstance(resume_content, dict):
                source_name = Path(resume_content.get("filename") or resume_filename or "uploaded_resume").name
                content = str(resume_content.get("text", resume_content.get("content", ""))).strip()
            else:
                content = str(resume_content).strip()
                source_name = Path(resume_filename or "uploaded_resume").name
        else:
            if not source.is_file():
                logger.warning("resume_file_not_found path=%s", source)
                return None
            content = source.read_text(encoding="utf-8").strip()
            source_name = source.name
        if not content:
            logger.warning("resume_file_empty path=%s", source_name)
            return None
        documents = [Document(text=content, metadata={"file_name": source_name, "source_type": "resume"})]
        if knowledge_content and knowledge_content.strip():
            documents.append(Document(text=knowledge_content.strip(), metadata={"file_name": "job_description", "source_type": "job_description"}))
        return VectorStoreIndex.from_documents(documents, embed_model=get_embed_model())
    except Exception:
        logger.exception("rag_index_build_failed")
        return None


def retrieve_context(rag_index, query: str, top_k: int = 2) -> str:
    if rag_index is None:
        return ""
    try:
        nodes = VectorIndexRetriever(index=rag_index, similarity_top_k=top_k).retrieve(query)
        return "\n\n".join(node.text for node in nodes)
    except Exception:
        logger.exception("rag_retrieval_failed")
        return ""


def generate_questions_from_resume(rag_index, num_questions=5, context=None, job_description=""):
    resume_context = retrieve_context(rag_index, "candidate experience, projects, and technical skills", 4)
    schema = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "minLength": 1},
                        "focus": {"type": "string", "minLength": 1},
                        "basis": {
                            "type": "string",
                            "enum": ["jd_resume_match", "jd_requirement", "resume_only"],
                        },
                    },
                    "required": ["question", "focus", "basis"],
                    "additionalProperties": False,
                },
                "minItems": num_questions,
                "maxItems": num_questions,
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    }
    if job_description:
        source_rules = """The job description is the source of truth for what must be tested. Every question must trace to a job requirement.
When the resume demonstrates a related skill or experience, personalize the question and set basis to "jd_resume_match".
For an important job requirement not demonstrated by the resume, ask a fair verification question and set basis to "jd_requirement".
When at least two questions are requested and overlap exists, ask the strongest resume/job match first and then cover a high-priority unmet or unverified requirement.
Do not use "resume_only" while a job description is available."""
    else:
        source_rules = """No usable job description is available. Base questions on the resume and set basis to "resume_only".
If the resume is unavailable, ask broadly applicable questions for the requested role context and still use "resume_only"."""

    prompt = f"""Generate exactly {num_questions} concise interview questions and return the required structured JSON.
First infer the professional or academic domain and seniority from the available material.
Match question difficulty to the job seniority and the candidate's demonstrated experience level.
Do not assume the candidate is a software engineer. Do not ask programming, web-development, or framework questions unless they are required by the job description or supported by the resume.
Do not invent resume facts, job requirements, projects, tools, or experience.

SOURCE PRIORITY:
{source_rules}

ROLE CONTEXT:
{context or 'General technical interview'}

CANDIDATE RESUME MATERIAL:
{resume_context or 'No candidate material was available; ask broadly applicable technical questions.'}

JOB DESCRIPTION MATERIAL:
{job_description or 'No usable job description was available.'}
"""
    result = complete_json(prompt, schema, {"temperature": 0.3, "max_tokens": 900, "schema_name": "interview_questions"})
    questions = result["questions"]
    if job_description and any(item["basis"] == "resume_only" for item in questions):
        raise InvalidModelResponse("The AI service returned questions that did not follow the job-description priority.")
    if not job_description:
        for item in questions:
            item["basis"] = "resume_only"
    return questions


def retrieve_evaluation_context(question, candidate_answer, rag_index):
    return retrieve_context(rag_index, f"{question}\n{candidate_answer}", 2)


SKILL_GRAPH_SCHEMA = {
    "type": "object",
    "properties": {
        "skills": {
            "type": "array",
            "minItems": 1,
            "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "minLength": 1},
                    "name": {"type": "string", "minLength": 1},
                    "category": {"type": "string", "minLength": 1},
                    "evidence": {"type": "string", "minLength": 1},
                },
                "required": ["skill_id", "name", "category", "evidence"],
                "additionalProperties": False,
            },
        },
        "edges": {
            "type": "array",
            "maxItems": 80,
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "minLength": 1},
                    "target": {"type": "string", "minLength": 1},
                    "relation": {"type": "string", "enum": ["prerequisite", "related"]},
                    "weight": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["source", "target", "relation", "weight"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["skills", "edges"],
    "additionalProperties": False,
}


def extract_skill_graph(rag_index, job_description="", context=None, *, provider="gemini"):
    """Extract a grounded initial graph from the requested provider."""
    from backend.skill_graph import normalize_skill_graph

    retrieved = retrieve_context(
        rag_index,
        "candidate technical skills, tools, engineering concepts, projects, education, and experience",
        8,
    )
    prompt = f"""Build an initial candidate-specific technical Skill Graph and return only the required JSON.
Extract concrete technical skills and topics grounded in the supplied material. Do not invent candidate experience.
Use stable lowercase snake_case skill IDs. Include only skills useful for evaluating this candidate for the role.
For every skill, evidence must name a concrete project, tool, responsibility, or requirement from the supplied material;
do not use generic labels such as "resume" or "job description" as evidence.
An edge source is a prerequisite of its target when relation is prerequisite. Use related only for close peer concepts.
Include supported relationships, but do not invent an edge just to connect independent skills.
Every edge must reference a skill ID in the skills array. Keep the graph focused and avoid generic soft skills.

ROLE CONTEXT:
{context or 'General technical interview'}

RETRIEVED RESUME AND KNOWLEDGE MATERIAL:
{retrieved or 'No retrievable material was available.'}

JOB DESCRIPTION MATERIAL:
{job_description or 'No usable job description was available.'}
"""
    options = {"temperature": 0.2, "max_tokens": 1400, "schema_name": "skill_graph"}
    if provider == "gemini":
        result = complete_gemini_json(prompt, SKILL_GRAPH_SCHEMA, options)
    elif provider == "groq":
        result = complete_groq_json(prompt, SKILL_GRAPH_SCHEMA, options)
    else:
        raise ValueError("Unsupported skill graph provider.")
    graph = normalize_skill_graph(result)
    if not graph["skills"]:
        raise InvalidModelResponse("The AI service returned an empty skill graph.")
    generic_evidence = {"resume", "jd", "job description", "technical", "resume_only", "jd_requirement", "jd_resume_match"}
    if any(not node["evidence"] or node["evidence"].casefold() in generic_evidence for node in graph["skills"].values()):
        raise InvalidModelResponse("The AI service returned unsupported skill evidence.")
    return graph
