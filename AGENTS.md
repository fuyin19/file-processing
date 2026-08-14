# AGENTS.md

This file is the single source of project guidance for the `file-processing`
plugin. `CLAUDE.md` is a one-line `@AGENTS.md` import; do not maintain a second
copy of this content there.

> This file is static project guidance. Do not modify it unless the user
> explicitly asks for refresh/sync/update — the minimal Core and scope design
> depends on it not drifting.

## Project Purpose

`file-processing` is a Claude Code plugin (v4.0.0) that packages four
file-processing skills as `/file-processing:<name>` commands. It serves
developers working inside Claude Code who need repeatable, script-backed
document workflows — convert, review, translate, clean up — where deterministic
Python pipelines do the structural work and Claude / sub-agents do the
linguistic and judgment work. It exists so these workflows are consistent,
testable, and resistant to LLM step-skipping (via sub-agent orchestration)
rather than depending on ad-hoc per-conversation prompting.

Immutable long-term tradeoff (one line): for `translate`, accuracy / terminology
match is prioritized over speed and token cost; across all skills,
structure-safety and testability are prioritized over feature breadth.

The four skills:

- **markdown-conversion** (v6.5.0) — Convert local PDFs, AnyDoc-eligible
  non-PDF documents, remaining supported files, URLs, or directories through one
  canonical model. Local PDFs use a behaviorally compatible PDF Inspector as the
  authoritative text and structure source; RapidOCR recovers only routed pages,
  while PDFium remains limited to OCR rasterization and image-object export.
  Office inputs receive bounded read-only preflight; Word conversion uses a
  temporary accepted/final-view revision snapshot, and only a provider-typed
  DOCX `max_xml_nodes` capacity error may trigger ordered structural sharding.
  AnyDoc remains the default local adapter and MarkItDown the explicit rollback
  and URL/other-format adapter. Native providers run in deadline-bounded workers,
  Office images are exported without OCR by default, and `--enrich-images`
  explicitly adds provenance-linked OCR text in bundle mode. The default output
  is a JSON + Markdown bundle, while `--output-mode markdown` emits one clean
  Markdown file. Canonical output preserves raw/cleaned/normalized text, uses
  exact five-field YAML frontmatter, defaults Chinese normalization to
  simplified, and publishes transactionally.
- **content-review** (v2.0.0) — Review files for grammar, typos, logic, and stylistic issues. Verify content against reference materials (fact-checking). `scripts/review_plan.py` computes a dimension × chunk matrix and assembles sub-agent results; `references/` (criteria + sub-agent prompts) and `assets/` (report template).
- **markdown-cleanup** (v1.0.0) — Clean up formatting artifacts in markitdown-converted .md files. Pure Python stdlib.
- **translate** (v2.0.0) — Translate files to a target language with optional reference-guided terminology. Hybrid architecture: Python pipeline (`translate_pipeline.py`, `glossary_utils.py`) for deterministic work (structure-safe chunking, source-driven glossary slicing, per-occurrence forced-application QA); Claude/sub-agents for linguistic work.

## Goal Format

How future `/goal` (or similar goal-driven) tasks in this repo should be written.
This section is **not** a current objective and **not** a peer of Project
Purpose — it stores "how future goals are expressed", not what the current goal
is.

Every goal includes, at minimum:

- **Objective** — the concrete outcome this task delivers.
- **Definition of Done** — DoD must name the verification evidence. For this
  repo that usually means a specific `python -m pytest tests/<skill>/...` command
  (or a pipeline invocation) plus its expected exit code / output. No separate
  mandatory field named Verification or Work Loop is required.

Optional fields (add only when they'd change execution):

- **Constraints** — hard boundaries for this task (e.g. "do not touch
  markdown-conversion or markdown-cleanup"; "no new runtime dependencies").
- **Work Plan / Work Loop** — multi-step rhythm, e.g. `write test → run pytest →
  implement → run full suite`.
- **Non-Goals**, **State** (progress ledger for long tasks), **Reviewer**,
  **Recovery**, **Cost Boundary**, **Stop / Escalation**.

Transform vague asks into verifiable goals:

- "add a cleanup fixer" → "add a fixer plus a test fixture it must clean; run
  `python -m pytest tests/markdown-cleanup/test_cleanup_pipeline.py -v` and
  confirm the new test passes and existing fixers are unchanged."
- "fix a pipeline bug" → "add a reproducing `pytest` case that fails, then make
  it pass; run the full suite to confirm no regression."
- "add validation" → "write tests for invalid inputs, then make them pass."
- "fix bug" → "write a reproducing test, then make it pass."

Scope red line: `Objective` belongs to a specific goal, not to this file's top
level. `Project Purpose` belongs to the repo, not to a goal. General constraints
live in the behavioral expectations implied by the sections below, not repeated
per goal.

## Commands

### Running Tests

```bash
# Markdown-conversion tests
python -m pytest tests/markdown-conversion/test_pipeline.py -v

# Markdown-cleanup tests
python -m pytest tests/markdown-cleanup/test_cleanup_pipeline.py -v

# Content-review tests
python -m pytest tests/content-review/test_review_plan.py -v

# Translate tests
python -m pytest tests/translate/test_translate_pipeline.py -v

# Single test or pattern
python -m pytest tests/markdown-conversion/test_pipeline.py -k "test_fix_encoding_utf8" -v
python -m pytest tests/translate/test_translate_pipeline.py -k "glossary" -v

# Categories (markdown-conversion)
python -m pytest tests/markdown-conversion/test_pipeline.py -k "pdf_inspector or product_pdf_bundle" -v    # PDF Inspector product path
python -m pytest tests/markdown-conversion/test_pipeline.py -k "legacy_pdfium" -v    # retained v6.2 adapter characterization only
python -m pytest tests/markdown-conversion/test_pipeline.py -k "bundle or markdown_only or replace_failure" -v    # publication / rollback
python -m pytest tests/markdown-conversion/test_pipeline.py -k "canonical or semantic_validator or normalization" -v    # schema / text fidelity
python -m pytest tests/markdown-conversion/test_pipeline.py -k "markitdown or ooxml or office" -v    # Office regression
```

All test files use the same pattern: `sys.path` includes the skill's `scripts/` dir, and integration tests run the pipeline as a subprocess via `SCRIPT = [sys.executable, <pipeline.py path>]`.

**Test config isolation**: All test suites pass `--config tests/<skill>/fixtures/test_config.json` via `CONFIG_ARG` to avoid touching the real `config.json` (which may contain API keys or local settings). When adding subprocess-based tests, always use `CONFIG_ARG` from the test file.

### Running Pipelines Directly

```bash
# Markdown-conversion — single file, default bundle beside the source
python skills/markdown-conversion/scripts/pipeline.py --input <file>

# Markdown-conversion — one clean Markdown file
python skills/markdown-conversion/scripts/pipeline.py --input <file> --output-mode markdown --output-path <out.md>

# Markdown-conversion — URL
python skills/markdown-conversion/scripts/pipeline.py --input <url> --output-mode markdown --output-path <out.md>

# Markdown-conversion — batch
python skills/markdown-conversion/scripts/pipeline.py --input-dir <dir> [--output-dir <outdir>] [--output-mode bundle|markdown] [--types pdf,docx] [--no-recursive] [--overwrite|--rename]

# Markdown-conversion — alternate config / version
python skills/markdown-conversion/scripts/pipeline.py --config <path> --input <file> [--output-dir <outdir>]
python skills/markdown-conversion/scripts/pipeline.py --version

# Markdown-cleanup — single file or directory
python skills/markdown-cleanup/scripts/cleanup_pipeline.py --input <file.md>
python skills/markdown-cleanup/scripts/cleanup_pipeline.py --input <dir>

# Markdown-cleanup — preview / selective fixers / list fixers
python skills/markdown-cleanup/scripts/cleanup_pipeline.py --input <file.md> --dry-run --diff
python skills/markdown-cleanup/scripts/cleanup_pipeline.py --input <file.md> --only base64_image_stubs,blank_lines
python skills/markdown-cleanup/scripts/cleanup_pipeline.py --list-fixers
```

**Exit codes (markdown-conversion)**: 0=success (including publishable warnings/partial output), 1=input/conversion/validation/publication error, 2=single-target collision (needs `--overwrite` or `--rename`). `--overwrite` uses staged replacement with rollback before commit; `--rename` appends a deterministic suffix.

**Exit codes (markdown-cleanup)**: 0=success, 1=error. Output writes to same directory as source by default.

```bash
# content-review — matrix plan + assemble (deterministic, testable without sub-agents)
python skills/content-review/scripts/review_plan.py plan --input <file> [--references <ref>...] [--focus all|grammar|style|logic|consistency] [--chunk-lines 400] [--dry-run] [--plan-output plan.json]
python skills/content-review/scripts/review_plan.py assemble --plan plan.json --cells-dir <workspace> [--output report.md] [--accept-partial]

# translate — prepare emits a structure-safe CHUNK PLAN + PASSAGE MANIFEST (legacy SOURCE/REFERENCES/INSTRUCTIONS sections preserved)
python skills/translate/scripts/translate_pipeline.py prepare --input <file> --language <lang> [--references <ref>...] [--glossary <seed>...] [--chunk-lines 300]
python skills/translate/scripts/translate_pipeline.py qa --source <file> --translation <translated.md> --language <lang> [--workspace .translate-workspace] [--glossary <g>]
```

**Exit codes (content-review `review_plan`)**: 0=success, 1=error, 2=caps exceeded (raise `--chunk-lines`, reduce `--focus`, or pass `--accept-partial`).
**Exit codes (translate `qa`)**: 0=pass (no errors), 1=errors present — re-translate the chunks in the printed FIX MAP, then re-run.

### Content Review and Translate (slash-command invocation)

```bash
# Content review
/file-processing:content-review <filepath-or-url> [--focus grammar|style|logic|consistency|all] [--language <lang>]
/file-processing:content-review <filepath-or-url> --references <ref1> [ref2...] [--focus grammar|style|logic|consistency|all]

# Translate
/file-processing:translate <filepath> --language <lang>
/file-processing:translate <filepath> --language <lang> --references <ref1> [ref2 ...]
/file-processing:translate <filepath> --language <lang> --glossary <glossary.json> --overwrite
```

## Architecture

### Skill Structure

The plugin lives in `skills/` with one subdirectory per skill. Each skill has a `SKILL.md` defining the `/file-processing:<name>` command and workflow. All four skills are script-backed. `content-review` and `translate` additionally keep sub-agent prompt templates in `references/subagent-prompts.md`.

### Pipeline Flow (markdown-conversion)

1. **Precheck and target resolution** — validates `--input` versus `--input-dir`, CLI combinations, output roots, source/output aliasing, and collisions before loading expensive adapters.
2. **Preflight and adapter selection** — Office containers receive bounded ZIP,
   XML, relationship, and image-budget checks before conversion. Word revisions
   are classified by exact namespace-aware tags and converted from a temporary
   accepted/final-view snapshot without changing the source. AnyDoc handles
   eligible local non-PDF inputs; only its typed DOCX `max_xml_nodes` capacity
   error may enter ordered structural sharding and Canonical merge. Other
   provider errors and safety limits fail closed. Local PDFs use a behaviorally
   compatible PDF Inspector full-document result as the authoritative text and
   structure source. Standard Adobe CJK Identity fonts without `ToUnicode` are
   repaired only in a temporary copy. Inspector page signals route required
   pages to RapidOCR; selected-page removal or replacement still requires a
   unique complete signature, otherwise the whole document routes to ordered
   OCR. PDFium never supplies canonical text or structure. URLs and remaining
   local formats use MarkItDown, and `--local-document-adapter markitdown`
   remains the explicit rollback. AnyDoc, PDF Inspector, RapidOCR, local
   MarkItDown, and URL MarkItDown execute in deadline-bounded workers. URL fetch
   policy rejects private destinations, revalidates redirects, limits bytes and
   time, and redacts sensitive locator data.
3. **Canonical assembly** — builds Canonical JSON v1 with source hash, stable document/node ids, source units/locators, authoritative `content` order, general tables, assets, relationships, and quality warnings.
4. **Language normalization** — preserves `raw_text` and cleaned `text`, then produces `normalized_text` in one batched OpenCC pass while protecting code, URLs, paths, ids, hashes, locators, and formulas.
5. **Validation and rendering** — validates JSON Schema plus semantic references, paths, hashes, output manifests, and quality state; renders Markdown from canonical content with exact five-field frontmatter unless disabled.
6. **Publication** — default `bundle` writes `<stem>/<stem>.json`, `<stem>.md`, and optional `assets/images/`; `--output-mode markdown` writes exactly one clean `.md` and omits image binaries/dead links.
7. **Transactional replace** — stages complete output beside the target, validates before commit, rolls back pre-commit replacement failures, and treats post-commit backup cleanup failure as a non-fatal maintenance warning.

Provider compatibility is capability-based rather than an exact/minimum/maximum
version gate. The pipeline never installs, upgrades, downgrades, or repairs
provider packages at runtime; missing or incompatible providers fail explicitly.
PDF Inspector supplies full-document headings, paragraphs, lists, tables, line
wrapping, and reading order but does not execute OCR. The default `auto` mode
refines Inspector's OCR-routing signal and retains conservatively readable
standard-CID-repaired pages; `off` leaves remaining required pages unrecovered
after removing any proven untrusted Inspector span, and `force` routes every page
to RapidOCR. OCR output has exact page provenance; an unrecovered required page
remains `ocr_required` and produces publishable `partial` output when other
usable content exists. PDFium native text is never a fallback. Office image
assets are relationship-reconciled: unresolved placements are recorded without
guessing content position, and image OCR is opt-in through `--enrich-images`.
Accepted/final-view revision text is preserved, while revision history and
comments remain explicitly reported losses. RAG chunk schemas, advanced
formulas, revision-history preservation, and model enrichment remain non-goals.

### Cleanup Pipeline Flow (markdown-cleanup)

1. **Precheck** → **Load config** (merge `DEFAULT_CONFIG` with `config.json`, resolve `--only`/`--disable`)
2. **Collect .md files** → **Protect** (extract frontmatter, replace code blocks with placeholders)
3. **Run fixers** — pure functions `(text) → (text, changes)`, executed in defined order. 12 fixers total (10 enabled, 2 disabled by default). Key principle: preserve meaningful structure (PPT slide boundaries, list numbering, sections).
4. **Restore** → **Write** → **Report**

Uses only Python stdlib (`re`, `difflib`).

### Translate Pipeline Flow (translate)

Two runtime modes (preflight in SKILL.md records `runtime_mode`):

- **`orchestrated`** (default when a sub-agent tool is available):
  1. **Prepare** — reads input (markitdown for non-.md), collects references recursively, **structure-safe chunks** the source + references (writes `.translate-workspace/chunk_NNN.md` + passage files with stable ids), preserves the legacy `=== SOURCE TEXT ===` / `=== REFERENCES ===` / `=== INSTRUCTIONS ===` sections and adds `=== CHUNK PLAN ===` + `=== PASSAGE MANIFEST ===`.
  2. **Glossary (source-driven, two phases)** — G1 sub-agents extract candidate terms per source chunk; G2 sub-agents ground them against pre-selected reference passages; merged into a structured `{"terms":[...]}` glossary (with `confidence` + `source_chunks` + `occurrences`). `confidence:"none"` entries carry `target:null`.
  3. **Translate** — one translator sub-agent per chunk with a sliced glossary (`slice_glossary_for_chunk`); returns `{translated_markdown, self_audit}`; orchestrator validates + writes `chunk_NNN.<lang>.md`.
  4. **QA** — per-occurrence forced application (target must appear in each chunk where the source occurs; convergence-gated), structural counts, untranslated-fragment detection, `confidence:none` consistency. Auto-discovers the glossary via `derive_glossary_path`. Emits a **FIX MAP** (`term → chunk`) and exits 1 on errors.
  5. **Write** — blocked on QA errors / `FAILED` required stages unless the user accepts a partial artifact.
- **`legacy_single_agent`** (fallback, no sub-agent tool) — original single-pass prepare → translate → qa → write; output stamped "legacy-single-agent; no matrix guarantee".

Glossary supports JSON (structured `{"terms":[...]}` or flat), CSV/TSV, and Markdown table formats via `glossary_utils.py` (legacy inputs load as `confidence:"high"` seeds).

### Sub-agent Orchestration (content-review, translate)

Both skills combat LLM "laziness / step-skipping" by replacing one agent "doing everything" with **narrow, exhaustive sub-agents** whose completeness is structurally enforced:

- **content-review** — a **dimension × chunk matrix**. `review_plan.py plan` computes the cells deterministically (structure-safe chunks, never split mid-fence/table; `max_chunks`/`max_cells` caps); the orchestrator dispatches one reviewer per cell (grammar-style / logic-consistency / fact-check), writes each result to `<dimension>__<chunk>.json`, and `review_plan.py assemble` validates every cell, dedupes findings, fills the report, and emits a diff. A `FAILED` required cell marks the report **incomplete** (no diff unless `--accept-partial`). The whole contract is testable with mocked cell fixtures — no sub-agents needed.
- **translate** — source-driven glossary (enumerate terms from the document, ground in references) + chunked translation + per-occurrence QA + a bounded (2×/chunk) re-translation loop.

Anti-skip levers (both skills): (1) narrow per-cell/per-chunk mandates; (2) prompt-forced JSON returns parsed strictly (`json.loads` + required fields + cell-identity check); (3) deterministic cell counts with mandatory before/after coverage accounting; (4) per-cell retry cap of 2 → `FAILED`; (5) runtime preflight that refuses to claim matrix guarantees when no sub-agent tool exists.

### Cross-Skill Patterns

- **Config merge**: `load_config()` reads `scripts/config.json`, auto-creates with defaults if missing, merges partial configs with `DEFAULT_CONFIG`
- **Dependency management**: `content-review` and `translate` may install
  MarkItDown on demand; `markdown-conversion` never mutates the active
  environment and only accepts dependencies that pass its behavioral capability
  checks
- **Gate-based error handling**: Each step validates output and calls `die()` on failure
- **Code block/frontmatter protection**: Used by both cleanup and translate pipelines
- **Path resolution**: `config.json` is resolved relative to the pipeline script's directory (not CWD), unless overridden by `--config`.

## Configuration

Each skill stores its own `scripts/config.json` (gitignored):
- `skills/markdown-conversion/scripts/config.json` — `pdf_ocr` defaults: `mode` (`auto`), `engine` (`rapidocr`), `language` (`ch`), `dpi` (`300.0`), `max_long_edge` (`4096`), and `min_confidence` (`0.5`); single-file bundle output defaults beside the source and batch output defaults to `<input-dir>/_converted`
- `skills/markdown-cleanup/scripts/config.json` — fixer enable/disable settings
- `skills/content-review/scripts/config.json` — `chunk_lines` (400), `max_chunks` (20), `max_cells` (60)
- `skills/translate/scripts/config.json` — `default_target_language` (zh), `chunk_lines` (300), `max_chunks` (30), `max_terms` (800), `max_terms_per_chunk_prompt` (120), `max_reference_passages_per_term` (5), `max_workspace_mb` (100)

## Permissions

`.claude/settings.json` pre-allows:
- `python -m pytest tests/markdown-conversion/test_pipeline.py *`
- `python -m pytest tests/markdown-cleanup/test_cleanup_pipeline.py *`
- `python -m pytest tests/content-review/test_review_plan.py *`
- `python *pipeline.py*`
- `python *cleanup_pipeline.py*`
- `python *translate_pipeline.py*`
- `python *review_plan.py*`
