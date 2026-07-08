---
name: content-review
description: |
  Review and verify files for grammatical, logical, factual, and stylistic issues.
  Use this skill whenever the user wants to:
  - Check a document for grammar, spelling, or typo errors
  - Review writing quality, style, or readability
  - Find logical issues, contradictions, or inconsistencies in a document
  - Verify a document against reference materials (fact-checking)
  - Check if content goes beyond or contradicts its source references
  - Audit a file for content quality
  Even if they don't say "content review", if they mention proofreading, fact-checking,
  verifying against sources, checking writing quality, or finding issues in a document, use this skill.
metadata:
  version: 2.0.0
---

# Content Review

Review files for grammatical, logical, stylistic, and factual issues. Supports standalone review and reference-based verification. Produces a structured markdown report with a unified diff of suggested fixes.

**Two runtime modes (preflight on every run):**

- `orchestrated` — the main agent can spawn sub-agents. Review runs as a
  **dimension × chunk matrix**: one sub-agent per (dimension, chunk). This is the
  anti-skip design — narrow, exhaustive cells replace one agent "doing everything".
- `legacy_single_agent` — no sub-agent tool available. Falls back to the original
  two-pass inline review. Output is stamped "legacy-single-agent; no matrix
  guarantee".

The matrix flow is deterministic and testable: `scripts/review_plan.py` computes
the plan and assembles results; the main agent only dispatches sub-agents and
writes their JSON to the workspace.

## Markitdown Artifact Awareness

Many `.md` files originate from markitdown conversion. The following are **known conversion artifacts** and should NOT be reported as issues:

- Orphaned image filenames on standalone lines (e.g., `image1.png`, `photo.jpg`)
- Markdown image syntax (`![...](...)`)
- Broken or malformed tables (merged cells, misaligned columns)
- YAML frontmatter blocks (`---` delimited with `source:`, `converted_at:`, `converted_by:` fields)
- Extra blank lines or collapsed sections from image removal
- Encoding artifacts already handled by the pipeline

Only flag these if the user explicitly asks about formatting or conversion quality.

## Commands

### `/file-processing:content-review <filepath-or-url> [options]`

Review a file for content quality issues. In `orchestrated` mode, performs a dimension × chunk matrix review (grammar-style + logic-consistency; fact-check added with `--references`). In `legacy_single_agent` mode, performs the two-pass inline review.

**Arguments:**
- `filepath-or-url` (required): Path to a local file or URL to review
- `--references, -r`: One or more reference files to verify against (triggers fact-check dimension)
- `--language, -l`: Language of the content (default: auto-detect). Affects grammar rules and spelling expectations.
- `--focus grammar|style|logic|consistency|all`: What to focus on (default: all)
- `--output, -o`: Write the review report to a file instead of displaying inline
- `--chunk-lines`: Override chunk size (default 400, from `scripts/config.json`). Raise it for very large documents to stay under the `max_cells` cap.
- `--accept-partial`: Allow exceeding caps / emit a partial diff when a required cell FAILED.

**Supported file types:** `.md`, `.txt`, `.html`, `.rtf`, `.docx` (via markitdown), `.pdf` (via markitdown). Unsupported types are skipped with a warning.

**Verification mode** (when `--references` is provided): adds the `fact-check` dimension — factual consistency, scope violations (potential fabrication), and omissions against the references.

**Examples:**
```
/file-processing:content-review ~/Documents/report.md
/file-processing:content-review ~/Downloads/article.pdf --focus grammar
/file-processing:content-review ~/Notes/meeting-notes.docx --language zh
/file-processing:content-review https://example.com/page.html --output review-report.md
/file-processing:content-review ~/Documents/summary.md --references ~/Documents/source-report.pdf
/file-processing:content-review ~/Docs/draft.md --references ~/Docs/ref1.md ~/Docs/ref2.md --focus consistency
/file-processing:content-review ~/Big/manual.md --chunk-lines 800
```

## Reference Files

- **Grammar & Spelling**: `references/grammar-and-spelling.md`
- **Style**: `references/style.md`
- **Logic & Consistency**: `references/logic-and-consistency.md`
- **Sub-agent prompts & JSON shapes**: `references/subagent-prompts.md` (the single source of truth for how the orchestrator dispatches reviewers)
- **Report templates**: `assets/report-template.md`

## Runtime Preflight

Before any review, record `runtime_mode`:

- **`orchestrated`** if the main agent has a sub-agent tool available (Claude Code default: `Agent`; legacy name `Task`). This tool is core and is only removed if `allowed-tools` restricts it — this skill must NOT set `allowed-tools`.
- **`legacy_single_agent`** otherwise (e.g., a host without sub-agent dispatch). Announce this to the user and do not claim matrix coverage.

If the host removes the sub-agent tool, fall back to `legacy_single_agent` and stamp the output. Never silently degrade.

## Workflow (orchestrated — matrix review)

### Step 1: Compute the matrix

```bash
python scripts/review_plan.py plan \
  --input "<filepath-or-url-or-local>" \
  [--references <ref1> [ref2 ...]] \
  [--focus grammar|style|logic|consistency|all] \
  [--chunk-lines <N>] \
  [--plan-output .review-workspace/plan.json]
```

Gate: exit `0` = success (plan JSON printed + chunk files written to `.review-workspace/`). Exit `2` = caps exceeded (`max_chunks` / `max_cells`) — raise `--chunk-lines`, reduce `--focus`, or re-run with `--accept-partial`. Exit `1` = error.

The plan JSON contains `cells`: the exact list of `(dimension, chunk)` pairs to review. **The main agent MUST dispatch exactly one sub-agent per cell** — print the cell count before dispatching and the number of results after; fewer is step-skipping and must be corrected.

### Step 2: Fan out the matrix

For each cell in `plan.cells`, dispatch one sub-agent using the prompt template for its dimension from `references/subagent-prompts.md`. Pass the chunk path (`<workspace>/chunk_NNN.md`), the chunk's `start`–`end` line range, the chunk index, and (for `fact-check`) the reference paths. Run concurrently, batched to the runtime's concurrency limit; if a batch returns partial results, re-dispatch only the missing cells.

Each sub-agent returns ONLY its JSON. Write each result to `<workspace>/<dimension>__<chunk:03d>.json`. A result that is non-JSON, wrong shape, or has a mismatched `cell` is re-dispatched **up to 2 times**; still failing → leave the file missing (the assembler marks it `FAILED`).

**Coverage accounting is mandatory:** after dispatch, every cell must have a result file (ok / empty / or absent=FAILED). An empty findings list is a legitimate result; only missing/unparseable/mismatched cells are `FAILED`.

### Step 3: Assemble

```bash
python scripts/review_plan.py assemble \
  --plan .review-workspace/plan.json \
  --cells-dir .review-workspace \
  [--output <report.md>] \
  [--accept-partial]
```

Gate: the assembler validates every cell, dedupes findings across the matrix, fills `assets/report-template.md` (Standalone or Verification), and emits a unified diff for `fixable` findings. If any **required** cell (`grammar-style` / `logic-consistency` / `fact-check`) is `FAILED`, the report is marked **incomplete** and no diff is produced unless `--accept-partial` is set — surface the `FAILED` cells to the user and re-dispatch them rather than shipping a partial review silently.

If `--output` is specified, write the report + diff there; otherwise display inline. Always show the coverage table so the user can see which cells ran, which were empty, and which failed.

## Workflow (legacy_single_agent — fallback)

When the preflight records `legacy_single_agent`, run the original two-pass inline review (no `review_plan.py`, no sub-agents):

1. **Read** the file (Read; `.docx`/`.pdf` via the markdown-conversion skill; URLs via WebFetch).
2. **Pass 1 — surface**: apply `references/grammar-and-spelling.md` + `references/style.md`.
3. **Pass 2 — deep**: apply `references/logic-and-consistency.md`; with `--references`, add the Verification Mode checks (factual consistency, scope, omissions).
4. **Report**: fill `assets/report-template.md`; produce a unified diff for mechanical fixes.

Stamp the output: **"legacy-single-agent; no matrix guarantee"**.
