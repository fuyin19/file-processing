# Canonical JSON v1

`<stem>.json` is the canonical, loss-aware bundle output of
`markdown-conversion` v7.0.0.
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

For a `text_based`/`mixed` PDF with usable Inspector body, successful OCR that
cannot be placed safely is appended after body image placement, in physical page
order. These real OCR nodes keep their exact page and raw text and add the open
locator field `placement: unanchored_supplement`. The Markdown renderer supplies
the supplement heading, page labels, and possible-duplication notice; these are
not source nodes in Canonical JSON. `pdf_inspector_alignment_unresolved` carries
`content_loss: true`, keeping quality `partial` even when this OCR succeeds.

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
complete. The v7.0.0 Inspector path does not perform this stitching and instead
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
Every Office occurrence also has an open `relationships` record with
`type: image_occurrence`, `source_unit_id`, `asset_id`, a positive
`occurrence_index`, and `placement: resolved|unresolved`. A resolved occurrence
has `content_node_id`; an unresolved occurrence must not. Validators reject
dangling references and duplicate occurrence identities. Optional
`--enrich-images` adds a paragraph after a resolved image plus an
`image_ocr_text` relationship linking the asset, image node, and OCR text node.
Local PDF bundles default to rendered figure enhancement (`--pdf-images auto`).
The same image node and asset structures hold composited page crops or page
previews, including painted text and vector graphics. Their open
`source_locator` includes the physical `page`, PDF `bbox`,
`extraction_method: pdfium_page_render`, and
`placement: body_region|pdf_page_supplement`. These locator values are distinct
from Office `image_occurrence` relationship placement values.

`body_region` means both a complete region and its position relative to existing
Inspector paragraphs or tables were proved. Original body values and order are
unchanged. A `pdf_page_supplement` preserves the full visible page when a complete
region or precise position cannot be established. These image nodes follow the
body and any unplaced OCR supplement in ascending physical page order, with at
most one page preview per page. The renderer supplies the `Supplementary PDF
figures` heading, page labels, and possible-duplication notice; these labels are
not source nodes or OCR text. A page preview may coexist with precise regions on
that page and must not be counted as a precise placement.

`--pdf-images objects` keeps image-object export and the existing unique raw
neighbor-anchor rule, with cached matching. Ambiguous objects remain assets
without an invented reading-order position. `--pdf-images off` disables image
enhancement. Neither mode adds PDFium body text, changes OCR routing, or alters
Inspector structure. Matching indexes and volatile image timings are not public
Canonical fields.

## Quality and publication

- `complete`: no warning and no known loss.
- `complete_with_warnings`: confidence/limitation warning without known omitted
  semantic content.
- `partial`: known omitted/quarantined content or unresolved PDF order/duplication, while artifacts remain
  valid and at least one usable content node exists.

All three states publish with exit code `0`. In bundle mode, invalid schema/
serialization, duplicate IDs, broken references, asset verification failure,
no usable content, or transactional publication failure prevents publication. Markdown-only emits
no Canonical JSON artifact and therefore skips JSON Schema validation while
retaining applicable semantic checks.

PDF image enhancement begins only after the Inspector/OCR result passes the
usable-body gate. Images cannot turn an otherwise empty or unreadable PDF into
a successful body conversion. An enhancement timeout or failure retains the
completed body and records the unprocessed range as a loss. A successful page
supplement alone is a placement warning; a known unrecovered inline image glyph
is a loss even when its page preview is retained.

`outputs` records the rendered Markdown path/hash and all published asset
paths/hashes. JSON does not record its own hash, avoiding a circular digest.
