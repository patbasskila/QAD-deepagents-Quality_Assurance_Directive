# app/services/llm/json_contract.py
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from app.services.embeddings import EmbeddingsClient, get_embedder
from app.services.faiss_index import load_faiss_index, load_chunk_meta, search as faiss_search
from app.services.qad_schema import (
    get_sharepoint_qad_schema,
    schema_to_prompt_table,
    schema_keys,
    required_field_keys,
)
from app.services.llm.client import chat_json
from app.services.llm.prompts import SYSTEM_QAD_MAPPER, build_user_prompt

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> Dict[str, Any]:
    """Best-effort JSON extraction."""
    t = (text or "").strip()
    t = t.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(t)
    except Exception:
        pass

    m = _JSON_RE.search(t)
    if not m:
        raise ValueError("Model did not return valid JSON.")
    return json.loads(m.group(0))


def _mk_citation_label(h: Dict[str, Any]) -> str:
    """Human-friendly citation string for reviewers."""
    src = (h.get("source_name") or "").strip() or "contract"
    page = h.get("page_num")
    kind = (h.get("block_kind") or "").strip()
    if page is not None:
        return f"{src} p.{page} ({kind})" if kind else f"{src} p.{page}"
    return f"{src} ({kind})" if kind else src


def _short_quote(text: str, max_chars: int = 350) -> str:
    t = (text or "").strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def _build_excerpts_for_schema(
    job_artifacts: Dict[str, str],
    *,
    schema_hints: List[str],
    embedder: EmbeddingsClient,
    k_per_hint: int = 2,
    max_total: int = 18,
) -> List[Dict[str, Any]]:
    """
    Retrieve relevant chunks from FAISS using schema hint queries.
    """
    index_path = job_artifacts.get("faiss_index")
    meta_path = job_artifacts.get("chunk_meta")
    if not index_path or not meta_path:
        raise RuntimeError("Missing FAISS artifacts. Ensure Section 3 generated faiss_index + chunk_meta.")

    index = load_faiss_index(index_path)
    meta = load_chunk_meta(meta_path)

    picked: List[Dict[str, Any]] = []
    seen_chunk_ids = set()

    for q in schema_hints:
        qvec = embedder.embed(q)
        hits = faiss_search(index=index, query_vec=qvec, meta=meta, k=k_per_hint)

        for h in hits:
            cid = h.get("chunk_id")
            if not cid or cid in seen_chunk_ids:
                continue
            seen_chunk_ids.add(cid)

            hit = {
                "chunk_id": cid,
                "text": h.get("text", "") or "",
                "source_name": h.get("source_name", "") or "",
                "page_num": h.get("page_num"),
                "block_kind": h.get("block_kind"),
            }
            hit["citation"] = _mk_citation_label(hit)
            hit["quote"] = _short_quote(hit["text"])

            picked.append(hit)
            if len(picked) >= max_total:
                return picked

    return picked


def generate_qad_checks_json(
    *,
    job_artifacts: Dict[str, str],
    contract_metadata: Dict[str, Any],
    temperature: float = 0.0,
) -> Dict[str, Any]:
    """
    Section 4.5:
      - load SharePoint gold schema (column keys)
      - retrieve relevant excerpts (page-aware citations)
      - call LLM to generate a LIST of check definitions
      - enforce shape + required key presence
    """
    schema = get_sharepoint_qad_schema()
    keys = schema_keys(schema)
    schema_table = schema_to_prompt_table(schema)

    # Build retrieval hints (dedupe)
    hints: List[str] = []
    seen = set()
    for f in schema:
        for h in (f.retrieval_hints or [f.display_name]):
            hh = (h or "").strip()
            if not hh or hh in seen:
                continue
            seen.add(hh)
            hints.append(hh)

    # Keep retrieval lightweight
    hints = hints[:40]

    embedder = get_embedder()
    excerpts = _build_excerpts_for_schema(job_artifacts, schema_hints=hints, embedder=embedder)

    prompt_payload = {
        "schema_table": schema_table,
        "schema_keys": keys,
        "contract_metadata": contract_metadata,
        "excerpts": [
            {
                "chunk_id": e["chunk_id"],
                "citation": e["citation"],
                "page_num": e.get("page_num"),
                "source_name": e.get("source_name"),
                "block_kind": e.get("block_kind"),
                "quote": e.get("quote"),
            }
            for e in excerpts
        ],
    }

    user_prompt = build_user_prompt(prompt_payload)
    raw = chat_json(SYSTEM_QAD_MAPPER, user_prompt, temperature=temperature)
    obj = _extract_json(raw)

    # -----------------------------
    # Shape enforcement
    # -----------------------------
    checks = obj.get("checks")
    top_notes = obj.get("notes") if isinstance(obj.get("notes"), dict) else {}

    if not isinstance(checks, list) or len(checks) == 0:
        # Fail soft: produce one empty check so downstream export works.
        checks = [
            {
                "columns": {k: None for k in keys},
                "evidence": {k: [] for k in keys},
                "notes": {
                    "needs_sme_review": True,
                    "missing_fields": keys[:],
                    "questions_for_sme": ["Model returned no checks; re-run or review retrieval excerpts."],
                },
            }
        ]
        top_notes = {
            "needs_sme_review": True,
            "missing_fields": keys[:],
            "questions_for_sme": ["Model returned no checks; re-run or review retrieval excerpts."],
        }

    req_keys = required_field_keys(schema)

    normalized_checks: List[Dict[str, Any]] = []
    any_needs_review = False
    missing_union: set[str] = set()
    questions_union: List[str] = []

    for chk in checks:
        cols = chk.get("columns") if isinstance(chk, dict) else None
        ev = chk.get("evidence") if isinstance(chk, dict) else None
        notes = chk.get("notes") if isinstance(chk, dict) else None

        if not isinstance(cols, dict):
            cols = {}
        if not isinstance(ev, dict):
            ev = {}
        if not isinstance(notes, dict):
            notes = {}

        # Ensure all keys exist
        for k in keys:
            cols.setdefault(k, None)

        # Evidence: if value present but evidence missing, force empty list
        for k, v in cols.items():
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            if not isinstance(ev.get(k), list) or len(ev.get(k)) == 0:
                ev[k] = []

        missing_required = [k for k in req_keys if cols.get(k) in (None, "", [])]
        notes.setdefault("missing_fields", [])
        notes.setdefault("questions_for_sme", [])
        needs = bool(notes.get("needs_sme_review", False))

        if missing_required:
            needs = True
            notes["needs_sme_review"] = True
            notes["missing_fields"] = sorted(set(list(notes.get("missing_fields", [])) + missing_required))

        any_needs_review = any_needs_review or bool(needs)

        for mf in (notes.get("missing_fields") or []):
            if isinstance(mf, str) and mf:
                missing_union.add(mf)

        for q in (notes.get("questions_for_sme") or []):
            if isinstance(q, str) and q and q not in questions_union:
                questions_union.append(q)

        normalized_checks.append({"columns": cols, "evidence": ev, "notes": notes})

    if not isinstance(top_notes, dict):
        top_notes = {}
    top_notes.setdefault("missing_fields", [])
    top_notes.setdefault("questions_for_sme", [])
    if any_needs_review:
        top_notes["needs_sme_review"] = True
    if missing_union:
        top_notes["missing_fields"] = sorted(set(list(top_notes.get("missing_fields", [])) + list(missing_union)))
    if questions_union:
        top_notes["questions_for_sme"] = list(
            dict.fromkeys(list(top_notes.get("questions_for_sme", [])) + questions_union)
        )

    out = {
        "checks": normalized_checks,
        "notes": top_notes,
        "_debug": {
            "excerpts_provided": [e["chunk_id"] for e in excerpts],
            "citations_provided": [e["citation"] for e in excerpts],
        },
    }
    return out
