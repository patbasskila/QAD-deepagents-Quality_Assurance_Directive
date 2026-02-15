# app/services/cleanup.py
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from typing import Dict, Any


@dataclass(frozen=True)
class CleanupConfig:
    tmp_dir: str
    retention_days: int


def cleanup_old_jobs(cfg: CleanupConfig) -> Dict[str, Any]:
    """
    Deletes job folders older than retention_days under {tmp_dir}/jobs.
    Returns summary stats.
    """
    jobs_root = os.path.join(cfg.tmp_dir, "jobs")
    if cfg.retention_days <= 0:
        return {"ok": True, "deleted": 0, "skipped": 0, "jobs_root": jobs_root, "note": "Retention disabled (<=0)."}

    if not os.path.isdir(jobs_root):
        return {"ok": True, "deleted": 0, "skipped": 0, "jobs_root": jobs_root, "note": "No jobs dir found."}

    cutoff = time.time() - (cfg.retention_days * 86400)

    deleted = 0
    skipped = 0

    for name in os.listdir(jobs_root):
        p = os.path.join(jobs_root, name)
        if not os.path.isdir(p):
            skipped += 1
            continue

        try:
            mtime = os.path.getmtime(p)
            if mtime < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                deleted += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1

    return {"ok": True, "deleted": deleted, "skipped": skipped, "jobs_root": jobs_root}
