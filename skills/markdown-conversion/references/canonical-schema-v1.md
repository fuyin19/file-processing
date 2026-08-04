# Canonical JSON v1

`<stem>.json` is the canonical, loss-aware output of `markdown-conversion` v6.2.
The machine-readable schema is `../schemas/canonical-v1.schema.json`.

## Top-level fields

```json
{
  "schema_version": "1.0",
  "source": {},
  "document": {},
  "adapter": {},
  "source_units": [],
  "content": [],
  "tables": [],
  "assets": [],
  "relationships": [],
  "quality": {},
  "outputs": {}
}
```

- `source.sha256` is the SHA-256 of local source bytes. URL compatibility mode
  records `hash_basis: adapter_text` instead.
- `document.document_id` is `sha256:<source.sha256>`; no registry is required.
- IDs are deterministic hashes of document ID, source locator, node type, and
  occurrence. They contain no timestamp or random value.
- `adapter` records only backend name/version and static limitations.
- `source_units` represent pages or logical Office documents and carry locator,
  extraction status, and unit-level warnings.

## Text and reading order

Every text-bearing node stores:

```json
{
  "raw_text": "adapter characters before structural cleanup",
  "text": "cleaned source-language text",
  "normalized_text": "selected language-normalization result"
}
```

`raw_text` means the raw characters returned by the adapter, not raw OOXML/PDF
binary syntax. Code, URLs, paths, IDs, hashes, and formulas are protected during
language normalization. For born-digital PDFs, the adapter only resolves an
unmapped character as a hyphen when its glyph box is hyphen-like; otherwise it
uses the replacement character and records a content-loss warning.

When optional PDF OCR contributes to a node, its open `source_locator` records
`extraction_method` (`ocr` or `native+ocr`), `ocr_provider`, `ocr_version`, and
the conservative minimum `ocr_confidence` represented by that node. OCR boxes
are converted from raster coordinates back to the same PDF canvas coordinate
system used by native text. These provenance fields do not change the ordered
content or text-field contract.

The page source unit's open `locator.ocr` object records the provider and
runtime versions, model profile, language, requested/effective DPI, confidence
threshold, raster dimensions, and filtering/merge counts. Volatile elapsed time
is not serialized. These audit fields are deliberately excluded from stable-id
inputs, so upgrading an OCR runtime does not churn otherwise identical page or
node identities.

`content` is the only reading order. Tables and images appear as reference nodes:

```json
{"id": "node-…", "type": "table", "table_id": "table-…", "source_locator": {}}
{"id": "node-…", "type": "image", "asset_id": "asset-…", "source_locator": {}}
```

Every reference must resolve exactly once in `tables` or `assets`.

## Tables and assets

Tables always contain `table_id`, `source_locator`, `raw_rows`, normalized
`rows`, nullable `confidence`, and `warnings`. Each cell retains raw/cleaned/
normalized text, spans, and an optional parsed value. Headers, caption, units,
currency, period, footnotes, and cross-page continuation are optional; the
pipeline never invents them.

The open table locator may include `table_detection` (`word_grid`,
`vector_grid`, or `vector_booktabs`) and `vector_rule_count`. Vector-derived
tables require high-confidence geometry; decorative frames, diagonal chart
geometry, and ambiguous aligned prose remain outside table output.

When one logical paragraph or table crosses pages, its locator may include a
`spans` array. Every nested span carries its page/source-unit provenance and is
validated against `source_units`; stable node/table IDs are generated only
after continuation stitching is complete.

Repeated running headers, footers, and page labels are represented as
`boilerplate` or `page_label` content nodes. They remain available to canonical
consumers and preserve locators, but the default Markdown renderer suppresses
them. This allows continuation stitching to pass over page chrome without
silently deleting source provenance.

Asset paths are bundle-relative and cannot contain `..`. Every asset records a
SHA-256, media type, source locator, alt, and caption. Publication fails on a
missing asset, path escape, dangling reference, or hash mismatch.

PDF and referenced DOCX/PPTX/XLSX images use this same structure. Office asset
locators include the OOXML package part; repeated uses of one embedded binary
share one asset record and appear as separate `content` image references.
Rendered Markdown uses the asset's bundle-relative `path` unchanged.

## Quality and publication

- `complete`: no warning and no known loss.
- `complete_with_warnings`: confidence/limitation warning without known omitted
  semantic content.
- `partial`: known omitted or quarantined source content, while artifacts remain
  valid and at least one usable content node exists.

All three states publish with exit code `0`. Invalid schema/serialization,
duplicate IDs, broken references, asset verification failure, no usable content,
or transactional publication failure prevents publication.

`outputs` records the rendered Markdown path/hash and all published asset
paths/hashes. JSON does not record its own hash, avoiding a circular digest.
