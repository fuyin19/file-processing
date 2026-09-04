# markdown-conversion troubleshooting

## Core runner configuration or version mismatch

The conversion runtime is installed as the ordinary sibling
file-processing skill under the same skills root. The pipeline accepts only
that sibling's ordinary, non-link runtime files and never falls back to the
current directory, a source checkout, PYTHONPATH, or another skills root.
Restore the complete unified installation when this preflight reports a
missing, linked, reparsed, or escaping dependency.

Bundle routes additionally require the independently installed
anti-entropy-core skill, exactly Core 1.2.1 with ABI
anti-entropy-core.runner/v1. By default the pipeline selects only
anti-entropy-core/scripts/knowledge_unit_runner.py under the same skills root.
ANTI_ENTROPY_CORE_RUNNER is an optional absolute-path override for a different
installation root; an explicit empty, invalid, nonordinary, ABI-mismatched or
version-mismatched value fails without fallback. Bundle Core preflight happens
before explicit config loading, providers or stages; conversion never creates a
config file, and one invocation keeps the same runner.
Each conversion skill carries a local standard-library Core client. Core
completes only caller-owned disposable stages; conversion and publication
remain in the conversion skills.

Diagnostics identify the selected path, known actual values and required ABI/
version. Install Core 1.2.1 beside the consumer skills, or correct the explicit
override; update the matching consumer release when upgrading Core. There is
no search of other skills roots, PATH lookup, download or automatic update.

## Configuration errors

Omitted `--config` always uses fresh in-memory defaults and never consults a
`scripts/config.json` sentinel. For explicit configuration, pass an existing
ordinary regular non-link/reparse file containing strict UTF-8 JSON with an
object root. Relative paths resolve from the current working directory;
absolute paths and paths inside or outside the skill directory are accepted. A
stable path outside the installed skill is recommended across upgrades.
Malformed JSON, invalid encoding, a missing/directory/link path, or a non-object
known block is an error with no fallback or config/output write. Partial known
blocks still merge over defaults, unknown keys remain available, and CLI values
win. `--version --config <path>` applies this same contract.

## Output already exists

Exit code `2` means the target bundle directory or Markdown file already exists.
Use `--overwrite` for one final replacement attempt or `--rename` for `_1`, `_2`, ….

Exact collision checks happen before adapters or staging. Publication also uses
an atomic no-replace rename, so a target created after preflight is preserved
and reported as a collision. Similarly named files have no special meaning and
do not suppress conversion.

## Long Windows paths

Local input, batch traversal, staging, archived source bytes, assets,
validation, and publication use extended-length Windows paths internally,
including UNC forms. The canonical source locator and displayed final output
remain normalized ordinary absolute paths; a `\\?\` prefix must never appear in
persisted output. This does not require changing the machine-wide
`LongPathsEnabled` policy.

## Overwrite publication failure

Overwrite uses the operating-system replace operation for regular files. For
bundle directories it removes the existing target and then performs a
no-replace rename of the completed stage. There is no backup or automatic
rollback. If publication fails, the error prints the exact owned stage path;
inspect the stage and target before retrying. There is intentionally no prefix
sweep, broad recovery/delete command, journal, lock, or cleanup service.

## Partial output

`partial` is publishable and returns exit code `0`. Inspect
`report.json -> quality.warnings` for exact page/unit codes. In Markdown-only
mode the same warnings are printed to stderr because no sidecar is allowed.

Common codes include `ocr_applied`, `ocr_required`, `ocr_unavailable`,
`ocr_failed`, `ocr_incomplete_result`,
`pdf_inspector_cid_tounicode_repaired`,
`pdf_inspector_cid_page_retained`,
`pdf_inspector_document_ocr_fallback`,
`pdf_inspector_alignment_ocr_fallback`, `pdf_inspector_alignment_unresolved`,
`pdf_inspector_cid_repair_failed`,
`pdf_image_position_ambiguous`, `pdf_image_extraction_failed`,
`pdf_images_page_supplement`, `pdf_images_unfinished`,
`pdf_image_enhancement_incomplete`, `pdf_images_unavailable`,
`pdf_inline_image_unrecovered`,
`office_image_target_missing`,
`office_external_image_not_exported`, `office_image_media_type_unsupported`,
`office_image_position_unresolved`, `office_revisions_flattened_to_accepted_view`,
`office_image_ocr_unavailable`, and `adapter_fallback_used`.

## No usable content

An all-blank or unsupported source with no accepted text, table, or published
asset fails and leaves no target. PDF OCR is optional: install compatible
`rapidocr` and `onnxruntime` packages, then run with `--ocr auto` or
`--ocr force`. A missing backend is reported as `ocr_unavailable`; the pipeline
does not fabricate text or silently fall back to a cloud service.
For PDFs, the Inspector/OCR body must be usable before optional figure recovery
starts. A page image appendix cannot rescue an empty or unreadable body.

## PDF OCR modes

- `--ocr off` never imports or initializes OCR. A page that Inspector marks as
  OCR-required has its proven Inspector span removed, remains unrecovered, and
  is published `partial` when the rest of the document is usable. For a
  `text_based`/`mixed` PDF with usable Inspector text or tables, unaligned content
  is retained. If local removals would exhaust that body while a page remains
  unaligned, the original body is retained instead.
- `--ocr auto` is the default. It refines Inspector's full-result
  `pages_needing_ocr` list with the precise per-page layout classification so
  short readable pages remain Inspector content, then OCRs pages still marked
  scanned, empty, or garbled. In the text-retention path, unaligned pages do not
  enlarge that OCR set: successful OCR with unresolved placement is appended
  after the body, labelled with its PDF page and possible duplication. Inspector
  failure and alignment failure outside that path retain whole-document OCR.
- `--ocr force` routes every PDF page to OCR. It is an explicit replacement
  mode, not a merge with PDFium native text, and does not require PDF Inspector.

The defaults are configurable under `pdf_ocr` in an explicit `--config` file:
`mode`, `engine`, `language`, `dpi`, `max_long_edge`, and `min_confidence`.
CLI values override config values. OCR page rasters are in-memory only and are
not published as bundle assets or Markdown sidecars. OCR output keeps the
provider-returned line order in one page-level paragraph and records a union
bounding box; individual polygons are not published. Volatile elapsed timing is excluded
from Canonical JSON so repeated equivalent conversions remain deterministic.

## PDF images and figure placement

Local PDF bundles use `--pdf-images auto` by default. The pass checks every page
for graphic candidates, including vector-only figures, and renders each enhanced
page at most once. It retains the normal page appearance, including image
fragments, overlaid text, and vector layers. It inserts a crop only when the
complete figure boundary and its position beside Inspector body content can be
proved; paragraphs and tables are never split or rewritten to fit an image.

`pdf_images_page_supplement` means an image is in the `Supplementary PDF figures`
appendix with its physical page number and a possible-duplication notice. This
preserves uncertain or disconnected figures without claiming a precise body
position. It is separate from `Supplementary OCR (unplaced)` and does not run
OCR. Cross-page figures remain separate page previews. A name or other inline
image glyph is retained on its page with `pdf_inline_image_unrecovered`; the
pipeline does not reconstruct the missing character.

Use `--pdf-images objects` for the previous image-object export and unique
neighbor-anchor placement behavior, now with cached matching. Use
`--pdf-images off` to disable PDF image enhancement. Markdown-only output skips
image enhancement entirely. These choices do not change `--ocr off|auto|force`
or Office `--enrich-images`.

The body and image workers have independent 1000-second defaults. Set
`--pdf-image-timeout 30` to give image enhancement a 30-second hard budget, or
configure `pdf_images: {"mode": "auto", "timeout_seconds": 30}`. CLI values take
precedence, and timeout values must be positive and finite. Image timeout,
resource limits, or unavailable optional image capabilities retain an already
usable body and report the unfinished page range. They cannot trigger extra OCR
or clear the body. Completion and publication still run their normal validation
after selecting the body or enhanced result, so an image budget is not a total
conversion-time limit. Report image steps and full conversion time separately.

## PDF Inspector encoding and fallback

The PDF path accepts a behaviorally compatible PDF Inspector rather than a
specific version. Some CJK prospectuses use Type0
Identity fonts without embedded `ToUnicode` streams. PDF Inspector's binary
standard-CMap fallback does not decode those fonts reliably, which can otherwise
produce empty or garbled Markdown. This is not a missing system font. The
pipeline detects standard Adobe-CNS1, Adobe-GB1, Adobe-Japan1, and Adobe-Korea1
fonts and writes orientation-correct horizontal or vertical `ToUnicode` maps
from pinned `pdfminer.six` data into a temporary
copy. No environment variable, external CMap directory, or source-file mutation
is required. If Inspector still reports such a repaired page only as suspected
garbled text, a conservative selected-page readability gate may retain its
Inspector Markdown instead of flattening a valid table through OCR; this emits
`pdf_inspector_cid_page_retained`.

Inspector's full-document Markdown is never replaced by PDFium native text.
`pdf_inspector_document_ocr_fallback` means Inspector itself failed and every
page was routed to OCR. Per-page success is reported as `ocr_applied`; missing,
failed, empty, or insufficient OCR is reported by the corresponding `ocr_*`
warning and produces loss-aware `partial` output. The adapter does not revive a
flagged page from PDFium or from Inspector's separate per-page formatter.
`pdf_inspector_alignment_unresolved` means a `text_based`/`mixed` PDF had usable
Inspector text or tables but a flagged page could not be placed safely. The
body is retained where placement is unproved; safely proved spans are processed
individually without consuming healthy text between them. Unplaced OCR appears
in a `Supplementary OCR (unplaced)` section with physical page labels. The result
remains `partial` even when OCR succeeds because order or duplication is unresolved.
`pdf_inspector_alignment_ocr_fallback` is the conservative whole-document fallback
for other classifications or no usable Inspector body. Only a unique complete
selected-page signature is accepted. Affine anchors may
select an occurrence only when strict character-by-character extension proves
both page edges; partial, reordered, or gapped matches are rejected. An empty
selected page receives a zero-width OCR insertion point only when the ordered
per-page Markdown accounts for every visible character in the global result;
otherwise it remains unaligned under the classification-specific policy above.

The text-retention regression covers a long application proof where Inspector
reported `text_based` and produced English Markdown, but an unaligned flagged
page formerly erased the body with `--ocr off`. A shorter text-based document
serves as the healthy control. This differs from a Chinese CID prospectus whose
Inspector body is actually unusable; that still needs OCR or a trusted Word
conversion. Selectable Acrobat/pypdf text alone does not establish usable
Canonical content.

## PDF structure behavior

The v7.0.2 PDF path parses Inspector's full-document headings, paragraphs, lists,
tables, line wrapping, and reading order, avoiding page-local heading
re-rooting. It does
not apply PDFium native-text corrections for columns, cross-page joins, table
partitions, duplicate paint layers, or heading levels. No additional
header/footer pass is applied; Inspector's default behavior is authoritative.
OCR replacement provides one conservative page-level paragraph with
provider-returned line breaks and does not invent headings or tables. Figure
enhancement may use Inspector text positions to associate a crop with an existing
paragraph or table. PDFium supplies graphics and page appearance, never canonical
body text or a replacement document text/layout/table parser. The `objects` mode
uses neighboring raw text only to prove a unique insertion point. Uncertain
positions follow the selected image mode's warning behavior; optional image
failure does not block otherwise usable Inspector/OCR content.

## Bundle validation failure

Publication is blocked if Canonical JSON fails its schema, table/asset references
are dangling, an asset escapes the bundle, or an asset SHA-256 differs. Existing
targets are restored when replacement fails.

## URL input

URLs, including PDF URLs, are fetched through a public-network-only,
DNS/IP-pinned downloader with
redirect revalidation, byte and time limits. Credentials in URLs are rejected;
persisted locators omit query strings and fragments. The downloaded response is
then converted locally through MarkItDown and its bytes define source identity.

## Legacy `.doc` with MarkItDown

A local `.doc` explicitly routed to MarkItDown fails before any provider worker,
staging directory, or output is created. Use the default AnyDoc route, or first
convert the source to `.docx` in a trusted desktop workflow.

## Office images and long Word documents

Bundle mode exports relationship-referenced Office image assets. It never treats
orphan package media as content and never appends unresolved images to the end of
Markdown. Use `--enrich-images` to OCR resolved image occurrences; the default
path performs no Office image OCR. A typed AnyDoc `max_xml_nodes` error on DOCX
uses bounded structural shards and one Canonical merge. Other limits and worker
timeouts do not fallback and leave no published target.
