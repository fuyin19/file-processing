"""MarkItDown-backed adapters and Markdown-to-canonical parsing."""
from __future__ import annotations

import importlib.metadata
import hashlib
import io
import re
import stat
import tempfile
import zipfile
from pathlib import Path
from types import MappingProxyType
from typing import Any
from xml.etree import ElementTree

from canonical import make_text_fields, normalize_canonical_text, stable_id, title_from_markdown
from docx_sharding import shard_docx_bytes
from ooxml_images import OOXML_SUFFIXES, create_sanitized_ooxml_copy, extract_ooxml_images
from office_preflight import preflight_office


_MARKITDOWN = None
_ANYDOC = None
_ANYDOC_CAPABILITY: MappingProxyType | None = None
_ANYDOC_CAPABILITY_MODULE_ID: int | None = None
_ANYDOC_CAPABILITY_METADATA_SIGNATURE: tuple[int, int, int] | None = None

# Keep this set deliberately aligned with AnyDoc's public format enum.  The
# pipeline's wider SUPPORTED_EXTENSIONS set remains unchanged; only these
# local formats are eligible for the opt-in/default AnyDoc route.
ANYDOC_FORMAT_BY_EXTENSION = MappingProxyType({
    ".doc": "doc",
    ".docx": "docx", ".docm": "docx",
    ".ppt": "ppt", ".pps": "ppt", ".pot": "ppt",
    ".pptx": "pptx", ".pptm": "pptx", ".ppsx": "pptx", ".ppsm": "pptx",
    ".xls": "xlsx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".xlsb": "xlsx",
    ".odt": "odt", ".ods": "ods", ".odp": "odp", ".rtf": "rtf",
    ".epub": "epub", ".csv": "csv",
})
ANYDOC_CANONICAL_FORMATS = frozenset(ANYDOC_FORMAT_BY_EXTENSION.values())
ANYDOC_SUFFIXES = frozenset(ANYDOC_FORMAT_BY_EXTENSION)
ANYDOC_ALLOWED_DETECTED_FORMATS = frozenset(ANYDOC_CANONICAL_FORMATS | {"pdf"})
ANYDOC_DISTRIBUTION = "firecrawl-anydoc"
ANYDOC_IMPORT = "anydoc"
_ASSET_MEDIA_TYPES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/tiff": ".tiff",
}
_ASSET_MAX_BYTES = 100 * 1024 * 1024
_ASSET_TOTAL_MAX_BYTES = 512 * 1024 * 1024


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


def anydoc_version() -> str:
    """Return the installed distribution version without importing native code."""
    try:
        return importlib.metadata.version("firecrawl-anydoc")
    except importlib.metadata.PackageNotFoundError:
        return "NOT INSTALLED"


def _load_anydoc():
    global _ANYDOC
    if _ANYDOC is None:
        try:
            import anydoc
        except ImportError as exc:
            raise RuntimeError(
                "AnyDoc is unavailable in the active Python interpreter. "
                f"Install a compatible provider with: {__import__('sys').executable} -m pip install {ANYDOC_DISTRIBUTION}"
            ) from exc
        _ANYDOC = anydoc
    return _ANYDOC


def anydoc_capability_check() -> dict[str, Any]:
    """Validate the public AnyDoc package/API used by this adapter.

    This is intentionally a no-install check.  A newer package may be used if
    its public symbols and model shape remain compatible.
    """
    global _ANYDOC_CAPABILITY, _ANYDOC_CAPABILITY_MODULE_ID, _ANYDOC_CAPABILITY_METADATA_SIGNATURE
    # Capability validation is process-static.  Cache the fully validated
    # result after the first successful check; repeated conversions should not
    # rescan distribution metadata or package files.  Tests and callers that
    # replace the imported module reset `_ANYDOC`, which intentionally invalidates
    # this cache and re-runs the complete gate.
    metadata_signature = (
        id(importlib.metadata.distribution),
        id(importlib.metadata.packages_distributions),
        id(importlib.metadata.version),
    )
    if (
        _ANYDOC_CAPABILITY is not None
        and _ANYDOC is not None
        and _ANYDOC_CAPABILITY_MODULE_ID == id(_ANYDOC)
        and _ANYDOC_CAPABILITY_METADATA_SIGNATURE == metadata_signature
    ):
        # A lightweight version read keeps the cache correct if an operator
        # replaces the wheel while this interpreter remains alive, and lets
        # tests exercise newer-version warning behavior deterministically.
        if anydoc_version() == _ANYDOC_CAPABILITY.get("version"):
            return dict(_ANYDOC_CAPABILITY)
    try:
        distribution = importlib.metadata.distribution(ANYDOC_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            f"{ANYDOC_DISTRIBUTION} is unavailable in the active Python interpreter. "
            f"Install a compatible provider with: {__import__('sys').executable} -m pip install {ANYDOC_DISTRIBUTION}"
        ) from exc
    package = _load_anydoc()
    required = (
        "to_document", "format_from_bytes", "format_from_extension", "ConvertError",
        "Document", "Block", "Inline", "List", "ListItem", "Table", "CellSlot",
        "Cell", "Note", "Asset", "ImageSource", "LinkTarget", "Style",
    )
    missing = [
        name for name in required
        if (not callable(getattr(package, name, None)) if name in {"to_document", "format_from_bytes", "format_from_extension"}
            else not isinstance(getattr(package, name, None), type))
    ]
    if missing:
        raise RuntimeError(
            "Installed firecrawl-anydoc is incompatible; missing public API: "
            + ", ".join(missing)
            + f". Reinstall with: {__import__('sys').executable} -m pip install --upgrade {ANYDOC_DISTRIBUTION}"
        )
    providers = importlib.metadata.packages_distributions().get("anydoc", [])
    normalized_providers = {str(name).replace("_", "-").lower() for name in providers}
    if normalized_providers != {ANYDOC_DISTRIBUTION}:
        raise RuntimeError(
            "The active interpreter provides `anydoc` from an unexpected distribution "
            + ", ".join(sorted(providers))
            + "; install firecrawl-anydoc with: "
            + f"{__import__('sys').executable} -m pip install --upgrade {ANYDOC_DISTRIBUTION}"
        )
    module_file = getattr(package, "__file__", None)
    package_root = Path(distribution.locate_file("")).resolve()
    if not module_file:
        raise RuntimeError("Installed firecrawl-anydoc has no import location")
    module_path = Path(module_file).resolve()
    try:
        module_path.relative_to(package_root)
    except ValueError as exc:
        raise RuntimeError(
            "The imported anydoc module is shadowed outside the firecrawl-anydoc distribution; "
            f"active module: {module_path}"
        ) from exc
    files = distribution.files or ()
    declared_files = {Path(distribution.locate_file(item)).resolve() for item in files}
    if declared_files and module_path not in declared_files:
        raise RuntimeError(
            "The imported anydoc module is not declared by the firecrawl-anydoc distribution files; "
            f"active module: {module_path}"
        )
    version = anydoc_version()
    if version == "NOT INSTALLED":
        raise RuntimeError(
            f"{ANYDOC_DISTRIBUTION} is unavailable in the active Python interpreter. "
            f"Install a compatible provider with: {__import__('sys').executable} -m pip install {ANYDOC_DISTRIBUTION}"
        )
    result = {
        "version": version,
        "module": str(module_path),
    }
    _ANYDOC_CAPABILITY = MappingProxyType(dict(result))
    _ANYDOC_CAPABILITY_MODULE_ID = id(_ANYDOC)
    _ANYDOC_CAPABILITY_METADATA_SIGNATURE = metadata_signature
    return dict(_ANYDOC_CAPABILITY)


def anydoc_format_for_path(path: str | Path) -> str | None:
    suffix = Path(path).suffix.lower()
    return ANYDOC_FORMAT_BY_EXTENSION.get(suffix)


def convert_basic(source: str) -> str:
    return get_markitdown().convert(source).text_content


def _warning(code: str, message: str, source_unit: str, content_loss: bool) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "content_loss": content_loss,
        "source_unit": source_unit,
    }


_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WORD_REVISION_TAGS = {
    f"{{{_WORD_NS}}}{name}"
    for name in (
        "ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange",
        "tblPrChange", "trPrChange", "tcPrChange", "sectPrChange",
        "tblGridChange", "tblPrExChange", "numberingChange",
        "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
        "customXmlInsRangeStart", "customXmlInsRangeEnd",
        "customXmlDelRangeStart", "customXmlDelRangeEnd",
        "customXmlMoveFromRangeStart", "customXmlMoveFromRangeEnd",
        "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd",
    )
}
_WORD_ACCEPT_KEEP = {f"{{{_WORD_NS}}}ins", f"{{{_WORD_NS}}}moveTo"}
_WORD_ACCEPT_DROP_CONTENT = {f"{{{_WORD_NS}}}del", f"{{{_WORD_NS}}}moveFrom"}
_WORD_ACCEPT_DROP_MARKERS = _WORD_REVISION_TAGS - _WORD_ACCEPT_KEEP - _WORD_ACCEPT_DROP_CONTENT
_WORD_UNSUPPORTED_REVISION_TAGS = {
    f"{{{_WORD_NS}}}{name}"
    for name in ("cellIns", "cellDel", "cellMerge", "conflictIns", "conflictDel")
}


def _word_story_parts(names: set[str]) -> list[str]:
    return sorted(
        name for name in names
        if name == "word/document.xml"
        or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
        or name in {"word/footnotes.xml", "word/endnotes.xml"}
    )


def _word_revision_counts(package: zipfile.ZipFile, names: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for part in _word_story_parts(names):
        try:
            for _, element in ElementTree.iterparse(io.BytesIO(package.read(part)), events=("start",)):
                if element.tag in _WORD_UNSUPPORTED_REVISION_TAGS:
                    name = element.tag.rsplit("}", 1)[-1]
                    raise RuntimeError(
                        f"Word revision class {name} in {part} has no proved accepted-view transform"
                    )
                if element.tag in _WORD_REVISION_TAGS:
                    name = element.tag.rsplit("}", 1)[-1]
                    counts[name] = counts.get(name, 0) + 1
        except (ElementTree.ParseError, KeyError, OSError) as exc:
            raise RuntimeError(f"Could not inspect Word revisions in {part}: {type(exc).__name__}: {exc}") from exc
    return counts


def _accepted_word_tree(root: ElementTree.Element) -> None:
    """Apply the supported accepted/final-view semantics to one Word story."""
    def visit(parent: ElementTree.Element) -> None:
        for child in list(parent):
            if child.tag in _WORD_ACCEPT_DROP_CONTENT or child.tag in _WORD_ACCEPT_DROP_MARKERS:
                parent.remove(child)
                continue
            if child.tag in _WORD_ACCEPT_KEEP:
                visit(child)
                position = list(parent).index(child)
                descendants = list(child)
                tail = child.tail
                parent.remove(child)
                for offset, descendant in enumerate(descendants):
                    parent.insert(position + offset, descendant)
                if tail:
                    if descendants:
                        descendants[-1].tail = (descendants[-1].tail or "") + tail
                    elif position:
                        previous = list(parent)[position - 1]
                        previous.tail = (previous.tail or "") + tail
                    else:
                        parent.text = (parent.text or "") + tail
                continue
            visit(child)

    visit(root)


def accepted_word_snapshot(path: Path) -> tuple[bytes, dict[str, int]]:
    """Return immutable source bytes or a temporary accepted-view OOXML snapshot."""
    raw = path.read_bytes()
    if path.suffix.lower() not in {".docx", ".docm"} or not zipfile.is_zipfile(path):
        return raw, {}
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        names = set(package.namelist())
        counts = _word_revision_counts(package, names)
        if not counts:
            return raw, {}
        replacements: dict[str, bytes] = {}
        for part in _word_story_parts(names):
            try:
                root = ElementTree.fromstring(package.read(part))
                _accepted_word_tree(root)
                replacements[part] = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
            except (ElementTree.ParseError, KeyError, OSError) as exc:
                raise RuntimeError(f"Could not build accepted Word view for {part}: {type(exc).__name__}: {exc}") from exc
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as accepted:
            for item in package.infolist():
                accepted.writestr(item, replacements.get(item.filename, package.read(item.filename)))
        return output.getvalue(), counts


def inspect_ooxml_features(path: Path, source_unit: str) -> list[dict[str, Any]]:
    """Detect unsupported OOXML semantics without prefix or substring guesses."""
    if path.suffix.lower() not in OOXML_SUFFIXES | {".docm", ".pptm", ".xlsm"} or not zipfile.is_zipfile(path):
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
        revisions = _word_revision_counts(package, names)
        if revisions:
            summary = ", ".join(f"{name}={revisions[name]}" for name in sorted(revisions))
            warnings.append(
                _warning(
                    "office_revisions_flattened_to_accepted_view",
                    f"Word revisions were flattened to the final accepted view ({summary})",
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
    relationships: list[dict[str, Any]] = []
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
        node_id = stable_id("node", document_id, source_locator, "image", occurrence)
        content.append(
            {
                "id": node_id,
                "type": "image",
                "source_locator": source_locator,
                "asset_id": asset_id,
            }
        )
        relationships.append(
            {
                "type": "image_occurrence",
                "source_unit_id": unit_id,
                "asset_id": asset_id,
                "occurrence_index": image_cursor,
                "placement": "resolved",
                "content_node_id": node_id,
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
        unresolved_ids = asset_occurrences[image_cursor:]
        warnings.append(
            _warning(
                "office_image_position_unresolved",
                f"The reading position of {len(unresolved_ids)} Office image occurrence(s) could not be proved; assets were retained without guessed content nodes",
                unit_id,
                True,
            )
        )

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
        "relationships": relationships + ([
            {
                "type": "image_occurrence",
                "source_unit_id": unit_id,
                "asset_id": asset_id,
                "occurrence_index": image_cursor + index,
                "placement": "unresolved",
            }
            for index, asset_id in enumerate(unresolved_ids, 1)
        ] if image_cursor < len(asset_occurrences) else []),
        "warnings": warnings,
        "title": title_source or title_from_markdown(markdown, "untitled"),
    }


def _ad_attr(value: Any, name: str, default: Any = None) -> Any:
    """Read an AnyDoc model field while keeping mocked model objects simple."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _ad_kind(value: Any) -> str:
    return str(_ad_attr(value, "kind", "") or "").lower()


def _ad_text_blocks(blocks: list[Any] | None, include_lists: bool = True) -> str:
    """Render block content to visible text for table cells/notes."""
    output: list[str] = []
    for block in blocks or []:
        kind = _ad_kind(block)
        if kind in {"heading", "paragraph"}:
            for inline in _ad_attr(block, "content", []) or []:
                inline_kind = _ad_kind(inline)
                if inline_kind in {"text", "link"}:
                    text = _ad_attr(inline, "text", None)
                    if text is not None:
                        output.append(str(text))
                    else:
                        output.append(_ad_text_inlines(_ad_attr(inline, "content", []) or [], None)[0])
                elif inline_kind == "line_break":
                    output.append("\n")
                elif inline_kind == "image":
                    output.append(str(_ad_attr(inline, "alt", "") or ""))
            output.append("\n")
        elif kind == "code_block":
            output.append(str(_ad_attr(block, "text", "") or ""))
            output.append("\n")
        elif kind == "list" and include_lists:
            listing = _ad_attr(block, "list", block)
            for item in _ad_attr(listing, "items", []) or []:
                output.append(_ad_text_blocks(_ad_attr(item, "blocks", []) or []))
        elif kind == "block_quote":
            output.append(_ad_text_blocks(_ad_attr(block, "blocks", []) or []))
        elif kind == "table":
            # Canonical v1 cannot nest table records inside a cell.  Preserve
            # the nested table's visible values in deterministic row-major
            # order instead of silently dropping the nested block.
            table = _ad_attr(block, "table", block)
            grid = _ad_attr(table, "grid", []) or []
            for row in grid:
                for slot in row or []:
                    if _ad_kind(slot) != "origin":
                        continue
                    cell = _ad_attr(slot, "cell", None)
                    if cell is not None:
                        value = _ad_text_blocks(_ad_attr(cell, "blocks", []) or [], include_lists=include_lists)
                        if value:
                            output.append(value)
                            output.append("\n")
        elif kind == "rule":
            output.append("---\n")
    return "".join(output).strip()


def _ad_blocks_contain_inline_kind(blocks: list[Any] | None, target_kind: str) -> bool:
    """Return whether nested blocks contain an inline of ``target_kind``."""
    for block in blocks or []:
        if any(_ad_kind(inline) == target_kind for inline in _ad_attr(block, "content", []) or []):
            return True
        if _ad_blocks_contain_inline_kind(_ad_attr(block, "content", []) or [], target_kind):
            return True
        if _ad_blocks_contain_inline_kind(_ad_attr(block, "blocks", []) or [], target_kind):
            return True
        listing = _ad_attr(block, "list", None)
        for item in _ad_attr(listing, "items", []) or []:
            if _ad_blocks_contain_inline_kind(_ad_attr(item, "blocks", []) or [], target_kind):
                return True
        table = _ad_attr(block, "table", None)
        for row in _ad_attr(table, "grid", []) or []:
            for slot in row or []:
                cell = _ad_attr(slot, "cell", None)
                if cell is not None and _ad_blocks_contain_inline_kind(_ad_attr(cell, "blocks", []) or [], target_kind):
                    return True
    return False


def _ad_text_inlines(inlines: list[Any], asset_lookup: dict[int, dict[str, Any]] | None) -> tuple[str, list[tuple[int, str]]]:
    """Return visible text and embedded-image references in inline order."""
    visible: list[str] = []
    images: list[tuple[int, str]] = []
    for inline in inlines:
        kind = _ad_kind(inline)
        if kind == "text":
            visible.append(str(_ad_attr(inline, "text", "") or ""))
        elif kind == "link":
            nested_text, nested_images = _ad_text_inlines(_ad_attr(inline, "content", []) or [], asset_lookup)
            visible.append(nested_text)
            images.extend(nested_images)
        elif kind == "line_break":
            visible.append("\n")
        elif kind == "anchor":
            continue
        elif kind == "note_ref":
            # Canonical v1 has no note-reference field; never invent Markdown
            # footnote syntax that could collide with source content.
            continue
        elif kind == "image":
            alt = str(_ad_attr(inline, "alt", "") or "")
            source = _ad_attr(inline, "source", None)
            if _ad_kind(source) == "asset":
                index = _ad_attr(source, "asset_id", None)
                if isinstance(index, int):
                    images.append((index, alt))
            elif alt:
                visible.append(alt)
        else:
            text = _ad_attr(inline, "text", None)
            if text:
                visible.append(str(text))
    return "".join(visible), images


def _ad_inline_segments(inlines: list[Any]) -> list[tuple[str, Any]]:
    """Flatten inline containers while retaining visible image/text order."""
    segments: list[tuple[str, Any]] = []
    for inline in inlines or []:
        kind = _ad_kind(inline)
        if kind == "text":
            segments.append(("text", str(_ad_attr(inline, "text", "") or "")))
        elif kind == "link":
            target = _ad_attr(inline, "target", None)
            target_kind = _ad_kind(target)
            if target_kind:
                segments.append(("link_target", target_kind))
            segments.extend(_ad_inline_segments(_ad_attr(inline, "content", []) or []))
        elif kind == "line_break":
            segments.append(("text", "\n"))
        elif kind == "image":
            alt = str(_ad_attr(inline, "alt", "") or "")
            source = _ad_attr(inline, "source", None)
            if _ad_kind(source) == "asset" and isinstance(_ad_attr(source, "asset_id", None), int):
                segments.append(("image", (_ad_attr(source, "asset_id", None), alt)))
            else:
                segments.append(("external_image", alt))
        elif kind in {"anchor", "note_ref"}:
            segments.append((kind, None))
        else:
            text = _ad_attr(inline, "text", None)
            if text:
                segments.append(("text", str(text)))
    return segments


def _ad_has_rich_style(inlines: list[Any]) -> bool:
    for inline in inlines or []:
        style = _ad_attr(inline, "style", None)
        if style is not None and any(bool(_ad_attr(style, field, False)) for field in ("bold", "italic", "strike", "code")):
            return True
        if _ad_kind(inline) == "link" and _ad_has_rich_style(_ad_attr(inline, "content", []) or []):
            return True
    return False


def _ad_has_rich_loss(inlines: list[Any]) -> bool:
    """Whether inline semantics cannot be represented by Canonical v1."""
    for inline in inlines or []:
        kind = _ad_kind(inline)
        if kind == "anchor":
            return True
        if kind == "link":
            if _ad_has_rich_loss(_ad_attr(inline, "content", []) or []):
                return True
    return False


_ANYDOC_MAX_DEPTH = 64
_ANYDOC_MAX_TABLE_ROWS = 10_000
_ANYDOC_MAX_TABLE_COLUMNS = 1_000
_ANYDOC_MAX_TABLE_CELLS = 1_000_000
_ANYDOC_MAX_CHARS_PER_FIELD = 10_000_000
_ANYDOC_MAX_TOTAL_TEXT = 100_000_000
_ANYDOC_BLOCK_KINDS = {"heading", "paragraph", "list", "table", "block_quote", "code_block", "rule"}
_ANYDOC_INLINE_KINDS = {"text", "link", "image", "anchor", "note_ref", "line_break"}


def _validate_anydoc_document(document: Any) -> None:
    """Fail closed on malformed/native model graphs before canonical allocation."""
    if document is None:
        raise RuntimeError("AnyDoc returned no Document model")
    blocks = _ad_attr(document, "blocks", None)
    notes = _ad_attr(document, "notes", None)
    assets = _ad_attr(document, "assets", None)
    if not isinstance(blocks, (list, tuple)) or not isinstance(notes, (list, tuple)) or not isinstance(assets, (list, tuple)):
        raise RuntimeError("AnyDoc Document has invalid blocks/notes/assets collections")
    visited: set[int] = set()
    active: set[int] = set()
    total_text = 0

    def integer(value: Any, field: str, minimum: int | None = None) -> None:
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"AnyDoc {field} must be an integer")
        if minimum is not None and value < minimum:
            raise RuntimeError(f"AnyDoc {field} must be >= {minimum}")

    def walk(value: Any, path: str, depth: int) -> None:
        nonlocal total_text
        if depth > _ANYDOC_MAX_DEPTH:
            raise RuntimeError(f"AnyDoc model exceeds max depth {_ANYDOC_MAX_DEPTH} at {path}")
        if value is None or isinstance(value, (bool, int, float, bytes, bytearray)):
            return
        if isinstance(value, str):
            if len(value) > _ANYDOC_MAX_CHARS_PER_FIELD:
                raise RuntimeError(f"AnyDoc text field exceeds {_ANYDOC_MAX_CHARS_PER_FIELD} characters at {path}")
            total_text += len(value)
            if total_text > _ANYDOC_MAX_TOTAL_TEXT:
                raise RuntimeError(f"AnyDoc text exceeds {_ANYDOC_MAX_TOTAL_TEXT} characters")
            return
        identity = id(value)
        if identity in active:
            raise RuntimeError(f"AnyDoc model cycle detected at {path}")
        if identity in visited:
            return
        visited.add(identity)
        active.add(identity)
        try:
            if isinstance(value, dict):
                for key, child in value.items():
                    walk(key, f"{path}.key", depth + 1)
                    walk(child, f"{path}[{key!r}]", depth + 1)
                return
            if isinstance(value, (list, tuple)):
                for index, child in enumerate(value):
                    walk(child, f"{path}[{index}]", depth + 1)
                return
            kind = _ad_kind(value)
            if kind and kind not in _ANYDOC_BLOCK_KINDS | _ANYDOC_INLINE_KINDS | {
                "origin", "covered", "asset", "external", "relative", "anchor", "unavailable", "data", "layout",
                "footnote", "endnote", "bullet", "decimal", "lower_alpha", "upper_alpha", "lower_roman", "upper_roman",
            }:
                raise RuntimeError(f"AnyDoc returned unknown model kind {kind!r} at {path}")
            for field in (
                "kind", "level", "anchor", "content", "list", "table", "blocks", "lang", "text",
                "target", "alt", "source", "note_id", "marker", "start", "items", "checked", "marker_label",
                "grid", "header_rows", "cell", "origin_row", "origin_col", "col_span", "row_span", "id",
                "media_type", "origin_part", "data", "url", "asset_id", "style",
            ):
                child = _ad_attr(value, field, None)
                if child is None:
                    continue
                if field in {"level", "start", "header_rows", "origin_row", "origin_col", "col_span", "row_span", "id", "asset_id"}:
                    integer(child, f"{path}.{field}", 0 if field not in {"level", "col_span", "row_span"} else 1)
                if field == "marker" and str(child) not in {"bullet", "decimal", "lower_alpha", "upper_alpha", "lower_roman", "upper_roman"}:
                    raise RuntimeError(f"AnyDoc list marker {child!r} is invalid at {path}")
                walk(child, f"{path}.{field}", depth + 1)
        finally:
            active.remove(identity)

    # Guard the native graph with an explicit stack before the compatibility
    # field walk below.  This keeps adversarial depth/cycle/visited failures
    # deterministic without relying on Python recursion behavior.
    graph_fields = (
        "blocks", "notes", "assets", "content", "list", "table", "items", "grid", "cell",
        "source", "target", "style", "data", "value",
    )
    graph_stack: list[tuple[Any, str, int, bool, int | None]] = [(document, "document", 0, False, None)]
    graph_active: set[int] = set()
    graph_seen: set[int] = set()
    while graph_stack:
        value, path, depth, exiting, identity = graph_stack.pop()
        if exiting:
            if identity is not None:
                graph_active.discard(identity)
            continue
        if depth > _ANYDOC_MAX_DEPTH:
            raise RuntimeError(f"AnyDoc model exceeds max depth {_ANYDOC_MAX_DEPTH} at {path}")
        if value is None or isinstance(value, (str, bool, int, float, bytes, bytearray)):
            continue
        object_id = id(value)
        if object_id in graph_active:
            raise RuntimeError(f"AnyDoc model cycle detected at {path}")
        if object_id in graph_seen:
            continue
        graph_seen.add(object_id)
        graph_active.add(object_id)
        graph_stack.append((value, path, depth, True, object_id))
        if isinstance(value, dict):
            children = list(value.items())
            for index, (key, child) in reversed(list(enumerate(children))):
                graph_stack.append((child, f"{path}[{key!r}]", depth + 1, False, None))
            continue
        if isinstance(value, (list, tuple)):
            for index, child in reversed(list(enumerate(value))):
                graph_stack.append((child, f"{path}[{index}]", depth + 1, False, None))
            continue
        for field in reversed(graph_fields):
            child = _ad_attr(value, field, None)
            if child is not None:
                graph_stack.append((child, f"{path}.{field}", depth + 1, False, None))

    walk(document, "document", 0)
    asset_ids: set[int] = set()
    total_asset_bytes = 0
    for index, asset in enumerate(assets):
        raw_id = _ad_attr(asset, "id", index)
        integer(raw_id, f"assets[{index}].id", 0)
        if raw_id in asset_ids:
            raise RuntimeError(f"AnyDoc duplicate Asset.id {raw_id}")
        asset_ids.add(raw_id)
        data = _ad_attr(asset, "data", None)
        if not isinstance(data, (bytes, bytearray)):
            raise RuntimeError(f"AnyDoc assets[{index}].data must be in-memory bytes")
        if len(data) > _ASSET_MAX_BYTES:
            raise RuntimeError(f"AnyDoc asset {raw_id} exceeds 100 MiB limit")
        total_asset_bytes += len(data)
        if total_asset_bytes > _ASSET_TOTAL_MAX_BYTES:
            raise RuntimeError("AnyDoc assets exceed the 512 MiB total limit")
        if _ad_attr(asset, "origin_part", None) is None or not isinstance(_ad_attr(asset, "origin_part", ""), str):
            raise RuntimeError(f"AnyDoc assets[{index}].origin_part must be text")

    def validate_table(table: Any, path: str) -> None:
        grid = _ad_attr(table, "grid", None)
        if not isinstance(grid, (list, tuple)):
            raise RuntimeError(f"AnyDoc table grid is invalid at {path}")
        rows = len(grid)
        if rows > _ANYDOC_MAX_TABLE_ROWS:
            raise RuntimeError(f"AnyDoc table exceeds {_ANYDOC_MAX_TABLE_ROWS} rows")
        widths = [len(row) for row in grid if isinstance(row, (list, tuple))]
        width = max(widths, default=0)
        if width > _ANYDOC_MAX_TABLE_COLUMNS:
            raise RuntimeError(f"AnyDoc table exceeds {_ANYDOC_MAX_TABLE_COLUMNS} columns")
        if widths and any(row_width != width for row_width in widths):
            raise RuntimeError(f"AnyDoc table grid is ragged at {path}; every row must have {width} slots")
        cells = 0
        origins: dict[tuple[int, int], tuple[int, int]] = {}
        slots: dict[tuple[int, int], Any] = {}
        for row_index, row in enumerate(grid):
            if not isinstance(row, (list, tuple)):
                raise RuntimeError(f"AnyDoc table row is invalid at {path}[{row_index}]")
            for col_index, slot in enumerate(row):
                cells += 1
                if cells > _ANYDOC_MAX_TABLE_CELLS:
                    raise RuntimeError(f"AnyDoc table exceeds {_ANYDOC_MAX_TABLE_CELLS} cells")
                slot_kind = _ad_kind(slot)
                slots[(row_index, col_index)] = slot
                if slot_kind == "origin":
                    if (row_index, col_index) in origins:
                        raise RuntimeError(f"AnyDoc table repeats origin slot at {path}[{row_index}][{col_index}]")
                    origins[(row_index, col_index)] = (row_index, col_index)
                    cell = _ad_attr(slot, "cell", None)
                    if cell is None:
                        raise RuntimeError(f"AnyDoc origin slot has no cell at {path}[{row_index}][{col_index}]")
                    row_span = _ad_attr(cell, "row_span", None)
                    col_span = _ad_attr(cell, "col_span", None)
                    if row_span is None or col_span is None:
                        raise RuntimeError(f"AnyDoc origin slot has missing span at {path}[{row_index}][{col_index}]")
                    integer(row_span, f"{path}.row_span", 1)
                    integer(col_span, f"{path}.col_span", 1)
                    if row_index + row_span > rows or col_index + col_span > width:
                        raise RuntimeError(f"AnyDoc table span exceeds grid bounds at {path}[{row_index}][{col_index}]")
                elif slot_kind == "covered":
                    origin_row = _ad_attr(slot, "origin_row", None)
                    origin_col = _ad_attr(slot, "origin_col", None)
                    if origin_row is None or origin_col is None:
                        raise RuntimeError(f"AnyDoc covered slot has missing origin at {path}[{row_index}][{col_index}]")
                    integer(origin_row, f"{path}.origin_row", 0)
                    integer(origin_col, f"{path}.origin_col", 0)
                    origins.setdefault((origin_row, origin_col), (origin_row, origin_col))
                else:
                    raise RuntimeError(f"AnyDoc table slot kind {slot_kind!r} is invalid at {path}[{row_index}][{col_index}]")
        # Validate coverage after collecting all origins so a covered slot may
        # legally precede its origin in row-major order while still matching
        # the declared positive span exactly.
        for (origin_row, origin_col), _ in list(origins.items()):
            origin = slots.get((origin_row, origin_col))
            if origin is None or _ad_kind(origin) != "origin":
                raise RuntimeError(f"AnyDoc covered slot points to missing origin at {path}[{origin_row}][{origin_col}]")
            cell = _ad_attr(origin, "cell", None)
            row_span = int(_ad_attr(cell, "row_span", 1) or 1)
            col_span = int(_ad_attr(cell, "col_span", 1) or 1)
            for covered_row in range(origin_row, origin_row + row_span):
                for covered_col in range(origin_col, origin_col + col_span):
                    if (covered_row, covered_col) == (origin_row, origin_col):
                        continue
                    covered = slots.get((covered_row, covered_col))
                    if covered is None or _ad_kind(covered) != "covered":
                        raise RuntimeError(
                            f"AnyDoc origin span lacks covered slot at {path}[{covered_row}][{covered_col}]"
                        )
                    if (
                        _ad_attr(covered, "origin_row", None) != origin_row
                        or _ad_attr(covered, "origin_col", None) != origin_col
                    ):
                        raise RuntimeError(
                            f"AnyDoc covered slot points to the wrong origin at {path}[{covered_row}][{covered_col}]"
                        )
        for coordinate, slot in slots.items():
            if _ad_kind(slot) == "covered":
                origin_coordinate = (
                    _ad_attr(slot, "origin_row", None),
                    _ad_attr(slot, "origin_col", None),
                )
                if origin_coordinate not in origins:
                    raise RuntimeError(f"AnyDoc covered slot points to missing origin at {path}{coordinate}")

    def scan_tables(value: Any, path: str, seen: set[int]) -> None:
        if value is None or isinstance(value, (str, bytes, bytearray, int, float, bool)):
            return
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        if _ad_kind(value) == "table":
            validate_table(_ad_attr(value, "table", value), path)
        if isinstance(value, dict):
            children = value.values()
        elif isinstance(value, (list, tuple)):
            children = value
        else:
            children = [_ad_attr(value, field, None) for field in ("blocks", "content", "list", "table", "items", "grid", "cell")]
        for index, child in enumerate(children):
            scan_tables(child, f"{path}[{index}]", seen)

    scan_tables(document, "document", set())


def _ad_warning(code: str, message: str, source_unit: str, content_loss: bool) -> dict[str, Any]:
    return _warning(code, message, source_unit, content_loss)


def _is_anydoc_max_xml_nodes(package: Any, error: BaseException) -> bool:
    """Recognize only the provider's typed/legacy capacity error, never lookalikes."""
    if str(getattr(error, "limit", "")) == "max_xml_nodes":
        resource_error = getattr(package, "ResourceLimitError", None)
        return isinstance(resource_error, type) and type(error) is resource_error
    convert_error = getattr(package, "ConvertError", None)
    if not isinstance(convert_error, type) or type(error) is not convert_error:
        return False
    return re.fullmatch(r"(?:.*:\s*)?max_xml_nodes exceeded(?:\s*\([^\r\n]*\))?", str(error).strip()) is not None


def _merge_sharded_anydoc_results(
    results: list[tuple[int, int, dict[str, Any]]],
    document_id: str,
    original_feature_warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not results:
        raise RuntimeError("DOCX capacity recovery produced no shard results")
    merged = dict(results[0][2])
    unit = merged["source_units"][0]
    unit_id = unit["id"]
    content: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    asset_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    node_counter = 0
    table_counter = 0
    image_counter = 0

    for shard_index, (first_block, last_block, result) in enumerate(results, 1):
        old_assets = {item["asset_id"]: item for item in result.get("assets", [])}
        asset_map: dict[str, str] = {}
        for old_id, item in old_assets.items():
            key = (str(item.get("source_locator", {}).get("origin_part", "")), item["sha256"])
            canonical_asset = asset_by_key.get(key)
            if canonical_asset is None:
                canonical_asset = dict(item)
                asset_by_key[key] = canonical_asset
                assets.append(canonical_asset)
            asset_map[old_id] = canonical_asset["asset_id"]

        table_map: dict[str, str] = {}
        for item in result.get("tables", []):
            table_counter += 1
            table = dict(item)
            locator = dict(table.get("source_locator", {}))
            locator.update({"shard_index": shard_index, "original_block_first": first_block, "original_block_last": last_block})
            table["source_locator"] = locator
            old_id = table["table_id"]
            table["table_id"] = stable_id("table", document_id, locator, "table", table_counter)
            table_map[old_id] = table["table_id"]
            tables.append(table)

        node_map: dict[str, str] = {}
        for item in result.get("content", []):
            node_counter += 1
            node = dict(item)
            locator = dict(node.get("source_locator", {}))
            locator.update({"shard_index": shard_index, "original_block_first": first_block, "original_block_last": last_block})
            node["source_locator"] = locator
            old_id = node["id"]
            node["id"] = stable_id("node", document_id, locator, node["type"], node_counter)
            node_map[old_id] = node["id"]
            if node.get("table_id") in table_map:
                node["table_id"] = table_map[node["table_id"]]
            if node.get("asset_id") in asset_map:
                node["asset_id"] = asset_map[node["asset_id"]]
            content.append(node)

        for item in result.get("relationships", []):
            relationship = dict(item)
            if relationship.get("type") == "image_occurrence":
                image_counter += 1
                relationship["occurrence_index"] = image_counter
                relationship["asset_id"] = asset_map.get(relationship.get("asset_id"), relationship.get("asset_id"))
                if relationship.get("content_node_id") in node_map:
                    relationship["content_node_id"] = node_map[relationship["content_node_id"]]
            relationships.append(relationship)
        for warning in result.get("warnings", []):
            if not any(
                existing.get("code") == warning.get("code") and existing.get("message") == warning.get("message")
                for existing in warnings
            ):
                warnings.append(warning)

    warnings.append(_ad_warning(
        "adapter_fallback_used",
        f"adapter=anydoc phase=capacity_recovery limit=max_xml_nodes shards={len(results)}",
        unit_id,
        False,
    ))
    for warning in original_feature_warnings or []:
        if not any(
            existing.get("code") == warning.get("code")
            and existing.get("message") == warning.get("message")
            for existing in warnings
        ):
            warnings.append(warning)
    unit = dict(unit)
    unit["warnings"] = warnings
    unit["status"] = "warning"
    merged.update({
        "source_units": [unit],
        "content": content,
        "tables": tables,
        "assets": assets,
        "relationships": relationships,
        "warnings": warnings,
        "title": next((item.get("normalized_text") for item in content if item.get("type") == "heading" and item.get("level") == 1), merged.get("title")),
    })
    return merged


def _reconcile_anydoc_ooxml_images(
    assets: list[dict[str, Any]],
    content: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    inventory_assets: list[dict[str, Any]],
    inventory_occurrence_ids: list[str],
    asset_dir: Path,
    unit_id: str,
    warnings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Publish only relationship-proved OOXML images and account for every occurrence."""
    inventory_by_id = {item["asset_id"]: item for item in inventory_assets}
    provider_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    provider_by_digest: dict[str, list[dict[str, Any]]] = {}
    for asset in assets:
        locator = asset.get("source_locator", {})
        part = str(locator.get("origin_part") or "").replace("\\", "/").lstrip("/")
        digest = str(asset.get("sha256") or "")
        provider_by_key[(part, digest)] = asset
        provider_by_digest.setdefault(digest, []).append(asset)

    inventory_to_provider: dict[str, str] = {}
    inventory_part_by_provider: dict[str, str] = {}
    for inventory in inventory_assets:
        locator = inventory.get("source_locator", {})
        part = str(locator.get("package_part") or "").replace("\\", "/").lstrip("/")
        digest = str(inventory.get("sha256") or "")
        provider = provider_by_key.get((part, digest))
        if provider is None and len(provider_by_digest.get(digest, [])) == 1:
            provider = provider_by_digest[digest][0]
        if provider is not None:
            inventory_to_provider[inventory["asset_id"]] = provider["asset_id"]
            inventory_part_by_provider[provider["asset_id"]] = part

    proved_asset_ids = set(inventory_to_provider.values())
    removed_asset_ids = {item["asset_id"] for item in assets} - proved_asset_ids
    if removed_asset_ids:
        for asset in assets:
            if asset["asset_id"] not in removed_asset_ids:
                continue
            published = asset_dir / Path(asset["path"]).name
            published.unlink(missing_ok=True)
        warnings.append(_ad_warning(
            "office_image_relationship_unproved",
            f"Discarded {len(removed_asset_ids)} provider image asset(s) without an OOXML relationship",
            unit_id,
            True,
        ))
    assets = [item for item in assets if item["asset_id"] in proved_asset_ids]

    removed_node_ids = {
        item["id"] for item in content
        if item.get("type") == "image" and item.get("asset_id") not in proved_asset_ids
    }
    content = [item for item in content if item.get("id") not in removed_node_ids]
    relationships = [
        item for item in relationships
        if item.get("content_node_id") not in removed_node_ids
        and item.get("asset_id") not in removed_asset_ids
    ]

    for relationship in relationships:
        if relationship.get("type") == "image_occurrence":
            part = inventory_part_by_provider.get(str(relationship.get("asset_id")))
            if part:
                relationship["package_part"] = part

    inventory_counts: dict[str, int] = {}
    for inventory_id in inventory_occurrence_ids:
        asset_id = inventory_to_provider.get(inventory_id)
        if asset_id is not None:
            inventory_counts[asset_id] = inventory_counts.get(asset_id, 0) + 1

    kept_resolved: dict[str, int] = {}
    surplus_node_ids: set[str] = set()
    reconciled_relationships: list[dict[str, Any]] = []
    surplus = 0
    for relationship in relationships:
        if relationship.get("type") != "image_occurrence" or relationship.get("placement") != "resolved":
            reconciled_relationships.append(relationship)
            continue
        asset_id = str(relationship.get("asset_id"))
        if kept_resolved.get(asset_id, 0) < inventory_counts.get(asset_id, 0):
            kept_resolved[asset_id] = kept_resolved.get(asset_id, 0) + 1
            reconciled_relationships.append(relationship)
            continue
        surplus += 1
        content_node_id = relationship.get("content_node_id")
        if isinstance(content_node_id, str):
            surplus_node_ids.add(content_node_id)
    if surplus_node_ids:
        content = [item for item in content if item.get("id") not in surplus_node_ids]
    relationships = reconciled_relationships
    if surplus:
        warnings.append(_ad_warning(
            "office_image_occurrence_unproved",
            f"Discarded {surplus} provider image occurrence(s) beyond the OOXML relationship inventory",
            unit_id,
            True,
        ))

    resolved_remaining = dict(kept_resolved)
    next_occurrence = max(
        (int(item.get("occurrence_index", 0)) for item in relationships if item.get("type") == "image_occurrence"),
        default=0,
    )
    unresolved = 0
    unavailable = 0
    for inventory_id in inventory_occurrence_ids:
        asset_id = inventory_to_provider.get(inventory_id)
        if asset_id is None:
            unavailable += 1
            continue
        if resolved_remaining.get(asset_id, 0):
            resolved_remaining[asset_id] -= 1
            continue
        next_occurrence += 1
        unresolved += 1
        inventory = inventory_by_id[inventory_id]
        relationships.append({
            "type": "image_occurrence",
            "source_unit_id": unit_id,
            "asset_id": asset_id,
            "occurrence_index": next_occurrence,
            "package_part": str(inventory.get("source_locator", {}).get("package_part") or ""),
            "placement": "unresolved",
        })
    if unresolved:
        warnings.append(_ad_warning(
            "office_image_position_unresolved",
            f"Could not prove canonical positions for {unresolved} of {len(inventory_occurrence_ids)} OOXML image occurrence(s)",
            unit_id,
            True,
        ))
    if unavailable:
        warnings.append(_ad_warning(
            "anydoc_image_asset_unavailable",
            f"AnyDoc did not export assets for {unavailable} relationship-proved OOXML image occurrence(s)",
            unit_id,
            True,
        ))
    _remove_owned_empty_image_dir(asset_dir)
    return assets, content, relationships


def _remove_owned_empty_image_dir(asset_dir: Path) -> None:
    """Remove only the converter-owned empty ``assets/images`` leaf."""
    if asset_dir.name != "images" or asset_dir.parent.name != "assets":
        return
    stage = asset_dir.parent.parent
    try:
        resolved_stage = stage.resolve(strict=True)
        resolved_images = asset_dir.resolve(strict=True)
        resolved_images.relative_to(resolved_stage)
        for node in (stage, asset_dir.parent, asset_dir):
            info = node.lstat()
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
                raise RuntimeError("AnyDoc image directory contains a linked path")
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("AnyDoc image directory is not an ordinary directory")
    except FileNotFoundError:
        return
    if next(asset_dir.iterdir(), None) is None:
        asset_dir.rmdir()


def _safe_asset_extension(media_type: str) -> str | None:
    return _ASSET_MEDIA_TYPES.get(media_type.lower())


def _asset_magic_matches(media_type: str, data: bytes) -> bool:
    if media_type.lower() == "image/webp":
        return data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP"
    signatures = {
        "image/png": (b"\x89PNG\r\n\x1a\n",),
        "image/jpeg": (b"\xff\xd8\xff",),
        "image/gif": (b"GIF87a", b"GIF89a"),
        "image/bmp": (b"BM",),
        "image/tiff": (b"II*\x00", b"MM\x00*"),
    }
    return any(data.startswith(signature) for signature in signatures.get(media_type.lower(), ()))


def _write_anydoc_assets(
    raw_assets: list[Any],
    document_id: str,
    unit_id: str,
    asset_dir: Path | None,
    warnings: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Validate and optionally publish AnyDoc image assets.

    AnyDoc retains non-image assets in its model.  Canonical v1 only has image
    assets, so those are reported and skipped without exposing package paths.
    """
    lookup: dict[int, dict[str, Any]] = {}
    assets: list[dict[str, Any]] = []
    total = 0
    for position, raw in enumerate(raw_assets or []):
        media_type = str(_ad_attr(raw, "media_type", "application/octet-stream") or "").lower()
        data = _ad_attr(raw, "data", b"") or b""
        if not isinstance(data, (bytes, bytearray)):
            raise RuntimeError(f"AnyDoc asset {position} has non-byte data")
        data = bytes(data)
        source_part = str(_ad_attr(raw, "origin_part", "") or "")
        raw_id = _ad_attr(raw, "id", position)
        try:
            asset_index = int(raw_id)
        except (TypeError, ValueError):
            asset_index = position
        if not media_type.startswith("image/"):
            warnings.append(_ad_warning(
                "anydoc_non_image_asset_not_preserved",
                f"Embedded AnyDoc asset {asset_index} ({media_type}) is not representable by Canonical v1",
                unit_id,
                True,
            ))
            continue
        extension = _safe_asset_extension(media_type)
        if extension is None:
            warnings.append(_ad_warning(
                "anydoc_image_media_type_not_supported",
                f"Embedded AnyDoc image asset {asset_index} has unsupported media type {media_type}",
                unit_id,
                True,
            ))
            continue
        if len(data) > _ASSET_MAX_BYTES:
            raise RuntimeError(f"AnyDoc image asset {asset_index} exceeds 100 MiB limit")
        total += len(data)
        if total > _ASSET_TOTAL_MAX_BYTES:
            raise RuntimeError("AnyDoc image assets exceed the 512 MiB total limit")
        if not _asset_magic_matches(media_type, data):
            warnings.append(_ad_warning(
                "anydoc_image_magic_mismatch",
                f"Embedded AnyDoc image asset {asset_index} does not match declared MIME {media_type}",
                unit_id,
                True,
            ))
            continue
        digest = hashlib.sha256(data).hexdigest()
        locator = {
            "source_unit_id": unit_id,
            "origin_part": source_part,
            "asset_index": asset_index,
            "model_path": ["assets", asset_index],
        }
        asset_id = stable_id("asset", document_id, locator, "image", position + 1)
        path = f"assets/images/{asset_id}{extension}"
        item = {
            "asset_id": asset_id,
            "type": "image",
            "path": path,
            "sha256": digest,
            "media_type": media_type,
            "source_locator": locator,
            "alt": "",
            "caption": "",
        }
        if asset_dir is not None:
            asset_dir.mkdir(parents=True, exist_ok=True)
            destination = asset_dir / f"{asset_id}{extension}"
            destination.write_bytes(data)
        lookup[asset_index] = item
        assets.append(item)
    return lookup, assets if asset_dir is not None else []


class AnyDocAdapter:
    name = "anydoc"
    limitations = [
        "page_slide_sheet_provenance_not_exposed",
        "rich_text_styles_flattened",
        "external_images_not_exported",
        "canonical_v1_non_image_assets_not_preserved",
    ]

    def extract(
        self,
        source: str,
        document_id: str,
        mode: str,
        asset_dir: Path | None = None,
        _allow_capacity_recovery: bool = True,
    ) -> dict[str, Any]:
        path = Path(source)
        fmt = anydoc_format_for_path(path)
        if fmt is None:
            raise RuntimeError(f"AnyDoc does not support local extension {path.suffix.lower()}")
        preflight = preflight_office(path)
        capability = anydoc_capability_check()
        package = _load_anydoc()
        raw_bytes, revision_counts = accepted_word_snapshot(path)
        try:
            extension_format = package.format_from_extension(path.suffix.lower())
        except Exception as exc:
            raise RuntimeError(f"AnyDoc extension format detection failed for {path.name}: {exc}") from exc
        if str(extension_format).lower() != fmt:
            raise RuntimeError(f"AnyDoc format_from_extension returned {extension_format!s} for {path.suffix}; expected {fmt}")
        try:
            detected = package.format_from_bytes(raw_bytes)
        except Exception as exc:
            raise RuntimeError(f"AnyDoc format detection failed for {path.name}: {exc}") from exc
        # CSV has no signature.  For all other container formats, reject a
        # signature/extension mismatch before writing any output.
        normalized_detected = str(detected).lower() if detected is not None else None
        if normalized_detected not in ANYDOC_ALLOWED_DETECTED_FORMATS and normalized_detected is not None:
            raise RuntimeError(f"AnyDoc returned unsupported detected format {detected!s}")
        if normalized_detected == "pdf":
            raise RuntimeError("AnyDoc detected PDF; use the PDF Inspector route for .pdf inputs")
        if normalized_detected is None and fmt != "csv":
            raise RuntimeError(f"AnyDoc could not identify {path.name}; only CSV permits extension fallback")
        if normalized_detected is not None and normalized_detected != fmt:
            raise RuntimeError(f"AnyDoc detected {detected!s} but extension requires {fmt}")
        try:
            try:
                document = package.to_document(raw_bytes, fmt)
            except TypeError:
                document = package.to_document(raw_bytes, format=fmt)
        except Exception as exc:
            if _allow_capacity_recovery and fmt == "docx" and _is_anydoc_max_xml_nodes(package, exc):
                oversized_non_body = [
                    part for part, count in preflight.xml_nodes_by_part.items()
                    if part != "word/document.xml" and count >= 2_000_000
                ]
                if oversized_non_body:
                    raise RuntimeError(
                        "AnyDoc max_xml_nodes occurred in a non-shardable Word part: "
                        + ", ".join(sorted(oversized_non_body))
                    ) from exc
                shard_results: list[tuple[int, int, dict[str, Any]]] = []
                shards = shard_docx_bytes(raw_bytes)
                with tempfile.TemporaryDirectory(prefix=".anydoc-docx-shards-") as shard_dir:
                    shard_root = Path(shard_dir)
                    for shard_index, (first_block, last_block, shard_bytes) in enumerate(shards, 1):
                        shard_path = shard_root / f"shard-{shard_index:04d}.docx"
                        shard_path.write_bytes(shard_bytes)
                        shard_result = self.extract(
                            str(shard_path), document_id, mode, asset_dir,
                            _allow_capacity_recovery=False,
                        )
                        shard_results.append((first_block, last_block, shard_result))
                original_unit_id = stable_id(
                    "unit", document_id, {"kind": "document", "index": 1}, "document", 1
                )
                return _merge_sharded_anydoc_results(
                    shard_results,
                    document_id,
                    inspect_ooxml_features(path, original_unit_id),
                )
            raise RuntimeError(f"AnyDoc could not convert {path.name}: {exc}") from exc
        _validate_anydoc_document(document)

        unit_locator = {"kind": "document", "index": 1}
        unit_id = stable_id("unit", document_id, unit_locator, "document", 1)
        source_unit = {
            "id": unit_id,
            "type": "document",
            "index": 1,
            "locator": unit_locator,
            "status": "complete",
            "warnings": [],
        }
        warnings: list[dict[str, Any]] = []

        def add_warning(item: dict[str, Any], dedupe: bool = False) -> None:
            if dedupe and any(existing.get("code") == item.get("code") for existing in warnings):
                return
            warnings.append(item)

        def mark_rich_loss() -> None:
            add_warning(_ad_warning(
                "anydoc_anchor_omitted",
                "An AnyDoc anchor was omitted because Canonical v1 has no inline anchor field",
                unit_id,
                True,
            ), dedupe=True)

        def mark_style_flattened() -> None:
            add_warning(_ad_warning(
                "anydoc_inline_style_flattened",
                "AnyDoc inline emphasis was flattened to visible text in Canonical v1",
                unit_id,
                False,
            ), dedupe=True)

        feature_warnings = inspect_ooxml_features(path, unit_id)
        warnings.extend(feature_warnings)
        raw_assets = list(_ad_attr(document, "assets", []) or [])
        asset_lookup, assets = _write_anydoc_assets(raw_assets, document_id, unit_id, asset_dir, warnings)
        # AnyDoc remains the sole source of canonical Office assets.  A small
        # OOXML image preflight is used only to surface loss when a package
        # references an unavailable image that AnyDoc necessarily omits from
        # its model; its temporary exports are never published or used as
        # canonical assets.  This keeps the adapter fail-closed while making
        # missing-image output explicitly loss-aware.
        inventory_assets: list[dict[str, Any]] = []
        inventory_occurrence_ids: list[str] = []
        if path.suffix.lower() in OOXML_SUFFIXES and path.exists():
            with tempfile.TemporaryDirectory(prefix=".anydoc-image-preflight-") as preflight_dir:
                try:
                    preflight_source = path
                    if revision_counts:
                        preflight_source = Path(preflight_dir) / path.name
                        preflight_source.write_bytes(raw_bytes)
                    inventory_assets, inventory_occurrence_ids, image_warnings = extract_ooxml_images(
                        preflight_source,
                        document_id,
                        unit_id,
                        Path(preflight_dir),
                    )
                except (OSError, zipfile.BadZipFile) as exc:
                    image_warnings = [_ad_warning(
                        "office_image_preflight_failed",
                        f"Could not inspect Office image relationships: {type(exc).__name__}: {exc}",
                        unit_id,
                        True,
                    )]
                warnings.extend(image_warnings)
        content: list[dict[str, Any]] = []
        tables: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []
        occurrence = 0
        image_occurrence = 0

        def locator(
            block_index: int,
            kind: str,
            extra: dict[str, Any] | None = None,
            model_path: list[Any] | None = None,
        ) -> dict[str, Any]:
            value: dict[str, Any] = {
                "source_unit_id": unit_id,
                "block_index": block_index,
                "block_kind": kind,
                "model_path": list(model_path or ["blocks", block_index]),
            }
            if extra:
                value.update(extra)
            return value

        def add_text(kind: str, raw: str, source_locator: dict[str, Any], **extra: Any) -> None:
            nonlocal occurrence
            if not raw.strip() and kind != "code":
                return
            occurrence += 1
            content.append({
                "id": stable_id("node", document_id, source_locator, kind, occurrence),
                "type": kind,
                "source_locator": source_locator,
                **make_text_fields(raw, raw if kind == "code" else raw.strip(), mode, defer=True),
                **extra,
            })

        def add_image(
            asset_index: int,
            alt: str,
            source_locator: dict[str, Any],
            fallback_kind: str = "paragraph",
            fallback_extra: dict[str, Any] | None = None,
        ) -> None:
            nonlocal occurrence, image_occurrence
            image_occurrence += 1
            asset = asset_lookup.get(asset_index)
            if asset is None:
                warnings.append(_ad_warning(
                    "anydoc_image_asset_unavailable",
                    f"AnyDoc image asset {asset_index} was referenced but not exported",
                    unit_id,
                    True,
                ))
                if alt:
                    add_text(fallback_kind, alt, source_locator, **(fallback_extra or {}))
                return
            if asset_dir is None:
                if alt:
                    add_text(fallback_kind, alt, source_locator, **(fallback_extra or {}))
                add_warning(_ad_warning(
                    "anydoc_markdown_asset_omitted",
                    "Embedded AnyDoc image bytes were omitted in markdown-only mode; available alt text was retained",
                    unit_id,
                    False,
                ), dedupe=True)
                return
            if alt and not asset["alt"]:
                asset["alt"] = alt
            node_locator = {
                **source_locator,
                "image_occurrence": image_occurrence,
                "model_path": source_locator.get("model_path", []) + ["inline", image_occurrence],
            }
            occurrence += 1
            node_id = stable_id("node", document_id, node_locator, "image", occurrence)
            content.append({
                "id": node_id,
                "type": "image",
                "source_locator": node_locator,
                "asset_id": asset["asset_id"],
            })
            relationships.append({
                "type": "image_occurrence",
                "source_unit_id": unit_id,
                "asset_id": asset["asset_id"],
                "occurrence_index": image_occurrence,
                "placement": "resolved",
                "content_node_id": node_id,
            })

        def emit_inlines(
            inlines: list[Any],
            source_locator: dict[str, Any],
            first_kind: str = "paragraph",
            first_extra: dict[str, Any] | None = None,
        ) -> None:
            pending: list[str] = []
            emitted_text = False
            saw_text = False
            saw_image = False

            def flush() -> None:
                nonlocal emitted_text, saw_text
                text = "".join(pending)
                pending.clear()
                if not text.strip():
                    return
                kind = first_kind if not emitted_text else "paragraph"
                extra = dict(first_extra or {}) if not emitted_text else {}
                add_text(kind, text, source_locator, **extra)
                emitted_text = True
                saw_text = True

            if _ad_has_rich_loss(inlines):
                mark_rich_loss()
            if _ad_has_rich_style(inlines):
                mark_style_flattened()
            for segment_kind, value in _ad_inline_segments(inlines):
                if segment_kind == "image":
                    flush()
                    saw_image = True
                    index, alt = value
                    add_image(
                        index,
                        alt,
                        source_locator,
                        fallback_kind=first_kind if not emitted_text else "paragraph",
                        fallback_extra=first_extra if not emitted_text else None,
                    )
                elif segment_kind == "external_image":
                    flush()
                    saw_image = True
                    if value:
                        pending.append(value)
                    warnings.append(_ad_warning(
                        "anydoc_external_image_not_exported",
                        "External or unavailable image was reduced to its text alternative",
                        unit_id,
                        True,
                    ))
                elif segment_kind == "link_target":
                    if value in {"external", "relative", "anchor"}:
                        add_warning(_ad_warning(
                            "anydoc_link_target_flattened",
                            "Link target was flattened to visible text in Canonical v1",
                            unit_id,
                            True,
                        ), dedupe=True)
                elif segment_kind == "note_ref":
                    add_warning(_ad_warning(
                        "anydoc_note_reference_omitted",
                        "AnyDoc note reference was omitted because Canonical v1 has no linkage field",
                        unit_id,
                        True,
                    ), dedupe=True)
                elif segment_kind == "anchor":
                    mark_rich_loss()
                else:
                    pending.append(str(value or ""))
            flush()
            if saw_image and saw_text:
                add_warning(_ad_warning(
                    "anydoc_inline_structure_flattened",
                    "Inline text/image structure was split into ordered Canonical v1 nodes",
                    unit_id,
                    False,
                ), dedupe=True)

        def emit_table(table: Any, block_index: int, source_locator: dict[str, Any]) -> None:
            nonlocal occurrence
            grid = _ad_attr(table, "grid", []) or []
            raw_rows: list[list[str]] = []
            rows: list[list[dict[str, Any]]] = []
            seen_origins: set[tuple[int, int]] = set()
            for row_index, row in enumerate(grid):
                raw_row: list[str] = []
                canonical_row: list[dict[str, Any]] = []
                for col_index, slot in enumerate(row or []):
                    if _ad_kind(slot) == "covered":
                        raw_row.append("")
                        canonical_row.append({
                            **make_text_fields("", "", mode, defer=True),
                            "value": None,
                            "rowspan": 1,
                            "colspan": 1,
                        })
                        continue
                    if _ad_kind(slot) != "origin":
                        continue
                    cell = _ad_attr(slot, "cell", None)
                    if cell is None or (row_index, col_index) in seen_origins:
                        continue
                    seen_origins.add((row_index, col_index))
                    cell_blocks = _ad_attr(cell, "blocks", []) or []
                    value = _ad_text_blocks(cell_blocks)
                    if _ad_blocks_contain_inline_kind(cell_blocks, "image"):
                        add_warning(_ad_warning(
                            "anydoc_inline_structure_flattened",
                            "Inline image structure in a table cell was flattened to cell text",
                            unit_id,
                            True,
                        ), dedupe=True)
                    if any(_ad_kind(child) == "table" for child in cell_blocks):
                        warnings.append(_ad_warning(
                            "anydoc_nested_table_flattened",
                            "A nested AnyDoc table in a cell was flattened to row-major text",
                            unit_id,
                            True,
                        ))
                    raw_row.append(value)
                    canonical_row.append({
                        **make_text_fields(value, value, mode, defer=True),
                        "value": _numeric_value(value),
                        "rowspan": max(1, int(_ad_attr(cell, "row_span", 1) or 1)),
                        "colspan": max(1, int(_ad_attr(cell, "col_span", 1) or 1)),
                    })
                if raw_row:
                    raw_rows.append(raw_row)
                    rows.append(canonical_row)
            if not rows:
                warnings.append(_ad_warning("anydoc_empty_table", "An AnyDoc table contained no origin cells", unit_id, True))
                return
            occurrence += 1
            table_id = stable_id("table", document_id, source_locator, "table", occurrence)
            header_rows = int(_ad_attr(table, "header_rows", 0) or 0)
            table_record: dict[str, Any] = {
                "table_id": table_id,
                "source_locator": source_locator,
                "raw_rows": raw_rows,
                "rows": rows,
                "confidence": 1.0,
                "warnings": [],
            }
            if header_rows > 0 and rows:
                table_record["headers"] = [cell["text"] for cell in rows[0]]
                if header_rows > 1:
                    header_warning = _ad_warning(
                        "anydoc_multirow_header_flattened",
                        "Only the first AnyDoc table header row is represented in Canonical v1",
                        unit_id,
                        True,
                    )
                    table_record["warnings"].append(header_warning)
                    warnings.append(header_warning)
            if str(_ad_attr(table, "kind", "data") or "data") == "layout":
                layout_warning = _ad_warning(
                    "anydoc_layout_table_preserved",
                    "AnyDoc classified this table as layout scaffolding; values were preserved as a table",
                    unit_id,
                    False,
                )
                table_record["warnings"].append(layout_warning)
                warnings.append(layout_warning)
            tables.append(table_record)
            content.append({
                "id": stable_id("node", document_id, source_locator, "table", occurrence),
                "type": "table",
                "source_locator": source_locator,
                "table_id": table_id,
            })

        def emit_block(
            block: Any,
            block_index: int,
            nesting: int = 0,
            model_path: list[Any] | None = None,
        ) -> None:
            kind = _ad_kind(block)
            source_locator = locator(block_index, kind, model_path=model_path)
            if kind == "heading":
                heading_inlines = _ad_attr(block, "content", []) or []
                level = max(1, min(6, int(_ad_attr(block, "level", 1) or 1)))
                emit_inlines(heading_inlines, source_locator, first_kind="heading", first_extra={"level": level})
            elif kind == "paragraph":
                emit_inlines(_ad_attr(block, "content", []) or [], source_locator)
            elif kind == "code_block":
                add_text("code", str(_ad_attr(block, "text", "") or ""), source_locator, language=str(_ad_attr(block, "lang", "") or ""))
            elif kind == "table":
                emit_table(_ad_attr(block, "table", None), block_index, source_locator)
            elif kind == "list":
                listing = _ad_attr(block, "list", block)
                marker = str(_ad_attr(listing, "marker", "bullet") or "bullet")
                ordered = marker != "bullet"
                raw_start = _ad_attr(listing, "start", 1)
                ordinal = int(raw_start) if raw_start is not None else 1
                if ordinal == 0:
                    ordinal = 1
                    warnings.append(_ad_warning(
                        "anydoc_list_start_clamped",
                        "AnyDoc list start 0 was clamped to ordinal 1",
                        unit_id,
                        True,
                    ))
                for item_index, item in enumerate(_ad_attr(listing, "items", []) or [], start=1):
                    item_blocks = _ad_attr(item, "blocks", []) or []
                    nested_lists = [child for child in item_blocks if _ad_kind(child) == "list"]
                    checked = _ad_attr(item, "checked", None)
                    marker_label = _ad_attr(item, "marker_label", None)
                    item_locator = {
                        **source_locator,
                        "list_ordinal": ordinal,
                        "marker": marker,
                        "nesting": nesting,
                        "task": checked is not None,
                        "checked": checked,
                        "marker_label": str(marker_label) if marker_label is not None else "",
                        "model_path": source_locator["model_path"] + ["list", "items", item_index],
                    }
                    emitted_item = False
                    item_extra = {"ordered": ordered, "ordinal": ordinal}
                    for child_index, child in enumerate(item_blocks, start=1):
                        child_kind = _ad_kind(child)
                        if child_kind == "list":
                            continue
                        before = len(content)
                        if child_kind == "paragraph":
                            emit_inlines(
                                _ad_attr(child, "content", []) or [],
                                item_locator,
                                first_kind="list_item" if not emitted_item else "paragraph",
                                first_extra=item_extra if not emitted_item else None,
                            )
                        else:
                            value = _ad_text_blocks([child], include_lists=False)
                            if value:
                                add_text(
                                    "list_item" if not emitted_item else "paragraph",
                                    value,
                                    item_locator,
                                    **(item_extra if not emitted_item else {}),
                                )
                        if len(content) > before:
                            emitted_item = True
                    if nested_lists:
                        add_warning(_ad_warning(
                            "anydoc_nested_block_structure_flattened",
                            "Nested AnyDoc list containment was flattened to canonical list items",
                            unit_id,
                            True,
                        ), dedupe=True)
                    for child_index, child in enumerate(item_blocks, start=1):
                        if _ad_kind(child) == "list":
                            emit_block(child, block_index, nesting=nesting + 1, model_path=item_locator["model_path"] + ["blocks", child_index])
                    ordinal += 1
            elif kind == "block_quote":
                add_warning(_ad_warning(
                    "anydoc_nested_block_structure_flattened",
                    "AnyDoc block-quote containment was flattened to canonical blocks",
                    unit_id,
                    True,
                ), dedupe=True)
                for child_index, child in enumerate(_ad_attr(block, "blocks", []) or [], start=1):
                    emit_block(
                        child,
                        block_index,
                        nesting=nesting + 1,
                        model_path=source_locator["model_path"] + ["quote", child_index],
                    )
            elif kind == "rule":
                warnings.append(_ad_warning("anydoc_rule_omitted", "A horizontal rule is not representable by Canonical v1 and was omitted", unit_id, False))
            else:
                text = _ad_text_blocks([block])
                if text:
                    add_text("paragraph", text, source_locator)

        for block_index, block in enumerate(_ad_attr(document, "blocks", []) or [], start=1):
            emit_block(block, block_index)
        notes = _ad_attr(document, "notes", []) or []
        for note_index, note in enumerate(notes, start=1):
            note_text = _ad_text_blocks(_ad_attr(note, "blocks", []) or [])
            if note_text:
                note_locator = {
                    "source_unit_id": unit_id,
                    "note_index": note_index,
                    "note_id": str(_ad_attr(note, "id", "") or ""),
                    "note_kind": str(_ad_attr(note, "kind", "note") or "note"),
                    "model_path": ["notes", note_index],
                }
                add_text("paragraph", note_text, note_locator)
                add_warning(_ad_warning(
                    "anydoc_notes_flattened",
                    f"AnyDoc {_ad_attr(note, 'kind', 'note')} {str(_ad_attr(note, 'id', ''))} was appended as a flattened note block",
                    unit_id,
                    True,
                ), dedupe=True)
        if (
            asset_dir is not None
            and path.suffix.lower() in OOXML_SUFFIXES
            and zipfile.is_zipfile(path)
        ):
            assets, content, relationships = _reconcile_anydoc_ooxml_images(
                assets,
                content,
                relationships,
                inventory_assets,
                inventory_occurrence_ids,
                asset_dir,
                unit_id,
                warnings,
            )
        if not content:
            raise RuntimeError(f"AnyDoc produced no usable content for {path.name}")
        normalize_canonical_text(content, tables, mode)
        source_unit["warnings"].extend(warnings)
        if warnings:
            source_unit["status"] = "warning"
        title_source = next((node["normalized_text"] for node in content if node["type"] == "heading" and node.get("level") == 1), None)
        return {
            "source_units": [source_unit],
            "content": content,
            "tables": tables,
            "assets": assets,
            "relationships": relationships,
            "warnings": warnings,
            "title": title_source or path.stem,
            "adapter": {
                "name": self.name,
                "version": capability["version"],
                "limitations": list(self.limitations),
            },
        }


class MarkItDownAdapter:
    name = "markitdown"
    limitations = [
        "tracked_changes_flattened_to_accepted_view",
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
        if path.exists() and anydoc_format_for_path(path) is not None:
            preflight_office(path)
        conversion_source = source
        accepted_temp: Path | None = None
        if path.exists() and path.suffix.lower() in {".docx", ".docm"}:
            accepted_bytes, revisions = accepted_word_snapshot(path)
            if revisions:
                temporary = tempfile.NamedTemporaryFile(suffix=path.suffix, delete=False)
                temporary.write(accepted_bytes)
                temporary.close()
                accepted_temp = Path(temporary.name)
                conversion_source = str(accepted_temp)
        try:
            conversion_path = Path(conversion_source)
            if conversion_path.exists() and asset_dir is not None and conversion_path.suffix.lower() in OOXML_SUFFIXES:
                assets, image_occurrences, image_warnings = extract_ooxml_images(
                    conversion_path,
                    document_id,
                    unit_id,
                    asset_dir,
                )
            try:
                markdown = convert_basic(conversion_source)
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
                    if not create_sanitized_ooxml_copy(conversion_path, sanitized):
                        raise
                    markdown = convert_basic(str(sanitized))
                finally:
                    sanitized.unlink(missing_ok=True)
        finally:
            if accepted_temp is not None:
                accepted_temp.unlink(missing_ok=True)
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
