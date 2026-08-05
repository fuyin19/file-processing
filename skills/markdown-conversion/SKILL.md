---
name: markdown-conversion
description: |
  Convert local PDF and Office documents, supported files, URLs, or directories into a canonical JSON plus Markdown bundle, or one clean Markdown file. Use for PDF Inspector-backed PDF extraction, MarkItDown-backed Office conversion, deterministic five-field frontmatter, Chinese language normalization, batch conversion, and transactional output handling.
metadata:
  version: 6.3.0
---

# Convert files to canonical JSON and Markdown

Use `scripts/pipeline.py`. Local PDFs use PDF Inspector's full-document
Markdown as the authoritative result and normally route Inspector-reported
OCR-required pages to local OCR. A standard-CID-repaired page reported only as
garbled stays on the Inspector path when its selected-page Markdown passes a
conservative readability gate, preserving Inspector tables and headings.
Successful OCR replaces the corresponding Inspector page span only after
selected-page output proves one unique complete selected-page signature.
Inspector failure or an unprovable flagged-page span routes the whole document to
ordered OCR. PDFium native text is never a content or structure fallback;
PDFium is limited to OCR rasterization and lightweight bundle image-object
export, without running its document text/layout/table pipeline. Office and other supported formats use a reused
MarkItDown adapter. In bundle mode, referenced images in DOCX, PPTX, and XLSX
packages are exported alongside the MarkItDown text. Both adapters produce the
same Canonical JSON v1 asset model and shared Markdown rendering.

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
2. Extract local PDFs with PDF Inspector. Before extraction, standard
   Identity-H/Identity-V CJK fonts that lack `ToUnicode` receive temporary,
   self-contained maps generated from the bundled `pdfminer.six` Adobe data;
   the source PDF is never modified. Parse Inspector's full Markdown once so
   its document-wide headings, tables, wrapping, and reading order stay intact.
   Refine Inspector's full-result OCR signal with its precise per-page layout
   classification so sparse readable pages stay on the Inspector path. When a
   page remains OCR-required, use local OCR for that whole
   page, except that readable standard-CID-repaired pages remain Inspector
   content so OCR cannot flatten a valid table or degrade text. Locate every
   remaining flagged page's own retained text with fast selected-page Inspector
   calls. Accept a unique complete visible signature; affine anchors may choose
   the occurrence only when character-by-character extension proves the entire
   signature. Reject partial or reordered matches and never extrapolate across
   an unmatched prefix, suffix, or inter-run gap. Place an empty selected page
   at a zero-width boundary only when ordered per-page Inspector Markdown
   accounts for the complete global visible signature. Merge consecutive proven
   flagged-page spans and replace the entire run, never append OCR beside
   retained page text. If a flagged-page span cannot be proved, prefer ordered
   all-document OCR over a guessed replacement. If OCR fails after a span is proven, remove the unusable
   Inspector span, mark the page `ocr_required`, and never substitute PDFium
   native text. Add no custom header/footer cleanup; inherit Inspector's default
   behavior. Use MarkItDown for Office text and
   export referenced OOXML Office images when publishing a bundle.
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
title: "<local input source stem>"
description: ""
tags: []
timestamp: "<timezone-aware conversion time>"
---
```

`--timestamp` accepts an ISO date or RFC3339 timezone-aware datetime and
preserves the supplied value. `--no-frontmatter` affects Markdown only; bundle
JSON still contains document metadata. Local input titles use the original
filename without its final extension, preserve the filename's literal
characters, and do not change with output renaming. URL inputs retain the
compatibility behavior of using the first effective H1 with a URL-derived
stem/slug fallback.

## Scope and limitations

- PDF v6.3 uses PDF Inspector 0.2.6 for headings, paragraphs, line wrapping,
  reading order, and Markdown tables. Its full-document Markdown is parsed
  without PDFium-driven heading, table, list, dewrapping, duplicate-layer,
  column-order, cross-page, or chrome rewrites. Healthy content therefore keeps
  Inspector's global structure instead of re-rooting headings page by page.
  Inspector nodes use a document-range locator, while OCR nodes retain exact
  page provenance. Header/footer behavior is left to Inspector. Bundle image
  binaries are enumerated directly from PDF image objects. Adjacent raw PDF text
  is used only as a unique placement anchor, never as canonical content or
  structural evidence; ambiguous image positions are reported and not inserted
  arbitrarily.
- OCR is optional and local. `auto` is the default, but its engine remains lazy:
  healthy born-digital pages do not import or initialize an OCR model. Install
  the tested `rapidocr==3.9.2` and `onnxruntime>=1.20,<2`; auto mode follows
  Inspector's full-result `pages_needing_ocr` signal after confirming it with
  Inspector's per-page layout classification. A proven flagged-page
  Inspector span is removed even if OCR is disabled, unavailable, or empty;
  `--ocr off` therefore leaves that page unrecovered and reports required OCR,
  while `--ocr force` routes every
  page to OCR. The default `ch` model recognizes Chinese and English. OCR fallback
  currently publishes one page-level paragraph with provider-returned line
  breaks rather than inventing headings or tables. Confidence is statistical,
  and a failed or empty required
  OCR page remains loss-aware `partial` output. Runtime elapsed time is intentionally not serialized so
  equivalent conversions remain deterministic.
- Office keeps MarkItDown for text. DOCX, PPTX, and XLSX bundle output exports
  referenced embedded images to `assets/images/`; Canonical JSON stores their
  relative paths, hashes, media types, locators, and ordered content references,
  and Markdown uses those same relative paths. Missing, external, or unreadable
  image targets become non-blocking quality warnings without dangling links.
- Legacy binary Office formats may still omit embedded images; Markdown-only
  intentionally publishes no image binaries or links.
- v6.3 does not emit RAG chunks, bind an ingestion library, implement a native
  Office adapter, or claim semantic formula recognition.
- Existing URL input remains a compatibility path through MarkItDown; its source
  identity hashes extracted adapter text and is outside the local PDF Inspector
  guarantees above.

See `references/canonical-schema-v1.md` for the public JSON contract,
`references/frontmatter-template.md` for frontmatter, and
`references/troubleshooting.md` for failures.

## Exit codes

- `0` — output published, including warning or partial output.
- `1` — input, conversion, validation, staging, write, or rollback failure.
- `2` — output collision without `--overwrite`/`--rename`; in batch, at least one
  collision and no true failure.
