import json
import os
from typing import List, Dict, Any

import faiss
import numpy as np

from app.utils.files import ensure_dir


def build_faiss_index(embeddings: np.ndarray) -> faiss.IndexFlatL2:
    """
    embeddings: (N, D) float32
    """
    if embeddings is None or len(embeddings) == 0:
        raise ValueError("Cannot build FAISS index: embeddings is empty.")

    if embeddings.dtype != np.float32:
        embeddings = embeddings.astype(np.float32)

    if embeddings.ndim != 2:
        raise ValueError(f"Embeddings must be 2D (N,D). Got shape={embeddings.shape}")

    d = embeddings.shape[1]
    index = faiss.IndexFlatL2(d)
    index.add(embeddings)
    return index


def save_faiss_index(index: faiss.IndexFlatL2, path: str) -> None:
    ensure_dir(os.path.dirname(path))
    faiss.write_index(index, path)


def load_faiss_index(path: str) -> faiss.IndexFlatL2:
    return faiss.read_index(path)


def save_chunk_meta(meta: List[Dict[str, Any]], path: str) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


def load_chunk_meta(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def search(
    index: faiss.IndexFlatL2,
    query_vec: np.ndarray,
    meta: List[Dict[str, Any]],
    k: int = 6,
) -> List[Dict[str, Any]]:
    if not meta:
        return []

    k = max(1, min(int(k), len(meta)))

    if query_vec.ndim == 1:
        q = query_vec.reshape(1, -1).astype(np.float32)
    else:
        q = query_vec.astype(np.float32)

    D, I = index.search(q, k)
    hits: List[Dict[str, Any]] = []

    for dist, idx in zip(D[0].tolist(), I[0].tolist()):
        if idx < 0 or idx >= len(meta):
            continue
        row = dict(meta[idx])
        row["score_l2"] = float(dist)
        hits.append(row)

    return hits
