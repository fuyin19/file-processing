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
  version: 3.0.0
---

# Translate

Translate files to a target language. **Accuracy / terminology match is the top
priority; speed and token cost are secondary.** v3 is reference-first and
manifest-bound: source occurrences, reference evidence, agent artifacts, and QA
are tied to one isolated run before a formal translation may be written.

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
- `--quality-mode`: `strict` (default) or `report-only`
- `--runtime-mode`: set by the orchestrator after runtime preflight

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

- **`orchestrated`** if the main agent has a sub-agent tool (`Agent`/legacy `Task`). Run the v3 workflow below. This tool is core; this skill must NOT set `allowed-tools` that removes it.
- **`report-only`** otherwise. Do not use `legacy_single_agent` to produce a formal output. Create only an `INCOMPLETE` diagnostic run with `--quality-mode report-only --runtime-mode unavailable`.

## V3 Manifest Contract

`prepare` is the only command that creates a v3 run. It creates an isolated
`.translate-runs/<run-id>/` workspace and prints `=== RUN MANIFEST ===`; retain
that path for every later step.

```bash
python scripts/translate_pipeline.py prepare --input "<file>" --language <lang> \
  --runtime-mode orchestrated --quality-mode strict
```

Agents must not edit a manifest directly. After the orchestrator validates each
agent JSON result, it publishes it atomically through the Python boundary:

```bash
python scripts/translate_pipeline.py publish-stage --manifest "<manifest>" \
  --stage <reference_mining|source_matching|translation|semantic_qa> \
  --artifact <name> --input "<validated-agent-json>"
```

Published JSON requires `schema_version:"3.0"`, the manifest `run_id`, an
attempt number within the configured retry budget, and the Python-computed
`stage_input_hash` for that exact stage (including upstream artifact hashes).
The publisher validates stage-specific schemas, complete passage/occurrence
coverage, and semantic-QA errors before atomically recording both raw-response
and parsed-artifact hashes. V3 `qa` and `write` must receive
`--manifest`; never scan a previous `.translate-workspace` or auto-discover
cross-run artifacts.

```bash
python scripts/translate_pipeline.py qa --source "<file>" --translation "<assembled>" \
  --language <lang> --manifest "<manifest>"
python scripts/translate_pipeline.py write --input "<file>" --translation "<assembled>" \
  --language <lang> --manifest "<manifest>"
python scripts/translate_pipeline.py resume --manifest "<manifest>"
```

Exit codes: `0` strict formal output written; `1` permanent input/schema/QA/gate
failure; `2` recoverable run awaiting orchestration; `3` report-only
`INCOMPLETE` artifact written; `4` strict mode without orchestrated runtime.

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
  [--chunk-lines <N>] \
  --runtime-mode orchestrated --quality-mode strict
```

Gate: exit `0`. Prints the legacy `=== SOURCE TEXT ===` / `=== REFERENCES ===` / `=== INSTRUCTIONS ===` sections plus `=== CHUNK PLAN ===`, `=== PASSAGE MANIFEST ===`, and `=== RUN MANIFEST ===`. The run workspace is isolated under `.translate-runs/`; extraction and translation reuse the manifest-bound chunk boundaries.

### Step 2: Build the glossary (source-driven, two phases — only with `--references` or `--glossary`)

1. **G1 — source-term extraction:** dispatch one `source-term-extractor` per source chunk. Each exhaustively enumerates candidate terms in its chunk (proper nouns, common nouns, multi-word phrases, jargon, acronyms, recurring expressions — err toward inclusion). Merge into a candidate list with `source_chunks`.
2. **G2 — reference grounding:** for each term, Python pre-selects the top-K relevant reference passages (`select_reference_passages`); dispatch `reference-grounder` agents over batches. Each returns `{source, target, alternatives, context_note, evidence (passage id), confidence, source_chunks}`. Terms with no reference basis get `target:null, confidence:"none"` — they are NOT fabricated and NOT silently dropped.
3. **Merge + save:** write the structured glossary `{"terms":[...]}` with `save_glossary_structured` to `derive_glossary_path` (and `--glossary-output`). Pre-made `--glossary` terms merge in as `confidence:"high"` seeds.

After validating G1/G2 payloads, publish the `reference_mining` and
`source_matching` artifacts through `publish-stage`. The latter must contain a
completed scan acknowledgement for every source chunk, deterministic relevance
batches whose union exactly equals the occurrence ledger, and an explicit reason
for every empty scan. It is not valid to claim coverage from a truncated slice.

### Step 3: Translate (chunked, parallel)

Per source chunk, build the complete relevance ledger, then use deterministic batches rather than silently truncating a glossary slice. Dispatch one initial `translator-chunk` plus bounded occurrence-specific repair batches with `references/translation-guidelines.md`. Each returns `{translated_markdown, self_audit}` with occurrence IDs. The orchestrator validates and publishes each artifact through `publish-stage`; invalid/missing payloads re-dispatch up to **2×**, then mark the stage `FAILED` (blocks strict `write`).

**`confidence:none` terms must still be translated** (the translator picks a rendering, marks `human_confirm:true`); QA verifies they were handled, not dropped.

### Step 4: Assemble + forced-application QA

Concatenate chunk translations in order → temp file, then:

```bash
python scripts/translate_pipeline.py qa \
  --source "<filepath>" \
  --translation "<temp_assembled>" \
  --language "<target_lang>" \
  --manifest "<run_manifest.json>"
```

`qa` reads only the manifest-bound ledger, translation, and published artifacts. It enforces **per-occurrence application**: every eligible occurrence has one terminal disposition and each grounded target is checked in its mapped chunk. Output is tiered (`error` / `warning`) and includes a **FIX MAP** (`term → chunk`).

Gate: exit `1` means errors present. If `error`s or a required stage is `FAILED`, **`write` is blocked** unless the user accepts a partial artifact.

### Step 5: Fix loop (re-translation)

For each FIX MAP entry, re-translate that chunk with a forced prompt listing the required term(s); re-assemble; re-run `qa`. Cap **2** re-translates per chunk. Remaining issues → a human-handoff list.

### Step 6: Required semantic consistency pass + write

After deterministic `qa` passes, dispatch a `consistency-QA` agent over the
assembled translation for cross-chunk drift/fluency. Publish its validated result
as the `semantic_qa` stage, bound to the assembled translation hash and complete
occurrence-ID set; an error blocks strict write. Then:

```bash
python scripts/translate_pipeline.py write \
  --input "<filepath>" --translation "<final>" --language "<target_lang>" \
  --manifest "<run_manifest.json>" \
  [--overwrite | --rename]
```

## Workflow (report-only — no agent runtime)

When preflight cannot establish `orchestrated`, run only `prepare --quality-mode
report-only --runtime-mode unavailable`, record the missing capability, and do
not write a formal translation. A later report-only write is explicitly stamped
`INCOMPLETE` and exits `3`.

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
