---
name: okf-frontmatter
description: >-
  Prepare, repair, or validate YAML frontmatter in Markdown files for generic
  OKF or Cortex workspaces. Use when a user asks to add missing type, title,
  description, timestamp, tags, or policy-required metadata; make Markdown
  OKF-ready or Cortex-ready; review frontmatter before writing; process a
  Markdown directory; or apply a previously sealed frontmatter plan. Do not use
  for Cortex ingest, publish, indexing, or direct edits inside an active bundle.
metadata:
  version: 1.0.0
---

# Prepare OKF frontmatter

Use the deterministic `scripts/frontmatter_pipeline.py` boundary. Never edit a
target Markdown file directly and never claim that this workflow publishes it
to Cortex.

## Interface

```text
/file-processing:okf-frontmatter <file-or-directory>
  [--workspace <cortex-root>]
  [--metadata-json <path>]
  [--replace-fields <csv>]
  [--no-recursive]
  [--plan-only]
  [--apply-plan <plan>]
  [--accept-partial]
  [--keep-workspace]
```

Without `--workspace`, prepare the four-field portable authoring baseline:
`type`, `title`, `description`, and `timestamp`. With `--workspace`, require a
Cortex 2.1-compatible CLI on `PATH`; bind the active policy digest and add every
policy-declared required field.

## Workflow

If `--apply-plan <plan>` was supplied, skip Prepare and Propose: inspect that
exact sealed plan with the user, then continue at Apply. Do not accept new
metadata or target arguments alongside it.

### 1. Prepare

```bash
python scripts/frontmatter_pipeline.py prepare \
  --input "<file-or-directory>" \
  [--workspace "<cortex-root>"] \
  [--metadata-json "<metadata.json>"] \
  [--replace-fields "title,tags"] \
  [--no-recursive] [--accept-partial] [--plan-only] [--keep-workspace]
```

Exit `2` means preparation succeeded but required semantic fields still need a
proposal. Retain the printed `state_path`. Exit `4` is a Cortex prerequisite or
policy block; do not fall back silently to generic OKF.

Read `run.json`. When `context.policy_files` is non-empty, read every declared
file and use only its verified vocabulary and field semantics. Do not read or
write other Cortex internals.

### 2. Propose metadata

Produce exactly one `FrontmatterProposal/v1` item per selected input unless the
run explicitly permits partial coverage. Preserve existing non-empty fields;
only fields listed in `replace_fields` may be replaced. Never propose
`source_ids`.

```json
{
  "schema_version": "FrontmatterProposal/v1",
  "run_id": "frontmatter-run@sha256:...",
  "complete_coverage": true,
  "items": [
    {
      "path": "<absolute input path from run.json>",
      "input_sha256": "<hash from run.json>",
      "fields": {
        "type": "Reference",
        "description": "One sentence grounded in the Markdown body."
      },
      "evidence": {
        "type": "Document purpose and section structure.",
        "description": "Summary of the document's stated subject."
      }
    }
  ]
}
```

Every proposed field requires a non-empty evidence string. If a custom required
field lacks reliable evidence, stop and ask the user; do not invent a value.

### 3. Seal and review the plan

```bash
python scripts/frontmatter_pipeline.py plan \
  --state "<run.json>" --proposal "<proposal.json>"
```

Require exit `0`. Inspect the sealed plan's `frontmatter_before`,
`frontmatter_after`, changed fields, targets, skipped items, readiness, and
policy digest. Present that frontmatter-only diff to the user. If `--plan-only`
was requested, stop after reporting the plan path.

Before Apply, require explicit confirmation in the current conversation. If the
user declines and `--keep-workspace` was not requested, delete only the exact
system-temporary run directory printed by prepare.

### 4. Apply and validate

```bash
python scripts/frontmatter_pipeline.py apply --plan "<sealed-plan.json>"
python scripts/frontmatter_pipeline.py validate --input "<target>" [--workspace "<cortex-root>"]
```

Apply only the exact reviewed plan. Exit `3` means its content, target, staged
bytes, or policy digest is stale or tampered; prepare a fresh run. Report the
`FrontmatterApplyReceipt/v1` and final readiness:

- `okf_ready` - portable OKF authoring metadata is complete.
- `cortex_authoring_ready` - Cortex authoring metadata is complete and no valid
  custom policy is active.
- `cortex_policy_ready` - authoring metadata and the active policy are complete.
- `blocked` - never describe the document as ready.

## Safety

- Reject malformed or duplicate-key YAML, non-mapping frontmatter, symlinks,
  `index.md`, and `log.md`.
- Preserve YAML comments, ordering, quoting, unknown keys, provenance fields,
  and the Markdown body through round-trip parsing.
- Never create or replace `source_ids`; Cortex owns that field during ingest.
- Never write inside the active Cortex bundle. Route bundle mutations through
  Cortex's own content-addressed plan/apply boundary.
- A successful Apply cleans the system-temporary run unless `--keep-workspace`
  or `--plan-only` retained it. Failures retain the run and print its path.

## Exit codes

- `0` - command completed successfully.
- `1` - invalid input, YAML, proposal, or usage.
- `2` - recoverable incomplete metadata/coverage.
- `3` - stale, conflicting, replayed, or tampered plan.
- `4` - Cortex CLI, method contract, workspace, or policy failure.
