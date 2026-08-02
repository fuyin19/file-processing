---
name: markdown-conversion
description: |
  Convert local PDF and Office documents, supported files, URLs, or directories into a canonical JSON plus Markdown bundle, or one clean Markdown file. Use for native born-digital PDF extraction, MarkItDown-backed Office conversion, deterministic five-field frontmatter, Chinese language normalization, batch conversion, and transactional output handling.
metadata:
  version: 6.0.0
---

# Convert files to canonical JSON and Markdown

Use `scripts/pipeline.py`. Local PDFs use the native PDFium adapter; Office and
other supported formats use a reused MarkItDown adapter. Both adapters produce
the same Canonical JSON v1 model and shared Markdown rendering.

## Interface

```text
/file-processing:markdown-conversion <file-url-or-directory>
  [--output-mode bundle|markdown]
  [--output-dir <directory> | --output-path <markdown-file>]
  [--language-normalization simplified|preserve|traditional]
  [--timestamp <ISO-date-or-aware-datetime>]
  [--no-frontmatter]
  [--overwrite | --rename]
  [--types pdf,docx] [--no-recursive]
```

`bundle` is the default. `--output-path` is the compatibility interface for one
exact Markdown file and implies `--output-mode markdown`; batch `--output-path`
is a deprecated alias for `--output-dir`.

## Outputs

Single-file bundle output defaults beside the source:

```text
report/
├── report.json
├── report.md
└── assets/
    └── images/
```

The assets directory is created only when assets exist. Batch output defaults
to `<input-dir>/_converted/` and mirrors the input hierarchy. An explicit
`--output-dir` replaces that output root.

Markdown-only mode emits exactly one `.md` file: no JSON, assets, or sidecars.
Image caption text is retained in reading order; unlabelled images are omitted,
and no broken image links are emitted.

## Workflow

1. Resolve and preflight every target before loading an expensive adapter.
2. Extract local PDFs with PDFium; use MarkItDown for Office and other formats.
3. Build Canonical JSON v1 with source units, one ordered content stream,
   referenced tables/assets, stable IDs, and quality warnings.
4. Preserve adapter `raw_text` and cleaned `text`; derive `normalized_text` with
   one protected OpenCC pass. The default is `simplified`.
5. Render Markdown from canonical nodes with the exact five-field frontmatter.
6. Validate schema, references, asset containment, and hashes.
7. Publish through staging plus replace/rollback. `--rename` uses `_1`, `_2`, ….

`quality.status` is `complete`, `complete_with_warnings`, or `partial`; all
three publish successfully. Known loss, including an OCR-required page, is
`partial`. Structurally invalid output or a document with no usable content is a
failure and is not published. Markdown-only warnings are reported on stderr,
not inserted into the clean file.

## Frontmatter

Unless `--no-frontmatter` is supplied, Markdown begins with exactly:

```yaml
---
type: ""
title: "<first effective H1 or source stem>"
description: ""
tags: []
timestamp: "<timezone-aware conversion time>"
---
```

`--timestamp` accepts an ISO date or RFC3339 timezone-aware datetime and
preserves the supplied value. `--no-frontmatter` affects Markdown only; bundle
JSON still contains document metadata.

## Scope and limitations

- PDF v6 targets born-digital text, rotation, conservative two-column ordering,
  headings, paragraphs, lists, tables, source bboxes, and extractable images.
- OCR has an internal provider seam but v6 ships with `NullOcrProvider` only.
- Office keeps MarkItDown. Detected tracked changes, comments, or unexported
  embedded images become non-blocking quality warnings/partial output.
- v6 does not emit RAG chunks, bind an ingestion library, implement a native
  Office adapter, or claim semantic formula recognition.
- Existing URL input remains a compatibility path through MarkItDown; its source
  identity hashes extracted adapter text and is outside native PDF guarantees.

See `references/canonical-schema-v1.md` for the public JSON contract,
`references/frontmatter-template.md` for frontmatter, and
`references/troubleshooting.md` for failures.

## Exit codes

- `0` — output published, including warning or partial output.
- `1` — input, conversion, validation, staging, write, or rollback failure.
- `2` — output collision without `--overwrite`/`--rename`; in batch, at least one
  collision and no true failure.
