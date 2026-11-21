# AI Interviewer

A resume-aware technical interviewer with a speed-first Groq LLM and fully local speech. Groq `openai/gpt-oss-20b` is primary; Gemini `gemini-3.5-flash-lite` is used only after a Groq timeout, connection failure, HTTP 429, or HTTP 5xx response.

## Runtime stack

| Function | Runtime |
|---|---|
| Interview questions, scoring, follow-ups, reports | Groq `openai/gpt-oss-20b` with low reasoning effort |
| Transient LLM fallback | Gemini `gemini-3.5-flash-lite` |
| Resume retrieval | LlamaIndex + `all-MiniLM-L6-v2`, local embeddings only |
| Job-description input | All top-level Markdown and text-based PDF files in `job_description/` |
| Speech recognition | faster-whisper `distil-small.en`, CPU INT8, 8 threads |
| Speech synthesis | Piper `en_US-lessac-medium`, local WAV output |
| Browser voice detection | Silero VAD v5 + ONNX Runtime Web, served locally |

No Google Cloud speech credentials or services are used.

## Setup

Python 3.10+, Node.js 18+, `curl`, and enough disk space for the local models are required.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
npm install
bash scripts/download_models.sh
cp .env.example .env
```

Set `GROQ_API_KEY` and `GEMINI_API_KEY` in `.env`, then run:

```bash
.venv/bin/python app.py
```

Candidates upload a PDF, Markdown, or TXT resume on `/` after the observer launches a session. Its text is indexed in memory. `RESUME_PATH` selects the demo resume used by compatibility callers and asset readiness checks.

```env
RESUME_PATH=interview_logs/sample_resume.md
```

Place every document describing the target job directly in `job_description/`:

```text
job_description/
├── role.md
├── responsibilities.pdf
└── requirements.md
```

Markdown and PDFs with selectable text are supported. Files are loaded alphabetically and treated as one job-description corpus. Unsupported, encrypted, corrupt, empty, and image-only files are skipped with a warning. If no usable JD is available, question generation falls back to the resume. No job-description environment variable is required.

When a JD is available, technical questions prioritize its requirements. Resume/JD matches are personalized to the candidate, while important requirements missing from the resume are asked as fair verification questions. The static introduction remains the first question.

Open `http://localhost:5000/observer` to configure and launch a demo. Then open `http://localhost:5000/` in the candidate browser or tab. `npm install` copies the pinned VAD, ONNX model, JavaScript runtime, and WASM artifacts to `frontend/vendor`, so the browser makes no CDN requests. The downloaded Python inference models are stored below `models/` and excluded from version control.

Before a demo, visit `http://localhost:5000/api/health`. It must return `"status": "ready"`; otherwise the response lists missing credentials or local assets. This check does not load heavyweight speech models, so the first transcription or synthesis request can still take longer while a model initializes.

## Voice behavior

Complete setup from `/observer`, then upload a resume on the candidate page. Check or enter the first name beneath the resume; the interviewer speaks this name in the welcome. Then click **Start Interview**. Camera and microphone permission is requested before the interview starts; permission failures offer a retry. Click **Start recording** for each answer. Silero VAD listens at 16 kHz and automatically submits after 1.4 seconds of silence. It uses 0.30/0.25 speech thresholds, 800 ms pre-speech padding, a 400 ms minimum speech segment, a 10-second initial-speech timeout, and a 120-second answer limit. Clicking the button again submits the current recording. The camera preview stays live, but video and microphone audio are recorded only from answer recording start through stop. Clips for accepted answers, including the introduction, are uploaded in the background. Failed uploads retry silently while the page stays open; closing the tab before an upload finishes can lose that clip.

Refreshing restores the current question and offers **Resume Interview** before reconnecting devices. The candidate page detects observer stop/reset every two seconds and releases preview/VAD streams. Errors appear beside the controls. Transient failures offer **Retry transcription** using the retained audio or **Retry answer** using the retained transcript; **Record again** discards the pending input. Pending input stays only in page memory and is lost on refresh. Conversation history remains hidden.

`POST /api/transcribe` accepts mono, uncompressed, 16-bit, 16 kHz WAV only. It rejects empty/short speech, invalid WAV, files above 20 MiB, and audio longer than 120 seconds. `POST /api/tts` returns `audio/wav`.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/roles` | Return the curated role library and interview modes |
| `GET /api/session/config` | Return the current onboarding/session configuration |
| `POST /api/session/config` | Save role, mode, and pasted job-description setup |
| `POST /api/session/config/upload` | Extract a PDF, Markdown, or text job description |
| `POST /api/session/launch` | Mark observer configuration ready for the candidate |
| `POST /api/session/resume/upload` | Upload a candidate resume using field `resume` |
| `POST /api/session/start` | Start the configured interview using the uploaded resume |
| `GET /api/session/state` | Return private-content-free readiness and current-question state for resume/synchronization |
| `POST /api/session/reset` | Return the observer to onboarding |
| `GET /api/health` | Check live credentials and required local demo assets |
| `POST /api/session/stop` | Manually end the active interview and generate its report |
| `POST /api/start` | Start an interview; accepts `context`, optional `seed_questions`, and `mode` |
| `POST /api/answer` | Submit `{ "answer": "..." }` |
| `POST /api/interview` | Compatibility route accepting `{ "content": "..." }` |
| `POST /api/transcribe` | Transcribe a WAV upload named `audio` |
| `POST /api/tts` | Synthesize `{ "text": "..." }` as WAV |
| `GET /api/log` | Return the current interview entries, skill graph, and completed report |
| `GET /api/session/report` | View the current session's completed Markdown report from its interview folder |
| `GET /api/session/report?download=1` | Download that report as a `.md` attachment |
| `POST /api/session/recordings` | Queue a WebM or MP4 clip for an accepted answer (`interview_id`, `session_id`, `question_number`, `video`) |
| `GET /api/session/recordings/status` | Check whether a clip is queued, saved, failed, or missing |

Each started interview immediately creates `interview_logs/interview_<id>/session.json`. The record
contains its role and mode, current status, skill graph, introduction, and scored answers; it is
updated after each answer. Resetting or replacing an unfinished interview marks that record as
interrupted. Completing or manually ending an interview writes its readable final analysis to
`interview_logs/interview_<id>/analysis.md` and stores that path in the JSON record. Each accepted answer's clip is saved in the same folder under `recordings/question_NNN.webm` (or `.mp4`), and the entry gains a `video_file` path. Video saving is asynchronous and does not delay scoring or the next question. Earlier flat `.json` and `.txt` reports
remain unchanged. These files are retained independently across interviews. `/api/log` continues to show the current in-memory
session rather than reading archived files.

At interview start, the app builds a local index from the configured resume and usable files in
`job_description/`, then uses Gemini `GEMINI_INIT_MODEL` (default `gemini-3.5-flash-lite`) to initialize
a validated in-memory skill graph. Answer requests may include optional `latency_ms` and
`input_mode` metadata. Each skill-tagged answer records behavior signals, applies a bounded
mastery penalty, updates the tested node, and propagates a smaller one-hop update to prerequisite
and related nodes.

The default interview mode is fully adaptive: after each answer, the planner chooses the next
skill, prerequisite, difficulty, question type, and follow-up from the updated graph. It continues
until the graph is mastered or the candidate/observer manually ends the session. `quick`, `standard`,
and `thorough` remain adaptive and ask 4, 8, and 12 main questions respectively, plus up to
2, 4, and 6 answer-specific follow-ups. Their maximum scored-answer counts are 6, 12, and 18.
They allow 1, 1, and 2 follow-ups per skill; fully adaptive allows 3. The introduction is unscored.
Reports distinguish
`all_skills_mastered`, `mode_safety_limit`, and `manual_stop`.

Required model failures return HTTP 503 with `code: "AI_SERVICE_UNAVAILABLE"`. Authentication, permission, invalid-request, and missing-model failures do not trigger Gemini. A turn is committed only after evaluation—and, on the final turn, report generation—succeeds, so the same request can safely be retried.

The candidate sends `session_id`, positive `question_number`, and `request_id` together with each answer. Successful request IDs return the saved response on retry, including after a lost network response, without re-evaluating. Failed evaluations remain retryable. Stale session/question submissions return `409` with `SESSION_CHANGED`, `QUESTION_CHANGED`, or `INTERVIEW_NOT_ACTIVE`; reusing an ID with different input returns `REQUEST_ID_REUSED`. These fields are optional for older callers. Reconfiguration/reset changes the session ID and clears saved responses. Session mutations are serialized within the single server process.

## Local demo runbook

1. Activate `.venv` and confirm `.env` contains valid `GROQ_API_KEY` and `GEMINI_API_KEY`; never share or commit `.env`.
2. Start the app with `.venv/bin/python app.py` (the system Python may not have `python-dotenv` installed).
3. Confirm `GET /api/health` returns `ready`.
4. Open `/observer`, choose a role and mode, and launch the session.
5. Open `/` for the candidate, upload a resume, click **Start Interview**, allow camera/microphone access, and answer the introduction and first technical question.
6. Keep `/observer` visible in a second window to demonstrate graph updates, evaluated-turn counts, and follow-up selection.
7. Demonstrate adaptive continuation or the **End interview** control. Confirm the report stopping reason.

The app is intentionally a single in-memory session for this demo. Do not use it for concurrent candidates or unattended production interviews.

## Tests and benchmarks

```bash
.venv/bin/python -m unittest discover -s tests -v
npm run test:recovery
.venv/bin/python scripts/verify_candidate_workflow.py
python scripts/benchmark_local.py --audio path/to/30-second-indian-english.wav
```

The automated tests cover strict structured output, allowed fallback categories, atomic retries, session identity, concurrent duplicate submissions, resume state, synchronous report failure, and WAV validation. Browser recovery tests require Node.js 22+ and Chromium; they use controlled device/provider failures with the real candidate scripts. The live verification script uses configured providers and Piper-generated speech through Whisper for three answer cycles, then checks completion and reset; its interview logs stay in a temporary directory. Physical microphone/camera quality still requires a manual check. For the target i5-1235U, measure warmed LLM turns under 2 seconds, 30-second transcription under 3 seconds, and Piper synthesis under 1 second; actual values depend on network, thermals, and model cache state.

The current `piper-tts` runtime is GPL-3.0-or-later. The Piper voices repository is MIT-licensed, while the Lessac voice model card also points to the separate Lessac/Blizzard 2013 dataset license. Review all three before redistribution; this configuration is intended for personal/academic use.
