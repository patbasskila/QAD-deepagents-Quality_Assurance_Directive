# app/services/llm/client.py
from __future__ import annotations

import json
import random
from typing import Any, Dict, List, Optional

import httpx
from openai import AzureOpenAI

from app.utils.config import get_settings


def _get_http_client() -> httpx.Client:
    """
    Centralized httpx client creation.

    - http2 is optional; if enabled but extras aren't installed, we gracefully fall back to http1.
    - verify=False is kept for corp scenarios (can make configurable later).
    """
    s = get_settings()

    timeout = httpx.Timeout(s.llm_timeout_seconds)

    if not s.llm_use_http2:
        return httpx.Client(http2=False, verify=False, timeout=timeout)

    # If user enables http2 but does not have h2 installed, fall back to http1.
    try:
        return httpx.Client(http2=True, verify=False, timeout=timeout)
    except Exception:
        # Best-effort fallback (covers missing 'h2' extra)
        return httpx.Client(http2=False, verify=False, timeout=timeout)


def get_azure_client() -> AzureOpenAI:
    s = get_settings()
    http_client = _get_http_client()

    return AzureOpenAI(
        api_key=s.openai_api_key,
        azure_endpoint=s.azure_openai_endpoint,
        api_version=s.azure_openai_api_version,
        azure_deployment=s.azure_openai_deployment,
        http_client=http_client,
    )


# ----------------------------
# Mock helpers
# ----------------------------

def _looks_like_planner(system_prompt: str, user_prompt: str) -> bool:
    """
    Detect planner calls in mock mode. We can't reliably import DeepAgents prompts here,
    so we use robust keyword heuristics on the system + user prompt.
    """
    sp = (system_prompt or "").lower()
    up = (user_prompt or "").lower()

    # Strong signals
    if "planning agent" in sp:
        return True
    if "extraction areas" in sp or "extraction areas" in up:
        return True
    if '"areas"' in up or '"areas"' in sp:
        # Many prompts include this string, but planner will strongly.
        # We'll require an additional hint to avoid false positives.
        if "queries" in up and "k_per_query" in up:
            return True
    if "propose" in sp and "areas" in sp and "queries" in sp:
        return True

    return False


def _mock_plan_json(system_prompt: str, user_prompt: str) -> str:
    """
    Deterministic plan JSON for offline development.
    Returns the planner-shaped output: {"areas":[...], "notes": {...}}.
    """
    s = get_settings()
    rnd = random.Random(s.llm_mock_seed)

    # Heuristic: if user_prompt contains a cap hint like "areas_cap_hint": N, respect it.
    cap = None
    try:
        if "areas_cap_hint" in user_prompt:
            # very light parsing
            import re
            m = re.search(r'"areas_cap_hint"\s*:\s*(\d+)', user_prompt)
            if m:
                cap = int(m.group(1))
    except Exception:
        cap = None

    # Default 4–6 areas for demo; deterministic
    target = cap if isinstance(cap, int) and 4 <= cap <= 10 else rnd.choice([4, 5, 6])

    standard = [
        {
            "name": "inspection",
            "goal": "Extract inspection and test requirements (who/what/when/records).",
            "queries": ["inspection", "inspect", "test", "acceptance test", "verification", "sampling", "aql", "records"],
            "k_per_query": 2,
            "max_total_excerpts": 18,
        },
        {
            "name": "acceptance",
            "goal": "Extract acceptance criteria, acceptance methods, and approval steps.",
            "queries": ["acceptance", "acceptance criteria", "approve", "verification", "validation", "conformance"],
            "k_per_query": 2,
            "max_total_excerpts": 18,
        },
        {
            "name": "packaging",
            "goal": "Extract packaging, packing, and preservation requirements.",
            "queries": ["packaging", "packing", "preservation", "pallet", "carton", "bag", "mil-std-2073", "astm"],
            "k_per_query": 2,
            "max_total_excerpts": 18,
        },
        {
            "name": "marking_labeling",
            "goal": "Extract marking, labeling, serialization, and traceability requirements.",
            "queries": ["marking", "labeling", "label", "serial", "serialization", "traceability", "barcode", "uid"],
            "k_per_query": 2,
            "max_total_excerpts": 18,
        },
        {
            "name": "shipping_delivery",
            "goal": "Extract shipping, delivery, handling, and transportation requirements.",
            "queries": ["shipping", "delivery", "handling", "transport", "fob", "incoterms", "carrier", "address"],
            "k_per_query": 2,
            "max_total_excerpts": 18,
        },
        {
            "name": "documentation_certificates",
            "goal": "Extract documentation and certificate/report deliverables (CoC, test reports).",
            "queries": ["certificate", "certificate of conformance", "coc", "test report", "as-built", "data package", "manual"],
            "k_per_query": 2,
            "max_total_excerpts": 18,
        },
        {
            "name": "nonconformance",
            "goal": "Extract nonconforming material, deviations, waivers, and disposition requirements.",
            "queries": ["nonconforming", "non-conforming", "ncm", "disposition", "waiver", "deviation", "mr", "return"],
            "k_per_query": 2,
            "max_total_excerpts": 18,
        },
    ]

    plan = {
        "areas": standard[:target],
        "notes": {
            "needs_sme_review": True,
            "missing_fields": [],
            "questions_for_sme": [
                "Mock mode enabled (offline). Re-run in azure mode inside company network to generate real mapping."
            ],
            "rationale": "Mock planner: returning a deterministic standard plan for offline development.",
            "risks": ["Planner output in mock mode may not reflect true contract-specific decomposition."],
        },
    }
    return json.dumps(plan, indent=2)


def _mock_qad_json(system_prompt: str, user_prompt: str) -> str:
    """
    Deterministic mock JSON for offline development.
    Goal: keep pipeline moving + produce valid shape.
    """
    s = get_settings()
    rnd = random.Random(s.llm_mock_seed)

    # Extract likely schema keys from the schema table (simple heuristic).
    keys: List[str] = []
    for line in (user_prompt or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if parts and parts[0] and parts[0].lower() not in {"field_key", "key"}:
            k = parts[0]
            if all(ch.isalnum() or ch in {"_", "-"} for ch in k):
                keys.append(k)

    # de-dupe stable order
    seen = set()
    keys = [k for k in keys if not (k in seen or seen.add(k))]

    columns: Dict[str, Any] = {k: None for k in keys} if keys else {}
    evidence: Dict[str, Any] = {k: [] for k in keys} if keys else {}

    # populate 1-2 random keys with stub values so downstream export gets exercised
    if keys:
        for k in rnd.sample(keys, k=min(2, len(keys))):
            columns[k] = "STUB_VALUE"
            evidence[k] = [{"excerpt_id": "mock_excerpt", "quote": "Mock mode: no real citation available."}]

    out = {
        "columns": columns,
        "evidence": evidence,
        "notes": {
            "needs_sme_review": True,
            "missing_fields": [k for k, v in columns.items() if v in (None, "", [])],
            "questions_for_sme": [
                "Mock mode enabled (offline). Re-run in azure mode inside company network to generate real mapping."
            ],
        },
    }
    return json.dumps(out, indent=2)


def chat_json(system_prompt: str, user_prompt: str, *, temperature: float = 0.0, max_tokens: int = 1800) -> str:
    """
    Single entrypoint used by json_contract.py and DeepAgents.
    Chooses mock vs azure based on Settings.llm_mode.
    """
    s = get_settings()

    if s.llm_mode == "mock":
        # NEW: return planner-shaped JSON when planner prompt is used
        if _looks_like_planner(system_prompt, user_prompt):
            return _mock_plan_json(system_prompt, user_prompt)
        return _mock_qad_json(system_prompt, user_prompt)

    client = get_azure_client()

    last_err: Optional[Exception] = None
    attempts = max(1, int(s.llm_max_retries) + 1)

    for _ in range(attempts):
        try:
            resp = client.chat.completions.create(
                model=s.azure_openai_deployment,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last_err = e

    raise RuntimeError(f"LLM call failed after {attempts} attempt(s): {last_err}")
