---
name: translate
description: |
  Translate files to a target language with optional reference-guided terminology.
  Use this skill whenever the user wants to:
  - Translate a document from one language to another (e.g., English to Chinese or vice versa)
  - Convert a markdown file, PDF, DOCX, or other document to another language
  - Translate content using reference materials for consistent terminology
  - Generate a glossary from reference files and apply it during translation
  - Check translation quality and completeness
  Even if they don't say "translate", if they mention converting content to another language,
  making a Chinese/English version, or localizing a document, use this skill.
metadata:
  version: 2.0.0
---

# Translate

Translate files to a target language. **Accuracy / terminology match is the top
priority; speed and token cost are secondary.** In `orchestrated` mode it runs a
source-driven two-phase glossary + chunked parallel translation + forced
per-occurrence application QA with a re-translation loop. Falls back to the
original single-pass translate when the runtime has no sub-agent tool.

## Commands

### `/file-processing:translate <filepath> --language <lang> [options]`

**Arguments:**
- `filepath` (required): Path to a local file to translate
- `--language, -l` (required): Target language code (e.g., `zh`, `en`, `zh-CN`, `ja`)
- `--references, -r`: One or more reference files or directories (triggers the two-phase glossary)
- `--glossary, -g`: Pre-made glossary files (JSON structured `{"terms":[...]}`, flat JSON, CSV/TSV, or MD table)
- `--glossary-output`: Path to save the auto-generated glossary (default: alongside input, e.g. `report.glossary.zh.json`)
- `--output, -o`: Custom output path (default: auto-generated with language suffix)
- `--no-frontmatter`: Skip adding YAML frontmatter
- `--overwrite`: Overwrite existing output file
- `--rename`: Rename output if file exists (append timestamp)
- `--chunk-lines`: Override chunk size (default 300, from `scripts/config.json`)

**Supported file types:** `.md`, `.txt`, and all formats supported by markdown-conversion (`.pdf`, `.docx`, `.html`, etc.).

**Examples:**
```
/file-processing:translate ~/Documents/report.md --language zh
/file-processing:translate ~/Notes/meeting.md -l zh --references ~/Notes/meeting-zh-summary.md
/file-processing:translate ~/Documents/paper.md -l zh --references ~/Docs/reference-folder/ --glossary-output ~/Docs/paper.glossary.zh.json
/file-processing:translate ~/Documents/report.md -l en --glossary ~/Docs/terms.json --overwrite
```

## Runtime Preflight

Record `runtime_mode` before translating:

- **`orchestrated`** if the main agent has a sub-agent tool (`Agent`/legacy `Task`). Run the workflow below. This tool is core; this skill must NOT set `allowed-tools` that removes it.
- **`legacy_single_agent`** otherwise. Run the original single-pass flow (prepare → translate → qa → write) and stamp the output **"legacy-single-agent; no matrix guarantee"**.

## Workflow (orchestrated — source-driven glossary + chunked translation)

See `references/subagent-prompts.md` for the exact sub-agent prompts and JSON shapes.

### Step 1: Prepare

```bash
python scripts/translate_pipeline.py prepare \
  --input "<filepath>" \
  --language "<target_lang>" \
  [--references <ref1> [ref2 ...]] \
  [--glossary <seed1> ...] \
  [--glossary-output "<glossary_path>"] \
  [--chunk-lines <N>]
```

Gate: exit `0`. Prints the legacy `=== SOURCE TEXT ===` / `=== REFERENCES ===` / `=== INSTRUCTIONS ===` sections AND the new `=== CHUNK PLAN ===` (structure-safe chunks, written to `.translate-workspace/chunk_NNN.md`) and `=== PASSAGE MANIFEST ===` (references chunked into stable passage ids like `ref2#p12`). Source is chunked once; extraction and translation reuse the same boundaries.

### Step 2: Build the glossary (source-driven, two phases — only with `--references` or `--glossary`)

1. **G1 — source-term extraction:** dispatch one `source-term-extractor` per source chunk. Each exhaustively enumerates candidate terms in its chunk (proper nouns, common nouns, multi-word phrases, jargon, acronyms, recurring expressions — err toward inclusion). Merge into a candidate list with `source_chunks`.
2. **G2 — reference grounding:** for each term, Python pre-selects the top-K relevant reference passages (`select_reference_passages`); dispatch `reference-grounder` agents over batches. Each returns `{source, target, alternatives, context_note, evidence (passage id), confidence, source_chunks}`. Terms with no reference basis get `target:null, confidence:"none"` — they are NOT fabricated and NOT silently dropped.
3. **Merge + save:** write the structured glossary `{"terms":[...]}` with `save_glossary_structured` to `derive_glossary_path` (and `--glossary-output`). Pre-made `--glossary` terms merge in as `confidence:"high"` seeds.

### Step 3: Translate (chunked, parallel)

Per source chunk, slice the glossary with `slice_glossary_for_chunk` (occurrence terms for that chunk + global protected terms, capped by `max_terms_per_chunk_prompt`), then dispatch one `translator-chunk` with the chunk + its slice + `references/translation-guidelines.md`. Each returns `{translated_markdown, self_audit}`. The orchestrator validates the payload (`validate_translator_payload`) and writes `chunk_NNN.<lang>.md` + `self_audit_NNN.json`. Invalid/missing → re-dispatch up to **2×** → still failing marks the chunk `FAILED` (blocks `write`).

**`confidence:none` terms must still be translated** (the translator picks a rendering, marks `human_confirm:true`); QA verifies they were handled, not dropped.

### Step 4: Assemble + forced-application QA

Concatenate chunk translations in order → temp file, then:

```bash
python scripts/translate_pipeline.py qa \
  --source "<filepath>" \
  --translation "<temp_assembled>" \
  --language "<target_lang>" \
  [--glossary "<glossary_path>"] \
  [--workspace .translate-workspace]
```

`qa` auto-discovers the glossary at `derive_glossary_path` if `--glossary` is not given. It enforces **per-occurrence application**: each grounded term's target must appear in the chunk(s) where its source occurs (convergence-gated — it only requires the target where the source is actually present). It checks `confidence:none` consistency (rendered non-empty, source not residual, cross-chunk consistent). Output is tiered (`error` / `warning`) and includes a **FIX MAP** (`term → chunk`).

Gate: exit `1` means errors present. If `error`s or a required stage is `FAILED`, **`write` is blocked** unless the user accepts a partial artifact.

### Step 5: Fix loop (re-translation)

For each FIX MAP entry, re-translate that chunk with a forced prompt listing the required term(s); re-assemble; re-run `qa`. Cap **2** re-translates per chunk. Remaining issues → a human-handoff list.

### Step 6: (Optional) consistency pass + write

Optionally dispatch a `consistency-QA` agent over the assembled translation for cross-chunk drift/fluency (its failure is a warning, not a block). Then:

```bash
python scripts/translate_pipeline.py write \
  --input "<filepath>" --translation "<final>" --language "<target_lang>" \
  [--overwrite | --rename]
```

## Workflow (legacy_single_agent — fallback)

When preflight records `legacy_single_agent`: `prepare` → translate the whole document in one pass (apply `--glossary`/references as before) → `qa` → `write`. Stamp the output **"legacy-single-agent; no matrix guarantee"**.

## Translation Guidelines

### Preserve
- **Markdown structure**: Keep all headings, lists, tables, links, and formatting intact
- **Code blocks**: Never translate content inside fenced (```) or inline (`) code blocks
- **URLs and file paths**: Keep as-is
- **Variable names and technical identifiers**: Keep as-is
- **Frontmatter**: Keep the structure but do not translate field names

### Translate
- **Headings**: Translate heading text, keep the `#` markers and level
- **Table content**: Translate cell text, keep the `|` structure and alignment
- **Link text**: Translate the display text, keep the URL unchanged
- **List items**: Translate the text, keep the numbering/bullet markers

### Quality
- **Consistency**: Use the same translation for the same term throughout (the glossary + per-occurrence QA enforce this)
- **Natural language**: Produce fluent, natural-sounding output
- **Completeness**: Translate every paragraph and section — do not skip or summarize
- **Accuracy**: Preserve the original meaning

For detailed rules and edge cases, see `references/translation-guidelines.md`. For sub-agent prompts and JSON shapes, see `references/subagent-prompts.md`.

## Glossary (auto-generated, first-class artifact)

- **Structured format** `{"terms": [{source, target, alternatives, context_note, evidence, confidence, source_chunks, occurrences}, ...]}`. `confidence:"none"` entries carry `target:null` and are translated by the translator with `human_confirm`, then QA-verified (source not residual, rendered non-empty, cross-chunk consistent).
- **Saved to disk** (e.g. `report.glossary.zh.json`); **reviewable/editable**; **reusable** via `--glossary`. Legacy flat/CSV/MD glossaries load as `confidence:"high"` seeds (`glossary_utils.py` dispatches all three shapes).
- **Round-trips** through `load_glossary_structured` / `save_glossary_structured`.

## Prerequisites

- `markitdown` — format conversion for non-.md files (auto-installed if missing)
- No other external dependencies for the pipeline scripts

## Configuration

Stored in `scripts/config.json` (merged over defaults):

| Setting | Default | Description |
|---------|---------|-------------|
| `default_target_language` | `zh` | Default target language |
| `chunk_lines` | `300` | Structure-safe chunk size |
| `max_chunks` | `30` | Hard cap on chunk count |
| `max_terms` | `800` | Hard cap on glossary terms |
| `max_terms_per_chunk_prompt` | `120` | Cap on glossary slice per translator |
| `max_reference_passages_per_term` | `5` | top-K passages per grounded term |
| `max_workspace_mb` | `100` | Workspace size guard |
