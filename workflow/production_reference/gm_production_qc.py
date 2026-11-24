"""Carry audited historical-GM eligibility into production without rewriting GM_OK.

GM_OK remains the original 24/24 flag. Production eligibility is a separate,
explicit policy: either 24/24 or the authorized 20/24 with >=95% country area.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from functools import lru_cache
from pathlib import Path

import fiona

from prepare_disease_input import sha256


@lru_cache(maxsize=1)
def generic_candidates():
    catalog = json.loads((Path(__file__).resolve().parents[1] /
                          "config/generic_maturity_candidates.json").read_text())
    return {item["gm"]: item for item in catalog["cultivars"]}


def integer(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer {name}: {value!r}") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"Invalid integer {name}: {value!r}")
    return int(number)


def normalized_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError("Missing audited GM production policy")
    cases = integer(policy.get("historical_cases"), "historical_cases")
    maximum = integer(policy.get("maximum_failed_cases"), "maximum_failed_cases")
    minimum = integer(policy.get("minimum_valid_cases"), "minimum_valid_cases")
    if cases != 24 or maximum not in {0, 4} or minimum != cases - maximum:
        raise ValueError("Only the approved GM policies 24/24 and 20/24 are supported")
    return dict(historical_cases=cases, maximum_failed_cases=maximum,
                minimum_valid_cases=minimum)


def validate_gm(props, policy=None):
    """Validate a single site, preserving legacy strict configs without metadata."""
    cell = props.get("CellID", "?")
    if props.get("GM_METHOD") != "GGCMI_HIST_MAT_V2":
        raise ValueError(f"CellID {cell}: unexpected GM method")
    if policy is None:
        if integer(props.get("GM_OK"), "GM_OK") != 1:
            raise ValueError(f"CellID {cell}: GM_OK=0 requires an audited production policy")
        return
    rule = normalized_policy(policy)
    cases = integer(props.get("GM_CASE"), "GM_CASE")
    failures = integer(props.get("GM_FAIL"), "GM_FAIL")
    if cases != 24 or not 0 <= failures <= rule["maximum_failed_cases"]:
        raise ValueError(f"CellID {cell}: {failures} failed cases / {cases}; policy requires "
                         f"{rule['minimum_valid_cases']}/24 valid cases")
    if integer(props.get("GM_GCM"), "GM_GCM") != 1:
        raise ValueError(f"CellID {cell}: expected one historical reference GCM")
    if integer(props.get("GM_OK"), "GM_OK") != int(failures == 0):
        raise ValueError(f"CellID {cell}: inconsistent original GM_OK/GM_FAIL")
    # The calibration method selects among unmodified DSSAT generic cultivars.
    gm = str(props.get("GM_WIN", ""))
    candidates = generic_candidates()
    if gm not in candidates:
        raise ValueError(f"CellID {cell}: invalid generic GM {gm!r}")
    expected = candidates[gm]
    for field, value in (("ZONE_ID", f"GM_{gm}"), ("INGENO", expected["ingeno"]),
                         ("CNAME", expected["cname"])):
        if str(props.get(field, "")) != value:
            raise ValueError(f"CellID {cell}: {field} does not match selected GM {gm}")


def policy_from_sites(sites):
    """Read the existing merge audit, recheck its sites and area, then bind hashes."""
    sites = Path(sites).resolve()
    audit_path = sites.with_name(sites.stem + "_audit.json")
    if not audit_path.is_file():
        return None  # Old strict workflows retain their original GM_OK gate.
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if "gm_calibration_policy" not in audit:
        return None
    rule = normalized_policy(audit["gm_calibration_policy"])
    if audit.get("status") != "ok" or Path(audit.get("calibrated_sites", "")).resolve() != sites:
        raise ValueError(f"Invalid or mismatched calibrated-site audit: {audit_path}")
    if audit.get("candidates_per_site") != 13:
        raise ValueError("GM audit must contain all 13 generic candidates")
    counts = Counter()
    keep = set()
    with fiona.open(sites) as source:
        for feature in source:
            props = dict(feature["properties"])
            validate_gm(props, rule)
            cell = str(props["CellID"])
            if cell in keep:
                raise ValueError(f"Duplicate calibrated CellID {cell}")
            keep.add(cell)
            counts[str(integer(props["GM_FAIL"], "GM_FAIL"))] += 1
    if not keep or audit.get("site_count") != len(keep) or audit.get("score_rows") != 13 * len(keep):
        raise ValueError("GM audit/site count disagreement")
    coverage = audit.get("coverage")
    if rule["maximum_failed_cases"] == 4 and not coverage:
        raise ValueError("The 20/24 policy requires an audited country area gate")
    if coverage:
        from coverage_policy import coverage_report
        minimum = float(coverage.get("minimum_area_fraction", 0))
        if not .95 <= minimum <= 1:
            raise ValueError("GM country coverage must require at least 95% area")
        measured = coverage_report(Path(audit["base_sites"]), keep, minimum)
        for name in ("original_points", "retained_points"):
            if coverage.get(name) != measured[name]:
                raise ValueError(f"GM coverage count mismatch: {name}")
        for name in ("original_soy_area_ha", "retained_soy_area_ha", "retained_area_pct"):
            if not math.isclose(float(coverage[name]), measured[name], rel_tol=1e-9, abs_tol=1e-7):
                raise ValueError(f"GM coverage area mismatch: {name}")
        if coverage.get("status") != "ok" or measured["status"] != "ok":
            raise ValueError("Insufficient GM country area coverage")
    return {**rule, "source_sites": str(sites), "source_sites_sha256": sha256(sites),
            "source_audit": str(audit_path), "source_audit_sha256": sha256(audit_path),
            "site_count": len(keep), "points_by_failed_cases": dict(sorted(counts.items())),
            "retained_area_pct": coverage["retained_area_pct"] if coverage else None,
            "gm_ok_semantics": "original flag: 1 only when GM_FAIL=0; not production eligibility"}


def verify_policy_provenance(policy):
    """Validate the recorded policy and immutable provenance in a generated config."""
    if policy is None:
        return
    rule = normalized_policy(policy)
    for field in ("source_sites", "source_audit"):
        path = Path(policy.get(field, ""))
        if not path.is_file() or sha256(path) != policy.get(field + "_sha256"):
            raise ValueError(f"GM policy provenance changed: {field}")
    audit = json.loads(Path(policy["source_audit"]).read_text(encoding="utf-8"))
    if audit.get("status") != "ok" or normalized_policy(audit.get("gm_calibration_policy")) != rule:
        raise ValueError("GM policy differs from its approved merge audit")
    if Path(audit["calibrated_sites"]).resolve() != Path(policy["source_sites"]).resolve():
        raise ValueError("GM audit points to different calibrated sites")
    if rule["maximum_failed_cases"] == 4:
        coverage = audit.get("coverage", {})
        if (coverage.get("status") != "ok" or
                float(coverage.get("minimum_area_fraction", 0)) < .95 or
                float(coverage.get("retained_area_pct", 0)) + 1e-9 < 95):
            raise ValueError("The 20/24 policy requires >=95% audited area")
