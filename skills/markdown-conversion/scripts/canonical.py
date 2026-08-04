"""Canonical document model, normalization, rendering, and validation for v6."""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
QUALITY_STATUSES = {"complete", "complete_with_warnings", "partial"}
TEXT_NODE_TYPES = {"heading", "paragraph", "list_item", "code", "boilerplate", "page_label"}

MOJIBAKE_PATTERNS = ["茂驴陆", "脙陇", "芒鈧", "脙漏", "脙篓", "脙鹿", "脙禄"]
_mojibake_re = re.compile("|".join(re.escape(value) for value in MOJIBAKE_PATTERNS))
_h1_re = re.compile(r"^ {0,3}#(?!#)\s+(.+?)(?:\s+#+\s*)?$")
_fence_re = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_inline_image_re = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_orphan_image_re = re.compile(
    r"^\s*\S+\.(?:jpg|jpeg|png|gif|bmp|svg|webp|tiff?)\s*$", re.IGNORECASE
)
_protected_re = re.compile(
    r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`|"
    r"https?://[^\s<>)]+|"
    r"\b[A-Za-z]:\\[^\s<>\"']+|"
    r"(?<!\w)/(?:[\w.~-]+/)+[\w.~-]+|"
    r"\b(?:sha256:)?[0-9a-fA-F]{32,64}\b|"
    r"\$[^$\n]+\$|\\\([^\n]+?\\\)|"
    r"\b[A-Z][A-Z0-9_-]{2,}\d[A-Z0-9_-]*\b"
)


class CanonicalValidationError(ValueError):
    """Raised when a canonical artifact cannot safely be published."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, document_id: str, locator: Any, node_type: str, occurrence: int) -> str:
    payload = json.dumps(
        [document_id, locator, node_type, occurrence],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}-{sha256_bytes(payload)[:16]}"


def fix_encoding(raw_bytes: bytes) -> str:
    """Decode actual byte input once; in-memory Unicode should bypass this function."""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            import chardet
        except ImportError as exc:  # pragma: no cover - dependency contract
            raise CanonicalValidationError("chardet is required to decode non-UTF-8 input") from exc
        detected = chardet.detect(raw_bytes)
        candidates = []
        if detected.get("encoding") and float(detected.get("confidence") or 0) >= 0.7:
            candidates.append(str(detected["encoding"]))
        candidates.extend(["gb18030", "big5", "shift_jis", "euc-jp", "euc-kr"])
        text = ""
        for encoding in dict.fromkeys(candidates):
            try:
                text = raw_bytes.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            raise CanonicalValidationError("Could not detect file encoding")
    if _mojibake_re.search(text):
        raise CanonicalValidationError("Encoding fix produced garbled output")
    return text


@lru_cache(maxsize=3)
def _opencc_converter(profile: str):
    try:
        import opencc
    except ImportError as exc:  # pragma: no cover - dependency contract
        raise CanonicalValidationError("opencc-python-reimplemented is required") from exc
    return opencc.OpenCC(profile)


def _protect_spans(text: str) -> tuple[str, list[str]]:
    protected: list[str] = []

    def replace(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\ue000{len(protected) - 1}\ue001"

    return _protected_re.sub(replace, text), protected


def _restore_spans(text: str, protected: list[str]) -> str:
    for index, value in enumerate(protected):
        text = text.replace(f"\ue000{index}\ue001", value)
    return text


def convert_chinese(text: str, mode: str = "simplified") -> str:
    """Perform one protected OpenCC pass; runtime does not repeat stability checks."""
    if mode == "preserve":
        return text
    profiles = {"simplified": "t2s", "traditional": "s2t"}
    if mode not in profiles:
        raise CanonicalValidationError(f"Unsupported language normalization: {mode}")
    masked, protected = _protect_spans(text)
    converted = _opencc_converter(profiles[mode]).convert(masked)
    return _restore_spans(converted, protected)


def make_text_fields(raw_text: str, text: str | None, mode: str, defer: bool = False) -> dict[str, str]:
    cleaned = raw_text if text is None else text
    return {
        "raw_text": raw_text,
        "text": cleaned,
        "normalized_text": cleaned if defer else convert_chinese(cleaned, mode),
    }


def normalize_canonical_text(
    content: list[dict[str, Any]], tables: list[dict[str, Any]], mode: str
) -> None:
    """Normalize all visible canonical text with one OpenCC call per document."""
    def sync_headers() -> None:
        for table in tables:
            if table.get("headers") is not None and table.get("rows"):
                table["headers"] = [cell["normalized_text"] for cell in table["rows"][0]]

    records: list[dict[str, Any]] = [
        node for node in content if node.get("type") in TEXT_NODE_TYPES - {"code"}
    ]
    records.extend(cell for table in tables for row in table.get("rows", []) for cell in row)
    if not records:
        return
    if mode == "preserve":
        for record in records:
            record["normalized_text"] = record["text"]
        sync_headers()
        return
    separator = "\n\ue0fe\ue0ff\n"
    while any(separator in str(record.get("text", "")) for record in records):
        separator += "\ue0fd"
    joined = separator.join(str(record.get("text", "")) for record in records)
    converted = convert_chinese(joined, mode)
    values = converted.split(separator)
    if len(values) != len(records):
        raise CanonicalValidationError("language normalization changed the canonical record boundary")
    for record, value in zip(records, values, strict=True):
        record["normalized_text"] = value
    sync_headers()


def strip_images(text: str) -> str:
    """Legacy helper: strip images outside fenced code while retaining ordinary links."""
    output: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        marker = _fence_re.match(line)
        if fence_char is not None:
            output.append(line)
            if re.fullmatch(rf" {{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*", line):
                fence_char = None
            continue
        if marker:
            fence_char = marker.group(1)[0]
            fence_length = len(marker.group(1))
            output.append(line)
            continue
        line = _inline_image_re.sub("", line)
        if not _orphan_image_re.fullmatch(line):
            output.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(output))


def title_from_markdown(text: str, fallback: str) -> str:
    fence_char: str | None = None
    fence_length = 0
    for line in text.splitlines():
        if fence_char is not None:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*", line):
                fence_char = None
            continue
        fence = _fence_re.match(line)
        if fence:
            fence_char = fence.group(1)[0]
            fence_length = len(fence.group(1))
            continue
        match = _h1_re.match(line)
        if match:
            title = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", match.group(1))
            title = re.sub(r"[*_`]+", "", title).strip()
            if title:
                return title
    return fallback or "untitled"


def frontmatter(title: str, timestamp: str) -> str:
    return (
        "---\n"
        "type: \"\"\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        "description: \"\"\n"
        "tags: []\n"
        f"timestamp: {json.dumps(timestamp, ensure_ascii=False)}\n"
        "---\n\n"
    )


def inject_frontmatter(text: str, source: str, timestamp: str) -> str:
    fallback = Path(source).stem if source else "untitled"
    title = title_from_markdown(text, fallback) if source.lower().startswith(("http://", "https://")) else fallback
    return frontmatter(title or "untitled", timestamp) + text


def quality_from_warnings(warnings: list[dict[str, Any]]) -> str:
    if any(bool(item.get("content_loss")) for item in warnings):
        return "partial"
    return "complete_with_warnings" if warnings else "complete"


def _cell_text(cell: dict[str, Any]) -> str:
    return str(cell.get("normalized_text", cell.get("text", cell.get("raw_text", ""))))


def render_table(table: dict[str, Any]) -> str:
    rows = table.get("rows") or []
    if not rows:
        return ""
    matrix = [[_cell_text(cell) for cell in row] for row in rows]
    rectangular = len({len(row) for row in matrix}) == 1 and all(
        int(cell.get("rowspan", 1)) == 1 and int(cell.get("colspan", 1)) == 1
        for row in rows
        for cell in row
    )
    if rectangular and matrix[0]:
        def escaped(value: str) -> str:
            return value.replace("|", "\\|").replace("\n", " ").strip()

        width = len(matrix[0])
        headers = table.get("headers")
        output: list[str] = []
        if isinstance(headers, list) and len(headers) == width:
            output.append("| " + " | ".join(escaped(str(value)) for value in headers) + " |")
            data_rows = matrix[1:]
        else:
            output.append("| " + " | ".join("" for _ in range(width)) + " |")
            data_rows = matrix
        output.append("| " + " | ".join("---" for _ in range(width)) + " |")
        output.extend("| " + " | ".join(escaped(value) for value in row) + " |" for row in data_rows)
        return "\n".join(output)
    return "\n".join(" | ".join(value.strip() for value in row if value.strip()) for row in matrix).strip()


def render_markdown(document: dict[str, Any], include_frontmatter: bool = True, output_mode: str = "bundle") -> str:
    tables = {item["table_id"]: item for item in document.get("tables", [])}
    assets = {item["asset_id"]: item for item in document.get("assets", [])}
    lines: list[str] = []
    for node in document.get("content", []):
        kind = node["type"]
        text = str(node.get("normalized_text", node.get("text", ""))).strip()
        if kind == "heading" and text:
            lines.append(f"{'#' * max(1, min(int(node.get('level', 1)), 6))} {text}")
        elif kind == "paragraph" and text:
            lines.append(text)
        elif kind == "list_item" and text:
            marker = f"{int(node.get('ordinal', 1))}." if node.get("ordered") else "-"
            lines.append(f"{marker} {text}")
        elif kind == "code":
            lines.append(
                f"```{node.get('language', '')}".rstrip()
                + "\n"
                + str(node.get("text", ""))
                + "\n```"
            )
        elif kind in {"boilerplate", "page_label"} and text:
            continue
        elif kind == "table":
            rendered = render_table(tables[node["table_id"]])
            if rendered:
                lines.append(rendered)
        elif kind == "image":
            asset = assets[node["asset_id"]]
            caption = str(asset.get("caption") or "").strip()
            alt = str(asset.get("alt") or "").strip()
            if output_mode == "markdown":
                label = caption or alt
                if label:
                    lines.append(label)
            else:
                label = alt or caption
                lines.append(f"![{label}]({asset['path']})")
                if caption and caption != alt:
                    lines.append(f"*{caption}*")
    body = "\n\n".join(part for part in lines if part).rstrip() + "\n"
    if include_frontmatter:
        return frontmatter(document["document"]["title"], document["document"]["conversion_timestamp"]) + body
    return body


def _all_ids(document: dict[str, Any]) -> Iterable[str]:
    yield from (unit["id"] for unit in document.get("source_units", []))
    yield from (node["id"] for node in document.get("content", []))
    yield from (table["table_id"] for table in document.get("tables", []))
    yield from (asset["asset_id"] for asset in document.get("assets", []))


def validate_canonical(
    document: dict[str, Any],
    bundle_root: Path | None = None,
    validate_schema: bool = True,
) -> None:
    if document.get("quality", {}).get("status") not in QUALITY_STATUSES:
        raise CanonicalValidationError("invalid quality status")
    if not any(
        node.get("type") in {"heading", "paragraph", "list_item", "code", "table", "image"}
        for node in document.get("content", [])
    ):
        raise CanonicalValidationError("no usable content nodes were extracted")
    expected_document_id = f"sha256:{document.get('source', {}).get('sha256', '')}"
    if document.get("document", {}).get("document_id") != expected_document_id:
        raise CanonicalValidationError("document_id does not match source SHA-256")
    ids = list(_all_ids(document))
    if len(ids) != len(set(ids)):
        raise CanonicalValidationError("canonical ids are not unique")
    table_ids = {item["table_id"] for item in document.get("tables", [])}
    asset_ids = {item["asset_id"] for item in document.get("assets", [])}
    source_unit_ids = {item["id"] for item in document.get("source_units", [])}
    locators = [node.get("source_locator", {}) for node in document.get("content", [])]
    locators.extend(table.get("source_locator", {}) for table in document.get("tables", []))
    locators.extend(asset.get("source_locator", {}) for asset in document.get("assets", []))
    for locator in locators:
        spans = locator.get("spans", [])
        if spans is not None and not isinstance(spans, list):
            raise CanonicalValidationError("source locator spans must be a list")
        for span in spans or []:
            source_unit_id = span.get("source_unit_id") if isinstance(span, dict) else None
            if source_unit_id and source_unit_id not in source_unit_ids:
                raise CanonicalValidationError(f"dangling source-unit span reference: {source_unit_id}")
    for node in document["content"]:
        source_unit_id = node.get("source_locator", {}).get("source_unit_id")
        if source_unit_id and source_unit_id not in source_unit_ids:
            raise CanonicalValidationError(f"dangling source-unit reference: {source_unit_id}")
        if node["type"] == "table" and node.get("table_id") not in table_ids:
            raise CanonicalValidationError(f"dangling table reference: {node.get('table_id')}")
        if node["type"] == "image" and node.get("asset_id") not in asset_ids:
            raise CanonicalValidationError(f"dangling asset reference: {node.get('asset_id')}")
        if node["type"] in TEXT_NODE_TYPES:
            for field in ("raw_text", "text", "normalized_text"):
                if field not in node:
                    raise CanonicalValidationError(f"text node lacks {field}: {node['id']}")
    for asset in document.get("assets", []):
        relative = PurePosixPath(asset["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise CanonicalValidationError(f"asset path escapes bundle: {asset['path']}")
        if bundle_root is not None:
            path = bundle_root.joinpath(*relative.parts)
            try:
                path.resolve().relative_to(bundle_root.resolve())
            except ValueError as exc:
                raise CanonicalValidationError(f"asset path escapes bundle: {asset['path']}") from exc
            if not path.is_file():
                raise CanonicalValidationError(f"asset does not exist: {asset['path']}")
            if sha256_file(path) != asset["sha256"]:
                raise CanonicalValidationError(f"asset hash mismatch: {asset['path']}")
    expected_status = quality_from_warnings(document.get("quality", {}).get("warnings", []))
    if document.get("quality", {}).get("status") != expected_status:
        raise CanonicalValidationError("quality status does not match warning loss semantics")
    output = document.get("outputs", {})
    output_assets = {(item.get("path"), item.get("sha256")) for item in output.get("assets", [])}
    canonical_assets = {(item.get("path"), item.get("sha256")) for item in document.get("assets", [])}
    if output_assets != canonical_assets:
        raise CanonicalValidationError("outputs.assets does not match canonical assets")
    markdown_output = output.get("markdown", {})
    markdown_relative = PurePosixPath(str(markdown_output.get("path", "")))
    if markdown_relative.is_absolute() or ".." in markdown_relative.parts:
        raise CanonicalValidationError("Markdown output path escapes bundle")
    if bundle_root is not None:
        markdown_path = bundle_root.joinpath(*markdown_relative.parts)
        if not markdown_path.is_file():
            raise CanonicalValidationError("rendered Markdown output is missing")
        if sha256_file(markdown_path) != markdown_output.get("sha256"):
            raise CanonicalValidationError("rendered Markdown hash mismatch")
    if not validate_schema:
        return
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "canonical-v1.schema.json"
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover - dependency contract
        raise CanonicalValidationError("jsonschema is required for bundle validation") from exc
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        location = "/".join(str(value) for value in first.path) or "$"
        raise CanonicalValidationError(f"canonical schema {location}: {first.message}")
