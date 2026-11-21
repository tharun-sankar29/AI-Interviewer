"""Interview state machine with atomic model-backed turns."""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import tempfile
import uuid
from threading import RLock
from copy import deepcopy
from pathlib import Path

from backend.analysis_module import build_rag_index, extract_candidate_first_name, extract_skill_graph, generate_questions_from_resume, load_job_description, retrieve_evaluation_context
from backend.llm_service import LLMServiceUnavailable, complete_json, complete_text
from backend.skill_graph import GRAPH_VERSION, extract_behavior_signals, propagate_mastery, update_mastery
from backend.adaptive_planner import all_mastered, fingerprint, is_mastered, next_spec, refresh_statuses, select_next_skill, select_next_untested_skill

logger = logging.getLogger(__name__)
SESSION_LOCK = RLock()
ANSWER_RECEIPTS = {}

INTERVIEW_MODES = {
    # ``adaptive`` is retained as the public API identifier for Fully Adaptive.
    "adaptive": {"label": "Fully Adaptive", "description": "Continues until the skill graph is mastered or someone ends the interview.", "adaptive_turn_budget": None, "allow_followups": True, "max_followups_per_skill": 3, "fully_adaptive": True},
    "standard": {"label": "Standard", "description": "Eight main skill questions plus up to four answer-specific follow-ups (twelve scored answers maximum).", "adaptive_turn_budget": 12, "main_question_budget": 8, "max_followups_total": 4, "allow_followups": True, "max_followups_per_skill": 1, "fully_adaptive": False},
    "quick": {"label": "Quick", "description": "Four main skill questions plus up to two answer-specific follow-ups (six scored answers maximum).", "adaptive_turn_budget": 6, "main_question_budget": 4, "max_followups_total": 2, "allow_followups": True, "max_followups_per_skill": 1, "fully_adaptive": False},
    "thorough": {"label": "Thorough", "description": "Twelve main skill questions plus up to six answer-specific follow-ups (eighteen scored answers maximum).", "adaptive_turn_budget": 18, "main_question_budget": 12, "max_followups_total": 6, "allow_followups": True, "max_followups_per_skill": 2, "fully_adaptive": False},
}

ROLE_LIBRARY = [
    {"id": "software-engineer", "name": "Software Engineer", "category": "Engineering", "description": "Algorithms, systems, debugging, and practical engineering decisions."},
    {"id": "frontend-engineer", "name": "Frontend Engineer", "category": "Engineering", "description": "Web architecture, accessibility, performance, and UI engineering."},
    {"id": "backend-engineer", "name": "Backend Engineer", "category": "Engineering", "description": "APIs, databases, distributed systems, and reliability."},
    {"id": "data-analyst", "name": "Data Analyst", "category": "Data", "description": "Metrics, SQL, experimentation, and communicating insights."},
    {"id": "product-manager", "name": "Product Manager", "category": "Product", "description": "Product strategy, prioritization, discovery, and trade-offs."},
    {"id": "ux-designer", "name": "UX/Product Designer", "category": "Design", "description": "Research, interaction design, prototyping, and design decisions."},
    {"id": "marketing-manager", "name": "Marketing Manager", "category": "Marketing", "description": "Positioning, campaigns, growth experiments, and measurement."},
    {"id": "sales-representative", "name": "Sales Representative", "category": "Sales", "description": "Discovery, objections, relationship building, and closing."},
    {"id": "customer-success", "name": "Customer Success Manager", "category": "Customer", "description": "Onboarding, retention, stakeholder communication, and outcomes."},
    {"id": "finance-analyst", "name": "Finance Analyst", "category": "Finance", "description": "Financial modeling, forecasting, analysis, and business judgment."},
]

SESSION_CONFIG = {
    "session_id": str(uuid.uuid4()),
    "role_id": None,
    "role_name": None,
    "category": None,
    "context": "",
    "mode": "adaptive",
    "job_description": "",
    "job_description_files": [],
    "job_description_source": "default",
    "launched": False,
    "resume_content": None,
    "resume_filename": None,
    "candidate_first_name": None,
    "resume_uploaded": False,
    "candidate_started": False,
}

INTRO_QUESTION = (
    "To begin, please introduce yourself and briefly describe your background, "
    "experience, and the kind of work you’re interested in."
)

INTERVIEW_STATE = {
    "context": "",
    "log": [],
    "intro_entry": None,
    "next_questions": [],
    "rag_index": None,
    "mode": "adaptive",
    "log_file": "",
    "session_record": None,
    "main_questions": [],
    "current_main_index": 0,
    "followups_for_current": 0,
    "job_description": "",
    "job_description_files": [],
    "skill_graph": {"version": GRAPH_VERSION, "skills": {}, "edges": []},
    "graph_initialized": False,
    "graph_source": None,
    "graph_version": GRAPH_VERSION,
    "last_question_audio_ended_at": None,
    "current_skill_id": None,
    "adaptive": False,
    "current_question": None,
    "question_history": [],
    "questions_asked": 0,
    "main_questions_used": 0,
    "followups_used": 0,
    "question_fingerprints": [],
    "planner_reason": None,
    "current_difficulty": None,
    "current_question_type": None,
    "turn_number": 0,
    "stopping_reason": None,
    "execution_phase": "idle",
    "report": None,
}
_EMPTY_INTERVIEW_STATE = deepcopy(INTERVIEW_STATE)


def _reset_interview_state():
    INTERVIEW_STATE.clear()
    INTERVIEW_STATE.update(deepcopy(_EMPTY_INTERVIEW_STATE))


def session_config() -> dict:
    return deepcopy(SESSION_CONFIG)


def configure_session(*, role_id: str, role_name: str | None = None, mode: str = "adaptive", context: str | None = None,
                      job_description: str = "", job_description_files=None,
                      job_description_source: str = "default") -> dict:
    if role_id == "custom":
        if not isinstance(role_name, str) or not 1 <= len(role_name.strip()) <= 80:
            raise ValueError("Enter a custom role name between 1 and 80 characters.")
        name = role_name.strip()
        category = "Custom"
        role_context = name
    else:
        role = next((item for item in ROLE_LIBRARY if item["id"] == role_id), None)
        if role is None:
            raise ValueError("Choose a valid role before continuing.")
        name = role["name"]
        category = role["category"]
        role_context = (context or name).strip() or name
    if mode not in INTERVIEW_MODES:
        raise ValueError("Choose a valid interview mode.")
    content = (job_description or "").strip()
    if len(content) > 100_000:
        raise ValueError("The job description is too long. Keep it under 100,000 characters.")
    _interrupt_active_record("reconfigured")
    SESSION_CONFIG.update({
        "session_id": str(uuid.uuid4()),
        "role_id": role_id,
        "role_name": name,
        "category": category,
        "context": role_context,
        "mode": mode,
        "job_description": content,
        "job_description_files": list(job_description_files or []),
        "job_description_source": job_description_source if content else "default",
        "launched": False,
        "resume_content": None,
        "resume_filename": None,
        "candidate_first_name": None,
        "resume_uploaded": False,
        "candidate_started": False,
    })
    _reset_interview_state()
    ANSWER_RECEIPTS.clear()
    return session_config()


def clear_session_config() -> None:
    _interrupt_active_record("reset")
    SESSION_CONFIG.update({
        "session_id": str(uuid.uuid4()),
        "role_id": None, "role_name": None, "category": None, "context": "",
        "mode": "adaptive", "job_description": "", "job_description_files": [],
        "job_description_source": "default", "launched": False,
        "resume_content": None, "resume_filename": None, "candidate_first_name": None, "resume_uploaded": False,
        "candidate_started": False,
    })
    _reset_interview_state()
    ANSWER_RECEIPTS.clear()


def mark_session_launched() -> dict:
    SESSION_CONFIG["launched"] = True
    return session_config()


def store_resume(filename: str, content: str) -> dict:
    if not SESSION_CONFIG.get("launched"):
        raise ValueError("The observer must launch a session before uploading a resume.")
    if SESSION_CONFIG.get("candidate_started"):
        raise ValueError("A resume cannot be replaced during an interview. Ask the observer to reset the session.")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("The uploaded resume is empty.")
    SESSION_CONFIG.update({"resume_content": content, "resume_filename": filename,
                           "candidate_first_name": extract_candidate_first_name(content),
                           "resume_uploaded": True, "candidate_started": False})
    return session_config()

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "reaction": {"type": "string", "minLength": 1},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "evaluation": {"type": "string", "minLength": 1},
        "next_question": {"type": "string", "minLength": 1},
    },
    "required": ["reaction", "score", "evaluation", "next_question"],
    "additionalProperties": False,
}

QUESTION_SCHEMA = {
    "type": "object", "properties": {"question": {"type": "string", "minLength": 1}},
    "required": ["question"], "additionalProperties": False,
}


def _planner_state():
    graph = INTERVIEW_STATE.get("skill_graph") or {}
    return {
        "questions_asked": INTERVIEW_STATE.get("questions_asked", 0),
        "main_questions_used": INTERVIEW_STATE.get("main_questions_used", 0),
        "followups_used": INTERVIEW_STATE.get("followups_used", 0),
        "evaluated_turn_count": INTERVIEW_STATE.get("questions_asked", 0),
        "followup_depth": (INTERVIEW_STATE.get("current_question") or {}).get("followup_depth", 0),
        "active_skill_count": sum(1 for node in graph.get("skills", {}).values() if not (node.get("mastery", 0) >= .85 and node.get("confidence", 0) >= .85)),
        "mastered_skill_count": sum(1 for node in graph.get("skills", {}).values() if node.get("mastery", 0) >= .85 and node.get("confidence", 0) >= .85),
    }


def _question_spec(spec, question):
    value = question.get("question", "")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("The AI service returned an empty question.")
    value = value.strip()
    result = {"question": value, **{key: spec[key] for key in ("skill_id", "difficulty", "question_type", "planner_reason", "is_followup")}}
    result["followup_depth"] = int(spec.get("followup_depth", 0))
    result["fingerprint"] = fingerprint(value)
    prior_fingerprints = set(INTERVIEW_STATE.get("question_fingerprints", []))
    current = (INTERVIEW_STATE.get("next_questions") or [INTERVIEW_STATE.get("current_question")])[0]
    if current and current.get("question"):
        prior_fingerprints.add(fingerprint(current["question"]))
    prior_fingerprints.update(fingerprint(item["question"]) for item in INTERVIEW_STATE.get("log", []) if item.get("question"))
    if result["fingerprint"] in prior_fingerprints:
        raise ValueError("The AI service returned a repeated question.")
    return result


def _recent_questions():
    prior = [item["question"] for item in INTERVIEW_STATE.get("question_history", []) if item.get("question")]
    current = INTERVIEW_STATE.get("current_question")
    if current and not current.get("is_intro") and current.get("question"):
        prior.append(current["question"])
    return prior[-12:]


def _generate_adaptive_question(spec, *, graph=None):
    graph = graph if graph is not None else INTERVIEW_STATE["skill_graph"]
    node = graph["skills"][spec["skill_id"]]
    resume_context = retrieve_evaluation_context(node["name"], node.get("evidence", ""), INTERVIEW_STATE["rag_index"])
    prerequisites = [graph["skills"][edge["source"]]["name"] for edge in graph.get("edges", []) if edge.get("relation") == "prerequisite" and edge.get("target") == spec["skill_id"]]
    prompt = f"""Generate exactly one natural-language technical interview question as strict JSON.
Assess only the selected skill. Do not mention planning, scores, confidence, or other skills.
Selected skill: {node['name']} (skill_id: {spec['skill_id']})
Mastery: {node.get('mastery', 0.5)}; confidence: {node.get('confidence', 0.0)}
Difficulty level: {spec['difficulty']}; question type: {spec['question_type']}
Prerequisite context: {', '.join(prerequisites) or 'none'}
Relevant resume/JD excerpts: {resume_context or 'none'}
Recent questions already asked: {json.dumps(_recent_questions())}
Ask a distinct question; do not repeat the wording or scenario of a recent question.
Return only {{\"question\": \"...\"}}."""
    result = complete_json(prompt, QUESTION_SCHEMA, {"temperature": 0.3, "max_tokens": 300, "schema_name": "adaptive_question"})
    return _question_spec(spec, result)


def _generate_followup_question(spec, evaluation, current, candidate_answer, *, graph=None):
    """Prefer the evaluator's answer-specific question, regenerating only if unusable."""
    try:
        return _question_spec(spec, {"question": evaluation.get("next_question", "")})
    except ValueError:
        graph = graph if graph is not None else INTERVIEW_STATE.get("skill_graph", {})
        selected_skill = graph.get("skills", {}).get(spec["skill_id"], {}).get("name", current.get("focus", "Current question"))
        prompt = f"""Ask exactly one technical follow-up as strict JSON about the missing detail in this answer.
Stay on the same skill and do not repeat the original question.
Selected skill: {selected_skill}
Original question: {current['question']}
Candidate answer: {candidate_answer}
Evaluation: {evaluation['evaluation']}
Recent questions already asked: {json.dumps(_recent_questions())}
Return only {{"question": "..."}}."""
        result = complete_json(prompt, QUESTION_SCHEMA, {"temperature": 0.3, "max_tokens": 300, "schema_name": "adaptive_question"})
        return _question_spec(spec, result)


def _adaptive_next_spec(score=None, current=None, *, graph=None):
    graph = graph if graph is not None else INTERVIEW_STATE["skill_graph"]
    if not INTERVIEW_MODES[INTERVIEW_STATE["mode"]]["fully_adaptive"]:
        skill_id, reason = select_next_untested_skill(graph)
        if not skill_id:
            skill_id, reason = select_next_skill(graph)
        if not skill_id and graph.get("skills"):
            node = min(graph["skills"].values(), key=lambda item: (item.get("questions_asked", 0), item["skill_id"]))
            skill_id, reason = node["skill_id"], "main_question_coverage"
        if not skill_id:
            return None
        node = graph["skills"][skill_id]
        return next_spec(
            node,
            score=score if current and skill_id == current.get("skill_id") else None,
            previous_type=current.get("question_type") if current else None,
            previous_difficulty=current.get("difficulty") if current else None,
            reason=reason,
        )
    if score is not None and current and score < 4:
        node = graph["skills"][current["skill_id"]]
        prerequisites = [edge["source"] for edge in graph.get("edges", []) if edge.get("relation") == "prerequisite" and edge.get("target") == current["skill_id"]]
        weak = [graph["skills"][item] for item in prerequisites if not (graph["skills"][item].get("mastery", 0) >= .85 and graph["skills"][item].get("confidence", 0) >= .85)]
        if weak:
            node = weak[0]
            return next_spec(node, score=score, previous_type=current.get("question_type"), previous_difficulty=current.get("difficulty"), is_followup=True, reason="prerequisite_remediation")
        return next_spec(node, score=score, previous_type=current.get("question_type"), previous_difficulty=current.get("difficulty"), is_followup=True)
    if score is not None and current and score <= 8:
        node = graph["skills"][current["skill_id"]]
        if is_mastered(node):
            skill_id, reason = select_next_skill(graph)
            if not skill_id:
                return None
            return next_spec(graph["skills"][skill_id], previous_type=current.get("question_type"), previous_difficulty=current.get("difficulty"), reason=reason)
        return next_spec(node, score=score, previous_type=current.get("question_type"), previous_difficulty=current.get("difficulty"))
    if score is not None and current and score >= 9 and int(current.get("difficulty", 2)) < 5:
        node = graph["skills"][current["skill_id"]]
        if is_mastered(node):
            skill_id, reason = select_next_skill(graph)
            if not skill_id:
                return None
            return next_spec(graph["skills"][skill_id], previous_type=current.get("question_type"), previous_difficulty=current.get("difficulty"), reason=reason)
        return next_spec(node, score=score, previous_type=current.get("question_type"), previous_difficulty=current.get("difficulty"))
    skill_id, reason = select_next_skill(graph)
    if not skill_id:
        return None
    node = graph["skills"][skill_id]
    spec = next_spec(node, previous_type=current.get("question_type") if current else None, previous_difficulty=current.get("difficulty") if current else None, reason=reason)
    spec["planner_reason"] = reason if node.get("evidence_count", 0) == 0 else spec["planner_reason"]
    return spec


def _log_interview_state(
    status: str,
    *,
    reaction: str | None = None,
    follow_up_required: bool = False,
    follow_up_question: str | None = None,
) -> None:
    """Log useful progression state without exposing interview content."""
    entries = INTERVIEW_STATE["log"]
    next_question = INTERVIEW_STATE["next_questions"][0] if INTERVIEW_STATE["next_questions"] else None
    mode = INTERVIEW_MODES[INTERVIEW_STATE["mode"]]
    snapshot = {
        "status": status,
        "mode": INTERVIEW_STATE["mode"],
        "main_question_index": (INTERVIEW_STATE["main_questions_used"] + (0 if next_question.get("is_intro") or next_question.get("is_followup") else 1)) if next_question else None,
        "main_questions_total": mode.get("main_question_budget"),
        "main_questions_used": INTERVIEW_STATE["main_questions_used"],
        "followups_used": INTERVIEW_STATE["followups_used"],
        "max_followups_total": mode.get("max_followups_total"),
        "responses_recorded": len(entries),
        "followups_for_current": INTERVIEW_STATE["followups_for_current"],
        "awaiting_answer": next_question is not None,
        "next_is_followup": next_question.get("is_followup", False) if next_question else False,
        "last_score": entries[-1]["score"] if entries else None,
        "average_score": round(sum(entry["score"] for entry in entries) / len(entries), 2) if entries else None,
        "reaction": reaction,
        "follow_up_required": follow_up_required,
        "follow_up_question": follow_up_question if follow_up_required else None,
        "job_description_loaded": bool(INTERVIEW_STATE["job_description"]),
        "job_description_file_count": len(INTERVIEW_STATE["job_description_files"]),
        "question_focus": next_question.get("focus") if next_question else None,
        "question_basis": next_question.get("basis") if next_question else None,
    }
    lines = [
        f"  {key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in snapshot.items()
    ]
    logger.info("interview_state\n%s", "\n".join(lines))


def _atomic_write(path: Path, content: str) -> None:
    """Replace a whole artifact only after its new contents are safely written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _analysis_path(log_file: str | Path) -> Path:
    return Path(log_file).parent / "analysis.md"


def _new_record(log_file, *, role_name, context, mode, graph, current_question, graph_source=None):
    now = datetime.datetime.now().isoformat()
    return {
        "schema_version": 1,
        "interview_id": Path(log_file).parent.name,
        "session_id": SESSION_CONFIG["session_id"],
        "started_at": now,
        "updated_at": now,
        "ended_at": None,
        "role_name": role_name,
        "context": context,
        "mode": mode,
        "status": "in_progress",
        "stopping_reason": None,
        "graph_source": graph_source,
        "skill_graph": deepcopy(graph),
        "current_question": deepcopy(current_question),
        "entries": [],
        "analysis_file": None,
    }


def _prospective_record(entries, graph, current_question, *, intro_entry=None,
                        status="in_progress", stopping_reason=None, analysis_file=None):
    log_file = INTERVIEW_STATE["log_file"]
    record = deepcopy(INTERVIEW_STATE.get("session_record") or _new_record(
        log_file, role_name=INTERVIEW_STATE.get("role_name") or INTERVIEW_STATE.get("context", "Interview"),
        context=INTERVIEW_STATE.get("context", ""), mode=INTERVIEW_STATE.get("mode", "adaptive"),
        graph=graph, current_question=current_question,
        graph_source=INTERVIEW_STATE.get("graph_source"),
    ))
    new_entries = deepcopy(([intro_entry] if intro_entry else []) + list(entries))
    for previous, entry in zip(record.get("entries", []), new_entries):
        if previous.get("video_file"):
            entry["video_file"] = previous["video_file"]
    record.update({
        "updated_at": datetime.datetime.now().isoformat(),
        "ended_at": datetime.datetime.now().isoformat() if status != "in_progress" else None,
        "status": status,
        "stopping_reason": stopping_reason,
        "skill_graph": deepcopy(graph),
        "current_question": deepcopy(current_question),
        "entries": new_entries,
        "analysis_file": str(analysis_file) if analysis_file else None,
    })
    return record


def _save_record(record, log_file):
    _atomic_write(Path(log_file), json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def _interrupt_active_record(reason):
    record = INTERVIEW_STATE.get("session_record")
    path = Path(INTERVIEW_STATE.get("log_file") or "")
    if not record or record.get("status") != "in_progress" or not path.is_file():
        return
    interrupted = deepcopy(record)
    interrupted.update(status="interrupted", stopping_reason=reason,
                       updated_at=datetime.datetime.now().isoformat(),
                       ended_at=datetime.datetime.now().isoformat(), current_question=None)
    _save_record(interrupted, path)
    INTERVIEW_STATE["session_record"] = interrupted


def _save_completed_artifacts(record, log_file, entries, narrative, report, graph):
    path = _analysis_path(log_file)
    _atomic_write(path, _render_analysis(entries, narrative, report, graph))
    try:
        _save_record(record, log_file)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def build_skill_report(entries, skill_graph=None, *, stopping_reason=None):
    """Build deterministic, model-independent report data from final evidence."""
    graph = skill_graph or INTERVIEW_STATE.get("skill_graph") or {"skills": {}, "edges": []}
    technical = [entry for entry in entries if entry.get("skill_id")]
    scores = [entry.get("score") for entry in entries if isinstance(entry.get("score"), (int, float))]
    skills = []
    for skill_id, node in graph.get("skills", {}).items():
        evidence = [entry for entry in technical if entry.get("skill_id") == skill_id]
        evidence_scores = [entry["score"] for entry in evidence if isinstance(entry.get("score"), (int, float))]
        signals = [entry.get("signals", {}) for entry in evidence]
        latencies = [item.get("latency_ms") for item in signals if isinstance(item.get("latency_ms"), (int, float))]
        inputs = sorted({item.get("input_mode", "text") for item in signals})
        bases = sorted({entry.get("basis") for entry in evidence if entry.get("basis")})
        skills.append({
            "skill_id": skill_id,
            "name": node.get("name", skill_id),
            "mastery": round(float(node.get("mastery", 0.0)), 4),
            "confidence": round(float(node.get("confidence", 0.0)), 4),
            "status": node.get("status", "untested"),
            "evidence_count": int(node.get("evidence_count", len(evidence))),
            "questions_asked": int(node.get("questions_asked", len(evidence))),
            "average_score": round(sum(evidence_scores) / len(evidence_scores), 2) if evidence_scores else None,
            "highest_score": max(evidence_scores) if evidence_scores else None,
            "lowest_score": min(evidence_scores) if evidence_scores else None,
            "followup_count": sum(1 for entry in evidence if entry.get("is_followup")),
            "input_modes": inputs,
            "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "basis": bases,
        })
    strongest = [item["skill_id"] for item in sorted(skills, key=lambda item: (item["mastery"], item["confidence"]), reverse=True) if item["evidence_count"] and item["mastery"] >= .7][:3]
    weakest = [item["skill_id"] for item in sorted(skills, key=lambda item: (item["mastery"], item["confidence"])) if item["evidence_count"] and item["status"] in {"weak", "developing"}][:3]
    untested = [item["skill_id"] for item in skills if item["evidence_count"] == 0]
    prerequisite_observations = []
    by_id = {item["skill_id"]: item for item in skills}
    for edge in graph.get("edges", []):
        if edge.get("relation") != "prerequisite" or edge.get("source") not in by_id or edge.get("target") not in by_id:
            continue
        source, target = by_id[edge["source"]], by_id[edge["target"]]
        if source["status"] in {"weak", "developing", "untested"} and target["mastery"] > source["mastery"]:
            prerequisite_observations.append(f"{source['name']} remains a prerequisite risk for {target['name']}.")
        elif source["mastery"] >= .7 and target["mastery"] < .7:
            prerequisite_observations.append(f"{source['name']} is stronger than the dependent skill {target['name']}.")
    recommendations = [f"Revisit {by_id[skill_id]['name']} with a simpler explanation and a concrete application example." for skill_id in weakest]
    recommendations.extend(f"Verify {by_id[skill_id]['name']} with an applied question." for skill_id in untested[:3])
    return {
        "overall_score": round(sum(scores) / len(scores), 2) if scores else 0,
        "questions_answered": len(entries),
        "main_questions": sum(1 for entry in entries if not entry.get("is_followup")),
        "followups": sum(1 for entry in entries if entry.get("is_followup")),
        "stopping_reason": stopping_reason or INTERVIEW_STATE.get("stopping_reason"),
        "skills": skills,
        "strongest_skills": strongest,
        "weakest_skills": weakest,
        "untested_skills": untested,
        "prerequisite_observations": prerequisite_observations,
        "recommendations": recommendations,
    }


def _report_prompt(entries, report=None, skill_graph=None):
    transcript = "\n\n".join(
        f"Question: {entry['question']}\nAnswer: {entry['answer']}\n"
        f"Focus: {entry.get('focus', 'Not specified')}\nBasis: {entry.get('basis', 'Not specified')}\n"
        f"Score: {entry['score']}/10\nEvaluation: {entry['evaluation']}"
        for entry in entries
    )
    report = report or build_skill_report(entries, skill_graph)
    graph = skill_graph or INTERVIEW_STATE.get("skill_graph") or {}
    return f"""Write a concise final interview report with these headings:
PERFORMANCE SUMMARY, TECHNICAL PROFICIENCY, COMMUNICATION SKILLS, AREAS FOR IMPROVEMENT, RECOMMENDATIONS.
Base every observation on the transcript and computed report data. Do not alter, recalculate, or contradict any numerical values.
Do not invent skills, evidence, scores, mastery, confidence, or experience.

TRANSCRIPT:
{transcript}

PYTHON-COMPUTED REPORT DATA:
{json.dumps(report, indent=2, ensure_ascii=False)}

FINAL SKILL GRAPH:
{json.dumps(graph, indent=2, ensure_ascii=False)}
"""


def _render_analysis(entries, llm_analysis, report=None, skill_graph=None):
    report = report or build_skill_report(entries, skill_graph)
    scores = [entry["score"] for entry in entries if isinstance(entry.get("score"), (int, float))]
    main = [entry for entry in entries if not entry["is_followup"]]
    followups = [entry for entry in entries if entry["is_followup"]]
    average = sum(scores) / len(scores) if scores else 0
    breakdown = "\n\n".join(
        f"### Q{i} — {'Introduction' if entry.get('is_intro') else 'Follow-up' if entry['is_followup'] else 'Main'}\n\n"
        f"**Question:** {entry['question']}\n\n"
        f"**Answer:** {entry['answer']}\n\n"
        f"**Focus:** {entry.get('focus', 'Not specified')}\n\n"
        f"**Basis:** {entry.get('basis', 'Not specified')}\n\n"
        f"**Score:** {str(entry['score']) + '/10' if isinstance(entry.get('score'), (int, float)) else 'Not scored'}\n\n"
        f"**Evaluation:** {entry['evaluation']}"
        for i, entry in enumerate(entries, 1)
    ) or "No scored questions were answered."
    skill_rows = "\n".join(
        f"- {item['name']} [{item['skill_id']}]: mastery={item['mastery']:.2f}, confidence={item['confidence']:.2f}, "
        f"status={item['status']}, evidence={item['evidence_count']}, average_score={item['average_score'] if item['average_score'] is not None else 'n/a'}, "
        f"followups={item['followup_count']}, basis={', '.join(item['basis']) or 'n/a'}"
        for item in report["skills"]
    ) or "- No skill-tagged evidence was recorded."
    content = (
        f"# Final Interview Analysis — {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
        f"## Summary\n\n"
        f"- Total questions: {len(entries)} ({len(main)} main, {len(followups)} follow-up)\n"
        f"- Overall score: {average:.1f}/10\n"
        f"- Stopping reason: {report.get('stopping_reason') or 'not recorded'}\n\n"
        f"## Skill-wise mastery\n\n{skill_rows}\n\n"
        f"### Strongest skills\n\n{', '.join(report['strongest_skills']) or 'None identified'}\n\n"
        f"### Weakest or uncertain skills\n\n{', '.join(report['weakest_skills']) or 'None identified'}\n\n"
        f"### Prerequisite observations\n\n" + ("\n".join(f"- {item}" for item in report["prerequisite_observations"]) or "None identified.") + "\n\n"
        f"## Recommendations\n\n" + ("\n".join(f"- {item}" for item in report["recommendations"]) or "None identified.") + "\n\n"
        f"## Narrative assessment\n\n{llm_analysis}\n\n"
        f"## Question-by-question analysis\n\n{breakdown}\n"
    )
    return content


def _prepare_main_questions(questions, skill_graph=None):
    prepared = []
    skill_ids = list((skill_graph or {}).get("skills", {}))
    for item in questions:
        if isinstance(item, dict):
            prepared.append({
                "question": item["question"],
                "focus": item.get("focus", "General role competency"),
                "basis": item.get("basis", "resume_only"),
                "skill_id": item.get("skill_id"),
                "is_followup": False,
                "is_intro": False,
            })
        else:
            prepared.append({
                "question": str(item),
                "focus": "Seeded interview question",
                "basis": "seed",
                "skill_id": None,
                "is_followup": False,
                "is_intro": False,
            })
    if prepared and re.search(r"\b(introduce yourself|tell me about yourself)\b", prepared[0]["question"], re.IGNORECASE):
        prepared[0]["is_intro"] = True
        first_non_intro = 0
        for item in prepared:
            if not item.get("is_intro") and not item.get("skill_id") and skill_ids:
                item["skill_id"] = skill_ids[first_non_intro % len(skill_ids)]
                first_non_intro += 1
        return prepared
    intro = {
        "question": INTRO_QUESTION,
        "focus": "Candidate background and communication",
        "basis": "introduction",
        "skill_id": None,
        "is_followup": False,
        "is_intro": True,
    }
    for index, item in enumerate(prepared):
        if not item.get("skill_id") and skill_ids:
            item["skill_id"] = skill_ids[index % len(skill_ids)]
    return [intro, *prepared]


def start_interview(context: str, seed_questions=None, mode="adaptive", job_description_override=None,
                    job_description_files_override=None, role_name=None, resume_content=None, resume_filename=None,
                    candidate_first_name=None):
    if mode not in INTERVIEW_MODES:
        mode = "adaptive"
    if seed_questions:
        raise ValueError("seed_questions are not supported in adaptive mode.")
    max_questions = INTERVIEW_MODES[mode].get("adaptive_turn_budget") or 12
    if job_description_override is None:
        job_description = load_job_description()
    else:
        job_description = {
            "content": str(job_description_override or "").strip(),
            "files": list(job_description_files_override or []),
            "truncated": False,
        }
    rag_index = (
        build_rag_index(knowledge_content=job_description["content"], resume_content=resume_content, resume_filename=resume_filename)
        if resume_content is not None else
        build_rag_index(knowledge_content=job_description["content"])
        if job_description_override is not None
        else (INTERVIEW_STATE["rag_index"] or build_rag_index(knowledge_content=job_description["content"]))
    )
    if rag_index is None:
        raise LLMServiceUnavailable("model_not_found", "The resume index could not be built. Verify the embedding model is installed, then retry.")
    graph_source = "gemini"
    try:
        skill_graph = extract_skill_graph(rag_index, job_description["content"], context)
        graph_initialized = True
    except Exception as exc:
        logger.warning("skill_graph_initialization_failed provider=gemini reason=%s", type(exc).__name__)
        try:
            skill_graph = extract_skill_graph(rag_index, job_description["content"], context, provider="groq")
            graph_source = "groq"
            graph_initialized = True
        except Exception as fallback_exc:
            logger.warning("skill_graph_initialization_failed provider=groq reason=%s", type(fallback_exc).__name__)
            skill_graph = {"version": GRAPH_VERSION, "skills": {}, "edges": []}
            graph_source = "question_fallback"
            graph_initialized = False

    if not skill_graph.get("skills"):
        # Keep startup useful when graph extraction is unavailable: metadata from
        # the grounded question call is a conservative fallback graph.
        graph_source = "question_fallback"
        graph_initialized = False
        questions = generate_questions_from_resume(rag_index, max_questions, context, job_description=job_description["content"])
        fallback = []
        for index, item in enumerate(questions):
            skill_id = item.get("skill_id") or f"skill_{index + 1}"
            fallback.append({"skill_id": skill_id, "name": item.get("focus", skill_id), "category": "technical", "evidence": item.get("basis", "")})
        from backend.skill_graph import normalize_skill_graph
        skill_graph = normalize_skill_graph({"skills": fallback, "edges": []})

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_dir = Path("interview_logs")
    log_file = log_dir / f"interview_{timestamp}" / "session.json"
    # The introduction is intentionally unscored and does not consume a technical slot.
    intro = {"question": INTRO_QUESTION, "focus": "Candidate background and communication",
             "basis": "introduction", "skill_id": None, "is_followup": False, "is_intro": True}
    # Retained as a read-only compatibility projection for older clients/tests;
    # adaptive progression never consults this list.
    compatibility_questions = _prepare_main_questions(questions, skill_graph) if 'questions' in locals() else [intro]
    refresh_statuses(skill_graph)
    record = _new_record(log_file, role_name=role_name or context, context=context, mode=mode,
                         graph=skill_graph, current_question=intro, graph_source=graph_source)
    _save_record(record, log_file)
    try:
        _interrupt_active_record("replaced")
    except Exception:
        log_file.unlink(missing_ok=True)
        raise
    INTERVIEW_STATE.update({
        "context": context,
        "candidate_question_number": 1,
        "role_name": role_name or context,
        "log": [],
        "intro_entry": None,
        "next_questions": [intro],
        "mode": mode,
        "log_file": str(log_file),
        "session_record": record,
        "main_questions": compatibility_questions,
        "current_main_index": 0,
        "followups_for_current": 0,
        "rag_index": rag_index,
        "job_description": job_description["content"],
        "job_description_files": job_description["files"],
        "skill_graph": skill_graph,
        "graph_initialized": graph_initialized,
        "graph_source": graph_source,
        "graph_version": skill_graph.get("version", GRAPH_VERSION),
        "last_question_audio_ended_at": None,
        "current_skill_id": None,
        "adaptive": True,
        "current_question": intro,
        "question_history": [], "questions_asked": 0, "main_questions_used": 0,
        "followups_used": 0, "question_fingerprints": [], "planner_reason": "introduction",
        "current_difficulty": None, "current_question_type": None, "turn_number": 0,
        "stopping_reason": None,
        "report": None,
        "execution_phase": "waiting_for_introduction",
    })
    first_name = candidate_first_name or extract_candidate_first_name(resume_content)
    welcome_message = f"Welcome, {first_name}! Let's begin." if first_name else "Welcome! Let's begin."
    _log_interview_state("started", reaction=welcome_message)
    return {
        "message": "Interview started.",
        "welcome_message": welcome_message,
        "next_question": intro["question"],
        "skill_graph": skill_graph,
        "graph_initialized": graph_initialized,
        "graph_source": graph_source,
        "current_skill_id": None,
        "current": intro,
        "planner_state": _planner_state(),
    }


def _evaluate(question, answer):
    resume_context = retrieve_evaluation_context(question["question"], answer, INTERVIEW_STATE["rag_index"])
    prompt = f"""You are conducting a technical interview. Evaluate the current answer and return the required JSON.
The reaction is spoken aloud directly to the interviewee. It must be one natural sentence using second-person language such as "you" and "your".
Never call the person "the candidate", "the interviewee", or "the applicant" in the reaction, and do not mention a score or hiring decision there.
The score must be an integer from 0 to 10. The evaluation must be concise and evidence-based.
The next_question must ask for one concrete missing detail in this specific answer, stay on the question's skill, and avoid repeating the original question. It is only used if the interview state requests a follow-up.

ROLE CONTEXT:
{INTERVIEW_STATE['context']}

RESUME CONTEXT:
{resume_context or 'No relevant resume excerpt found.'}

QUESTION:
{question['question']}

QUESTION TYPE:
{'Introduction/warm-up' if question.get('is_intro') else 'Technical'}

QUESTION FOCUS:
{question.get('focus', 'Not specified')}

QUESTION BASIS:
{question.get('basis', 'Not specified')}

CANDIDATE ANSWER:
{answer}
"""
    return complete_json(prompt, TURN_SCHEMA, {"temperature": 0.3, "max_tokens": 450, "schema_name": "interview_turn"})


def _candidate_facing_reaction(reaction: str, score: int) -> str:
    """Keep evaluator-style third-person wording out of the spoken response."""
    text = reaction.strip()
    for subject in ("candidate", "interviewee", "applicant"):
        text = re.sub(
            rf"\bthe {subject}['’]s (answer|response)\b",
            lambda match: f"Your {match.group(1).lower()}",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(rf"\bthe {subject}\b", "you", text, flags=re.IGNORECASE)
        text = re.sub(rf"\b{subject}['’]s\b", "your", text, flags=re.IGNORECASE)

    # Do not risk speaking awkward internal-review language if an unexpected
    # form remains after normalizing the common cases above.
    if re.search(r"\b(candidate|interviewee|applicant)\b", text, re.IGNORECASE):
        if score >= 7:
            return "Good answer—you covered the main points clearly."
        if score >= 4:
            return "You covered part of the answer; let’s explore it further."
        return "Thank you—let’s clarify a few important points."
    return text


def submit_answer(candidate_answer: str, *, latency_ms=None, input_mode="text"):
    if INTERVIEW_STATE.get("execution_phase") == "completed":
        return _closing_response(skill_graph=INTERVIEW_STATE.get("skill_graph"), report=INTERVIEW_STATE.get("report"))
    if INTERVIEW_STATE.get("adaptive") and INTERVIEW_STATE.get("current_question") is INTERVIEW_STATE.get("next_questions", [None])[0]:
        return _submit_adaptive_answer(candidate_answer, latency_ms=latency_ms, input_mode=input_mode)
    if not INTERVIEW_STATE["next_questions"]:
        return _closing_response()

    # Peek only. Nothing below is committed until all required provider calls finish.
    current = INTERVIEW_STATE["next_questions"][0]
    result = _evaluate(current, candidate_answer)
    mode_config = INTERVIEW_MODES[INTERVIEW_STATE["mode"]]
    short_or_weak = len(candidate_answer.split()) < 8 or result["score"] < 5
    can_follow = mode_config["allow_followups"] and INTERVIEW_STATE["followups_for_current"] < mode_config.get("max_followups_per_skill", 0)

    if short_or_weak and can_follow:
        spec = {"skill_id": current.get("skill_id"), "difficulty": current.get("difficulty", 2),
                "question_type": current.get("question_type", "explanation"),
                "planner_reason": "weak_answer_remediation", "is_followup": True}
        next_question = _generate_followup_question(spec, result, current, candidate_answer) | {
            "focus": current.get("focus", "Not specified"), "basis": current.get("basis", "Not specified"), "is_intro": False}
        next_main_index = INTERVIEW_STATE["current_main_index"]
        next_followup_count = INTERVIEW_STATE["followups_for_current"] + 1
    else:
        next_main_index = INTERVIEW_STATE["current_main_index"] + 1
        next_followup_count = 0
        next_question = INTERVIEW_STATE["main_questions"][next_main_index] if next_main_index < len(INTERVIEW_STATE["main_questions"]) else None

    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "question": current["question"],
        "answer": candidate_answer,
        "evaluation": result["evaluation"],
        "score": result["score"],
        "is_followup": current.get("is_followup", False),
        "is_intro": current.get("is_intro", False),
        "focus": current.get("focus", "Not specified"),
        "basis": current.get("basis", "Not specified"),
        "follow_up_question": next_question["question"] if next_question and next_question.get("is_followup") else None,
    }
    prospective_log = [*INTERVIEW_STATE["log"], entry]

    followup_dependency_count = INTERVIEW_STATE["followups_for_current"] + (1 if short_or_weak and can_follow else 0)
    signals = extract_behavior_signals(
        candidate_answer,
        latency_ms=latency_ms,
        followup_count=followup_dependency_count,
        input_mode=input_mode,
    )
    entry["signals"] = signals
    entry["skill_id"] = current.get("skill_id")
    prospective_graph = deepcopy(INTERVIEW_STATE.get("skill_graph") or {"version": GRAPH_VERSION, "skills": {}, "edges": []})
    mastery_update = None
    propagation_updates = []
    skill_id = current.get("skill_id")
    if skill_id and skill_id in prospective_graph.get("skills", {}):
        mastery_update = update_mastery(prospective_graph["skills"][skill_id], result["score"], signals)
        propagation_updates = propagate_mastery(prospective_graph, skill_id, mastery_update["new_mastery"] - mastery_update["old_mastery"])
    entry["mastery_update"] = mastery_update
    entry["propagation_updates"] = propagation_updates

    # The report is synchronous and required on the last turn. Failure leaves the turn retryable.
    final_report = None
    report_data = None
    if next_question is None:
        report_data = build_skill_report(prospective_log, prospective_graph, stopping_reason="mode_safety_limit")
        final_report = complete_text(_report_prompt(prospective_log, report_data, prospective_graph), {"temperature": 0.4, "max_tokens": 1200})

    record = _prospective_record(
        prospective_log, prospective_graph, next_question,
        intro_entry=INTERVIEW_STATE.get("intro_entry"),
        status="completed" if next_question is None else "in_progress",
        stopping_reason="mode_safety_limit" if next_question is None else None,
        analysis_file=_analysis_path(INTERVIEW_STATE["log_file"]) if next_question is None else None,
    )
    if next_question is None:
        _save_completed_artifacts(record, INTERVIEW_STATE["log_file"], prospective_log,
                                  final_report, report_data, prospective_graph)
    else:
        _save_record(record, INTERVIEW_STATE["log_file"])

    INTERVIEW_STATE["log"].append(entry)
    scored_entries = [item for item in INTERVIEW_STATE["log"] if not item.get("is_intro")]
    INTERVIEW_STATE["questions_asked"] = len(scored_entries)
    INTERVIEW_STATE["main_questions_used"] = sum(not item.get("is_followup", False) for item in scored_entries)
    INTERVIEW_STATE["followups_used"] = sum(bool(item.get("is_followup")) for item in scored_entries)
    INTERVIEW_STATE["current_main_index"] = next_main_index
    INTERVIEW_STATE["followups_for_current"] = next_followup_count
    INTERVIEW_STATE["next_questions"] = [next_question] if next_question else []
    INTERVIEW_STATE["skill_graph"] = prospective_graph
    INTERVIEW_STATE["current_skill_id"] = next_question.get("skill_id") if next_question else None
    INTERVIEW_STATE["session_record"] = record

    if next_question is None:
        INTERVIEW_STATE["report"] = {**(report_data or {}), "narrative": final_report}
        return _closing_response(
            skill_id=skill_id,
            signals=signals,
            mastery_update=mastery_update,
            propagation_updates=propagation_updates,
            skill_graph=prospective_graph,
            report=INTERVIEW_STATE["report"],
            evaluation=result,
        )

    candidate_reaction = _candidate_facing_reaction(result["reaction"], result["score"])
    is_follow_up = next_question.get("is_followup", False)
    _log_interview_state(
        "ongoing",
        reaction=candidate_reaction,
        follow_up_required=is_follow_up,
        follow_up_question=next_question["question"] if is_follow_up else None,
    )
    return {
        "reaction": candidate_reaction,
        "next_question": next_question["question"],
        "evaluation": result["evaluation"],
        "score": result["score"],
        "analysis_score": result["score"],
        "status": "ongoing",
        "skill_id": skill_id,
        "signals": signals,
        "mastery_update": mastery_update,
        "propagation_updates": propagation_updates,
        "skill_graph": prospective_graph,
    }


def _submit_adaptive_answer(candidate_answer: str, *, latency_ms=None, input_mode="text"):
    previous_phase = INTERVIEW_STATE.get("execution_phase", "idle")
    previous_log_length = len(INTERVIEW_STATE["log"])
    try:
        return _submit_adaptive_answer_impl(candidate_answer, latency_ms=latency_ms, input_mode=input_mode)
    except Exception:
        # Providers run before committing the answer. Restore the observable
        # phase on failure so the same question remains available for retry.
        if len(INTERVIEW_STATE["log"]) == previous_log_length:
            INTERVIEW_STATE["execution_phase"] = previous_phase
        raise


def _submit_adaptive_answer_impl(candidate_answer: str, *, latency_ms=None, input_mode="text"):
    if not INTERVIEW_STATE["next_questions"]:
        return _closing_response(skill_graph=INTERVIEW_STATE.get("skill_graph"))
    current = INTERVIEW_STATE["next_questions"][0]

    # Introduction is deliberately unscored and only opens the technical loop.
    if current.get("is_intro"):
        previous_phase = INTERVIEW_STATE.get("execution_phase", "idle")
        INTERVIEW_STATE["execution_phase"] = "selecting_skill"
        spec = _adaptive_next_spec()
        intro_entry = {
            "timestamp": datetime.datetime.now().isoformat(), "question": current["question"],
            "answer": candidate_answer, "evaluation": "Introduction; not scored.", "score": None,
            "is_followup": False, "is_intro": True, "skill_id": None,
            "planner_reason": "introduction", "follow_up_question": None,
        }
        if not spec:
            graph = INTERVIEW_STATE["skill_graph"]
            report_data = build_skill_report([], graph, stopping_reason="all_skills_mastered")
            narrative = complete_text(_report_prompt([], report_data, graph), {"temperature": 0.4, "max_tokens": 1200})
            record = _prospective_record([], graph, None, intro_entry=intro_entry, status="completed",
                                         stopping_reason="all_skills_mastered",
                                         analysis_file=_analysis_path(INTERVIEW_STATE["log_file"]))
            _save_completed_artifacts(record, INTERVIEW_STATE["log_file"], [], narrative, report_data, graph)
            INTERVIEW_STATE["next_questions"] = []
            INTERVIEW_STATE["current_question"] = None
            INTERVIEW_STATE["stopping_reason"] = "all_skills_mastered"
            INTERVIEW_STATE["intro_entry"] = intro_entry
            INTERVIEW_STATE["session_record"] = record
            INTERVIEW_STATE["report"] = {**report_data, "narrative": narrative}
            return _closing_response(skill_graph=INTERVIEW_STATE["skill_graph"])
        INTERVIEW_STATE["execution_phase"] = "generating_question"
        try:
            question = _generate_adaptive_question(spec)
        except Exception:
            INTERVIEW_STATE["execution_phase"] = previous_phase
            raise
        record = _prospective_record([], INTERVIEW_STATE["skill_graph"], question, intro_entry=intro_entry)
        _save_record(record, INTERVIEW_STATE["log_file"])
        INTERVIEW_STATE["next_questions"] = [question]
        INTERVIEW_STATE["current_question"] = question
        INTERVIEW_STATE["current_skill_id"] = question["skill_id"]
        INTERVIEW_STATE["planner_reason"] = question["planner_reason"]
        INTERVIEW_STATE["current_difficulty"] = question["difficulty"]
        INTERVIEW_STATE["current_question_type"] = question["question_type"]
        INTERVIEW_STATE["intro_entry"] = intro_entry
        INTERVIEW_STATE["session_record"] = record
        return {"reaction": "Thanks for the introduction. Let’s begin with a technical question.",
                "next_question": question["question"], "status": "ongoing", "skill_id": question["skill_id"],
                "current": question, "planner_state": _planner_state(), "skill_graph": INTERVIEW_STATE["skill_graph"]}

    previous_phase = INTERVIEW_STATE.get("execution_phase", "idle")
    INTERVIEW_STATE["execution_phase"] = "evaluating_answer"
    try:
        result = _evaluate(current, candidate_answer)
    except Exception:
        INTERVIEW_STATE["execution_phase"] = previous_phase
        raise
    INTERVIEW_STATE["execution_phase"] = "updating_mastery"
    graph = deepcopy(INTERVIEW_STATE["skill_graph"])
    node = graph["skills"][current["skill_id"]]
    signals = extract_behavior_signals(candidate_answer, latency_ms=latency_ms,
                                       followup_count=INTERVIEW_STATE.get("followups_used", 0), input_mode=input_mode)
    mastery_update = update_mastery(node, result["score"], signals)
    propagation_updates = propagate_mastery(graph, current["skill_id"], mastery_update["new_mastery"] - mastery_update["old_mastery"])
    node["questions_asked"] = int(node.get("questions_asked", 0)) + 1
    node["last_question_type"] = current.get("question_type")
    node["last_difficulty"] = current.get("difficulty")
    node["last_tested_turn"] = INTERVIEW_STATE.get("turn_number", 0) + 1
    if current.get("is_followup"):
        node["followup_count"] = int(node.get("followup_count", 0)) + 1
    refresh_statuses(graph)

    main_used = INTERVIEW_STATE["main_questions_used"] + (0 if current.get("is_followup") else 1)
    followups_used = INTERVIEW_STATE["followups_used"] + (1 if current.get("is_followup") else 0)
    config = INTERVIEW_MODES[INTERVIEW_STATE["mode"]]
    per_skill = config.get("max_followups_per_skill", 0)
    quick = INTERVIEW_STATE["mode"] == "quick"
    bounded = not config["fully_adaptive"]
    want_followup = result["score"] <= (5 if quick else 3) or len(candidate_answer.split()) < 8
    evaluated_turns = INTERVIEW_STATE["questions_asked"] + 1
    within_budget = not bounded or evaluated_turns < config["adaptive_turn_budget"]
    allow_followup = (want_followup and config["allow_followups"]
                      and int(node.get("followup_count", 0)) < per_skill
                      and within_budget
                      and (bounded or not all_mastered(graph))
                      and (not bounded or (followups_used < config["max_followups_total"]
                                           and main_used <= config["main_question_budget"]))
                      and (not quick or not current.get("is_followup")))
    if allow_followup:
        spec = next_spec(node, score=result["score"], previous_type=current.get("question_type"),
                         previous_difficulty=current.get("difficulty"), is_followup=True)
        spec["planner_reason"] = "weak_answer_remediation"
        spec["followup_depth"] = int(node.get("followup_count", 0)) + 1
    else:
        INTERVIEW_STATE["execution_phase"] = "selecting_skill"
        prospective_done = all_mastered(graph)
        main_budget_reached = bounded and main_used >= config["main_question_budget"]
        if (config["fully_adaptive"] and prospective_done) or main_budget_reached or (bounded and evaluated_turns >= config["adaptive_turn_budget"]):
            spec = None
        else:
            spec = _adaptive_next_spec(score=result["score"], current=current, graph=graph)
            # If remediation depth for this skill is exhausted, continue with
            # the normal graph selection instead of producing another follow-up.
            if spec and spec.get("is_followup"):
                skill_id, reason = select_next_skill(graph)
                spec = next_spec(graph["skills"][skill_id], previous_type=current.get("question_type"),
                                 previous_difficulty=current.get("difficulty"), reason=reason) if skill_id else None

    # All provider work is complete before any state is committed.
    if spec:
        INTERVIEW_STATE["execution_phase"] = "generating_question"
    try:
        next_question = (_generate_followup_question(spec, result, current, candidate_answer, graph=graph)
                         if spec and spec.get("is_followup") else
                         _generate_adaptive_question(spec, graph=graph) if spec else None)
    except Exception:
        INTERVIEW_STATE["execution_phase"] = previous_phase
        raise
    entry = {
        "timestamp": datetime.datetime.now().isoformat(), "question": current["question"],
        "answer": candidate_answer, "evaluation": result["evaluation"], "score": result["score"],
        "is_followup": current.get("is_followup", False), "is_intro": False,
        "skill_id": current["skill_id"], "signals": signals, "mastery_update": mastery_update,
        "propagation_updates": propagation_updates, "question_spec": {key: current.get(key) for key in ("skill_id", "difficulty", "question_type", "planner_reason", "is_followup")},
        "planner_reason": current.get("planner_reason"),
        "follow_up_question": next_question["question"] if next_question and next_question.get("is_followup") else None,
        "next_question_spec": {key: next_question.get(key) for key in ("skill_id", "difficulty", "question_type", "planner_reason", "is_followup")} if next_question else None,
        "stopping_reason": ("all_skills_mastered" if config["fully_adaptive"] and all_mastered(graph) else "mode_safety_limit") if not next_question else None,
    }
    final_report = None
    report_data = build_skill_report([*INTERVIEW_STATE["log"], entry], graph,
                                     stopping_reason=("all_skills_mastered" if config["fully_adaptive"] and all_mastered(graph) else "mode_safety_limit")) if not next_question else None
    if next_question is None:
        final_report = complete_text(_report_prompt([*INTERVIEW_STATE["log"], entry], report_data, graph), {"temperature": 0.4, "max_tokens": 1200})
    prospective_log = [*INTERVIEW_STATE["log"], entry]
    record = _prospective_record(
        prospective_log, graph, next_question, intro_entry=INTERVIEW_STATE.get("intro_entry"),
        status="completed" if next_question is None else "in_progress",
        stopping_reason=entry["stopping_reason"] if next_question is None else None,
        analysis_file=_analysis_path(INTERVIEW_STATE["log_file"]) if next_question is None else None,
    )
    if next_question is None:
        _save_completed_artifacts(record, INTERVIEW_STATE["log_file"], prospective_log,
                                  final_report, report_data, graph)
    else:
        _save_record(record, INTERVIEW_STATE["log_file"])
    INTERVIEW_STATE["log"].append(entry)
    INTERVIEW_STATE["question_history"].append({**current, "fingerprint": current["fingerprint"]})
    INTERVIEW_STATE["question_fingerprints"].append(current["fingerprint"])
    INTERVIEW_STATE["skill_graph"] = graph
    INTERVIEW_STATE["session_record"] = record
    INTERVIEW_STATE["questions_asked"] += 1
    INTERVIEW_STATE["main_questions_used"] = main_used
    INTERVIEW_STATE["followups_used"] = followups_used
    INTERVIEW_STATE["followups_for_current"] = next_question.get("followup_depth", 0) if next_question and next_question.get("is_followup") else 0
    INTERVIEW_STATE["turn_number"] += 1
    INTERVIEW_STATE["next_questions"] = [next_question] if next_question else []
    INTERVIEW_STATE["current_question"] = next_question
    INTERVIEW_STATE["current_skill_id"] = next_question.get("skill_id") if next_question else None
    INTERVIEW_STATE["planner_reason"] = next_question.get("planner_reason") if next_question else None
    INTERVIEW_STATE["current_difficulty"] = next_question.get("difficulty") if next_question else None
    INTERVIEW_STATE["current_question_type"] = next_question.get("question_type") if next_question else None
    if next_question is None:
        INTERVIEW_STATE["stopping_reason"] = "all_skills_mastered" if config["fully_adaptive"] and all_mastered(graph) else "mode_safety_limit"
        INTERVIEW_STATE["execution_phase"] = "completed"
        INTERVIEW_STATE["report"] = {**report_data, "narrative": final_report}
    else:
        INTERVIEW_STATE["execution_phase"] = "waiting_for_answer"
    candidate_reaction = _candidate_facing_reaction(result["reaction"], result["score"])
    if not next_question:
        return _closing_response(skill_id=current["skill_id"], signals=signals, mastery_update=mastery_update,
                                 propagation_updates=propagation_updates, skill_graph=graph, report=INTERVIEW_STATE["report"], evaluation=result)
    _log_interview_state("ongoing", reaction=candidate_reaction, follow_up_required=next_question.get("is_followup", False),
                         follow_up_question=next_question["question"] if next_question.get("is_followup") else None)
    return {"reaction": candidate_reaction, "next_question": next_question["question"], "evaluation": result["evaluation"],
            "score": result["score"], "analysis_score": result["score"], "status": "ongoing", "skill_id": current["skill_id"],
            "evaluated_skill_id": current["skill_id"], "updated_skill": graph["skills"][current["skill_id"]],
            "signals": signals, "mastery_update": mastery_update, "propagation_updates": propagation_updates,
            "current": next_question, "planner_reason": next_question["planner_reason"], "planner_state": _planner_state(),
            "skill_graph": graph}


INTERVIEW_SENDOFF = (
    "Thank you for sharing your experience today. I appreciate the thought you put into your answers. "
    "Your interview is complete, and your report is ready for review. "
    "Take care, and best of luck with what comes next."
)


def _closing_response(*, skill_id=None, signals=None, mastery_update=None, propagation_updates=None, skill_graph=None, report=None, evaluation=None):
    INTERVIEW_STATE["execution_phase"] = "completed"
    message = INTERVIEW_SENDOFF
    report = report or INTERVIEW_STATE.get("report") or build_skill_report(INTERVIEW_STATE.get("log", []), skill_graph)
    INTERVIEW_STATE["report"] = report
    _log_interview_state("completed", reaction=message)
    return {
        "reaction": _candidate_facing_reaction(evaluation["reaction"], evaluation["score"]) if evaluation else message,
        "response": message,
        "message": message,
        "closing_message": message,
        "next_question": "",
        "evaluation": evaluation["evaluation"] if evaluation else "Interview completed.",
        "score": evaluation["score"] if evaluation else None,
        "analysis_score": evaluation["score"] if evaluation else None,
        "status": "completed",
        "skill_id": skill_id,
        "signals": signals,
        "mastery_update": mastery_update,
        "propagation_updates": propagation_updates or [],
        "skill_graph": skill_graph or INTERVIEW_STATE.get("skill_graph", {"skills": {}, "edges": []}),
        "report": report,
    }


def stop_interview():
    """Finalize the current session without evaluating another answer."""
    if INTERVIEW_STATE.get("execution_phase") == "completed":
        return _closing_response(skill_graph=INTERVIEW_STATE.get("skill_graph"), report=INTERVIEW_STATE.get("report"))
    graph = INTERVIEW_STATE.get("skill_graph") or {"skills": {}, "edges": []}
    entries = INTERVIEW_STATE.get("log", [])
    report_data = build_skill_report(entries, graph, stopping_reason="manual_stop")
    narrative = complete_text(_report_prompt(entries, report_data, graph), {"temperature": 0.4, "max_tokens": 1200})
    record = None
    if INTERVIEW_STATE.get("log_file"):
        record = _prospective_record(entries, graph, None, intro_entry=INTERVIEW_STATE.get("intro_entry"),
                                     status="completed", stopping_reason="manual_stop",
                                     analysis_file=_analysis_path(INTERVIEW_STATE["log_file"]))
        _save_completed_artifacts(record, INTERVIEW_STATE["log_file"], entries,
                                  narrative, report_data, graph)
    INTERVIEW_STATE["next_questions"] = []
    INTERVIEW_STATE["current_question"] = None
    INTERVIEW_STATE["current_skill_id"] = None
    INTERVIEW_STATE["stopping_reason"] = "manual_stop"
    INTERVIEW_STATE["execution_phase"] = "completed"
    INTERVIEW_STATE["report"] = {**report_data, "narrative": narrative}
    if record:
        INTERVIEW_STATE["session_record"] = record
    return _closing_response(skill_graph=graph, report=INTERVIEW_STATE["report"])


def get_full_log():
    intro = INTERVIEW_STATE.get("intro_entry")
    return ([intro] if intro else []) + INTERVIEW_STATE["log"]
