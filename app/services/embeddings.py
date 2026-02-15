"""app/services/embeddings.py

Embeddings provider abstraction.

Supports two modes:
- Remote Hugging Face embeddings via HTTP (pip-only; no local model weights).
- Local embeddings via sentence-transformers or transformers (optional dependencies).

Design goals:
- Keep FAISS pipeline stable.
- Avoid importing heavy deps unless local embeddings are selected.
- Make provider selection purely config-driven.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

from app.utils.config import get_settings


def _truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _mean_pool(arr: np.ndarray) -> np.ndarray:
    """Mean-pool token embeddings (T, D) -> (D,)."""
    if arr.ndim == 1:
        return arr
    if arr.ndim == 2:
        return arr.mean(axis=0)
    # e.g. (1, T, D)
    if arr.ndim == 3 and arr.shape[0] == 1:
        return arr[0].mean(axis=0)
    raise ValueError(f"Unexpected embedding shape: {arr.shape}")


@dataclass
class EmbeddingResult:
    vectors: np.ndarray  # (N, D) float32


class EmbeddingsClient:
    """Unified interface for embedding text for retrieval/FAISS."""

    def embed(self, text: str) -> np.ndarray:
        raise NotImplementedError

    def embed_many(self, texts: Sequence[str]) -> np.ndarray:
        raise NotImplementedError


class HFRemoteEmbeddingsClient(EmbeddingsClient):
    """Remote embeddings via Hugging Face Inference API / Inference Endpoints."""

    def __init__(self, *, model: str, token: str = "", endpoint: str = ""):
        import requests  # lightweight

        self._requests = requests
        self._model = model.strip()
        self._token = token.strip()
        self._endpoint = endpoint.strip()

        if not self._endpoint:
            # Feature extraction pipeline endpoint
            self._endpoint = f"https://api-inference.huggingface.co/pipeline/feature-extraction/{self._model}"

        if not self._endpoint.startswith("http"):
            raise ValueError("HF embedding endpoint must be a URL")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _post(self, payload: dict) -> object:
        # very small retry loop for transient 5xx / rate limits
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                resp = self._requests.post(self._endpoint, headers=self._headers(), json=payload, timeout=90)
                if resp.status_code >= 500 or resp.status_code == 429:
                    time.sleep(0.8 * attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_err = e
                time.sleep(0.6 * attempt)
        raise RuntimeError(f"Hugging Face embeddings request failed: {last_err}")

    def embed_many(self, texts: Sequence[str]) -> np.ndarray:
        # HF supports either single string or list in `inputs` depending on backend.
        clean = [(t or "").strip() for t in texts]
        clean = [t for t in clean if t]
        if not clean:
            return np.zeros((0, 0), dtype=np.float32)

        data = self._post({"inputs": clean})

        # Common returns:
        # - List[List[float]]  (already pooled)
        # - List[List[List[float]]] (per-token)
        # - List[List[List[List[float]]]] (batched per-token)

        # Normalize into list of (D,) vectors.
        vecs: List[np.ndarray] = []

        if isinstance(data, list) and data and isinstance(data[0], (int, float)):
            # Single embedding (D,) returned for a single input
            vecs = [np.array(data, dtype=np.float32)]
        elif isinstance(data, list) and data and isinstance(data[0], list):
            # Possibly batched
            for item in data:
                arr = np.array(item, dtype=np.float32)
                vecs.append(_mean_pool(arr).astype(np.float32))
        else:
            raise RuntimeError(f"Unexpected HF embeddings response shape/type: {type(data)}")

        out = np.vstack([v.reshape(1, -1) for v in vecs]).astype(np.float32)
        return out

    def embed(self, text: str) -> np.ndarray:
        embs = self.embed_many([text])
        if embs.size == 0:
            return np.zeros((0,), dtype=np.float32)
        return embs[0]


class LocalEmbeddingsClient(EmbeddingsClient):
    """Local embeddings using optional dependencies.

    Prefers sentence-transformers if installed, else falls back to transformers+torch.

    If `local_files_only=True`, the model must already exist on disk (cache or explicit path).
    """

    def __init__(
        self,
        *,
        model: str,
        device: str = "cpu",
        local_files_only: bool = False,
        cache_dir: str = "",
        backend: str = "auto",  # auto|sentence_transformers|transformers
    ):
        self._model_id = model.strip()
        self._device = (device or "cpu").strip()
        self._local_only = bool(local_files_only)
        self._cache_dir = cache_dir.strip() or None
        self._backend = (backend or "auto").strip().lower()

        # Allow users to override the OpenMP duplicate lib issue in certain Windows setups.
        if _truthy(os.environ.get("KMP_DUPLICATE_LIB_OK")):
            os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

        self._st = None
        self._tok = None
        self._tfm = None

        if self._backend in {"auto", "sentence_transformers"}:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore

                self._st = SentenceTransformer(
                    self._model_id,
                    device=self._device,
                    cache_folder=self._cache_dir,
                )
                return
            except Exception:
                if self._backend == "sentence_transformers":
                    raise

        # Fallback to transformers mean pooling
        try:
            import torch  # type: ignore
            from transformers import AutoTokenizer, AutoModel  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "Local embeddings require either sentence-transformers OR transformers+torch. "
                f"Missing dependency: {e}"
            )

        self._torch = torch
        self._tok = AutoTokenizer.from_pretrained(
            self._model_id,
            local_files_only=self._local_only,
            cache_dir=self._cache_dir,
        )
        self._tfm = AutoModel.from_pretrained(
            self._model_id,
            local_files_only=self._local_only,
            cache_dir=self._cache_dir,
        )
        self._tfm.eval()
        self._tfm.to(self._device)

    def embed_many(self, texts: Sequence[str]) -> np.ndarray:
        clean = [(t or "").strip() for t in texts]
        clean = [t for t in clean if t]
        if not clean:
            return np.zeros((0, 0), dtype=np.float32)

        if self._st is not None:
            vecs = self._st.encode(list(clean), convert_to_numpy=True, normalize_embeddings=False)
            return np.asarray(vecs, dtype=np.float32)

        assert self._tok is not None and self._tfm is not None
        torch = self._torch

        # Simple batching
        all_vecs: List[np.ndarray] = []
        batch = 16
        for i in range(0, len(clean), batch):
            chunk = clean[i : i + batch]
            inputs = self._tok(
                chunk,
                return_tensors="pt",
                truncation=True,
                padding=True,
                max_length=512,
            )
            inputs = {k: v.to(self._device) for k, v in inputs.items()}
            with torch.no_grad():
                out = self._tfm(**inputs)
                pooled = out.last_hidden_state.mean(dim=1)  # (B, D)
                vecs = pooled.detach().cpu().numpy().astype(np.float32)
                all_vecs.append(vecs)

        return np.vstack(all_vecs).astype(np.float32)

    def embed(self, text: str) -> np.ndarray:
        embs = self.embed_many([text])
        if embs.size == 0:
            return np.zeros((0,), dtype=np.float32)
        return embs[0]


_singleton: Optional[EmbeddingsClient] = None


def get_embedder() -> EmbeddingsClient:
    """Factory returning a process-wide singleton embedder."""
    global _singleton
    if _singleton is not None:
        return _singleton

    s = get_settings()

    provider = (getattr(s, "embeddings_provider", "huggingface") or "huggingface").strip().lower()

    if provider in {"hf", "huggingface", "remote"}:
        _singleton = HFRemoteEmbeddingsClient(
            model=getattr(s, "hf_embed_model", "sentence-transformers/all-MiniLM-L6-v2"),
            token=getattr(s, "hf_token", ""),
            endpoint=getattr(s, "hf_embed_endpoint", ""),
        )
        return _singleton

    if provider in {"local", "offline"}:
        _singleton = LocalEmbeddingsClient(
            model=getattr(s, "local_embed_model", "sentence-transformers/all-MiniLM-L6-v2"),
            device=getattr(s, "local_embed_device", "cpu"),
            local_files_only=bool(getattr(s, "local_embed_local_files_only", False)),
            cache_dir=getattr(s, "local_embed_cache_dir", ""),
            backend=getattr(s, "local_embed_backend", "auto"),
        )
        return _singleton

    raise RuntimeError("EMBEDDINGS_PROVIDER must be one of: huggingface, local")
