# app/api/routes.py
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional, Dict, Any, List, Tuple

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.schemas import (
    UploadResponse,
    JobStatusResponse,
    JobResultResponse,
    ReviewRequest,
    ReviewResponse,
    RerunRequest,
    RerunResponse,
)
from app.services.jobs import JobStore
from app.utils.config import get_settings
from app.utils.files import new_job_id, safe_filename, write_bytes, ensure_dir

log = logging.getLogger("app.api")

router = APIRouter()

# set by app startup (see main.py)
JOB_STORE: Optional[JobStore] = None


def _require_store() -> JobStore:
    if JOB_STORE is None:
        raise RuntimeError("JobStore not initialized")
    return JOB_STORE


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_metadata(rec) -> Dict[str, Any]:
    """Read metadata.json sidecar artifact (if present)."""
    try:
        mp = (rec.artifacts or {}).get("metadata")
        if mp and os.path.exists(mp):
            return _read_json(mp) or {}
    except Exception:
        return {}
    return {}


def _write_metadata(rec, md: Dict[str, Any]) -> str:
    """Persist metadata.json under the job work_dir and return its path."""
    meta_path = os.path.join(rec.work_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(md or {}, f, indent=2, ensure_ascii=False)
    rec.artifacts.setdefault("metadata", meta_path)
    return meta_path


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=3, description="Natural language query for retrieval")
    k: int = Field(6, ge=1, le=20, description="Top-K results to return (1-20)")


# Debug artifacts that should be gated server-side.
DEBUG_ARTIFACT_KEYS = {
    "qad_json",  # qad_checks.json
    "contract_text",
    "document_blocks",
    "ingest_report",
    "chunks",
    "chunk_meta",
    "faiss_index",
    "metadata",
    # DeepAgents artifacts
    "agents_run_config",  # NEW (6.8)
    "agents_plan",
    "agents_draft",
    "agents_normalized",
    "agents_validation",
    "agents_final",
    "agents_merged",
    "agents_dedupe_report",
    "agents_planner_raw",
    "agents_repair_attempt",
    "agents_repair_report",
    "agents_quality_report",
    "agents_quality_summary",
    "agents_export_policy",
    "agents_export_summary",
    "agents_export_ready_checks",
    "agents_export_used",
}


def _is_debug_artifact(key: str) -> bool:
    return key in DEBUG_ARTIFACT_KEYS


def _server_allows_debug_downloads(settings) -> bool:
    """
    Server-side gate for debug artifact downloads.

    Precedence:
    1) server_allow_debug_downloads (explicit backend control)
    2) ui_allow_debug_downloads (UI toggle fallback)
    3) default: False (secure by default)
    """

    def _as_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return False

    if hasattr(settings, "server_allow_debug_downloads"):
        return _as_bool(getattr(settings, "server_allow_debug_downloads"))

    if hasattr(settings, "ui_allow_debug_downloads"):
        return _as_bool(getattr(settings, "ui_allow_debug_downloads"))

    return False


def _email_enabled(settings) -> bool:
    """
    If Settings has email_enabled, use it. Otherwise, treat presence of smtp_host as 'enabled'.
    """
    if hasattr(settings, "email_enabled"):
        return bool(getattr(settings, "email_enabled"))
    host = getattr(settings, "smtp_host", "") or ""
    return bool(host.strip())


def _select_checks_for_export(
    *,
    qad_obj: Dict[str, Any],
    artifacts: Dict[str, str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Section 6.7:
    Prefer DeepAgents export-ready checks if present; fallback to qad_obj["checks"].

    Returns: (checks_list, export_used_report)
    """
    checks = qad_obj.get("checks") if isinstance(qad_obj, dict) else None
    if not isinstance(checks, list):
        checks = []

    used = {
        "used": "qad_obj.checks",
        "artifact_key": None,
        "path": None,
        "num_checks": len(checks),
    }

    export_ready_path = artifacts.get("agents_export_ready_checks")
    if isinstance(export_ready_path, str) and os.path.exists(export_ready_path):
        try:
            data = _read_json(export_ready_path)
            ready_checks = data.get("checks") if isinstance(data, dict) else None
            if isinstance(ready_checks, list):
                checks = ready_checks
                used = {
                    "used": "agents_export_ready_checks",
                    "artifact_key": "agents_export_ready_checks",
                    "path": export_ready_path,
                    "num_checks": len(checks),
                }
        except Exception:
            pass

    return checks, used


def _get_bool(settings, name: str, default: bool) -> bool:
    v = getattr(settings, name, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return bool(v)


def _get_int(settings, name: str, default: int) -> int:
    v = getattr(settings, name, default)
    try:
        return int(v)
    except Exception:
        return default


def _get_float(settings, name: str, default: float) -> float:
    v = getattr(settings, name, default)
    try:
        return float(v)
    except Exception:
        return default


def _safe_path(v: Any) -> Optional[str]:
    """Return v if it's a string path that exists, else None."""
    if isinstance(v, str) and v and os.path.exists(v):
        return v
    return None


def _dedupe_paths(paths: List[Any]) -> List[str]:
    """Keep only existing string paths, de-dupe while preserving order."""
    seen: set[str] = set()
    out: List[str] = []
    for p in paths:
        sp = _safe_path(p)
        if not sp:
            continue
        if sp in seen:
            continue
        seen.add(sp)
        out.append(sp)
    return out


def _simulate_processing(job_id: str) -> None:
    """
    Section 2 + Section 3 + Section 4.5/6 + Section 5:
    - ingest (PDF/DOCX + OCR fallback)
    - chunk + embed + build FAISS
    - LLM: generate LIST of QAD check definitions (DeepAgents orchestration)
    - export: CSV/XLSX (one row per check definition)
    - email CSV/XLSX (+ optional debug artifacts), update job status with email result
    """
    store = _require_store()
    settings = get_settings()
    store.update(job_id, status="running", message="Ingestion started")

    rec = store.get(job_id)
    if not rec:
        return

    try:
        from app.services.ingest import ingest_contract_to_blocks, persist_ingest_artifacts
        from app.services.chunking import chunk_contract_text
        from app.services.embeddings import get_embedder
        from app.services.faiss_index import build_faiss_index, save_faiss_index, save_chunk_meta
        from app.services.export import write_csv_from_checks, write_xlsx_from_checks

        # Section 6: DeepAgents orchestrator
        from app.deepagents.orchestrator import run_deepagents

        # Email helpers (Section 5)
        from app.services.emailer import EmailConfig, send_job_email

        # -------------------------
        # 1) Ingest
        # -------------------------
        store.update(job_id, message="Extracting text from contract (PDF/DOCX)")
        blocks, report = ingest_contract_to_blocks(rec.input_path, rec.input_filename)

        needs_review_ingest = False
        used_ocr = False
        source_type = None

        if isinstance(report, dict):
            source_type = report.get("source_type")
            used_ocr = bool(report.get("used_ocr", False))
            oq = report.get("ocr_quality") or {}
            needs_review_ingest = bool(oq.get("needs_review", False))

        if source_type == "docx":
            input_reliability = "docx"
            norm_source_type = "docx"
        elif used_ocr:
            input_reliability = "ocr_poor" if needs_review_ingest else "ocr_ok"
            norm_source_type = "pdf"
        else:
            input_reliability = "embedded_text"
            norm_source_type = "pdf"

        store.update(job_id, message="Persisting ingestion artifacts")
        artifacts = persist_ingest_artifacts(rec.work_dir, blocks, report)

        # -------------------------
        # 2) RAG artifacts (chunk + embed + FAISS)
        # -------------------------
        contract_text_path = artifacts.get("contract_text")
        if not contract_text_path or not os.path.exists(contract_text_path):
            raise RuntimeError("Missing contract_text artifact; cannot build RAG artifacts.")

        store.update(job_id, message="Building RAG artifacts (chunking + embeddings + FAISS)")
        contract_text = _read_text(contract_text_path)

        chunks = chunk_contract_text(
            contract_text,
            max_tokens=650,
            overlap_tokens=120,
            source_name=rec.input_filename,
            source_type=norm_source_type,
        )
        if not chunks:
            raise RuntimeError("Chunking produced 0 chunks. Check ingestion output (contract_text.txt).")

        store.update(job_id, message=f"Embedding {len(chunks)} chunks")
        embedder = get_embedder()
        embeddings = embedder.embed_many([c["text"] for c in chunks])

        store.update(job_id, message="Building FAISS index")
        index = build_faiss_index(embeddings)

        artifacts_dir = os.path.join(rec.work_dir, "artifacts")
        ensure_dir(artifacts_dir)

        chunks_path = os.path.join(artifacts_dir, "chunks.json")
        faiss_index_path = os.path.join(artifacts_dir, "faiss.index")
        chunk_meta_path = os.path.join(artifacts_dir, "chunk_meta.json")

        with open(chunks_path, "w", encoding="utf-8") as f:
            json.dump(chunks, f, indent=2, ensure_ascii=False)

        meta: List[Dict[str, Any]] = []
        for c in chunks:
            meta.append(
                {
                    "chunk_id": c["id"],
                    "text": c.get("text", "") or "",
                    "token_estimate": c.get("token_estimate"),
                    "source_name": c.get("source_name") or rec.input_filename,
                    "page_num": c.get("page_num"),
                    "source_type": c.get("source_type") or norm_source_type,
                    "block_kind": c.get("block_kind") or ("ocr_page" if used_ocr else "pdf_text"),
                }
            )

        save_chunk_meta(meta, chunk_meta_path)
        save_faiss_index(index, faiss_index_path)

        artifacts.update(
            {
                "chunks": chunks_path,
                "faiss_index": faiss_index_path,
                "chunk_meta": chunk_meta_path,
            }
        )

        # -------------------------
        # 3) LLM mapping (Section 6: DeepAgents) + Run Config (6.8)
        # -------------------------
        store.update(job_id, message="Generating QAD check definitions (LLM)")
        contract_metadata = {
            "source_name": rec.input_filename,
            "program": rec.program,
            "contract_id": rec.contract_id,
            "contract_version": rec.contract_version,
            "input_reliability": input_reliability,
        }

        # HITL reruns: feed user feedback back into the generation loop
        if getattr(rec, "user_feedback", None):
            contract_metadata["user_feedback"] = rec.user_feedback
        if getattr(rec, "rerun_of", None):
            contract_metadata["rerun_of"] = rec.rerun_of
            contract_metadata["rerun_iteration"] = getattr(rec, "rerun_iteration", 0)

        run_config: Dict[str, Any] = {
            "planner_enabled": _get_bool(settings, "deepagents_planner_enabled", True),
            "planner_temperature": _get_float(settings, "deepagents_planner_temperature", 0.0),
            "areas_cap": None,
            "repair_enabled": _get_bool(settings, "deepagents_repair_enabled", True),
            "quality_enabled": _get_bool(settings, "deepagents_quality_enabled", True),
            "temperature": _get_float(settings, "deepagents_temperature", 0.0),
            "export_policy": {
                "sort_by_score_desc": _get_bool(settings, "deepagents_export_sort_desc", True),
                "drop_bad": _get_bool(settings, "deepagents_export_drop_bad", False),
                "drop_empty": _get_bool(settings, "deepagents_export_drop_empty", False),
                "min_score": _get_int(settings, "deepagents_export_min_score", 0),
                "max_checks": None,
            },
        }

        cap = getattr(settings, "deepagents_areas_cap", None)
        try:
            if cap is not None and str(cap).strip() != "":
                run_config["areas_cap"] = int(cap)
        except Exception:
            run_config["areas_cap"] = None

        max_checks = getattr(settings, "deepagents_export_max_checks", None)
        try:
            if max_checks is not None and str(max_checks).strip() != "":
                run_config["export_policy"]["max_checks"] = int(max_checks)
        except Exception:
            run_config["export_policy"]["max_checks"] = None

        agents_dir = os.path.join(artifacts_dir, "agents")
        ensure_dir(agents_dir)

        run_cfg_path = os.path.join(agents_dir, "run_config.json")
        with open(run_cfg_path, "w", encoding="utf-8") as f:
            json.dump(run_config, f, indent=2, ensure_ascii=False)
        artifacts["agents_run_config"] = run_cfg_path

        qad_obj, agent_artifacts = run_deepagents(
            job_id=job_id,
            work_dir=rec.work_dir,
            job_artifacts=artifacts,
            contract_metadata=contract_metadata,
            temperature=float(run_config.get("temperature", 0.0) or 0.0),
            run_config=run_config,
        )
        if isinstance(agent_artifacts, dict):
            # Only merge file-path artifacts (avoid injecting dicts into artifacts)
            for k, v in agent_artifacts.items():
                if isinstance(k, str) and isinstance(v, str):
                    artifacts[k] = v

        qad_json_path = os.path.join(artifacts_dir, "qad_checks.json")
        with open(qad_json_path, "w", encoding="utf-8") as f:
            json.dump(qad_obj, f, indent=2, ensure_ascii=False)
        artifacts["qad_json"] = qad_json_path

        needs_review_llm = False
        try:
            top_notes = (qad_obj.get("notes") or {}) if isinstance(qad_obj, dict) else {}
            needs_review_llm = bool(top_notes.get("needs_sme_review", False))
        except Exception:
            needs_review_llm = False

        needs_review = bool(needs_review_ingest or needs_review_llm)

        # -------------------------
        # 4) Exports (CSV/XLSX) — uses export-ready checks if available (6.7)
        # -------------------------
        store.update(job_id, message="Exporting CSV/XLSX (one row per check definition)")

        export_checks, export_used = _select_checks_for_export(qad_obj=qad_obj, artifacts=artifacts)

        export_used_path = os.path.join(agents_dir, "export_used.json")
        with open(export_used_path, "w", encoding="utf-8") as f:
            json.dump(export_used, f, indent=2, ensure_ascii=False)
        artifacts["agents_export_used"] = export_used_path

        export_obj = {
            "checks": export_checks,
            "notes": (qad_obj.get("notes") or {}) if isinstance(qad_obj, dict) else {},
            "_debug": {"export_used": export_used},
        }

        csv_path = os.path.join(artifacts_dir, "qad_definition.csv")
        xlsx_path = os.path.join(artifacts_dir, "qad_definition.xlsx")

        write_csv_from_checks(export_obj, csv_path)
        write_xlsx_from_checks(export_obj, xlsx_path)

        artifacts.update({"qad_csv": csv_path, "qad_excel": xlsx_path})

        # -------------------------
        # 5) Email (Section 5) — NON-FATAL
        # -------------------------
        store.update(job_id, message="Sending email with outputs (CSV/XLSX)")

        email_enabled_flag = _email_enabled(settings)
        email_ok = False
        email_status = "disabled" if not email_enabled_flag else "failed"
        email_error: Optional[str] = None
        email_result: Dict[str, Any] = {}

        if email_enabled_flag:
            try:
                email_cfg = EmailConfig(
                    smtp_host=getattr(settings, "smtp_host", ""),
                    smtp_port=getattr(settings, "smtp_port", 25),
                    smtp_use_tls=bool(getattr(settings, "smtp_use_tls", False)),
                    smtp_username=getattr(settings, "smtp_username", None),
                    smtp_password=getattr(settings, "smtp_password", None),
                    smtp_from=getattr(settings, "smtp_from", ""),
                    email_enabled=bool(getattr(settings, "email_enabled", email_enabled_flag)),
                    max_attachment_mb=float(getattr(settings, "email_max_attachment_mb", 20)),
                )

                attachments_raw: List[Any] = [csv_path, xlsx_path]

                attach_debug = bool(getattr(settings, "email_attach_debug_artifacts", False))
                if attach_debug:
                    # Include a few useful debug artifacts IF they exist as real files
                    attachments_raw.extend(
                        [
                            qad_json_path,
                            artifacts.get("ingest_report"),
                            artifacts.get("chunks"),
                            artifacts.get("agents_run_config"),
                            artifacts.get("agents_export_used"),
                        ]
                    )

                attachments = _dedupe_paths(attachments_raw)

                subject = f"QAD Intake Results — {rec.input_filename} — Job {job_id}"
                body_lines = [
                    f"Job ID: {job_id}",
                    f"Input: {rec.input_filename}",
                    f"Overall Status: {'NEEDS SME REVIEW' if needs_review else 'OK'}",
                    "",
                    "Attached outputs:",
                    "- qad_definition.csv (one row per check definition)",
                    "- qad_definition.xlsx (one row per check definition)",
                ]
                if attach_debug:
                    body_lines.append("- (debug) selected artifacts included when present")

                try:
                    notes = (qad_obj.get("notes") or {}) if isinstance(qad_obj, dict) else {}
                    qs = notes.get("questions_for_sme") or []
                    mf = notes.get("missing_fields") or []
                    if mf:
                        body_lines.append("")
                        body_lines.append(f"Missing fields (top-level): {', '.join(map(str, mf[:25]))}")
                    if qs:
                        body_lines.append("")
                        body_lines.append("Questions for SME (top-level):")
                        for q in qs[:15]:
                            body_lines.append(f"- {q}")
                except Exception:
                    pass

                email_result = send_job_email(
                    cfg=email_cfg,
                    to_raw=rec.email,
                    subject=subject,
                    body="\n".join(body_lines),
                    attachments=attachments,
                    extra_headers={"X-QAD-Job-ID": job_id},
                ) or {}

                email_ok = bool(email_result.get("ok", False))
                email_status = "sent" if email_ok else "failed"
                email_error = email_result.get("error")

            except Exception as e:
                # IMPORTANT: do NOT fail the whole job due to email issues
                email_ok = False
                email_status = "failed"
                email_error = str(e)
                log.exception("Email sending failed (non-fatal): %s", e)
        else:
            # disabled by config
            attach_debug = bool(getattr(settings, "email_attach_debug_artifacts", False))

        # -------------------------
        # Final job status/message
        # -------------------------
        final_status = "completed_with_warnings" if needs_review else "completed"
        if not email_ok:
            final_status = "completed_with_warnings"

        final_message = (
            ("Completed with warnings. " if final_status == "completed_with_warnings" else "Completed. ")
            + ("OCR quality warnings. " if needs_review_ingest else "")
            + ("LLM flagged SME review. " if needs_review_llm else "")
        ).strip()

        store.update(
            job_id,
            status=final_status,
            message=final_message,
            artifacts=artifacts,
            metadata={
                "source_name": rec.input_filename,
                "program": rec.program,
                "contract_id": rec.contract_id,
                "contract_version": rec.contract_version,
                "needs_review": needs_review,
                "input_reliability": input_reliability,
                "ingest_report": report,
                "needs_review_ingest": needs_review_ingest,
                "needs_review_llm": needs_review_llm,
                "num_checks": len(qad_obj.get("checks") or []) if isinstance(qad_obj, dict) else None,
                "email_ok": email_ok,
                "email_status": email_status,
                "email_error": email_error,
                "email_recipients": email_result.get("recipients"),
                "email_attached_files": email_result.get("attached_files"),

                # HITL
                "review_status": "pending",
                "review_feedback": None,
            },
        )

    except Exception as e:
        import traceback
        store.update(job_id, status="failed", error=traceback.format_exc(), message="Processing failed")



@router.post("/upload", response_model=UploadResponse)
async def upload_contract(
    background_tasks: BackgroundTasks,
    contract: UploadFile = File(...),
    email: str = Form(...),
    program: Optional[str] = Form(None),
    contract_id: Optional[str] = Form(None),
    contract_version: Optional[str] = Form(None),
):
    settings = get_settings()
    store = _require_store()

    if not email.strip():
        raise HTTPException(status_code=400, detail="Email is required.")

    filename = safe_filename(contract.filename or "contract.bin")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in {".pdf", ".docx"}:
        raise HTTPException(status_code=400, detail="Only .pdf and .docx files are supported.")

    contents = await contract.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(contents) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File too large. Max {settings.max_upload_mb} MB.")

    job_id = new_job_id()
    job_root = os.path.join(settings.tmp_dir, "jobs", job_id)
    ensure_dir(job_root)

    input_path = os.path.join(job_root, filename)
    write_bytes(input_path, contents)

    store.create_job(
        job_id=job_id,
        work_dir=job_root,
        input_filename=filename,
        input_path=input_path,
        email=email.strip(),
        program=(program.strip() if program else None),
        contract_id=(contract_id.strip() if contract_id else None),
        contract_version=(contract_version.strip() if contract_version else None),
    )

    log.info("Job created: %s (%s)", job_id, filename)

    background_tasks.add_task(_simulate_processing, job_id)
    return UploadResponse(job_id=job_id)


@router.get("/job-status/{job_id}", response_model=JobStatusResponse)
def job_status(job_id: str):
    store = _require_store()
    rec = store.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job not found")

    artifacts = rec.artifacts or {}
    artifact_keys = {k: os.path.basename(v) for k, v in artifacts.items() if isinstance(v, str)}

    md = _read_metadata(rec)

    return JobStatusResponse(
        job_id=rec.job_id,
        status=rec.status,
        message=rec.message,
        error=rec.error,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
        artifacts=artifact_keys,
        email_status=md.get("email_status"),
        email_error=md.get("email_error"),
        review_status=md.get("review_status"),
        review_feedback=md.get("review_feedback"),
        rerun_of=getattr(rec, "rerun_of", None),
        rerun_iteration=getattr(rec, "rerun_iteration", None),
    )


@router.get("/job-result/{job_id}", response_model=JobResultResponse)
def job_result(job_id: str):
    store = _require_store()
    rec = store.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobResultResponse(
        job_id=rec.job_id,
        status=rec.status,
        artifacts=rec.artifacts,
        metadata={
            "email": rec.email,
            "program": rec.program,
            "contract_id": rec.contract_id,
            "contract_version": rec.contract_version,
            "input_filename": rec.input_filename,
        },
    )


@router.get("/download/{job_id}/{artifact_key}")
def download_artifact(job_id: str, artifact_key: str):
    """
    Downloads a known artifact by key.
    Server-side enforces debug gating for advanced artifacts.
    """
    settings = get_settings()
    store = _require_store()
    rec = store.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job not found")

    if _is_debug_artifact(artifact_key) and not _server_allows_debug_downloads(settings):
        raise HTTPException(status_code=403, detail="Debug artifact downloads are disabled.")

    artifacts = rec.artifacts or {}
    path = artifacts.get(artifact_key)
    if not path or not isinstance(path, str) or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Artifact not found")

    return FileResponse(path, filename=os.path.basename(path))


@router.post("/jobs/{job_id}/retrieve")
def retrieve(job_id: str, req: RetrieveRequest):
    store = _require_store()
    rec = store.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job not found")

    artifacts = rec.artifacts or {}
    index_path = artifacts.get("faiss_index")
    meta_path = artifacts.get("chunk_meta")

    if not index_path or not meta_path:
        raise HTTPException(status_code=400, detail="RAG artifacts missing. Upload a contract first.")

    if not os.path.exists(index_path) or not os.path.exists(meta_path):
        raise HTTPException(status_code=400, detail="RAG artifacts not found on disk. Re-run the job.")

    from app.services.embeddings import get_embedder
    from app.services.faiss_index import load_faiss_index, load_chunk_meta, search

    embedder = get_embedder()
    qvec = embedder.embed(req.query)

    index = load_faiss_index(index_path)
    meta = load_chunk_meta(meta_path)

    hits = search(index, qvec, meta, k=req.k)
    return {"job_id": job_id, "query": req.query, "k": req.k, "hits": hits}


@router.post("/jobs/{job_id}/review", response_model=ReviewResponse)
def submit_review(job_id: str, req: ReviewRequest):
    """Human-in-the-loop decision on the generated output."""
    store = _require_store()
    rec = store.get(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Job not found")

    decision = (req.decision or "").strip().lower()
    if decision not in {"approve", "approved", "reject", "rejected"}:
        raise HTTPException(status_code=400, detail="decision must be approve or reject")

    status = "approved" if decision.startswith("app") else "rejected"
    md = _read_metadata(rec)
    md["review_status"] = status
    md["review_feedback"] = (req.feedback or "").strip() or None
    md["reviewed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_metadata(rec, md)

    # Light status message for UI
    msg = "Approved by user." if status == "approved" else "Rejected by user."
    store.update(job_id, message=msg)

    return ReviewResponse(job_id=job_id, ok=True, review_status=status)


@router.post("/jobs/{job_id}/rerun", response_model=RerunResponse)
def rerun_job(background_tasks: BackgroundTasks, job_id: str, req: RerunRequest):
    """Create a new job using the same input file, and feed user feedback into the next run."""
    store = _require_store()
    prev = store.get(job_id)
    if not prev:
        raise HTTPException(status_code=404, detail="Job not found")

    # Create a new job dir and copy the original input file into it
    new_id = new_job_id()
    work_dir = os.path.join(get_settings().tmp_dir, "jobs", new_id)
    ensure_dir(work_dir)

    # Copy input bytes
    in_path = os.path.join(work_dir, safe_filename(prev.input_filename))
    with open(prev.input_path, "rb") as f:
        write_bytes(in_path, f.read())

    # Determine iteration
    prev_iter = int(getattr(prev, "rerun_iteration", 0) or 0)
    base = getattr(prev, "rerun_of", None) or job_id
    new_iter = prev_iter + 1 if getattr(prev, "rerun_of", None) else 1

    rec = store.create_job(
        job_id=new_id,
        work_dir=work_dir,
        input_path=in_path,
        input_filename=prev.input_filename,
        email=prev.email,
        program=prev.program,
        contract_id=prev.contract_id,
        contract_version=prev.contract_version,
        rerun_of=base,
        rerun_iteration=new_iter,
        user_feedback=(req.feedback or "").strip() or None,
    )

    # Seed metadata for provenance
    _write_metadata(
        rec,
        {
            "source_name": prev.input_filename,
            "rerun_of": base,
            "rerun_iteration": new_iter,
            "user_feedback": (req.feedback or "").strip() or None,
            "review_status": "pending",
        },
    )

    background_tasks.add_task(_simulate_processing, new_id)
    return RerunResponse(job_id=job_id, new_job_id=new_id)
