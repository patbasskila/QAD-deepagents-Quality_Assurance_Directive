# app/services/export.py
from __future__ import annotations

import csv
import os
from typing import Dict, Any, List

import pandas as pd

from app.utils.files import ensure_dir
from app.services.qad_schema import get_sharepoint_qad_schema, schema_keys


def checks_json_to_rows(checks_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Convert checks JSON into list[flat_row_dict] suitable for CSV/XLSX export.
    Ensures all SharePoint gold columns are present (blank if missing).

    Expected checks_json shape:
      { "checks": [ { "columns": {...} }, ... ], ... }
    """
    schema = get_sharepoint_qad_schema()
    keys = schema_keys(schema)

    checks = checks_json.get("checks") or []
    if not isinstance(checks, list):
        checks = []

    rows: List[Dict[str, Any]] = []
    for chk in checks:
        cols = chk.get("columns") if isinstance(chk, dict) else None
        if not isinstance(cols, dict):
            cols = {}

        row: Dict[str, Any] = {}
        for k in keys:
            v = cols.get(k)
            if v is None:
                row[k] = ""
            elif isinstance(v, (str, int, float, bool)):
                row[k] = v
            else:
                row[k] = str(v)
        rows.append(row)

    if not rows:
        rows = [{k: "" for k in keys}]

    return rows


def write_csv_from_checks(checks_json: Dict[str, Any], path: str) -> None:
    ensure_dir(os.path.dirname(path))
    rows = checks_json_to_rows(checks_json)
    keys = list(rows[0].keys())

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_xlsx_from_checks(checks_json: Dict[str, Any], path: str) -> None:
    ensure_dir(os.path.dirname(path))
    rows = checks_json_to_rows(checks_json)
    df = pd.DataFrame(rows)
    df.to_excel(path, index=False)
