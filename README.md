# file-processing

A Claude Code plugin for deterministic document conversion, content review,
Markdown cleanup, and translation.

## Installation

```bash
claude skill add /path/to/file-processing
```

## Skills

### markdown-conversion (v6.3.0)

Convert local PDFs, Office documents, supported files, URLs, or directories
through one canonical pipeline. Local PDFs use PDF Inspector as the
authoritative text and structure source. Its full-document Markdown is kept as
one global result. A page reported as garbled after standard CID `ToUnicode`
repair remains on that Inspector path when its selected-page Markdown passes a
conservative readability gate, preserving Inspector tables and headings.
Other OCR-required pages route to local OCR. Selected-page calls replace only a
unique complete selected-page signature; reordered, partial, or unprovable spans route
the whole document to ordered OCR rather than risking healthy content. Empty
selected pages are placed only when Inspector's ordered per-page Markdown fully
accounts for the global visible text. PDFium is limited to
OCR rasterization plus lightweight bundle image-object export; it does not run
the PDF text/layout/table pipeline. Office text continues through MarkItDown,
while DOCX, PPTX, and XLSX images are exported by default in bundle mode. The
default output is a movable bundle:

```text
report/
├── report.json
├── report.md
└── assets/images/     # only when images exist
```

```text
/file-processing:markdown-conversion ~/Downloads/report.pdf
/file-processing:markdown-conversion ~/Downloads/report.docx --output-mode markdown
/file-processing:markdown-conversion ~/Downloads/papers --types pdf,docx
```

Use `--output-mode markdown` or single-file `--output-path` for exactly one clean
Markdown file. Bundle batch output defaults to `<input-dir>/_converted/`; use
`--output-dir` to choose another root.

Canonical JSON v1 preserves source text and stable locators/IDs while Markdown
defaults to simplified Chinese. Change this with
`--language-normalization preserve|traditional`. Markdown keeps exactly the
`type/title/description/tags/timestamp` frontmatter fields unless
`--no-frontmatter` is supplied.

PDF and OOXML Office bundle images use the same canonical asset contract:
binary files live under `assets/images/`, Markdown references their
bundle-relative paths, and JSON records paths, hashes, media types, source
locators, and ordered image nodes. Markdown-only intentionally omits binaries
and image links.

PDF OCR is an optional local path in v6.3. Install the tested
`rapidocr==3.9.2` plus `onnxruntime>=1.20,<2`. The default `auto` mode refines
Inspector's full-result signal with its per-page layout classification, then
OCRs only pages still reported as missing, empty, scanned, or garbled;
`--ocr force` uses OCR for every PDF page, and `--ocr off` disables the OCR
model path. A proven Inspector span for an OCR-required page is removed even
when OCR is disabled, unavailable, or insufficient, so untrusted text is never
published as a fallback. OCR text and its page-level union bounding box retain
page provenance; an
unrecovered page remains `ocr_required` and the document is published as
loss-aware `partial` output when other content is usable.

The PDF path preserves Inspector's full-document headings, paragraphs, line
wrapping, tables, and reading order without PDFium reinterpretation. It adds no
custom header/footer cleanup; Inspector's own default behavior remains
authoritative. PDFium native text never replaces or restructures Inspector
content. Unrecovered OCR pages
and detected unsupported Office features produce publishable `partial` output
when other usable content exists. The converter does not emit RAG chunks or
bind an ingestion library.

See [the skill contract](skills/markdown-conversion/SKILL.md) and
[Canonical JSON v1 reference](skills/markdown-conversion/references/canonical-schema-v1.md).

### content-review (v2.0.0)

Review documents for grammar, style, logic, consistency, and reference-backed
fact checking through a deterministic dimension × chunk matrix.

```text
/file-processing:content-review ~/Documents/report.md --focus all
/file-processing:content-review ~/Documents/report.md --references ~/Documents/sources
```

### markdown-cleanup (v1.0.0)

Clean MarkItDown formatting artifacts while preserving meaningful Markdown
structure.

```text
/file-processing:markdown-cleanup ~/Documents/report.md
/file-processing:markdown-cleanup ~/Documents/notes --dry-run --diff
```

### translate (v2.0.0)

Translate with structure-safe chunking, source-driven terminology, reference
grounding, and deterministic per-occurrence QA.

```text
/file-processing:translate ~/Documents/report.md --language zh
/file-processing:translate ~/Documents/report.md --language en --references ~/Documents/reference
```
