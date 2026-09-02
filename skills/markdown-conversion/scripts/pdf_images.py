"""Optional PDF visuals, with document-local matching and bounded page work.

Only Inspector text is used to associate visual regions with existing content.
PDFium contributes geometry and the composited appearance, never body text.
This module runs inside the separately supervised image worker: its deadline is
also enforced by the parent, including native calls which cannot cooperate.
"""
from __future__ import annotations

import copy
import html
import math
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from canonical import sha256_file, stable_id

TEXT_TYPES = {"heading", "paragraph", "list_item"}
MAX_OBJECTS_PER_PAGE = 20_000
MAX_NEIGHBOR_CHECKS = 100_000
MAX_ASSETS = 4096
MAX_ASSET_BYTES = 256 * 1024 * 1024


def _fingerprint(node: dict[str, Any]) -> str:
    value = str(node.get("raw_text") or node.get("text") or "")
    return "".join(character.casefold() for character in value if not character.isspace())


class LegacyImageMatcher:
    """Cache the old matcher without changing its occurrence/ambiguity rules."""

    def __init__(self, content: list[dict[str, Any]]):
        self.stats = {"fingerprint_computations": 0, "cache_hits": 0, "cache_misses": 0}
        self.fingerprints: dict[int, str] = {}
        # List occurrences deliberately are not deduplicated by object identity.
        self.entries = [(id(node), self.fingerprint(node)) for node in content
                        if node.get("type") in TEXT_TYPES]
        self.queries: dict[tuple[str, str], int | None] = {}

    def fingerprint(self, node: dict[str, Any]) -> str:
        key = id(node)
        if key not in self.fingerprints:
            self.fingerprints[key] = _fingerprint(node)
            self.stats["fingerprint_computations"] += 1
        return self.fingerprints[key]

    def anchor(self, value: str | None, position: str) -> int | None:
        if not value:
            return None
        key = (value, position)
        if key in self.queries:
            self.stats["cache_hits"] += 1
            return self.queries[key]
        self.stats["cache_misses"] += 1
        partial = len(value) >= 12
        matches = [identity for identity, candidate in self.entries
                   if candidate == value
                   or (partial and position == "previous" and candidate.endswith(value))
                   or (partial and position == "following" and candidate.startswith(value))]
        self.queries[key] = matches[0] if len(matches) == 1 else None
        return self.queries[key]

    def merge(self, content: list[dict[str, Any]], support: list[dict[str, Any]],
              unit: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        images = [node for node in support if node.get("type") == "image"]
        if not images:
            return content, 0
        before: list[str | None] = []
        value = None
        for node in support:
            before.append(value)
            if node.get("type") in TEXT_TYPES:
                value = self.fingerprint(node) or value
        after: list[str | None] = [None] * len(support)
        value = None
        for index in range(len(support) - 1, -1, -1):
            after[index] = value
            if support[index].get("type") in TEXT_TYPES:
                value = self.fingerprint(support[index]) or value
        support_indexes = {id(node): index for index, node in enumerate(support)}
        indexes = {id(node): index for index, node in enumerate(content)
                   if node.get("type") in TEXT_TYPES}
        slots: dict[int, list[dict[str, Any]]] = defaultdict(list)
        ambiguous = 0
        for image in images:
            index = support_indexes[id(image)]
            previous, following = before[index], after[index]
            left = indexes.get(self.anchor(previous, "previous"))
            right = indexes.get(self.anchor(following, "following"))
            slot = None
            if left is not None and right is not None:
                if left < right:
                    slot = left + 1
            elif left is not None and following is None:
                slot = left + 1
            elif right is not None and previous is None:
                slot = right
            if slot is None:
                ambiguous += 1
                continue
            clone = dict(image)
            clone["source_locator"] = dict(image.get("source_locator", {}))
            clone["source_locator"]["source_unit_id"] = unit["id"]
            slots[slot].append(clone)
        merged = []
        for index in range(len(content) + 1):
            merged.extend(slots.get(index, []))
            if index < len(content):
                merged.append(content[index])
        return merged, ambiguous


def merge_support_images_cached(content, support, unit, cache=None):
    return (cache or LegacyImageMatcher(content)).merge(content, support, unit)


def merge_cached(base_content, support_by_page, units_by_page):
    """Whole-document compatibility entry point, also useful for replay tests."""
    matcher = LegacyImageMatcher(base_content)
    content = base_content
    stats = {"processed_images": 0, "inserted_images": 0, "unpositioned_images": 0,
             "pages_processed": 0, "pages_with_images": 0, "unpositioned_by_page": {}}
    for page, support in sorted(support_by_page.items()):
        count = sum(node.get("type") == "image" for node in support)
        content, ambiguous = matcher.merge(content, support, units_by_page[page])
        stats["pages_processed"] += 1
        stats["pages_with_images"] += bool(count)
        stats["processed_images"] += count
        stats["inserted_images"] += count - ambiguous
        stats["unpositioned_images"] += ambiguous
        if count:
            stats["unpositioned_by_page"][page] = ambiguous
    stats.update(matcher.stats)
    stats["query_cache_entries"] = len(matcher.queries)
    return content, stats


class ImageBudgetExceeded(RuntimeError):
    pass


def _check_deadline(deadline: float) -> None:
    # Leave a small cooperative margin for validation and result serialization;
    # the parent still enforces the original deadline over the complete worker.
    if time.monotonic() >= deadline - 0.25:
        raise ImageBudgetExceeded("image deadline")


def _box(value) -> tuple[float, float, float, float] | None:
    try:
        result = tuple(float(item) for item in value)
        if len(result) == 4 and all(math.isfinite(item) for item in result):
            if result[0] <= result[2] and result[1] <= result[3]:
                return result
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _union(boxes):
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _overlap(a, b, margin=0.0):
    return a[0] <= b[2] + margin and a[2] + margin >= b[0] and a[1] <= b[3] + margin and a[3] + margin >= b[1]


def _contains(a, b, margin=0.0):
    return a[0] - margin <= b[0] and a[1] - margin <= b[1] and a[2] + margin >= b[2] and a[3] + margin >= b[3]


def _normal(text):
    # Comparison only. Original body/cells are never rewritten.
    plain = html.unescape(re.sub(r"<[^>]*>", "", str(text)))
    return "".join(c.casefold() for c in plain if c.isalnum())


def _tokens(text):
    words = re.findall(r"[^\W_]+", html.unescape(re.sub(r"<[^>]*>", "", str(text))).casefold())
    result = {word for word in words if len(word) >= 3}
    # CJK has no mandatory whitespace; sparse text items still get a bounded
    # character index. Every fragment of at least six chars shares a trigram.
    for word in words:
        if any("\u3400" <= c <= "\u9fff" for c in word):
            result.update(word[i:i + 3] for i in range(len(word) - 2))
    return result


class BodyIndex:
    """Transient occurrence and cell index over the frozen Inspector body."""

    def __init__(self, body):
        self.content = body["content"]
        self.entries = []
        self.postings = defaultdict(set)
        self.exact = defaultdict(list)
        self.queries = {}
        tables = {str(t["table_id"]): t for t in body.get("tables", [])}
        for ordinal, node in enumerate(self.content):
            if node.get("source_locator", {}).get("placement") == "unanchored_supplement":
                continue
            if node.get("type") in TEXT_TYPES:
                self._add(ordinal, node.get("raw_text") or node.get("text") or "", None)
            elif node.get("type") == "table":
                table = tables.get(str(node["table_id"]), {})
                rows = table.get("raw_rows") or table.get("rows") or []
                for row_index, row in enumerate(rows):
                    for column, cell in enumerate(row):
                        value = cell.get("raw_text", cell.get("text", "")) if isinstance(cell, dict) else cell
                        self._add(ordinal, value, (row_index, column))

    def _add(self, ordinal, value, cell):
        normal = _normal(value)
        if not normal:
            return
        index = len(self.entries)
        self.entries.append({"ordinal": ordinal, "key": (ordinal, self.content[ordinal].get("id")),
                             "value": normal, "cell": cell})
        self.exact[normal].append(index)
        for token in _tokens(value):
            self.postings[token].add(index)

    def match(self, value):
        normal = _normal(value)
        if normal in self.queries:
            return self.queries[normal]
        matches = list(self.exact.get(normal, []))
        if len(normal) >= 12:
            lists = [self.postings[token] for token in _tokens(value) if token in self.postings]
            candidates = min(lists, key=len) if lists else ()
            matches = sorted(set(matches) | {i for i in candidates if normal in self.entries[i]["value"]})
        self.queries[normal] = matches
        return matches


def _position_items(source_path: Path, pages: list[int]):
    """Optional Inspector API. Pages are one-based (unlike page Markdown API)."""
    import pdf_inspector
    return pdf_inspector.extract_text_with_positions(str(source_path), pages=pages)


def _get(item, name, default=None):
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _lines(items):
    """Keep native Inspector fragments and join visual lines within columns."""
    result = []
    invalid = False
    for item in items:
        try:
            if _get(item, "item_type", "text") != "text":
                # Inspector also reports image placeholders through this API;
                # their y/height are not text baselines or font measurements.
                continue
            text = str(_get(item, "text", ""))
            x, y, width, height = (float(_get(item, key)) for key in ("x", "y", "width", "height"))
            # Inspector y denotes baseline. Include the normal descender band.
            box = _box((x, y - height * 0.25, x + width, y + height))
            if box is None or width < 0 or height <= 0:
                invalid = True
                continue
            if text.strip():
                result.append({"text": text, "bbox": box, "baseline": y})
        except (TypeError, ValueError, OverflowError):
            invalid = True
    if not result:
        return [], invalid
    height = statistics.median(r["bbox"][3] - r["bbox"][1] for r in result)
    rows = []
    for item in sorted(result, key=lambda r: (-r["baseline"], r["bbox"][0])):
        if not rows or abs(rows[-1][0]["baseline"] - item["baseline"]) > height * 0.3:
            rows.append([item])
        else:
            rows[-1].append(item)
    joined = []
    for row in rows:
        group = []
        for item in sorted(row, key=lambda r: r["bbox"][0]):
            if group and item["bbox"][0] - group[-1]["bbox"][2] > height * 2:
                if len(group) > 1:
                    joined.append({"text": " ".join(i["text"] for i in group), "bbox": _union(i["bbox"] for i in group), "baseline": group[0]["baseline"]})
                group = []
            group.append(item)
        if len(group) > 1:
            joined.append({"text": " ".join(i["text"] for i in group), "bbox": _union(i["bbox"] for i in group), "baseline": group[0]["baseline"]})
    return result + joined, invalid


def _complete_adjacent_tail(members, value, ordered, spatial, ordinal):
    """Complete only an exact prefix through nearby consecutive text lines."""
    starts = [item for item in members if len(_normal(item["text"])) >= 16
              and value.startswith(_normal(item["text"]))]
    if not starts:
        return []
    first = max(starts, key=lambda item: len(_normal(item["text"])))
    cursor = len(_normal(first["text"]))
    added = []
    last = first
    height = first["bbox"][3] - first["bbox"][1]
    for _ in range(32):
        if cursor == len(value):
            return added
        area = (first["bbox"][0] - height * .5, last["baseline"] - height * 2,
                first["bbox"][2] + height * .5, last["baseline"] - height * .3)
        nearby = [ordered[i] for i in spatial.query(area)
                  if ordered[i]["baseline"] < last["baseline"] - height * .4
                  and abs((ordered[i]["bbox"][3] - ordered[i]["bbox"][1]) - height) <= height * .2
                  and abs(ordered[i]["bbox"][0] - first["bbox"][0]) <= height * 2.5]
        if not nearby:
            break
        baseline = max(item["baseline"] for item in nearby)
        candidates = [item for item in nearby if baseline - item["baseline"] <= height * .2
                      and item.get("ordinal", ordinal) == ordinal
                      and _normal(item["text"]) and value[cursor:].startswith(_normal(item["text"]))]
        if not candidates:
            break
        last = max(candidates, key=lambda item: len(_normal(item["text"])))
        cursor += len(_normal(last["text"]))
        added.append(last)
    return added if cursor == len(value) else []


def map_page(items, body_index: BodyIndex):
    lines, invalid = _lines(items)
    ordered = sorted(lines, key=lambda item: (-item["baseline"], item["bbox"][0]))
    seed_counts = defaultdict(set)
    for item in ordered:
        matches = body_index.match(item["text"])
        item["matches"] = matches
        item["entry"] = None
        normal = _normal(item["text"])
        if len(matches) == 1 and len(normal) >= 16:
            entry = body_index.entries[matches[0]]
            if entry["cell"] is None:
                seed_counts[entry["ordinal"]].add(normal)
    layout_members = defaultdict(list)
    for item in ordered:
        if len(item["matches"]) == 1 and len(_normal(item["text"])) >= 12:
            entry = body_index.entries[item["matches"][0]]
            if entry["cell"] is None:
                layout_members[entry["ordinal"]].append(item)
    layout_blocks = [{"ordinal": ordinal, "bbox": _union(item["bbox"] for item in members)}
                     for ordinal, members in layout_members.items()
                     if len({round(item["baseline"], 1) for item in members}) >= 2]
    # A lone common word/cell found elsewhere in the book cannot establish a
    # page location. Use substantial phrases or multiple independent fragments.
    for item in ordered:
        if len(item["matches"]) == 1:
            index = item["matches"][0]
            entry = body_index.entries[index]
            normal = _normal(item["text"])
            if entry["cell"] is None and (len(normal) >= 48 or len(normal) >= 16 and len(seed_counts[entry["ordinal"]]) >= 2):
                item["entry"] = index
    # A repeated sentence can be unique as a fragment of the wrong occurrence.
    # Preserve the maximum evidence chain in reading order, rather than letting
    # an isolated out-of-order hit redirect every subsequent short cell match.
    seeds = [(i, body_index.entries[item["entry"]]["ordinal"], len(_normal(item["text"])))
             for i, item in enumerate(ordered) if item["entry"] is not None]
    if seeds:
        ranks = {value: i + 1 for i, value in enumerate(sorted({v for _, v, _ in seeds}))}
        tree = [(0, -1)] * (len(ranks) + 1)
        parents = []
        best = (0, -1)
        for index, (_, ordinal, weight) in enumerate(seeds):
            rank, previous = ranks[ordinal], (0, -1)
            cursor = rank
            while cursor:
                previous = max(previous, tree[cursor])
                cursor -= cursor & -cursor
            value = (previous[0] + weight, index)
            parents.append(previous[1])
            best = max(best, value)
            while rank < len(tree):
                tree[rank] = max(tree[rank], value)
                rank += rank & -rank
        accepted = set()
        cursor = best[1]
        while cursor >= 0:
            accepted.add(seeds[cursor][0])
            cursor = parents[cursor]
        for index, _, _ in seeds:
            if index not in accepted:
                ordered[index]["entry"] = None
    # Ambiguous matches may only use an interval bounded by two proven anchors.
    left = None
    right_anchors = [None] * len(ordered)
    right = None
    for pos in range(len(ordered) - 1, -1, -1):
        right_anchors[pos] = right
        entry = ordered[pos]["entry"]
        if entry is not None:
            right = body_index.entries[entry]["ordinal"]
    for pos, item in enumerate(ordered):
        if item["entry"] is None and left is not None and right_anchors[pos] is not None:
            right = right_anchors[pos]
            if left <= right:
                matches = [i for i in item["matches"] if left <= body_index.entries[i]["ordinal"] <= right]
                if len(matches) == 1:
                    item["entry"] = matches[0]
        if item["entry"] is not None:
            left = body_index.entries[item["entry"]]["ordinal"]
    # Short words may fill coverage only inside the spatial extent of a proven
    # paragraph, or between already-proven neighboring body occurrences.
    proven = defaultdict(list)
    for item in ordered:
        if item["entry"] is not None:
            entry = body_index.entries[item["entry"]]
            if entry["cell"] is None:
                proven[entry["ordinal"]].append(item["bbox"])
    for item in ordered:
        if item["entry"] is not None:
            continue
        candidates = []
        for index in item["matches"]:
            entry = body_index.entries[index]
            boxes = proven.get(entry["ordinal"])
            if boxes and _overlap(_union(boxes), item["bbox"]):
                candidates.append(index)
        if len(candidates) == 1:
            item["entry"] = candidates[0]
    groups = defaultdict(list)
    for item in ordered:
        if item["entry"] is not None:
            entry = body_index.entries[item["entry"]]
            item["ordinal"] = entry["ordinal"]
            item["cell"] = entry["cell"]
            groups[entry["ordinal"]].append(item)
    blocks = []
    completion_counter = [0]
    completion_spatial = None
    for ordinal, members in groups.items():
        node = body_index.content[ordinal]
        if node.get("type") == "table":
            cells = {}
            for item in members:
                entry = body_index.entries[item["entry"]]
                # Repeated numeric labels alone cannot prove a table.
                if entry["cell"] is not None and any(c.isalpha() for c in entry["value"]):
                    old = cells.get(entry["cell"])
                    if old is None or len(_normal(item["text"])) > len(_normal(old["text"])):
                        cells[entry["cell"]] = item
            if len(cells) < 2:
                continue
            distinct = {_normal(item["text"]) for item in cells.values()}
            if len(distinct) < 2:
                continue
            pairs = sorted(cells.items())
            consistent_pairs = []
            for (a, first), (b, second) in zip(pairs, pairs[1:]):
                same_row = a[0] == b[0] and a[1] < b[1] and first["bbox"][2] <= second["bbox"][0]
                next_row = a[0] < b[0] and first["baseline"] > second["baseline"]
                if same_row or next_row:
                    consistent_pairs.extend((first, second))
            if not consistent_pairs:
                continue
            # Invalid/split cells degrade their own evidence rather than
            # invalidating every healthy fragment of a page-spanning table.
            members = list({id(item): item for item in consistent_pairs}.values())
        bbox = _union(item["bbox"] for item in members)
        value = _normal(node.get("raw_text") or node.get("text") or "")
        covered = {_normal(item["text"]) for item in members}
        full = any(text == value for text in covered)
        head = tail = full
        if not full and value:
            # Counting unique text avoids inflating coverage with joined/raw duplicates.
            ranges = []
            for text in covered:
                start = value.find(text)
                if start >= 0:
                    ranges.append((start, start + len(text)))
            end, count = 0, 0
            for start, stop in sorted(ranges):
                count += max(0, stop - max(start, end))
                end = max(end, stop)
            full = count >= len(value) * 0.9
            head = bool(ranges) and min(start for start, _ in ranges) == 0
            tail = bool(ranges) and max(stop for _, stop in ranges) == len(value)
        if head and not tail and node["type"] in {"paragraph", "list_item"}:
            # A common final line may not be unique across the document. It
            # can complete this occurrence only when exact text continues from
            # its proven prefix in the same nearby column, through its end.
            try:
                if completion_spatial is None:
                    cell = statistics.median(item["bbox"][3] - item["bbox"][1] for item in ordered) * 4
                    completion_spatial = _SpatialIndex([item["bbox"] for item in ordered], cell, completion_counter)
                added = _complete_adjacent_tail(members, value, ordered, completion_spatial, ordinal)
                if added:
                    for item in added:
                        item["ordinal"] = ordinal
                        item["cell"] = None
                    members.extend(item for item in added if item not in members)
                    bbox = _union(item["bbox"] for item in members)
                    full = head = tail = True
            except ImageBudgetExceeded:
                invalid = True
        baselines = {round(item["baseline"], 1) for item in members}
        blocks.append({"ordinal": ordinal, "bbox": bbox, "kind": node["type"],
                       "members": members, "complete": full and head and tail, "head": head, "tail": tail,
                       "line_height": statistics.median(item["bbox"][3] - item["bbox"][1] for item in members),
                       "prose": node["type"] in {"paragraph", "list_item"} and (full or head or tail) and len(baselines) >= 2})
    return {"items": ordered, "blocks": blocks, "layout_blocks": layout_blocks, "invalid": invalid,
            "checks": completion_counter[0],
            "body_index": body_index}


class _SpatialIndex:
    """Uniform grid with a shared, hard cap on insertion/query work per page."""

    def __init__(self, boxes, cell, counter):
        self.boxes, self.cell, self.counter = boxes, max(cell, 1.0), counter
        self.grid = defaultdict(list)
        self.large = []
        for index, box in enumerate(boxes):
            cells = self.cells(box)
            if cells is None:
                self.large.append(index)
                continue
            for key in cells:
                self.tick()
                self.grid[key].append(index)

    def tick(self):
        self.counter[0] += 1
        if self.counter[0] > MAX_NEIGHBOR_CHECKS:
            raise ImageBudgetExceeded("page neighbor cap")

    def cells(self, box):
        x0, y0, x1, y1 = (math.floor(c / self.cell) for c in box)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 4096:
            return None
        return ((x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))

    def query(self, box, margin=0):
        expanded = (box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin)
        cells = self.cells(expanded)
        candidates = set(self.large)
        if cells is None:
            candidates.update(range(len(self.boxes)))
        else:
            for key in cells:
                self.tick()
                candidates.update(self.grid.get(key, ()))
        result = []
        for index in candidates:
            self.tick()
            if _overlap(box, self.boxes[index], margin):
                result.append(index)
        return result


def _scan_page(page, pdfium):
    page_box = _box(page.get_bbox())
    if page_box is None:
        raise ValueError("invalid page bounds")
    graphics = []
    uncertain = False
    capped = False
    objects = 0
    raw = pdfium.raw
    for obj in page.get_objects(max_depth=15):
        objects += 1
        if objects > MAX_OBJECTS_PER_PAGE:
            capped = True
            break
        if obj.type == raw.FPDF_PAGEOBJ_TEXT:
            continue
        if obj.type not in {raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH,
                            raw.FPDF_PAGEOBJ_SHADING, raw.FPDF_PAGEOBJ_FORM}:
            uncertain = True
            continue
        try:
            bounds = _box(obj.get_bounds())
            if bounds is None:
                uncertain = True
                continue
            separator = False
            rectangle = False
            if obj.type == raw.FPDF_PAGEOBJ_PATH:
                count = raw.FPDFPath_CountSegments(obj)
                rectangle = count in {4, 5} and all(raw.FPDFPathSegment_GetType(raw.FPDFPath_GetPathSegment(obj, i)) != raw.FPDF_SEGMENT_BEZIERTO for i in range(count))
                if count == 2:
                    segment = raw.FPDFPath_GetPathSegment(obj, 1)
                    separator = raw.FPDFPathSegment_GetType(segment) == raw.FPDF_SEGMENT_LINETO
                # A page background alone is not evidence of a figure.
                if _contains(bounds, page_box, 1.0):
                    continue
            if obj.type == raw.FPDF_PAGEOBJ_FORM or getattr(obj, "level", 0):
                uncertain = True
            clip = raw.FPDFPageObj_GetClipPath(obj)
            if clip and raw.FPDFClipPath_CountPaths(clip) > 0:
                # Ordinary rectangular clipping is a known enclosure; complex
                # or transformed clips remain uncertain and use page previews.
                if raw.FPDFClipPath_CountPaths(clip) != 1 or raw.FPDFClipPath_CountPathSegments(clip, 0) not in {4, 5}:
                    uncertain = True
                elif any(raw.FPDFPathSegment_GetType(raw.FPDFClipPath_GetPathSegment(clip, 0, i)) == raw.FPDF_SEGMENT_BEZIERTO
                         for i in range(raw.FPDFClipPath_CountPathSegments(clip, 0))):
                    uncertain = True
            graphics.append({"bbox": bounds, "kind": obj.type, "separator": separator, "rectangle": rectangle})
        except Exception:
            uncertain = True
    return {"bbox": page_box, "graphics": graphics, "uncertain": uncertain,
            "capped": capped, "objects": objects, "rotation": page.get_rotation()}


def plan_page(geometry, mapped, deadline):
    """Propose complete whitespace-bounded bands, or explicit page fallback.

    Connected graphic components only seed the search. The complete band is
    expanded to external body blocks/page edges, so disconnected panels and
    offset labels are not lost by the connectivity radius. A block is external
    only when complete Inspector prose occupies multiple visual lines and is
    separated from the graphics; a unique caption string alone is insufficient.
    """
    _check_deadline(deadline)
    graphics = geometry["graphics"]
    seeds = [i for i, item in enumerate(graphics) if not item["separator"]]
    result = {"regions": [], "supplement": False, "inline_glyph": False, "checks": 0}
    if not seeds and len(graphics) < 2 and not geometry["uncertain"] and not geometry["capped"]:
        return result
    if geometry["uncertain"] or geometry["capped"] or geometry["rotation"] or not mapped["items"]:
        result["supplement"] = True
        return result
    heights = [item["bbox"][3] - item["bbox"][1] for item in mapped["items"]]
    tolerance = statistics.median(heights) * 0.5
    counter = [mapped.get("checks", 0)]
    try:
        text_spatial = _SpatialIndex([item["bbox"] for item in mapped["items"]], tolerance * 4, counter)
        # Filled narrow rectangles are also used for ordinary text underline /
        # strikeout. They are not standalone figure seeds when their entire box
        # is owned by a positioned text line. Connected drawing rules remain in
        # the spatial index and will be included with their actual figure.
        for item in graphics:
            box = item["bbox"]
            width, height = box[2] - box[0], box[3] - box[1]
            if item.get("rectangle") and min(width, height) <= tolerance * 0.2 and max(width, height) >= tolerance:
                item["separator"] = True
            if item["kind"] == 2 and box[3] - box[1] <= tolerance * 0.3:
                # Text advances and visible glyph bounds differ slightly. A
                # short final underline can overhang the last glyph by a small
                # fraction of the line height without becoming a figure.
                if any(_contains((line[0] - tolerance * 0.5, line[1], line[2] + tolerance * 0.5, line[3]), box, tolerance * 0.1)
                       for i in text_spatial.query(box, tolerance * 0.5)
                       for line in [mapped["items"][i]["bbox"]]):
                    item["separator"] = True
                    item["text_decoration"] = True
        spatial = _SpatialIndex([item["bbox"] for item in graphics], tolerance * 4, counter)
        # A filled/stroked frame can surround ordinary Inspector body prose.
        # Require multiple substantial owned lines and no other visual content
        # or connectors crossing that frame; color and document templates play
        # no role. A chart panel/flowchart box with drawings remains a seed.
        text_frames = set()
        for index, item in enumerate(graphics):
            if item.get("rectangle") and not item["separator"]:
                owners = defaultdict(set)
                for line_index in text_spatial.query(item["bbox"]):
                    line = mapped["items"][line_index]
                    if _contains(item["bbox"], line["bbox"], tolerance) and len(_normal(line["text"])) >= 16:
                        matches = set(line.get("matches", ()))
                        # Rich formatting may interrupt a visual line while
                        # leaving its exact phrases in Inspector body/cells.
                        # These fragment matches classify decoration only; they
                        # never establish placement or import source text.
                        if not matches and "body_index" in mapped:
                            words = line["text"].split()
                            for start in range(0, len(words), 4):
                                fragment = " ".join(words[start:start + 4])
                                if len(_normal(fragment)) >= 16:
                                    matches.update(mapped["body_index"].match(fragment))
                        for match in matches:
                            owners[match].add(round(line["baseline"], 1))
                if any(len(rows) >= 2 for rows in owners.values()):
                    text_frames.add(index)
        for index in list(text_frames):
            box = graphics[index]["bbox"]
            def frame_edge(other):
                edge = graphics[other]
                b = edge["bbox"]
                horizontal = abs(b[0] - box[0]) <= tolerance and abs(b[2] - box[2]) <= tolerance and min(abs(b[1] - box[1]), abs(b[3] - box[3])) <= tolerance
                vertical = abs(b[1] - box[1]) <= tolerance and abs(b[3] - box[3]) <= tolerance and min(abs(b[0] - box[0]), abs(b[2] - box[2])) <= tolerance
                return edge["separator"] and (horizontal or vertical)
            neighbors = spatial.query(box, tolerance)
            if any(other != index and other not in text_frames and not graphics[other].get("text_decoration") and not frame_edge(other)
                   for other in neighbors):
                continue
            graphics[index]["separator"] = True
            graphics[index]["text_decoration"] = True
            for other in neighbors:
                if frame_edge(other):
                    graphics[other]["text_decoration"] = True
        seeds = [i for i, item in enumerate(graphics) if not item["separator"]]
        if not seeds:
            # A chart can consist entirely of straight paths. Connected axes /
            # line series are visual seeds. Isolated rules and margin change
            # bars do not become a figure merely because there are several.
            rules = [i for i, item in enumerate(graphics) if not item.get("text_decoration")]
            rule_set = set(rules)
            for index in rules:
                a = graphics[index]["bbox"]
                for other in spatial.query(a, tolerance):
                    b = graphics[other]["bbox"]
                    if other != index and other in rule_set and (a[2] - a[0] > a[3] - a[1]) != (b[2] - b[0] > b[3] - b[1]):
                        seeds.extend((index, other))
            seeds = sorted(set(seeds))
            if not seeds:
                result["checks"] = counter[0]
                return result
        components = []
        connected = set()
        component_for_object = {}
        remaining = set(seeds)
        while remaining:
            _check_deadline(deadline)
            index = min(remaining)
            remaining.remove(index)
            component, pending = {index}, [index]
            while pending:
                current = pending.pop()
                for neighbor in spatial.query(graphics[current]["bbox"], tolerance):
                    if neighbor not in component and not graphics[neighbor].get("text_decoration"):
                        component.add(neighbor)
                        remaining.discard(neighbor)
                        pending.append(neighbor)
            component_box = _union(graphics[i]["bbox"] for i in component)
            components.append(component_box)
            for member in component:
                component_for_object[member] = component_box
            connected.update(component)
        component_spatial = _SpatialIndex(components, tolerance * 4, counter)
        # Bitmap objects inside a text line may be missing characters. Keep the
        # whole page and an explicit loss warning, never invent inline text.
        for index in seeds:
            graphic = graphics[index]
            if graphic["kind"] != 3:  # FPDF_PAGEOBJ_IMAGE
                continue
            box = graphic["bbox"]
            component_box = component_for_object.get(index, box)
            if component_box[2] - component_box[0] > tolerance * 3 or component_box[3] - component_box[1] > tolerance * 3:
                continue
            for candidate in text_spatial.query(box):
                line = mapped["items"][candidate]["bbox"]
                if _contains(line, box, tolerance * 0.25) and box[2] - box[0] <= (line[3] - line[1]) * 1.5:
                    result["inline_glyph"] = result["supplement"] = True
        if result["inline_glyph"]:
            return result
        page_box = geometry["bbox"]
        blocks = mapped["blocks"]
        possible_prose = [block for block in blocks if block["prose"] and block["complete"]
                          and not component_spatial.query(block["bbox"])]
        # A complete text match is not sufficient to call a caption external.
        # Require independent body-flow evidence: another paragraph in the same
        # column, at the same line scale and with matching full-line extents.
        # Offset / small-print labels remain inside the enclosing visual band.
        strong = []
        for block in possible_prose:
            for other in possible_prose:
                counter[0] += 1
                if counter[0] > MAX_NEIGHBOR_CHECKS:
                    raise ImageBudgetExceeded("page neighbor cap")
                a, b = block["bbox"], other["bbox"]
                if block is not other and abs(a[0] - b[0]) <= tolerance and abs(a[2] - b[2]) <= tolerance * 2 and abs(block["line_height"] - other["line_height"]) <= tolerance * 0.25:
                    strong.append(block)
                    break
        seen = set()
        seen_bands = set()
        strong_spatial = _SpatialIndex([b["bbox"] for b in strong], tolerance * 4, counter)
        # Side-by-side independent text columns have a different reading order
        # from a single full-width band. Keep a page preview when that order is
        # not established; never include an unrelated column as a precise crop.
        exterior_layout = [b for b in mapped.get("layout_blocks", strong)
                           if not component_spatial.query(b["bbox"])]
        for pos, block in enumerate(exterior_layout):
            for other in exterior_layout[pos + 1:]:
                counter[0] += 1
                if counter[0] > MAX_NEIGHBOR_CHECKS:
                    raise ImageBudgetExceeded("page neighbor cap")
                a, b = block["bbox"], other["bbox"]
                if a[1] < b[3] and b[1] < a[3] and (a[2] + tolerance < b[0] or b[2] + tolerance < a[0]):
                    result["supplement"] = True
                    result["checks"] = counter[0]
                    return result
        for component in sorted(components, key=lambda box: (-box[3], box[0])):
            _check_deadline(deadline)
            # Any body block overlapping graphic geometry belongs to the region.
            # External boundaries require whitespace and complete body blocks.
            column = (component[0], page_box[1], component[2], page_box[3])
            column_blocks = [strong[i] for i in strong_spatial.query(column)]
            column_components = [components[i] for i in component_spatial.query(column)]
            above = [b for b in column_blocks if b["tail"] and b["bbox"][1] > component[3] + tolerance
                     and b["bbox"][0] < component[2] and b["bbox"][2] > component[0]]
            below = [b for b in column_blocks if b["head"] and b["bbox"][3] < component[1] - tolerance
                     and b["bbox"][0] < component[2] and b["bbox"][2] > component[0]]
            previous = min(above, key=lambda b: b["bbox"][1], default=None)
            following = max(below, key=lambda b: b["bbox"][3], default=None)
            # A multiline caption between separated panels is inside the
            # complete figure, even if Inspector represented it as a paragraph.
            if previous and any(other[1] > previous["bbox"][3] and other[0] < component[2] and other[2] > component[0]
                                for other in column_components):
                previous = None
            if following and any(other[3] < following["bbox"][1] and other[0] < component[2] and other[2] > component[0]
                                 for other in column_components):
                following = None
            # At least one complete body boundary is needed for inline order.
            if previous is None and following is None:
                # Side association requires a complete independently established
                # body column and an empty vertical gutter. All visual/text
                # items on the figure side are included, so detached labels are
                # not lost. Conflicting/straddling items use page preview.
                adjacent = [b for b in strong if b["complete"] and b["bbox"][1] < component[3]
                            and b["bbox"][3] > component[1]
                            and (b["bbox"][0] > component[2] + tolerance or b["bbox"][2] < component[0] - tolerance)]
                if len(adjacent) == 1:
                    neighbor = adjacent[0]
                    left_side = component[2] < neighbor["bbox"][0]
                    split = (component[2] + neighbor["bbox"][0]) / 2 if left_side else (component[0] + neighbor["bbox"][2]) / 2
                    side = (page_box[0], page_box[1], split, page_box[3]) if left_side else (split, page_box[1], page_box[2], page_box[3])
                    graphic_indexes = [i for i in spatial.query(side) if i in connected]
                    text_indexes = text_spatial.query(side)
                    side_boxes = [graphics[i]["bbox"] for i in graphic_indexes] + [mapped["items"][i]["bbox"] for i in text_indexes]
                    all_seeds_inside = all(_contains(side, graphics[i]["bbox"]) for i in seeds)
                    if side_boxes and all_seeds_inside and all(_contains(side, b) for b in side_boxes):
                        enclosed = _union(side_boxes)
                        box = (max(side[0], enclosed[0] - tolerance), max(side[1], enclosed[1] - tolerance),
                               min(side[2], enclosed[2] + tolerance), min(side[3], enclosed[3] + tolerance))
                        if not any(_overlap(box, b["bbox"]) for b in blocks if b["ordinal"] != neighbor["ordinal"]):
                            result["regions"].append({"bbox": box, "slot": neighbor["ordinal"] + (0 if left_side else 1)})
                            continue
                result["supplement"] = True
                continue
            high = previous["bbox"][1] if previous else page_box[3]
            low = following["bbox"][3] if following else page_box[1]
            band_key = (high, low)
            if band_key in seen_bands:
                continue
            seen_bands.add(band_key)
            # Inspect the entire page-width layout band, not just the component.
            # Multi-column text inside it cannot silently be assigned outside.
            band = (page_box[0], low + tolerance * 0.05, page_box[2], high - tolerance * 0.05)
            # Isolated page rules / change bars are independently outside the
            # figure. Rules connected to any graphic seed remain included.
            members = [i for i in spatial.query(band) if not graphics[i]["separator"] or i in connected]
            texts = text_spatial.query(band)
            if any(not _contains(band, graphics[i]["bbox"]) for i in members):
                result["supplement"] = True
                continue
            if any(not _contains(band, mapped["items"][i]["bbox"]) for i in texts):
                result["supplement"] = True
                continue
            included = [graphics[i]["bbox"] for i in members] + [mapped["items"][i]["bbox"] for i in texts]
            if not included:
                result["supplement"] = True
                continue
            enclosed = _union(included)
            box = (max(page_box[0], enclosed[0] - tolerance), max(page_box[1], enclosed[1] - tolerance),
                   min(page_box[2], enclosed[2] + tolerance), min(page_box[3], enclosed[3] + tolerance))
            # Padding cannot cut through a boundary block, and crops cannot
            # cross columns whose independently mapped reading order conflicts.
            if previous and _overlap(box, previous["bbox"]) or following and _overlap(box, following["bbox"]):
                result["supplement"] = True
                continue
            # A page-width band can also contain a following section, an
            # introduction or a list lead-in. Merely mapping that text does
            # not make it part of the figure. A heading outside the actual
            # graphic enclosure, or prose-scale text with no such enclosure,
            # leaves the boundary unproven. Small captions/notes remain in the
            # complete crop; unknown normal-size captions use a page preview.
            body_height = min(b["line_height"] for b in (previous, following) if b is not None)
            def enclosed_by_graphics(text_box):
                return any(_contains(components[i], text_box, tolerance * 0.1)
                           for i in component_spatial.query(text_box))
            unowned_heading = any(b["kind"] == "heading" and _overlap(box, b["bbox"])
                                  and any(c.isalpha() for m in b["members"] for c in m["text"])
                                  and not enclosed_by_graphics(b["bbox"]) for b in blocks)
            closed_between = previous is not None and following is not None and set(
                range(previous["ordinal"] + 1, following["ordinal"])) <= {
                    b["ordinal"] for b in blocks if _contains(box, b["bbox"])}
            unowned_body_text = any(any(c.isalpha() for c in mapped["items"][i]["text"])
                                   and mapped["items"][i]["bbox"][3] - mapped["items"][i]["bbox"][1] >= body_height * 0.95
                                   and not enclosed_by_graphics(mapped["items"][i]["bbox"])
                                   and (mapped["items"][i].get("matches") or not closed_between)
                                   for i in texts)
            if unowned_heading or unowned_body_text:
                result["supplement"] = True
                continue
            # Tables may have separate page/region fragments. Keep at least two
            # independently matched ordered cells in this enclosure, without
            # letting one outside/invalid cell hit invalidate healthy cells.
            local_blocks = []
            for block in blocks:
                if block["kind"] != "table":
                    local_blocks.append(block)
                    continue
                members = [item for item in block["members"] if _contains(box, item["bbox"])]
                if len({item.get("cell") for item in members}) >= 2 and len({_normal(item["text"]) for item in members}) >= 2:
                    local_blocks.append({**block, "bbox": _union(item["bbox"] for item in members), "members": members})
            contained = [b for b in local_blocks if _contains(box, b["bbox"])
                         and (b["kind"] == "table" or b["complete"])]
            if any(_overlap(box, b["bbox"]) and not _contains(box, b["bbox"]) for b in local_blocks):
                result["supplement"] = True
                continue
            ordered = sorted(contained, key=lambda b: (-b["bbox"][3], b["bbox"][0]))
            ordinals = [b["ordinal"] for b in ordered]
            if ordinals != sorted(ordinals) or previous and following and previous["ordinal"] >= following["ordinal"]:
                result["supplement"] = True
                continue
            if previous and any(i <= previous["ordinal"] for i in ordinals) or following and any(i >= following["ordinal"] for i in ordinals):
                result["supplement"] = True
                continue
            # Table/content association wins, then a proven predecessor/successor.
            slot = max(ordinals) + 1 if ordinals else previous["ordinal"] + 1 if previous else following["ordinal"]
            # Partial mappings do not establish a later slot, but they still
            # reveal when that slot cuts through the enclosed content group.
            # A known table continuation below the crop likewise prevents a
            # local cell fragment from standing in for its whole occurrence.
            if any(b["ordinal"] >= slot and _overlap(box, b["bbox"]) for b in local_blocks) or any(
                b["kind"] == "table" and b["ordinal"] in ordinals
                and any(m["bbox"][3] < box[1] for m in b["members"]) for b in blocks):
                result["supplement"] = True
                continue
            key = tuple(round(v, 3) for v in box)
            if key not in seen:
                seen.add(key)
                result["regions"].append({"bbox": box, "slot": slot})
        if mapped["invalid"]:
            result["supplement"] = True
        result["checks"] = counter[0]
        return result
    except ImageBudgetExceeded:
        result["regions"] = []
        result["supplement"] = True
        result["capped"] = True
        result["checks"] = counter[0]
        return result


def _warning(result, code, message, loss=False, unit=None):
    warning = {"code": code, "message": message, "content_loss": loss}
    if unit is not None:
        warning["source_unit"] = unit["id"]
        unit.setdefault("warnings", []).append(warning)
        if unit.get("status") == "complete":
            unit["status"] = "warning"
    result.setdefault("warnings", []).append(warning)


def _page_ranges(pages):
    ranges = []
    for page in sorted(set(pages)):
        if ranges and page == ranges[-1][-1] + 1:
            ranges[-1].append(page)
        else:
            ranges.append([page])
    return ",".join(str(r[0]) if len(r) == 1 else f"{r[0]}-{r[-1]}" for r in ranges)


def _save_page(page, plans, page_number, unit, image_dir, document_id, result, metrics, deadline):
    """Render once, then crop in bitmap coordinates supplied by PDFium."""
    _check_deadline(deadline)
    width, height = page.get_size()
    scale = min(300 / 72, 4096 / max(width, height))
    metrics["render_calls"] += 1
    metrics["rendered_pages"].append(page_number)
    bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 255))
    outputs = []
    try:
        image = bitmap.to_pil()
        posconv = bitmap.get_posconv(page)
        for ordinal, plan in enumerate(plans, 1):
            _check_deadline(deadline)
            if len(result["assets"]) >= MAX_ASSETS:
                raise ImageBudgetExceeded("image asset count cap")
            box = plan["bbox"]
            corners = [posconv.to_bitmap(x, y) for x, y in ((box[0], box[1]), (box[0], box[3]), (box[2], box[1]), (box[2], box[3]))]
            crop = (max(0, min(p[0] for p in corners)), max(0, min(p[1] for p in corners)),
                    min(image.width, max(p[0] for p in corners)), min(image.height, max(p[1] for p in corners)))
            if crop[0] >= crop[2] or crop[1] >= crop[3]:
                raise ValueError("empty rendered image region")
            locator = {"source_unit_id": unit["id"], "page": page_number,
                       "bbox": [round(v, 3) for v in box], "extraction_method": "pdfium_page_render",
                       "placement": "pdf_page_supplement" if plan.get("supplement") else "body_region"}
            asset_id = stable_id("asset", document_id, locator, "image", ordinal)
            target = image_dir / f"{asset_id}.png"
            with image.crop(crop).convert("RGB") as cropped:
                cropped.save(target, format="PNG")
            size = target.stat().st_size
            if metrics["asset_bytes"] + size > MAX_ASSET_BYTES:
                target.unlink()
                raise ImageBudgetExceeded("image asset byte cap")
            metrics["asset_bytes"] += size
            asset = {"asset_id": asset_id, "type": "image", "path": f"assets/images/{asset_id}.png",
                     "sha256": sha256_file(target), "media_type": "image/png", "source_locator": locator,
                     "alt": f"PDF page {page_number}" + (" preview" if plan.get("supplement") else " figure"), "caption": ""}
            result["assets"].append(asset)
            outputs.append((plan, {"id": "pending", "type": "image", "asset_id": asset_id, "source_locator": locator}))
    finally:
        bitmap.close()
    return outputs


def enhance_pdf_images(source_path: Path, body: dict[str, Any], image_dir: Path,
                       settings: dict[str, Any], deadline: float) -> dict[str, Any]:
    """Enhance a valid, image-free extraction inside the bounded image worker."""
    from pdf_inspector_adapter import _extract_pdf_image_support, _reassign_content_ids
    result = copy.deepcopy(body)
    if settings.get("mode", "auto") == "off":
        return result
    document_id = settings["document_id"]
    units = {int(u["locator"]["page"]): u for u in result["source_units"]
             if isinstance(u.get("locator", {}).get("page"), int)}
    _check_deadline(deadline)
    if settings.get("mode") == "objects":
        support = _extract_pdf_image_support(str(source_path), document_id, image_dir, units,
                                             max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES, deadline=deadline)
        supplements = [n for n in result["content"] if n.get("source_locator", {}).get("placement") == "unanchored_supplement"]
        content = [n for n in result["content"] if n.get("source_locator", {}).get("placement") != "unanchored_supplement"]
        pages = defaultdict(list)
        for node in support.get("content", []):
            page = node.get("source_locator", {}).get("page")
            if page in units:
                pages[page].append(node)
        content, stats = merge_cached(content, pages, units)
        for page, count in stats["unpositioned_by_page"].items():
            if count:
                _warning(result, "pdf_image_position_ambiguous", f"Page {page} left {count} bundle image(s) unpositioned", unit=units[page])
                warnings = units[page]["warnings"]
                warning = warnings.pop()
                before = next((i for i, item in enumerate(warnings) if item.get("code") == "pdf_inspector_alignment_unresolved"), len(warnings))
                warnings.insert(before, warning)
        result["content"] = content + supplements
        result["assets"] = support.get("assets", [])
        # The legacy adapter flattened page warnings only after placement.
        # Keep this ordering, followed by document-level and export warnings.
        old_unit_warnings = [w for unit in body["source_units"] for w in unit.get("warnings", [])]
        document_warnings = [w for w in body.get("warnings", []) if w not in old_unit_warnings]
        result["warnings"] = [w for unit in result["source_units"] for w in unit.get("warnings", [])]
        result["warnings"].extend(w for w in document_warnings if w not in result["warnings"])
        result["warnings"].extend(w for w in support.get("warnings", []) if w.get("code") == "pdf_image_extraction_failed" and w not in result["warnings"])
        _reassign_content_ids(result["content"], result["tables"], document_id)
        result["image_metrics"] = {"mode": "objects", **stats}
        return result
    metrics = {"mode": "auto", "pages_scanned": 0, "candidate_pages": [], "precise_regions": 0,
               "page_supplements": 0, "render_calls": 0, "rendered_pages": [], "capped_pages": [],
               "neighbor_checks": 0, "asset_bytes": 0, "processed_pages": [], "unprocessed_pages": [],
               "stages_seconds": {"scan": 0.0, "positions": 0.0, "mapping": 0.0, "render_save": 0.0}}
    result["image_metrics"] = metrics
    result.setdefault("assets", [])
    try:
        import pypdfium2 as pdfium
    except ImportError:
        _warning(result, "pdf_images_unavailable", "Optional PDF image capability is unavailable; body retained", True)
        return result
    started = time.monotonic()
    body_index = BodyIndex(result)
    image_dir.mkdir(parents=True, exist_ok=True)
    slots = defaultdict(list)
    supplements = []
    document = pdfium.PdfDocument(str(source_path))
    total = len(document)
    next_page = 1
    try:
        while next_page <= total:
            _check_deadline(deadline)
            batch = []
            scan_start = time.monotonic()
            while next_page <= total and len(batch) < 32:
                _check_deadline(deadline)
                page_number = next_page
                next_page += 1
                page = document.get_page(page_number - 1)
                try:
                    geometry = _scan_page(page, pdfium)
                    metrics["pages_scanned"] += 1
                    if len(geometry["graphics"]) >= 2 or any(not g["separator"] for g in geometry["graphics"]) or geometry["uncertain"] or geometry["capped"]:
                        batch.append((page_number, geometry))
                        metrics["candidate_pages"].append(page_number)
                except Exception:
                    batch.append((page_number, {"bbox": page.get_bbox(), "graphics": [], "uncertain": True, "capped": False, "rotation": page.get_rotation()}))
                finally:
                    page.close()
            metrics["stages_seconds"]["scan"] += time.monotonic() - scan_start
            if not batch:
                continue
            positions = defaultdict(list)
            position_start = time.monotonic()
            try:
                values = _position_items(source_path, [number for number, _ in batch])
                for item in values:
                    number = _get(item, "page")
                    if number in units:
                        positions[number].append(item)
            except Exception:
                # Positioning is optional, including in force OCR mode.
                pass
            metrics["stages_seconds"]["positions"] += time.monotonic() - position_start
            for page_number, geometry in batch:
                _check_deadline(deadline)
                unit = units.get(page_number)
                if unit is None:
                    metrics["unprocessed_pages"].append(page_number)
                    continue
                mapping_start = time.monotonic()
                mapped = map_page(positions[page_number], body_index)
                planned = plan_page(geometry, mapped, deadline)
                metrics["stages_seconds"]["mapping"] += time.monotonic() - mapping_start
                metrics["neighbor_checks"] += planned["checks"]
                if planned.get("capped") or geometry["capped"]:
                    metrics["capped_pages"].append(page_number)
                if planned["inline_glyph"]:
                    _warning(result, "pdf_inline_image_unrecovered", f"Page {page_number} contains inline image text; retained as page preview", True, unit)
                plans = list(planned["regions"])
                if planned["supplement"]:
                    plans.append({"bbox": geometry["bbox"], "supplement": True})
                page = None
                rendering_start = time.monotonic()
                try:
                    if plans:
                        page = document.get_page(page_number - 1)
                        outputs = _save_page(page, plans, page_number, unit, image_dir, document_id, result, metrics, deadline)
                        for plan, node in outputs:
                            if plan.get("supplement"):
                                supplements.append(node)
                                metrics["page_supplements"] += 1
                            else:
                                slots[plan["slot"]].append(node)
                                metrics["precise_regions"] += 1
                        if planned["supplement"]:
                            _warning(result, "pdf_images_page_supplement", f"Page {page_number} figures retained in a supplementary page preview; body text may repeat", unit=unit)
                    metrics["processed_pages"].append(page_number)
                except ImageBudgetExceeded:
                    raise
                except Exception:
                    metrics["unprocessed_pages"].append(page_number)
                    _warning(result, "pdf_images_render_failed", f"Page {page_number} visual recovery failed; body retained", True, unit)
                finally:
                    metrics["stages_seconds"]["render_save"] += time.monotonic() - rendering_start
                    if page is not None:
                        page.close()
    except ImageBudgetExceeded:
        # Unfinished candidate pages plus unscanned pages are honest unknowns.
        metrics["unprocessed_pages"] = sorted((set(metrics["candidate_pages"]) - set(metrics["processed_pages"])) | set(range(next_page, total + 1)))
        _warning(result, "pdf_images_unfinished", f"PDF image budget exhausted; unprocessed pages {_page_ranges(metrics['unprocessed_pages']) or 'unknown'}; body retained", True)
    finally:
        document.close()
    content = []
    for ordinal in range(len(result["content"]) + 1):
        content.extend(slots.get(ordinal, []))
        if ordinal < len(result["content"]):
            content.append(result["content"][ordinal])
    result["content"] = content + sorted(supplements, key=lambda n: n["source_locator"]["page"])
    # A failed page may have written assets before completion. Only accepted
    # complete image references survive; unaccepted files are removed here.
    used = {node["asset_id"] for node in result["content"] if node.get("type") == "image"}
    accepted = []
    for asset in result["assets"]:
        if asset["asset_id"] in used:
            accepted.append(asset)
        else:
            (image_dir / Path(asset["path"]).name).unlink(missing_ok=True)
    result["assets"] = accepted
    _reassign_content_ids(result["content"], result["tables"], document_id)
    metrics["stages_seconds"]["enhancement"] = time.monotonic() - started
    return result
