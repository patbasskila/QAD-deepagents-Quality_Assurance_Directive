# app/services/llm/prompts.py
from __future__ import annotations

from typing import Dict, Any


SYSTEM_QAD_MAPPER = """You are a contract compliance assistant.
Your job is to extract shipment/QAD-relevant requirements from the provided contract excerpts and map them into QAD check definitions.

Critical rules:
- Output MUST be valid JSON only. No markdown, no code fences, no commentary.
- Only use information supported by the provided excerpts. If missing, use null and explain in notes.
- Be conservative: if the excerpt is unclear, set the value to null and ask for SME review in notes.
- Each non-null field value MUST have evidence with excerpt_id referencing the provided chunk_id.
- Quotes must be short (<= 25 words) and copied from the excerpt snippet provided.
"""


def build_user_prompt(payload: Dict[str, Any]) -> str:
    """
    payload keys (expected):
      - schema_table: str
      - contract_metadata: dict
      - excerpts: list[dict] each has {chunk_id, citation, quote, ...}
      - schema_keys: list[str]  (exact SharePoint column names)
    """
    schema_table = payload.get("schema_table", "")
    schema_keys = payload.get("schema_keys", []) or []
    meta = payload.get("contract_metadata", {}) or {}
    excerpts = payload.get("excerpts", []) or []

    excerpt_lines = []
    for ex in excerpts:
        cid = ex.get("chunk_id")
        citation = (ex.get("citation") or "").strip()
        quote = (ex.get("quote") or "").strip()

        header = f"[{cid}] {citation}".strip()
        excerpt_lines.append(f"{header}\n{quote}")

    excerpt_block = "\n\n".join(excerpt_lines)

    return f"""
You will generate QAD check definitions as JSON.

CONTRACT METADATA (may be partial):
{meta}

TARGET SHAREPOINT COLUMN KEYS (must use these exact keys):
{schema_keys}

SCHEMA SUMMARY:
{schema_table}

EXCERPTS (you MUST cite excerpt chunk_id in evidence.excerpt_id):
{excerpt_block}

OUTPUT JSON FORMAT (MUST MATCH EXACTLY):
{{
  "checks": [
    {{
      "columns": {{
        "<column_key>": <value or null>,
        ...
      }},
      "evidence": {{
        "<column_key>": [
          {{
            "excerpt_id": "<chunk_id from excerpts>",
            "quote": "<short quote supporting the value, <= 25 words>"
          }}
        ],
        ...
      }},
      "notes": {{
        "needs_sme_review": <true/false>,
        "missing_fields": [ "<column_key>", ... ],
        "questions_for_sme": [ "<question>", ... ]
      }}
    }}
  ],
  "notes": {{
    "needs_sme_review": <true/false>,
    "missing_fields": [ "<column_key>", ... ],
    "questions_for_sme": [ "<question>", ... ]
  }}
}}

Rules:
- Produce 1+ check definitions. Each check definition corresponds to ONE distinct requirement/check that a shipper would verify.
- For each check, columns MUST include every column_key listed in TARGET SHAREPOINT COLUMN KEYS (value may be null).
- evidence MUST exist for each column_key that has a non-null value. If you set a value, you must provide at least one supporting evidence item.
- Do not invent values. If not supported by excerpts, set null.
- If any important fields are null/unknown for a check, set that check's needs_sme_review=true and add questions_for_sme.
- Top-level notes should reflect overall SME review needs across all checks.
""".strip()
