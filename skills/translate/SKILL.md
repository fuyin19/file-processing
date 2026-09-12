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
  version: 4.0.0
---

# Translate

Translate files to a target language. **Accuracy / terminology match is the top
priority; speed and token cost are secondary.** v4 is reference-first and
manifest-bound: source occurrences, reference evidence, agent artifacts, and QA
are tied to one isolated run before a formal translation may be written.

## Commands

### `/file-processing:translate <filepath> --language <lang> [options]`

**Arguments:**
- `filepath` (required): Path to a local file to translate
- `--language, -l` (required): Target language code (e.g., `zh`, `en`, `zh-CN`, `ja`)
- `--references, -r`: One or more reference files or directories (triggers the two-phase glossary)
- `--glossary, -g`: Pre-made glossary files (JSON structured `{"terms":[...]}`, flat JSON, CSV/TSV, or MD table)
- `--glossary-output`: Explicit path for a validated structured glossary export after formal QA (default: no glossary export)
- `--output, -o`: Custom output path (default: `<stem>.<lang>.json`)
- `--output-format`: `json` (default ordered bilingual pairs) or `markdown` (explicit `.md`)
- `--no-frontmatter`: Skip YAML in explicit Markdown output; JSON never has YAML
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

## Manifest Contract (schema 3.0 retained)

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

Gate: exit `0`. Prints a compact `=== RUN MANIFEST ===` summary with counts and local chunk-plan/passage-manifest paths; read the named files, without loading all source/reference bodies into a single request. The run workspace is isolated under `.translate-runs/`; extraction and translation reuse the manifest-bound chunk boundaries.

### Step 2: Build the glossary (source-driven, two phases — only with `--references` or `--glossary`)

1. **G1 — source-term extraction:** dispatch one `source-term-extractor` per source chunk. Each exhaustively enumerates candidate terms in its chunk (proper nouns, common nouns, multi-word phrases, jargon, acronyms, recurring expressions — err toward inclusion). Merge into a candidate list with `source_chunks`.
2. **G2 — reference grounding:** for each term, Python pre-selects the top-K relevant reference passages (`select_reference_passages`); dispatch `reference-grounder` agents over batches. Each returns `{source, target, alternatives, context_note, evidence (passage id), confidence, source_chunks}`. Terms with no reference basis get `target:null, confidence:"none"` — they are NOT fabricated and NOT silently dropped.
3. **Merge + save privately:** retain the structured glossary `{"terms":[...]}` inside this run for agent use. Pre-made `--glossary` terms merge as `confidence:"high"` seeds. Do not export beside the input. Persist an explicit `--glossary-output` through prepare; the final writer exports it only after formal QA.

After validating G1/G2 payloads, publish the `reference_mining` and
`source_matching` artifacts through `publish-stage`. The latter must contain a
completed scan acknowledgement for every source chunk, deterministic relevance
batches whose union exactly equals the occurrence ledger, and an explicit reason
for every empty scan. It is not valid to claim coverage from a truncated slice.

### Step 3: Translate all scheduling groups automatically

Read `scheduling_groups(manifest)` from `translate_pipeline.py`: consecutive
chunk IDs in groups of at most configured `max_chunks` (default 30). This is a
scheduling batch size, not a whole-document cap. Iterate **all** groups without
asking whether to continue. Bound concurrent G1/G2 reference work and translator
dispatches by the same group size; retain existing reference passage/term limits.
Each translator request reads one source chunk and its complete occurrence
subset, with bounded relevance/repair requests when needed. An indivisible block
is preserved and marked `oversized`; if it exceeds the real model request budget,
stop with that explicit limitation rather than truncate it.

After reference/source stages are published, Python derives exact partial
identities with `expected_partial_tasks(manifest, 'translation')`. The orchestrator
imports this helper under the skill's scripts path and loads the manifest via
`_load_v3_manifest_or_die`. It may persist the returned descriptors privately for
dispatch, but must recompute them after upstream changes. Each descriptor provides
`path`, run/stage/task hashes, chunk/occurrence IDs and required checks. Store a
validated result at exactly that deterministic path (one JSON object per task),
copying descriptor identity and adding `schema_version:"3.0"`, `attempt`,
`status:"completed"`, `translated_markdown`, and `occurrence_ids` from the
translator self-audit. Never mark a missing/invalid result completed. Retry at
most `agent_retry_limit` times (default 2); exhaustion leaves this isolated run
incomplete and blocks publication. No per-batch manifest state is introduced.

**`confidence:none` terms must still be translated** with `human_confirm:true`;
QA must verify their handling. Internal terminology/reference use remains required.

### Step 4: Assemble + forced-application QA

After every translation group succeeds:

```bash
python scripts/translate_pipeline.py assemble --manifest "<manifest>" --stage translation
python scripts/translate_pipeline.py publish-stage --manifest "<manifest>" \
  --stage translation --artifact translation --input "<workspace>/assembled.translation.json"
python scripts/translate_pipeline.py qa --source "<filepath>" \
  --translation "<workspace>/assembled.translation.md" --language "<target_lang>" \
  --manifest "<manifest>"
```

`assemble` reads only expected local partial paths, validates exact chunk and
per-chunk occurrence coverage and recomputes the canonical ordered text/hash.
It feeds the existing full-stage publisher; neither agents nor the orchestrator
concatenate the final text manually. Exact full-stage replay is idempotent;
corrected publication invalidates downstream QA using the existing mechanism.
`qa` retains per-occurrence application, structure checks and the FIX MAP.
Errors block strict output; do not declare completion on partial work.

### Step 5: Fix loop (re-translation)

For each FIX MAP entry, re-translate that chunk with a forced prompt listing the required term(s); re-assemble; re-run `qa`. Cap **2** re-translates per chunk. Remaining issues → a human-handoff list.

### Step 6: Required semantic consistency pass + write

After deterministic `qa` passes, derive
`expected_partial_tasks(manifest, 'semantic_qa')`. Dispatch bounded QA requests
for every local chunk, every adjacent-chunk seam (including scheduling-group
boundaries), and every required shared-term occurrence comparison. The Python
plan partitions repeated terms into an exhaustive adjacent-occurrence chain;
each QA request reads at most two chunks, relevant occurrences and bounded
reference/style context. It must never receive the entire assembled translation.
Use the existing five check categories for each task's applicable subset. Copy
the exact descriptor identity, plus `schema_version:"3.0"`, `attempt`,
`status:"completed"`, `checks`, and all `issues`, to its prescribed partial path.
Continue every group automatically, with the same bounded retries.

```bash
python scripts/translate_pipeline.py assemble --manifest "<manifest>" --stage semantic_qa
python scripts/translate_pipeline.py publish-stage --manifest "<manifest>" \
  --stage semantic_qa --artifact semantic_qa --input "<workspace>/assembled.semantic_qa.json"
```

The full-stage `task_coverage` evidence is mandatory at assemble, direct publish
and strict write. Missing chunks, seams, terminology comparisons, stale hashes,
errors or incomplete checks cannot pass via aggregate flags. Then:

```bash
python scripts/translate_pipeline.py write \
  --input "<filepath>" --translation "<final>" --language "<target_lang>" \
  --manifest "<run_manifest.json>" \
  [--output-format json|markdown] [--overwrite | --rename]
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

## Glossary (private by default, optional export)

- **Structured format** `{"terms": [{source, target, alternatives, context_note, evidence, confidence, source_chunks, occurrences}, ...]}`. `confidence:"none"` entries carry `target:null` and are translated by the translator with `human_confirm`, then QA-verified (source not residual, rendered non-empty, cross-chunk consistent).
- **Private in the run by default**; a requested export is reviewable/editable and reusable via `--glossary`. Legacy flat/CSV/MD glossaries load as `confidence:"high"` seeds (`glossary_utils.py` dispatches all three shapes).
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
| `max_chunks` | `30` | Positive maximum chunks per scheduling group; continue all groups |
| `max_terms` | `800` | Hard cap on glossary terms |
| `max_terms_per_chunk_prompt` | `120` | Cap on glossary slice per translator |
| `max_reference_passages_per_term` | `5` | top-K passages per grounded term |
| `max_workspace_mb` | `100` | Workspace size guard |

## Output and compatibility

Default JSON is readable UTF-8 (indent 2, final LF), schema version 1, with
`source:{name,prepared_text_sha256}`, `target_language`, `qa_status` and ordered
`segments:{id,source_start_line,source_end_line,source,translation}`. Chunk IDs
preserve repeated source text. Source strings include their inter-chunk newline
so concatenating them reconstructs the prepared text exactly; positions refer to
prepared-text lines, not PDF pages. JSON has no YAML. Explicit Markdown retains
frontmatter. Custom extensions must agree with the output format. Report-only
JSON is named `.incomplete.json` and carries `qa_status:"INCOMPLETE"`.

Both requested destinations are preflighted for collisions and aliases. Files
publish individually and atomically, without a cross-file transaction promise.
If requested glossary export fails after translation publication, report exact
published paths and leave write incomplete. Retry can reuse this run's recorded,
unchanged published output; differing user bytes require explicit overwrite or
rename. Default glossary delivery is absent even with references.

Schema 3.0 and existing stages remain unchanged. The v4 runtime fingerprint
rejects older runs at the common load boundary before assemble, publish, QA,
write or resume mutates anything. Re-prepare old runs; do not migrate manifests.
The genuine 100 MiB workspace guard and existing QA/retry protections remain.
