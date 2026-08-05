# Canonical JSON v1

`<stem>.json` is the canonical, loss-aware output of `markdown-conversion` v6.3.
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
- `adapter` records only backend name/version and static limitations. Local PDF
  output names `pdf-inspector` as the backend. The full Inspector document
  retains Inspector provenance; OCR nodes carry exact page provenance whether
  triggered per page, by `force`, or by a document-level safety fallback.
- `source_units` represent logical documents and/or pages and carry locator,
  extraction status, and unit-level warnings. PDF output includes one
  document-range unit for Inspector content plus page units for exact OCR and
  image provenance.

## Text and reading order

Every text-bearing node stores:

```json
{
  "raw_text": "adapter characters before structural cleanup",
  "text": "cleaned source-language text",
  "normalized_text": "selected language-normalization result"
}
```

`raw_text` means the raw characters returned by the selected adapter, not
raw OOXML/PDF binary syntax. Code, URLs, paths, IDs, hashes, and formulas are
protected during language normalization. Full-document PDF Inspector nodes
record `extraction_method: pdf-inspector` and `page_range`; OCR replacement nodes record
`extraction_method: ocr` plus their provider, version, confidence, and page
geometry. PDFium native text is not emitted as a fallback. Standard Adobe CJK Identity fonts missing
`ToUnicode` are repaired only in a temporary PDF copy before Inspector runs.

When optional PDF OCR contributes to a node, its open `source_locator` records
`extraction_method: ocr`, `ocr_provider`, `ocr_version`, and
the conservative minimum `ocr_confidence` represented by that node. OCR boxes
are converted from raster coordinates back to the same PDF canvas coordinate
system used by PDF page locators. These provenance fields do not change the ordered
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

The open table locator may include adapter-specific detection metadata. The
main PDF path publishes Inspector's Markdown tables without PDFium
repartitioning, header inference, continuation stitching, or semantic
reclassification.

For adapters that emit one stitched logical paragraph or table across pages,
its locator may include a `spans` array. Every nested span carries its
page/source-unit provenance and is validated against `source_units`; stable
node/table IDs are generated only after any adapter-provided stitching is
complete. The v6.3 Inspector path does not perform this stitching and instead
uses document-range provenance for Inspector nodes.

The main PDF path adds no custom running-header, footer, or page-label rewrite;
PDF Inspector's full-document defaults are authoritative. The `boilerplate`
and `page_label` node types remain valid for other adapters, but the main PDF
path does not infer them from PDFium native text.

Asset paths are bundle-relative and cannot contain `..`. Every asset records a
SHA-256, media type, source locator, alt, and caption. Publication fails on a
missing asset, path escape, dangling reference, or hash mismatch.

PDF and referenced DOCX/PPTX/XLSX images use this same structure. Office asset
locators include the OOXML package part; repeated uses of one embedded binary
share one asset record and appear as separate `content` image references.
Rendered Markdown uses the asset's bundle-relative `path` unchanged.
PDF bundle images are exported by a lightweight PDFium image-object pass. Raw
neighboring text may be consulted only to prove an image insertion point; it is
not emitted and does not alter Inspector paragraphs, headings, lists, or tables.
An image with an ambiguous position remains an asset without an invented
reading-order placement and produces a warning.

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
