"""Native born-digital PDF adapter for the canonical v6 pipeline."""
from __future__ import annotations

import importlib.metadata
import ctypes
import difflib
import math
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from canonical import make_text_fields, normalize_canonical_text, sha256_file, stable_id
from ocr_provider import (
    NullOcrProvider,
    OcrPageResult,
    OcrProviderError,
    OcrSettings,
    OcrUnavailableError,
)


LIST_RE = re.compile(r"^\s*(?:[•◦▪■·]|[-*]|\([0-9A-Za-zivxlcdm]+\)|[0-9]+[.)、]|[一二三四五六七八九十]+[、.])\s*", re.I)
NUMERIC_RE = re.compile(r"(?:\(?-?\d[\d,.%]*\)?|[-–—])")
SENTENCE_END_RE = re.compile(r"[。！？：；;.!?:][\"'’”）)\]}】》〉]*$")
PHYSICAL_BREAK_RE = re.compile(r"(?:\r\n?|\n)+")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
TABLE_NOTE_RE = re.compile(r"^(?:sources?|notes?|figure|table|来源|資料|资料|注|备注)\s*[:：]", re.I)
TABLE_SUMMARY_RE = re.compile(r"^(?:grand\s+)?(?:sub)?total\b|^(?:合计|總計|总计|小计)\b", re.I)
WRAP_SUFFIX_RE = re.compile(
    r"^(?:zation|nization|tion|sion|ment|ness|ity|ous|ive|ize|ise|ing|ed|er|est|ally|ably|ibly|ship|ance|ence)",
    re.I,
)
WRAP_PREFIXES = {
    "anti", "co", "inter", "intra", "macro", "micro", "multi", "non", "over",
    "post", "pre", "re", "sub", "super", "trans", "under",
}
HEADING_CAPTION_RE = re.compile(
    r"^(?:fig(?:ure)?|table|chart|exhibit)\s*[A-Za-z0-9IVXLCDM.-]+\b|^(?:图|表)\s*[一二三四五六七八九十0-9]+",
    re.I,
)
TOC_LEADER_RE = re.compile(r"\.{3,}\s*\d+\s*$")
MARKER_ONLY_RE = re.compile(r"^\s*(?:\(?[0-9A-Za-zivxlcdm]+\)?[.)]?)\s*$", re.I)
BULLET_PREFIX_RE = re.compile(r"^\s*(?P<marker>[-*•‣▪◦])\s+(?P<body>\S.*)$")
PAREN_MARKER_RE = re.compile(r"^\s*\((?P<token>[0-9A-Za-z]+)\)\s+(?P<body>\S.*)$")
SUFFIX_MARKER_RE = re.compile(r"^\s*(?P<token>[0-9A-Za-z]+)(?P<delimiter>[.)])\s+(?P<body>\S.*)$")


def _alpha_ordinal(token: str) -> int | None:
    if not token.isalpha() or not token.isascii():
        return None
    value = 0
    for character in token.upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return value or None


def _roman_ordinal(token: str) -> int | None:
    candidate = token.upper()
    if not candidate or not re.fullmatch(r"M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})", candidate):
        return None
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for index, character in enumerate(candidate):
        value = values[character]
        total += -value if index + 1 < len(candidate) and value < values[candidate[index + 1]] else value
    return total or None


def _parse_list_marker(value: str) -> dict[str, Any] | None:
    bullet = BULLET_PREFIX_RE.match(value)
    if bullet:
        return {
            "marker": bullet.group("marker"),
            "body": bullet.group("body"),
            "delimiter": "bullet",
            "candidates": (("bullet", 1),),
        }
    match = PAREN_MARKER_RE.match(value)
    delimiter = "()"
    if match is None:
        match = SUFFIX_MARKER_RE.match(value)
        delimiter = match.group("delimiter") if match else ""
    if match is None:
        return None
    token = match.group("token")
    candidates: list[tuple[str, int]] = []
    if token.isdigit():
        ordinal = int(token)
        if ordinal > 0:
            candidates.append(("arabic", ordinal))
    else:
        alpha = _alpha_ordinal(token)
        roman = _roman_ordinal(token)
        if alpha is not None:
            candidates.append(("alpha", alpha))
        if roman is not None:
            candidates.append(("roman", roman))
    if not candidates:
        return None
    marker = value[: match.end("token")]
    if delimiter == "()":
        marker = value[: value.find(")") + 1]
    else:
        marker += delimiter
    return {
        "marker": marker.strip(),
        "body": match.group("body"),
        "delimiter": delimiter,
        "candidates": tuple(candidates),
    }


def _confirmed_list_markers(
    lines: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    parsed = [_parse_list_marker(str(line.get("text", "")).strip()) for line in lines]
    confirmed: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(parsed):
        if item is None:
            continue
        immediate = next(
            (
                (kind, ordinal)
                for kind, ordinal in item["candidates"]
                if kind in {"bullet", "arabic"}
            ),
            None,
        )
        if immediate is not None:
            kind, ordinal = immediate
            confirmed[index] = {
                **item,
                "kind": kind,
                "ordered": kind != "bullet",
                "ordinal": ordinal,
            }

    index = 0
    while index < len(lines):
        if parsed[index] is None or index in confirmed:
            index += 1
            continue
        run = [index]
        while run[-1] + 1 < len(lines):
            previous_index, next_index = run[-1], run[-1] + 1
            previous_item, next_item = parsed[previous_index], parsed[next_index]
            if previous_item is None or next_item is None or next_index in confirmed:
                break
            if previous_item["delimiter"] != next_item["delimiter"]:
                break
            previous_line, next_line = lines[previous_index], lines[next_index]
            if abs(_item_box(previous_line)[0] - _item_box(next_line)[0]) > 8.0:
                break
            if previous_line.get("_layout_column", "single") != next_line.get("_layout_column", "single"):
                break
            run.append(next_index)
        minimum = 2 if parsed[run[0]]["delimiter"] in {"()", ")"} else 3
        if len(run) >= minimum:
            for kind in ("alpha", "roman"):
                ordinals: list[int] = []
                for run_index in run:
                    ordinal = next(
                        (
                            value
                            for candidate_kind, value in parsed[run_index]["candidates"]
                            if candidate_kind == kind
                        ),
                        None,
                    )
                    if ordinal is None:
                        ordinals = []
                        break
                    ordinals.append(ordinal)
                if ordinals and all(
                    right == left + 1 for left, right in zip(ordinals, ordinals[1:])
                ):
                    for run_index, ordinal in zip(run, ordinals, strict=True):
                        confirmed[run_index] = {
                            **parsed[run_index],
                            "kind": kind,
                            "ordered": True,
                            "ordinal": ordinal,
                        }
                    break
        index = run[-1] + 1
    return confirmed


def _heading_blocked(value: str) -> bool:
    candidate = value.strip()
    return bool(
        not candidate
        or HEADING_CAPTION_RE.match(candidate)
        or TOC_LEADER_RE.search(candidate)
        or TABLE_NOTE_RE.match(candidate)
        or MARKER_ONLY_RE.fullmatch(candidate)
    )


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


def transform_point_for_orientation(
    point: tuple[float, float], width: float, height: float, angle: int
) -> tuple[float, float]:
    x, y = point
    angle %= 360
    if angle == 90:
        transformed = (y, width - x)
    elif angle == 180:
        transformed = (width - x, height - y)
    elif angle == 270:
        transformed = (height - y, x)
    else:
        transformed = (x, y)
    return round(float(transformed[0]), 3), round(float(transformed[1]), 3)


def _compose_page_object_matrix(obj: Any) -> Any:
    """Compose a page object's local matrix through nested Form containers."""

    matrix = obj.get_matrix()
    container = getattr(obj, "container", None)
    depth = 0
    while container is not None and depth < 32:
        matrix = matrix.multiply(container.get_matrix())
        container = getattr(container, "container", None)
        depth += 1
    return matrix


def _extract_path_vector_geometry(
    obj: Any, object_index: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract conservative straight vector rules from one PDF path object."""

    raw = _pdfium().raw
    fill_mode = ctypes.c_int()
    stroke = ctypes.c_int()
    if not raw.FPDFPath_GetDrawMode(
        obj.raw, ctypes.byref(fill_mode), ctypes.byref(stroke)
    ):
        return [], []
    stroke_visible = bool(stroke.value)
    fill_visible = bool(fill_mode.value)
    if not stroke_visible and not fill_visible:
        return [], []
    stroke_width_value = ctypes.c_float()
    if raw.FPDFPageObj_GetStrokeWidth(obj.raw, ctypes.byref(stroke_width_value)):
        stroke_width = max(float(stroke_width_value.value), 0.0)
    else:
        stroke_width = 1.0
    try:
        matrix = _compose_page_object_matrix(obj)
        segment_count = int(raw.FPDFPath_CountSegments(obj.raw))
    except Exception:
        return [], []
    if segment_count <= 0 or segment_count > 20000:
        return [], []

    rules: list[dict[str, Any]] = []
    obstacles: list[dict[str, Any]] = []
    path_points: list[tuple[float, float]] = []
    previous: tuple[float, float] | None = None
    subpath_start: tuple[float, float] | None = None
    for segment_index in range(segment_count):
        segment = raw.FPDFPath_GetPathSegment(obj.raw, segment_index)
        if not segment:
            continue
        x_value = ctypes.c_float()
        y_value = ctypes.c_float()
        if not raw.FPDFPathSegment_GetPoint(
            segment, ctypes.byref(x_value), ctypes.byref(y_value)
        ):
            continue
        point = tuple(
            round(float(value), 3)
            for value in matrix.on_point(float(x_value.value), float(y_value.value))
        )
        path_points.append(point)
        segment_type = int(raw.FPDFPathSegment_GetType(segment))
        if segment_type == raw.FPDF_SEGMENT_MOVETO:
            previous = point
            subpath_start = point
            continue
        if previous is None:
            previous = point
            subpath_start = point
            continue
        pairs: list[tuple[tuple[float, float], tuple[float, float]]] = []
        if segment_type == raw.FPDF_SEGMENT_LINETO:
            pairs.append((previous, point))
        else:
            box = bbox_union(
                [[previous[0], previous[1], previous[0], previous[1]], [point[0], point[1], point[0], point[1]]]
            )
            obstacles.append(
                {"bbox": box, "reason": "bezier_or_complex_path", "object_index": object_index}
            )
        if bool(raw.FPDFPathSegment_GetClose(segment)) and subpath_start is not None:
            pairs.append((point, subpath_start))
        for start, end in pairs:
            if not stroke_visible:
                continue
            delta_x = end[0] - start[0]
            delta_y = end[1] - start[1]
            length = math.hypot(delta_x, delta_y)
            axis_tolerance = max(0.75, length * 0.01)
            if length < 18.0 or stroke_width > 4.0:
                continue
            if abs(delta_y) <= axis_tolerance:
                orientation = "horizontal"
            elif abs(delta_x) <= axis_tolerance:
                orientation = "vertical"
            else:
                obstacles.append(
                    {
                        "bbox": [
                            round(min(start[0], end[0]), 3),
                            round(min(start[1], end[1]), 3),
                            round(max(start[0], end[0]), 3),
                            round(max(start[1], end[1]), 3),
                        ],
                        "reason": "diagonal_path",
                        "object_index": object_index,
                    }
                )
                continue
            rules.append(
                {
                    "source_start": start,
                    "source_end": end,
                    "orientation": orientation,
                    "stroke_width": round(stroke_width, 3),
                    "object_index": object_index,
                }
            )
        previous = point
    if fill_visible and not stroke_visible and path_points:
        left = min(point[0] for point in path_points)
        bottom = min(point[1] for point in path_points)
        right = max(point[0] for point in path_points)
        top = max(point[1] for point in path_points)
        width, height = right - left, top - bottom
        long_edge, short_edge = max(width, height), min(width, height)
        if long_edge >= 18.0 and short_edge <= 4.0 and long_edge / max(short_edge, 0.1) >= 12.0:
            if width >= height:
                start = (round(left, 3), round((bottom + top) / 2, 3))
                end = (round(right, 3), round((bottom + top) / 2, 3))
                orientation = "horizontal"
            else:
                start = (round((left + right) / 2, 3), round(bottom, 3))
                end = (round((left + right) / 2, 3), round(top, 3))
                orientation = "vertical"
            rules.append(
                {
                    "source_start": start,
                    "source_end": end,
                    "orientation": orientation,
                    "stroke_width": round(max(short_edge, 0.1), 3),
                    "object_index": object_index,
                    "synthetic_fill": True,
                }
            )
        elif width > 4.0 and height > 4.0:
            obstacles.append(
                {
                    "bbox": [round(left, 3), round(bottom, 3), round(right, 3), round(top, 3)],
                    "reason": "complex_fill",
                    "object_index": object_index,
                }
            )
    return rules, obstacles


def _normalize_vector_geometry(
    rules: list[dict[str, Any]],
    obstacles: list[dict[str, Any]],
    width: float,
    height: float,
    angle: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized_rules: list[dict[str, Any]] = []
    for rule in rules:
        start = transform_point_for_orientation(rule["source_start"], width, height, angle)
        end = transform_point_for_orientation(rule["source_end"], width, height, angle)
        delta_x = end[0] - start[0]
        delta_y = end[1] - start[1]
        normalized_rules.append(
            {
                **rule,
                "layout_start": start,
                "layout_end": end,
                "orientation": "horizontal" if abs(delta_x) >= abs(delta_y) else "vertical",
            }
        )
    normalized_obstacles: list[dict[str, Any]] = []
    for obstacle in obstacles:
        layout_bbox, _, _ = transform_bbox_for_orientation(
            obstacle["bbox"], width, height, angle
        )
        normalized_obstacles.append({**obstacle, "layout_bbox": layout_bbox})
    return normalized_rules, normalized_obstacles


def _item_box(item: dict[str, Any]) -> list[float]:
    table_meta = item.get("_table_meta")
    if isinstance(table_meta, dict) and table_meta.get("layout_bbox"):
        return table_meta["layout_bbox"]
    return item.get("layout_bbox") or item["bbox"]


def _text_angle(matrix) -> int:
    angle = math.degrees(math.atan2(matrix.b, matrix.a)) % 360
    snapped = int(round(angle / 90.0) * 90) % 360
    distance = abs(((angle - snapped + 180) % 360) - 180)
    return snapped if distance <= 12 else int(round(angle))


def _font_size(matrix, nominal_size: float) -> float:
    return round(max(math.hypot(matrix.a, matrix.b), math.hypot(matrix.c, matrix.d)) * nominal_size, 3)


def _font_weight(font: Any) -> int:
    try:
        weight = int(font.get_weight())
    except Exception:
        weight = 0
    if 100 <= weight <= 1000:
        return weight
    try:
        name = str(font.get_base_name())
    except Exception:
        name = ""
    return 700 if re.search(r"(?:bold|black|heavy|demi|semibold)", name, re.I) else 400


def _font_size_bucket(value: float) -> float:
    return round(float(value) * 4.0) / 4.0


def _native_text_key(value: str) -> str:
    """Normalize only layout-insignificant whitespace for paint-layer checks."""

    return re.sub(r"\s+", " ", str(value)).strip()


def _native_fragments_duplicate(
    first: dict[str, Any], second: dict[str, Any]
) -> bool:
    """Return True only for effectively identical native paint layers.

    The tolerance is intentionally sub-point.  A visible shadow or outline
    offset must survive even when its text matches the foreground layer.
    """

    if tuple(first.get("_container_context", ())) != tuple(
        second.get("_container_context", ())
    ):
        return False
    if _native_text_key(first.get("text", "")) != _native_text_key(second.get("text", "")):
        return False
    if int(first.get("text_angle", 0)) % 360 != int(second.get("text_angle", 0)) % 360:
        return False
    first_box = [float(value) for value in first.get("bbox", [])]
    second_box = [float(value) for value in second.get("bbox", [])]
    if len(first_box) != 4 or len(second_box) != 4:
        return False
    tolerance = max(
        0.35,
        min(
            float(first.get("font_size", 0.0) or 0.0),
            float(second.get("font_size", 0.0) or 0.0),
        )
        * 0.025,
    )
    if max(abs(left - right) for left, right in zip(first_box, second_box, strict=True)) > tolerance:
        return False
    first_size = float(first.get("font_size", 0.0) or 0.0)
    second_size = float(second.get("font_size", 0.0) or 0.0)
    if abs(first_size - second_size) > max(0.25, min(first_size, second_size) * 0.03):
        return False
    first_weight = int(first.get("font_weight", 400) or 400)
    second_weight = int(second.get("font_weight", 400) or 400)
    return (first_weight >= 600) == (second_weight >= 600)


def _deduplicate_native_fragments(
    fragments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Drop later exact native paint layers while preserving source order."""

    retained: list[dict[str, Any]] = []
    position_cell = 4.0
    candidates_by_text_angle_position: dict[
        tuple[str, int, tuple[Any, ...], int, int], list[dict[str, Any]]
    ] = {}
    dropped = 0
    for fragment in fragments:
        text_key = _native_text_key(fragment.get("text", ""))
        angle_key = int(fragment.get("text_angle", 0)) % 360
        container_key = tuple(fragment.get("_container_context", ()))
        box = [float(value) for value in fragment.get("bbox", [])]
        if len(box) == 4:
            center_x = (box[0] + box[2]) / 2
            center_y = (box[1] + box[3]) / 2
            x_cell = math.floor(center_x / position_cell)
            y_cell = math.floor(center_y / position_cell)
            position_tolerance = max(
                0.35, float(fragment.get("font_size", 0.0) or 0.0) * 0.025
            )
            search_radius = max(1, math.ceil(position_tolerance / position_cell))
            candidates = [
                existing
                for x_offset in range(-search_radius, search_radius + 1)
                for y_offset in range(-search_radius, search_radius + 1)
                for existing in candidates_by_text_angle_position.get(
                    (
                        text_key,
                        angle_key,
                        container_key,
                        x_cell + x_offset,
                        y_cell + y_offset,
                    ),
                    (),
                )
            ]
        else:
            x_cell = y_cell = 0
            candidates = candidates_by_text_angle_position.get(
                (text_key, angle_key, container_key, x_cell, y_cell), []
            )
        if any(_native_fragments_duplicate(existing, fragment) for existing in candidates):
            dropped += 1
            continue
        retained.append(fragment)
        candidates_by_text_angle_position.setdefault(
            (text_key, angle_key, container_key, x_cell, y_cell), []
        ).append(fragment)
    return retained, dropped


def _dominant_font_size(fragments: list[dict[str, Any]]) -> float:
    weights: Counter[float] = Counter()
    for fragment in fragments:
        weight = len(re.sub(r"\s+", "", str(fragment.get("text", ""))))
        if weight:
            weights[_font_size_bucket(float(fragment.get("font_size", 0.0)))] += weight
    if not weights:
        return 0.0
    return max(weights, key=lambda size: (weights[size], size))


def _dominant_font_weight(fragments: list[dict[str, Any]]) -> int:
    weights: Counter[int] = Counter()
    for fragment in fragments:
        character_count = len(re.sub(r"\s+", "", str(fragment.get("text", ""))))
        if character_count:
            weights[int(fragment.get("font_weight", 400) or 400)] += character_count
    return max(weights, key=lambda weight: (weights[weight], weight)) if weights else 400


def _dominant_line_height(fragments: list[dict[str, Any]]) -> float:
    weights: Counter[float] = Counter()
    for fragment in fragments:
        if not fragment.get("_layout_horizontal", True):
            continue
        weight = len(re.sub(r"\s+", "", str(fragment.get("text", ""))))
        box = _item_box(fragment)
        height = max(float(box[3]) - float(box[1]), 0.0)
        if weight and height > 0:
            weights[round(height * 2.0) / 2.0] += weight
    if not weights:
        return 0.0
    return max(weights, key=lambda height: (weights[height], height))


def _document_body_size(page_lines: list[list[dict[str, Any]]]) -> float:
    weights: Counter[float] = Counter()
    for lines in page_lines:
        for line in lines:
            char_count = int(line.get("char_count") or len(re.sub(r"\s+", "", str(line.get("text", "")))))
            size = float(line.get("dominant_font_size") or line.get("font_size") or 0.0)
            if char_count >= 4 and size > 0:
                weights[_font_size_bucket(size)] += char_count
    if not weights:
        return 12.0
    midpoint = sum(weights.values()) / 2
    cumulative = 0
    for size in sorted(weights):
        cumulative += weights[size]
        if cumulative >= midpoint:
            return size
    return min(weights)


def _document_body_height(page_lines: list[list[dict[str, Any]]]) -> float:
    weighted: list[tuple[float, int]] = []
    for lines in page_lines:
        for line in lines:
            char_count = int(line.get("char_count") or len(re.sub(r"\s+", "", str(line.get("text", "")))))
            height = float(line.get("dominant_line_height") or 0.0)
            if char_count >= 4 and height > 0:
                weighted.append((round(height * 2.0) / 2.0, char_count))
    if not weighted:
        return 12.0
    midpoint = sum(weight for _, weight in weighted) / 2
    cumulative = 0
    for height, weight in sorted(weighted):
        cumulative += weight
        if cumulative >= midpoint:
            return height
    return min(height for height, _ in weighted)


def _document_has_heading_size_signal(
    page_lines: list[list[dict[str, Any]]], body_size: float
) -> bool:
    threshold = max(float(body_size), 0.1) * 1.12
    for lines in page_lines:
        for line in lines:
            value = str(line.get("text", "")).strip()
            size = float(line.get("dominant_font_size") or line.get("font_size") or 0.0)
            if (
                line.get("_layout_horizontal", True)
                and 4 <= len(re.sub(r"\s+", "", value)) <= 100
                and size >= threshold
                and not SENTENCE_END_RE.search(value)
            ):
                return True
    return False


def _merge_fragments(fragments: list[dict[str, Any]]) -> str:
    fragments = sorted(fragments, key=lambda item: _item_box(item)[0])
    output = ""
    previous: dict[str, Any] | None = None
    for fragment in fragments:
        value = str(fragment["text"])
        if PHYSICAL_BREAK_RE.search(value):
            value = _join_lines([value])
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


def _raw_pointer_key(raw: Any) -> int:
    return int(ctypes.cast(raw, ctypes.c_void_p).value or 0)


def _container_chain_identity(obj: Any) -> tuple[int, ...]:
    """Return an extraction-local identity for the object's container chain.

    PDFium may expose identical text and geometry from separate nested Form
    contexts.  Pointer values are used only during this extraction pass and are
    removed before Canonical publication, so they cannot affect stable output.
    """

    identity: list[int] = []
    container = getattr(obj, "container", None)
    depth = 0
    while container is not None and depth < 32:
        raw = getattr(container, "raw", None)
        if raw is not None:
            identity.append(_raw_pointer_key(raw))
        container = getattr(container, "container", None)
        depth += 1
    return tuple(identity)


def _needs_character_geometry(fragment: dict[str, Any]) -> bool:
    value = str(fragment.get("text", ""))
    words = re.findall(r"\S+", value)
    nonspace = len(re.sub(r"\s+", "", value))
    box = fragment["bbox"]
    width = max(float(box[2]) - float(box[0]), 0.0)
    font_size = max(float(fragment.get("font_size", 0.0)), 0.1)
    return bool(
        "\x02" in value
        or "\ufffe" in value
        or (len(words) >= 2 and nonspace and width / nonspace >= font_size * 0.85)
        or bool(PHYSICAL_BREAK_RE.search(value))
    )


def _collect_text_object_characters(textpage: Any) -> dict[int, list[dict[str, Any]]]:
    by_object: dict[int, list[dict[str, Any]]] = {}
    pdfium_raw = _pdfium().raw
    for index in range(textpage.count_chars()):
        text_object = textpage.get_textobj(index)
        if text_object is None:
            continue
        codepoint = int(pdfium_raw.FPDFText_GetUnicode(textpage, index))
        if not 0 < codepoint <= 0x10FFFF:
            continue
        value = "\x02" if codepoint == 0xFFFE else chr(codepoint)
        if value in "\r\n":
            continue
        try:
            box = [round(float(item), 3) for item in textpage.get_charbox(index)]
        except Exception:
            continue
        by_object.setdefault(_raw_pointer_key(text_object.raw), []).append(
            {"text": value, "bbox": box}
        )
    return by_object


def _is_hyphen_like_glyph(box: list[float], font_size: float) -> bool:
    width = max(float(box[2]) - float(box[0]), 0.0)
    height = max(float(box[3]) - float(box[1]), 0.0)
    size = max(float(font_size), 0.1)
    return bool(
        size * 0.08 <= width <= size * 0.8
        and height <= max(size * 0.25, width * 0.5)
        and width >= height * 1.5
    )


def _resolve_unmapped_glyphs(
    fragment: dict[str, Any], characters: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    value = str(fragment.get("text", ""))
    if "\x02" not in value and "\ufffe" not in value:
        return fragment, characters, 0
    marker_characters = [
        character for character in characters if character["text"] in {"\x02", "\ufffe"}
    ]
    replacements = [
        "-" if _is_hyphen_like_glyph(character["bbox"], fragment.get("font_size", 0.0)) else "\ufffd"
        for character in marker_characters
    ]
    marker_count = value.count("\x02") + value.count("\ufffe")
    if len(replacements) != marker_count:
        replacements = ["\ufffd"] * marker_count
    replacement_iter = iter(replacements)
    resolved_value = "".join(
        next(replacement_iter) if character in {"\x02", "\ufffe"} else character
        for character in value
    )
    character_replacements = iter(replacements)
    resolved_characters: list[dict[str, Any]] = []
    for character in characters:
        resolved = dict(character)
        if resolved["text"] in {"\x02", "\ufffe"}:
            resolved["text"] = next(character_replacements, "\ufffd")
        resolved_characters.append(resolved)
    resolved_fragment = dict(fragment)
    resolved_fragment["text"] = resolved_value
    return resolved_fragment, resolved_characters, replacements.count("\ufffd")


def _fragments_from_character_geometry(
    fragment: dict[str, Any], characters: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    normalize = lambda value: re.sub(r"\s+", " ", value).strip()
    reconstructed = "".join(character["text"] for character in characters)
    if normalize(reconstructed) != normalize(str(fragment.get("text", ""))):
        return [fragment]

    tokens: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal current
        if current:
            tokens.append(current)
            current = []

    for character in characters:
        value = str(character["text"])
        if value.isspace():
            flush()
            continue
        if current:
            previous_box = current[-1]["bbox"]
            box = character["bbox"]
            overlap = max(0.0, min(previous_box[3], box[3]) - max(previous_box[1], box[1]))
            smaller_height = max(min(previous_box[3] - previous_box[1], box[3] - box[1]), 0.1)
            previous_widths = [max(item["bbox"][2] - item["bbox"][0], 0.1) for item in current]
            gap = box[0] - previous_box[2]
            if overlap / smaller_height < 0.35 or gap > max(
                float(fragment.get("font_size", 0.0)) * 1.2,
                statistics.median(previous_widths) * 3.0,
            ):
                flush()
        current.append(character)
    flush()

    refined: list[dict[str, Any]] = []
    internal_metadata = {
        key: fragment[key]
        for key in ("_object_key", "_object_index", "_container_context")
        if key in fragment
    }
    for token in tokens:
        value = "".join(item["text"] for item in token)
        if not value:
            continue
        box = bbox_union(item["bbox"] for item in token)
        glyph_widths = [
            item["bbox"][2] - item["bbox"][0]
            for item in token
            if item["bbox"][2] - item["bbox"][0] > 0.1
        ]
        refined.append(
            {
                "text": value,
                "bbox": box,
                "text_angle": fragment["text_angle"],
                "font_size": fragment["font_size"],
                "font_weight": fragment["font_weight"],
                "char_width": max(statistics.median(glyph_widths or [0.1]), 0.1),
                **internal_metadata,
            }
        )
    return refined or [fragment]


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


def _ocr_item_metadata(items: list[dict[str, Any]]) -> dict[str, Any]:
    ocr_items = [
        item
        for item in items
        if item.get("_source_method") == "ocr"
        or item.get("_extraction_method") in {"ocr", "native+ocr"}
    ]
    if not ocr_items:
        return {}
    methods = {
        item.get("_source_method") or item.get("_extraction_method", "native")
        for item in items
    }
    providers = sorted(
        {str(item["_ocr_provider"]) for item in ocr_items if item.get("_ocr_provider")}
    )
    versions = sorted(
        {str(item["_ocr_version"]) for item in ocr_items if item.get("_ocr_version")}
    )
    confidences = [
        float(item["_ocr_confidence"])
        for item in ocr_items
        if item.get("_ocr_confidence") is not None
    ]
    metadata: dict[str, Any] = {
        "_extraction_method": "ocr" if methods == {"ocr"} else "native+ocr"
    }
    if providers:
        metadata["_ocr_provider"] = "+".join(providers)
    if versions:
        metadata["_ocr_version"] = "+".join(versions)
    if confidences:
        metadata["_ocr_confidence"] = round(min(confidences), 6)
    return metadata


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
            union = group["layout_bbox"]
            overlap = max(0.0, min(box[3], union[3]) - max(box[1], union[1]))
            if abs(center - group["center"]) <= max(2.0, min(height, group["height"]) * 0.42) or overlap / max(min(height, group["height"]), 0.1) >= 0.5:
                selected = group
                break
        if selected is None:
            groups.append(
                {
                    "center": center,
                    "height": height,
                    "layout_bbox": list(box),
                    "fragments": [fragment],
                }
            )
        else:
            selected["fragments"].append(fragment)
            union = bbox_union([selected["layout_bbox"], box])
            selected["layout_bbox"] = union
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
                    "dominant_font_size": _dominant_font_size(values),
                    "dominant_line_height": _dominant_line_height(values),
                    "char_count": len(re.sub(r"\s+", "", text)),
                    "font_weight": _dominant_font_weight(values),
                    "_layout_horizontal": sum(
                        len(re.sub(r"\s+", "", str(value.get("text", ""))))
                        for value in values if value.get("_layout_horizontal", True)
                    ) >= max(1, len(re.sub(r"\s+", "", text)) / 2),
                    "cells": _line_cells(values),
                    **_ocr_item_metadata(values),
                }
            )
    return sorted(lines, key=lambda item: (-_item_box(item)[3], _item_box(item)[0]))


def _line_from_cell(line: dict[str, Any], cell: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": cell["text"],
        "bbox": cell["bbox"],
        "layout_bbox": cell["layout_bbox"],
        "font_size": line["font_size"],
        "dominant_font_size": line.get("dominant_font_size", line["font_size"]),
        "dominant_line_height": line.get("dominant_line_height", 0.0),
        "char_count": len(re.sub(r"\s+", "", cell["text"])),
        "font_weight": line["font_weight"],
        "_layout_horizontal": line.get("_layout_horizontal", True),
        "cells": [cell],
        "_split_from_two_columns": True,
        **{
            key: line[key]
            for key in (
                "_extraction_method", "_ocr_provider", "_ocr_version", "_ocr_confidence"
            )
            if key in line
        },
    }


def _expand_parallel_prose_lines(
    lines: list[dict[str, Any]], page_width: float
) -> list[dict[str, Any]]:
    """Split aligned 3+ column prose before word-grid table inference.

    This is deliberately limited to repeated, sentence-like rows with no
    numeric table signal.  Ambiguous short-field grids continue to the table
    detector unchanged.
    """

    by_count: dict[int, list[int]] = {}
    for index, line in enumerate(lines):
        count = len(line.get("cells", []))
        if (
            3 <= count <= 6
            and not line.get("_table_meta")
            and not line.get("_canonical_type")
        ):
            by_count.setdefault(count, []).append(index)
    split_indexes: set[int] = set()
    for count, indexes in by_count.items():
        if len(indexes) < 3:
            continue
        candidate_lines = [lines[index] for index in indexes]
        cells = [cell for line in candidate_lines for cell in line.get("cells", [])]
        if any(_is_numeric_cell(str(cell.get("text", ""))) for cell in cells):
            continue
        prose_like = sum(
            len(re.sub(r"\s+", "", str(cell.get("text", "")))) >= 12
            and bool(SENTENCE_END_RE.search(str(cell.get("text", "")).strip()))
            for cell in cells
        )
        if prose_like / max(len(cells), 1) < 0.70:
            continue
        stable = True
        for column in range(count):
            starts = [line["cells"][column]["layout_bbox"][0] for line in candidate_lines]
            if max(starts) - min(starts) > max(8.0, page_width * 0.015):
                stable = False
                break
        if not stable:
            continue
        minimum_gap = min(
            line["cells"][column + 1]["layout_bbox"][0]
            - line["cells"][column]["layout_bbox"][2]
            for line in candidate_lines
            for column in range(count - 1)
        )
        if minimum_gap < max(12.0, page_width * 0.02):
            continue
        split_indexes.update(indexes)
    if not split_indexes:
        return lines
    expanded: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if index not in split_indexes:
            expanded.append(line)
            continue
        for column, cell in enumerate(line["cells"]):
            split = _line_from_cell(line, cell)
            split["_split_from_parallel_columns"] = True
            split["_parallel_column_index"] = column
            expanded.append(split)
    return expanded


def _score_two_column_gutter(lines: list[dict[str, Any]], width: float) -> dict[str, Any] | None:
    eligible = [
        line
        for line in lines
        if not line.get("_table_meta")
        and not line.get("_canonical_type")
        and len(re.sub(r"\s+", "", line.get("text", ""))) >= 2
        and _item_box(line)[2] - _item_box(line)[0] >= 6
    ]
    if len(eligible) < 6 or not math.isfinite(width) or width <= 0:
        return None
    tolerance = max(4.0, width * 0.008)
    minimum_side = max(3, math.ceil(len(eligible) * 0.35))
    best: dict[str, Any] | None = None
    for index in range(61):
        gutter = width * (0.35 + 0.30 * index / 60)
        left = [line for line in eligible if _item_box(line)[2] <= gutter - tolerance]
        right = [line for line in eligible if _item_box(line)[0] >= gutter + tolerance]
        crossing = len(eligible) - len(left) - len(right)
        if len(left) < minimum_side or len(right) < minimum_side:
            continue
        crossing_ratio = crossing / len(eligible)
        balance = min(len(left), len(right)) / max(len(left), len(right))
        left_top = max(_item_box(line)[3] for line in left)
        left_bottom = min(_item_box(line)[1] for line in left)
        right_top = max(_item_box(line)[3] for line in right)
        right_bottom = min(_item_box(line)[1] for line in right)
        overlap = max(0.0, min(left_top, right_top) - max(left_bottom, right_bottom))
        span = max(min(left_top - left_bottom, right_top - right_bottom), 0.1)
        vertical_overlap = min(1.0, overlap / span)
        confidence = 0.50 * (1.0 - crossing_ratio) + 0.25 * balance + 0.25 * vertical_overlap
        candidate = {
            "mode": "two_column",
            "gutter_x": gutter,
            "tolerance": tolerance,
            "confidence": confidence,
            "crossing_ratio": crossing_ratio,
            "left_count": len(left),
            "right_count": len(right),
        }
        if crossing_ratio <= 0.22 and balance >= 0.35 and vertical_overlap >= 0.35 and confidence >= 0.72:
            if best is None or candidate["confidence"] > best["confidence"]:
                best = candidate
    return best


def _order_two_column_regions(
    lines: list[dict[str, Any]], gutter_x: float, tolerance: float, page_width: float
) -> list[dict[str, Any]]:
    left: list[dict[str, Any]] = []
    right: list[dict[str, Any]] = []
    spanning: list[dict[str, Any]] = []
    for line in lines:
        box = _item_box(line)
        center = (box[0] + box[2]) / 2
        visually_centered = (
            not line.get("_split_from_two_columns")
            and abs(center - page_width / 2) <= page_width * 0.08
        )
        if visually_centered and box[2] - box[0] <= page_width * 0.60:
            line["_layout_column"] = "spanning"
            spanning.append(line)
        elif box[2] <= gutter_x + tolerance:
            line["_layout_column"] = "left"
            left.append(line)
        elif box[0] >= gutter_x - tolerance:
            line["_layout_column"] = "right"
            right.append(line)
        else:
            line["_layout_column"] = "spanning"
            spanning.append(line)

    pending = left + right
    ordered: list[dict[str, Any]] = []
    band_id = 0

    def append_band(values: list[dict[str, Any]]) -> None:
        nonlocal band_id
        left_band = sorted(
            (line for line in values if line["_layout_column"] == "left"),
            key=lambda item: (-_item_box(item)[3], _item_box(item)[0]),
        )
        right_band = sorted(
            (line for line in values if line["_layout_column"] == "right"),
            key=lambda item: (-_item_box(item)[3], _item_box(item)[0]),
        )
        if not left_band and not right_band:
            return
        band_id += 1
        for line in left_band + right_band:
            line["_layout_band"] = band_id
        if left_band and right_band:
            left_band[-1]["_column_flow_tail"] = True
            right_band[0]["_column_flow_head"] = True
        ordered.extend(left_band)
        ordered.extend(right_band)

    for anchor in sorted(spanning, key=lambda item: (-_item_box(item)[3], _item_box(item)[0])):
        anchor_center = (_item_box(anchor)[1] + _item_box(anchor)[3]) / 2
        above = [line for line in pending if (_item_box(line)[1] + _item_box(line)[3]) / 2 > anchor_center]
        append_band(above)
        above_ids = {id(line) for line in above}
        pending = [line for line in pending if id(line) not in above_ids]
        band_id += 1
        anchor["_layout_band"] = band_id
        ordered.append(anchor)
    append_band(pending)
    return ordered


def _score_recursive_vertical_cut(
    lines: list[dict[str, Any]], region_left: float, region_right: float
) -> dict[str, Any] | None:
    eligible = [
        line
        for line in lines
        if not line.get("_table_meta")
        and not line.get("_canonical_type")
        and len(re.sub(r"\s+", "", str(line.get("text", "")))) >= 2
        and _item_box(line)[2] - _item_box(line)[0] >= 6.0
    ]
    region_width = region_right - region_left
    if len(eligible) < 6 or region_width <= 40.0:
        return None
    tolerance = max(3.0, region_width * 0.01)
    minimum_side = max(3, math.ceil(len(eligible) * 0.24))
    best: dict[str, Any] | None = None
    for index in range(81):
        gutter = region_left + region_width * (0.15 + 0.70 * index / 80)
        left = [line for line in eligible if _item_box(line)[2] <= gutter - tolerance]
        right = [line for line in eligible if _item_box(line)[0] >= gutter + tolerance]
        crossing = len(eligible) - len(left) - len(right)
        if len(left) < minimum_side or len(right) < minimum_side:
            continue
        crossing_ratio = crossing / len(eligible)
        balance = min(len(left), len(right)) / max(len(left), len(right))
        left_top = max(_item_box(line)[3] for line in left)
        left_bottom = min(_item_box(line)[1] for line in left)
        right_top = max(_item_box(line)[3] for line in right)
        right_bottom = min(_item_box(line)[1] for line in right)
        overlap = max(0.0, min(left_top, right_top) - max(left_bottom, right_bottom))
        span = max(min(left_top - left_bottom, right_top - right_bottom), 0.1)
        vertical_overlap = min(1.0, overlap / span)
        confidence = 0.50 * (1.0 - crossing_ratio) + 0.25 * balance + 0.25 * vertical_overlap
        if (
            crossing_ratio > 0.18
            or balance < 0.35
            or vertical_overlap < 0.30
            or confidence < 0.72
        ):
            continue
        candidate = {
            "gutter_x": round(gutter, 3),
            "tolerance": round(tolerance, 3),
            "confidence": round(confidence, 6),
            "crossing_ratio": round(crossing_ratio, 6),
            "left_count": len(left),
            "right_count": len(right),
        }
        if best is None or (
            candidate["confidence"],
            -candidate["crossing_ratio"],
            -candidate["gutter_x"],
        ) > (
            best["confidence"],
            -best["crossing_ratio"],
            -best["gutter_x"],
        ):
            best = candidate
    return best


def _recursive_order_columns(
    lines: list[dict[str, Any]], page_width: float
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    cuts: list[dict[str, Any]] = []
    band_counter = [0]

    def physical(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(values, key=lambda item: (-_item_box(item)[3], _item_box(item)[0]))

    def recurse(
        values: list[dict[str, Any]],
        region_left: float,
        region_right: float,
        path: tuple[int, ...],
        depth: int,
    ) -> list[dict[str, Any]]:
        if not values:
            return []
        if depth >= 8:
            for line in values:
                line["_layout_path"] = path
            return physical(values)
        typography = [
            float(line.get("dominant_font_size") or line.get("font_size") or 0.0)
            for line in values
            if not line.get("_table_meta") and not line.get("_canonical_type")
        ]
        median_size = statistics.median([value for value in typography if value > 0] or [12.0])
        forced_anchors = [
            line
            for line in values
            if not line.get("_table_meta")
            and not line.get("_canonical_type")
            and not line.get("_split_from_two_columns")
            and not line.get("_split_from_parallel_columns")
            and len(str(line.get("text", "")).strip()) <= 120
            and abs(((_item_box(line)[0] + _item_box(line)[2]) / 2) - page_width / 2)
            <= page_width * 0.08
            and _item_box(line)[2] - _item_box(line)[0] <= page_width * 0.65
            and float(line.get("dominant_font_size") or line.get("font_size") or 0.0)
            >= median_size * 1.05
        ]
        forced_ids = {id(line) for line in forced_anchors}
        cut = _score_recursive_vertical_cut(
            [line for line in values if id(line) not in forced_ids],
            region_left,
            region_right,
        )
        if cut is None:
            for line in values:
                line["_layout_path"] = path
            return physical(values)
        cuts.append({**cut, "depth": depth, "path": list(path)})
        gutter, tolerance = cut["gutter_x"], cut["tolerance"]
        left = [
            line
            for line in values
            if id(line) not in forced_ids and _item_box(line)[2] <= gutter - tolerance
        ]
        right = [
            line
            for line in values
            if id(line) not in forced_ids and _item_box(line)[0] >= gutter + tolerance
        ]
        partitioned = {id(line) for line in left + right}
        anchors = [line for line in values if id(line) not in partitioned]
        if anchors:
            pending = left + right
            ordered: list[dict[str, Any]] = []
            for anchor in physical(anchors):
                anchor_center = (_item_box(anchor)[1] + _item_box(anchor)[3]) / 2
                above = [
                    line
                    for line in pending
                    if (_item_box(line)[1] + _item_box(line)[3]) / 2 > anchor_center
                ]
                ordered.extend(recurse(above, region_left, region_right, path, depth + 1))
                above_ids = {id(line) for line in above}
                pending = [line for line in pending if id(line) not in above_ids]
                anchor["_layout_path"] = None
                anchor["_layout_column"] = "spanning"
                ordered.append(anchor)
            ordered.extend(recurse(pending, region_left, region_right, path, depth + 1))
            return ordered
        left_order = recurse(
            left, region_left, gutter - tolerance, path + (0,), depth + 1
        )
        right_order = recurse(
            right, gutter + tolerance, region_right, path + (1,), depth + 1
        )
        if left_order and right_order:
            band_counter[0] += 1
            left_order[-1]["_column_flow_tail"] = True
            right_order[0]["_column_flow_head"] = True
            left_order[-1]["_layout_band"] = band_counter[0]
            right_order[0]["_layout_band"] = band_counter[0]
        return left_order + right_order

    ordered = recurse(lines, 0.0, float(page_width), (), 0)
    if not cuts:
        for line in ordered:
            line["_layout_column"] = "single"
        return ordered, None
    paths: list[tuple[int, ...]] = []
    for line in ordered:
        path = line.get("_layout_path")
        if isinstance(path, tuple) and path not in paths:
            paths.append(path)
    if len(paths) < 2:
        fallback = physical(lines)
        for line in fallback:
            line["_layout_column"] = "single"
            line.pop("_column_flow_tail", None)
            line.pop("_column_flow_head", None)
            line.pop("_layout_band", None)
        return fallback, None
    labels = {
        path: (
            "left" if len(paths) == 2 and index == 0
            else "right" if len(paths) == 2 and index == 1
            else f"column_{index + 1}"
        )
        for index, path in enumerate(paths)
    }
    for line in ordered:
        path = line.get("_layout_path")
        if isinstance(path, tuple):
            line["_layout_column"] = labels[path]
    return ordered, {
        "mode": "two_column" if len(paths) == 2 else "recursive_columns",
        "column_count": len(paths),
        "confidence": min(cut["confidence"] for cut in cuts),
        "gutter_x": cuts[0]["gutter_x"],
        "tolerance": cuts[0]["tolerance"],
        "cuts": cuts,
    }


def _order_lines(lines: list[dict[str, Any]], width: float) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Infer recursive column cuts while preserving spanning anchors."""
    split_candidates = [
        line
        for line in lines
        if not line.get("_table_meta")
        if len(line.get("cells", [])) == 2
        and not any(
            re.fullmatch(
                r"\(?\s*[-+]?\s*(?:[$€£¥￥]\s*)?\d[\d,.]*(?:\s*%)?\s*\)?",
                cell["text"].strip(),
            )
            for cell in line["cells"]
        )
        and line["cells"][1]["layout_bbox"][0] - line["cells"][0]["layout_bbox"][2]
        >= max(12.0, width * 0.035)
    ]
    if len(split_candidates) >= 2:
        expanded: list[dict[str, Any]] = []
        for line in lines:
            if line not in split_candidates:
                expanded.append(line)
                continue
            for cell in line["cells"]:
                expanded.append(_line_from_cell(line, cell))
        lines = expanded
    return _recursive_order_columns(lines, width)


def _join_physical_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    hyphenated = re.search(r"([A-Za-z]+(?:-[A-Za-z]+)*)-$", left)
    if hyphenated and re.match(r"[a-z]", right):
        terminal_token = hyphenated.group(1)
        should_drop = bool(
            "-" not in terminal_token
            and (
                terminal_token.lower() in WRAP_PREFIXES
                or WRAP_SUFFIX_RE.match(right)
            )
        )
        return left[:-1] + right if should_drop else left + right
    left_char, right_char = left[-1], right[0]
    if right_char in ",.;:!?%)]}’”）】》〉" or left_char in "([{‘“（【《〈":
        return left + right
    if CJK_RE.fullmatch(left_char) and CJK_RE.fullmatch(right_char):
        return left + right
    if left_char.isspace() or right_char.isspace():
        return left.rstrip() + " " + right.lstrip()
    if left_char.isascii() and right_char.isascii():
        return left + " " + right
    return left + right


def _join_lines(values: list[str]) -> str:
    output = ""
    for value in values:
        raw_value = str(value)
        parts = PHYSICAL_BREAK_RE.split(raw_value)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            output = _join_physical_text(output, part)
    return output


def _can_merge_column_flow_lines(
    previous: dict[str, Any],
    current: dict[str, Any],
    body_size: float,
    body_height: float,
) -> bool:
    """Gate the one legitimate left-tail to right-head paragraph bridge."""

    if not previous.get("_column_flow_tail") or not current.get("_column_flow_head"):
        return False
    previous_column = previous.get("_layout_column")
    current_column = current.get("_layout_column")
    if (
        previous_column == current_column
        or previous_column in {None, "single", "spanning", "mixed"}
        or current_column in {None, "single", "spanning", "mixed"}
    ):
        return False
    if previous.get("_layout_band") != current.get("_layout_band"):
        return False
    previous_text = str(previous.get("text", "")).rstrip()
    current_text = str(current.get("text", "")).lstrip()
    if (
        not previous_text
        or not current_text
        or SENTENCE_END_RE.search(previous_text)
        or not _starts_continuation(current_text)
        or LIST_RE.match(previous_text)
        or LIST_RE.match(current_text)
        or TABLE_NOTE_RE.match(previous_text)
        or TABLE_NOTE_RE.match(current_text)
        or previous.get("_canonical_type")
        or current.get("_canonical_type")
        or previous.get("_table_meta")
        or current.get("_table_meta")
    ):
        return False
    previous_size = float(previous.get("dominant_font_size") or previous.get("font_size") or body_size)
    current_size = float(current.get("dominant_font_size") or current.get("font_size") or body_size)
    if abs(previous_size - current_size) > max(0.75, min(previous_size, current_size) * 0.12):
        return False
    previous_height = float(previous.get("dominant_line_height") or body_height)
    current_height = float(current.get("dominant_line_height") or body_height)
    if abs(previous_height - current_height) > max(3.0, min(previous_height, current_height) * 0.35):
        return False
    return (int(previous.get("font_weight", 400)) >= 600) == (
        int(current.get("font_weight", 400)) >= 600
    )


def _is_numeric_cell(value: str) -> bool:
    candidate = value.strip().replace(" ", "")
    candidate = re.sub(r"^[\$£€¥￥]", "", candidate)
    return bool(re.fullmatch(r"(?:\(?[-+]?\d[\d,.%]*\)?|[-–—])", candidate))


def _table_row_compatible(left: dict[str, Any], right: dict[str, Any], body_size: float) -> bool:
    left_cells, right_cells = left.get("cells", []), right.get("cells", [])
    if len(left_cells) < 2 or len(right_cells) < 2:
        return False
    left_box, right_box = _item_box(left), _item_box(right)
    overlap = max(0.0, min(left_box[2], right_box[2]) - max(left_box[0], right_box[0]))
    narrower = max(min(left_box[2] - left_box[0], right_box[2] - right_box[0]), 0.1)
    if overlap / narrower < 0.45:
        return False
    if len(left_cells) == len(right_cells):
        distances = [
            abs((a["layout_bbox"][0] + a["layout_bbox"][2]) / 2 - (b["layout_bbox"][0] + b["layout_bbox"][2]) / 2)
            for a, b in zip(left_cells, right_cells)
        ]
        return statistics.median(distances) <= max(72.0, body_size * 7.0)
    if abs(len(left_cells) - len(right_cells)) == 1:
        return True
    larger, smaller = (
        (left_cells, right_cells) if len(left_cells) > len(right_cells) else (right_cells, left_cells)
    )
    larger_centers = [(_item_box(cell)[0] + _item_box(cell)[2]) / 2 for cell in larger]
    matched: set[int] = set()
    for cell in smaller:
        center = (_item_box(cell)[0] + _item_box(cell)[2]) / 2
        column = min(range(len(larger_centers)), key=lambda index: abs(larger_centers[index] - center))
        if column in matched or abs(larger_centers[column] - center) > max(72.0, body_size * 7.0):
            return False
        matched.add(column)
    return True


def _looks_like_wrapped_table_continuation(
    previous: dict[str, Any], continuation: dict[str, Any], following: dict[str, Any], body_size: float
) -> bool:
    continuation_cells = continuation.get("cells", [])
    anchor_count = max(len(previous.get("cells", [])), len(following.get("cells", [])))
    if not 1 <= len(continuation_cells) < anchor_count:
        return False
    value = str(continuation.get("text", "")).strip()
    if not value or TABLE_NOTE_RE.match(value) or SENTENCE_END_RE.search(value):
        return False
    previous_gap = _item_box(previous)[1] - _item_box(continuation)[3]
    next_gap = _item_box(continuation)[1] - _item_box(following)[3]
    if not -1.0 <= previous_gap <= max(6.0, body_size * 0.65):
        return False
    if next_gap <= 0 or previous_gap >= next_gap * 0.6:
        return False
    anchor_cells = max(
        (previous.get("cells", []), following.get("cells", [])),
        key=len,
    )
    anchor_centers = [(_item_box(cell)[0] + _item_box(cell)[2]) / 2 for cell in anchor_cells]
    tolerance = max(72.0, body_size * 7.0)
    return all(
        min(abs(((_item_box(cell)[0] + _item_box(cell)[2]) / 2) - center) for center in anchor_centers)
        <= tolerance
        for cell in continuation_cells
    )


def _table_cell_column(cell: dict[str, Any], boundaries: list[float], column_count: int) -> int:
    box = cell["layout_bbox"]
    center = (box[0] + box[2]) / 2
    return min(sum(center > boundary for boundary in boundaries), column_count - 1)


def _table_cell_fits_column(
    cell: dict[str, Any], boundaries: list[float], column_count: int, tolerance: float
) -> bool:
    column = _table_cell_column(cell, boundaries, column_count)
    segment_left = -math.inf if column == 0 else boundaries[column - 1]
    segment_right = math.inf if column == column_count - 1 else boundaries[column]
    box = cell["layout_bbox"]
    return bool(
        box[0] >= segment_left - tolerance
        and box[2] <= segment_right + tolerance
    )


def _assign_table_cells(
    cells: list[dict[str, Any]], boundaries: list[float], column_count: int
) -> tuple[list[str], list[list[float] | None]]:
    values = [""] * column_count
    boxes: list[list[float] | None] = [None] * column_count
    for cell in cells:
        box = cell["layout_bbox"]
        column = _table_cell_column(cell, boundaries, column_count)
        values[column] = _join_lines([values[column], cell["text"]])
        boxes[column] = box if boxes[column] is None else bbox_union([boxes[column], box])
    return values, boxes


def _vector_rule_box(rule: dict[str, Any]) -> list[float]:
    cached = rule.get("_layout_rule_box")
    if isinstance(cached, list) and len(cached) == 4:
        return cached
    start, end = rule["layout_start"], rule["layout_end"]
    box = [
        round(min(start[0], end[0]), 3),
        round(min(start[1], end[1]), 3),
        round(max(start[0], end[0]), 3),
        round(max(start[1], end[1]), 3),
    ]
    rule["_layout_rule_box"] = box
    return box


def _vector_rules_connected(
    first: dict[str, Any], second: dict[str, Any], tolerance: float
) -> bool:
    first_box, second_box = _vector_rule_box(first), _vector_rule_box(second)
    first_orientation = first["orientation"]
    second_orientation = second["orientation"]
    if first_orientation != second_orientation:
        horizontal = first_box if first_orientation == "horizontal" else second_box
        vertical = second_box if first_orientation == "horizontal" else first_box
        return bool(
            horizontal[0] - tolerance <= vertical[0] <= horizontal[2] + tolerance
            and vertical[1] - tolerance <= horizontal[1] <= vertical[3] + tolerance
        )
    if first_orientation == "horizontal":
        same_axis = abs(first_box[1] - second_box[1]) <= tolerance
        first_interval, second_interval = first_box[0:3:2], second_box[0:3:2]
    else:
        same_axis = abs(first_box[0] - second_box[0]) <= tolerance
        first_interval = (first_box[1], first_box[3])
        second_interval = (second_box[1], second_box[3])
    return bool(
        same_axis
        and min(first_interval[1], second_interval[1])
        >= max(first_interval[0], second_interval[0]) - tolerance
    )


def _vector_rule_components(
    rules: list[dict[str, Any]], tolerance: float
) -> list[list[dict[str, Any]]]:
    if not rules:
        return []

    # A page-grid index avoids the quadratic all-pairs walk on financial PDFs
    # with hundreds of independent chart/tick paths. Expanded boxes guarantee
    # that every pair accepted by _vector_rules_connected shares a grid cell;
    # the exact predicate remains the final authority.
    cell_size = max(24.0, tolerance * 8.0)
    parents = list(range(len(rules)))
    ranks = [0] * len(rules)

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root, second_root = find(first), find(second)
        if first_root == second_root:
            return
        if ranks[first_root] < ranks[second_root]:
            first_root, second_root = second_root, first_root
        parents[second_root] = first_root
        if ranks[first_root] == ranks[second_root]:
            ranks[first_root] += 1

    spatial_bins: dict[tuple[int, int], list[int]] = {}
    for index, rule in enumerate(rules):
        box = _vector_rule_box(rule)
        min_x = math.floor((box[0] - tolerance) / cell_size)
        max_x = math.floor((box[2] + tolerance) / cell_size)
        min_y = math.floor((box[1] - tolerance) / cell_size)
        max_y = math.floor((box[3] + tolerance) / cell_size)
        bin_keys = [
            (x_index, y_index)
            for x_index in range(min_x, max_x + 1)
            for y_index in range(min_y, max_y + 1)
        ]
        nearby: set[int] = set()
        for bin_key in bin_keys:
            nearby.update(spatial_bins.get(bin_key, ()))
        for other_index in sorted(nearby):
            if _vector_rules_connected(rule, rules[other_index], tolerance):
                union(index, other_index)
        for bin_key in bin_keys:
            spatial_bins.setdefault(bin_key, []).append(index)

    grouped: dict[int, list[int]] = {}
    for index in range(len(rules)):
        grouped.setdefault(find(index), []).append(index)
    return [
        [rules[index] for index in indexes]
        for _, indexes in sorted(grouped.items(), key=lambda item: min(item[1]))
    ]


def _cluster_axis(values: list[float], tolerance: float) -> list[float]:
    clusters: list[list[float]] = []
    for value in sorted(values):
        if not clusters or abs(value - statistics.median(clusters[-1])) > tolerance:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    return [round(float(statistics.median(cluster)), 3) for cluster in clusters]


def _boxes_overlap(first: list[float], second: list[float], tolerance: float = 0.0) -> bool:
    return bool(
        min(first[2], second[2]) > max(first[0], second[0]) + tolerance
        and min(first[3], second[3]) > max(first[1], second[1]) + tolerance
    )


def _vector_candidate_has_obstacle(
    candidate_box: list[float], obstacles: list[dict[str, Any]]
) -> bool:
    return any(
        _boxes_overlap(candidate_box, obstacle.get("layout_bbox") or obstacle["bbox"])
        for obstacle in obstacles
    )


def _source_rule_bbox(rules: list[dict[str, Any]]) -> list[float]:
    boxes = []
    for rule in rules:
        start, end = rule["source_start"], rule["source_end"]
        boxes.append(
            [min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]), max(start[1], end[1])]
        )
    return bbox_union(boxes)


def _grid_table_candidate(
    component: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    obstacles: list[dict[str, Any]],
    body_size: float,
    group_id: str,
) -> dict[str, Any] | None:
    horizontal = [rule for rule in component if rule["orientation"] == "horizontal"]
    vertical = [rule for rule in component if rule["orientation"] == "vertical"]
    tolerance = max(1.25, statistics.median([rule["stroke_width"] for rule in component]) * 1.5)
    x_boundaries = _cluster_axis(
        [(_vector_rule_box(rule)[0] + _vector_rule_box(rule)[2]) / 2 for rule in vertical],
        tolerance,
    )
    y_boundaries = sorted(
        _cluster_axis(
            [(_vector_rule_box(rule)[1] + _vector_rule_box(rule)[3]) / 2 for rule in horizontal],
            tolerance,
        ),
        reverse=True,
    )
    if not 3 <= len(x_boundaries) <= 16 or not 3 <= len(y_boundaries) <= 201:
        return None
    left, right = x_boundaries[0], x_boundaries[-1]
    bottom, top = y_boundaries[-1], y_boundaries[0]
    if right - left < max(36.0, body_size * 4.0) or top - bottom < max(24.0, body_size * 2.0):
        return None
    candidate_box = [left, bottom, right, top]
    if _vector_candidate_has_obstacle(candidate_box, obstacles):
        return None
    if any(
        (_vector_rule_box(rule)[2] - _vector_rule_box(rule)[0]) / max(right - left, 0.1) < 0.80
        for rule in horizontal
    ):
        return None
    if any(
        (_vector_rule_box(rule)[3] - _vector_rule_box(rule)[1]) / max(top - bottom, 0.1) < 0.80
        for rule in vertical
    ):
        return None

    row_count, column_count = len(y_boundaries) - 1, len(x_boundaries) - 1
    cell_values: list[list[list[tuple[float, float, str]]]] = [
        [[] for _ in range(column_count)] for _ in range(row_count)
    ]
    consumed: list[int] = []
    boundary_tolerance = max(1.5, body_size * 0.12)
    for line_index, line in enumerate(lines):
        inside_cells: list[dict[str, Any]] = []
        outside_cells: list[dict[str, Any]] = []
        for cell in line.get("cells", []):
            box = cell["layout_bbox"]
            center_x = (box[0] + box[2]) / 2
            center_y = (box[1] + box[3]) / 2
            if left <= center_x <= right and bottom <= center_y <= top:
                inside_cells.append(cell)
            else:
                outside_cells.append(cell)
        if not inside_cells:
            continue
        if outside_cells or line.get("_canonical_type") or line.get("_table_meta"):
            return None
        for cell in inside_cells:
            box = cell["layout_bbox"]
            if any(
                box[0] < boundary - boundary_tolerance
                and box[2] > boundary + boundary_tolerance
                for boundary in x_boundaries[1:-1]
            ):
                return None
            if any(
                box[1] < boundary - boundary_tolerance
                and box[3] > boundary + boundary_tolerance
                for boundary in y_boundaries[1:-1]
            ):
                return None
            center_x = (box[0] + box[2]) / 2
            center_y = (box[1] + box[3]) / 2
            column = min(
                max(sum(center_x > boundary for boundary in x_boundaries) - 1, 0),
                column_count - 1,
            )
            row = min(
                max(sum(center_y < boundary for boundary in y_boundaries) - 1, 0),
                row_count - 1,
            )
            cell_values[row][column].append((-box[3], box[0], str(cell["text"])))
        consumed.append(line_index)
    if not consumed:
        return None
    rows = [
        [
            _join_lines([value for _, _, value in sorted(cell)])
            for cell in row
        ]
        for row in cell_values
    ]
    populated_rows = sum(any(cell for cell in row) for row in rows)
    populated_columns = sum(any(row[column] for row in rows) for column in range(column_count))
    multi_cell_rows = sum(sum(bool(cell) for cell in row) >= 2 for row in rows)
    if populated_rows < 2 or populated_columns < 2 or multi_cell_rows < 2:
        return None
    headers = (
        list(rows[0])
        if all(rows[0])
        and not any(_is_numeric_cell(value) for value in rows[0])
        else None
    )
    return {
        "group_id": group_id,
        "rows": rows,
        "column_ranges": [
            [x_boundaries[index], x_boundaries[index + 1]]
            for index in range(column_count)
        ],
        "column_centers": [
            round((x_boundaries[index] + x_boundaries[index + 1]) / 2, 3)
            for index in range(column_count)
        ],
        "confidence": 0.98,
        "headers": headers,
        "bbox": _source_rule_bbox(component),
        "layout_bbox": candidate_box,
        "table_detection": "vector_grid",
        "vector_rule_count": len(component),
        "consumed": sorted(set(consumed)),
    }


def _booktabs_table_candidates(
    rules: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    obstacles: list[dict[str, Any]],
    body_size: float,
    start_index: int,
) -> list[dict[str, Any]]:
    horizontal = sorted(
        (rule for rule in rules if rule["orientation"] == "horizontal"),
        key=lambda rule: -(_vector_rule_box(rule)[1] + _vector_rule_box(rule)[3]) / 2,
    )
    candidates: list[dict[str, Any]] = []
    candidate_number = start_index
    for first_index in range(len(horizontal) - 2):
        selected = horizontal[first_index:first_index + 3]
        boxes = [_vector_rule_box(rule) for rule in selected]
        left = max(box[0] for box in boxes)
        right = min(box[2] for box in boxes)
        union_left = min(box[0] for box in boxes)
        union_right = max(box[2] for box in boxes)
        if right <= left or (right - left) / max(union_right - union_left, 0.1) < 0.80:
            continue
        top, middle, bottom = [
            (box[1] + box[3]) / 2 for box in boxes
        ]
        if not top > middle > bottom:
            continue
        candidate_box = [left, bottom, right, top]
        if _vector_candidate_has_obstacle(candidate_box, obstacles):
            continue
        candidate_lines: list[tuple[int, dict[str, Any]]] = []
        mixed = False
        for line_index, line in enumerate(lines):
            cells = line.get("cells", [])
            inside = [
                cell
                for cell in cells
                if left <= (cell["layout_bbox"][0] + cell["layout_bbox"][2]) / 2 <= right
                and bottom <= (cell["layout_bbox"][1] + cell["layout_bbox"][3]) / 2 <= top
            ]
            if inside and len(inside) != len(cells):
                mixed = True
                break
            if inside:
                if line.get("_canonical_type") or line.get("_table_meta"):
                    mixed = True
                    break
                candidate_lines.append((line_index, line))
        if mixed or len(candidate_lines) < 3:
            continue
        header_lines = [
            pair for pair in candidate_lines
            if middle < (_item_box(pair[1])[1] + _item_box(pair[1])[3]) / 2 < top
        ]
        data_lines = [
            pair for pair in candidate_lines
            if bottom < (_item_box(pair[1])[1] + _item_box(pair[1])[3]) / 2 < middle
        ]
        if len(header_lines) != 1 or len(data_lines) < 1:
            continue
        physical_rows = sorted(header_lines + data_lines, key=lambda pair: -_item_box(pair[1])[3])
        template = max((line for _, line in physical_rows), key=lambda line: len(line.get("cells", [])))
        template_cells = sorted(template.get("cells", []), key=lambda cell: cell["layout_bbox"][0])
        column_count = len(template_cells)
        if not 2 <= column_count <= 15:
            continue
        boundaries = [
            (template_cells[index]["layout_bbox"][2] + template_cells[index + 1]["layout_bbox"][0]) / 2
            for index in range(column_count - 1)
        ]
        rows: list[list[str]] = []
        row_boxes: list[list[list[float] | None]] = []
        valid = True
        for _, line in physical_rows:
            row, boxes_by_column = _assign_table_cells(line.get("cells", []), boundaries, column_count)
            if sum(bool(value) for value in row) < 2:
                valid = False
                break
            rows.append(row)
            row_boxes.append(boxes_by_column)
        if not valid:
            continue
        numeric_present = any(_is_numeric_cell(value) for row in rows[1:] for value in row if value)
        if not numeric_present and len(data_lines) < 2:
            continue
        observed_ranges: list[list[float]] = []
        for column in range(column_count):
            boxes_for_column = [row[column] for row in row_boxes if row[column] is not None]
            observed_ranges.append(
                [
                    round(min(box[0] for box in boxes_for_column), 3),
                    round(max(box[2] for box in boxes_for_column), 3),
                ]
            )
        candidate_number += 1
        candidates.append(
            {
                "group_id": f"vector:{candidate_number}",
                "rows": rows,
                "column_ranges": observed_ranges,
                "column_centers": [round((left_edge + right_edge) / 2, 3) for left_edge, right_edge in observed_ranges],
                "confidence": 0.96,
                "headers": list(rows[0]),
                "bbox": _source_rule_bbox(selected),
                "layout_bbox": candidate_box,
                "table_detection": "vector_booktabs",
                "vector_rule_count": 3,
                "consumed": sorted(index for index, _ in physical_rows),
            }
        )
    return candidates


def _mark_vector_table_groups(
    lines: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    obstacles: list[dict[str, Any]],
    body_size: float,
) -> None:
    """Annotate high-confidence vector-grid and booktabs table groups."""

    if len(rules) < 3 or len(rules) > 5000:
        return
    stroke_widths = [float(rule.get("stroke_width", 1.0)) for rule in rules]
    tolerance = max(1.25, statistics.median(stroke_widths) * 1.5)
    candidates: list[dict[str, Any]] = []
    grid_number = 0
    for component in _vector_rule_components(rules, tolerance):
        grid_number += 1
        candidate = _grid_table_candidate(
            component, lines, obstacles, body_size, f"vector:{grid_number}"
        )
        if candidate is not None:
            candidates.append(candidate)
    candidates.extend(
        _booktabs_table_candidates(
            rules, lines, obstacles, body_size, grid_number
        )
    )
    used_lines: set[int] = set()
    for candidate in sorted(
        candidates,
        key=lambda item: (
            -float(item["confidence"]),
            -len(item["consumed"]),
            (item["layout_bbox"][2] - item["layout_bbox"][0])
            * (item["layout_bbox"][3] - item["layout_bbox"][1]),
            item["group_id"],
        ),
    ):
        consumed = set(candidate.pop("consumed"))
        if consumed & used_lines:
            continue
        used_lines.update(consumed)
        for line_index in consumed:
            lines[line_index]["_table_meta"] = candidate


def _mark_table_groups(lines: list[dict[str, Any]], body_size: float) -> None:
    """Annotate conservative word-grid table groups before column ordering."""
    raw_candidates = [
        index
        for index, line in enumerate(lines)
        if not line.get("_canonical_type")
        and not line.get("_table_meta")
        and 2 <= len(line.get("cells", [])) <= 15
    ]
    candidates: list[int] = []
    for position, index in enumerate(raw_candidates):
        bridge_continuation = False
        if 0 < position < len(raw_candidates) - 1:
            previous_index = raw_candidates[position - 1]
            next_index = raw_candidates[position + 1]
            previous_count = len(lines[previous_index]["cells"])
            current_count = len(lines[index]["cells"])
            next_count = len(lines[next_index]["cells"])
            bridge_continuation = (
                previous_count == next_count
                and current_count <= previous_count - 2
                and _looks_like_wrapped_table_continuation(
                    lines[previous_index], lines[index], lines[next_index], body_size
                )
            )
        if not bridge_continuation:
            candidates.append(index)
    groups: list[list[int]] = []
    groups_have_numeric: list[bool] = []
    for index in candidates:
        current_has_numeric = any(
            _is_numeric_cell(cell["text"]) for cell in lines[index].get("cells", [])
        )
        if not groups:
            groups.append([index])
            groups_have_numeric.append(current_has_numeric)
            continue
        previous_index = groups[-1][-1]
        previous, current = lines[previous_index], lines[index]
        gap = _item_box(previous)[1] - _item_box(current)[3]
        intervening = index - previous_index - 1
        wrapped_intervening = True
        physical_previous = previous
        for continuation_index in range(previous_index + 1, index):
            continuation = lines[continuation_index]
            if not _looks_like_wrapped_table_continuation(
                physical_previous, continuation, current, body_size
            ):
                wrapped_intervening = False
                break
            physical_previous = continuation
        header_reset = (
            groups_have_numeric[-1]
            and (not current_has_numeric or bool(TABLE_SUMMARY_RE.match(str(current.get("text", "")).strip())))
        )
        if (
            -1.0 <= gap <= max(body_size * 4.0, 32.0)
            and intervening <= 2
            and wrapped_intervening
            and _table_row_compatible(previous, current, body_size)
            and not header_reset
        ):
            groups[-1].append(index)
            groups_have_numeric[-1] = groups_have_numeric[-1] or current_has_numeric
        else:
            groups.append([index])
            groups_have_numeric.append(current_has_numeric)

    group_id = 0
    for candidate_indexes in groups:
        if len(candidate_indexes) < 2:
            continue
        candidate_lines = [lines[index] for index in candidate_indexes]
        template = max(candidate_lines, key=lambda line: len(line["cells"]))
        template_cells = sorted(template["cells"], key=lambda cell: cell["layout_bbox"][0])
        column_count = len(template_cells)
        if not 2 <= column_count <= 15:
            continue
        gaps = [
            template_cells[index + 1]["layout_bbox"][0] - template_cells[index]["layout_bbox"][2]
            for index in range(column_count - 1)
        ]
        if not gaps or min(gaps) < max(8.0, body_size * 0.7):
            continue
        boundaries = [
            (template_cells[index]["layout_bbox"][2] + template_cells[index + 1]["layout_bbox"][0]) / 2
            for index in range(column_count - 1)
        ]
        rows: list[list[str]] = []
        row_boxes: list[list[list[float] | None]] = []
        assigned_by_index: dict[int, int] = {}
        for index in candidate_indexes:
            row, boxes = _assign_table_cells(lines[index]["cells"], boundaries, column_count)
            assigned_by_index[index] = len(rows)
            rows.append(row)
            row_boxes.append(boxes)

        numeric_present = any(_is_numeric_cell(cell) for row in rows for cell in row if cell)
        multi_column_rows = sum(sum(bool(cell) for cell in row) >= 2 for row in rows)
        support = [sum(bool(row[column]) for row in rows) for column in range(column_count)]
        label_value_table = bool(
            column_count == 2
            and len(rows) >= 3
            and all(row[0] and row[1] for row in rows)
            and max(len(row[0].strip()) for row in rows) <= 40
            and statistics.median(len(row[0].strip()) for row in rows) <= 25
            and statistics.median(
                line["cells"][1]["layout_bbox"][0] - line["cells"][0]["layout_bbox"][2]
                for line in candidate_lines
            )
            <= max(140.0, body_size * 14.0)
            and not any(SENTENCE_END_RE.search(row[0].strip()) for row in rows)
        )
        if column_count == 2 and not numeric_present and not label_value_table:
            continue
        if multi_column_rows < 2 or multi_column_rows / len(rows) < 0.5:
            continue
        if max(sum(bool(cell) for cell in row) for row in rows) < math.ceil(column_count * 0.6):
            continue
        weak_columns = [
            column for column, value in enumerate(support)
            if value < min(2, len(rows))
        ]
        if weak_columns and not (
            len(rows) >= 3
            and all(support[column] == 1 and bool(rows[0][column]) for column in weak_columns)
        ):
            continue

        consumed = set(candidate_indexes)
        continuation_signatures: set[frozenset[int]] = set()
        for previous_index, next_index in zip(candidate_indexes, candidate_indexes[1:]):
            for continuation_index in range(previous_index + 1, next_index):
                continuation = lines[continuation_index]
                if not 1 <= len(continuation.get("cells", [])) < column_count:
                    continue
                previous_gap = _item_box(lines[previous_index])[1] - _item_box(continuation)[3]
                next_gap = _item_box(continuation)[1] - _item_box(lines[next_index])[3]
                if not (-1.0 <= previous_gap <= max(body_size * 1.4, 14.0)):
                    continue
                if not (-1.0 <= next_gap <= max(body_size * 2.2, 22.0)):
                    continue
                continuation_row, continuation_boxes = _assign_table_cells(
                    continuation["cells"], boundaries, column_count
                )
                populated = [column for column, value in enumerate(continuation_row) if value]
                if not populated:
                    continue
                fits = True
                boundary_tolerance = max(6.0, body_size * 0.75)
                for cell in continuation["cells"]:
                    if not _table_cell_fits_column(
                        cell, boundaries, column_count, boundary_tolerance
                    ):
                        fits = False
                        break
                if not fits:
                    continue
                row_index = assigned_by_index[previous_index]
                for column in populated:
                    box = continuation_boxes[column]
                    rows[row_index][column] = _join_lines([rows[row_index][column], continuation_row[column]])
                    row_boxes[row_index][column] = (
                        box
                        if row_boxes[row_index][column] is None
                        else bbox_union([row_boxes[row_index][column], box])
                    )
                consumed.add(continuation_index)
                continuation_signatures.add(frozenset(populated))

        if column_count >= 3 and continuation_signatures:
            last_candidate = candidate_indexes[-1]
            last_physical = last_candidate
            for continuation_index in range(last_candidate + 1, min(len(lines), last_candidate + 3)):
                continuation = lines[continuation_index]
                if not 1 <= len(continuation.get("cells", [])) < column_count:
                    break
                value = str(continuation.get("text", "")).strip()
                if not value or TABLE_NOTE_RE.match(value) or SENTENCE_END_RE.search(value):
                    break
                gap = _item_box(lines[last_physical])[1] - _item_box(continuation)[3]
                if not -1.0 <= gap <= max(6.0, body_size * 0.65):
                    break
                continuation_row, continuation_boxes = _assign_table_cells(
                    continuation["cells"], boundaries, column_count
                )
                populated = [column for column, cell_value in enumerate(continuation_row) if cell_value]
                if frozenset(populated) not in continuation_signatures:
                    break
                boundary_tolerance = max(6.0, body_size * 0.75)
                if any(
                    not _table_cell_fits_column(
                        cell, boundaries, column_count, boundary_tolerance
                    )
                    for cell in continuation["cells"]
                ):
                    break
                row_index = assigned_by_index[last_candidate]
                for column in populated:
                    box = continuation_boxes[column]
                    rows[row_index][column] = _join_lines(
                        [rows[row_index][column], continuation_row[column]]
                    )
                    row_boxes[row_index][column] = (
                        box
                        if row_boxes[row_index][column] is None
                        else bbox_union([row_boxes[row_index][column], box])
                    )
                consumed.add(continuation_index)
                last_physical = continuation_index

        group_id += 1
        consumed_lines = [lines[index] for index in sorted(consumed)]
        observed_ranges: list[list[float]] = []
        for column in range(column_count):
            boxes = [row[column] for row in row_boxes if row[column] is not None]
            if boxes:
                observed_ranges.append([
                    round(min(box[0] for box in boxes), 3),
                    round(max(box[2] for box in boxes), 3),
                ])
            else:
                observed_ranges.append([
                    round(template_cells[column]["layout_bbox"][0], 3),
                    round(template_cells[column]["layout_bbox"][2], 3),
                ])
        confidence = min(0.96, 0.72 + 0.12 * (multi_column_rows / len(rows)) + 0.12 * (min(support) / len(rows)))
        headers = (
            list(rows[0])
            if rows
            and all(rows[0])
            and not any(_is_numeric_cell(value) for value in rows[0])
            and any(_is_numeric_cell(value) for row in rows[1:] for value in row if value)
            else None
        )
        meta = {
            "group_id": group_id,
            "rows": rows,
            "column_ranges": observed_ranges,
            "column_centers": [round((left + right) / 2, 3) for left, right in observed_ranges],
            "confidence": round(confidence, 3),
            "headers": headers,
            "bbox": bbox_union(line["bbox"] for line in consumed_lines),
            "layout_bbox": bbox_union(_item_box(line) for line in consumed_lines),
        }
        for index in consumed:
            lines[index]["_table_meta"] = meta


def _classify_blocks(
    lines: list[dict[str, Any]],
    page_number: int,
    *,
    document_body_size: float | None = None,
    document_body_height: float | None = None,
    document_has_heading_size_signal: bool | None = None,
) -> list[dict[str, Any]]:
    if not lines:
        return []
    body_sizes = [line["font_size"] for line in lines if len(line["text"]) >= 8]
    body_size = float(document_body_size or statistics.median(body_sizes or [line["font_size"] for line in lines]))
    line_heights = [max(_item_box(line)[3] - _item_box(line)[1], 0.1) for line in lines]
    body_height = float(document_body_height or statistics.median(line_heights))
    body_weight_counts: Counter[int] = Counter()
    for line in lines:
        value = str(line.get("text", ""))
        character_count = len(re.sub(r"\s+", "", value))
        line_size = float(line.get("dominant_font_size") or line.get("font_size") or body_size)
        if character_count >= 8 and abs(line_size - body_size) <= max(1.0, body_size * 0.15):
            body_weight_counts[int(line.get("font_weight", 400) or 400)] += character_count
    body_weight = (
        max(body_weight_counts, key=lambda weight: (body_weight_counts[weight], -weight))
        if body_weight_counts else 400
    )
    if document_has_heading_size_signal is None:
        document_has_heading_size_signal = any(
            float(line.get("dominant_font_size") or line["font_size"]) >= body_size * 1.12
            and line.get("_layout_horizontal", True)
            for line in lines
        )
    size_degenerate = bool(
        body_size <= 2.0
        or not document_has_heading_size_signal
    )
    gaps = []
    for previous, current in zip(lines, lines[1:]):
        gap = _item_box(previous)[1] - _item_box(current)[3]
        if 0 <= gap <= body_size * 4:
            gaps.append(gap)
    if gaps:
        normal_gap = statistics.median(gaps)
        lower_gaps = sorted(gaps)[: max(1, math.ceil(len(gaps) / 2))]
        paragraph_gap = statistics.median(lower_gaps)
    else:
        normal_gap = body_size * 0.35
        paragraph_gap = normal_gap
    list_markers = _confirmed_list_markers(lines)
    typed: list[tuple[str, dict[str, Any]]] = []
    for line_index, line in enumerate(lines):
        if line.get("_canonical_type"):
            typed.append((str(line["_canonical_type"]), line))
            continue
        if line.get("_table_meta"):
            typed.append(("table_group", line))
            continue
        value = line["text"].strip()
        numeric = len(NUMERIC_RE.findall(value))
        line_size = float(line.get("dominant_font_size") or line["font_size"])
        line_height = float(
            line.get("dominant_line_height")
            or max(_item_box(line)[3] - _item_box(line)[1], 0.1)
        )
        visual_ratio = max(
            line_size / max(body_size, 0.1),
            line_height / max(body_height, 0.1)
            if size_degenerate and line.get("_layout_horizontal", True) else 0.0,
        )
        size_ratio = line_size / max(body_size, 0.1)
        height_ratio = (
            line_height / max(body_height, 0.1)
            if size_degenerate and line.get("_layout_horizontal", True) else 0.0
        )
        blocked_heading = _heading_blocked(value)
        strong_heading = bool(
            len(value) <= 100
            and (size_ratio >= 1.18 or height_ratio >= 1.35)
            and not SENTENCE_END_RE.search(value)
            and not blocked_heading
        )
        line_weight = int(line.get("font_weight", 400) or 400)
        bold_heading = bool(
            3 <= len(re.sub(r"\s+", "", value)) <= 100
            and line_weight >= 600
            and line_weight >= body_weight + 150
            and line_size >= body_size * 0.95
            and len(line.get("cells", [])) == 1
            and not line.get("_split_from_two_columns")
            and not SENTENCE_END_RE.search(value)
            and not blocked_heading
        )
        list_meta = list_markers.get(line_index)
        if strong_heading:
            kind = "heading"
        elif bold_heading:
            kind = "heading"
        elif list_meta is not None:
            kind = "list_item"
            line["_list_meta"] = list_meta
        elif LIST_RE.match(value) and not MARKER_ONLY_RE.fullmatch(value):
            kind = "list_item"
            line["_list_meta"] = {
                "body": value,
                "ordered": bool(re.match(r"^\s*\d+[.)]", value)),
                "ordinal": int(re.match(r"^\s*(\d+)", value).group(1))
                if re.match(r"^\s*(\d+)", value) else 1,
            }
        elif len(line["cells"]) >= 2 and numeric >= 1:
            kind = "table"
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
        source_groups = current.pop("_source_line_groups", [group])
        list_meta = current.pop("_list_meta", None)
        current["bbox"] = bbox_union(line["bbox"] for line in group)
        current["layout_bbox"] = bbox_union(_item_box(line) for line in group)
        current["_layout_rank"] = min(
            int(line.get("_layout_rank", 0)) for line in group
        )
        if current["type"] == "table":
            current["rows"] = [[cell["text"] for cell in line["cells"]] for line in group]
            current["text"] = "\n".join(line["text"] for line in group)
        elif current["type"] == "list_item" and list_meta is not None:
            current["raw_text"] = _join_lines([line["text"] for line in group])
            current["text"] = _join_lines(
                [str(list_meta.get("body", group[0]["text"]))]
                + [line["text"] for line in group[1:]]
            )
            current["ordered"] = bool(list_meta.get("ordered"))
            current["ordinal"] = int(list_meta.get("ordinal", 1))
        else:
            current["text"] = _join_lines([line["text"] for line in group])
        columns = {line.get("_layout_column", "single") for line in group}
        current["_layout_column"] = next(iter(columns)) if len(columns) == 1 else "mixed"
        if len(source_groups) > 1:
            current["_source_spans"] = [
                {
                    "bbox": bbox_union(line["bbox"] for line in source_group),
                    "layout_bbox": bbox_union(_item_box(line) for line in source_group),
                    "layout_column": source_group[0].get("_layout_column", "single"),
                    **{
                        key.removeprefix("_"): value
                        for key, value in _ocr_item_metadata(source_group).items()
                    },
                }
                for source_group in source_groups
            ]
        current.update(_ocr_item_metadata(group))
        blocks.append(current)
        current = None

    emitted_table_groups: set[int | str] = set()
    for kind, line in typed:
        if kind == "table_group":
            flush()
            meta = line["_table_meta"]
            if meta["group_id"] not in emitted_table_groups:
                emitted_table_groups.add(meta["group_id"])
                group_lines = [item for _, item in typed if item.get("_table_meta") is meta]
                columns = {item.get("_layout_column", "single") for item in group_lines}
                blocks.append(
                    {
                        "type": "table",
                        "rows": meta["rows"],
                        "text": "\n".join(_join_lines(row) for row in meta["rows"]),
                        "bbox": meta["bbox"],
                        "layout_bbox": meta["layout_bbox"],
                        "column_ranges": meta["column_ranges"],
                        "column_centers": meta["column_centers"],
                        "confidence": meta["confidence"],
                        "headers": meta["headers"],
                        "table_detection": meta.get("table_detection", "word_grid"),
                        "vector_rule_count": meta.get("vector_rule_count"),
                        "_layout_rank": min(
                            int(item.get("_layout_rank", 0)) for item in group_lines
                        ),
                        "_layout_column": next(iter(columns)) if len(columns) == 1 else "mixed",
                        **_ocr_item_metadata(group_lines),
                    }
                )
            continue
        can_join = False
        column_flow_join = False
        if current is not None:
            previous = current["_lines"][-1]
            gap = _item_box(previous)[1] - _item_box(line)[3]
            indent_shift = _item_box(line)[0] - _item_box(previous)[0]
            same_column = previous.get("_layout_column", "single") == line.get("_layout_column", "single")
            same_weight_class = (int(previous.get("font_weight", 400)) >= 600) == (
                int(line.get("font_weight", 400)) >= 600
            )
            continuation = bool(
                line["text"].strip()
                and not SENTENCE_END_RE.search(previous["text"].rstrip())
                and line["text"].lstrip()[0].isascii()
                and line["text"].lstrip()[0].islower()
            )
            if current["type"] == kind == "paragraph":
                indent_limit = max(48.0, body_size * 4.0) if continuation else max(18.0, body_size * 1.5)
                can_join = (
                    -1.0 <= gap <= max(paragraph_gap * 1.75, body_size * 0.9)
                    and abs(indent_shift) <= indent_limit
                    and same_column
                    and same_weight_class
                )
                if not can_join and not current.get("_column_flow_joined"):
                    column_flow_join = _can_merge_column_flow_lines(
                        previous, line, body_size, body_height
                    )
                    can_join = column_flow_join
            elif current["type"] == "list_item" and kind == "paragraph":
                can_join = -1.0 <= gap <= max(normal_gap * 1.6, body_size * 0.8) and indent_shift >= -3 and same_column
            elif current["type"] == kind == "heading":
                previous_size = float(previous.get("dominant_font_size") or previous.get("font_size") or body_size)
                current_size = float(line.get("dominant_font_size") or line.get("font_size") or body_size)
                can_join = bool(
                    -1.0 <= gap <= max(normal_gap * 1.5, body_size * 0.9)
                    and abs(indent_shift) <= max(12.0, body_size)
                    and same_column
                    and same_weight_class
                    and abs(previous_size - current_size)
                    <= max(0.75, min(previous_size, current_size) * 0.08)
                    and len(_join_lines([previous["text"], line["text"]])) <= 160
                )
            elif current["type"] == kind == "table":
                can_join = -1.0 <= gap <= max(normal_gap * 2.2, body_size * 1.2) and same_column
        if not can_join:
            flush()
            current = {
                "type": kind,
                "_lines": [line],
                "_source_line_groups": [[line]],
            }
            if kind == "list_item":
                current["_list_meta"] = dict(line.get("_list_meta", {}))
            if kind == "heading":
                ratio = max(
                    float(line.get("dominant_font_size") or line["font_size"]) / max(body_size, 0.1),
                    float(
                        line.get("dominant_line_height")
                        or max(_item_box(line)[3] - _item_box(line)[1], 0.1)
                    ) / max(body_height, 0.1)
                    if size_degenerate and line.get("_layout_horizontal", True) else 0.0,
                )
                current["level"] = 1 if ratio >= 1.5 else 2 if ratio >= 1.18 else 3
        else:
            current["_lines"].append(line)
            if column_flow_join:
                current["_source_line_groups"].append([line])
                current["_column_flow_joined"] = True
            else:
                current["_source_line_groups"][-1].append(line)
    flush()
    return blocks


def _extract_image(image, target: Path) -> None:
    bitmap = image.get_bitmap(render=True, scale_to_original=True)
    try:
        bitmap.to_pil().save(target, format="PNG")
    finally:
        bitmap.close()


def _ocr_compact_text(value: str) -> str:
    return "".join(character for character in value if not character.isspace())


def _ocr_garbled_ratio(value: str) -> float:
    compact = _ocr_compact_text(value)
    if not compact:
        return 0.0
    bad = 0
    for character in compact:
        codepoint = ord(character)
        if (
            character in {"\ufffd", "\ufffe", "\x02"}
            or (codepoint < 32 and character not in "\t\r\n")
            or 0xE000 <= codepoint <= 0xF8FF
        ):
            bad += 1
    return bad / len(compact)


def _ocr_image_coverage(
    image_boxes: list[list[float]], page_width: float, page_height: float
) -> float:
    page_area = max(float(page_width) * float(page_height), 0.1)
    largest = 0.0
    for box in image_boxes:
        if len(box) != 4:
            continue
        width = max(float(box[2]) - float(box[0]), 0.0)
        height = max(float(box[3]) - float(box[1]), 0.0)
        largest = max(largest, width * height)
    return min(largest / page_area, 1.0)


def _unicode_map_error_ratio(textpage: Any, *, sample_limit: int = 512) -> float:
    """Sample PDFium's character-level Unicode-map error signal deterministically."""

    try:
        count = int(textpage.count_chars())
    except Exception:
        return 0.0
    if count <= 0:
        return 0.0
    sample_count = min(count, max(int(sample_limit), 1))
    if sample_count == count:
        indexes = range(count)
    elif sample_count == 1:
        indexes = (0,)
    else:
        indexes = tuple(
            round(position * (count - 1) / (sample_count - 1))
            for position in range(sample_count)
        )
    raw_textpage = getattr(textpage, "raw", textpage)
    try:
        failures = sum(
            bool(_pdfium().raw.FPDFText_HasUnicodeMapError(raw_textpage, index))
            for index in indexes
        )
    except Exception:
        return 0.0
    return failures / sample_count


def _analyze_ocr_need(
    mode: str,
    raw_text: str,
    fragments: list[dict[str, Any]],
    image_boxes: list[list[float]],
    visual_object_count: int,
    page_width: float,
    page_height: float,
    extraction_warnings: list[dict[str, Any]],
    *,
    unicode_map_error_ratio: float = 0.0,
) -> dict[str, Any]:
    """Return a conservative page-local OCR decision.

    A small logo must not route a healthy born-digital page through OCR.  The
    automatic path is reserved for pages with no usable text, a dominant page
    raster plus sparse text, or a clearly garbled native text layer.
    """

    if mode not in {"off", "auto", "force"}:
        raise ValueError("OCR mode must be one of: off, auto, force")
    compact = _ocr_compact_text(raw_text)
    garbled_ratio = _ocr_garbled_ratio(raw_text)
    image_coverage = _ocr_image_coverage(image_boxes, page_width, page_height)
    usable_characters = sum(
        1
        for character in compact
        if character not in {"\ufffd", "\ufffe", "\x02"}
        and not (0xE000 <= ord(character) <= 0xF8FF)
    )
    has_loss_warning = any(
        item.get("content_loss")
        and item.get("code") in {"unmapped_pdf_glyph", "pdf_text_object_error"}
        for item in extraction_warnings
    )
    fragment_extraction_failed = bool(
        compact
        and not fragments
        and (visual_object_count > 0 or has_loss_warning)
    )
    unicode_map_error_ratio = max(0.0, min(float(unicode_map_error_ratio), 1.0))
    reasons: list[str] = []
    if usable_characters < 5 and visual_object_count:
        reasons.append("no_usable_text")
    if fragment_extraction_failed:
        reasons.append("native_fragment_extraction_failed")
    if unicode_map_error_ratio >= 0.20:
        reasons.append("unicode_map_error")
    if garbled_ratio >= 0.20 or (has_loss_warning and usable_characters < 80):
        reasons.append("garbled_text")
    if usable_characters < 40 and image_coverage >= 0.35:
        reasons.append("sparse_text_with_dominant_image")
    requires_ocr = bool(reasons)
    if mode == "force":
        reasons = ["forced", *reasons]
    native_unusable = bool(
        usable_characters < 5
        or garbled_ratio >= 0.20
        or fragment_extraction_failed
        or unicode_map_error_ratio >= 0.20
    )
    return {
        "should_run": mode == "force" or (mode == "auto" and requires_ocr),
        "requires_ocr": requires_ocr,
        "native_unusable": native_unusable,
        "reasons": tuple(dict.fromkeys(reasons)),
        "usable_characters": usable_characters,
        "garbled_ratio": round(garbled_ratio, 4),
        "unicode_map_error_ratio": round(unicode_map_error_ratio, 4),
        "image_coverage": round(image_coverage, 4),
        "native_fragment_count": len(fragments),
    }


def _ocr_output_to_fragments(
    output: OcrPageResult | list[dict[str, Any]], provider: Any
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(output, OcrPageResult):
        entries = [
            {
                "text": span.text,
                "bbox": list(span.bbox),
                "polygon": [list(point) for point in span.polygon],
                "confidence": span.confidence,
            }
            for span in output.spans
        ]
        metadata = {
            "provider": output.engine,
            "version": output.engine_version,
            "runtime": output.runtime,
            "runtime_version": output.runtime_version,
            "model_profile": output.model_profile,
            "language": output.language,
            "requested_dpi": output.requested_dpi,
            "effective_dpi": output.effective_dpi,
            "min_confidence": output.min_confidence,
            "raster_width": output.raster_width,
            "raster_height": output.raster_height,
            "elapsed_seconds": output.elapsed_seconds,
            "dropped_low_confidence": output.dropped_low_confidence,
            "dropped_invalid": output.dropped_invalid,
        }
    elif isinstance(output, list):
        entries = output
        version = getattr(provider, "version", "unknown")
        settings = getattr(provider, "settings", None)
        metadata = {
            "provider": str(getattr(provider, "name", "custom-ocr")),
            "version": str(version() if callable(version) else version),
            "runtime": "custom",
            "runtime_version": "unknown",
            "model_profile": "custom",
            "language": getattr(settings, "language", "unknown"),
            "requested_dpi": getattr(settings, "dpi", None),
            "effective_dpi": None,
            "min_confidence": getattr(settings, "min_confidence", None),
            "raster_width": None,
            "raster_height": None,
            "elapsed_seconds": None,
            "dropped_low_confidence": 0,
            "dropped_invalid": 0,
        }
    else:
        raise OcrProviderError("OCR provider returned an unsupported result type")

    fragments: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            metadata["dropped_invalid"] += 1
            continue
        text = str(entry.get("text", "")).strip()
        box = entry.get("bbox")
        try:
            confidence = float(entry.get("confidence", 1.0))
            normalized_box = [round(float(value), 3) for value in box]
        except (TypeError, ValueError, OverflowError):
            metadata["dropped_invalid"] += 1
            continue
        if (
            not text
            or len(normalized_box) != 4
            or not all(math.isfinite(value) for value in normalized_box)
            or normalized_box[2] <= normalized_box[0]
            or normalized_box[3] <= normalized_box[1]
            or not math.isfinite(confidence)
            or not 0.0 <= confidence <= 1.0
        ):
            metadata["dropped_invalid"] += 1
            continue
        height = normalized_box[3] - normalized_box[1]
        width = normalized_box[2] - normalized_box[0]
        text_angle = 0
        polygon = entry.get("polygon")
        if polygon is not None:
            try:
                points = [tuple(float(value) for value in point) for point in polygon]
                if len(points) == 4 and all(len(point) == 2 for point in points):
                    delta_x = points[1][0] - points[0][0]
                    delta_y = points[1][1] - points[0][1]
                    raw_angle = math.degrees(math.atan2(delta_y, delta_x)) % 360
                    snapped = int(round(raw_angle / 90.0) * 90) % 360
                    if abs(((raw_angle - snapped + 180) % 360) - 180) <= 12:
                        text_angle = snapped
            except (TypeError, ValueError, OverflowError):
                text_angle = 0
        fragments.append(
            {
                "text": text,
                "bbox": normalized_box,
                "text_angle": text_angle,
                "font_size": max(min(height * 0.8, 72.0), 1.0),
                "font_weight": 400,
                "char_width": max(width / max(len(text), 1), 0.1),
                "_ocr_provider": metadata["provider"],
                "_ocr_version": metadata["version"],
                "_ocr_confidence": round(confidence, 6),
                "_source_method": "ocr",
            }
        )
    return fragments, metadata


def _ocr_overlap_ratio(first: list[float], second: list[float]) -> float:
    left = max(first[0], second[0])
    bottom = max(first[1], second[1])
    right = min(first[2], second[2])
    top = min(first[3], second[3])
    intersection = max(right - left, 0.0) * max(top - bottom, 0.0)
    first_area = max(first[2] - first[0], 0.0) * max(first[3] - first[1], 0.0)
    second_area = max(second[2] - second[0], 0.0) * max(second[3] - second[1], 0.0)
    return intersection / max(min(first_area, second_area), 0.1)


def _ocr_text_similarity(first: str, second: str) -> float:
    normalize = lambda value: re.sub(r"[^\w\u3400-\u9fff]+", "", value.casefold())
    left, right = normalize(first), normalize(second)
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right))
    return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()


def _ocr_fragments_duplicate(first: dict[str, Any], second: dict[str, Any]) -> bool:
    overlap = _ocr_overlap_ratio(first["bbox"], second["bbox"])
    similarity = _ocr_text_similarity(str(first.get("text", "")), str(second.get("text", "")))
    return overlap >= 0.45 and similarity >= 0.55


def _ocr_usable_character_count(fragments: list[dict[str, Any]]) -> int:
    """Count semantic characters used to decide whether OCR restored a page."""

    return sum(
        character.isalnum()
        for fragment in fragments
        for character in str(fragment.get("text", ""))
        if character != "\ufffd" and not 0xE000 <= ord(character) <= 0xF8FF
    )


def _merge_native_ocr_fragments(
    native: list[dict[str, Any]],
    ocr: list[dict[str, Any]],
    *,
    native_unusable: bool,
) -> tuple[list[dict[str, Any]], int, int]:
    """Merge OCR fragments conservatively and return drop counts.

    Healthy native text wins over an overlapping OCR estimate.  For a garbled
    native layer, a high-confidence overlapping OCR line replaces that native
    fragment.  Non-overlapping material from either source is retained.
    """

    retained_native: list[dict[str, Any]] = []
    dropped_native = 0
    for fragment in native:
        replacement = next(
            (
                candidate
                for candidate in ocr
                if float(candidate.get("_ocr_confidence", 0.0)) >= 0.70
                and _ocr_fragments_duplicate(fragment, candidate)
            ),
            None,
        )
        if native_unusable and replacement is not None:
            dropped_native += 1
        else:
            retained_native.append(fragment)

    retained_ocr: list[dict[str, Any]] = []
    dropped_ocr = 0
    for fragment in ocr:
        if any(_ocr_fragments_duplicate(candidate, fragment) for candidate in retained_native):
            dropped_ocr += 1
            continue
        if any(_ocr_fragments_duplicate(candidate, fragment) for candidate in retained_ocr):
            dropped_ocr += 1
            continue
        retained_ocr.append(fragment)
    return retained_native + retained_ocr, dropped_ocr, dropped_native


class PdfAdapter:
    name = "pdfium"
    limitations = ["formulas_not_semantically_recognized"]

    def __init__(self, ocr_provider=None, *, ocr_mode: str | None = None):
        if ocr_provider is None:
            resolved_mode = ocr_mode or "off"
            ocr_provider = NullOcrProvider(
                OcrSettings(mode=resolved_mode, engine="none")
            )
        else:
            provider_settings = getattr(ocr_provider, "settings", None)
            resolved_mode = ocr_mode or getattr(provider_settings, "mode", "auto")
        if resolved_mode not in {"off", "auto", "force"}:
            raise ValueError("OCR mode must be one of: off, auto, force")
        self.ocr_provider = ocr_provider
        self.ocr_mode = resolved_mode
        self.limitations = ["formulas_not_semantically_recognized"]
        if resolved_mode == "off" or isinstance(ocr_provider, NullOcrProvider):
            self.limitations.append("ocr_not_available")
        else:
            self.limitations.append("ocr_quality_is_statistical")

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
        page_lines: list[list[dict[str, Any]]] = []
        page_layouts: list[dict[str, Any]] = []
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
                unicode_map_error_ratio = _unicode_map_error_ratio(textpage)
                counts = Counter()
                fragments: list[dict[str, Any]] = []
                image_objects: list[Any] = []
                vector_rules: list[dict[str, Any]] = []
                vector_obstacles: list[dict[str, Any]] = []
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
                                    "font_weight": _font_weight(font),
                                    "char_width": max((box[2] - box[0]) / max(len(value.strip()), 1), 0.1),
                                    "_object_key": _raw_pointer_key(obj.raw),
                                    "_object_index": object_index,
                                    "_container_context": _container_chain_identity(obj),
                                }
                            )
                        except Exception as exc:
                            extraction_warnings.append(
                                {"code": "pdf_text_object_error", "message": f"text object {object_index}: {type(exc).__name__}: {exc}", "content_loss": True}
                            )
                    elif obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE:
                        image_objects.append(obj)
                    elif obj.type == pdfium.raw.FPDF_PAGEOBJ_PATH:
                        path_rules, path_obstacles = _extract_path_vector_geometry(
                            obj, object_index
                        )
                        vector_rules.extend(path_rules)
                        vector_obstacles.extend(path_obstacles)
                if any(_needs_character_geometry(fragment) for fragment in fragments):
                    characters_by_object = _collect_text_object_characters(textpage)
                    refined_fragments: list[dict[str, Any]] = []
                    for fragment in fragments:
                        characters = characters_by_object.get(fragment["_object_key"], [])
                        unresolved = 0
                        if "\x02" in fragment["text"] or "\ufffe" in fragment["text"]:
                            if characters:
                                fragment, characters, unresolved = _resolve_unmapped_glyphs(
                                    fragment, characters
                                )
                            else:
                                unresolved = fragment["text"].count("\x02") + fragment["text"].count("\ufffe")
                                fragment = dict(fragment)
                                fragment["text"] = fragment["text"].replace("\x02", "\ufffd").replace("\ufffe", "\ufffd")
                            if unresolved:
                                extraction_warnings.append(
                                    {
                                        "code": "unmapped_pdf_glyph",
                                        "message": (
                                            f"text object {fragment['_object_index']} contained {unresolved} "
                                            "unmapped glyph(s) that could not be identified safely"
                                        ),
                                        "content_loss": True,
                                    }
                                )
                        if characters and _needs_character_geometry(fragment):
                            refined_fragments.extend(
                                _fragments_from_character_geometry(fragment, characters)
                            )
                        else:
                            refined_fragments.append(fragment)
                    fragments = refined_fragments
                fragments, native_duplicates_dropped = _deduplicate_native_fragments(
                    fragments
                )
                for fragment in fragments:
                    fragment.pop("_object_key", None)
                    fragment.pop("_object_index", None)
                    fragment.pop("_container_context", None)
                image_boxes: list[list[float]] = []
                for image in image_objects:
                    try:
                        image_boxes.append(
                            [round(float(value), 3) for value in image.get_bounds()]
                        )
                    except Exception:
                        continue
                visual_objects = (
                    counts[pdfium.raw.FPDF_PAGEOBJ_TEXT]
                    + counts[pdfium.raw.FPDF_PAGEOBJ_IMAGE]
                    + counts[pdfium.raw.FPDF_PAGEOBJ_PATH]
                )
                ocr_decision = _analyze_ocr_need(
                    self.ocr_mode,
                    raw_text,
                    fragments,
                    image_boxes,
                    visual_objects,
                    width,
                    height,
                    extraction_warnings,
                    unicode_map_error_ratio=unicode_map_error_ratio,
                )
                ocr_applied = False
                ocr_recovery_sufficient = False
                ocr_actual_required = any(
                    reason != "forced" for reason in ocr_decision["reasons"]
                )
                ocr_failure: dict[str, str] | None = None
                ocr_metadata: dict[str, Any] = {}
                if ocr_decision["should_run"]:
                    provider_version = getattr(self.ocr_provider, "version", "unknown")
                    provider_settings = getattr(self.ocr_provider, "settings", None)
                    ocr_metadata = {
                        "provider": str(getattr(self.ocr_provider, "name", "custom-ocr")),
                        "version": str(
                            provider_version()
                            if callable(provider_version)
                            else provider_version
                        ),
                        "language": getattr(provider_settings, "language", "unknown"),
                        "requested_dpi": getattr(provider_settings, "dpi", None),
                        "min_confidence": getattr(
                            provider_settings, "min_confidence", None
                        ),
                    }
                    try:
                        ocr_output = self.ocr_provider.extract(page, page_number)
                        ocr_fragments, result_metadata = _ocr_output_to_fragments(
                            ocr_output, self.ocr_provider
                        )
                        ocr_metadata.update(result_metadata)
                        if ocr_fragments:
                            usable_ocr_characters = _ocr_usable_character_count(
                                ocr_fragments
                            )
                            ocr_metadata["usable_characters"] = usable_ocr_characters
                            fragments, dropped_ocr, dropped_native = (
                                _merge_native_ocr_fragments(
                                    fragments,
                                    ocr_fragments,
                                    native_unusable=ocr_decision["native_unusable"],
                                )
                            )
                            ocr_metadata["dropped_overlap"] = dropped_ocr
                            ocr_metadata["replaced_native"] = dropped_native
                            ocr_applied = True
                            ocr_recovery_sufficient = (
                                usable_ocr_characters >= 5
                                if ocr_actual_required
                                else True
                            )
                            if not ocr_recovery_sufficient:
                                ocr_failure = {
                                    "code": "ocr_incomplete_result",
                                    "message": (
                                        f"Page {page_number} OCR recovered only "
                                        f"{usable_ocr_characters} usable character(s)"
                                    ),
                                }
                        else:
                            ocr_failure = {
                                "code": "ocr_empty_result",
                                "message": (
                                    f"Page {page_number} OCR returned no usable text above "
                                    "the configured confidence threshold"
                                ),
                            }
                    except OcrUnavailableError as exc:
                        ocr_failure = {
                            "code": "ocr_unavailable",
                            "message": f"Page {page_number} OCR backend is unavailable: {exc}",
                        }
                    except OcrProviderError as exc:
                        ocr_failure = {
                            "code": "ocr_failed",
                            "message": f"Page {page_number} OCR failed: {exc}",
                        }
                    except Exception as exc:
                        ocr_failure = {
                            "code": "ocr_failed",
                            "message": (
                                f"Page {page_number} OCR failed with an unexpected "
                                f"{type(exc).__name__}"
                            ),
                        }
                angle_counts = Counter()
                angle_fragments = [
                    fragment for fragment in fragments if fragment.get("_source_method") != "ocr"
                ] or fragments
                for fragment in angle_fragments:
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
                for fragment in fragments:
                    fragment["_layout_horizontal"] = fragment["text_angle"] == dominant
                vector_rules, vector_obstacles = _normalize_vector_geometry(
                    vector_rules,
                    vector_obstacles,
                    width,
                    height,
                    dominant if normalized else 0,
                )
                image_obstacles = [
                    {
                        "index": image_index,
                        "bbox": box,
                        "layout_bbox": transform_bbox_for_orientation(
                            box,
                            width,
                            height,
                            dominant if normalized else 0,
                        )[0],
                    }
                    for image_index, box in enumerate(image_boxes, start=1)
                ]
                lines = _group_lines(fragments)
                unit_locator = {
                    "page": page_number,
                    "bbox_order": ["left", "bottom", "right", "top"],
                    "orientation_normalized": normalized,
                    "dominant_text_angle": dominant,
                }
                unit_id = stable_id("unit", document_id, unit_locator, "page", page_number)
                if native_duplicates_dropped:
                    unit_locator["native_dedup"] = {
                        "dropped_duplicate_paint_layers": native_duplicates_dropped
                    }
                if ocr_metadata:
                    ocr_locator_keys = (
                        "provider", "version", "runtime", "runtime_version",
                        "model_profile", "language", "requested_dpi",
                        "effective_dpi", "min_confidence", "raster_width",
                        "raster_height", "usable_characters",
                        "dropped_low_confidence", "dropped_invalid",
                        "dropped_overlap", "replaced_native",
                    )
                    unit_locator["ocr"] = {
                        key: ocr_metadata[key]
                        for key in ocr_locator_keys
                        if ocr_metadata.get(key) is not None
                    }
                unit_warnings: list[dict[str, Any]] = []
                for item in extraction_warnings:
                    item["source_unit"] = unit_id
                    unit_warnings.append(item)
                if native_duplicates_dropped:
                    unit_warnings.append(
                        {
                            "code": "native_duplicate_paint_layer",
                            "message": (
                                f"Page {page_number} removed {native_duplicates_dropped} "
                                "duplicate native text paint layer(s)"
                            ),
                            "content_loss": False,
                            "source_unit": unit_id,
                        }
                    )
                status = "complete"
                if ocr_applied and ocr_recovery_sufficient:
                    status = "warning"
                    provider_name = str(
                        ocr_metadata.get("provider")
                        or getattr(self.ocr_provider, "name", "OCR")
                    )
                    unit_warnings.append(
                        {
                            "code": "ocr_applied",
                            "message": (
                                f"Page {page_number} text was supplied by {provider_name} "
                                f"({', '.join(ocr_decision['reasons'])})"
                            ),
                            "content_loss": False,
                            "source_unit": unit_id,
                        }
                    )
                elif ocr_decision["requires_ocr"]:
                    status = "ocr_required"
                    if ocr_failure is not None:
                        unit_warnings.append(
                            {
                                **ocr_failure,
                                "content_loss": True,
                                "source_unit": unit_id,
                            }
                        )
                    unit_warnings.append(
                        {
                            "code": "ocr_required",
                            "message": (
                                f"Page {page_number} requires OCR "
                                f"({', '.join(ocr_decision['reasons'])})"
                            ),
                            "content_loss": True,
                            "source_unit": unit_id,
                        }
                    )
                elif ocr_failure is not None:
                    status = "warning"
                    unit_warnings.append(
                        {
                            **ocr_failure,
                            "content_loss": False,
                            "source_unit": unit_id,
                        }
                    )
                elif not lines and not image_objects:
                    status = "empty"
                    unit_warnings.append(
                        {"code": "empty_page", "message": f"Page {page_number} contains no extractable content", "content_loss": True, "source_unit": unit_id}
                    )
                filtered = int(ocr_metadata.get("dropped_low_confidence", 0)) + int(
                    ocr_metadata.get("dropped_invalid", 0)
                )
                if filtered:
                    unit_warnings.append(
                        {
                            "code": "ocr_detections_filtered",
                            "message": (
                                f"Page {page_number} discarded {filtered} invalid or "
                                "low-confidence OCR detection(s)"
                            ),
                            "content_loss": ocr_actual_required,
                            "source_unit": unit_id,
                        }
                    )
                if unit_warnings and status == "complete":
                    status = "warning"
                source_units.append(
                    {"id": unit_id, "type": "page", "index": page_number, "locator": unit_locator, "status": status, "warnings": unit_warnings}
                )
                warnings.extend(unit_warnings)
                page_lines.append(lines)
                page_layouts.append(
                    {
                        "width": layout_width,
                        "height": layout_height,
                        "column_layout": None,
                        "vector_rules": vector_rules,
                        "vector_obstacles": vector_obstacles,
                        "image_obstacles": image_obstacles,
                    }
                )
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

        document_body_size = _document_body_size(page_lines)
        document_body_height = _document_body_height(page_lines)
        document_has_heading_size_signal = _document_has_heading_size_signal(
            page_lines, document_body_size
        )
        chrome_by_page = _classify_running_chrome(page_lines, page_layouts)
        page_blocks: list[list[dict[str, Any]]] = []
        for page_index, (lines, layout, unit) in enumerate(
            zip(page_lines, page_layouts, source_units, strict=True), start=1
        ):
            _mark_vector_table_groups(
                lines,
                layout.get("vector_rules", []),
                layout.get("vector_obstacles", []),
                document_body_size,
            )
            lines = _expand_parallel_prose_lines(lines, layout["width"])
            page_lines[page_index - 1] = lines
            _mark_table_groups(lines, document_body_size)
            layout_items = list(lines)
            for obstacle in layout.get("image_obstacles", []):
                layout_items.append(
                    {
                        "text": "",
                        "bbox": obstacle["bbox"],
                        "layout_bbox": obstacle["layout_bbox"],
                        "font_size": document_body_size,
                        "dominant_font_size": document_body_size,
                        "dominant_line_height": document_body_height,
                        "font_weight": 400,
                        "cells": [],
                        "_layout_horizontal": True,
                        "_layout_obstacle": "image",
                        "_obstacle_index": obstacle["index"],
                    }
                )
            ordered_items, column_layout = _order_lines(layout_items, layout["width"])
            ordered_lines: list[dict[str, Any]] = []
            image_ranks: dict[int, int] = {}
            for rank, item in enumerate(ordered_items):
                if item.get("_layout_obstacle") == "image":
                    image_ranks[int(item["_obstacle_index"])] = rank
                    continue
                item["_layout_rank"] = rank
                ordered_lines.append(item)
            layout["image_ranks"] = image_ranks
            layout["column_layout"] = column_layout
            blocks = _classify_blocks(
                ordered_lines,
                page_index,
                document_body_size=document_body_size,
                document_body_height=document_body_height,
                document_has_heading_size_signal=document_has_heading_size_signal,
            )
            page_blocks.append(blocks)
            chrome_counts = chrome_by_page.get(page_index)
            if chrome_counts:
                classified_count = sum(chrome_counts.values())
                warning = {
                    "code": "running_chrome_classified",
                    "message": (
                        f"Page {page_index} classified {classified_count} repeated "
                        "edge line(s) as running chrome"
                    ),
                    "content_loss": False,
                    "source_unit": unit["id"],
                }
                unit["warnings"].append(warning)
                warnings.append(warning)
                if unit["status"] == "complete":
                    unit["status"] = "warning"
            if column_layout is not None:
                warning = {
                    "code": "multi_column_order_inferred",
                    "message": f"Page {page_index} reading order was inferred from two-column geometry",
                    "content_loss": False,
                    "source_unit": unit["id"],
                }
                unit["warnings"].append(warning)
                warnings.append(warning)
                if unit["status"] == "complete":
                    unit["status"] = "warning"

        content: list[dict[str, Any]] = []
        tables: list[dict[str, Any]] = []
        occurrence = 0
        for unit, blocks, images, page_layout in zip(source_units, page_blocks, page_assets, page_layouts, strict=True):
            page_number = unit["index"]
            ranked_items: list[tuple[int, int, str, dict[str, Any]]] = [
                (int(block.get("_layout_rank", index)), index, "block", block)
                for index, block in enumerate(blocks)
            ]
            image_ranks = page_layout.get("image_ranks", {})
            for image_index, asset in enumerate(images, start=1):
                fallback_rank = len(blocks) + image_index
                ranked_items.append(
                    (
                        int(image_ranks.get(image_index, fallback_rank)),
                        len(blocks) + image_index,
                        "image",
                        asset,
                    )
                )
            ordered_items = [
                (kind, item)
                for _, _, kind, item in sorted(
                    ranked_items, key=lambda value: (value[0], value[1])
                )
            ]
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
                locator = {
                    "source_unit_id": unit["id"],
                    "page": page_number,
                    "bbox": block["bbox"],
                    "layout_bbox": block.get("layout_bbox", block["bbox"]),
                    "page_width": page_layout["width"],
                    "page_height": page_layout["height"],
                    "layout_column": block.get("_layout_column", "single"),
                }
                if block.get("_extraction_method"):
                    locator["extraction_method"] = block["_extraction_method"]
                if block.get("_ocr_provider"):
                    locator["ocr_provider"] = block["_ocr_provider"]
                if block.get("_ocr_version"):
                    locator["ocr_version"] = block["_ocr_version"]
                if block.get("_ocr_confidence") is not None:
                    locator["ocr_confidence"] = block["_ocr_confidence"]
                if block.get("column_ranges"):
                    locator["column_ranges"] = block["column_ranges"]
                if block.get("table_detection"):
                    locator["table_detection"] = block["table_detection"]
                if block.get("vector_rule_count") is not None:
                    locator["vector_rule_count"] = block["vector_rule_count"]
                if block.get("_source_spans"):
                    locator["spans"] = [
                        {
                            "source_unit_id": unit["id"],
                            "page": page_number,
                            "page_width": page_layout["width"],
                            "page_height": page_layout["height"],
                            **span,
                        }
                        for span in block["_source_spans"]
                    ]
                if block["type"] == "table":
                    table_id = stable_id("table", document_id, locator, "table", occurrence)
                    raw_rows = block["rows"]
                    widths = {len(row) for row in raw_rows}
                    confidence = block.get("confidence")
                    if confidence is None and len(widths) == 1 and next(iter(widths), 0) >= 2:
                        confidence = 0.82
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
                    table_record = {
                        "table_id": table_id,
                        "source_locator": locator,
                        "raw_rows": raw_rows,
                        "rows": rows,
                        "confidence": confidence,
                        "warnings": table_warnings,
                    }
                    if block.get("headers"):
                        table_record["headers"] = block["headers"]
                    tables.append(table_record)
                    content.append(
                        {"id": stable_id("node", document_id, locator, "table", occurrence), "type": "table", "source_locator": locator, "table_id": table_id}
                    )
                else:
                    extra: dict[str, Any] = {}
                    if block["type"] == "heading":
                        extra["level"] = block.get("level", 2)
                    if block["type"] == "list_item":
                        extra.update(
                            {
                                "ordered": bool(block.get("ordered")),
                                "ordinal": int(block.get("ordinal", 1)),
                            }
                        )
                    content.append(
                        {
                            "id": stable_id("node", document_id, locator, block["type"], occurrence),
                            "type": block["type"],
                            "source_locator": locator,
                            **make_text_fields(
                                block.get("raw_text", block["text"]),
                                block["text"].strip(),
                                mode,
                                defer=True,
                            ),
                            **extra,
                        }
                    )
        _merge_cross_page_tables(content, tables)
        _merge_cross_page_paragraphs(content, mode)
        _reassign_content_ids(content, tables, document_id)
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


def _span_from_locator(locator: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "source_unit_id", "page", "bbox", "layout_bbox", "page_width",
        "page_height", "layout_column", "column_ranges", "extraction_method",
        "ocr_provider", "ocr_version", "ocr_confidence", "table_detection",
        "vector_rule_count",
    )
    return {field: locator[field] for field in fields if field in locator}


def _locator_spans(locator: dict[str, Any]) -> list[dict[str, Any]]:
    spans = locator.get("spans")
    if isinstance(spans, list) and spans:
        return [dict(span) for span in spans]
    return [_span_from_locator(locator)]


def _tail_span(locator: dict[str, Any]) -> dict[str, Any]:
    spans = _locator_spans(locator)
    return spans[-1]


def _merge_locator_ocr_provenance(
    target: dict[str, Any], source: dict[str, Any]
) -> None:
    if not target.get("extraction_method") and not source.get("extraction_method"):
        return
    target_method = target.get("extraction_method", "native")
    source_method = source.get("extraction_method", "native")
    target["extraction_method"] = (
        "ocr" if target_method == source_method == "ocr" else "native+ocr"
    )
    for key in ("ocr_provider", "ocr_version"):
        values = {
            str(value)
            for value in (target.get(key), source.get(key))
            if value
        }
        if values:
            target[key] = "+".join(sorted(values))
    confidences = [
        float(value)
        for value in (target.get("ocr_confidence"), source.get("ocr_confidence"))
        if value is not None
    ]
    if confidences:
        target["ocr_confidence"] = round(min(confidences), 6)


def _page_edge_limit(height: float) -> float:
    return max(36.0, min(72.0, float(height) * 0.10))


def _starts_continuation(value: str) -> bool:
    candidate = value.lstrip(" \t\"'‘’“”（([【《〈")
    if not candidate or LIST_RE.match(candidate):
        return False
    first = candidate[0]
    return first.islower() or bool(CJK_RE.fullmatch(first))


def _is_page_label(value: str) -> bool:
    candidate = value.strip()
    return bool(
        re.fullmatch(r"\d+", candidate)
        or re.fullmatch(r"page\s+\d+(?:\s+of\s+\d+)?", candidate, re.I)
        or re.fullmatch(r"第\s*\d+\s*页", candidate)
    )


def _edge_fingerprint(value: str) -> str:
    candidate = re.sub(r"\d+", "#", value.casefold())
    return re.sub(r"\s+", " ", candidate).strip()


def _classify_running_chrome(
    page_lines: list[list[dict[str, Any]]],
    page_layouts: list[dict[str, Any]],
) -> dict[int, dict[str, int]]:
    """Annotate only repeated, position-stable edge lines as non-body content."""

    total_pages = len(page_lines)
    if total_pages < 2:
        return {}
    occurrences: dict[
        tuple[str, str], list[tuple[int, dict[str, Any], float, float]]
    ] = {}
    for page_number, (lines, layout) in enumerate(
        zip(page_lines, page_layouts, strict=True), start=1
    ):
        width = float(layout.get("width") or 0.0)
        height = float(layout.get("height") or 0.0)
        if width <= 0 or height <= 0:
            continue
        edge_limit = _page_edge_limit(height)
        for line in lines:
            value = str(line.get("text", "")).strip()
            box = _item_box(line)
            fingerprint = _edge_fingerprint(value)
            if not fingerprint or len(fingerprint) > 180:
                continue
            edge: str | None = None
            edge_offset = 0.0
            if box[3] >= height - edge_limit:
                edge = "top"
                edge_offset = (height - box[3]) / height
            elif box[1] <= edge_limit:
                edge = "bottom"
                edge_offset = box[1] / height
            if edge is None:
                continue
            occurrences.setdefault((edge, fingerprint), []).append(
                (page_number, line, box[0] / width, edge_offset)
            )

    minimum_pages = max(2, math.ceil(total_pages * 0.50))
    classified: dict[int, dict[str, int]] = {}
    for (_edge, _fingerprint), items in occurrences.items():
        pages = {page for page, _, _, _ in items}
        if len(pages) < minimum_pages:
            continue
        x_positions = [x for _, _, x, _ in items]
        edge_positions = [offset for _, _, _, offset in items]
        median_x = statistics.median(x_positions)
        median_edge = statistics.median(edge_positions)
        if max(abs(value - median_x) for value in x_positions) > 0.04:
            continue
        if max(abs(value - median_edge) for value in edge_positions) > 0.025:
            continue
        for page, line, _, _ in items:
            kind = "page_label" if _is_page_label(str(line.get("text", ""))) else "boilerplate"
            line["_canonical_type"] = kind
            counts = classified.setdefault(page, {"boilerplate": 0, "page_label": 0})
            counts[kind] += 1
    return classified


def _repeated_edge_fingerprints(content: list[dict[str, Any]]) -> set[tuple[str, str]]:
    occurrences: dict[tuple[str, str], set[int]] = {}
    pages: set[int] = set()
    for node in content:
        if node.get("type") != "paragraph" or not node.get("text"):
            continue
        locator = node.get("source_locator", {})
        page = locator.get("page")
        box = locator.get("layout_bbox") or locator.get("bbox")
        height = locator.get("page_height")
        if not page or not box or not height:
            continue
        pages.add(int(page))
        edge = _page_edge_limit(float(height))
        fingerprint = _edge_fingerprint(str(node["text"]))
        if len(fingerprint) < 3:
            continue
        if box[3] >= float(height) - edge:
            occurrences.setdefault(("top", fingerprint), set()).add(int(page))
        if box[1] <= edge:
            occurrences.setdefault(("bottom", fingerprint), set()).add(int(page))
    threshold = max(2, math.ceil(max(len(pages), 1) * 0.30))
    return {key for key, page_numbers in occurrences.items() if len(page_numbers) >= threshold}


def _can_merge_cross_page_paragraphs(
    previous: dict[str, Any],
    current: dict[str, Any],
    repeated_edges: set[tuple[str, str]] | None = None,
) -> bool:
    if previous.get("type") != "paragraph" or current.get("type") != "paragraph":
        return False
    previous_locator = previous.get("source_locator", {})
    current_locator = current.get("source_locator", {})
    previous_tail = _tail_span(previous_locator)
    current_head = _locator_spans(current_locator)[0]
    previous_page = previous_tail.get("page")
    current_page = current_head.get("page")
    if not previous_page or current_page != previous_page + 1:
        return False
    previous_text = str(previous.get("text", "")).rstrip()
    current_text = str(current.get("text", "")).lstrip()
    if (
        not previous_text
        or not current_text
        or SENTENCE_END_RE.search(previous_text)
        or not _starts_continuation(current_text)
        or _is_page_label(previous_text)
        or _is_page_label(current_text)
    ):
        return False
    repeated_edges = repeated_edges or set()
    if ("bottom", _edge_fingerprint(previous_text)) in repeated_edges:
        return False
    if ("top", _edge_fingerprint(current_text)) in repeated_edges:
        return False
    previous_box = previous_tail.get("layout_bbox") or previous_tail.get("bbox")
    current_box = current_head.get("layout_bbox") or current_head.get("bbox")
    previous_height = previous_tail.get("page_height") or previous_locator.get("page_height")
    current_height = current_head.get("page_height") or current_locator.get("page_height")
    previous_width = previous_tail.get("page_width") or previous_locator.get("page_width")
    current_width = current_head.get("page_width") or current_locator.get("page_width")
    if not all((previous_box, current_box, previous_height, current_height, previous_width, current_width)):
        return False
    if previous_box[1] > _page_edge_limit(previous_height):
        return False
    if current_box[3] < float(current_height) - _page_edge_limit(current_height):
        return False
    previous_column = previous_tail.get("layout_column", previous_locator.get("layout_column", "single"))
    current_column = current_head.get("layout_column", current_locator.get("layout_column", "single"))
    if previous_column in {"mixed", "spanning"} or current_column in {"mixed", "spanning"}:
        return False
    if previous_column == current_column == "single":
        overlap = max(0.0, min(previous_box[2], current_box[2]) - max(previous_box[0], current_box[0]))
        narrower = max(min(previous_box[2] - previous_box[0], current_box[2] - current_box[0]), 0.1)
        left_tolerance = max(18.0, min(28.0, min(float(previous_width), float(current_width)) * 0.04))
        return overlap / narrower >= 0.60 and abs(previous_box[0] - current_box[0]) <= left_tolerance
    return previous_column == "right" and current_column == "left"


def _merge_cross_page_paragraphs(content: list[dict[str, Any]], mode: str) -> None:
    repeated_edges = _repeated_edge_fingerprints(content)
    index = 0
    while index < len(content):
        current = content[index]
        if current.get("type") != "paragraph":
            index += 1
            continue
        previous_index = index - 1
        while (
            previous_index >= 0
            and content[previous_index].get("type") in {"boilerplate", "page_label"}
        ):
            previous_index -= 1
        if previous_index < 0:
            index += 1
            continue
        previous = content[previous_index]
        if _can_merge_cross_page_paragraphs(previous, current, repeated_edges):
            current_page = _tail_span(current["source_locator"])["page"]
            raw = _join_lines([previous.get("raw_text", previous["text"]), current.get("raw_text", current["text"])])
            text = _join_lines([previous["text"], current["text"]])
            previous.update(make_text_fields(raw, text, mode, defer=True))
            previous_locator = previous["source_locator"]
            previous_locator["spans"] = _locator_spans(previous_locator) + _locator_spans(current["source_locator"])
            previous_locator["continued_to_page"] = current_page
            _merge_locator_ocr_provenance(previous_locator, current["source_locator"])
            content.pop(index)
            continue
        index += 1


def _normalized_table_row(row: list[str]) -> tuple[str, ...]:
    return tuple(re.sub(r"\s+", " ", value).strip().casefold() for value in row)


def _table_columns_compatible(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_ranges = previous.get("source_locator", {}).get("column_ranges") or []
    current_ranges = current.get("source_locator", {}).get("column_ranges") or []
    if len(previous_ranges) < 2 or len(previous_ranges) != len(current_ranges):
        return False
    previous_width = float(previous.get("source_locator", {}).get("page_width") or 1.0)
    current_width = float(current.get("source_locator", {}).get("page_width") or 1.0)
    tolerance = max(15.0, min(previous_width, current_width) * 0.03)
    for left, right in zip(previous_ranges, current_ranges):
        previous_center = (left[0] + left[1]) / 2
        current_center = (right[0] + right[1]) / 2
        if abs(previous_center - current_center) > tolerance:
            return False
    return True


def _can_stitch_tables(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_locator = previous.get("source_locator", {})
    current_locator = current.get("source_locator", {})
    previous_tail = _tail_span(previous_locator)
    current_head = _locator_spans(current_locator)[0]
    if current_head.get("page") != previous_tail.get("page", 0) + 1:
        return False
    previous_box = previous_tail.get("layout_bbox") or previous_tail.get("bbox")
    current_box = current_head.get("layout_bbox") or current_head.get("bbox")
    previous_height = previous_tail.get("page_height") or previous_locator.get("page_height")
    current_height = current_head.get("page_height") or current_locator.get("page_height")
    if not all((previous_box, current_box, previous_height, current_height)):
        return False
    if previous_box[1] > _page_edge_limit(previous_height):
        return False
    if current_box[3] < float(current_height) - _page_edge_limit(current_height):
        return False
    repeated_header = bool(
        previous.get("raw_rows")
        and current.get("raw_rows")
        and _normalized_table_row(previous["raw_rows"][0])
        == _normalized_table_row(current["raw_rows"][0])
    )
    return repeated_header and _table_columns_compatible(previous, current)


def _merge_cross_page_tables(content: list[dict[str, Any]], tables: list[dict[str, Any]]) -> None:
    by_id = {table["table_id"]: table for table in tables}
    index = 0
    while index < len(content):
        current_node = content[index]
        if current_node.get("type") != "table":
            index += 1
            continue
        previous_index = index - 1
        while (
            previous_index >= 0
            and content[previous_index].get("type") in {"boilerplate", "page_label"}
        ):
            previous_index -= 1
        if previous_index < 0 or content[previous_index].get("type") != "table":
            index += 1
            continue
        previous_node = content[previous_index]
        previous = by_id.get(previous_node.get("table_id"))
        current = by_id.get(current_node.get("table_id"))
        if previous is None or current is None or not _can_stitch_tables(previous, current):
            index += 1
            continue
        skip_header = bool(
            previous["raw_rows"]
            and current["raw_rows"]
            and _normalized_table_row(previous["raw_rows"][0]) == _normalized_table_row(current["raw_rows"][0])
        )
        start = 1 if skip_header else 0
        previous["raw_rows"].extend(current["raw_rows"][start:])
        previous["rows"].extend(current["rows"][start:])
        previous["cross_page_continuation"] = True
        merged_locator = previous["source_locator"]
        merged_locator["spans"] = _locator_spans(merged_locator) + _locator_spans(current["source_locator"])
        merged_locator["continued_to_page"] = _tail_span(current["source_locator"])["page"]
        _merge_locator_ocr_provenance(merged_locator, current["source_locator"])
        previous_node["source_locator"] = merged_locator
        tables.remove(current)
        by_id.pop(current["table_id"], None)
        content.pop(index)


def _locator_identity(locator: dict[str, Any]) -> dict[str, Any]:
    """Return the stable geometric identity of a locator.

    OCR engine versions and confidence values are useful provenance but may
    change after a model/runtime upgrade.  They must not make an otherwise
    identical page location acquire a different canonical node id.
    """

    volatile = {"extraction_method", "ocr_provider", "ocr_version", "ocr_confidence"}
    identity = {key: value for key, value in locator.items() if key not in volatile}
    if isinstance(identity.get("spans"), list):
        identity["spans"] = [
            _locator_identity(span) if isinstance(span, dict) else span
            for span in identity["spans"]
        ]
    return identity


def _reassign_content_ids(
    content: list[dict[str, Any]], tables: list[dict[str, Any]], document_id: str
) -> None:
    tables_by_id = {table["table_id"]: table for table in tables}
    for occurrence, node in enumerate(content, start=1):
        if node["type"] == "table":
            table = tables_by_id[node["table_id"]]
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
