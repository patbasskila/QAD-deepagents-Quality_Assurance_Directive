# app/services/emailer.py
from __future__ import annotations

import mimetypes
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Iterable, List, Optional, Dict, Any, Tuple


@dataclass(frozen=True)
class EmailConfig:
    smtp_host: str
    smtp_port: int
    smtp_use_tls: bool
    smtp_username: str
    smtp_password: str
    smtp_from: str
    email_enabled: bool
    max_attachment_mb: int


def _parse_recipients(raw: str) -> List[str]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def _guess_mime(path: str) -> Tuple[str, str]:
    ctype, _ = mimetypes.guess_type(path)
    if not ctype:
        return ("application", "octet-stream")
    major, minor = ctype.split("/", 1)
    return (major, minor)


def _file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def _iter_valid_paths(attachments: Iterable[Any]) -> List[str]:
    """
    Accept only actual filesystem paths (strings).
    Silently skip dicts/None/objects to avoid runtime errors.
    """
    out: List[str] = []
    for p in attachments or []:
        if not isinstance(p, str):
            continue
        p = p.strip()
        if not p:
            continue
        if not os.path.exists(p):
            continue
        out.append(p)
    return out


def send_job_email(
    *,
    cfg: EmailConfig,
    to_raw: str,
    subject: str,
    body: str,
    attachments: Iterable[Any],
    extra_headers: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Sends an email with attachments.
    Returns dict: { ok: bool, error: str|None, recipients: [...], attached_files: [...] }
    """
    recipients = _parse_recipients(to_raw)
    if not recipients:
        return {"ok": False, "error": "No recipients provided.", "recipients": [], "attached_files": []}

    if not cfg.email_enabled:
        return {
            "ok": False,
            "error": "Email is disabled by configuration.",
            "recipients": recipients,
            "attached_files": [],
        }

    msg = EmailMessage()
    msg["From"] = str(cfg.smtp_from or "")
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = str(subject or "")

    if extra_headers:
        for k, v in extra_headers.items():
            if not k:
                continue
            # EmailMessage requires header values to be strings
            msg[str(k)] = "" if v is None else str(v)

    msg.set_content(body or "")

    attached: List[str] = []

    safe_paths = _iter_valid_paths(attachments)

    # enforce size cap + attach
    for path in safe_paths:
        try:
            if _file_size_mb(path) > float(cfg.max_attachment_mb):
                continue

            filename = os.path.basename(path)
            maintype, subtype = _guess_mime(path)
            with open(path, "rb") as f:
                data = f.read()

            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
            attached.append(filename)
        except Exception:
            # skip problematic attachment rather than failing the whole email
            continue

    try:
        if cfg.smtp_use_tls:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
                s.starttls()
                if (cfg.smtp_username or "").strip():
                    s.login(cfg.smtp_username, cfg.smtp_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
                if (cfg.smtp_username or "").strip():
                    s.login(cfg.smtp_username, cfg.smtp_password)
                s.send_message(msg)

        return {"ok": True, "error": None, "recipients": recipients, "attached_files": attached}

    except Exception as e:
        return {"ok": False, "error": repr(e), "recipients": recipients, "attached_files": attached}
