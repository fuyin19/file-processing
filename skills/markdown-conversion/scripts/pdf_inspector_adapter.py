"""PDF Inspector baseline adapter with page-local OCR recovery.

Usable PDF Inspector Markdown is authoritative.  PDFium native text is never
used as a content or structure fallback: PDFium is opened only by the OCR
provider to rasterize selected pages, or by the optional bundle image adapter.
"""
from __future__ import annotations

import importlib.metadata
import os
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from adapters import markdown_to_canonical
from canonical import (
    MOJIBAKE_PATTERNS,
    make_text_fields,
    normalize_canonical_text,
    sha256_file,
    stable_id,
)
from ocr_provider import OcrProviderError, OcrUnavailableError


STANDARD_CID_COLLECTIONS = {
    "Adobe-CNS1",
    "Adobe-GB1",
    "Adobe-Japan1",
    "Adobe-Korea1",
}
_CID_CMAP_CACHE: dict[tuple[str, bool], bytes] = {}


def pdf_inspector_version() -> str:
    try:
        return importlib.metadata.version("pdf-inspector")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def _pdf_inspector():
    import pdf_inspector

    return pdf_inspector


def _resolve(value):
    getter = getattr(value, "get_object", None)
    return getter() if callable(getter) else value


def _object_identity(value: Any) -> tuple[str, int] | tuple[str, int, int]:
    reference = getattr(value, "indirect_reference", None)
    if reference is not None:
        return ("indirect", int(reference.idnum), int(reference.generation))
    return ("direct", id(value))


def _iter_resource_fonts(resources: Any, seen_resources: set[tuple]) -> Iterable[Any]:
    resources = _resolve(resources)
    if not resources:
        return
    resource_key = _object_identity(resources)
    if resource_key in seen_resources:
        return
    seen_resources.add(resource_key)
    fonts = _resolve(resources.get("/Font"))
    if fonts:
        for font in fonts.values():
            yield _resolve(font)
    xobjects = _resolve(resources.get("/XObject"))
    if not xobjects:
        return
    for xobject in xobjects.values():
        resolved = _resolve(xobject)
        nested = _resolve(resolved.get("/Resources")) if resolved else None
        if nested:
            yield from _iter_resource_fonts(nested, seen_resources)


def _standard_cid_collection(font: Any) -> tuple[str, bool] | None:
    if not font or str(font.get("/Subtype")) != "/Type0" or font.get("/ToUnicode"):
        return None
    encoding = str(font.get("/Encoding"))
    if encoding not in {"/Identity-H", "/Identity-V"}:
        return None
    descendants = _resolve(font.get("/DescendantFonts")) or []
    if not descendants:
        return None
    descendant = _resolve(descendants[0])
    system_info = _resolve(descendant.get("/CIDSystemInfo")) if descendant else None
    if not system_info or str(system_info.get("/Registry")) != "Adobe":
        return None
    collection = f"Adobe-{system_info.get('/Ordering')}"
    if collection not in STANDARD_CID_COLLECTIONS:
        return None
    return collection, encoding == "/Identity-V"


def _unicode_hex(value: str) -> str:
    return value.encode("utf-16-be", errors="strict").hex().upper()


def _build_standard_tounicode_cmap(collection: str, *, vertical: bool) -> bytes:
    cache_key = (collection, vertical)
    cached = _CID_CMAP_CACHE.get(cache_key)
    if cached is not None:
        return cached
    from pdfminer.cmapdb import CMapDB

    unicode_map = CMapDB.get_unicode_map(collection, vertical=vertical)
    mappings: list[tuple[int, str]] = []
    for cid, value in sorted(unicode_map.cid2unichr.items()):
        if not 0 <= int(cid) <= 0xFFFF or not value or "\ufffd" in value:
            continue
        try:
            _unicode_hex(value)
        except UnicodeEncodeError:
            continue
        mappings.append((int(cid), value))
    if not mappings:
        raise RuntimeError(f"No Unicode CID mappings available for {collection}")

    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        f"/CMapName /{collection}-Identity-{'V' if vertical else 'H'}-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    for offset in range(0, len(mappings), 100):
        chunk = mappings[offset : offset + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        lines.extend(
            f"<{cid:04X}> <{_unicode_hex(value)}>" for cid, value in chunk
        )
        lines.append("endbfchar")
    lines.extend(
        [
            "endcmap",
            "CMapName currentdict /CMap defineresource pop",
            "end",
            "end",
            "",
        ]
    )
    result = "\n".join(lines).encode("ascii")
    _CID_CMAP_CACHE[cache_key] = result
    return result


def repair_standard_cid_tounicode(source: Path, target: Path) -> tuple[int, list[str]]:
    """Write a temporary repaired copy for standard Identity CID fonts."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    reader = PdfReader(str(source), strict=False)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    streams: dict[tuple[str, bool], Any] = {}
    seen_fonts: set[tuple] = set()
    repaired = 0
    collections: set[str] = set()
    for page in writer.pages:
        for font in _iter_resource_fonts(page.get("/Resources"), set()):
            font_key = _object_identity(font)
            if font_key in seen_fonts:
                continue
            seen_fonts.add(font_key)
            mapping = _standard_cid_collection(font)
            if mapping is None:
                continue
            collection, vertical = mapping
            stream_key = (collection, vertical)
            if stream_key not in streams:
                stream = DecodedStreamObject()
                stream.set_data(
                    _build_standard_tounicode_cmap(
                        collection,
                        vertical=vertical,
                    )
                )
                streams[stream_key] = writer._add_object(stream)
            font[NameObject("/ToUnicode")] = streams[stream_key]
            repaired += 1
            collections.add(collection)
    if repaired:
        with target.open("wb") as handle:
            writer.write(handle)
    return repaired, sorted(collections)


def _warning(
    code: str,
    message: str,
    content_loss: bool,
    source_unit: str | None = None,
) -> dict[str, Any]:
    warning: dict[str, Any] = {
        "code": code,
        "message": message,
        "content_loss": content_loss,
    }
    if source_unit:
        warning["source_unit"] = source_unit
    return warning


def _page_number(locator: dict[str, Any]) -> int | None:
    value = locator.get("page")
    return int(value) if isinstance(value, int) else None


def _pdf_page_count(source: str) -> int:
    from pypdf import PdfReader

    return len(PdfReader(source, strict=False).pages)


def _locator_identity(locator: dict[str, Any]) -> dict[str, Any]:
    volatile = {"extraction_method", "ocr_provider", "ocr_version", "ocr_confidence"}
    identity = {key: value for key, value in locator.items() if key not in volatile}
    if isinstance(identity.get("spans"), list):
        identity["spans"] = [
            _locator_identity(span) if isinstance(span, dict) else span
            for span in identity["spans"]
        ]
    return identity


def _reassign_content_ids(
    content: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    document_id: str,
) -> None:
    tables_by_id = {str(table["table_id"]): table for table in tables}
    for occurrence, node in enumerate(content, start=1):
        if node["type"] == "table":
            table = tables_by_id[str(node["table_id"])]
            table_id = stable_id(
                "table",
                document_id,
                _locator_identity(node["source_locator"]),
                "table",
                occurrence,
            )
            table["table_id"] = table_id
            table["source_locator"] = node["source_locator"]
            node["table_id"] = table_id
        node["id"] = stable_id(
            "node",
            document_id,
            _locator_identity(node["source_locator"]),
            node["type"],
            occurrence,
        )


def _remap_inspector_document(
    markdown: str,
    document_id: str,
    mode: str,
    document_unit: dict[str, Any],
    total_pages: int,
    ocr_placeholders: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse Inspector's full Markdown once and replace OCR placeholders."""
    parsed = markdown_to_canonical(
        markdown,
        document_id,
        mode,
        "pdf",
        source_index=1,
    )
    old_unit = parsed["source_units"][0]["id"]
    retained_table_ids: set[str] = set()
    content: list[dict[str, Any]] = []
    for node in parsed.get("content", []):
        placeholder = str(node.get("raw_text") or "")
        if placeholder in ocr_placeholders:
            content.append(ocr_placeholders[placeholder])
            continue
        locator = node.setdefault("source_locator", {})
        if locator.get("source_unit_id") == old_unit:
            locator["source_unit_id"] = document_unit["id"]
        locator["page_range"] = [1, total_pages]
        locator["extraction_method"] = "pdf-inspector"
        if node.get("type") == "table":
            retained_table_ids.add(str(node["table_id"]))
        content.append(node)

    tables: list[dict[str, Any]] = []
    for table in parsed.get("tables", []):
        if str(table.get("table_id")) not in retained_table_ids:
            continue
        locator = table.setdefault("source_locator", {})
        if locator.get("source_unit_id") == old_unit:
            locator["source_unit_id"] = document_unit["id"]
        locator["page_range"] = [1, total_pages]
        locator["extraction_method"] = "pdf-inspector"
        tables.append(table)
    return content, tables


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _visible_signature(value: str) -> tuple[str, list[int]]:
    """Return a formatting-insensitive signature plus raw-character offsets."""
    characters: list[str] = []
    raw_offsets: list[int] = []
    for raw_offset, character in enumerate(value):
        normalized = unicodedata.normalize("NFKC", character).casefold()
        for normalized_character in normalized:
            if normalized_character.isalnum():
                characters.append(normalized_character)
                raw_offsets.append(raw_offset)
    return "".join(characters), raw_offsets


def _cid_repaired_page_is_readable(markdown: str, reason: str) -> bool:
    """Treat a repaired CID page as usable only after conservative checks."""
    if reason.strip() != "suspected_garbled_text" or any(
        marker in markdown for marker in MOJIBAKE_PATTERNS
    ):
        return False
    signature, _ = _visible_signature(markdown)
    if len(signature) < 32:
        return False
    for character in markdown:
        codepoint = ord(character)
        if (
            character in {"\ufffd", "\ufffe", "\x02"}
            or (codepoint < 32 and character not in "\t\r\n")
            or 0xE000 <= codepoint <= 0xF8FF
        ):
            return False
    return True


def _unique_occurrence(haystack: str, needle: str) -> int | None:
    if not needle:
        return None
    first = haystack.find(needle)
    if first < 0 or haystack.find(needle, first + 1) >= 0:
        return None
    return first


def _sample_window_offsets(length: int, window: int, samples: int = 64) -> list[int]:
    maximum = length - window
    if maximum <= 0:
        return [0]
    count = min(samples, maximum + 1)
    if count <= 1:
        return [0]
    return sorted({round(index * maximum / (count - 1)) for index in range(count)})


def _align_selected_page(
    global_markdown: str,
    page_markdown: str,
) -> dict[str, Any] | None:
    """Return only the page text span proven to occur in document Markdown.

    Exact whole-page matches are preferred.  The affine path exists only for
    formatting differences: multiple unique windows must agree on one offset,
    then that offset is extended character by character to prove the complete
    page signature.  Reordered or unmatched fragments are rejected rather than
    bridged by an unsafe deletion envelope.
    """
    global_signature, global_raw_offsets = _visible_signature(global_markdown)
    page_signature, _ = _visible_signature(page_markdown)
    if not global_signature or not page_signature:
        return None

    exact = _unique_occurrence(global_signature, page_signature)
    if exact is not None:
        return {
            "start": exact,
            "end": exact + len(page_signature),
            "global_raw_offsets": global_raw_offsets,
            "method": "exact",
            "support": 1,
            "page_signature": page_signature,
            "page_start": 0,
            "page_end": len(page_signature),
        }

    anchors: set[tuple[int, int, int]] = set()
    for window in (160, 120, 80, 48, 32):
        if len(page_signature) < window:
            continue
        for page_offset in _sample_window_offsets(len(page_signature), window):
            sample = page_signature[page_offset : page_offset + window]
            global_offset = _unique_occurrence(global_signature, sample)
            if global_offset is not None:
                anchors.add((page_offset, global_offset, window))
    if not anchors:
        return None

    deltas = Counter(
        global_offset - page_offset
        for page_offset, global_offset, _ in anchors
    )
    delta, support = deltas.most_common(1)[0]
    supporting = [anchor for anchor in anchors if anchor[1] - anchor[0] == delta]
    distinct_offsets = {anchor[0] for anchor in supporting}
    if (
        support < 3
        or len(distinct_offsets) < 3
        or delta < 0
    ):
        return None

    page_start = min(anchor[0] for anchor in supporting)
    page_end = max(anchor[0] + anchor[2] for anchor in supporting)
    global_start = delta + page_start
    global_end = delta + page_end
    while (
        page_start > 0
        and global_start > 0
        and page_signature[page_start - 1] == global_signature[global_start - 1]
    ):
        page_start -= 1
        global_start -= 1
    while (
        page_end < len(page_signature)
        and global_end < len(global_signature)
        and page_signature[page_end] == global_signature[global_end]
    ):
        page_end += 1
        global_end += 1
    if (
        page_start != 0
        or page_end != len(page_signature)
        or global_start != delta
        or global_end != delta + len(page_signature)
    ):
        return None
    return {
        "start": global_start,
        "end": global_end,
        "global_raw_offsets": global_raw_offsets,
        "method": "affine",
        "support": support,
        "coverage": 1.0,
        "clusters": 1,
        "page_signature": page_signature,
        "page_start": page_start,
        "page_end": page_end,
    }


def _raw_alignment_start(markdown: str, alignment: dict[str, Any]) -> int:
    offsets = alignment["global_raw_offsets"]
    normalized_start = int(alignment["start"])
    if normalized_start <= 0:
        return 0
    if normalized_start >= len(offsets):
        return len(markdown)
    raw_start = int(offsets[normalized_start])
    line_start = markdown.rfind("\n", 0, raw_start) + 1
    prefix_signature, _ = _visible_signature(markdown[line_start:raw_start])
    return raw_start if prefix_signature else line_start


def _raw_alignment_end(markdown: str, alignment: dict[str, Any]) -> int:
    offsets = alignment["global_raw_offsets"]
    normalized_end = int(alignment["end"])
    if normalized_end <= 0:
        return 0
    if normalized_end > len(offsets):
        return len(markdown)
    raw_end = int(offsets[normalized_end - 1]) + 1
    line_end = markdown.find("\n", raw_end)
    if line_end < 0:
        line_end = len(markdown)
    else:
        line_end += 1
    suffix_signature, _ = _visible_signature(markdown[raw_end:line_end])
    return raw_end if suffix_signature else line_end


def _empty_page_insertion_positions(
    global_markdown: str,
    page_markdown: dict[int, str],
    total_pages: int,
) -> dict[int, int]:
    """Locate empty pages only when every global visible character is accounted for.

    PDF Inspector can legitimately return an empty selected-page result for a
    blank or scanned page.  Preserving the healthy global Markdown is safe only
    when the ordered per-page Markdown from Inspector accounts for the complete
    global visible signature; otherwise stale flagged-page text could survive.
    """
    if set(page_markdown) != set(range(1, total_pages + 1)):
        return {}
    global_signature, global_raw_offsets = _visible_signature(global_markdown)
    page_signatures = {
        page: _visible_signature(page_markdown[page])[0]
        for page in range(1, total_pages + 1)
    }
    if "".join(page_signatures.values()) != global_signature:
        return {}

    positions: dict[int, int] = {}
    signature_offset = 0
    for page in range(1, total_pages + 1):
        page_signature = page_signatures[page]
        if not page_signature:
            positions[page] = _raw_alignment_start(
                global_markdown,
                {
                    "global_raw_offsets": global_raw_offsets,
                    "start": signature_offset,
                },
            )
        signature_offset += len(page_signature)
    return positions


def _consecutive_runs(pages: set[int]) -> list[tuple[int, int]]:
    ordered = sorted(pages)
    if not ordered:
        return []
    runs: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        runs.append((start, previous))
        start = previous = page
    runs.append((start, previous))
    return runs


def _replace_with_ocr_placeholders(
    markdown: str,
    replacements: list[tuple[int, int, list[tuple[int, str]]]],
) -> str:
    """Replace proven Inspector page runs with ordered OCR placeholders."""
    previous_end = 0
    for raw_start, raw_end, _ in sorted(replacements):
        if not 0 <= raw_start <= raw_end <= len(markdown):
            raise ValueError("OCR replacement span is outside Inspector Markdown")
        if raw_start < previous_end:
            raise ValueError("OCR replacement spans overlap")
        previous_end = raw_end

    result = markdown
    for raw_start, raw_end, items in sorted(
        replacements,
        key=lambda item: (item[0], item[1]),
        reverse=True,
    ):
        placeholders = "\n\n".join(
            placeholder for _, placeholder in sorted(items)
        )
        prefix = result[:raw_start]
        suffix = result[raw_end:]
        if placeholders:
            before = "" if not prefix or prefix.endswith("\n\n") else "\n\n"
            after = "" if not suffix or suffix.startswith("\n\n") else "\n\n"
            result = prefix + before + placeholders + after + suffix
            continue

        trailing = len(prefix) - len(prefix.rstrip("\n"))
        leading = len(suffix) - len(suffix.lstrip("\n"))
        separator = "\n" * max(0, 2 - trailing - leading) if prefix and suffix else ""
        result = prefix + separator + suffix
    return result


def _ocr_spans(result: Any) -> list[Any]:
    spans = _value(result, "spans")
    if spans is None and isinstance(result, list):
        spans = result
    return [span for span in (spans or []) if str(_value(span, "text", "")).strip()]


def _ocr_metadata(result: Any, provider: Any, usable_characters: int) -> dict[str, Any]:
    settings = getattr(provider, "settings", None)
    version = _value(result, "engine_version") or getattr(provider, "version", "unknown")
    if callable(version):
        version = version()
    fields = {
        "provider": _value(result, "engine") or getattr(provider, "name", "ocr"),
        "version": str(version),
        "runtime": _value(result, "runtime"),
        "runtime_version": _value(result, "runtime_version"),
        "model_profile": _value(result, "model_profile"),
        "language": _value(result, "language") or getattr(settings, "language", None),
        "requested_dpi": _value(result, "requested_dpi") or getattr(settings, "dpi", None),
        "effective_dpi": _value(result, "effective_dpi"),
        "min_confidence": _value(result, "min_confidence")
        if _value(result, "min_confidence") is not None
        else getattr(settings, "min_confidence", None),
        "raster_width": _value(result, "raster_width"),
        "raster_height": _value(result, "raster_height"),
        "usable_characters": usable_characters,
        "dropped_low_confidence": int(_value(result, "dropped_low_confidence", 0) or 0),
        "dropped_invalid": int(_value(result, "dropped_invalid", 0) or 0),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _ocr_page_content(
    result: Any,
    provider: Any,
    unit: dict[str, Any],
    page: int,
    mode: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spans = _ocr_spans(result)
    raw_text = "\n".join(str(_value(span, "text", "")).strip() for span in spans)
    usable_characters = sum(not character.isspace() for character in raw_text)
    metadata = _ocr_metadata(result, provider, usable_characters)
    if usable_characters < 5:
        return [], metadata

    boxes = [list(map(float, _value(span, "bbox", []))) for span in spans]
    boxes = [box for box in boxes if len(box) == 4]
    locator: dict[str, Any] = {
        "source_unit_id": unit["id"],
        "page": page,
        "extraction_method": "ocr",
        "ocr_provider": str(metadata.get("provider", "ocr")),
        "ocr_version": str(metadata.get("version", "unknown")),
        "ocr_line_count": len(spans),
    }
    confidences = [
        float(_value(span, "confidence"))
        for span in spans
        if _value(span, "confidence") is not None
    ]
    if confidences:
        locator["ocr_confidence"] = round(min(confidences), 6)
    if boxes:
        locator["bbox"] = [
            round(min(box[0] for box in boxes), 3),
            round(min(box[1] for box in boxes), 3),
            round(max(box[2] for box in boxes), 3),
            round(max(box[3] for box in boxes), 3),
        ]
    node = {
        "id": "pending",
        "type": "paragraph",
        "source_locator": locator,
        **make_text_fields(raw_text, raw_text, mode, defer=True),
    }
    return [node], metadata


def _run_ocr_pages(
    source: str,
    pages: set[int],
    provider: Any,
) -> dict[int, tuple[Any | None, str | None, str | None]]:
    results: dict[int, tuple[Any | None, str | None, str | None]] = {}
    if not pages:
        return results
    if provider is None or not bool(getattr(provider, "available", True)):
        for page in pages:
            results[page] = (
                None,
                "ocr_unavailable",
                "OCR backend is unavailable",
            )
        return results

    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(source)
    except Exception as exc:
        for page in pages:
            results[page] = (
                None,
                "ocr_unavailable",
                f"PDF page rasterization is unavailable ({type(exc).__name__})",
            )
        return results
    try:
        for page_number in sorted(pages):
            page = None
            try:
                page = document.get_page(page_number - 1)
                results[page_number] = (
                    provider.extract(page, page_number),
                    None,
                    None,
                )
            except OcrUnavailableError as exc:
                results[page_number] = (None, "ocr_unavailable", str(exc))
            except OcrProviderError as exc:
                results[page_number] = (None, "ocr_failed", str(exc))
            except Exception as exc:
                results[page_number] = (
                    None,
                    "ocr_failed",
                    f"OCR failed with {type(exc).__name__}: {exc}",
                )
            finally:
                if page is not None:
                    page.close()
    finally:
        document.close()
    return results


def _text_fingerprint(node: dict[str, Any]) -> str:
    value = str(node.get("raw_text") or node.get("text") or "")
    return "".join(character.casefold() for character in value if not character.isspace())


def _extract_image(image: Any, target: Path) -> None:
    """Render one PDFium image object to PNG for optional bundle export."""
    bitmap = image.get_bitmap(render=True, scale_to_original=True)
    try:
        bitmap.to_pil().save(target, format="PNG")
    finally:
        bitmap.close()


def _extract_pdf_image_support(
    source: str,
    document_id: str,
    asset_dir: Path,
    units_by_page: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Extract bundle images without running PDFium's document parser.

    PDFium is used only to enumerate and render image objects.  Nearby raw text
    objects are retained solely as conservative placement anchors; they never
    become canonical document text or influence Inspector's structure.
    """
    import pypdfium2 as pdfium

    assets: list[dict[str, Any]] = []
    content: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    document = pdfium.PdfDocument(source)
    try:
        for page_index in range(len(document)):
            page_number = page_index + 1
            unit = units_by_page.get(page_number)
            if unit is None:
                continue
            page = document.get_page(page_index)
            textpage = None
            try:
                objects = list(page.get_objects(max_depth=15))
                image_objects = [
                    obj
                    for obj in objects
                    if obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE
                ]
                if not image_objects:
                    continue

                textpage = page.get_textpage()
                image_ordinal = 0
                page_content: list[dict[str, Any]] = []
                asset_dir.mkdir(parents=True, exist_ok=True)
                for object_index, obj in enumerate(objects, start=1):
                    if obj.type == pdfium.raw.FPDF_PAGEOBJ_TEXT:
                        try:
                            value = pdfium.PdfTextObj(
                                obj.raw,
                                textpage=textpage,
                            ).extract() or ""
                        except Exception:
                            continue
                        if value.strip():
                            page_content.append(
                                {
                                    "type": "paragraph",
                                    "raw_text": value,
                                    "text": value,
                                    "source_locator": {
                                        "source_unit_id": unit["id"],
                                        "page": page_number,
                                    },
                                }
                            )
                        continue
                    if obj.type != pdfium.raw.FPDF_PAGEOBJ_IMAGE:
                        continue

                    image_ordinal += 1
                    try:
                        bbox = [round(float(value), 3) for value in obj.get_bounds()]
                        locator = {
                            "source_unit_id": unit["id"],
                            "page": page_number,
                            "bbox": bbox,
                        }
                        asset_id = stable_id(
                            "asset",
                            document_id,
                            locator,
                            "image",
                            image_ordinal,
                        )
                        filename = f"{asset_id}.png"
                        target = asset_dir / filename
                        try:
                            _extract_image(obj, target)
                            record = {
                                "asset_id": asset_id,
                                "type": "image",
                                "path": f"assets/images/{filename}",
                                "sha256": sha256_file(target),
                                "media_type": "image/png",
                                "source_locator": locator,
                                "alt": (
                                    f"PDF page {page_number} image {image_ordinal}"
                                ),
                                "caption": "",
                            }
                            assets.append(record)
                            page_content.append(
                                {
                                    "id": "pending",
                                    "type": "image",
                                    "source_locator": locator,
                                    "asset_id": asset_id,
                                }
                            )
                        except Exception:
                            target.unlink(missing_ok=True)
                            raise
                    except Exception as exc:
                        warnings.append(
                            _warning(
                                "pdf_image_extraction_failed",
                                (
                                    f"Page {page_number} image {image_ordinal} "
                                    f"(object {object_index}): "
                                    f"{type(exc).__name__}: {exc}"
                                ),
                                True,
                                unit["id"],
                            )
                        )
                content.extend(page_content)
            finally:
                if textpage is not None:
                    textpage.close()
                page.close()
    finally:
        document.close()
    return {
        "source_units": [],
        "content": content,
        "tables": [],
        "assets": assets,
        "relationships": [],
        "warnings": warnings,
    }


def _merge_support_images(
    inspector_content: list[dict[str, Any]],
    support_content: list[dict[str, Any]],
    unit: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    """Add bundle images only when native text gives unique adjacent anchors."""
    images = [node for node in support_content if node.get("type") == "image"]
    if not images:
        return inspector_content, 0
    content = list(inspector_content)
    support_indexes = {id(node): index for index, node in enumerate(support_content)}
    slots: dict[int, list[dict[str, Any]]] = {}
    ambiguous = 0
    for image in images:
        index = support_indexes[id(image)]
        previous = next(
            (
                _text_fingerprint(node)
                for node in reversed(support_content[:index])
                if node.get("type") in {"heading", "paragraph", "list_item"}
                and _text_fingerprint(node)
            ),
            None,
        )
        following = next(
            (
                _text_fingerprint(node)
                for node in support_content[index + 1 :]
                if node.get("type") in {"heading", "paragraph", "list_item"}
                and _text_fingerprint(node)
            ),
            None,
        )

        def unique_anchor(value: str | None, position: str) -> int | None:
            if not value:
                return None
            partial = len(value) >= 12
            matches = [
                candidate
                for candidate, node in enumerate(content)
                if node.get("type") in {"heading", "paragraph", "list_item"}
                and (
                    _text_fingerprint(node) == value
                    or (
                        partial
                        and position == "previous"
                        and _text_fingerprint(node).endswith(value)
                    )
                    or (
                        partial
                        and position == "following"
                        and _text_fingerprint(node).startswith(value)
                    )
                )
            ]
            return matches[0] if len(matches) == 1 else None

        previous_index = unique_anchor(previous, "previous")
        following_index = unique_anchor(following, "following")
        slot: int | None = None
        if previous_index is not None and following_index is not None:
            if previous_index < following_index:
                slot = previous_index + 1
        elif previous_index is not None and following is None:
            slot = previous_index + 1
        elif following_index is not None and previous is None:
            slot = following_index
        if slot is None:
            ambiguous += 1
            continue
        clone = dict(image)
        clone["source_locator"] = dict(image.get("source_locator", {}))
        clone["source_locator"]["source_unit_id"] = unit["id"]
        slots.setdefault(slot, []).append(clone)

    merged: list[dict[str, Any]] = []
    for index in range(len(content) + 1):
        merged.extend(slots.get(index, []))
        if index < len(content):
            merged.append(content[index])
    return merged, ambiguous


class PdfInspectorAdapter:
    name = "pdf-inspector"
    limitations = [
        "formulas_not_semantically_recognized",
        "ocr_fallback_has_page_level_paragraph_structure_only",
        "running_headers_and_footers_follow_pdf_inspector_defaults",
        "standard_cid_tounicode_repair_applied_when_missing",
    ]

    def __init__(
        self,
        ocr_provider=None,
        *,
        ocr_mode: str | None = None,
        inspector=None,
        fallback_adapter=None,
        ocr_runner=None,
    ):
        self.ocr_provider = ocr_provider
        self.ocr_mode = ocr_mode or "off"
        self.inspector = inspector
        # This adapter is consulted only for bundle images.  Its text, tables,
        # chrome, and page structure are never used as fallback content.
        self.fallback_adapter = fallback_adapter
        self.ocr_runner = ocr_runner

    def extract(
        self,
        source: str,
        document_id: str,
        mode: str,
        asset_dir: Path | None = None,
    ) -> dict[str, Any]:
        try:
            total_pages = _pdf_page_count(source)
        except Exception:
            total_pages = 0

        inspector = self.inspector
        global_markdown = ""
        run_spans: dict[tuple[int, int], tuple[int, int]] = {}
        reason_by_page: dict[int, str] = {}
        repaired_count = 0
        repaired_collections: list[str] = []
        repair_error: Exception | None = None
        inspector_error: Exception | None = None
        alignment_ocr_fallback = False
        cid_retained_pages: set[int] = set()
        required_pages: set[int]
        if self.ocr_mode == "force":
            required_pages = set(range(1, total_pages + 1))
            reason_by_page = {page: "forced" for page in required_pages}
        else:
            inspector = inspector or _pdf_inspector()
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix="pdf-inspector-", suffix=".pdf"
            )
            os.close(temporary_fd)
            temporary = Path(temporary_name)
            inspector_source = Path(source)
            try:
                try:
                    repaired_count, repaired_collections = repair_standard_cid_tounicode(
                        Path(source), temporary
                    )
                    if repaired_count:
                        inspector_source = temporary
                except Exception as exc:
                    repair_error = exc
                try:
                    processed = inspector.process_pdf(str(inspector_source))
                    reported_pages = int(_value(processed, "page_count", 0) or 0)
                    if reported_pages > 0:
                        total_pages = reported_pages
                    global_markdown = str(_value(processed, "markdown", "") or "")
                    required_pages = {
                        int(page)
                        for page in (_value(processed, "pages_needing_ocr", []) or [])
                        if 1 <= int(page) <= total_pages
                    }
                    for item in (_value(processed, "ocr_reasons_by_page", []) or []):
                        page = int(_value(item, "page", 0) or 0)
                        reasons = _value(item, "reasons", []) or []
                        if page and reasons:
                            reason_by_page[page] = ", ".join(map(str, reasons))
                    if not global_markdown.strip():
                        required_pages = set(range(1, total_pages + 1))
                except Exception as exc:
                    inspector_error = exc
                    required_pages = set(range(1, total_pages + 1))

                for page in required_pages:
                    reason_by_page.setdefault(page, "Inspector requested OCR")

                if (
                    self.ocr_mode != "force"
                    and global_markdown.strip()
                    and required_pages
                ):
                    selected_markdown: dict[int, str] = {}

                    def selected(page: int) -> str:
                        if page not in selected_markdown:
                            result = inspector.process_pdf(
                                str(inspector_source), pages=[page]
                            )
                            selected_markdown[page] = str(
                                _value(result, "markdown", "") or ""
                            )
                        return selected_markdown[page]

                    per_page_extractor = getattr(
                        inspector, "extract_pages_markdown", None
                    )
                    # The full-result OCR signal intentionally favors recall
                    # and can flag short but readable pages.  The per-page API
                    # carries the more precise layout classification, so always
                    # consult it for every initially flagged page rather than
                    # waiting for a selected-page extraction to be empty.
                    if required_pages and callable(per_page_extractor):
                        requested_pages = sorted(required_pages)
                        try:
                            per_page_result = per_page_extractor(
                                str(inspector_source),
                                pages=[page - 1 for page in requested_pages],
                            )
                            entries = list(
                                _value(per_page_result, "pages", []) or []
                            )
                            by_page = {
                                int(_value(entry, "page", -1)) + 1: entry
                                for entry in entries
                            }
                            if set(by_page) == set(requested_pages):
                                refined_required = {
                                    page
                                    for page, entry in by_page.items()
                                    if bool(_value(entry, "needs_ocr", False))
                                }
                                refined_required.update(
                                    int(page)
                                    for page in (
                                        _value(
                                            per_page_result,
                                            "pages_needing_ocr",
                                            [],
                                        )
                                        or []
                                    )
                                    if 1 <= int(page) <= total_pages
                                    and int(page) in by_page
                                )
                                required_pages = refined_required
                                for page in required_pages:
                                    reason_by_page.setdefault(
                                        page, "Inspector requested OCR"
                                    )
                        except Exception:
                            pass

                    if repaired_count:
                        for page in sorted(required_pages):
                            try:
                                page_markdown = selected(page)
                            except Exception:
                                continue
                            if _cid_repaired_page_is_readable(
                                page_markdown,
                                reason_by_page.get(page, ""),
                            ):
                                cid_retained_pages.add(page)
                        required_pages -= cid_retained_pages

                    page_spans: dict[int, tuple[int, int]] = {}
                    alignment_failed = False
                    empty_page_positions: dict[int, int] | None = None
                    if required_pages == set(range(1, total_pages + 1)):
                        run_spans[(1, total_pages)] = (0, len(global_markdown))
                    else:
                        previous_page_start = 0
                        previous_page_end = 0
                        for page in sorted(required_pages):
                            try:
                                page_markdown = selected(page)
                                if not page_markdown.strip():
                                    if empty_page_positions is None:
                                        empty_page_positions = {}
                                        if callable(per_page_extractor):
                                            try:
                                                page_result = per_page_extractor(
                                                    str(inspector_source),
                                                    pages=list(range(total_pages)),
                                                )
                                                page_entries = list(
                                                    _value(
                                                        page_result,
                                                        "pages",
                                                        [],
                                                    )
                                                    or []
                                                )
                                                page_markdown_by_number = {
                                                    int(
                                                        _value(
                                                            entry,
                                                            "page",
                                                            -1,
                                                        )
                                                    )
                                                    + 1: str(
                                                        _value(
                                                            entry,
                                                            "markdown",
                                                            "",
                                                        )
                                                        or ""
                                                    )
                                                    for entry in page_entries
                                                }
                                                empty_page_positions = (
                                                    _empty_page_insertion_positions(
                                                        global_markdown,
                                                        page_markdown_by_number,
                                                        total_pages,
                                                    )
                                                )
                                            except Exception:
                                                empty_page_positions = {}
                                    raw_position = empty_page_positions.get(page)
                                    if raw_position is None or (
                                        raw_position < previous_page_start
                                        or raw_position < previous_page_end
                                    ):
                                        alignment_failed = True
                                        break
                                    page_spans[page] = (
                                        raw_position,
                                        raw_position,
                                    )
                                    previous_page_start = raw_position
                                    previous_page_end = raw_position
                                    continue
                                alignment = _align_selected_page(
                                    global_markdown,
                                    page_markdown,
                                )
                            except Exception:
                                alignment = None
                            if alignment is None:
                                alignment_failed = True
                                break
                            raw_page_start = _raw_alignment_start(
                                global_markdown, alignment
                            )
                            raw_page_end = _raw_alignment_end(
                                global_markdown, alignment
                            )
                            if (
                                raw_page_start >= raw_page_end
                                or raw_page_start < previous_page_start
                                or raw_page_end < previous_page_end
                            ):
                                alignment_failed = True
                                break
                            page_spans[page] = (raw_page_start, raw_page_end)
                            previous_page_start = raw_page_start
                            previous_page_end = raw_page_end

                    runs = _consecutive_runs(set(page_spans))
                    previous_run_end = 0
                    for run in (
                        runs
                        if not alignment_failed and not run_spans
                        else ()
                    ):
                        start, end = run
                        raw_start = page_spans[start][0]
                        raw_end = page_spans[end][1]

                        if (
                            not 0 <= raw_start <= raw_end <= len(global_markdown)
                            or raw_start < previous_run_end
                        ):
                            alignment_failed = True
                            break
                        run_spans[run] = (raw_start, raw_end)
                        previous_run_end = raw_end

                    if alignment_failed:
                        # A misplaced OCR block is worse than the slower but
                        # deterministic all-OCR path.
                        alignment_ocr_fallback = True
                        global_markdown = ""
                        required_pages = set(range(1, total_pages + 1))
                        reason_by_page = {
                            page: "Inspector could not prove the flagged-page text spans"
                            for page in required_pages
                        }
                        run_spans = {}
            finally:
                temporary.unlink(missing_ok=True)

        if total_pages <= 0:
            raise RuntimeError("Could not determine PDF page count")

        document_locator = {
            "kind": "pdf",
            "page_count": total_pages,
            "page_range": [1, total_pages],
        }
        document_unit = {
            "id": stable_id("unit", document_id, document_locator, "document", 1),
            "type": "document",
            "index": 1,
            "locator": document_locator,
            "status": "complete",
            "warnings": [],
        }
        units: list[dict[str, Any]] = [document_unit]
        units_by_page: dict[int, dict[str, Any]] = {}
        for page in range(1, total_pages + 1):
            locator = {"page": page, "bbox_order": ["left", "bottom", "right", "top"]}
            unit = {
                "id": stable_id("unit", document_id, locator, "page", page),
                "type": "page",
                "index": page,
                "locator": locator,
                "status": "complete",
                "warnings": [],
            }
            units.append(unit)
            units_by_page[page] = unit

        for page in sorted(cid_retained_pages):
            unit = units_by_page[page]
            unit["warnings"].append(
                _warning(
                    "pdf_inspector_cid_page_retained",
                    (
                        f"Page {page} retained readable Inspector Markdown "
                        "after standard CID ToUnicode repair"
                    ),
                    False,
                    unit["id"],
                )
            )
            unit["status"] = "warning"

        support: dict[str, Any] | None = None
        try:
            if self.fallback_adapter is not None:
                # Explicit injection is retained as a narrow test/extension seam.
                support = self.fallback_adapter.extract(
                    source,
                    document_id,
                    mode,
                    asset_dir,
                )
            elif asset_dir is not None:
                support = _extract_pdf_image_support(
                    source,
                    document_id,
                    asset_dir,
                    units_by_page,
                )
        except Exception as exc:
            support = {
                "content": [],
                "assets": [],
                "warnings": [
                    _warning(
                        "pdf_image_extraction_failed",
                        (
                            "Optional PDF image extraction failed with "
                            f"{type(exc).__name__}: {exc}"
                        ),
                        True,
                    )
                ],
            }

        runner = self.ocr_runner or _run_ocr_pages
        ocr_results = (
            runner(source, required_pages, self.ocr_provider)
            if required_pages and self.ocr_mode != "off"
            else {}
        )

        ocr_nodes_by_page: dict[int, dict[str, Any]] = {}
        for page in range(1, total_pages + 1):
            unit = units_by_page[page]
            used_ocr = False
            if page in required_pages and self.ocr_mode != "off":
                result, error_code, error_message = ocr_results.get(
                    page,
                    (None, "ocr_failed", "OCR produced no page result"),
                )
                if result is not None:
                    page_content, metadata = _ocr_page_content(
                        result,
                        self.ocr_provider,
                        unit,
                        page,
                        mode,
                    )
                    if page_content:
                        used_ocr = True
                        ocr_nodes_by_page[page] = page_content[0]
                        unit["locator"]["ocr"] = metadata
                        warning = _warning(
                            "ocr_applied",
                            f"Page {page} used OCR because {reason_by_page[page]}",
                            False,
                            unit["id"],
                        )
                        unit["warnings"].append(warning)
                        unit["status"] = "warning"
                    else:
                        error_code = "ocr_incomplete_result"
                        error_message = "OCR recovered fewer than 5 usable characters"
                if not used_ocr and error_code:
                    unit["warnings"].append(
                        _warning(
                            error_code,
                            f"Page {page} {error_message}",
                            True,
                            unit["id"],
                        )
                    )

            if page in required_pages and not used_ocr:
                unit["warnings"].append(
                    _warning(
                        "ocr_required",
                        f"Page {page} requires OCR because {reason_by_page[page]}",
                        True,
                        unit["id"],
                    )
                )
                unit["status"] = "ocr_required"

        ocr_placeholders: dict[str, dict[str, Any]] = {}
        replacements: list[tuple[int, int, list[tuple[int, str]]]] = []
        if global_markdown.strip():
            for (start, end), (raw_start, raw_end) in sorted(run_spans.items()):
                run_placeholders: list[tuple[int, str]] = []
                for page in range(start, end + 1):
                    node = ocr_nodes_by_page.get(page)
                    if node is None:
                        continue
                    placeholder = f"OKF_OCR_PLACEHOLDER_{document_id[-16:]}_{page}"
                    ocr_placeholders[placeholder] = node
                    run_placeholders.append((page, placeholder))
                if raw_start == raw_end and not run_placeholders:
                    continue
                replacements.append((raw_start, raw_end, run_placeholders))
            augmented_markdown = _replace_with_ocr_placeholders(
                global_markdown,
                replacements,
            )
            content, tables = _remap_inspector_document(
                augmented_markdown,
                document_id,
                mode,
                document_unit,
                total_pages,
                ocr_placeholders,
            )
        else:
            content = [ocr_nodes_by_page[page] for page in sorted(ocr_nodes_by_page)]
            tables = []

        support_nodes_by_page: dict[int, list[dict[str, Any]]] = {}
        for node in (support or {}).get("content", []):
            page = _page_number(node.get("source_locator", {}))
            if page is not None:
                support_nodes_by_page.setdefault(page, []).append(node)
        for page, support_content in sorted(support_nodes_by_page.items()):
            content, ambiguous_images = _merge_support_images(
                content,
                support_content,
                units_by_page[page],
            )
            if ambiguous_images:
                unit = units_by_page[page]
                unit["warnings"].append(
                    _warning(
                        "pdf_image_position_ambiguous",
                        f"Page {page} left {ambiguous_images} bundle image(s) unpositioned",
                        False,
                        unit["id"],
                    )
                )
                if unit["status"] == "complete":
                    unit["status"] = "warning"

        document_warnings: list[dict[str, Any]] = []
        if inspector_error is not None:
            inspector_outcome = (
                "Inspector content was unavailable; OCR was disabled"
                if self.ocr_mode == "off"
                else "pages were routed to OCR"
            )
            document_warnings.append(
                _warning(
                    "pdf_inspector_document_ocr_fallback",
                    (
                        "PDF Inspector failed with "
                        f"{type(inspector_error).__name__}; {inspector_outcome}"
                    ),
                    False,
                )
            )
        if alignment_ocr_fallback:
            fallback_outcome = (
                "Inspector content was discarded; OCR was disabled"
                if self.ocr_mode == "off"
                else "the document was routed to ordered OCR"
            )
            document_warnings.append(
                _warning(
                    "pdf_inspector_alignment_ocr_fallback",
                    (
                        "Inspector could not prove the flagged-page text "
                        f"spans; {fallback_outcome}"
                    ),
                    False,
                )
            )
        if repaired_count:
            warning = _warning(
                "pdf_inspector_cid_tounicode_repaired",
                f"Added standard ToUnicode maps to {repaired_count} font(s) for {', '.join(repaired_collections)} before Inspector extraction",
                False,
                document_unit["id"],
            )
            document_unit["warnings"].append(warning)
            if document_unit["status"] == "complete":
                document_unit["status"] = "warning"
        if repair_error is not None:
            document_warnings.append(
                _warning(
                    "pdf_inspector_cid_repair_failed",
                    f"CID repair preflight failed with {type(repair_error).__name__}: {repair_error}",
                    False,
                )
            )
        if document_warnings and document_unit["status"] == "complete":
            document_unit["status"] = "warning"

        normalize_canonical_text(content, tables, mode)
        _reassign_content_ids(content, tables, document_id)
        warnings = [warning for unit in units for warning in unit.get("warnings", [])]
        warnings.extend(warning for warning in document_warnings if warning not in warnings)
        warnings.extend(
            warning
            for warning in (support or {}).get("warnings", [])
            if warning.get("code") == "pdf_image_extraction_failed"
            and warning not in warnings
        )
        return {
            "source_units": units,
            "content": content,
            "tables": tables,
            "assets": (support or {}).get("assets", []),
            "relationships": [],
            "warnings": warnings,
            "title": Path(source).stem,
            "adapter": {
                "name": self.name,
                "version": pdf_inspector_version(),
                "limitations": list(self.limitations),
            },
        }
