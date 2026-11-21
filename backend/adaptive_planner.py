"""Deterministic skill-question planning for adaptive interviews."""

from __future__ import annotations

import hashlib
import math


QUESTION_TYPES = ("explanation", "scenario", "debugging", "tradeoff", "application")


def active_nodes(graph):
    return list((graph or {}).get("skills", {}).values())


def is_mastered(node):
    return node.get("mastery", 0.0) >= 0.85 and node.get("confidence", 0.0) >= 0.85


def status_for(node):
    mastery, confidence = float(node.get("mastery", 0.0)), float(node.get("confidence", 0.0))
    if node.get("evidence_count", 0) == 0:
        return "untested"
    if mastery < 0.4 or confidence < 0.35:
        return "weak"
    if mastery < 0.7 or confidence < 0.7:
        return "developing"
    if is_mastered(node):
        return "mastered"
    return "strong"


def refresh_statuses(graph):
    for node in (graph or {}).get("skills", {}).values():
        node["status"] = status_for(node)
    return graph


def _prerequisites(graph, skill_id):
    ids = [e["source"] for e in graph.get("edges", [])
           if e.get("relation") == "prerequisite" and e.get("target") == skill_id]
    order = list(graph.get("skills", {}))
    return sorted(ids, key=lambda value: order.index(value) if value in order else len(order))


def select_next_skill(graph):
    """Return (skill_id, reason), using graph insertion order as the final tie-break."""
    refresh_statuses(graph)
    skills = graph.get("skills", {})
    order = list(skills)
    candidates = [n for n in skills.values() if not is_mastered(n)]
    if not candidates:
        return None, "all_skills_mastered"

    def key(node):
        return (0 if node.get("evidence_count", 0) == 0 else 1,
                float(node.get("confidence", 0.0)), float(node.get("mastery", 0.0)),
                int(node.get("questions_asked", 0)), order.index(node["skill_id"]))

    selected = min(candidates, key=key)
    # A dependent is not allowed to leapfrog an available weak/untested prerequisite.
    for prerequisite in _prerequisites(graph, selected["skill_id"]):
        node = skills.get(prerequisite)
        if node and not is_mastered(node):
            return prerequisite, "prerequisite_first"
    return selected["skill_id"], "untested_skill" if selected.get("evidence_count", 0) == 0 else "lowest_confidence"


def select_next_untested_skill(graph):
    """Select a new skill once its direct prerequisites have each been tested."""
    refresh_statuses(graph)
    skills = graph.get("skills", {})
    for skill_id, node in skills.items():
        if node.get("evidence_count", 0):
            continue
        if all(skills[parent].get("evidence_count", 0) for parent in _prerequisites(graph, skill_id)):
            return skill_id, "untested_skill"
    return None, "no_reachable_untested_skill"


def next_spec(node, *, score=None, previous_type=None, previous_difficulty=None,
              is_followup=False, reason=None):
    if score is None or node.get("evidence_count", 0) == 0:
        difficulty, question_type, planner_reason = 2, "explanation", reason or "untested_skill"
    elif score < 4:
        difficulty, question_type, planner_reason = max(1, int(previous_difficulty or 2) - 1), "explanation", "weak_answer_remediation"
    elif score <= 6:
        difficulty = int(previous_difficulty or 2)
        choices = [item for item in QUESTION_TYPES if item != previous_type]
        question_type, planner_reason = choices[0], "developing_retest"
    elif score <= 8:
        difficulty, question_type, planner_reason = min(5, int(previous_difficulty or 2) + 1), previous_type or "application", "advance_difficulty"
    else:
        difficulty, question_type, planner_reason = min(5, int(previous_difficulty or 2) + 1), "tradeoff", "deepen_mastery"
    return {"skill_id": node["skill_id"], "difficulty": difficulty,
            "question_type": question_type, "planner_reason": planner_reason,
            "is_followup": bool(is_followup)}


def fingerprint(question):
    normalized = " ".join(str(question).lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def all_mastered(graph):
    return bool(graph.get("skills")) and all(is_mastered(node) for node in graph.get("skills", {}).values())


def confidence_for(evidence_count):
    return max(0.0, min(1.0, 1.0 - math.exp(-0.7 * max(0, int(evidence_count)))))
