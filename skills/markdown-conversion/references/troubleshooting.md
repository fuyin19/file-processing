# markdown-conversion troubleshooting

## Output already exists

Exit code `2` means the target bundle directory or Markdown file already exists.
Use `--overwrite` for transactional replacement or `--rename` for `_1`, `_2`, ….

## Partial output

`partial` is publishable and returns exit code `0`. Inspect
`report.json -> quality.warnings` for exact page/unit codes. In Markdown-only
mode the same warnings are printed to stderr because no sidecar is allowed.

Common codes include `ocr_applied`, `ocr_required`, `ocr_unavailable`,
`ocr_failed`, `ocr_empty_result`, `ocr_incomplete_result`,
`ocr_detections_filtered`, `native_duplicate_paint_layer`,
`running_chrome_classified`,
`office_image_target_missing`,
`office_external_image_not_exported`, `office_image_media_type_unsupported`,
`office_image_position_inferred`,
`office_tracked_changes_not_preserved`, and `table_structure_uncertain`.

## No usable content

An all-blank or unsupported source with no accepted text, table, or published
asset fails and leaves no target. PDF OCR is optional: install the tested pair
with `python -m pip install "rapidocr==3.9.2" "onnxruntime>=1.20,<2"`, then run with `--ocr auto` or
`--ocr force`. A missing backend is reported as `ocr_unavailable`; the pipeline
does not fabricate text or silently fall back to a cloud service.

## PDF OCR modes

- `--ocr off` never imports or initializes OCR and preserves the native-only
  behavior.
- `--ocr auto` is the default and OCRs only pages classified as
  no-text/scanned, garbled, Unicode-map-damaged, native-fragment-extraction
  failed, or sparse text over a dominant page image. A small logo does not
  trigger OCR.
- `--ocr force` attempts every PDF page. Usable native text still wins over an
  overlapping OCR estimate, while non-overlapping OCR text is retained. If the
  force-only attempt fails or returns nothing on an otherwise healthy page, the
  native result remains publishable and is not mislabeled as content loss.

The defaults are configurable under `pdf_ocr` in `scripts/config.json`:
`mode`, `engine`, `language`, `dpi`, `max_long_edge`, and `min_confidence`.
CLI values override config values. OCR page rasters are in-memory only and are
not published as bundle assets or Markdown sidecars. OCR polygon orientation is
preserved for rotated reading order, and volatile elapsed timing is excluded
from Canonical JSON so repeated equivalent conversions remain deterministic.

## PDF structure recovery

The v6.2 PDF path uses recursive column cuts and treats full-width text and
images as ordering obstacles. It can conservatively join a sentence from one
column tail to the next column head and across page chrome. Exact duplicate
native paint layers are collapsed; visible shadows and separately positioned
copies remain. Repeated headers, footers, and page labels are retained as
classified Canonical JSON nodes but omitted from default Markdown.

High-confidence vector grids and booktabs-style horizontal rules can form
tables. The table locator reports `table_detection` and `vector_rule_count`.
Page frames, diagonal chart geometry, and aligned sentence-like prose are
deliberately rejected as tables. Typography and contiguous marker sequences
support heading/list recovery; caption, table-of-contents, and isolated
initial-like markers stay ordinary paragraphs.

## Bundle validation failure

Publication is blocked if Canonical JSON fails its schema, table/asset references
are dangling, an asset escapes the bundle, or an asset SHA-256 differs. Existing
targets are restored when replacement fails.

## URL input

URLs remain a MarkItDown compatibility path. Use an explicit output target for
predictable automation. Remote download security/caching and native remote-PDF
guarantees are outside v6.
