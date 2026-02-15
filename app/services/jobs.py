import os
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Any

from app.utils.files import ensure_dir, job_dir


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobRecord:
    job_id: str
    status: str = "queued"  # queued | running | completed | failed
    message: Optional[str] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    # storage
    work_dir: str = ""
    input_filename: str = ""
    input_path: str = ""

    # user-provided metadata
    email: str = ""
    program: Optional[str] = None
    contract_id: Optional[str] = None
    contract_version: Optional[str] = None

    # artifacts produced (file paths)
    artifacts: Dict[str, str] = field(default_factory=dict)

    # lineage (HITL reruns)
    rerun_of: Optional[str] = None
    rerun_iteration: int = 0
    user_feedback: Optional[str] = None

    def touch(self) -> None:
        self.updated_at = _now_iso()


class JobStore:
    """
    In-memory job store for V1 local testing.
    (Later in K8s: swap with Redis or DB, but interfaces can remain.)
    """
    def __init__(self, tmp_root: str):
        self._tmp_root = tmp_root
        ensure_dir(tmp_root)
        self._lock = threading.Lock()
        self._jobs: Dict[str, JobRecord] = {}

    def create_job(
        self,
        job_id: str,
        *,
        input_filename: str,
        input_path: str,
        email: str,
        program: Optional[str],
        contract_id: Optional[str],
        contract_version: Optional[str],
        rerun_of: Optional[str] = None,
        rerun_iteration: int = 0,
        user_feedback: Optional[str] = None,
    ) -> JobRecord:
        with self._lock:
            wd = job_dir(self._tmp_root, job_id)
            rec = JobRecord(
                job_id=job_id,
                status="queued",
                message="Job created",
                work_dir=wd,
                input_filename=input_filename,
                input_path=input_path,
                email=email,
                program=program,
                contract_id=contract_id,
                contract_version=contract_version,
                rerun_of=rerun_of,
                rerun_iteration=rerun_iteration,
                user_feedback=user_feedback,
            )
            self._jobs[job_id] = rec
            self._persist_job(rec)
            return rec

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            return self._jobs.get(job_id)

    def update(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        message: Optional[str] = None,
        error: Optional[str] = None,
        artifacts: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[JobRecord]:
        with self._lock:
            rec = self._jobs.get(job_id)
            if not rec:
                return None

            if status is not None:
                rec.status = status
            if message is not None:
                rec.message = message
            if error is not None:
                rec.error = error
            if artifacts:
                rec.artifacts.update(artifacts)

            # Runtime metadata is persisted to a sidecar artifact for UI.
            # (We intentionally do NOT attach it as a field on JobRecord.)
            if metadata is not None:
                meta_path = os.path.join(rec.work_dir, "metadata.json")
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2)
                rec.artifacts.setdefault("metadata", meta_path)

            rec.touch()
            self._persist_job(rec)
            return rec

    def _persist_job(self, rec: JobRecord) -> None:
        """
        Writes a small state file so you can inspect jobs easily while developing.
        This is still "ephemeral runtime" under tmp/jobs/<job_id>/.
        """
        state_path = os.path.join(rec.work_dir, "job_state.json")
        data = {
            "job_id": rec.job_id,
            "status": rec.status,
            "message": rec.message,
            "error": rec.error,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
            "input_filename": rec.input_filename,
            "input_path": rec.input_path,
            "email": rec.email,
            "program": rec.program,
            "contract_id": rec.contract_id,
            "contract_version": rec.contract_version,
            "artifacts": rec.artifacts,
            "rerun_of": rec.rerun_of,
            "rerun_iteration": rec.rerun_iteration,
            "user_feedback": rec.user_feedback,
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
