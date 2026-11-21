# Skill Graph Engine MVP

## Summary

Transform the current single-session interviewer into a deterministic skill-driven engine through roadmap step #9:

```text
Skill Graph → Candidate State → Planner → LLM Question
→ Answer Evaluation → Skill Evidence → State Update → Planner
```

Keep Groq/Gemini failover, local resume retrieval, atomic turns, speech endpoints, frontend behavior, and reporting operational. Resume/JD skill extraction, coverage optimization, persistent sessions, and the larger directory migration are deferred.

The workspace is not a usable Git repository, so no branch will be created. The current 15-test baseline passes.

## Core Implementation

### Skill graph

Create versioned JSON datasets containing 40 backend/SWE skills:

- Languages/core: Python, Java, JavaScript, SQL, OOP, data structures, algorithms, concurrency.
- Engineering: Git, Linux, testing, debugging, performance optimization, architecture tradeoffs.
- Data: databases, indexing, transactions, data modeling, caching, Redis.
- APIs/security: REST APIs, API design, API security, authentication, application security.
- Distributed systems: message queues, Kafka, event processing, microservices, distributed systems.
- Architecture/operations: system design, scalability, reliability, observability, networking, Docker, Kubernetes, CI/CD, AWS, cloud architecture.

Each skill receives 3–5 ordered competencies. Competencies contain stable IDs, descriptions, `sequence`, and levels `[1,2,3,4,5]`. Levels map globally to Recall, Explain, Apply, Analyze, and Design.

Edges use uppercase `PREREQUISITE`, `RELATED_TO`, `PARENT_OF`, or `LEADS_TO`. Graph loading rejects duplicate IDs, missing references, self-edges, invalid relationship types, and prerequisite/parent cycles. Public graph operations resolve related skills, prerequisites, children, outgoing paths, ordered competencies, and the next competency.

### State and evidence

Introduce serializable models for:

- `InterviewState`: interview ID, status, goal/context, mode, required skills, current question/specification, skill states, evidence, history, counters, and observed coverage.
- `CandidateSkillState`: mastery, confidence, questions asked, last score, and status.
- `SkillEvidence`: stable question/answer IDs, skill and competency IDs, score, assessment, explanation, and confidence.
- `QuestionSpec`: skill, competency, level, difficulty, question type, traversal kind, and planner reason.

Mastery uses:

```text
new_mastery = 0.6 × (score / 10) + 0.4 × previous_mastery
```

Confidence is the running mean of evaluator confidence values. Status thresholds are:

- No evidence: `untested`
- `< 0.40`: `weak`
- `< 0.70`: `developing`
- `< 0.85`: `strong`
- `>= 0.85`: `mastered`

Assessment is restricted to `insufficient`, `incorrect`, `partial`, `correct`, or `advanced`.

### Deterministic planner

The planner never generates question text and never accepts traversal decisions from the LLM.

- Untested skill: select its first competency at level 2.
- Score `< 4`: select a direct prerequisite at level 1; otherwise move to an earlier/easier competency or reduce the level.
- Score `4–6`: retest the same competency and level using a different question type.
- Score `7–8`: move to the next competency and increase the level by one.
- Score `9–10`: move deeper immediately; leave the skill when no deeper competency remains.
- Mastery `>= 0.85`: stop testing that skill and choose another required skill.
- Exhausted follow-up allowance: move to the next required skill.
- Ties are deterministic: untested first, then lowest confidence, lowest mastery, fewest questions, and finally caller-provided skill order.

Question types map to difficulty: definition at level 1, explanation at 2, applied task at 3, scenario/tradeoff at 4, and design at 5. Recent specification fingerprints prevent exact repetition.

Retain current mode limits as main-question and remediation limits:

- `quick`: 2 main selections, at most 1 remediation each.
- `standard`: 5 main selections, at most 1 remediation each.
- `thorough`: 3 main selections, at most 2 remediations each.

## LLM and Interview Flow

### Question generation

The planner emits a `QuestionSpec`; a new question generator retrieves relevant resume context and asks the existing provider-neutral LLM service for exactly:

```json
{"question": "one natural-language question"}
```

The prompt constrains the model to the selected skill, competency, level, and question type and prohibits revealing an answer. Existing Groq-first and transient-only Gemini fallback behavior remains unchanged.

### Evaluation

Replace the current turn schema with:

```json
{
  "reaction": "Good explanation.",
  "score": 7,
  "evaluation": "Correctly explained indexes but missed write overhead.",
  "skill_evidence": [{
    "skill_id": "database_indexing",
    "competency_id": "index_tradeoffs",
    "assessment": "partial",
    "confidence": 0.88,
    "evidence": "Candidate understood read benefits but missed write costs."
  }],
  "follow_up": {
    "needed": true,
    "reason": "weak_competency"
  }
}
```

Require exactly one evidence item for the current skill and competency. Reject mismatched IDs, invalid confidence, missing evidence, or extra fields. Store the LLM’s follow-up recommendation for explanation/reporting, but let Python’s planner independently choose traversal.

For `/api/answer`, perform all work against a cloned prospective state:

1. Evaluate the current answer.
2. Validate evidence.
3. Update prospective mastery/confidence.
4. Run the planner.
5. Generate the next question, or synchronously generate the final report.
6. Commit state, evidence, history, and logs only after every required model call succeeds.

Any evaluation, question-generation, or final-report failure returns 503 with no state mutation, score, evidence, log append, or advancement.

### Reporting

Keep synchronous final reporting. Build deterministic skill summaries and evidence tables in Python, then let the LLM produce prose without changing computed values. MVP sections are overall assessment, skill breakdown, strong areas, weak areas, evidence, and recommended learning areas. Resume-claim and job-fit sections remain deferred.

## API Compatibility

Extend `POST /api/start` additively:

```json
{
  "context": "Backend Engineer",
  "mode": "standard",
  "required_skill_ids": ["python", "sql", "rest_apis", "system_design"]
}
```

- `required_skill_ids` is optional.
- Default: `python`, `sql`, `databases`, `rest_apis`, `system_design`, and `testing`.
- Reject empty, duplicate, or unknown skill IDs with HTTP 400.
- Reject `seed_questions` with HTTP 400 because planner-controlled skill mode cannot safely assign arbitrary questions.
- Return existing fields plus `interview_id`, `current`, and an initial skill-state summary.

Keep `/api/answer`, `/api/interview`, and `/api/log` compatible. Enrich successful answer responses with `skill_evidence`, updated `skill_state`, and current question specification. Enrich log entries with question/specification IDs and evidence while retaining question, answer, score, and evaluation.

Remain single-session; `interview_id` is informational and prepares for later multi-session support. Do not modify `/api/transcribe`, `/api/tts`, browser VAD, Whisper, Piper, or frontend audio behavior.

## Test Plan

Before integration, expand regression coverage for current behavior:

- Groq success and strict schemas.
- Gemini fallback for timeout, connection, 429, and 5xx.
- No fallback for configuration/4xx errors.
- Atomic rollback on evaluation and final-report failure.
- Successful and invalid transcription responses.
- WAV TTS response.
- Interview start, ordinary turn, and completion.

Add at least 45 skill-engine tests, bringing the suite to at least 60:

- Graph loading, relationship queries, cycles, invalid IDs/edges, and competency ordering.
- Five-level difficulty and question-type mapping.
- Initial state, mastery calculation, confidence averaging, status thresholds, serialization, and evidence persistence.
- At least 25 deterministic planner scenarios covering weak remediation, developing retests, strong progression, mastery avoidance, prerequisites, required-skill order, follow-up limits, repetition prevention, and completion.
- Start-to-question and answer-to-evidence integration.
- Question and evaluator schema rejection.
- Groq-to-Gemini behavior through both new LLM call sites.
- Dual-provider failure during evaluation, next-question generation, and reporting with full rollback.
- Historical log compatibility and enriched report generation.
- All original speech and static-asset tests unchanged.

Default tests use mocked providers; existing live Groq and Gemini verification scripts remain opt-in.

## Assumptions and Deferred Work

- Implement the critical MVP only through adaptive traversal step #9.
- Do not create a Git branch because the workspace has no usable Git metadata.
- Keep the stable LLM, retrieval, speech, and Flask modules in place; add `skill_engine/`, `interview/`, and `data/` rather than performing the full production directory migration now.
- Resume excerpts may contextualize questions, but resume/JD skill extraction and unverified-claim state are deferred.
- Track observed coverage for future use, but do not use coverage weights in MVP planning.
- Durable storage, concurrent interview IDs, job priorities, coverage optimization, and full job-fit reporting belong to later milestones.
