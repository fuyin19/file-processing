# markdown-conversion troubleshooting

## Output already exists

Exit code `2` means the target bundle directory or Markdown file already exists.
Use `--overwrite` for transactional replacement or `--rename` for `_1`, `_2`, ….

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

## Retained overwrite backup

Overwrite uses two atomic no-replace moves and is not crash-atomic. The old
entry is first moved to an exact sibling `.mc-backup-<uuid>/original`; only then
is the complete stage moved to the target. If safe restoration or cleanup
cannot be proved, the error or warning prints that exact recovery directory and
leaves it in place. Inspect that one path manually. There is intentionally no
prefix sweep, broad recovery/delete command, journal, lock, or cleanup service.
Symlinks and junctions inside an old target are removed only as leaf entries
during confirmed post-commit cleanup; their external targets are not followed.

## Partial output

`partial` is publishable and returns exit code `0`. Inspect
`report.json -> quality.warnings` for exact page/unit codes. In Markdown-only
mode the same warnings are printed to stderr because no sidecar is allowed.

Common codes include `ocr_applied`, `ocr_required`, `ocr_unavailable`,
`ocr_failed`, `ocr_incomplete_result`,
`pdf_inspector_cid_tounicode_repaired`,
`pdf_inspector_cid_page_retained`,
`pdf_inspector_document_ocr_fallback`,
`pdf_inspector_alignment_ocr_fallback`, `pdf_inspector_cid_repair_failed`,
`pdf_image_position_ambiguous`, `pdf_image_extraction_failed`,
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

## PDF OCR modes

- `--ocr off` never imports or initializes OCR. A page that Inspector marks as
  OCR-required has its proven Inspector span removed, remains unrecovered, and
  is published `partial` when the rest of the document is usable.
- `--ocr auto` is the default. It refines Inspector's full-result
  `pages_needing_ocr` list with the precise per-page layout classification so
  short readable pages remain Inspector content, then OCRs pages still marked
  scanned, empty, or garbled. If
  Inspector fails or a flagged page's retained text span cannot be proved, the
  documented safety fallback OCRs the whole document in page order.
- `--ocr force` routes every PDF page to OCR. It is an explicit replacement
  mode, not a merge with PDFium native text, and does not require PDF Inspector.

The defaults are configurable under `pdf_ocr` in `scripts/config.json`:
`mode`, `engine`, `language`, `dpi`, `max_long_edge`, and `min_confidence`.
CLI values override config values. OCR page rasters are in-memory only and are
not published as bundle assets or Markdown sidecars. OCR output keeps the
provider-returned line order in one page-level paragraph and records a union
bounding box; individual polygons are not published. Volatile elapsed timing is excluded
from Canonical JSON so repeated equivalent conversions remain deterministic.

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
`pdf_inspector_alignment_ocr_fallback` means selected-page output could not
prove every flagged page's retained text span. The adapter therefore discarded
the Inspector body and used ordered OCR for the whole document instead of
guessing. Only a unique complete selected-page signature is accepted. Affine anchors may
select an occurrence only when strict character-by-character extension proves
both page edges; partial, reordered, or gapped matches are rejected. An empty
selected page receives a zero-width OCR insertion point only when the ordered
per-page Markdown accounts for every visible character in the global result;
otherwise it takes the same whole-document safety fallback.

## PDF structure behavior

The v6.5.1 PDF path parses Inspector's full-document headings, paragraphs, lists,
tables, line wrapping, and reading order once, avoiding page-local heading
re-rooting. It does
not apply PDFium native-text corrections for columns, cross-page joins, table
partitions, duplicate paint layers, or heading levels. No additional
header/footer pass is applied; Inspector's default behavior is authoritative.
OCR replacement provides one conservative page-level paragraph with
provider-returned line breaks and does not invent headings or tables. Bundle images
are exported through a lightweight PDFium image-object pass; the full PDFium
text/layout/table parser is not run. Nearby raw text is used only to prove a
unique insertion point. An ambiguous image is reported but not inserted at an
arbitrary position, and optional image-export failure does not block otherwise
usable Inspector/OCR document content.

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
