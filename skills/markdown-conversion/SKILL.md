---
name: markdown-conversion
description: |
  Convert local PDF and Office documents, supported files, URLs, or directories into a canonical JSON plus Markdown bundle, or one clean Markdown file. Use for native born-digital PDF extraction, MarkItDown-backed Office conversion, deterministic five-field frontmatter, Chinese language normalization, batch conversion, and transactional output handling.
metadata:
  version: 6.2.0
---

# Convert files to canonical JSON and Markdown

Use `scripts/pipeline.py`. Local PDFs use the native PDFium adapter; Office and
other supported formats use a reused MarkItDown adapter. In bundle mode,
referenced images in DOCX, PPTX, and XLSX packages are exported alongside the
MarkItDown text. Both adapters produce the same Canonical JSON v1 asset model
and shared Markdown rendering.

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
  [--ocr off|auto|force] [--ocr-engine rapidocr]
  [--ocr-language ch] [--ocr-dpi 300]
  [--ocr-max-long-edge 4096] [--ocr-min-confidence 0.5]
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
2. Extract local PDFs with PDFium; use MarkItDown for Office text and export
   referenced OOXML Office images when publishing a bundle. When enabled, local
   OCR rasterizes only selected PDF pages, maps recognized lines back to PDF
   coordinates, removes native/OCR duplicates, and sends the merged fragments
   through the same table and reading-order pipeline. PDF vector rules, image
   obstacles, repeated page chrome, and typography remain geometry/provenance
   inputs rather than being flattened away before structure recovery.
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

- PDF v6.2 targets born-digital text, rotation, document-level typography,
  recursive multi-column ordering around full-width text/image obstacles,
  physical-line dewrapping, conservative cross-column and cross-page sentence
  continuation, and geometry-backed tables. Table recovery covers the existing
  character/word grid path plus high-confidence ruled and booktabs-style vector
  tables; ambiguous prose, page frames, and chart grids fall back to ordinary
  content. Source bboxes/spans and extractable images remain preserved.
  Repeated running headers, footers, and page labels stay in Canonical JSON as
  classified provenance but are suppressed from rendered Markdown. Exact
  duplicate native paint layers are collapsed without removing visible shadows
  or independently positioned text. Unmapped PDF glyphs are accepted as
  hyphens only when their geometry is hyphen-like; other unknown glyphs produce
  a content-loss warning.
- OCR is optional and local. `auto` is the default, but its engine remains lazy:
  healthy born-digital pages do not import or initialize an OCR model. Install
  the tested `rapidocr==3.9.2` and `onnxruntime>=1.20,<2`; auto mode handles
  no-text/scanned, dominant-image sparse-text, garbled, Unicode-map-damaged, or
  native-fragment-extraction-failed pages, while
  `--ocr off` preserves the native-only path and `--ocr force` attempts every
  PDF page. The default `ch` RapidOCR model recognizes Chinese and English.
  OCR polygon orientation is retained for rotated reading order. Confidence is
  statistical, and a failed or empty required OCR page remains loss-aware
  `partial` output; a force-only failure preserves healthy native output and is
  only a warning. Runtime elapsed time is intentionally not serialized so
  equivalent conversions remain deterministic.
- Office keeps MarkItDown for text. DOCX, PPTX, and XLSX bundle output exports
  referenced embedded images to `assets/images/`; Canonical JSON stores their
  relative paths, hashes, media types, locators, and ordered content references,
  and Markdown uses those same relative paths. Missing, external, or unreadable
  image targets become non-blocking quality warnings without dangling links.
- Legacy binary Office formats may still omit embedded images; Markdown-only
  intentionally publishes no image binaries or links.
- v6.2 does not emit RAG chunks, bind an ingestion library, implement a native
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
