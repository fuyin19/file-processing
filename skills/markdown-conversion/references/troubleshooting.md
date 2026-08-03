# markdown-conversion troubleshooting

## Output already exists

Exit code `2` means the target bundle directory or Markdown file already exists.
Use `--overwrite` for transactional replacement or `--rename` for `_1`, `_2`, ….

## Partial output

`partial` is publishable and returns exit code `0`. Inspect
`report.json -> quality.warnings` for exact page/unit codes. In Markdown-only
mode the same warnings are printed to stderr because no sidecar is allowed.

Common codes include `ocr_required`, `office_image_target_missing`,
`office_external_image_not_exported`, `office_image_media_type_unsupported`,
`office_image_position_inferred`,
`office_tracked_changes_not_preserved`, and `table_structure_uncertain`.

## No usable content

An all-blank or unsupported source with no accepted text, table, or published
asset fails and leaves no target. v6 has an OCR provider seam but does not ship
an OCR engine.

## Bundle validation failure

Publication is blocked if Canonical JSON fails its schema, table/asset references
are dangling, an asset escapes the bundle, or an asset SHA-256 differs. Existing
targets are restored when replacement fails.

## URL input

URLs remain a MarkItDown compatibility path. Use an explicit output target for
predictable automation. Remote download security/caching and native remote-PDF
guarantees are outside v6.
