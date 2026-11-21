# Adaptive Skill Graph Interviewer — Main Implementation Plan

## Architecture decision

Use standard Python dictionaries and dataclasses in memory for the initial Skill Graph implementation rather than adding NetworkX immediately.

The graph is small, session-scoped, and has simple directed prerequisite edges. A lightweight model keeps the Flask application dependency-free, makes serialization to interview logs straightforward, and lets the mastery and propagation rules be tested independently. NetworkX can be introduced later if graph visualization, complex traversal, cycle analysis, or persistence requirements justify it.

Suggested shape:

```python
SkillGraph = {
    "skills": {
        "skill_id": {
            "name": "CAD concepts",
            "mastery": 0.5,
            "confidence": 0.5,
            "evidence_count": 0,
            "last_score": None,
            "signals": {},
        }
    },
    "edges": [
        {"source": "mechanical_fundamentals", "target": "cad_concepts", "weight": 0.7}
    ],
}
```

Use a consistent edge direction: `source` is the prerequisite and `target` is the dependent skill. Validate skill IDs, reject self-edges, and guard against duplicate or cyclic relationships when initializing the graph.

## Phase 1 — Skill Graph Initialization

1. Extend `INTERVIEW_STATE` with a session-scoped `skill_graph`, initialization metadata, and the active skill/question state.
2. Reuse the existing RAG pipeline in `backend/analysis_module.py` to retrieve resume material.
3. Add a strict structured-output LLM prompt that extracts a specific, bounded list of technical skills from the resume. Each skill should include:
   - stable identifier;
   - display name;
   - short evidence excerpt or resume basis;
   - initial mastery/confidence defaults.
4. Add a second strict structured-output prompt that proposes prerequisite edges between extracted skills. The prompt must not invent skills absent from the extracted list.
5. Normalize and validate the result before storing it in the session. If no usable skills are extracted, use a small role-context fallback set and mark it as fallback-derived.
6. Run graph initialization inside `start_interview()` after the resume/JD context is available and before the first adaptive question is selected.
7. Persist the initial graph snapshot in the interview JSON log so the final report can explain how mastery changed.

## Phase 2 — Signal Extraction and Mastery Updates

### Behavior Signal Extractor

For every submitted answer, record:

- response latency in milliseconds, measured from question presentation/request completion to answer submission;
- answer length in words and characters;
- whether the answer required a follow-up;
- follow-up count for the skill/question;
- correctness score from the LLM;
- the skill node being assessed.

Keep raw interaction data in the turn log, while storing only the aggregate signal summary on the corresponding skill node. Handle missing client timestamps safely by recording an explicit `unknown` or server-measured fallback.

### Mastery update

Create a pure function such as:

```python
update_mastery(node, score, signals) -> update_result
```

Recommended behavior:

1. Normalize the 0–10 correctness score to `[0.0, 1.0]`.
2. Convert behavioral signals into bounded modifiers rather than allowing them to overwhelm correctness:
   - concise, sufficiently detailed answers: small positive modifier;
   - very short answers or follow-up dependency: negative modifier;
   - unusually high latency: small uncertainty penalty, not an automatic correctness penalty;
   - repeated evidence: reduce update size as confidence increases.
3. Combine the correctness estimate and behavior modifier with a bounded exponential moving average or Bayesian-style evidence update.
4. Update the tested node’s mastery, confidence, evidence count, last score, and signal summary.
5. Return an audit object containing old mastery, new mastery, score contribution, behavioral contribution, and reason codes.

### Graph propagation

After updating the tested node, propagate a smaller update through directly related nodes:

- prerequisite nodes receive a reduced update, for example `direct_delta * edge_weight * 0.20`;
- dependent nodes receive uncertainty changes rather than a strong mastery change, for example `direct_delta * edge_weight * 0.10`;
- clamp mastery and confidence to `[0.0, 1.0]`;
- use a visited set and a maximum propagation depth to prevent cycles or runaway updates.

Only propagate from the current evidence once per turn. Store propagation records in the turn metadata for reportability.

## Phase 3 — Adaptive Question Selection

Replace the current fixed-question progression with an adaptive loop while retaining a compatibility path for seeded questions and the existing API.

1. Represent each generated question as structured data containing:
   - question text;
   - `skill_id`;
   - focus and evidence basis;
   - difficulty;
   - whether it is a follow-up.
2. Select the next active node using a priority score based on:
   - low mastery;
   - low confidence/high uncertainty;
   - importance of the skill in the job description;
   - prerequisite readiness;
   - time since the node was last tested.
3. Ask the LLM for a question explicitly tagged to the selected skill ID. Validate that the returned skill ID is one of the active graph nodes.
4. Evaluate the answer, extract behavior signals, update the tested node, propagate graph effects, and then select the next node.
5. Prevent repeated questions for the same node unless the selector identifies unresolved uncertainty or a follow-up is justified.
6. Stop the interview when either condition is true:
   - the configured maximum question count is reached; or
   - all active/required nodes meet the configured mastery and confidence thresholds.
7. Keep `quick`, `standard`, and `thorough` as compatibility aliases for maximum-question configurations, but make adaptive node selection the source of truth within each limit.

## Phase 4 — Skill-wise Reporting

Redesign `_report_prompt()` and `_write_analysis()` so `interview_logs/analysis.txt` contains:

1. overall interview summary;
2. skill-wise mastery table with:
   - skill name;
   - final mastery;
   - confidence;
   - number of evidence turns;
   - average correctness score;
   - behavior-signal summary;
   - resume/JD basis;
3. strongest demonstrated skills;
4. weakest or uncertain skills;
5. prerequisite-chain observations;
6. recommended next learning or verification questions;
7. question-by-question evidence and graph updates.

The LLM-generated prose must receive the final graph snapshot and transcript as context, but numerical mastery values should be computed by Python and written directly so the report cannot alter them.

## State and API considerations

- Keep `/api/start`, `/api/answer`, `/api/interview`, and `/api/log` compatible where possible.
- Extend `/api/log` with the current graph snapshot and selector status without exposing API keys or unnecessary resume text.
- Add optional client timing fields to answer requests, while preserving the existing `{ "answer": "..." }` contract.
- Ensure failed LLM evaluation, question generation, or final report generation does not partially mutate the graph or mark a turn complete.
- Continue using strict schema validation for skill extraction, edge generation, question generation, and evaluation.

## Testing plan

Add tests for:

- skill extraction schema validation and resume-only grounding;
- prerequisite-edge validation, duplicate removal, cycle protection, and fallback behavior;
- response latency, answer-length, and follow-up signal calculation;
- bounded mastery updates for high, medium, and low scores;
- prerequisite/dependent propagation and clamping;
- adaptive selection of the lowest-mastery/highest-uncertainty node;
- skill tags on every generated question;
- both stopping conditions;
- atomic retry behavior when any adaptive turn or final report call fails;
- skill-wise report output and exact Python-computed mastery values.

## Suggested implementation order

1. Add graph dataclasses/validation and pure unit tests.
2. Add skill extraction and prerequisite initialization prompts.
3. Add behavior signal extraction and mastery/propagation functions.
4. Refactor interview state transitions to use adaptive selection.
5. Update API timing fields and frontend question/answer metadata if needed.
6. Replace flat reporting with the skill-wise report.
7. Run the full `.venv` test suite and perform one end-to-end interview with a real resume and job description.
