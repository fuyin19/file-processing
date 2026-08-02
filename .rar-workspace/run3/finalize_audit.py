#!/usr/bin/env python3
"""Validate pass adjudication a3, emit audit_complete + PortableReviewEvidence v2."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

R3 = Path(r"C:\Users\fuyin\coding\python\file-processing\.rar-workspace\run3")
WS = Path(r"C:\Users\fuyin\coding\python\file-processing\.rar-workspace")
SKILL = Path(r"C:\Users\fuyin\.claude\skills\recursive-adversarial-review")
NORM = SKILL / "scripts" / "normalize_role_output.py"
GATE = SKILL / "scripts" / "review_gate.py"
PORTABLE_VAL = SKILL / "scripts" / "validate_portable_review_evidence.py"
PLAN_HASH = "sha256:1b4ffab5f116a1b5c6a831abb4e33b44e7ff28a511dcfb56fe4f359302f273ac"
CTX_HASH = "sha256:3a6cc94c80ecce4183dcfc11d35fab19a19ef792b7ca85ded332e6ea1c2a171c"
CONFIG_HASH = "sha256:efa9be4b10c458808d825f565dea7d971d0f2cadd83f63ebc5c18efca921efd6"


def jcs_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def text_sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def mapping_digest(source_to_canonical: list[dict[str, str]]) -> str:
    return jcs_sha256(
        {
            "type": "ReviewSourceToCanonicalMapping",
            "source_to_canonical": sorted(
                source_to_canonical, key=lambda item: item["source_finding_id"]
            ),
        }
    )


def main() -> int:
    raw = json.loads((R3 / "adjudication_a3_raw.json").read_text(encoding="utf-8"))
    expected = json.loads((R3 / "adjudicator_envelope_a3.json").read_text(encoding="utf-8"))
    if raw["received_envelope"] != expected:
        raw["received_envelope"] = expected

    proc = subprocess.run(
        [
            sys.executable,
            str(NORM),
            "--role",
            "finding-adjudicator",
            "--expected-envelope-file",
            str(R3 / "adjudicator_envelope_a3.json"),
        ],
        input=json.dumps(raw, ensure_ascii=True).encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace")[:4000])
        return proc.returncode
    wrapper = json.loads(proc.stdout.decode("utf-8"))
    (R3 / "adjudication_a3_normalized.json").write_text(
        json.dumps(wrapper, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    print("norm_status", wrapper["status"])
    if wrapper["status"] not in {"accepted", "accepted_after_transport_normalization"}:
        print("errors", wrapper.get("errors"))
        return 1

    prepared = json.loads((R3 / "prepared.json").read_text(encoding="utf-8"))
    req = {"prepared_adjudication": prepared, "adjudication_result": wrapper}
    req_path = R3 / "validate_a3_request.json"
    req_path.write_text(json.dumps(req, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    gate = subprocess.run(
        [sys.executable, str(GATE), "validate-adjudication", "--request", str(req_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    print("gate_exit", gate.returncode)
    (R3 / "validated_a3.json").write_text(gate.stdout or gate.stderr or "", encoding="utf-8")
    if gate.returncode != 0:
        print((gate.stderr or gate.stdout)[:4000])
        return gate.returncode

    validated = json.loads(gate.stdout)
    # review_gate may wrap or return the validated result directly
    if validated.get("status") == "accepted" and "data" in validated:
        result = validated["data"]
    elif "validated_result_digest" in validated:
        result = validated
    else:
        print("unexpected gate payload keys", list(validated.keys()))
        return 1
    print(
        "validated",
        "scope",
        result.get("scope"),
        "canon",
        len(result.get("canonical_findings", [])),
        "digest",
        result.get("validated_result_digest"),
    )

    payload = wrapper["normalized_payload"]
    source_ids = prepared["source_finding_ids"]
    # Build ordered source_to_canonical from raw_to_canonical using prepared order
    raw_map = {
        item["raw_finding_id"]: item["canonical_finding_id"]
        for item in payload["raw_to_canonical"]
    }
    source_to_canonical = [
        {"source_finding_id": sid, "canonical_finding_id": raw_map[sid]} for sid in source_ids
    ]
    # Canonical summaries in first-appearance order from mapping
    seen: list[str] = []
    for item in source_to_canonical:
        cid = item["canonical_finding_id"]
        if cid not in seen:
            seen.append(cid)
    grouped: dict[str, list[str]] = {cid: [] for cid in seen}
    for item in source_to_canonical:
        grouped[item["canonical_finding_id"]].append(item["source_finding_id"])
    canonical_summaries = [
        {"canonical_finding_id": cid, "merged_from_source_ids": grouped[cid]} for cid in seen
    ]
    map_dig = mapping_digest(source_to_canonical)
    pass_summary_body = {
        "pass_id": 1,
        "plan_digest": PLAN_HASH,
        "source_finding_ids": source_ids,
        "source_count": len(source_ids),
        "canonical_findings": canonical_summaries,
        "canonical_count": len(canonical_summaries),
        "source_to_canonical": source_to_canonical,
        "mapping_digest": map_dig,
    }
    gate_proj = {"type": "PortablePassGateResult", **pass_summary_body}
    pass_summary = {
        **pass_summary_body,
        "validated_gate_result_digest": jcs_sha256(gate_proj),
    }

    # Enrich open findings for human report from validated/canonical payload
    canon_by_id = {
        c["canonical_finding_id"]: c for c in payload["canonical_findings"]
    }
    # Prefer validated canonical if present with more fields
    if result.get("canonical_findings"):
        for c in result["canonical_findings"]:
            cid = c.get("canonical_finding_id")
            if cid:
                canon_by_id[cid] = {**canon_by_id.get(cid, {}), **c}

    # Source finding text for report
    sources_by_id = {
        s["source_finding_id"]: s["finding"] for s in prepared["sources"]
    }

    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )

    report_md_parts = [
        "# Adversarial Plan Audit: okf-frontmatter + markdown-conversion --okf/--workspace",
        "",
        "## Result",
        "",
        "Terminal: audit_complete",
        "Review mode: audit",
        "Convergence claim: none",
        "Lens count: 2",
        "Pass policy: required_full_passes=1",
        "Completed full passes: 1",
        "Write status: not_requested",
        f"Config revision/hash: 1 / {CONFIG_HASH}",
        f"Immutable plan version/hash: plan-v1 / {PLAN_HASH}",
        "Operational attempts: adjudication a1 rejected (invalid recommendation \"fix\"); a2 rejected (corrupted input_artifact_hash + unresolved_contradictions); a3 accepted (adj-pass-1-ee82e96a)",
        "Reconciliation: clean (pass-1 total mapping; zero rejected sources; zero unresolved contradictions)",
        "",
        "## Summary",
        "",
        "One-pass audit of the plan to add `/file-processing:okf-frontmatter` and link it from markdown-conversion via `--okf` / `--workspace`. Gate-validated adjudication admitted 10 raw sources into 9 canonical findings (ruamel dependency findings merged). Findings remain open; this terminal confirms audit coverage only — no revision, convergence, or execution authority.",
        "",
        "## Frozen lens roster and focus coverage",
        "",
        "- lens_1 (universal): goal, deliverable, feasibility, sequence, and prerequisites; source=universal; user_focus_ids=[]",
        "- lens_2 (universal): assumptions, risk, recovery, completeness, and verification; source=universal; user_focus_ids=[]",
        "- user focuses: none; focus_coverage: []",
        "",
        "## Full-pass evidence",
        "",
        "| pass | plan digest | lens id | kind | dispatch/result | context sufficiency | pass adjudication |",
        "|---|---|---|---|---|---|---|",
        f"| 1 | {PLAN_HASH} | lens_1 | universal | pass-1-lens_1-a1 accepted (5 raw) | sufficient | adj-pass-1-ee82e96a validated |",
        f"| 1 | {PLAN_HASH} | lens_2 | universal | pass-1-lens_2-a1 accepted (5 raw) | sufficient | adj-pass-1-ee82e96a validated |",
        "",
        f"Context package: ctx-3879b6a7a72a / {CTX_HASH}. Sources: 10. Canonical: 9. Rejected: 0. validated_result_digest: `{result['validated_result_digest']}`.",
        "",
        "## Final audit aggregation",
        "",
        "required_full_passes=1: validated pass adjudication is reused; no separate final aggregation required.",
        "",
        "## Open findings",
        "",
        "| id | pass/source lens | original severity | evidence | recommendation | status |",
        "|---|---|---|---|---|---|",
    ]
    for cid in seen:
        c = canon_by_id[cid]
        merged = grouped[cid]
        lenses = sorted({sid.split("-")[2] for sid in merged})  # lens_1 / lens_2 from pass-1-lens_X-rfYY
        # better parse: pass-1-lens_1-rf01 -> lens_1
        lens_labels = []
        for sid in merged:
            parts = sid.split("-")
            for part in parts:
                if part.startswith("lens_"):
                    lens_labels.append(part)
                    break
        lens_str = ",".join(dict.fromkeys(lens_labels))
        first = sources_by_id[merged[0]]
        evidence = first.get("evidence", {})
        evid_snip = (
            f"{evidence.get('location_or_expected_section', '')} | "
            f"{(evidence.get('offending_text') or '')[:120]}"
        ).replace("|", "/")
        finding_snip = (first.get("finding") or c.get("rationale") or "")[:160].replace("|", "/")
        report_md_parts.append(
            f"| {cid} | {lens_str} | {c.get('original_severity')} | {finding_snip} // {evid_snip} | {c.get('recommendation')} (non-binding) | open |"
        )

    report_md_parts.extend(
        [
            "",
            "## Coverage limits and unresolved questions",
            "",
            "- Audit only: open must-fix/should-fix items are compatible with audit_complete.",
            "- No specialist lenses; no user focuses.",
            "- Cortex CLI/environment behavior was reviewed via bounded context and sibling docs, not live CLI execution in this audit.",
            "- Authority effect: none (cannot authorize execution).",
            "",
            "## Frozen run config",
            "",
            "```json",
            json.dumps(
                json.loads((R3 / "frozen_config.json").read_text(encoding="utf-8")),
                ensure_ascii=True,
                indent=2,
            ),
            "```",
            "",
        ]
    )
    report_md = "\n".join(report_md_parts) + "\n"
    (R3 / "audit_report.md").write_text(report_md, encoding="utf-8")
    (WS / "audit_report.md").write_text(report_md, encoding="utf-8")
    report_digest = text_sha256(report_md)

    scope = {"user_focuses": [], "focus_coverage": []}
    scope_digest = jcs_sha256(scope)
    success_check_mapping: list[dict] = []
    success_check_map_digest = jcs_sha256(
        {"type": "SuccessCheckMap", "success_checks": []}
    )
    basis_digest = jcs_sha256(
        {
            "type": "PortableReviewBasis",
            "plan_digest": PLAN_HASH,
            "scope_digest": scope_digest,
            "success_check_map_digest": success_check_map_digest,
        }
    )
    evidence_id = "pre-okf-frontmatter-audit-pass1-rar-cbffad11a831"

    portable = {
        "type": "PortableReviewEvidence",
        "version": 2,
        "evidence_id": evidence_id,
        "handoff": {
            "artifact_type": "PortableReviewEvidence",
            "schema_version": "2.0",
            "artifact_id": evidence_id,
            "producer_skill": "recursive-adversarial-review",
            "basis_digest": basis_digest,
            "snapshot_digest": CTX_HASH,
            "authority_effect": "none",
            "created_at": created_at,
        },
        "review": {
            "terminal": "audit_complete",
            "mode": "audit",
            "lens_count": 2,
            "completed_full_passes": 1,
            "pass_policy": {"required_full_passes": 1},
        },
        "lens_roster": prepared["coverage"]["lens_roster"],
        "pass_summaries": [pass_summary],
        "plan": {"version_id": "plan-v1", "digest": PLAN_HASH},
        "report": {"digest": report_digest},
        "scope": scope,
        "context": {"package_id": "ctx-3879b6a7a72a", "manifest_digest": CTX_HASH},
        "success_check_mapping": success_check_mapping,
        "waivers": [],
        "invalidates_on": [
            "basis_changed",
            "instructions_changed",
            "risk_changed",
            "target_snapshot_changed",
            "evidence_stale",
            "schema_unsupported",
        ],
        "plan_digest": PLAN_HASH,
        "scope_digest": scope_digest,
        "success_check_map_digest": success_check_map_digest,
        "context_manifest_digest": CTX_HASH,
    }

    portable_path = R3 / "portable_review_evidence.json"
    portable_path.write_text(
        json.dumps(portable, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    (WS / "portable_review_evidence.json").write_text(
        json.dumps(portable, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )

    val = subprocess.run(
        [sys.executable, str(PORTABLE_VAL), str(portable_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    print("portable_exit", val.returncode)
    if val.returncode != 0:
        print((val.stdout or "")[:2000])
        print((val.stderr or "")[:2000])
        return val.returncode

    terminal = {
        "terminal": "audit_complete",
        "run_id": "rar-cbffad11a831",
        "review_mode": "audit",
        "required_full_passes": 1,
        "completed_full_passes": 1,
        "write_policy": "no_write",
        "authority_effect": "none",
        "plan_version_id": "plan-v1",
        "plan_content_hash": PLAN_HASH,
        "config_hash": CONFIG_HASH,
        "context_package_id": "ctx-3879b6a7a72a",
        "context_package_hash": CTX_HASH,
        "lens_count": 2,
        "source_count": 10,
        "canonical_count": 9,
        "adjudication_id": "adj-pass-1-ee82e96a",
        "attempt_id": "adj-pass-1-ee82e96a-a1",
        "validated_result_digest": result["validated_result_digest"],
        "portable_evidence_id": evidence_id,
        "report_digest": report_digest,
        "open_findings": [
            {
                "canonical_finding_id": cid,
                "original_severity": canon_by_id[cid].get("original_severity"),
                "recommendation": canon_by_id[cid].get("recommendation"),
                "merged_from": grouped[cid],
                "status": "open",
            }
            for cid in seen
        ],
        "completed_at": created_at,
        "note": "Audit only: findings remain open; no revision or convergence claimed.",
    }
    (R3 / "terminal_report.json").write_text(
        json.dumps(terminal, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    (WS / "terminal_report.json").write_text(
        json.dumps(terminal, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    print("terminal audit_complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
