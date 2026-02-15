# app/deepagents/orchestrator.py
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Tuple, Optional

from app.services.qad_schema import get_sharepoint_qad_schema, schema_keys, schema_to_prompt_table
from app.utils.files import ensure_dir

from app.deepagents.subagents import retrieve_excerpts, draft_checks_for_area, plan_extraction_areas


def _agents_dir(work_dir: str) -> str:
    p = os.path.join(work_dir, "artifacts", "agents")
    ensure_dir(p)
    return p


def _write_artifact(path: str, obj: Any) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def default_plan() -> Dict[str, Any]:
    return {
        "areas": [
            {
                "name": "general_contract_requirements",
                "goal": "Extract auditable quality/inspection/packaging/marking/shipping/documentation requirements and map them to the SharePoint QAD schema.",
                "queries": [
                    "inspection",
                    "acceptance",
                    "packaging",
                    "packing",
                    "preservation",
                    "marking",
                    "labeling",
                    "shipping",
                    "certificate",
                    "certificate of conformance",
                    "test report",
                    "first article inspection",
                    "nonconforming material",
                ],
                "k_per_query": 2,
                "max_total_excerpts": 18,
            }
        ]
    }


# ----------------------------
# NEW: Contract-type + planner heuristics helpers (Definition 2)
# ----------------------------

_CONTRACT_TYPE_PATTERNS: List[Tuple[str, List[str]]] = [
    ("nda", ["non-disclosure", "nondisclosure", "confidentiality", "confidential information", "recipient", "disclosing party"]),
    ("sow", ["statement of work", "scope of work", "sow", "deliverables", "milestone", "performance of work"]),
    ("policy", ["policy", "procedure", "shall comply", "must comply", "governance"]),
]


def _detect_contract_type(contract_text: str) -> str:
    t = (contract_text or "").lower()
    for label, pats in _CONTRACT_TYPE_PATTERNS:
        for p in pats:
            if p in t:
                return label
    return "general"


_KEYWORD_FAMILIES: Dict[str, List[str]] = {
    "inspection": ["inspection", "inspect", "verification", "test", "testing", "aql", "sampling"],
    "acceptance": ["acceptance", "accept", "reject", "rejection", "acceptance criteria"],
    "first_article": ["first article", "fai", "first-article"],
    "packaging": ["packaging", "packing", "preservation", "pallet", "carton"],
    "marking": ["marking", "labeling", "label", "barcode", "serialization", "uid"],
    "shipping": ["shipping", "delivery", "transport", "carrier", "fob", "incoterms"],
    "documentation": ["certificate", "certificate of conformance", "coc", "test report", "data package", "records"],
    "nonconformance": ["nonconforming", "non-conforming", "ncm", "disposition", "waiver", "deviation", "mr"],
}


def _estimate_tokens(text: str) -> int:
    # crude but stable estimate (avg ~4 chars/token)
    t = text or ""
    return max(1, len(t) // 4)


def _suggest_areas_cap(token_est: int) -> int:
    # Conservative caps: planner can still propose 4–8, but we trim for demos.
    if token_est < 3_000:
        return 4
    if token_est < 8_000:
        return 6
    return 8


def _build_planner_hints(contract_text: str) -> Dict[str, Any]:
    t = (contract_text or "").lower()
    token_est = _estimate_tokens(contract_text)

    hits: Dict[str, int] = {}
    for fam, kws in _KEYWORD_FAMILIES.items():
        c = 0
        for kw in kws:
            if kw in t:
                c += 1
        if c:
            hits[fam] = c

    forced_families = sorted(hits.keys(), key=lambda k: hits[k], reverse=True)

    return {
        "token_estimate": token_est,
        "suggested_areas_cap": _suggest_areas_cap(token_est),
        "keyword_family_hits": hits,
        "forced_area_families": forced_families[:8],
    }


from typing import Any, Dict, List

def _normalize_checks(checks_obj: Dict[str, Any], keys: List[str]) -> Dict[str, Any]:
    """
    Normalize the model/agent output into:
      {
        "checks": [ { "columns": {k: ...}, "evidence": {k: [...]}, "notes": {...} }, ... ],
        "notes": {...}
      }

    - Ensures checks is a LIST (never dict/set)
    - Ensures columns/evidence contain exactly schema keys
    - Ensures evidence values are lists
    """
    if not isinstance(checks_obj, dict):
        checks_obj = {}

    checks = checks_obj.get("checks")
    top_notes = checks_obj.get("notes") if isinstance(checks_obj.get("notes"), dict) else {}

    # If no checks returned, create a single placeholder check (LIST!)
    if not isinstance(checks, list) or len(checks) == 0:
        placeholder_check = {
            "columns": {k: None for k in keys},
            "evidence": {k: [] for k in keys},
            "notes": {
                "needs_sme_review": True,
                "missing_fields": keys[:],
                "questions_for_sme": ["Model returned no checks; re-run or review retrieval excerpts."],
            },
        }
        checks = [placeholder_check]  # must be list, not dict/set
        top_notes = {
            "needs_sme_review": True,
            "missing_fields": keys[:],
            "questions_for_sme": ["Model returned no checks; re-run or review retrieval excerpts."],
        }

    normalized: List[Dict[str, Any]] = []
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

        for k in keys:
            cols.setdefault(k, None)
            if k not in ev or not isinstance(ev.get(k), list):
                ev[k] = []

        # Drop non-schema keys defensively
        cols = {k: cols.get(k) for k in keys}
        ev = {k: ev.get(k, []) for k in keys}

        normalized.append({"columns": cols, "evidence": ev, "notes": notes})

    if not isinstance(top_notes, dict):
        top_notes = {}
    top_notes.setdefault("missing_fields", [])
    top_notes.setdefault("questions_for_sme", [])
    top_notes.setdefault("needs_sme_review", False)

    return {"checks": normalized, "notes": top_notes}


def _validate_checks(
    *,
    checks_obj: Dict[str, Any],
    allowed_keys: List[str],
    excerpts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    excerpt_by_id = {e["chunk_id"]: e for e in (excerpts or [])}
    report = {"ok": True, "num_checks": 0, "failures": [], "warnings": []}

    checks = checks_obj.get("checks") or []
    report["num_checks"] = len(checks)

    for i, chk in enumerate(checks):
        cols = chk.get("columns") or {}
        ev = chk.get("evidence") or {}

        bad_col_keys = [k for k in cols.keys() if k not in allowed_keys]
        bad_ev_keys = [k for k in ev.keys() if k not in allowed_keys]
        if bad_col_keys or bad_ev_keys:
            report["ok"] = False
            report["failures"].append(
                {
                    "check_index": i,
                    "reason": "non_schema_keys_present",
                    "bad_column_keys": bad_col_keys,
                    "bad_evidence_keys": bad_ev_keys,
                }
            )
            continue

        # Evidence grounding: if a column has a non-empty value, require evidence
        for k in allowed_keys:
            v = cols.get(k)
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue

            items = ev.get(k) or []
            if not isinstance(items, list) or len(items) == 0:
                report["ok"] = False
                report["failures"].append(
                    {"check_index": i, "reason": "missing_evidence_for_filled_field", "field": k}
                )
                continue

            for item in items:
                if not isinstance(item, dict):
                    report["ok"] = False
                    report["failures"].append(
                        {"check_index": i, "reason": "evidence_item_not_object", "field": k}
                    )
                    continue

                cid = item.get("chunk_id")
                quote = (item.get("quote") or "").strip()
                if not cid or cid not in excerpt_by_id:
                    report["ok"] = False
                    report["failures"].append(
                        {"check_index": i, "reason": "invalid_chunk_id", "field": k, "chunk_id": cid}
                    )
                    continue

                full_text = excerpt_by_id[cid].get("text", "") or ""
                if quote and quote not in full_text:
                    report["ok"] = False
                    report["failures"].append(
                        {"check_index": i, "reason": "quote_not_substring", "field": k, "chunk_id": cid}
                    )

    return report


_WS_RE = re.compile(r"\s+")


def _norm_text(s: Any, max_len: int = 240) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s = _WS_RE.sub(" ", s.strip().lower())
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


def _check_signature(check: Dict[str, Any]) -> str:
    cols = check.get("columns") or {}
    pairs: List[str] = []
    for k in sorted(cols.keys()):
        v = cols.get(k)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        nv = _norm_text(v)
        if not nv:
            continue
        pairs.append(f"{k}={nv}")
        if len(pairs) >= 10:
            break

    if pairs:
        return "|".join(pairs)

    ev = check.get("evidence") or {}
    chunk_ids: List[str] = []
    for k in sorted(ev.keys()):
        items = ev.get(k) or []
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict) and it.get("chunk_id"):
                chunk_ids.append(str(it["chunk_id"]))
    chunk_ids = sorted(set(chunk_ids))[:30]
    if chunk_ids:
        return "evidence:" + ",".join(chunk_ids)
    return "empty"


def _dedupe_checks(checks: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    seen: Dict[str, int] = {}
    kept: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []

    for idx, chk in enumerate(checks):
        sig = _check_signature(chk)
        if sig in seen:
            removed.append(
                {
                    "removed_index": idx,
                    "kept_index": seen[sig],
                    "reason": "duplicate_signature",
                    "signature": sig[:300],
                }
            )
            continue
        seen[sig] = idx
        kept.append(chk)

    report = {"before": len(checks), "after": len(kept), "removed": removed}
    return kept, report


def _short_quote(text: str, max_chars: int = 220) -> str:
    t = (text or "").strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def _repair_checks_once(
    *,
    checks: List[Dict[str, Any]],
    validation_report: Dict[str, Any],
    allowed_keys: List[str],
    excerpts: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    excerpt_by_id = {e.get("chunk_id"): e for e in (excerpts or []) if e.get("chunk_id")}

    failures = validation_report.get("failures") or []
    if not isinstance(failures, list):
        failures = []

    repaired: List[Dict[str, Any]] = json.loads(json.dumps(checks))
    actions: List[Dict[str, Any]] = []

    def _has_value(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, str) and not v.strip():
            return False
        return True

    for f in failures:
        if not isinstance(f, dict):
            continue

        idx = f.get("check_index")
        reason = f.get("reason")
        field = f.get("field")

        if not isinstance(idx, int) or idx < 0 or idx >= len(repaired):
            continue

        chk = repaired[idx]
        cols = chk.get("columns") if isinstance(chk.get("columns"), dict) else {}
        ev = chk.get("evidence") if isinstance(chk.get("evidence"), dict) else {}

        if field and field not in allowed_keys:
            continue

        if reason == "evidence_item_not_object" and field:
            items = ev.get(field)
            if isinstance(items, list):
                before = len(items)
                items2 = [it for it in items if isinstance(it, dict)]
                ev[field] = items2
                actions.append(
                    {
                        "check_index": idx,
                        "field": field,
                        "action": "drop_non_object_evidence_items",
                        "before": before,
                        "after": len(items2),
                    }
                )

        elif reason == "invalid_chunk_id" and field:
            bad_cid = f.get("chunk_id")
            items = ev.get(field)
            if isinstance(items, list):
                before = len(items)
                items2 = [it for it in items if isinstance(it, dict) and it.get("chunk_id") in excerpt_by_id]
                ev[field] = items2
                actions.append(
                    {
                        "check_index": idx,
                        "field": field,
                        "action": "drop_invalid_chunk_id_evidence",
                        "bad_chunk_id": bad_cid,
                        "before": before,
                        "after": len(items2),
                    }
                )
                if _has_value(cols.get(field)) and len(items2) == 0:
                    cols[field] = None
                    actions.append({"check_index": idx, "field": field, "action": "clear_field_due_to_no_valid_evidence"})

        elif reason == "quote_not_substring" and field:
            cid = f.get("chunk_id")
            ex = excerpt_by_id.get(cid)
            if not ex:
                continue
            items = ev.get(field)
            if not isinstance(items, list):
                continue
            fixed = 0
            for it in items:
                if not isinstance(it, dict):
                    continue
                if it.get("chunk_id") != cid:
                    continue
                it["quote"] = _short_quote(ex.get("text", "") or "")
                fixed += 1
            if fixed:
                actions.append(
                    {
                        "check_index": idx,
                        "field": field,
                        "action": "replace_quote_with_excerpt_substring",
                        "chunk_id": cid,
                        "count": fixed,
                    }
                )

        elif reason == "missing_evidence_for_filled_field" and field:
            if _has_value(cols.get(field)):
                cols[field] = None
                ev[field] = []
                actions.append({"check_index": idx, "field": field, "action": "clear_field_missing_evidence"})

        chk["columns"] = cols
        chk["evidence"] = ev
        repaired[idx] = chk

    report = {"attempted": True, "num_failures_in": len(failures), "num_actions": len(actions), "actions": actions}
    return repaired, report


# ----------------------------
# Section 6.5: Quality scoring
# ----------------------------

_GENERIC_STRINGS = {
    "n/a", "na", "none", "tbd", "to be determined", "per contract", "per drawing", "see contract", "as required"
}
_GENERIC_RE = re.compile(r"\b(n/?a|tbd|to be determined|per contract|see contract|as required)\b", re.IGNORECASE)


def _has_value(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    return True


def _is_generic_text(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip().lower()
    if s in _GENERIC_STRINGS:
        return True
    return bool(_GENERIC_RE.search(v))


def _compute_quality_report(
    *,
    checks: List[Dict[str, Any]],
    allowed_keys: List[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    per_check: List[Dict[str, Any]] = []
    severity_counts = {"ok": 0, "warn": 0, "bad": 0}
    flag_counts: Dict[str, int] = {}
    missing_evidence_field_counts: Dict[str, int] = {}

    for i, chk in enumerate(checks):
        cols = chk.get("columns") if isinstance(chk.get("columns"), dict) else {}
        ev = chk.get("evidence") if isinstance(chk.get("evidence"), dict) else {}
        dbg = chk.get("_debug") if isinstance(chk.get("_debug"), dict) else {}
        area = dbg.get("area")

        filled_fields = [k for k in allowed_keys if _has_value(cols.get(k))]
        num_filled = len(filled_fields)

        missing_evidence_fields: List[str] = []
        filled_with_evidence = 0
        total_evidence_items = 0
        generic_fields: List[str] = []
        long_fields: List[str] = []

        for k in filled_fields:
            items = ev.get(k) if isinstance(ev.get(k), list) else []
            total_evidence_items += len(items)
            if len(items) > 0:
                filled_with_evidence += 1
            else:
                missing_evidence_fields.append(k)
                missing_evidence_field_counts[k] = missing_evidence_field_counts.get(k, 0) + 1

            v = cols.get(k)
            if _is_generic_text(v):
                generic_fields.append(k)
            if isinstance(v, str) and len(v) > 600:
                long_fields.append(k)

        evidence_coverage = (filled_with_evidence / num_filled) if num_filled else 0.0

        flags: List[str] = []
        if num_filled == 0:
            flags.append("empty_check")
        if evidence_coverage < 0.70 and num_filled >= 3:
            flags.append("low_evidence_coverage")
        if total_evidence_items < 2 and num_filled >= 2:
            flags.append("low_citation_density")
        if generic_fields:
            flags.append("generic_text")
        if long_fields:
            flags.append("overlong_fields")

        score = 100
        if num_filled == 0:
            score = 0
        else:
            score -= int((1.0 - evidence_coverage) * 50)
            if total_evidence_items == 0:
                score -= 25
            elif total_evidence_items < 2:
                score -= 10
            score -= min(20, 5 * len(generic_fields))
            score -= min(15, 5 * len(long_fields))
            score -= max(0, 12 - min(12, num_filled))

        score = max(0, min(100, score))

        if score >= 75 and "empty_check" not in flags:
            severity = "ok"
        elif score >= 45:
            severity = "warn"
        else:
            severity = "bad"

        severity_counts[severity] += 1
        for fl in flags:
            flag_counts[fl] = flag_counts.get(fl, 0) + 1

        per_check.append(
            {
                "check_index": i,
                "area": area,
                "score": score,
                "severity": severity,
                "filled_fields": num_filled,
                "filled_with_evidence": filled_with_evidence,
                "evidence_coverage": round(evidence_coverage, 3),
                "total_evidence_items": total_evidence_items,
                "missing_evidence_fields": missing_evidence_fields,
                "generic_fields": generic_fields,
                "long_fields": long_fields,
                "flags": flags,
            }
        )

    quality_report = {"checks": per_check}
    quality_summary = {
        "counts_by_severity": severity_counts,
        "flag_counts": dict(sorted(flag_counts.items(), key=lambda kv: kv[1], reverse=True)),
        "top_missing_evidence_fields": [
            {"field": k, "count": v}
            for k, v in sorted(missing_evidence_field_counts.items(), key=lambda kv: kv[1], reverse=True)[:15]
        ],
        "num_checks": len(checks),
    }
    return quality_report, quality_summary


# ----------------------------
# Section 6.6: Export readiness
# ----------------------------

def _build_export_set(
    *,
    checks: List[Dict[str, Any]],
    quality_report: Dict[str, Any],
    policy: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    drop_bad = bool(policy.get("drop_bad", False))
    drop_empty = bool(policy.get("drop_empty", False))
    min_score = int(policy.get("min_score", 0) or 0)
    max_checks = policy.get("max_checks", None)
    sort_desc = bool(policy.get("sort_by_score_desc", True))

    qlist = quality_report.get("checks") or []
    qmap: Dict[int, Dict[str, Any]] = {}
    if isinstance(qlist, list):
        for row in qlist:
            if isinstance(row, dict) and isinstance(row.get("check_index"), int):
                qmap[row["check_index"]] = row

    included: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
    dropped: List[Dict[str, Any]] = []

    for idx, chk in enumerate(checks):
        q = qmap.get(idx, {"score": 0, "severity": "bad", "flags": ["missing_quality_row"]})
        score = int(q.get("score", 0) or 0)
        severity = str(q.get("severity", "bad") or "bad")
        flags = q.get("flags") if isinstance(q.get("flags"), list) else []

        reasons: List[str] = []
        if drop_empty and "empty_check" in flags:
            reasons.append("empty_check")
        if drop_bad and severity == "bad":
            reasons.append("severity_bad")
        if score < min_score:
            reasons.append(f"score_below_{min_score}")

        if reasons:
            dropped.append({"check_index": idx, "score": score, "severity": severity, "reasons": reasons})
            continue

        included.append((idx, chk, q))

    if sort_desc:
        included.sort(key=lambda t: int(t[2].get("score", 0) or 0), reverse=True)

    export_ready: List[Dict[str, Any]] = []
    for rank, (idx, chk, q) in enumerate(included, start=1):
        chk2 = json.loads(json.dumps(chk))
        chk2.setdefault("_debug", {})
        if isinstance(chk2["_debug"], dict):
            chk2["_debug"]["export_rank"] = rank
            chk2["_debug"]["quality_score"] = int(q.get("score", 0) or 0)
            chk2["_debug"]["quality_severity"] = str(q.get("severity", "bad") or "bad")
            chk2["_debug"]["source_check_index"] = idx
        export_ready.append(chk2)

    capped = False
    if isinstance(max_checks, int) and max_checks > 0 and len(export_ready) > max_checks:
        export_ready = export_ready[:max_checks]
        capped = True

    summary = {
        "policy": policy,
        "total_in": len(checks),
        "total_out": len(export_ready),
        "total_dropped": len(dropped),
        "dropped": dropped[:200],
        "capped": capped,
        "max_checks": max_checks,
    }
    return export_ready, summary


def _enrich_export_summary(
    *,
    export_summary: Dict[str, Any],
    quality_summary: Dict[str, Any],
    per_area_summary: List[Dict[str, Any]],
    dedupe_report: Dict[str, Any],
    final_validation: Dict[str, Any],
    repair_report: Dict[str, Any],
    cfg: Dict[str, Any],
    plan: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Make export_summary.json demo/debug friendly while staying backward-compatible.

    We preserve the original export_summary keys (policy/total_in/total_out/...)
    and add extra top-level keys that are safe for consumers that ignore unknown fields.
    """
    out = json.loads(json.dumps(export_summary or {}))

    # --- rollups / convenience ---
    counts_by_sev = (quality_summary or {}).get("counts_by_severity") or {}
    flag_counts = (quality_summary or {}).get("flag_counts") or {}

    dropped = out.get("dropped") if isinstance(out.get("dropped"), list) else []
    dropped_reason_counts: Dict[str, int] = {}
    for d in dropped:
        if not isinstance(d, dict):
            continue
        reasons = d.get("reasons") if isinstance(d.get("reasons"), list) else []
        for r in reasons:
            rr = str(r)
            dropped_reason_counts[rr] = dropped_reason_counts.get(rr, 0) + 1

    planned_areas = [
        a.get("name")
        for a in (plan.get("areas") or [])
        if isinstance(a, dict) and a.get("name")
    ]

    # --- SME trigger reasons (mirrors final output SME logic) ---
    ok_effective = bool((final_validation or {}).get("ok", False))
    repair_attempted = bool((repair_report or {}).get("attempted", False))
    bad_count = int((counts_by_sev or {}).get("bad", 0) or 0)

    sme_reasons: List[str] = []
    if not ok_effective:
        sme_reasons.append("validation_failed")
    if bad_count > 0:
        sme_reasons.append("quality_bad_present")

    needs_sme_review = (not ok_effective) or (bad_count > 0)

    # --- add enrichments ---
    out["quality"] = {
        "counts_by_severity": counts_by_sev,
        "flag_counts": flag_counts,
        "num_checks_scored": int((quality_summary or {}).get("num_checks", 0) or 0),
    }

    out["sme_review"] = {
        "needs_sme_review": needs_sme_review,
        "reasons": sme_reasons,
    }

    out["pipeline"] = {
        "planner_enabled": bool(cfg.get("planner_enabled", True)),
        "repair_enabled": bool(cfg.get("repair_enabled", True)),
        "quality_enabled": bool(cfg.get("quality_enabled", True)),
        "planned_areas": planned_areas,
        "per_area": per_area_summary or [],
    }

    out["dedupe"] = {
        "before": int((dedupe_report or {}).get("before", 0) or 0),
        "after": int((dedupe_report or {}).get("after", 0) or 0),
        "removed_count": len((dedupe_report or {}).get("removed") or [])
        if isinstance((dedupe_report or {}).get("removed"), list)
        else 0,
    }

    out["validation"] = {
        "ok": bool((final_validation or {}).get("ok", False)),
        "num_checks_validated": int((final_validation or {}).get("num_checks", 0) or 0),
        "num_failures": len((final_validation or {}).get("failures") or [])
        if isinstance((final_validation or {}).get("failures"), list)
        else 0,
        "repair_attempted": repair_attempted,
    }

    out["drop_reason_counts"] = dict(
        sorted(dropped_reason_counts.items(), key=lambda kv: kv[1], reverse=True)
    )

    return out


def _merge_run_config(run_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    base = {
        "planner_enabled": True,
        "planner_temperature": 0.0,
        "areas_cap": None,
        "repair_enabled": True,
        "quality_enabled": True,
        "temperature": 0.0,
        "export_policy": {
            "sort_by_score_desc": True,
            "drop_bad": False,
            "drop_empty": False,
            "min_score": 0,
            "max_checks": None,
        },
    }
    if not isinstance(run_config, dict):
        return base

    out = json.loads(json.dumps(base))
    for k in ["planner_enabled", "planner_temperature", "areas_cap", "repair_enabled", "quality_enabled", "temperature"]:
        if k in run_config:
            out[k] = run_config.get(k)

    ep = run_config.get("export_policy")
    if isinstance(ep, dict):
        out.setdefault("export_policy", {})
        for k in ["sort_by_score_desc", "drop_bad", "drop_empty", "min_score", "max_checks"]:
            if k in ep:
                out["export_policy"][k] = ep.get(k)

    # normalize types
    out["planner_enabled"] = bool(out.get("planner_enabled", True))
    try:
        out["planner_temperature"] = float(out.get("planner_temperature", 0.0) or 0.0)
    except Exception:
        out["planner_temperature"] = 0.0
    out["repair_enabled"] = bool(out.get("repair_enabled", True))
    out["quality_enabled"] = bool(out.get("quality_enabled", True))
    try:
        out["temperature"] = float(out.get("temperature", 0.0) or 0.0)
    except Exception:
        out["temperature"] = 0.0
    cap = out.get("areas_cap", None)
    try:
        if cap is None or str(cap).strip() == "":
            out["areas_cap"] = None
        else:
            out["areas_cap"] = int(cap)
    except Exception:
        out["areas_cap"] = None

    ep2 = out.get("export_policy", {})
    if isinstance(ep2, dict):
        try:
            ep2["min_score"] = int(ep2.get("min_score", 0) or 0)
        except Exception:
            ep2["min_score"] = 0
        mc = ep2.get("max_checks", None)
        try:
            if mc is None or str(mc).strip() == "":
                ep2["max_checks"] = None
            else:
                ep2["max_checks"] = int(mc)
        except Exception:
            ep2["max_checks"] = None

    return out


def run_deepagents(
    *,
    job_id: str,
    work_dir: str,
    job_artifacts: Dict[str, str],
    contract_metadata: Dict[str, Any],
    temperature: float = 0.0,
    run_config: Optional[Dict[str, Any]] = None,  # NEW (6.8)
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    schema = get_sharepoint_qad_schema()
    keys = schema_keys(schema)
    schema_table = schema_to_prompt_table(schema)

    agents_dir = _agents_dir(work_dir)

    cfg = _merge_run_config(run_config)

    # NEW: enrich contract_metadata with contract_type + planner_hints (Definition 2)
    contract_text = ""
    try:
        cpath = job_artifacts.get("contract_text")
        if cpath and os.path.exists(cpath):
            with open(cpath, "r", encoding="utf-8", errors="ignore") as f:
                contract_text = f.read() or ""
    except Exception:
        contract_text = ""

    try:
        contract_type = _detect_contract_type(contract_text)
    except Exception:
        contract_type = "general"

    try:
        planner_hints = _build_planner_hints(contract_text)
    except Exception:
        planner_hints = {"token_estimate": None, "suggested_areas_cap": None, "keyword_family_hits": {}, "forced_area_families": []}

    # Ensure we don't clobber existing metadata; just add fields
    if isinstance(contract_metadata, dict):
        contract_metadata.setdefault("contract_type", contract_type)
        contract_metadata.setdefault("planner_hints", planner_hints)

    # If areas_cap not set in config, apply heuristic cap
    if cfg.get("areas_cap", None) is None:
        try:
            suggested_cap = int((planner_hints or {}).get("suggested_areas_cap") or 0)
        except Exception:
            suggested_cap = 0
        if suggested_cap > 0:
            cfg["areas_cap"] = suggested_cap

    # Always persist run_config for provenance (6.8)
    run_cfg_path = os.path.join(agents_dir, "run_config.json")
    _write_artifact(run_cfg_path, cfg)

    # effective temperature: explicit arg wins unless cfg has one
    effective_temp = float(cfg.get("temperature", temperature) or 0.0)

    # 1) Seed retrieval for planner signal
    seed_excerpts = retrieve_excerpts(
        job_artifacts=job_artifacts,
        queries=["scope", "requirements", "inspection", "acceptance", "packaging", "marking", "shipping", "certificate"],
        k_per_query=2,
        max_total=14,
    )

    # 2) Planner-generated plan (fallback safe) + persist raw planner output
    raw_plan: Dict[str, Any] = {}
    if cfg.get("planner_enabled", True):
        try:
            plan, raw_plan = plan_extraction_areas(
                contract_metadata=contract_metadata,
                seed_excerpts=seed_excerpts,
                temperature=float(cfg.get("planner_temperature", 0.0) or 0.0),
            )
            if not (isinstance(plan, dict) and (plan.get("areas") or [])):
                plan = default_plan()
        except Exception:
            plan = default_plan()
            raw_plan = {"error": "planner_exception_or_invalid_json"}
    else:
        plan = default_plan()
        raw_plan = {"disabled": True, "reason": "planner_enabled=false"}

    # Apply areas cap if set
    cap = cfg.get("areas_cap", None)
    if isinstance(plan, dict) and isinstance(plan.get("areas"), list) and isinstance(cap, int) and cap > 0:
        plan["areas"] = plan["areas"][:cap]

    plan_path = os.path.join(agents_dir, "plan.json")
    _write_artifact(plan_path, plan)

    planner_raw_path = os.path.join(agents_dir, "planner_raw.json")
    _write_artifact(planner_raw_path, raw_plan)

    # 3) Fan-out: run all areas
    all_checks: List[Dict[str, Any]] = []
    all_excerpts: List[Dict[str, Any]] = []
    per_area_summary: List[Dict[str, Any]] = []

    areas = plan.get("areas") or []
    if not isinstance(areas, list) or not areas:
        areas = default_plan().get("areas") or []

    for area in areas:
        if not isinstance(area, dict):
            continue

        area_name = area.get("name") or "area"
        area_goal = area.get("goal") or "Extract contract requirements and map them to QAD schema."
        queries = area.get("queries") or []
        try:
            k_per_query = int(area.get("k_per_query") or 2)
        except Exception:
            k_per_query = 2
        try:
            max_total = int(area.get("max_total_excerpts") or 18)
        except Exception:
            max_total = 18

        area_excerpts = retrieve_excerpts(
            job_artifacts=job_artifacts,
            queries=queries,
            k_per_query=k_per_query,
            max_total=max_total,
        )
        all_excerpts.extend(area_excerpts)

        draft = draft_checks_for_area(
            schema_table=schema_table,
            schema_keys=keys,
            contract_metadata=contract_metadata,
            excerpts=area_excerpts,
            area_name=area_name,
            area_goal=area_goal,
            temperature=effective_temp,
        )
        draft_path = os.path.join(agents_dir, f"draft_{area_name}.json")
        _write_artifact(draft_path, draft)

        normalized = _normalize_checks(draft, keys)
        checks = normalized.get("checks") or []
        if isinstance(checks, list):
            for c in checks:
                if isinstance(c, dict):
                    c.setdefault("_debug", {})
                    if isinstance(c["_debug"], dict):
                        c["_debug"].setdefault("area", area_name)
            all_checks.extend([c for c in checks if isinstance(c, dict)])

        area_validation = _validate_checks(allowed_keys=keys, checks_obj=normalized, excerpts=area_excerpts)
        per_area_summary.append(
            {
                "area": area_name,
                "num_excerpts": len(area_excerpts),
                "num_checks": len(checks) if isinstance(checks, list) else 0,
                "validation_ok": bool(area_validation.get("ok", False)),
                "num_failures": len(area_validation.get("failures") or []),
            }
        )

    merged_obj = {"checks": all_checks, "notes": {"per_area": per_area_summary}}
    merged_path = os.path.join(agents_dir, "drafts_merged.json")
    _write_artifact(merged_path, merged_obj)

    deduped_checks, dedupe_report = _dedupe_checks(all_checks)
    dedupe_path = os.path.join(agents_dir, "dedupe_report.json")
    _write_artifact(dedupe_path, dedupe_report)

    # 4) Validate merged checks
    final_obj = {"checks": deduped_checks, "notes": {"per_area": per_area_summary}}
    final_validation = _validate_checks(allowed_keys=keys, checks_obj=final_obj, excerpts=all_excerpts)
    validation_path = os.path.join(agents_dir, "validation_report.json")
    _write_artifact(validation_path, final_validation)

    # 5) Repair (optional)
    repaired_checks = deduped_checks
    repair_attempt_report: Dict[str, Any] = {"attempted": False, "reason": "disabled_or_not_applicable", "actions": []}
    repair_report: Dict[str, Any] = {"attempted": False}

    if bool(cfg.get("repair_enabled", True)) and not bool(final_validation.get("ok", False)):
        repaired_checks, repair_attempt_report = _repair_checks_once(
            checks=deduped_checks,
            validation_report=final_validation,
            allowed_keys=keys,
            excerpts=all_excerpts,
        )
        repaired_obj = {"checks": repaired_checks, "notes": {"per_area": per_area_summary}}
        repaired_validation = _validate_checks(allowed_keys=keys, checks_obj=repaired_obj, excerpts=all_excerpts)

        repair_report = {
            "attempted": True,
            "validation_before": final_validation,
            "validation_after": repaired_validation,
            "improved": (len(repaired_validation.get("failures") or []) < len(final_validation.get("failures") or [])),
        }
    else:
        repaired_validation = final_validation

    repair_attempt_path = os.path.join(agents_dir, "repair_attempt.json")
    _write_artifact(repair_attempt_path, repair_attempt_report)

    repair_report_path = os.path.join(agents_dir, "repair_report.json")
    _write_artifact(repair_report_path, repair_report)

    # 6) Quality scoring (optional)
    if bool(cfg.get("quality_enabled", True)):
        quality_report, quality_summary = _compute_quality_report(checks=repaired_checks, allowed_keys=keys)
    else:
        quality_report = {"checks": []}
        quality_summary = {"counts_by_severity": {"ok": 0, "warn": 0, "bad": 0}, "flag_counts": {}, "num_checks": len(repaired_checks), "disabled": True}

    quality_report_path = os.path.join(agents_dir, "quality_report.json")
    _write_artifact(quality_report_path, quality_report)

    quality_summary_path = os.path.join(agents_dir, "quality_summary.json")
    _write_artifact(quality_summary_path, quality_summary)

    # 6.6 Export readiness (policy from cfg; if quality disabled, ordering depends on policy only)
    export_policy = cfg.get("export_policy") if isinstance(cfg.get("export_policy"), dict) else {}
    export_policy.setdefault("notes", "Configured via run_config.json (Section 6.8).")

    export_ready_checks, export_summary = _build_export_set(
        checks=repaired_checks,
        quality_report=quality_report,
        policy=export_policy,
    )

    export_policy_path = os.path.join(agents_dir, "export_policy.json")
    _write_artifact(export_policy_path, export_policy)

    # Enrich export summary for demos/debug (backward compatible)
    enriched_export_summary = _enrich_export_summary(
        export_summary=export_summary,
        quality_summary=quality_summary,
        per_area_summary=per_area_summary,
        dedupe_report=dedupe_report,
        final_validation=final_validation_effective if 'final_validation_effective' in locals() else final_validation,
        repair_report=repair_report,
        cfg=cfg,
        plan=plan,
    )

    export_summary_path = os.path.join(agents_dir, "export_summary.json")
    _write_artifact(export_summary_path, enriched_export_summary)

    export_ready_path = os.path.join(agents_dir, "export_ready_checks.json")
    _write_artifact(export_ready_path, {"checks": export_ready_checks, "notes": enriched_export_summary})

    # 7) Final output uses repaired checks; SME flag can include quality signal too
    final_validation_effective = repair_report.get("validation_after") if repair_report.get("attempted") else final_validation
    ok_effective = bool((final_validation_effective or {}).get("ok", False))

    bad_count = int((quality_summary.get("counts_by_severity") or {}).get("bad", 0) or 0)
    needs_sme_review = (not ok_effective) or (bad_count > 0)

    questions: List[str] = []
    if not ok_effective:
        questions.append("Validation failed for one or more checks (see agents/validation_report.json and agents/repair_report.json).")
    if bad_count > 0:
        questions.append(f"Quality scoring flagged {bad_count} checks as 'bad' (see agents/quality_report.json).")

    out = {
        "checks": repaired_checks,
        "notes": {
            "needs_sme_review": needs_sme_review,
            "missing_fields": [],
            "questions_for_sme": questions,
            "per_area": per_area_summary,
        },
        "_debug": {
            "deepagents": True,
            "job_id": job_id,
            "run_config": cfg,
            "planned_areas": [a.get("name") for a in (plan.get("areas") or []) if isinstance(a, dict)],
            "seed_excerpts_provided": [e.get("chunk_id") for e in seed_excerpts],
            "total_area_excerpts": len(all_excerpts),
            "total_checks_before_dedupe": len(all_checks),
            "total_checks_after_dedupe": len(deduped_checks),
            "planner_raw_saved": True,
            "repair_attempted": bool(repair_attempt_report.get("attempted", False)),
            "repair_actions": int(repair_attempt_report.get("num_actions", 0) or 0),
            "quality": quality_summary,
            "export": {
                "policy": export_policy,
                "summary": export_summary,
                "export_ready_count": len(export_ready_checks),
            },
        },
    }

    final_path = os.path.join(agents_dir, "final_checks.json")
    _write_artifact(final_path, out)

    agent_artifacts = {
        "agents_run_config": run_cfg_path,  # NEW (6.8)
        "agents_plan": plan_path,
        "agents_planner_raw": planner_raw_path,
        "agents_draft": merged_path,
        "agents_merged": merged_path,
        "agents_dedupe_report": dedupe_path,
        "agents_validation": validation_path,
        "agents_repair_attempt": repair_attempt_path,
        "agents_repair_report": repair_report_path,
        "agents_quality_report": quality_report_path,
        "agents_quality_summary": quality_summary_path,
        "agents_export_policy": export_policy_path,
        "agents_export_summary": export_summary_path,
        "agents_export_ready_checks": export_ready_path,
        "agents_final": final_path,
        "agents_normalized": merged_path,
    }
    return out, agent_artifacts
