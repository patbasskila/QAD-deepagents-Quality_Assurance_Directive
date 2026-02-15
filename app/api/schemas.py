# app/api/schemas.py
from __future__ import annotations

from typing import Optional, Dict
from pydantic import BaseModel


class UploadResponse(BaseModel):
    job_id: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    message: Optional[str] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str
    artifacts: Dict[str, str]

    email_status: Optional[str] = None   # "sent" | "failed" | "disabled" | None
    email_error: Optional[str] = None

    # Human-in-the-loop review
    review_status: Optional[str] = None  # "pending" | "approved" | "rejected" | None
    review_feedback: Optional[str] = None
    rerun_of: Optional[str] = None
    rerun_iteration: Optional[int] = None


class JobResultResponse(BaseModel):
    job_id: str
    status: str
    artifacts: Dict[str, str]
    metadata: Dict


class ReviewRequest(BaseModel):
    decision: str  # approve|reject
    feedback: Optional[str] = None


class ReviewResponse(BaseModel):
    job_id: str
    ok: bool
    review_status: str


class RerunRequest(BaseModel):
    feedback: Optional[str] = None


class RerunResponse(BaseModel):
    job_id: str
    new_job_id: str
