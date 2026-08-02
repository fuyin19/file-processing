"""Native born-digital PDF adapter for the canonical v6 pipeline."""
from __future__ import annotations

import importlib.metadata
import math
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Protocol

from canonical import make_text_fields, normalize_canonical_text, sha256_file, stable_id


LIST_RE = re.compile(r"^\s*(?:[•◦▪■·]|[-*]|\([0-9A-Za-zivxlcdm]+\)|[0-9]+[.)、]|[一二三四五六七八九十]+[、.])\s*", re.I)
NUMERIC_RE = re.compile(r"(?:\(?-?\d[\d,.%]*\)?|[-–—])")
SENTENCE_END_RE = re.compile(r"[。！？；;.!?]$")


def _pdfium():
    import pypdfium2

    return pypdfium2


def _version() -> str:
    try:
        return importlib.metadata.version("pypdfium2")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def bbox_union(boxes: Iterable[list[float]]) -> list[float]:
    values = list(boxes)
    return [
        round(min(box[0] for box in values), 3),
        round(min(box[1] for box in values), 3),
        round(max(box[2] for box in values), 3),
        round(max(box[3] for box in values), 3),
    ]


def transform_bbox_for_orientation(
    box: list[float], width: float, height: float, angle: int
) -> tuple[list[float], float, float]:
    left, bottom, right, top = box
    angle %= 360
    if angle == 90:
        transformed, layout_width, layout_height = [bottom, width - right, top, width - left], height, width
    elif angle == 180:
        transformed, layout_width, layout_height = [width - right, height - top, width - left, height - bottom], width, height
    elif angle == 270:
        transformed, layout_width, layout_height = [height - top, left, height - bottom, right], height, width
    else:
        transformed, layout_width, layout_height = list(box), width, height
    return [round(float(value), 3) for value in transformed], layout_width, layout_height


def _item_box(item: dict[str, Any]) -> list[float]:
    return item.get("layout_bbox") or item["bbox"]


def _text_angle(matrix) -> int:
    angle = math.degrees(math.atan2(matrix.b, matrix.a)) % 360
    snapped = int(round(angle / 90.0) * 90) % 360
    distance = abs(((angle - snapped + 180) % 360) - 180)
    return snapped if distance <= 12 else int(round(angle))


def _font_size(matrix, nominal_size: float) -> float:
    return round(max(math.hypot(matrix.a, matrix.b), math.hypot(matrix.c, matrix.d)) * nominal_size, 3)


def _merge_fragments(fragments: list[dict[str, Any]]) -> str:
    fragments = sorted(fragments, key=lambda item: _item_box(item)[0])
    output = ""
    previous: dict[str, Any] | None = None
    for fragment in fragments:
        value = fragment["text"]
        if not value:
            continue
        if output and previous is not None:
            gap = _item_box(fragment)[0] - _item_box(previous)[2]
            char_width = max(float(previous["char_width"]), float(fragment["char_width"]), 1.0)
            if (
                gap > max(1.5, char_width * 0.55)
                and output[-1].isascii()
                and value[0].isascii()
                and output[-1].isalnum()
                and value[0].isalnum()
            ):
                output += " "
        output += value
        previous = fragment
    return output.strip()


def _line_cells(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    for fragment in sorted(fragments, key=lambda item: _item_box(item)[0]):
        if not groups:
            groups.append([fragment])
            continue
        previous = groups[-1][-1]
        gap = _item_box(fragment)[0] - _item_box(previous)[2]
        threshold = max(4.0, 1.8 * max(previous["char_width"], fragment["char_width"]))
        if gap <= threshold:
            groups[-1].append(fragment)
        else:
            groups.append([fragment])
    return [
        {
            "text": _merge_fragments(group),
            "bbox": bbox_union(fragment["bbox"] for fragment in group),
            "layout_bbox": bbox_union(_item_box(fragment) for fragment in group),
        }
        for group in groups
        if _merge_fragments(group)
    ]


def _group_lines(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        fragments,
        key=lambda item: (-(_item_box(item)[1] + _item_box(item)[3]) / 2, _item_box(item)[0]),
    )
    groups: list[dict[str, Any]] = []
    for fragment in ordered:
        box = _item_box(fragment)
        center = (box[1] + box[3]) / 2
        height = max(box[3] - box[1], 1.0)
        selected = None
        for group in groups[-10:]:
            union = bbox_union(_item_box(value) for value in group["fragments"])
            overlap = max(0.0, min(box[3], union[3]) - max(box[1], union[1]))
            if abs(center - group["center"]) <= max(2.0, min(height, group["height"]) * 0.42) or overlap / max(min(height, group["height"]), 0.1) >= 0.5:
                selected = group
                break
        if selected is None:
            groups.append({"center": center, "height": height, "fragments": [fragment]})
        else:
            selected["fragments"].append(fragment)
            union = bbox_union(_item_box(value) for value in selected["fragments"])
            selected["center"] = (union[1] + union[3]) / 2
            selected["height"] = max(union[3] - union[1], 1.0)
    lines: list[dict[str, Any]] = []
    for group in groups:
        values = sorted(group["fragments"], key=lambda item: _item_box(item)[0])
        text = _merge_fragments(values)
        if text:
            lines.append(
                {
                    "text": text,
                    "bbox": bbox_union(value["bbox"] for value in values),
                    "layout_bbox": bbox_union(_item_box(value) for value in values),
                    "font_size": max(value["font_size"] for value in values),
                    "font_weight": max(value["font_weight"] for value in values),
                    "cells": _line_cells(values),
                }
            )
    return sorted(lines, key=lambda item: (-_item_box(item)[3], _item_box(item)[0]))


def _order_lines(lines: list[dict[str, Any]], width: float) -> tuple[list[dict[str, Any]], bool]:
    """Apply a conservative two-column order when geometry strongly supports it."""
    split_candidates = [
        line
        for line in lines
        if len(line.get("cells", [])) == 2
        and not any(
            re.fullmatch(r"\(?-?\d[\d,.%]*\)?", cell["text"].strip())
            for cell in line["cells"]
        )
        and line["cells"][1]["layout_bbox"][0] - line["cells"][0]["layout_bbox"][2] >= width * 0.08
    ]
    if len(split_candidates) >= 2:
        expanded: list[dict[str, Any]] = []
        for line in lines:
            if line not in split_candidates:
                expanded.append(line)
                continue
            for cell in line["cells"]:
                expanded.append(
                    {
                        "text": cell["text"],
                        "bbox": cell["bbox"],
                        "layout_bbox": cell["layout_bbox"],
                        "font_size": line["font_size"],
                        "font_weight": line["font_weight"],
                        "cells": [cell],
                    }
                )
        lines = expanded
    candidates = [line for line in lines if (_item_box(line)[2] - _item_box(line)[0]) <= width * 0.55]
    left = [line for line in candidates if (_item_box(line)[0] + _item_box(line)[2]) / 2 < width * 0.45]
    right = [line for line in candidates if (_item_box(line)[0] + _item_box(line)[2]) / 2 > width * 0.55]
    if len(left) < 2 or len(right) < 2:
        return sorted(lines, key=lambda item: (-_item_box(item)[3], _item_box(item)[0])), False
    overlap = min(max(_item_box(item)[3] for item in left), max(_item_box(item)[3] for item in right)) - max(
        min(_item_box(item)[1] for item in left), min(_item_box(item)[1] for item in right)
    )
    if overlap <= 0:
        return sorted(lines, key=lambda item: (-_item_box(item)[3], _item_box(item)[0])), False
    top = max(max(_item_box(item)[3] for item in left), max(_item_box(item)[3] for item in right))
    bottom = min(min(_item_box(item)[1] for item in left), min(_item_box(item)[1] for item in right))
    above = [line for line in lines if line not in candidates and _item_box(line)[1] >= top - 2]
    below = [line for line in lines if line not in candidates and _item_box(line)[3] <= bottom + 2]
    middle_full = [line for line in lines if line not in candidates and line not in above and line not in below]
    ordered = (
        sorted(above, key=lambda item: -_item_box(item)[3])
        + sorted(left, key=lambda item: -_item_box(item)[3])
        + sorted(right, key=lambda item: -_item_box(item)[3])
        + sorted(middle_full, key=lambda item: -_item_box(item)[3])
        + sorted(below, key=lambda item: -_item_box(item)[3])
    )
    remaining = [line for line in lines if line not in ordered]
    ordered.extend(sorted(remaining, key=lambda item: (-_item_box(item)[3], _item_box(item)[0])))
    return ordered, True


def _join_lines(values: list[str]) -> str:
    output = ""
    for value in values:
        value = value.strip()
        if not value:
            continue
        if not output:
            output = value
            continue
        if output.endswith("-") and value[0].isascii() and value[0].islower():
            output = output[:-1] + value
        elif output[-1].isascii() and value[0].isascii() and output[-1].isalnum() and value[0].isalnum():
            output += " " + value
        else:
            output += value
    return output


def _classify_blocks(lines: list[dict[str, Any]], page_number: int) -> list[dict[str, Any]]:
    if not lines:
        return []
    body_sizes = [line["font_size"] for line in lines if len(line["text"]) >= 8]
    body_size = statistics.median(body_sizes or [line["font_size"] for line in lines])
    gaps = []
    for previous, current in zip(lines, lines[1:]):
        gap = _item_box(previous)[1] - _item_box(current)[3]
        if 0 <= gap <= body_size * 4:
            gaps.append(gap)
    normal_gap = statistics.median(gaps) if gaps else body_size * 0.35
    typed: list[tuple[str, dict[str, Any]]] = []
    for line in lines:
        value = line["text"].strip()
        numeric = len(NUMERIC_RE.findall(value))
        if LIST_RE.match(value):
            kind = "list_item"
        elif len(line["cells"]) >= 2 and numeric >= 1:
            kind = "table"
        elif len(value) <= 100 and line["font_size"] >= body_size * 1.18 and not SENTENCE_END_RE.search(value):
            kind = "heading"
        else:
            kind = "paragraph"
        typed.append((kind, line))
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        group = current.pop("_lines")
        current["bbox"] = bbox_union(line["bbox"] for line in group)
        current["layout_bbox"] = bbox_union(_item_box(line) for line in group)
        if current["type"] == "table":
            current["rows"] = [[cell["text"] for cell in line["cells"]] for line in group]
            current["text"] = "\n".join(line["text"] for line in group)
        else:
            current["text"] = _join_lines([line["text"] for line in group])
        blocks.append(current)
        current = None

    for kind, line in typed:
        can_join = False
        if current is not None:
            previous = current["_lines"][-1]
            gap = _item_box(previous)[1] - _item_box(line)[3]
            indent_shift = _item_box(line)[0] - _item_box(previous)[0]
            if current["type"] == kind == "paragraph":
                can_join = gap <= max(normal_gap * 1.75, body_size * 0.9) and abs(indent_shift) <= max(18.0, body_size * 1.5)
            elif current["type"] == "list_item" and kind == "paragraph":
                can_join = gap <= max(normal_gap * 1.6, body_size * 0.8) and indent_shift >= -3
            elif current["type"] == kind == "table":
                can_join = gap <= max(normal_gap * 2.2, body_size * 1.2)
        if not can_join:
            flush()
            current = {"type": kind, "_lines": [line]}
            if kind == "heading":
                ratio = line["font_size"] / max(body_size, 0.1)
                current["level"] = 1 if ratio >= 1.5 else 2 if ratio >= 1.3 else 3
        else:
            current["_lines"].append(line)
    flush()
    return blocks


def _extract_image(image, target: Path) -> None:
    bitmap = image.get_bitmap(render=True, scale_to_original=True)
    try:
        bitmap.to_pil().save(target, format="PNG")
    finally:
        bitmap.close()


class OcrProvider(Protocol):
    name: str

    def extract(self, page, page_number: int) -> list[dict[str, Any]]: ...


class NullOcrProvider:
    name = "none"

    def extract(self, _page, _page_number: int) -> list[dict[str, Any]]:
        return []


class PdfAdapter:
    name = "pdfium"
    limitations = ["ocr_not_available", "formulas_not_semantically_recognized"]

    def __init__(self, ocr_provider=None):
        self.ocr_provider = ocr_provider or NullOcrProvider()

    def extract(
        self,
        source: str,
        document_id: str,
        mode: str,
        asset_dir: Path | None = None,
    ) -> dict[str, Any]:
        pdfium = _pdfium()
        document = pdfium.PdfDocument(source)
        source_units: list[dict[str, Any]] = []
        page_blocks: list[list[dict[str, Any]]] = []
        warnings: list[dict[str, Any]] = []
        assets: list[dict[str, Any]] = []
        page_assets: list[list[dict[str, Any]]] = []
        try:
            for page_index in range(len(document)):
                page_number = page_index + 1
                page = document[page_index]
                textpage = page.get_textpage()
                width, height = page.get_size()
                raw_text = textpage.get_text_range()
                counts = Counter()
                fragments: list[dict[str, Any]] = []
                image_objects: list[Any] = []
                extraction_warnings: list[dict[str, Any]] = []
                for object_index, obj in enumerate(page.get_objects(max_depth=15), start=1):
                    counts[obj.type] += 1
                    if obj.type == pdfium.raw.FPDF_PAGEOBJ_TEXT:
                        try:
                            text_obj = pdfium.PdfTextObj(obj.raw, textpage=textpage)
                            value = text_obj.extract() or ""
                            if not value.strip():
                                continue
                            box = [round(float(item), 3) for item in text_obj.get_bounds()]
                            matrix = text_obj.get_matrix()
                            angle = _text_angle(matrix)
                            font = text_obj.get_font()
                            font_size = _font_size(matrix, float(text_obj.get_font_size()))
                            fragments.append(
                                {
                                    "text": value,
                                    "bbox": box,
                                    "text_angle": angle,
                                    "font_size": font_size,
                                    "font_weight": int(font.get_weight()),
                                    "char_width": max((box[2] - box[0]) / max(len(value.strip()), 1), 0.1),
                                }
                            )
                        except Exception as exc:
                            extraction_warnings.append(
                                {"code": "pdf_text_object_error", "message": f"text object {object_index}: {type(exc).__name__}: {exc}", "content_loss": True}
                            )
                    elif obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE:
                        image_objects.append(obj)
                angle_counts = Counter()
                for fragment in fragments:
                    angle_counts[fragment["text_angle"]] += max(len(fragment["text"].strip()), 1)
                dominant = angle_counts.most_common(1)[0][0] if angle_counts else 0
                total = sum(angle_counts.values())
                share = angle_counts[dominant] / total if total else 1.0
                normalized = dominant in {90, 180, 270} and share >= 0.6
                layout_width, layout_height = width, height
                if normalized:
                    for fragment in fragments:
                        fragment["layout_bbox"], layout_width, layout_height = transform_bbox_for_orientation(
                            fragment["bbox"], width, height, dominant
                        )
                        fragment["char_width"] = max(
                            (fragment["layout_bbox"][2] - fragment["layout_bbox"][0]) / max(len(fragment["text"].strip()), 1),
                            0.1,
                        )
                lines = _group_lines(fragments)
                lines, multi_column = _order_lines(lines, layout_width)
                blocks = _classify_blocks(lines, page_number)
                unit_locator = {
                    "page": page_number,
                    "bbox_order": ["left", "bottom", "right", "top"],
                    "orientation_normalized": normalized,
                    "dominant_text_angle": dominant,
                }
                unit_id = stable_id("unit", document_id, unit_locator, "page", page_number)
                unit_warnings: list[dict[str, Any]] = []
                for item in extraction_warnings:
                    item["source_unit"] = unit_id
                    unit_warnings.append(item)
                visual_objects = counts[pdfium.raw.FPDF_PAGEOBJ_IMAGE] + counts[pdfium.raw.FPDF_PAGEOBJ_PATH]
                status = "complete"
                if len(re.sub(r"\s+", "", raw_text)) < 5 and visual_objects:
                    ocr_blocks = self.ocr_provider.extract(page, page_number)
                    if ocr_blocks:
                        blocks.extend(ocr_blocks)
                        status = "warning"
                        unit_warnings.append(
                            {"code": "ocr_applied", "message": f"Page {page_number} text was supplied by {self.ocr_provider.name}", "content_loss": False, "source_unit": unit_id}
                        )
                    else:
                        status = "ocr_required"
                        unit_warnings.append(
                            {"code": "ocr_required", "message": f"Page {page_number} appears to require OCR", "content_loss": True, "source_unit": unit_id}
                        )
                elif not blocks and not image_objects:
                    status = "empty"
                    unit_warnings.append(
                        {"code": "empty_page", "message": f"Page {page_number} contains no extractable content", "content_loss": True, "source_unit": unit_id}
                    )
                if multi_column:
                    unit_warnings.append(
                        {"code": "multi_column_order_inferred", "message": f"Page {page_number} reading order was inferred from two-column geometry", "content_loss": False, "source_unit": unit_id}
                    )
                if unit_warnings and status == "complete":
                    status = "warning"
                source_units.append(
                    {"id": unit_id, "type": "page", "index": page_number, "locator": unit_locator, "status": status, "warnings": unit_warnings}
                )
                warnings.extend(unit_warnings)
                page_blocks.append(blocks)
                extracted_assets: list[dict[str, Any]] = []
                if asset_dir is not None and image_objects:
                    asset_dir.mkdir(parents=True, exist_ok=True)
                    for image_index, image in enumerate(image_objects, start=1):
                        locator = {
                            "source_unit_id": unit_id,
                            "page": page_number,
                            "bbox": [round(float(v), 3) for v in image.get_bounds()],
                        }
                        asset_id = stable_id("asset", document_id, locator, "image", image_index)
                        filename = f"{asset_id}.png"
                        target = asset_dir / filename
                        try:
                            _extract_image(image, target)
                            record = {
                                "asset_id": asset_id,
                                "type": "image",
                                "path": f"assets/images/{filename}",
                                "sha256": sha256_file(target),
                                "media_type": "image/png",
                                "source_locator": locator,
                                "alt": f"PDF page {page_number} image {image_index}",
                                "caption": "",
                            }
                            assets.append(record)
                            extracted_assets.append(record)
                        except Exception as exc:
                            warning = {
                                "code": "pdf_image_extraction_failed",
                                "message": f"Page {page_number} image {image_index}: {type(exc).__name__}: {exc}",
                                "content_loss": True,
                                "source_unit": unit_id,
                            }
                            warnings.append(warning)
                            source_units[-1]["warnings"].append(warning)
                            if source_units[-1]["status"] == "complete":
                                source_units[-1]["status"] = "warning"
                page_assets.append(extracted_assets)
                textpage.close()
                page.close()
        finally:
            document.close()

        content: list[dict[str, Any]] = []
        tables: list[dict[str, Any]] = []
        occurrence = 0
        for unit, blocks, images in zip(source_units, page_blocks, page_assets, strict=True):
            page_number = unit["index"]
            ordered_items: list[tuple[str, dict[str, Any]]] = [("block", block) for block in blocks]
            for asset in images:
                image_top = asset["source_locator"]["bbox"][3]
                insert_at = next(
                    (
                        index
                        for index, (kind, item) in enumerate(ordered_items)
                        if kind == "block" and image_top > item["bbox"][3]
                    ),
                    len(ordered_items),
                )
                ordered_items.insert(insert_at, ("image", asset))
            for item_kind, block in ordered_items:
                occurrence += 1
                if item_kind == "image":
                    content.append(
                        {
                            "id": stable_id("node", document_id, block["source_locator"], "image", occurrence),
                            "type": "image",
                            "source_locator": block["source_locator"],
                            "asset_id": block["asset_id"],
                        }
                    )
                    continue
                locator = {"source_unit_id": unit["id"], "page": page_number, "bbox": block["bbox"]}
                if block["type"] == "table":
                    table_id = stable_id("table", document_id, locator, "table", occurrence)
                    raw_rows = block["rows"]
                    widths = {len(row) for row in raw_rows}
                    confidence = 0.82 if len(widths) == 1 and next(iter(widths), 0) >= 2 else None
                    table_warnings: list[dict[str, Any]] = []
                    if confidence is None:
                        warning = {"code": "table_structure_uncertain", "message": f"Table on page {page_number} was conservatively normalized", "content_loss": False, "source_unit": unit["id"]}
                        table_warnings.append(warning)
                        warnings.append(warning)
                    rows = [
                        [
                            {**make_text_fields(cell, cell.strip(), mode, defer=True), "value": _parse_number(cell), "rowspan": 1, "colspan": 1}
                            for cell in row
                        ]
                        for row in raw_rows
                    ]
                    tables.append(
                        {"table_id": table_id, "source_locator": locator, "raw_rows": raw_rows, "rows": rows, "confidence": confidence, "warnings": table_warnings}
                    )
                    content.append(
                        {"id": stable_id("node", document_id, locator, "table", occurrence), "type": "table", "source_locator": locator, "table_id": table_id}
                    )
                else:
                    extra: dict[str, Any] = {}
                    if block["type"] == "heading":
                        extra["level"] = block.get("level", 2)
                    if block["type"] == "list_item":
                        extra.update({"ordered": bool(re.match(r"^\s*\d+[.)]", block["text"])), "ordinal": 1})
                    content.append(
                        {
                            "id": stable_id("node", document_id, locator, block["type"], occurrence),
                            "type": block["type"],
                            "source_locator": locator,
                            **make_text_fields(block["text"], block["text"].strip(), mode, defer=True),
                            **extra,
                        }
                    )
        _merge_cross_page_paragraphs(content, mode)
        normalize_canonical_text(content, tables, mode)
        title = next((node["normalized_text"] for node in content if node["type"] == "heading" and node.get("level") == 1), Path(source).stem)
        return {
            "source_units": source_units,
            "content": content,
            "tables": tables,
            "assets": assets,
            "relationships": [],
            "warnings": warnings,
            "title": title,
            "adapter": {"name": self.name, "version": _version(), "limitations": list(self.limitations)},
        }


def _parse_number(value: str) -> int | float | None:
    candidate = value.strip().replace(",", "")
    negative = candidate.startswith("(") and candidate.endswith(")")
    if negative:
        candidate = candidate[1:-1]
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", candidate):
        return None
    number = float(candidate) * (-1 if negative else 1)
    return int(number) if number.is_integer() else number


def _merge_cross_page_paragraphs(content: list[dict[str, Any]], mode: str) -> None:
    index = 1
    while index < len(content):
        previous, current = content[index - 1], content[index]
        previous_page = previous.get("source_locator", {}).get("page")
        current_page = current.get("source_locator", {}).get("page")
        if (
            previous["type"] == current["type"] == "paragraph"
            and previous_page
            and current_page == previous_page + 1
            and previous.get("text")
            and current.get("text")
            and not SENTENCE_END_RE.search(previous["text"].rstrip())
            and (current["text"][0].islower() or "\u3400" <= current["text"][0] <= "\u9fff")
        ):
            raw = _join_lines([previous["raw_text"], current["raw_text"]])
            text = _join_lines([previous["text"], current["text"]])
            previous.update(make_text_fields(raw, text, mode, defer=True))
            previous["source_locator"]["continued_to_page"] = current_page
            content.pop(index)
            continue
        index += 1
