"""Small, serializable skill graph and behavior-aware mastery utilities."""

from __future__ import annotations

from copy import deepcopy
from numbers import Real

from backend.adaptive_planner import confidence_for


GRAPH_VERSION = "phase-3"


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def normalize_skill_graph(payload: dict) -> dict:
    """Validate and normalize model output into the session graph shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("skills"), list) or not isinstance(payload.get("edges"), list):
        raise ValueError("Skill graph must contain skills and edges arrays.")

    skills = {}
    for raw in payload["skills"]:
        if not isinstance(raw, dict):
            raise ValueError("Each skill must be an object.")
        skill_id = str(raw.get("skill_id", "")).strip().lower().replace(" ", "_")
        name = str(raw.get("name", "")).strip()
        if not skill_id or not name or skill_id in skills:
            raise ValueError("Skill IDs must be non-empty and unique.")
        skills[skill_id] = {
            "skill_id": skill_id,
            "name": name,
            "category": str(raw.get("category", "technical")).strip() or "technical",
            "evidence": str(raw.get("evidence", "")).strip(),
            "mastery": 0.5,
            "confidence": 0.0,
            "evidence_count": 0,
            "last_score": None,
            "signals": {},
            "questions_asked": 0,
            "followup_count": 0,
            "last_question_type": None,
            "last_difficulty": None,
            "last_tested_turn": None,
            "status": "untested",
        }

    edges = []
    seen = set()
    for raw in payload["edges"]:
        if not isinstance(raw, dict):
            raise ValueError("Each graph edge must be an object.")
        source = str(raw.get("source", "")).strip().lower().replace(" ", "_")
        target = str(raw.get("target", "")).strip().lower().replace(" ", "_")
        relation = str(raw.get("relation", "prerequisite")).strip().lower()
        if relation not in {"prerequisite", "related"}:
            raise ValueError("Unsupported graph relationship.")
        if source == target or source not in skills or target not in skills:
            raise ValueError("Graph edges must reference distinct known skills.")
        key = (source, target, relation)
        if key in seen:
            continue
        seen.add(key)
        edges.append({"source": source, "target": target, "relation": relation, "weight": _clamp(float(raw.get("weight", 0.5)))})

    graph = {"version": GRAPH_VERSION, "skills": skills, "edges": edges}
    _reject_prerequisite_cycles(graph)
    return graph


def _reject_prerequisite_cycles(graph: dict) -> None:
    adjacency = {}
    for edge in graph["edges"]:
        if edge["relation"] == "prerequisite":
            adjacency.setdefault(edge["source"], []).append(edge["target"])

    visiting, visited = set(), set()

    def visit(node):
        if node in visiting:
            raise ValueError("Skill graph contains a prerequisite cycle.")
        if node in visited:
            return
        visiting.add(node)
        for child in adjacency.get(node, []):
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in graph["skills"]:
        visit(node)


def extract_behavior_signals(answer: str, *, latency_ms=None, followup_count=0, input_mode="text") -> dict:
    latency = None
    if isinstance(latency_ms, Real) and not isinstance(latency_ms, bool) and latency_ms >= 0:
        latency = round(float(latency_ms), 2)
    words = len(answer.split())
    return {
        "latency_ms": latency,
        "answer_words": words,
        "answer_characters": len(answer),
        "followup_count": max(0, int(followup_count)),
        "input_mode": input_mode if input_mode in {"text", "voice"} else "text",
    }


def update_mastery(node: dict, score: int, signals: dict) -> dict:
    """Apply a small, bounded behavior penalty and return an audit record."""
    old_mastery = _clamp(node.get("mastery", 0.5))
    score_signal = _clamp(float(score) / 10.0)

    latency = signals.get("latency_ms")
    latency_penalty = _clamp((float(latency) - 3000.0) / 20000.0, 0.0, 0.06) if latency is not None else 0.0
    followup_penalty = _clamp(float(signals.get("followup_count", 0)) * 0.04, 0.0, 0.08)
    short_answer_penalty = 0.04 if signals.get("answer_words", 0) < 8 else 0.0
    penalty = _clamp(latency_penalty + followup_penalty + short_answer_penalty, 0.0, 0.15)
    fused_score = _clamp(score_signal - penalty)
    new_mastery = _clamp(0.6 * fused_score + 0.4 * old_mastery)

    node.update({
        "mastery": round(new_mastery, 4),
        "evidence_count": int(node.get("evidence_count", 0)) + 1,
        "last_score": int(score),
        "signals": deepcopy(signals),
    })
    node["confidence"] = round(confidence_for(node["evidence_count"]), 4)
    return {
        "skill_id": node["skill_id"],
        "old_mastery": round(old_mastery, 4),
        "new_mastery": node["mastery"],
        "score_signal": round(score_signal, 4),
        "behavior_penalty": round(penalty, 4),
        "fused_score": round(fused_score, 4),
    }


def propagate_mastery(graph: dict, skill_id: str, delta: float) -> list[dict]:
    updates = []
    for edge in graph["edges"]:
        if edge["relation"] == "prerequisite" and edge["target"] == skill_id:
            related_id, factor = edge["source"], 0.20
        elif edge["relation"] == "related" and edge["source"] == skill_id:
            related_id, factor = edge["target"], 0.10
        else:
            continue
        node = graph["skills"][related_id]
        before = _clamp(node.get("mastery", 0.5))
        applied = delta * edge["weight"] * factor
        node["mastery"] = round(_clamp(before + applied), 4)
        updates.append({"skill_id": related_id, "relation": edge["relation"], "old_mastery": round(before, 4), "new_mastery": node["mastery"], "delta": round(applied, 4)})
    return updates
