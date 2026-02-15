# app/services/qad_schema.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Optional, Literal

FieldType = Literal["string", "boolean", "number", "date", "enum"]


@dataclass(frozen=True)
class QADFieldSpec:
    """
    Defines ONE SharePoint column (aka QAD form field) that the model may populate.
    For V1.5+ we treat SharePoint list column internal names as the CSV/XLSX headers.
    """
    key: str
    display_name: str
    field_type: FieldType = "string"
    required: bool = False
    description: str = ""
    enum_values: Optional[List[str]] = None
    retrieval_hints: Optional[List[str]] = None


# ---------------------------------------------------------------------
# Gold schema (SharePoint list columns)
# Note: in your own deployment, replace these with your SharePoint list internal column names.
# NOTE: Keep ordering stable; PowerApps imports often assume header order.
# ---------------------------------------------------------------------
SP_COLUMNS: List[str] = [
    "QADno",
    "Review_Date",
    "last_update",
    "Modified By",
    "QAD_Type",
    "QAD_Status",
    "ORG_POC",
    "GO_SO",
    "date",
    "customer",
    "Update_Due_YN",
    "Update_Due",
    "program",
    "cust_cognizance_01",
    "nonconform_mtl_01",
    "nonconform_mtl_02",
    "nonconform_mtl_03",
    "cust_cognizance_02",
    "cust_cognizance_03",
    "inspection_01",
    "inspection_02",
    "solder_reqmt",
    "point_of_insp01",
    "point_of_insp02",
    "point_of_accept01",
    "point_of_accept02",
    "software_specs",
    "pqa_reqmts_01",
    "pqa_reqmts_02",
    "first_art_insp01",
    "first_art_insp02",
    "presv_reqmt",
    "pack_reqmt01",
    "pack_reqmt02",
    "pack_reqmt03",
    "mark_reqmt01",
    "mark_reqmt02",
    "ship_reqmt01",
    "ship_reqmt02",
    "ship_reqmt03",
    "ship_reqmt04",
    "ship_reqmt05",
    "ship_reqmt06",
    "test_doc_reqmt01",
    "test_doc_reqmt02",
    "coc_reqmt",
    "cert_reqmt01",
    "cert_reqmt02",
    "cert_reqmt03",
    "cert_reqmt04",
    "cert_reqmt05",
    "cert_reqmt06",
    "cert_reqmt07",
    "cert_reqmt08",
    "cert_reqmt09",
    "cert_reqmt10",
    "cert_reqmt11",
    "cert_reqmt12",
    "cert_reqmt13",
    "cert_reqmt14",
    "cert_reqmt15",
    "cert_reqmt16",
    "cert_reqmt17",
    "cert_reqmt18",
    "cert_reqmt19",
    "cert_reqmt20",
    "cert_reqmt21",
]


def get_sharepoint_qad_schema() -> List[QADFieldSpec]:
    """
    Gold schema: one field spec per SharePoint column key.

    Notes:
    - Default field_type="string" and required=False for all columns.
    - Later, once SMEs confirm PowerApps import rules, we can:
        * mark a minimal required subset
        * set types for dates/bools/numbers
        * add per-field retrieval hints tuned to your contracts
    """
    out: List[QADFieldSpec] = []
    for key in SP_COLUMNS:
        out.append(
            QADFieldSpec(
                key=key,
                display_name=key,
                field_type="string",
                required=False,
                retrieval_hints=_default_hints_from_key(key),
            )
        )
    return out


def _default_hints_from_key(key: str) -> List[str]:
    """
    Convert a column key into lightweight retrieval hints.
    Keep it conservative to avoid pulling irrelevant chunks.
    """
    k = (key or "").strip()
    if not k:
        return []

    base = k.replace("_", " ").strip().lower()
    hints = {base}

    # Expand a few patterns that show up in your column keys
    # (These are retrieval hints only, not schema changes.)
    expansions = {
        "pack reqmt": "packaging requirement",
        "mark reqmt": "marking requirement",
        "ship reqmt": "shipping requirement",
        "insp": "inspection",
        "accept": "acceptance",
        "cert": "certificate",
        "coc": "certificate of conformance",
        "presv": "preservation requirement",
        "pqa": "product quality assurance",
        "nonconform": "nonconforming material",
        "cognizance": "customer cognizance",
    }

    for a, b in expansions.items():
        if a in base:
            hints.add(base.replace(a, b))

    # Token hints (skip tiny/noisy tokens)
    for tok in base.split():
        if len(tok) >= 3 and not tok.isdigit():
            hints.add(tok)

    return [h for h in hints if h]


def schema_keys(schema: List[QADFieldSpec]) -> List[str]:
    return [f.key for f in schema]


def schema_lookup(schema: List[QADFieldSpec]) -> Dict[str, QADFieldSpec]:
    return {f.key: f for f in schema}


def schema_to_prompt_table(schema: List[QADFieldSpec]) -> str:
    """
    Keep prompt schema compact and machine-friendly.
    """
    lines: List[str] = []
    for f in schema:
        req = "required" if f.required else "optional"
        lines.append(f"- {f.key} ({req})")
    return "\n".join(lines)


def required_field_keys(schema: List[QADFieldSpec]) -> List[str]:
    return [f.key for f in schema if f.required]
