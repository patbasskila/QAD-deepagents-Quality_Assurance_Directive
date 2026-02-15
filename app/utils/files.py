import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def ensure_dir(path: str) -> str:
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(name: str, default: str = "upload.bin") -> str:
    if not name:
        return default
    cleaned = _FILENAME_SAFE.sub("_", name).strip("._")
    return cleaned if cleaned else default


def new_job_id() -> str:
    return str(uuid.uuid4())


def job_dir(tmp_root: str, job_id: str) -> str:
    d = os.path.join(tmp_root, "jobs", job_id)
    ensure_dir(d)
    return d


def write_bytes(path: str, content: bytes) -> None:
    ensure_dir(str(Path(path).parent))
    with open(path, "wb") as f:
        f.write(content)


def write_text(path: str, content: str, encoding: str = "utf-8") -> None:
    ensure_dir(str(Path(path).parent))
    with open(path, "w", encoding=encoding) as f:
        f.write(content)


def read_text(path: str, encoding: str = "utf-8") -> str:
    with open(path, "r", encoding=encoding) as f:
        return f.read()


def cleanup_dir(path: str) -> None:
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
