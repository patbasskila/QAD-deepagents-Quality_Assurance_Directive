# app/deepagents/subagents.py
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from app.services.embeddings import get_embedder
from app.services.faiss_index import load_faiss_index, load_chunk_meta, search as faiss_search
from app.services.llm.client import chat_json

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> Dict[str, Any]:
    """
    Best-effort JSON extraction (mirrors the behavior in services/llm/json_contract.py).
    """
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


def mk_citation_label(hit: Dict[str, Any]) -> str:
    """
    Human-friendly citation string for reviewers.
    """
    src = (hit.get("source_name") or "").strip() or "contract"
    page = hit.get("page_num")
    kind = (hit.get("block_kind") or "").strip()
    if page is not None:
        return f"{src} p.{page} ({kind})" if kind else f"{src} p.{page}"
    return f"{src} ({kind})" if kind else src


def short_quote(text: str, max_chars: int = 350) -> str:
    t = (text or "").strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def retrieve_excerpts(
    *,
    job_artifacts: Dict[str, str],
    queries: List[str],
    k_per_query: int = 2,
    max_total: int = 18,
) -> List[Dict[str, Any]]:
    """
    Retrieve relevant chunks from FAISS using a list of query strings.

    Returns excerpts with stable ids + citation/quote fields:
      {
        "chunk_id": str,
        "text": str,
        "source_name": str,
        "page_num": Optional[int],
        "block_kind": Optional[str],
        "citation": str,
        "quote": str
      }
    """
    index_path = job_artifacts.get("faiss_index")
    meta_path = job_artifacts.get("chunk_meta")
    if not index_path or not meta_path:
        raise RuntimeError("Missing FAISS artifacts. Ensure RAG artifacts exist (faiss_index + chunk_meta).")

    embedder = get_embedder()
    index = load_faiss_index(index_path)
    meta = load_chunk_meta(meta_path)

    picked: List[Dict[str, Any]] = []
    seen = set()

    for q in queries:
        qq = (q or "").strip()
        if not qq:
            continue

        qvec = embedder.embed(qq)
        hits = faiss_search(index=index, query_vec=qvec, meta=meta, k=k_per_query)

        for h in hits:
            cid = h.get("chunk_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)

            hit = {
                "chunk_id": cid,
                "text": h.get("text", "") or "",
                "source_name": h.get("source_name", "") or "",
                "page_num": h.get("page_num"),
                "block_kind": h.get("block_kind"),
            }
            hit["citation"] = mk_citation_label(hit)
            hit["quote"] = short_quote(hit["text"])

            picked.append(hit)
            if len(picked) >= max_total:
                return picked

    return picked


# -----------------------------
# Drafting agent
# -----------------------------
SYSTEM_DRAFT_CHECKS = """You are an expert quality/compliance analyst.
You will draft QAD check definitions from contract excerpts.

CRITICAL RULES:
- Only use the provided excerpts as evidence. Do not invent citations.
- Produce multiple check definitions where appropriate (a list).
- Output MUST be valid JSON.

You must return:
{
  "checks": [
    {
      "columns": { "<sharepoint_column_key>": <value_or_null>, ... },
      "evidence": { "<sharepoint_column_key>": [ { "chunk_id": "...", "quote": "...", "citation": "..." } ], ... },
      "notes": { "needs_sme_review": <bool>, "missing_fields": [..], "questions_for_sme": [..] }
    }
  ],
  "notes": { "needs_sme_review": <bool>, "missing_fields": [..], "questions_for_sme": [..] }
}

Evidence requirements:
- If you set a non-empty value for a column, include at least 1 evidence item for that column.
- Each evidence item must reference an excerpt by chunk_id and include a short quote copied from that excerpt.
"""


def build_draft_prompt(
    *,
    schema_table: str,
    schema_keys: List[str],
    contract_metadata: Dict[str, Any],
    excerpts: List[Dict[str, Any]],
    area_name: str,
    area_goal: str,
) -> str:
    payload = {
        "area": {"name": area_name, "goal": area_goal},
        "contract_metadata": contract_metadata,
        "schema_table": schema_table,
        "schema_keys": schema_keys,
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
    return (
        "Use the following payload to draft QAD check definitions.\n"
        "Return ONLY JSON, no extra commentary.\n\n"
        + json.dumps(payload, indent=2, ensure_ascii=False)
    )


def draft_checks_for_area(
    *,
    schema_table: str,
    schema_keys: List[str],
    contract_metadata: Dict[str, Any],
    excerpts: List[Dict[str, Any]],
    area_name: str,
    area_goal: str,
    temperature: float = 0.0,
) -> Dict[str, Any]:
    user_prompt = build_draft_prompt(
        schema_table=schema_table,
        schema_keys=schema_keys,
        contract_metadata=contract_metadata,
        excerpts=excerpts,
        area_name=area_name,
        area_goal=area_goal,
    )
    raw = chat_json(SYSTEM_DRAFT_CHECKS, user_prompt, temperature=temperature)
    return extract_json(raw)


# -----------------------------
# Planner agent (Section 6.2.1)
# -----------------------------

SYSTEM_PLAN_AREAS = """You are a planning agent for contract intake.
Your job is to propose EXTRACTION AREAS and RETRIEVAL QUERIES to generate QAD check definitions.

CRITICAL:
- Output MUST be valid JSON only (no prose).
- Unless the snapshot is truly empty, you MUST propose 4 to 8 areas.
- Areas must be distinct (do not lump everything into "general").
- Queries must be short keyword phrases likely to match contract text.
- Do not invent document content; use ONLY the provided snapshot.

IMPORTANT CONTEXT:
- contract_metadata may contain:
  - contract_type: one of ["general","nda","sow","policy"]
  - planner_hints: { forced_area_families, keyword_family_hits, token_estimate, suggested_areas_cap }
- Use forced_area_families as a strong hint: ensure coverage for those topics.
- If contract_type is "nda", prioritize confidentiality/handling/records over packaging/shipping.

Return JSON in this exact shape:
{
  "areas": [
    {
      "name": "packaging",
      "goal": "Extract packaging/preservation requirements that are auditable",
      "queries": ["packaging", "packing", "preservation", "pallet", "carton", "bag"],
      "k_per_query": 2,
      "max_total_excerpts": 18
    }
  ],
  "notes": {
    "rationale": "...",
    "risks": ["..."]
  }
}
"""


def build_plan_prompt(*, contract_metadata: Dict[str, Any], doc_signal: Dict[str, Any]) -> str:
    planner_hints = contract_metadata.get("planner_hints") if isinstance(contract_metadata, dict) else None
    forced = []
    cap_hint = None
    if isinstance(planner_hints, dict):
        forced = planner_hints.get("forced_area_families") or []
        cap_hint = planner_hints.get("suggested_areas_cap")

    payload = {
        "contract_metadata": contract_metadata,
        "doc_signal": doc_signal,
        "constraints": {
            "max_areas": 10,
            "min_areas": 4,
            "preferred_areas": 6,
            "area_name_style": "snake_case",
            "k_per_query_default": 2,
            "max_total_excerpts_default": 18,
            "query_style": "short keyword phrases",
            "areas_cap_hint": cap_hint,
        },
        "forced_area_families_hint": forced,
        "required_area_families_if_present": [
            "inspection",
            "acceptance",
            "packaging",
            "marking",
            "shipping",
            "documentation",
        ],
        "suggested_area_families": [
            "inspection",
            "acceptance",
            "first_article_inspection",
            "packaging",
            "preservation",
            "marking",
            "labeling",
            "shipping",
            "documentation",
            "certificates",
            "nonconforming_material",
            "software_requirements",
            "quality_assurance",
        ],
        "instructions": [
            "Prefer specific areas (e.g., packaging, inspection, marking) over generic.",
            "Avoid duplicates (e.g., marking vs labeling are OK, but don’t repeat the same).",
            "Each area should have 6–12 queries.",
            "If forced_area_families_hint is non-empty, ensure each is represented by at least one area.",
        ],
    }
    return (
        "Use the following snapshot to propose extraction areas and retrieval queries.\n"
        "Return ONLY JSON.\n\n"
        + json.dumps(payload, indent=2, ensure_ascii=False)
    )


def make_doc_signal_from_excerpts(excerpts: List[Dict[str, Any]]) -> Dict[str, Any]:
    sig = {
        "num_excerpts": len(excerpts or []),
        "sample": [],
        "pages": sorted({e.get("page_num") for e in (excerpts or []) if e.get("page_num") is not None})[:25],
    }
    for e in (excerpts or [])[:10]:
        sig["sample"].append(
            {"chunk_id": e.get("chunk_id"), "citation": e.get("citation"), "quote": (e.get("quote") or "")[:220]}
        )
    return sig


def _sanitize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """
    Defensive cleanup:
    - ensure structure
    - clamp values
    - dedupe queries
    - enforce minimum areas by filling standard areas if model returns too few
    """
    out = {"areas": [], "notes": {}}
    if not isinstance(plan, dict):
        return out

    areas = plan.get("areas")
    if not isinstance(areas, list):
        areas = []

    def norm_name(s: str) -> str:
        s = (s or "").strip().lower()
        s = re.sub(r"[^a-z0-9_]+", "_", s)
        s = re.sub(r"_+", "_", s).strip("_")
        return s or "area"

    def sanitize_area(a: Dict[str, Any]) -> Dict[str, Any] | None:
        if not isinstance(a, dict):
            return None
        name = norm_name(a.get("name") or "")
        goal = (a.get("goal") or "").strip() or "Extract auditable requirements for this area."
        queries = a.get("queries") or []
        if not isinstance(queries, list):
            queries = []

        q_seen = set()
        q_out: List[str] = []
        for q in queries:
            qq = (q or "").strip()
            if not qq:
                continue
            if len(qq) > 80:
                qq = qq[:80].rstrip()
            key = qq.lower()
            if key in q_seen:
                continue
            q_seen.add(key)
            q_out.append(qq)

        try:
            k_per_query = int(a.get("k_per_query", 2))
        except Exception:
            k_per_query = 2
        try:
            max_total = int(a.get("max_total_excerpts", 18))
        except Exception:
            max_total = 18

        k_per_query = max(1, min(k_per_query, 6))
        max_total = max(6, min(max_total, 40))

        if not q_out:
            q_out = [name.replace("_", " ")]

        return {
            "name": name,
            "goal": goal,
            "queries": q_out[:12],
            "k_per_query": k_per_query,
            "max_total_excerpts": max_total,
        }

    cleaned: List[Dict[str, Any]] = []
    seen_names = set()
    for a in areas:
        sa = sanitize_area(a)
        if not sa:
            continue
        if sa["name"] in seen_names:
            continue
        seen_names.add(sa["name"])
        cleaned.append(sa)

    cleaned = cleaned[:10]

    # Enforce minimum areas: auto-fill standard areas if planner returned too few
    STANDARD_AREAS: List[Dict[str, Any]] = [
        {
            "name": "inspection",
            "goal": "Extract inspection and test requirements (who/what/when/records).",
            "queries": ["inspection", "inspect", "test", "acceptance test", "verification", "sampling", "aql", "records"],
        },
        {
            "name": "acceptance",
            "goal": "Extract acceptance criteria, acceptance methods, and approval steps.",
            "queries": ["acceptance", "acceptance criteria", "approve", "verification", "validation", "conformance"],
        },
        {
            "name": "packaging",
            "goal": "Extract packaging, packing, and preservation requirements.",
            "queries": ["packaging", "packing", "preservation", "pallet", "carton", "bag", "mil-std-2073", "astm"],
        },
        {
            "name": "marking_labeling",
            "goal": "Extract marking, labeling, serialization, and traceability requirements.",
            "queries": ["marking", "labeling", "label", "serial", "serialization", "traceability", "barcode", "uid"],
        },
        {
            "name": "shipping",
            "goal": "Extract shipping, delivery, handling, and transportation requirements.",
            "queries": ["shipping", "delivery", "handling", "transport", "fob", "incoterms", "carrier", "address"],
        },
        {
            "name": "documentation_certificates",
            "goal": "Extract documentation and certificate/report deliverables (CoC, test reports).",
            "queries": ["certificate", "certificate of conformance", "coc", "test report", "as-built", "data package", "manual"],
        },
    ]

    min_areas = 4
    if len(cleaned) < min_areas:
        for std in STANDARD_AREAS:
            if len(cleaned) >= min_areas:
                break
            nm = std["name"]
            if nm in seen_names:
                continue
            cleaned.append(
                {
                    "name": nm,
                    "goal": std["goal"],
                    "queries": std["queries"][:12],
                    "k_per_query": 2,
                    "max_total_excerpts": 18,
                }
            )
            seen_names.add(nm)

    out["areas"] = cleaned
    out["notes"] = plan.get("notes") if isinstance(plan.get("notes"), dict) else {}
    return out


def plan_extraction_areas(
    *,
    contract_metadata: Dict[str, Any],
    seed_excerpts: List[Dict[str, Any]],
    temperature: float = 0.0,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Returns (sanitized_plan, raw_plan) so callers can persist raw output for debugging.
    """
    doc_signal = make_doc_signal_from_excerpts(seed_excerpts)
    user_prompt = build_plan_prompt(contract_metadata=contract_metadata, doc_signal=doc_signal)

    raw_text = chat_json(SYSTEM_PLAN_AREAS, user_prompt, temperature=temperature, max_tokens=1400)
    raw_plan = extract_json(raw_text)

    sanitized = _sanitize_plan(raw_plan)

    # NEW: optional trim using planner_hints.suggested_areas_cap (if present)
    try:
        hints = contract_metadata.get("planner_hints") if isinstance(contract_metadata, dict) else None
        cap = hints.get("suggested_areas_cap") if isinstance(hints, dict) else None
        if cap is not None:
            cap_i = int(cap)
            cap_i = max(4, min(10, cap_i))
            if isinstance(sanitized.get("areas"), list):
                sanitized["areas"] = sanitized["areas"][:cap_i]
    except Exception:
        pass

    return sanitized, raw_plan
