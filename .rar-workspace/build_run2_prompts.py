#!/usr/bin/env python3
"""Build identical reviewer prompts for run2 with strict schema."""
from pathlib import Path
import json

ws = Path(__file__).resolve().parent
ids = json.loads((ws / "run_ids.json").read_text(encoding="utf-8"))
plan = (ws / "plan_v1_numbered.txt").read_text(encoding="utf-8")
facts = json.loads((ws / "source_manifest.json").read_text(encoding="utf-8"))["facts"]

COMMON_CONTEXT = "\n".join(f"- {f}" for f in facts)

SCHEMA_RULES = """
CRITICAL OUTPUT CONTRACT — violation causes rejection:
Return EXACTLY one JSON object. No markdown fences. No prose outside JSON.
Top-level keys MUST be exactly:
  received_envelope, context_sufficiency, missing_context, thin_haul, raw_findings

Each raw_findings[] item MUST have EXACTLY these keys (no extras, no renames):
  raw_finding_id, severity, dimension, finding, evidence, failure_mechanism,
  resolution_required, confidence, unresolved_questions

FORBIDDEN keys on findings: focus, plan_citation, expected_missing_material, refutation

severity: must-fix | should-fix | nice-to-have
confidence: high | medium | low
evidence MUST be object with exactly:
  offending_text, location_or_expected_section
unresolved_questions MUST be an array (may be empty)
thin_haul MUST be boolean false unless you truly failed to cover the lens
context_sufficiency: sufficient | insufficient
Echo received_envelope EXACTLY character-for-character from the Envelope block below.
raw_finding_id MUST be "<dispatch_id>-rf01", "<dispatch_id>-rf02", ... sequential.
""".strip()

LENSES = {
    "lens_1": {
        "focus_statement": "goal, deliverable, feasibility, sequence, and prerequisites",
        "refutation": "Break the plan by showing that its goal, deliverable, feasibility, sequence, ownership, or hidden prerequisites cannot support the promised outcome. Cite the exact text, or identify the expected section for a missing prerequisite.",
    },
    "lens_2": {
        "focus_statement": "assumptions, risk, recovery, completeness, and verification",
        "refutation": "Break the plan by attacking its assumptions, risk handling, recovery paths, future fit, completeness, and verification. Cite the exact text, or identify the expected section for missing protection or evidence.",
    },
}

for lens_id, meta in LENSES.items():
    dispatch_id = f"{ids['full_pass_id']}-{lens_id}"
    attempt_id = f"{dispatch_id}-a1"
    envelope = {
        "run_id": ids["run_id"],
        "config_revision": ids["config_revision"],
        "config_hash": ids["config_hash"],
        "review_mode": "audit",
        "full_pass_id": ids["full_pass_id"],
        "plan_version_id": ids["plan_version_id"],
        "plan_content_hash": ids["plan_content_hash"],
        "context_package_id": ids["context_package_id"],
        "context_package_hash": ids["context_package_hash"],
        "package_identity": ids["context_package_id"],
        "dispatch_id": dispatch_id,
        "attempt_id": attempt_id,
        "retry_of_attempt_id": None,
        "role": "adversarial-plan-reviewer",
        "lens_id": lens_id,
        "lens_kind": "universal",
    }
    prompt = f"""You are a fresh adversarial plan reviewer. Try to refute the complete plan ONLY through the assigned lens. Do not praise, edit, waive, adjudicate, or decide the terminal. Cite exact plan text or the expected location of missing material. Do NOT use tools. Do NOT read files. Work only from this message.

{SCHEMA_RULES}

Envelope (copy verbatim into received_envelope):
{json.dumps(envelope, ensure_ascii=False, indent=2)}

Assigned lens:
- lens_id: {lens_id}
- kind: universal
- focus_statement: {meta['focus_statement']}
- source: universal
- user_focus_ids: []
- Refutation prompt: {meta['refutation']}

Complete line-numbered plan:
```
{plan.rstrip()}
```

Bounded supporting context:
{COMMON_CONTEXT}

Example of ONE valid finding object (structure only; invent real content for your lens):
{{
  "raw_finding_id": "{dispatch_id}-rf01",
  "severity": "must-fix",
  "dimension": "feasibility",
  "finding": "One sentence stating the weakness.",
  "evidence": {{
    "offending_text": "exact quote from the plan",
    "location_or_expected_section": "Public Interface (lines 20-23)"
  }},
  "failure_mechanism": "One atomic mechanism.",
  "resolution_required": "What the plan must add or change.",
  "confidence": "high",
  "unresolved_questions": []
}}

Prefer 2-6 atomic findings if weaknesses exist. Return ONLY the JSON object.
"""
    (ws / f"prompt_{lens_id}_run2.txt").write_text(prompt, encoding="utf-8", newline="\n")
    (ws / f"expected_envelope_{lens_id}.json").write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(lens_id, dispatch_id, "prompt_chars", len(prompt))
