"""MarkItDown-backed adapters and Markdown-to-canonical parsing."""
from __future__ import annotations

import importlib.metadata
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from canonical import make_text_fields, normalize_canonical_text, stable_id, title_from_markdown
from ooxml_images import OOXML_SUFFIXES, create_sanitized_ooxml_copy, extract_ooxml_images


_MARKITDOWN = None


def get_markitdown():
    global _MARKITDOWN
    if _MARKITDOWN is None:
        from markitdown import MarkItDown

        _MARKITDOWN = MarkItDown()
    return _MARKITDOWN


def markitdown_version() -> str:
    try:
        return importlib.metadata.version("markitdown")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


def convert_basic(source: str) -> str:
    return get_markitdown().convert(source).text_content


def _warning(code: str, message: str, source_unit: str, content_loss: bool) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "content_loss": content_loss,
        "source_unit": source_unit,
    }


def inspect_ooxml_features(path: Path, source_unit: str) -> list[dict[str, Any]]:
    """Cheaply detect OOXML parts known not to be preserved by MarkItDown."""
    if path.suffix.lower() not in {".docx", ".xlsx", ".pptx"} or not zipfile.is_zipfile(path):
        return []
    warnings: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as package:
        names = set(package.namelist())
        comment_parts = [name for name in names if "comment" in name.lower() and name.endswith(".xml")]
        if comment_parts:
            warnings.append(
                _warning(
                    "office_comments_not_preserved",
                    "Office comments were detected and are not preserved",
                    source_unit,
                    True,
                )
            )
        if "word/document.xml" in names:
            xml = package.read("word/document.xml")
            if b"<w:ins" in xml or b"<w:del" in xml:
                warnings.append(
                    _warning(
                        "office_tracked_changes_not_preserved",
                        "Tracked changes were detected and cannot be represented faithfully",
                        source_unit,
                        True,
                    )
                )
    return warnings


def _numeric_value(value: str) -> int | float | None:
    candidate = value.strip().replace(",", "")
    negative = candidate.startswith("(") and candidate.endswith(")")
    if negative:
        candidate = candidate[1:-1]
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", candidate):
        return None
    number = float(candidate)
    if negative:
        number = -number
    return int(number) if number.is_integer() else number


def _inline_content(token) -> tuple[str, str, list[str], str]:
    images: list[str] = []
    visible: list[str] = []
    text_only: list[str] = []
    for child in token.children or []:
        if child.type == "image":
            images.append(child.content.strip())
            if child.content.strip():
                visible.append(child.content.strip())
        elif child.type == "code_inline":
            value = f"`{child.content}`"
            visible.append(value)
            text_only.append(value)
        elif child.type in {"text", "softbreak", "hardbreak", "html_inline"}:
            value = "\n" if child.type in {"softbreak", "hardbreak"} else child.content
            visible.append(value)
            text_only.append(value)
        else:
            value = child.content or ""
            visible.append(value)
            text_only.append(value)
    return (
        token.content.strip(),
        ("".join(visible).strip() if images else token.content.strip()),
        images,
        "".join(text_only).strip(),
    )


def markdown_to_canonical(
    markdown: str,
    document_id: str,
    mode: str,
    source_kind: str,
    source_index: int = 1,
    image_assets: list[dict[str, Any]] | None = None,
    image_occurrences: list[str] | None = None,
    warn_unexported_images: bool = False,
) -> dict[str, Any]:
    """Parse MarkItDown output through markdown-it-py into canonical nodes."""
    from markdown_it import MarkdownIt

    unit_locator = {"kind": source_kind, "index": source_index}
    unit_id = stable_id("unit", document_id, unit_locator, "document", source_index)
    source_unit = {
        "id": unit_id,
        "type": "document",
        "index": source_index,
        "locator": unit_locator,
        "status": "complete",
        "warnings": [],
    }
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)
    content: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    assets = list(image_assets or [])
    assets_by_id = {item["asset_id"]: item for item in assets}
    asset_occurrences = list(image_occurrences or [])
    export_images = image_assets is not None
    image_cursor = 0
    occurrence = 0

    def locator(token) -> dict[str, Any]:
        value: dict[str, Any] = {"source_unit_id": unit_id}
        if token.map:
            value.update({"line_start": int(token.map[0]) + 1, "line_end": int(token.map[1])})
        return value

    def add_text(kind: str, raw: str, token, cleaned: str | None = None, **extra: Any) -> None:
        nonlocal occurrence
        if not raw.strip() and kind != "code":
            return
        occurrence += 1
        source_locator = locator(token)
        node = {
            "id": stable_id("node", document_id, source_locator, kind, occurrence),
            "type": kind,
            "source_locator": source_locator,
            **make_text_fields(raw, (raw.strip() if kind != "code" else raw) if cleaned is None else cleaned, mode, defer=True),
            **extra,
        }
        content.append(node)

    def add_image(alt: str, token=None, inferred: bool = False) -> bool:
        nonlocal image_cursor, occurrence
        if image_cursor >= len(asset_occurrences):
            return False
        asset_id = asset_occurrences[image_cursor]
        image_cursor += 1
        asset = assets_by_id.get(asset_id)
        if asset is None:
            return False
        if alt and not str(asset.get("alt") or "").strip():
            asset["alt"] = alt
        occurrence += 1
        source_locator = locator(token) if token is not None else {"source_unit_id": unit_id}
        source_locator.update(
            {
                "package_part": asset["source_locator"].get("package_part", ""),
                "image_occurrence": image_cursor,
            }
        )
        if inferred:
            source_locator["position_inferred"] = True
        content.append(
            {
                "id": stable_id("node", document_id, source_locator, "image", occurrence),
                "type": "image",
                "source_locator": source_locator,
                "asset_id": asset_id,
            }
        )
        return True

    def add_inline_images(images: list[str], token) -> None:
        unresolved = 0
        for alt in images:
            if not add_image(alt, token):
                unresolved += 1
        if unresolved and export_images:
            warnings.append(
                _warning(
                    "office_image_reference_unresolved",
                    f"{unresolved} Office image reference(s) could not be matched to exported package media",
                    unit_id,
                    True,
                )
            )

    index = 0
    list_stack: list[tuple[bool, int]] = []
    while index < len(tokens):
        token = tokens[index]
        if token.type == "bullet_list_open":
            list_stack.append((False, 1))
        elif token.type == "ordered_list_open":
            start = int(token.attrGet("start") or 1)
            list_stack.append((True, start))
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            if list_stack:
                list_stack.pop()
        elif token.type == "heading_open" and index + 1 < len(tokens):
            inline = tokens[index + 1]
            raw, visible, images, text_only = _inline_content(inline)
            cleaned = text_only if export_images and images else visible
            if cleaned or not images:
                add_text("heading", raw, token, cleaned=cleaned, level=int(token.tag[1:]))
            if images and export_images:
                add_inline_images(images, token)
            elif images and warn_unexported_images:
                warnings.append(_warning("image_reference_not_exported", "An inline image reference was reduced to its text alternative", unit_id, True))
        elif token.type in {"fence", "code_block"}:
            add_text("code", token.content.rstrip("\n"), token, language=(token.info or "").strip())
        elif token.type == "paragraph_open" and not list_stack and index + 1 < len(tokens):
            inline = tokens[index + 1]
            raw, visible, images, text_only = _inline_content(inline)
            cleaned = text_only if export_images and images else visible
            if cleaned or not images:
                add_text("paragraph", raw, token, cleaned=cleaned)
            if images and export_images:
                add_inline_images(images, token)
            elif images and warn_unexported_images:
                warnings.append(_warning("image_reference_not_exported", "An inline image reference was reduced to its text alternative", unit_id, True))
        elif token.type == "list_item_open":
            depth = 1
            cursor = index + 1
            values: list[str] = []
            first_inline = token
            while cursor < len(tokens) and depth:
                candidate = tokens[cursor]
                if candidate.type == "list_item_open":
                    depth += 1
                elif candidate.type == "list_item_close":
                    depth -= 1
                    if depth == 0:
                        break
                elif depth == 1 and candidate.type == "inline":
                    values.append(candidate.content.strip())
                    first_inline = candidate
                cursor += 1
            ordered, ordinal = list_stack[-1] if list_stack else (False, 1)
            add_text("list_item", " ".join(value for value in values if value), first_inline, ordered=ordered, ordinal=ordinal)
            if list_stack and ordered:
                list_stack[-1] = (ordered, ordinal + 1)
            index = cursor
        elif token.type == "table_open":
            rows: list[list[str]] = []
            headers: list[str] = []
            current_row: list[str] | None = None
            header_cell = False
            cursor = index + 1
            while cursor < len(tokens) and tokens[cursor].type != "table_close":
                candidate = tokens[cursor]
                if candidate.type == "tr_open":
                    current_row = []
                elif candidate.type == "th_open":
                    header_cell = True
                elif candidate.type == "td_open":
                    header_cell = False
                elif candidate.type == "inline" and current_row is not None:
                    current_row.append(candidate.content.strip())
                    if header_cell:
                        headers.append(candidate.content.strip())
                elif candidate.type == "tr_close" and current_row is not None:
                    rows.append(current_row)
                    current_row = None
                cursor += 1
            occurrence += 1
            source_locator = locator(token)
            table_id = stable_id("table", document_id, source_locator, "table", occurrence)
            normalized_rows = [
                [
                    {
                        **make_text_fields(cell, cell.strip(), mode, defer=True),
                        "value": _numeric_value(cell),
                        "rowspan": 1,
                        "colspan": 1,
                    }
                    for cell in row
                ]
                for row in rows if row
            ]
            tables.append(
                {
                    "table_id": table_id,
                    "source_locator": source_locator,
                    "raw_rows": rows,
                    "rows": normalized_rows,
                    "confidence": 1.0,
                    "warnings": [],
                    **({"headers": headers} if headers else {}),
                }
            )
            content.append(
                {
                    "id": stable_id("node", document_id, source_locator, "table", occurrence),
                    "type": "table",
                    "source_locator": source_locator,
                    "table_id": table_id,
                }
            )
            index = cursor
        index += 1

    if image_cursor < len(asset_occurrences):
        inferred_count = len(asset_occurrences) - image_cursor
        warnings.append(
            _warning(
                "office_image_position_inferred",
                f"The reading position of {inferred_count} Office image occurrence(s) was inferred from OOXML package order",
                unit_id,
                False,
            )
        )
        while image_cursor < len(asset_occurrences):
            add_image("", inferred=True)

    if not content and markdown.strip():
        add_text("paragraph", markdown.strip(), tokens[0] if tokens else type("Token", (), {"map": None})())
    normalize_canonical_text(content, tables, mode)
    for table in tables:
        if table.get("headers") and table.get("rows"):
            table["headers"] = [cell["normalized_text"] for cell in table["rows"][0]]
    if warnings:
        source_unit["status"] = "warning"
        source_unit["warnings"].extend(warnings)
    title_source = next((node["normalized_text"] for node in content if node["type"] == "heading" and node.get("level") == 1), None)
    return {
        "source_units": [source_unit],
        "content": content,
        "tables": tables,
        "assets": assets,
        "relationships": [],
        "warnings": warnings,
        "title": title_source or title_from_markdown(markdown, "untitled"),
    }


class MarkItDownAdapter:
    name = "markitdown"
    limitations = [
        "tracked_changes_not_preserved",
        "comments_not_preserved",
        "legacy_office_embedded_images_may_not_be_exported",
    ]

    def extract(self, source: str, document_id: str, mode: str, asset_dir: Path | None = None) -> dict[str, Any]:
        path = Path(source)
        unit_locator = {"kind": "document", "index": 1}
        unit_id = stable_id("unit", document_id, unit_locator, "document", 1)
        assets: list[dict[str, Any]] | None = None
        image_occurrences: list[str] | None = None
        image_warnings: list[dict[str, Any]] = []
        if path.exists() and asset_dir is not None and path.suffix.lower() in OOXML_SUFFIXES:
            assets, image_occurrences, image_warnings = extract_ooxml_images(
                path,
                document_id,
                unit_id,
                asset_dir,
            )
        try:
            markdown = convert_basic(source)
        except Exception:
            recoverable_codes = {
                "office_external_image_not_exported",
                "office_image_relationship_missing",
                "office_image_target_missing",
                "office_image_target_unsafe",
            }
            if not any(item["code"] in recoverable_codes for item in image_warnings):
                raise
            temporary = tempfile.NamedTemporaryFile(suffix=path.suffix, delete=False)
            temporary.close()
            sanitized = Path(temporary.name)
            try:
                if not create_sanitized_ooxml_copy(path, sanitized):
                    raise
                markdown = convert_basic(str(sanitized))
            finally:
                sanitized.unlink(missing_ok=True)
        result = markdown_to_canonical(
            markdown,
            document_id,
            mode,
            "document",
            image_assets=assets,
            image_occurrences=image_occurrences,
            warn_unexported_images=asset_dir is not None and assets is None,
        )
        if path.exists():
            detected = inspect_ooxml_features(path, unit_id)
            detected.extend(image_warnings)
            result["warnings"].extend(detected)
            result["source_units"][0]["warnings"].extend(detected)
            if detected:
                result["source_units"][0]["status"] = "warning"
        result["adapter"] = {
            "name": self.name,
            "version": markitdown_version(),
            "limitations": list(self.limitations),
        }
        result["adapter_text"] = markdown
        return result
