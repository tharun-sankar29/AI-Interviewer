"""Background storage for accepted answer recordings.

The upload request stages bytes outside the interview lock. Only the short JSON
metadata update shares the lock used by interview turns.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from backend import interview_session as session

MAX_VIDEO_BYTES = 64 * 1024 * 1024
_INTERVIEW_ID = re.compile(r"interview_\d{8}_\d{6}_\d{6}\Z")
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="answer-video")
_status_lock = Lock()
_statuses: dict[tuple[str, int], str] = {}


class VideoUploadError(ValueError):
    pass


def _record_path(interview_id: str) -> Path:
    if not isinstance(interview_id, str) or not _INTERVIEW_ID.fullmatch(interview_id):
        raise VideoUploadError("Invalid interview ID.")
    return (Path.cwd() / "interview_logs" / interview_id / "session.json").resolve()


def _accepted_entry(path: Path, session_id: str, question_number: int) -> dict:
    if not isinstance(session_id, str) or not session_id:
        raise VideoUploadError("Invalid session ID.")
    if isinstance(question_number, bool) or not isinstance(question_number, int) or question_number < 1:
        raise VideoUploadError("Invalid question number.")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VideoUploadError("Interview record is unavailable.") from exc
    if record.get("interview_id") != path.parent.name or record.get("session_id") != session_id:
        raise VideoUploadError("Interview session does not match.")
    entries = record.get("entries", [])
    if len(entries) < question_number:
        raise VideoUploadError("The answer has not been accepted.")
    return record


def recording_status(interview_id: str, session_id: str, question_number: int) -> str:
    path = _record_path(interview_id)
    record = _accepted_entry(path, session_id, question_number)
    if record["entries"][question_number - 1].get("video_file"):
        return "saved"
    with _status_lock:
        return _statuses.get((str(path), question_number), "missing")


def _save_video(path: Path, staged: Path, final: Path, session_id: str, question_number: int) -> None:
    key = (str(path), question_number)
    try:
        with session.SESSION_LOCK:
            record = _accepted_entry(path, session_id, question_number)
            entry = record["entries"][question_number - 1]
            if not entry.get("video_file"):
                os.replace(staged, final)
                entry["video_file"] = str(final.relative_to(path.parent))
                try:
                    session._save_record(record, path)
                except Exception:
                    final.unlink(missing_ok=True)
                    raise
                active = session.INTERVIEW_STATE.get("session_record")
                if active and active.get("interview_id") == record["interview_id"]:
                    active["entries"][question_number - 1]["video_file"] = entry["video_file"]
        with _status_lock:
            _statuses[key] = "saved"
    except Exception:
        session.logger.exception("answer_video_save_failed interview_id=%s question=%s", path.parent.name, question_number)
        with _status_lock:
            _statuses[key] = "failed"
    finally:
        staged.unlink(missing_ok=True)


def queue_recording(interview_id: str, session_id: str, question_number: int, upload) -> str:
    path = _record_path(interview_id)
    record = _accepted_entry(path, session_id, question_number)
    if record["entries"][question_number - 1].get("video_file"):
        return "saved"
    if upload is None:
        raise VideoUploadError("Missing video file.")
    media_type = (upload.mimetype or "").lower()
    if media_type == "video/webm":
        extension, magic = ".webm", b"\x1a\x45\xdf\xa3"
    elif media_type == "video/mp4":
        extension, magic = ".mp4", b"ftyp"
    else:
        raise VideoUploadError("Use WebM or MP4 video.")
    recordings = path.parent / "recordings"
    recordings.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".upload_", dir=recordings)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            total = 0
            while chunk := upload.stream.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise VideoUploadError("Video exceeds the 64 MB limit.")
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        header = staged.open("rb").read(12)
        if not total or (extension == ".webm" and not header.startswith(magic)) or (extension == ".mp4" and header[4:8] != magic):
            raise VideoUploadError("The uploaded video is invalid.")
        key = (str(path), question_number)
        with _status_lock:
            if _statuses.get(key) == "queued":
                return "queued"
            _statuses[key] = "queued"
        final = recordings / f"question_{question_number:03d}{extension}"
        try:
            _executor.submit(_save_video, path, staged, final, session_id, question_number)
        except Exception:
            with _status_lock:
                _statuses[key] = "failed"
            raise
        staged = None
        return "queued"
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
