---
name: markdown-conversion
description: |
  Convert local PDF and AnyDoc/MarkItDown-supported documents, supported files, URLs, or directories into a canonical JSON plus Markdown bundle, or one clean Markdown file. Use for PDF Inspector-backed PDF extraction, AnyDoc-backed local non-PDF extraction, explicit MarkItDown rollback, deterministic five-field frontmatter, Chinese language normalization, batch conversion, and staged output handling.
metadata:
  version: 7.0.0
---

# Convert files to canonical JSON and Markdown

Bundle mode requires `ANTI_ENTROPY_CORE_RUNNER` to be the absolute path to
`anti-entropy-core/scripts/knowledge_unit_runner.py`. A missing or invalid
runner is a configuration error; there is no local Envelope fallback.

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
export, without running its document text/layout/table pipeline. Local formats
supported by AnyDoc use its `to_document` model by default; URLs (including PDF
URLs) and formats
outside AnyDoc continue through a reused MarkItDown adapter. Pass
`--local-document-adapter markitdown` (alias `--local-adapter`) to explicitly use MarkItDown for an eligible local
file. Legacy `.doc` is not eligible for this rollback: it is rejected before
worker or staging creation and must use AnyDoc or first be converted to `.docx`
in a trusted desktop workflow. In bundle mode, embedded AnyDoc image assets are exported from memory.
Both adapters produce the same Canonical JSON v1 asset model and shared
Markdown rendering.

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
  [--local-document-adapter anydoc|markitdown]
  [--ocr off|auto|force] [--ocr-engine rapidocr]
  [--ocr-language ch] [--ocr-dpi 300]
  [--ocr-max-long-edge 4096] [--ocr-min-confidence 0.5]
  [--enrich-images]
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
├── src/
│   └── report.pdf
└── assets/
    └── images/
```

Every local bundle contains the exact user input bytes at
`src/<original-basename>`; this also applies independently to every batch item.
The pipeline creates the owned stage and copies those bytes before adapter
execution. Canonical source hash, size, document ID, and persisted logical
locator are derived from that archived copy, which is identity- and hash-checked
again before publication. Windows extended-length operational paths are never
persisted in JSON or Markdown.
The source copy is archival output and is not listed in Canonical `assets` or
`outputs.assets`. URL bundles do not create `src/`. The assets directory is
created only when assets exist. Batch output defaults to
`<input-dir>/_converted/` and mirrors the input hierarchy. An explicit
`--output-dir` replaces that output root. Batch conversion handles each file as
its own preflight and staged publication boundary; it is not atomic across the
whole directory.

Markdown-only mode emits exactly one `.md` file: no JSON, assets, source copy,
or other sidecars.
Image caption text is retained in reading order; unlabelled images are omitted,
and no broken image links are emitted. Because no JSON artifact exists, this
mode skips JSON Schema validation while retaining applicable semantic checks.

## Workflow

1. Resolve and preflight a single target before loading an expensive adapter.
   For batch input, first reject an output root equal to or above the input
   root, then resolve, convert, and publish each collected file sequentially. A
   descendant output such as `_converted` remains allowed and is excluded from
   collection.
2. For a local bundle, create one short, exclusive `.mc-stage-<uuid>` sibling,
   reject a source equal to or beneath the bundle target, and stream-copy the
   source into `src/<original-basename>`. Derive source identity from the
   archive and give adapters its native operational path while preserving the
   original logical locator and basename in canonical output. URL input is
   unchanged.
3. Extract local PDFs with PDF Inspector. Before extraction, standard
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
   behavior. Use AnyDoc for eligible local non-PDF formats and MarkItDown for
   all URLs, including PDF URLs, and other formats. All local Office/AnyDoc
   inputs first pass the same bounded,
   read-only container/XML/image preflight. Native AnyDoc, MarkItDown, PDF
   Inspector, and OCR provider calls run in deadline-bounded workers. Only a
   provider-typed DOCX
   `max_xml_nodes` capacity error may trigger ordered structural DOCX sharding;
   other limits, parse errors, crashes, and timeouts fail without fallback.
4. Build Canonical JSON v1 with source units, one ordered content stream,
   referenced tables/assets, stable IDs, and quality warnings.
5. Recheck the archived source entry identity and SHA-256 after adapter work.
   Do not archive a temporary accepted/final-view Word snapshot; URL and
   Markdown-only outputs skip this archive.
6. Preserve adapter `raw_text` and cleaned `text`; derive `normalized_text` with
   one protected OpenCC pass. The default is `simplified`.
7. Render Markdown from canonical nodes with the exact five-field frontmatter.
8. In bundle mode, validate JSON Schema, references, asset containment, and
   hashes. In Markdown-only mode, skip JSON Schema and run the applicable
   semantic checks.
9. Publish a verified owned stage. An absent target receives a no-replace move;
   an existing target is rejected unless `--overwrite` is explicit. Regular
   files use the OS replace operation; bundle directories remove the selected
   target and then move the completed stage into place. There is no backup or
   automatic rollback. A failed publication retains the owned stage and
   reports its exact path. `--rename` keeps `_1`, `_2`, …; batch does not
   extend publication across files.

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

- Windows operations use absolute extended-length paths internally, including
  UNC paths, so local input, staging, assets, validation, and publication work
  when ordinary path APIs are limited to 260 characters. Canonical locators and
  reported final paths remain ordinary logical paths.
- Overwrite has no backup, rollback, or automatic recovery route. If
  publication fails, inspect the reported owned stage and target before
  retrying.

- PDF conversion uses a behaviorally compatible PDF Inspector for headings, paragraphs, line wrapping,
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
  compatible `rapidocr` and `onnxruntime` packages; auto mode follows
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
- Local PDF dependencies are capability-bound. `pypdf` supplies the common
  PDF layer; `auto/off` require PDF Inspector, while `force` does not.
  `pdfminer.six` loads only when a CID repair is needed, PDFium only for OCR
  rasterization or bundle image export, and RapidOCR/ONNX only when OCR runs.
  Missing route-required capabilities fail explicitly. Missing optional
  recovery or image capabilities preserve the documented warning/partial
  behavior.
- Embedded Office images remain assets by default and are not OCRed on the fast
  path. `--enrich-images` (bundle mode only) OCRs resolved image occurrences in
  the isolated worker and inserts provenance-linked paragraphs immediately after
  their image nodes. Unresolved image positions are never guessed. OCR failure
  leaves the extracted assets and document intact and reports a warning.
- AnyDoc handles `.doc`, `.docx`, `.docm`, `.ppt`, `.pps`, `.pot`, `.pptx`,
  `.pptm`, `.ppsx`, `.ppsm`, `.xls`, `.xlsx`, `.xlsm`, `.xlsb`, `.odt`, `.ods`,
  `.odp`, `.rtf`, `.epub`, and `.csv`. Its document model provides ordered
  blocks and embedded image bytes; page/slide/sheet provenance and rich styles
  are flattened with deterministic warnings because Canonical v1 has no fields
  for them. Canonical asset paths are generated and never derived from package
  paths; Markdown-only intentionally emits no binary assets or links. A local
  `.doc` must use AnyDoc; explicit MarkItDown selection fails before conversion.
- The runtime distribution is `firecrawl-anydoc`; the adapter performs a
  no-install compatibility check in the active Python interpreter. Install a
  provider explicitly with `python -m pip install firecrawl-anydoc`.
  Versions are accepted only when the public API/model capability check passes;
  rerun the full tests and benchmark after upgrades.
- The authoritative AnyDoc upstream is `firecrawl/anydoc`; `fuyin19/anydoc` is
  a mirror. Referencing GitHub does not update an installed wheel.
- v7.0.0 does not emit RAG chunks, change Canonical schema 1.0, or claim page,
  slide, sheet, rich-style, formula, or external-image fidelity beyond the
  AnyDoc model and documented warnings.
- URL input, including a PDF URL, is downloaded through a public-network-only,
  redirect-revalidated,
  DNS/IP-pinned byte/time-bounded client before local MarkItDown conversion.
  Source identity hashes response bytes, and persisted locators omit credentials,
  query strings, and fragments.

See `references/canonical-schema-v1.md` for the public JSON contract,
`references/frontmatter-template.md` for frontmatter, and
`references/troubleshooting.md` for failures.

## Exit codes

- `0` — output published, including warning or partial output.
- `1` — input, conversion, validation, staging, write, or publication failure.
- `2` — output collision without `--overwrite`/`--rename`; in batch, at least one
  collision and no true failure.
