# file-processing

A Claude Code plugin for deterministic document conversion, content review,
Markdown cleanup, and translation.

## Installation

```bash
claude skill add /path/to/file-processing
```

## Skills

### markdown-conversion (v6.2.0)

Convert local PDFs, Office documents, supported files, URLs, or directories
through one canonical pipeline. Local PDFs use native PDFium extraction;
Office text continues through MarkItDown, while DOCX, PPTX, and XLSX images are
exported by default in bundle mode. The default output is a movable bundle:

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

PDF OCR is an optional local path in v6.2. Install the tested
`rapidocr==3.9.2` plus `onnxruntime>=1.20,<2`. The default `auto` mode OCRs only
scanned, sparse-image, garbled, Unicode-map-damaged, or native-extraction-failed
pages; `--ocr force` attempts every PDF page, and `--ocr off` preserves the
zero-overhead native-only path. OCR text and rotated polygons are mapped back to
PDF coordinates, merged with native text, and routed through the same layout
pipeline. A force-only OCR failure never discards healthy native text.

The PDF path also removes exact duplicate paint layers, classifies repeated
headers/footers without deleting their canonical provenance, follows recursive
multi-column reading order around full-width text and image obstacles, joins
conservative cross-column/cross-page sentence continuations, recognizes
high-confidence ruled and booktabs-style vector tables, and uses typography
plus sequence evidence for headings and ordered lists. Unrecovered OCR pages
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
