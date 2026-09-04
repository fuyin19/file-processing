# file-processing

A Claude Code plugin with five deterministic, script-backed document
workflows and one ordinary shared-runtime carrier skill.

## Installation

```bash
claude skill add /path/to/file-processing
```

## Knowledge-unit Core

The three conversion skills bind shared runtime only from the ordinary sibling
file-processing skill under their own skills root. markdown-conversion requires
that carrier; pdf-conversion requires the carrier plus sibling
markdown-conversion; and bundle-only file-conversion requires both plus Core.
Missing, linked, reparsed, or escaping dependencies fail before config,
provider, stage, or output writes, with guidance to restore the complete
unified installation. There is no cwd, checkout, PYTHONPATH, or cross-root
fallback.

Bundle routes require the independently installed anti-entropy-core skill,
exactly Core 1.2.1 with ABI anti-entropy-core.runner/v1. By default the pipeline
selects only anti-entropy-core/scripts/knowledge_unit_runner.py under the same
skills root. ANTI_ENTROPY_CORE_RUNNER remains the explicit absolute-path
override. Direct Markdown/PDF output, help, and version do not acquire a Core
dependency.

Each conversion skill carries a local standard-library Core client. The
maintained canonical client is
skills/file-processing/scripts/anti_entropy_core_adapter.py. Run
python tools/sync_core_clients.py after editing it; run the read-only
python tools/sync_core_clients.py --check to verify all three installed copies.
Core completes only caller-owned disposable stages; conversion and publication
remain in the conversion skills.

## Shared runtime carrier

skills/file-processing is a normal discoverable skill at version 1.0.0. It
provides the conversion runtime and read-only installation diagnosis guidance,
without adding a unified conversion CLI.

## Skills

### markdown-conversion (v7.0.1)

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
the PDF text/layout/table pipeline. AnyDoc handles supported local non-PDF
formats by default through its ordered document model; URLs (including PDF
URLs) and other formats continue through MarkItDown. Use
`--local-document-adapter markitdown` for an explicit rollback. Legacy `.doc`
files must remain on AnyDoc; the MarkItDown rollback rejects them before
conversion and recommends a trusted desktop conversion to `.docx`. Embedded
AnyDoc image bytes are exported by default in bundle mode.
The
default output is a movable bundle:

```text
report/
├── AGENTS.md
├── CLAUDE.md
├── report.json
├── report.md
├── src/
│   └── report.pdf     # original local input bytes and basename
└── assets/
    ├── .keep          # only when there is no asset payload
    └── images/        # only when images exist
```

```text
/file-processing:markdown-conversion ~/Downloads/report.pdf
/file-processing:markdown-conversion ~/Downloads/report.docx --output-mode markdown
/file-processing:markdown-conversion ~/Downloads/papers --types pdf,docx
# explicit MarkItDown rollback for an AnyDoc-eligible local file
/file-processing:markdown-conversion ~/Downloads/report.docx --local-document-adapter markitdown
# optional Office embedded-image OCR (bundle mode)
/file-processing:markdown-conversion ~/Downloads/report.docx --enrich-images
```

Use `--output-mode markdown` or single-file `--output-path` for exactly one clean
Markdown file. Bundle batch output defaults to `<input-dir>/_converted/`; use
`--output-dir` to choose another root. Batch conversion preflights and publishes
each file sequentially rather than treating the whole directory as one atomic
transaction. Every local bundle, including each batch item, archives the exact
user input as `src/<original-basename>` before adapter execution. Canonical
source identity is derived from those archived bytes and rechecked before
publication. URL bundles carry `src/.keep`; Markdown-only output creates no
source copy or other sidecar because it remains the legacy one-file mode.

All local path-sensitive operations use extended-length native paths on
Windows, including UNC paths, while Canonical JSON retains only ordinary
normalized logical locators. Publication uses exclusive short `.mc-stage-<uuid>`
entries. An existing target is rejected by default; `--overwrite` makes one
replacement without a backup or automatic rollback. Regular files use the OS
replace operation; bundle directories remove the selected target and then move
the completed stage into place. A failed publication retains the owned stage
and reports its exact path for manual handling.

Canonical JSON v1 preserves source text and stable locators/IDs while Markdown
defaults to simplified Chinese. Change this with
`--language-normalization preserve|traditional`. Markdown keeps exactly the
`type/title/description/tags/timestamp` frontmatter fields unless
`--no-frontmatter` is supplied.

PDF and AnyDoc bundle images use the same canonical asset contract:
binary files live under `assets/images/`, Markdown references their
bundle-relative paths, and JSON records paths, hashes, media types, source
locators, and ordered image nodes. The `src/` source copy is archival output and
is not added to Canonical `assets` or `outputs.assets`. Markdown-only
intentionally omits binaries, image links, and JSON Schema validation; it still
runs the applicable semantic checks before publishing the single Markdown file.

PDF OCR is an optional local path. Install compatible `rapidocr` and
`onnxruntime` packages. The default `auto` mode refines
Inspector's full-result signal with its per-page layout classification, then
OCRs only pages still reported as missing, empty, scanned, or garbled;
`--ocr force` uses OCR for every PDF page, and `--ocr off` disables the OCR
model path. A proven Inspector span for an OCR-required page is removed even
when OCR is disabled, unavailable, or insufficient, so untrusted text is never
published as a fallback. OCR text and its page-level union bounding box retain
page provenance; an
unrecovered page remains `ocr_required` and the document is published as
loss-aware `partial` output when other content is usable.

Local PDF dependencies are loaded by capability: `pypdf` supplies the common
page/preflight layer; PDF Inspector is required by `auto` and `off` extraction
but not by `force`; `pdfminer.six` is loaded only for a required CID repair;
PDFium is loaded only for OCR rasterization or bundle image-object export; and
RapidOCR/ONNX is initialized only when OCR actually runs. Missing route-required
capabilities fail explicitly, while unavailable optional recovery or image
capabilities retain the documented warning/partial behavior.

The PDF path preserves Inspector's full-document headings, paragraphs, line
wrapping, tables, and reading order without PDFium reinterpretation. It adds no
custom header/footer cleanup; Inspector's own default behavior remains
authoritative. PDFium native text never replaces or restructures Inspector
content. Unrecovered OCR pages
and detected unsupported Office features produce publishable `partial` output
when other usable content exists. The converter does not emit RAG chunks or
bind an ingestion library. The runtime AnyDoc distribution is
`firecrawl-anydoc`; the adapter never installs packages automatically. Install
it with `python -m pip install firecrawl-anydoc`, then rerun the full test suite
and benchmark. Installed versions are used only after the public API
and model compatibility check passes. The authoritative upstream is
`firecrawl/anydoc`; `fuyin19/anydoc` is a mirror, and GitHub changes do not
update an already-installed wheel.

All local Office formats share a bounded read-only preflight. Word revisions
are converted from a temporary accepted/final-view snapshot; the source is not
modified. A provider-typed DOCX `max_xml_nodes` capacity error alone may use
ordered structural sharding. Native AnyDoc, MarkItDown, PDF Inspector, and OCR
provider calls run in deadline-bounded workers. Office images remain exported
assets by default;
`--enrich-images` explicitly adds provenance-linked OCR text for resolved image
occurrences without changing the default-path cost.

See [the skill contract](skills/markdown-conversion/SKILL.md) and
[Canonical JSON v1 reference](skills/markdown-conversion/references/canonical-schema-v1.md).

### pdf-conversion (v2.0.1)

Convert local PDFs and supported Word, PowerPoint, or Excel files to native,
high-fidelity multipage PDF. PDF inputs bypass LibreOffice and are copied from
the verified source snapshot. Office inputs use one private, deadline-bounded
x64 LibreOffice process per item and a separate bounded PDF validation worker.

```text
/file-processing:pdf-conversion ~/Documents/report.docx
/file-processing:pdf-conversion ~/Documents/report.xlsx --output-mode pdf
/file-processing:pdf-conversion ~/Documents/deal-room --types docx,pptx,xlsx
```

The default bundle is `<stem>/<stem>.pdf` plus `src/<original>`. Direct PDF
mode emits exactly one `.pdf`. The command is local-only and does not install,
repair, or drive Microsoft Office, WPS, cloud services, Docker, or LibreOffice
through COM/UNO reuse. Its private profile hardening reduces exposure but is
not an operating-system sandbox or no-external-access guarantee.

See [the PDF skill contract](skills/pdf-conversion/SKILL.md).

### file-conversion (v2.0.1)

Create one local bundle containing canonical Markdown/JSON, a sibling native
PDF, the exact source snapshot, and conditional image assets:

```text
report/
├── report.md
├── report.json
├── report.pdf
├── src/report.docx
└── assets/images/       # when emitted
```

```text
/file-processing:file-conversion ~/Documents/report.docx
/file-processing:file-conversion ~/Documents/deal-room --types docx,pptx,xlsx
```

The router acquires the source once and publishes only after both Markdown and
PDF stages validate. With the same source, timestamp, stem, and content flags,
its Markdown/JSON/source/assets bytes match standalone `markdown-conversion`;
it adds only the PDF. V1 accepts exactly `--formats markdown,pdf`; use the
specialized skills for singleton output.

See [the router skill contract](skills/file-conversion/SKILL.md).

### content-review (v2.0.0)

Review documents for grammar, style, logic, consistency, and reference-backed
fact checking through a deterministic dimension × chunk matrix.

```text
/file-processing:content-review ~/Documents/report.md --focus all
/file-processing:content-review ~/Documents/report.md --references ~/Documents/sources
```

### translate (v2.0.0)

Translate with structure-safe chunking, source-driven terminology, reference
grounding, and deterministic per-occurrence QA.

```text
/file-processing:translate ~/Documents/report.md --language zh
/file-processing:translate ~/Documents/report.md --language en --references ~/Documents/reference
```
