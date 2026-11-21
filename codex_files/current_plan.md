# Phase 1–2 Skill Graph and Mastery Updates

## Summary

Implement only Skill Graph initialization and behavior-aware mastery updates. Leave adaptive question selection and final reporting unchanged for now.

Use:

- Resume plus existing `job_description/` files as indexed knowledge.
- Gemini 2.5 Flash directly for skill and prerequisite extraction.
- In-memory Python dictionaries/dataclasses for the session graph.
- One-hop weighted decay for propagation.
- A small behavior penalty capped at `0.15`.

## Phase 1 — Skill Graph Initialization

Update the RAG pipeline so the index contains the configured candidate resume and Markdown/text-based PDF files from `job_description/`, with source metadata identifying resume versus job-description content.

At interview start:

1. Build or reuse the combined RAG index.
2. Retrieve excerpts focused on skills, tools, concepts, and experience.
3. Call Gemini 2.5 Flash directly with strict JSON output.
4. Extract bounded technical skill nodes containing stable ID, display name, category/topic, evidence source, initial mastery, and initial confidence.
5. Call Gemini 2.5 Flash for prerequisite and related edges.
6. Validate node references, reject self-edges and duplicates, and prevent cycles.
7. Store the graph in `INTERVIEW_STATE["skill_graph"]`.

Initial node defaults:

```text
mastery = 0.5
confidence = 0.0
evidence_count = 0
last_score = null
```

Store the initial graph snapshot and schema version in the interview log. Add a direct Gemini JSON helper while preserving the existing Groq-first service for answer evaluation.

## Phase 2 — Signals, Mastery, and Propagation

### Behavior signals

Capture per interaction:

- `latency_ms`: question-audio completion to VAD speech-start;
- answer word and character counts;
- follow-up dependency count;
- typed versus spoken input mode;
- tested `skill_id`.

The frontend records a monotonic timestamp when Piper playback ends and another when Silero VAD detects speech, then sends the elapsed latency. Typed answers use `null` latency.

### Mastery fusion

Normalize the LLM score:

```text
score_signal = score / 10
```

Calculate:

```text
penalty = latency_penalty + followup_penalty + short_answer_penalty
penalty = clamp(penalty, 0.0, 0.15)
fused_score = clamp(score_signal - penalty, 0.0, 1.0)
```

Use:

```text
new_mastery = 0.6 × fused_score + 0.4 × previous_mastery
```

High latency receives a gradual penalty above a hesitation threshold. Follow-up dependency and very short answers receive small capped penalties. Missing latency receives no latency penalty.

Store previous mastery, raw score, behavioral penalty, fused score, and new mastery for auditability.

### Graph propagation

After updating the tested node:

```text
direct_delta = new_mastery - old_mastery
prerequisite_delta = direct_delta × edge_weight × 0.20
related_delta = direct_delta × edge_weight × 0.10
```

Propagate one graph hop only, clamp mastery to `[0.0, 1.0]`, update confidence/evidence count only for the directly tested node, and record propagation results. Use a visited set to protect against malformed cycles.

## State and API changes

Extend `INTERVIEW_STATE` with `skill_graph`, `graph_version`, `graph_initialized`, `last_question_audio_ended_at`, and current `skill_id`.

Allow answer requests to include optional metadata:

```json
{
  "answer": "...",
  "latency_ms": 1840,
  "input_mode": "voice"
}
```

Keep `{ "answer": "..." }` valid. Extend successful answers and `/api/log` with the tested skill, raw score, behavior signals, fused mastery, updated node, propagation updates, and graph summary.

Preserve atomicity: failed evaluation, mastery calculation, or graph persistence must not commit the answer, graph updates, or log entry.

## Tests and acceptance criteria

Add tests for:

- Combined resume/JD indexing and source metadata.
- Gemini extraction schema validation.
- Edge validation, duplicate removal, and cycle rejection.
- Graph initialization during `/api/start`.
- Missing/empty source fallback behavior.
- Latency, answer-length, and follow-up signal calculation.
- Typed answers with missing latency.
- Penalty cases and the `0.15` cap.
- Mastery clamping.
- Weighted one-hop prerequisite/related propagation.
- Atomic rollback on evaluation or graph-update failure.
- Existing 34-test suite remaining green.

Phase 3 adaptive selection and Phase 4 skill-wise reporting remain out of scope for this implementation.
