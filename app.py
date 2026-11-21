import logging
import re
from copy import deepcopy
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, make_response, request, send_file, send_from_directory

load_dotenv()

from backend.analysis_module import build_rag_index, parse_job_description_upload, parse_resume_upload
from backend.demo_readiness import readiness_report
from backend.interview_session import (
    INTERVIEW_MODES,
    INTERVIEW_SENDOFF,
    INTERVIEW_STATE,
    ROLE_LIBRARY,
    SESSION_CONFIG,
    SESSION_LOCK,
    ANSWER_RECEIPTS,
    clear_session_config,
    configure_session,
    get_full_log,
    mark_session_launched,
    session_config,
    start_interview,
    stop_interview,
    submit_answer,
    store_resume,
)
from backend.llm_service import LLMServiceUnavailable
from backend.speech_service import SpeechInputError, stt_service, tts_service
from backend.video_storage import MAX_VIDEO_BYTES, queue_recording, recording_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="frontend")
app.config["MAX_CONTENT_LENGTH"] = 21 * 1024 * 1024


def serialized_session(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with SESSION_LOCK:
            return function(*args, **kwargs)
    return wrapped


def session_conflict(message, code):
    return jsonify({"error": message, "code": code, "retryable": False}), 409


def _candidate_state():
    config = session_config()
    started = bool(config["candidate_started"])
    return {
        "session_id": config["session_id"],
        "interview_id": (INTERVIEW_STATE.get("session_record") or {}).get("interview_id") if started else None,
        "observer_ready": bool(config["launched"]),
        "candidate_resume_uploaded": bool(config["resume_uploaded"]),
        "candidate_resume_filename": config["resume_filename"],
        "candidate_first_name": config.get("candidate_first_name"),
        "candidate_ready": bool(config["launched"] and config["resume_uploaded"] and not started),
        "interview_started": started,
        "role_name": config["role_name"], "category": config["category"], "mode": config["mode"],
        "execution_phase": INTERVIEW_STATE.get("execution_phase", "idle") if started else "idle",
        "current_question": deepcopy(INTERVIEW_STATE.get("current_question")) if started else None,
        "question_number": INTERVIEW_STATE.get("candidate_question_number", 1) if started else 0,
        "evaluated_answer_count": INTERVIEW_STATE.get("questions_asked", 0) if started else 0,
        "closing_message": INTERVIEW_SENDOFF if started and INTERVIEW_STATE.get("execution_phase") == "completed" else None,
    }


@app.route("/api/session/state", methods=["GET"])
@serialized_session
def api_candidate_state():
    response = jsonify(_candidate_state())
    response.headers["Cache-Control"] = "no-store"
    return response


def service_error(exc):
    logger.warning("required_ai_request_failed category=%s", exc.category)
    return jsonify({"error": exc.public_message, "code": "AI_SERVICE_UNAVAILABLE", "retryable": exc.category not in {"configuration", "model_not_found", "invalid_request"}}), 503


def persistence_error(exc):
    logger.error("interview_artifact_write_failed reason=%s", type(exc).__name__)
    return jsonify({"error": "Could not save interview data. Please retry.", "code": "PERSISTENCE_UNAVAILABLE", "retryable": True}), 503


def _current_report_path():
    record = INTERVIEW_STATE.get("session_record") or {}
    if record.get("status") != "completed" or not record.get("analysis_file"):
        return None
    path = Path(record["analysis_file"]).resolve()
    log_file = Path(INTERVIEW_STATE.get("log_file") or "").resolve()
    if path != log_file.parent / "analysis.md" or not path.is_file():
        return None
    return path


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/observer")
def observer():
    return send_from_directory(app.static_folder, "observer.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(app.static_folder, path)


@app.route("/api/start", methods=["POST"])
@serialized_session
def api_start():
    data = request.get_json(silent=True) or {}
    try:
        result = start_interview(data.get("context", "General AI Interview"), data.get("seed_questions"), data.get("mode", "adaptive"))
        return jsonify(result)
    except LLMServiceUnavailable as exc:
        return service_error(exc)
    except OSError as exc:
        return persistence_error(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


def _public_session_config():
    config = session_config()
    mode = INTERVIEW_MODES.get(config.get("mode"), INTERVIEW_MODES["adaptive"])
    return {
        key: value for key, value in config.items()
        if key not in {"job_description", "resume_content"}
    } | {
        "job_description_length": len(config["job_description"]),
        "mode_label": mode["label"],
        "mode_description": mode["description"],
        "adaptive_turn_budget": mode["adaptive_turn_budget"],
        "main_question_budget": mode.get("main_question_budget"),
        "max_followups_total": mode.get("max_followups_total"),
        "max_followups_per_skill": mode.get("max_followups_per_skill"),
        "manual_stop_available": bool(config.get("candidate_started")) and INTERVIEW_STATE.get("execution_phase") != "completed",
        "observer_ready": bool(config.get("launched")),
        "candidate_resume_uploaded": bool(config.get("resume_uploaded")),
        "candidate_resume_filename": config.get("resume_filename"),
        "candidate_ready": bool(config.get("launched") and config.get("resume_uploaded") and not config.get("candidate_started")),
        "interview_started": bool(config.get("candidate_started")),
    }


@app.route("/api/roles", methods=["GET"])
def api_roles():
    return jsonify({"roles": ROLE_LIBRARY, "modes": INTERVIEW_MODES})


@app.route("/api/health", methods=["GET"])
def api_health():
    report = readiness_report()
    return jsonify(report), 200 if report["status"] == "ready" else 503


@app.route("/api/session/config", methods=["GET", "POST"])
@serialized_session
def api_session_config():
    if request.method == "GET":
        return jsonify(_public_session_config())
    data = request.get_json(silent=True) or {}
    try:
        config = configure_session(
            role_id=data.get("role_id", ""),
            role_name=data.get("role_name"),
            mode=data.get("mode", "adaptive"),
            context=data.get("context"),
            job_description=data.get("job_description", ""),
            job_description_files=data.get("job_description_files", []),
            job_description_source=data.get("job_description_source", "pasted"),
        )
        return jsonify(_public_session_config())
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except OSError as exc:
        return persistence_error(exc)


@app.route("/api/session/config/upload", methods=["POST"])
def api_session_config_upload():
    uploaded = request.files.get("job_description")
    if uploaded is None:
        return jsonify({"error": "Upload a PDF, Markdown, or text job description."}), 400
    try:
        parsed = parse_job_description_upload(uploaded.filename, uploaded.read())
        return jsonify({
            "filename": parsed["files"][0],
            "content": parsed["content"],
            "character_count": len(parsed["content"]),
            "truncated": parsed["truncated"],
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/session/launch", methods=["POST"])
@serialized_session
def api_session_launch():
    config = session_config()
    if not config.get("role_id"):
        return jsonify({"error": "Complete onboarding before launching the interview."}), 400
    try:
        mark_session_launched()
        return jsonify({"status": "ready", "session": _public_session_config()})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/session/resume/upload", methods=["POST"])
@serialized_session
def api_resume_upload():
    if request.form.get("session_id") and request.form["session_id"] != SESSION_CONFIG["session_id"]:
        return session_conflict("The observer reset the session.", "SESSION_CHANGED")
    uploaded = request.files.get("resume")
    if uploaded is None:
        return jsonify({"error": "Upload a PDF, Markdown, or text resume."}), 400
    try:
        parsed = parse_resume_upload(uploaded.filename, uploaded.read())
        store_resume(parsed["filename"], parsed["text"])
        return jsonify({"status": "uploaded", "filename": parsed["filename"],
                        "candidate_first_name": SESSION_CONFIG.get("candidate_first_name"),
                        "character_count": len(parsed["text"]), "truncated": parsed["truncated"]})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/session/start", methods=["POST"])
@serialized_session
def api_session_start():
    config = session_config()
    data = request.get_json(silent=True) or {}
    if data.get("session_id") and data["session_id"] != config["session_id"]:
        return session_conflict("The observer reset the session.", "SESSION_CHANGED")
    if not config.get("launched"):
        return jsonify({"error": "The observer has not launched a session yet."}), 400
    if not config.get("resume_uploaded") or not config.get("resume_content"):
        return jsonify({"error": "Upload a valid resume before starting the interview."}), 400
    if config.get("candidate_started"):
        return jsonify({"error": "The interview has already started."}), 409
    candidate_first_name = data.get("candidate_first_name")
    if candidate_first_name is not None:
        if not isinstance(candidate_first_name, str) or not re.fullmatch(r"[^\W\d_]{2,30}(?:[-'’][^\W\d_]{2,30})?", candidate_first_name.strip(), re.UNICODE):
            return jsonify({"error": "Enter a first name using 2–30 letters."}), 400
        candidate_first_name = candidate_first_name.strip()
    try:
        result = start_interview(
            config["context"], mode=config["mode"],
            job_description_override=config["job_description"] if config["job_description"] else None,
            job_description_files_override=config["job_description_files"], role_name=config["role_name"],
            resume_content=config["resume_content"], resume_filename=config["resume_filename"],
            candidate_first_name=candidate_first_name,
        )
        if candidate_first_name:
            SESSION_CONFIG["candidate_first_name"] = candidate_first_name
        SESSION_CONFIG["candidate_started"] = True
        INTERVIEW_STATE["candidate_question_number"] = 1
        ANSWER_RECEIPTS.clear()
        return jsonify({"status": "started", "session_id": config["session_id"], "interview_id": (INTERVIEW_STATE.get("session_record") or {}).get("interview_id"), "role_name": config["role_name"], "mode": config["mode"], "resume_filename": config["resume_filename"], "next_question": result["next_question"], "question_number": 1, **result})
    except LLMServiceUnavailable as exc:
        return service_error(exc)
    except OSError as exc:
        return persistence_error(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/session/reset", methods=["POST"])
@serialized_session
def api_session_reset():
    try:
        clear_session_config()
        return jsonify(_public_session_config())
    except OSError as exc:
        return persistence_error(exc)


def _answer(field):
    data = request.get_json(silent=True) or {}
    answer = data.get(field)
    if not isinstance(answer, str) or not answer.strip():
        return jsonify({"error": "No answer provided."}), 400
    latency_ms = data.get("latency_ms")
    if latency_ms is not None and (isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float)) or latency_ms < 0):
        return jsonify({"error": "latency_ms must be a non-negative number."}), 400
    input_mode = data.get("input_mode", "text")
    if input_mode not in {"text", "voice"}:
        return jsonify({"error": "input_mode must be text or voice."}), 400
    scoped = any(key in data for key in ("session_id", "question_number", "request_id"))
    request_id = data.get("request_id")
    if scoped:
        number = data.get("question_number")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or not isinstance(data.get("session_id"), str) or isinstance(number, bool) or not isinstance(number, int) or number < 1:
            return jsonify({"error": "Supply session_id, a positive question_number, and request_id together."}), 400
        if data["session_id"] != SESSION_CONFIG["session_id"]:
            return session_conflict("The observer reset the session.", "SESSION_CHANGED")
        signature = (number, answer.strip(), latency_ms, input_mode)
        receipt = ANSWER_RECEIPTS.get(request_id)
        if receipt:
            if receipt["signature"] != signature:
                return session_conflict("This request ID was already used for another answer.", "REQUEST_ID_REUSED")
            return jsonify(receipt["result"])
        if not SESSION_CONFIG["candidate_started"] or INTERVIEW_STATE.get("execution_phase") == "completed":
            return session_conflict("This interview is no longer accepting answers.", "INTERVIEW_NOT_ACTIVE")
        if number != INTERVIEW_STATE.get("candidate_question_number", 1):
            return session_conflict("The current question has changed.", "QUESTION_CHANGED")
    try:
        result = submit_answer(answer.strip(), latency_ms=latency_ms, input_mode=input_mode)
        if SESSION_CONFIG["candidate_started"]:
            if result.get("next_question"):
                INTERVIEW_STATE["candidate_question_number"] = INTERVIEW_STATE.get("candidate_question_number", 1) + 1
            result = {**result, "question_number": INTERVIEW_STATE.get("candidate_question_number", 1), "session_id": SESSION_CONFIG["session_id"]}
        if scoped:
            ANSWER_RECEIPTS[request_id] = {"signature": signature, "result": deepcopy(result)}
        return jsonify(result)
    except LLMServiceUnavailable as exc:
        return service_error(exc)
    except OSError as exc:
        return persistence_error(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc), "retryable": True}), 503


@app.route("/api/answer", methods=["POST"])
@serialized_session
def api_answer():
    return _answer("answer")


@app.route("/api/session/stop", methods=["POST"])
@serialized_session
def api_session_stop():
    data = request.get_json(silent=True) or {}
    if data.get("session_id") and data["session_id"] != SESSION_CONFIG["session_id"]:
        return session_conflict("The observer reset the session.", "SESSION_CHANGED")
    try:
        return jsonify(stop_interview())
    except LLMServiceUnavailable as exc:
        return service_error(exc)
    except OSError as exc:
        return persistence_error(exc)


@app.route("/api/interview", methods=["POST"])
@serialized_session
def api_interview():
    response = _answer("content")
    if isinstance(response, tuple):
        return response
    data = response.get_json()
    return jsonify({
        "message": data.get("reaction", ""),
        "response": data.get("reaction", ""),
        "next_question": data.get("next_question", ""),
        "analysis_score": data.get("analysis_score", data.get("score")),
        "evaluation": data.get("evaluation", ""),
        "status": data.get("status"),
    })


@app.route("/api/log", methods=["GET"])
@serialized_session
def api_log():
    mode = SESSION_CONFIG["mode"] if SESSION_CONFIG["launched"] else INTERVIEW_STATE.get("mode", "adaptive")
    return jsonify({
        "session": _public_session_config(),
        "entries": get_full_log(),
        "skill_graph": INTERVIEW_STATE.get("skill_graph", {"skills": {}, "edges": []}),
        "graph_initialized": INTERVIEW_STATE.get("graph_initialized", False),
        "graph_source": INTERVIEW_STATE.get("graph_source"),
        "planner_state": {
            "questions_asked": INTERVIEW_STATE.get("questions_asked", 0),
            "main_questions_used": INTERVIEW_STATE.get("main_questions_used", 0),
            "followups_used": INTERVIEW_STATE.get("followups_used", 0),
            "evaluated_turn_count": INTERVIEW_STATE.get("questions_asked", 0),
            "followup_depth": (INTERVIEW_STATE.get("current_question") or {}).get("followup_depth", 0),
            "planner_reason": INTERVIEW_STATE.get("planner_reason"),
            "current_skill_id": INTERVIEW_STATE.get("current_skill_id"),
            "stopping_reason": INTERVIEW_STATE.get("stopping_reason"),
            "execution_phase": INTERVIEW_STATE.get("execution_phase", "idle"),
            "current_question": INTERVIEW_STATE.get("current_question"),
            "mode": mode,
            "mode_limits": INTERVIEW_MODES.get(mode, {}),
            "mode_description": INTERVIEW_MODES.get(mode, {}).get("description"),
            "adaptive_turn_budget": INTERVIEW_MODES.get(mode, {}).get("adaptive_turn_budget"),
            "manual_stop_available": bool(session_config().get("candidate_started")) and INTERVIEW_STATE.get("execution_phase") != "completed",
        },
        "report": INTERVIEW_STATE.get("report"),
        "report_available": _current_report_path() is not None,
    })


@app.route("/api/session/report", methods=["GET"])
@serialized_session
def api_session_report():
    path = _current_report_path()
    if path is None:
        abort(404)
    download = request.args.get("download") == "1"
    response = send_file(path, mimetype="text/markdown; charset=utf-8",
                         as_attachment=download, download_name=path.name)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/session/recordings", methods=["POST"])
def api_session_recordings():
    # This endpoint never holds the interview lock while the browser uploads.
    request.max_content_length = MAX_VIDEO_BYTES + 1024 * 1024
    try:
        number = int(request.form.get("question_number", ""))
        status = queue_recording(request.form.get("interview_id"), request.form.get("session_id"),
                                 number, request.files.get("video"))
        return jsonify({"status": status}), 200 if status == "saved" else 202
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc), "retryable": False}), 400
    except OSError as exc:
        return persistence_error(exc)


@app.route("/api/session/recordings/status", methods=["GET"])
def api_session_recording_status():
    try:
        number = int(request.args.get("question_number", ""))
        status = recording_status(request.args.get("interview_id"), request.args.get("session_id"), number)
        return jsonify({"status": status})
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc), "retryable": False}), 400


@app.route("/api/tts", methods=["POST"])
def api_tts():
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "No text provided."}), 400
    if len(text) > 3000:
        return jsonify({"error": "Text is too long."}), 400
    try:
        audio = tts_service.synthesize(text.strip())
        response = make_response(audio)
        response.headers["Content-Type"] = "audio/wav"
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response
    except Exception:
        logger.exception("piper_synthesis_failed")
        return jsonify({"error": "Text-to-speech service is unavailable."}), 503


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided."}), 400
    try:
        transcript = stt_service.transcribe(request.files["audio"].read())
        return jsonify({"transcript": transcript, "status": "success"})
    except SpeechInputError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("whisper_transcription_failed")
        return jsonify({"error": "Speech-to-text service is unavailable."}), 503


@app.after_request
def add_security_headers(response):
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; media-src 'self' blob:; connect-src 'self';"
    return response


def initialize_rag_index():
    INTERVIEW_STATE["rag_index"] = build_rag_index()


if __name__ == "__main__":
    initialize_rag_index()
    app.run(port=5000, debug=True, use_reloader=False)
