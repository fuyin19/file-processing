"""Safe, deterministic embedded-image extraction for OOXML Office packages."""
from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from canonical import stable_id


OOXML_SUFFIXES = {".docx", ".docm", ".pptx", ".pptm", ".ppsx", ".ppsm", ".xlsx", ".xlsm"}
RELATIONSHIP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
IMAGE_CONTAINER_NAMES = {
    "inline",
    "anchor",
    "pic",
    "twoCellAnchor",
    "oneCellAnchor",
    "absoluteAnchor",
}
IMAGE_MIME_OVERRIDES = {
    ".bmp": "image/bmp",
    ".emf": "image/emf",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".wmf": "image/wmf",
}


def _warning(code: str, message: str, source_unit: str, content_loss: bool) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "content_loss": content_loss,
        "source_unit": source_unit,
    }


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _natural_key(value: str) -> list[tuple[int, int | str]]:
    return [
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", value)
        if part
    ]


def _relationship_part(part: str) -> str:
    parent = posixpath.dirname(part)
    name = posixpath.basename(part)
    return posixpath.join(parent, "_rels", f"{name}.rels")


def _resolve_relationship_target(part: str, target: str) -> str | None:
    candidate = target.replace("\\", "/")
    if candidate.startswith("/"):
        candidate = candidate.lstrip("/")
    else:
        candidate = posixpath.join(posixpath.dirname(part), candidate)
    normalized = posixpath.normpath(candidate)
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    return pure.as_posix()


def _part_priority(part: str) -> tuple[int, list[tuple[int, int | str]]]:
    if part == "word/document.xml":
        rank = 0
    elif part.startswith("ppt/slides/slide"):
        rank = 0
    elif part.startswith("xl/drawings/drawing"):
        rank = 0
    elif part.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes")):
        rank = 1
    else:
        rank = 2
    return rank, _natural_key(part)


def _relationship_references(root: ElementTree.Element) -> list[tuple[str, str]]:
    """Return relationship ids plus nearby alternative text in XML order."""
    result: list[tuple[str, str]] = []
    seen: set[tuple[int, str]] = set()

    def alt_text(container: ElementTree.Element) -> str:
        for item in container.iter():
            if _local_name(item.tag) not in {"docPr", "cNvPr"}:
                continue
            for key in ("descr", "title", "name"):
                value = str(item.attrib.get(key) or "").strip()
                if value:
                    return value
        return ""

    def collect(container: ElementTree.Element, alt: str) -> None:
        for item in container.iter():
            for attribute, relationship_id in item.attrib.items():
                key = (id(item), attribute)
                if key in seen or _local_name(attribute) not in {"embed", "link"}:
                    continue
                if attribute.startswith("{") and not attribute.startswith(f"{{{RELATIONSHIP_NS}}}"):
                    continue
                seen.add(key)
                result.append((relationship_id, alt))

    for item in root.iter():
        if _local_name(item.tag) in IMAGE_CONTAINER_NAMES:
            collect(item, alt_text(item))
    collect(root, "")
    return result


def _media_type(part: str) -> str:
    suffix = PurePosixPath(part).suffix.lower()
    return IMAGE_MIME_OVERRIDES.get(suffix) or mimetypes.guess_type(part)[0] or "application/octet-stream"


def _contains_relationship(element: ElementTree.Element, relationship_ids: set[str]) -> bool:
    return any(
        _local_name(attribute) in {"embed", "link"} and value in relationship_ids
        for item in element.iter()
        for attribute, value in item.attrib.items()
    )


def _remove_relationship_elements(root: ElementTree.Element, relationship_ids: set[str]) -> None:
    def prune(parent: ElementTree.Element) -> None:
        for child in list(parent):
            direct_reference = any(
                _local_name(attribute) in {"embed", "link"} and value in relationship_ids
                for attribute, value in child.attrib.items()
            )
            image_container = _local_name(child.tag) in IMAGE_CONTAINER_NAMES
            if direct_reference or (image_container and _contains_relationship(child, relationship_ids)):
                parent.remove(child)
            else:
                prune(child)

    prune(root)


def create_sanitized_ooxml_copy(source: Path, destination: Path) -> bool:
    """Copy OOXML while removing references to unavailable embedded images."""
    source = Path(source)
    if source.suffix.lower() not in OOXML_SUFFIXES or not zipfile.is_zipfile(source):
        return False
    replacements: dict[str, bytes] = {}
    with zipfile.ZipFile(source) as package:
        names = set(package.namelist())
        for part in sorted(
            (name for name in names if name.endswith(".xml") and _relationship_part(name) in names),
            key=_part_priority,
        ):
            try:
                relationships_root = ElementTree.fromstring(package.read(_relationship_part(part)))
                unavailable: set[str] = set()
                for item in relationships_root:
                    relationship_id = str(item.attrib.get("Id") or "")
                    relationship_type = str(item.attrib.get("Type") or "").rstrip("/").lower()
                    target = str(item.attrib.get("Target") or "")
                    external = str(item.attrib.get("TargetMode") or "").lower() == "external"
                    is_image = (
                        relationship_type == "image"
                        or relationship_type.endswith("/image")
                        or "/media/" in target.replace("\\", "/")
                    )
                    resolved = None if external else _resolve_relationship_target(part, target)
                    if is_image and (external or resolved is None or resolved not in names):
                        unavailable.add(relationship_id)
                if not unavailable:
                    continue
                document_root = ElementTree.fromstring(package.read(part))
                _remove_relationship_elements(document_root, unavailable)
                replacements[part] = ElementTree.tostring(
                    document_root,
                    encoding="utf-8",
                    xml_declaration=True,
                )
            except (ElementTree.ParseError, KeyError, OSError):
                continue
        if not replacements:
            return False
        with zipfile.ZipFile(destination, "w") as sanitized:
            for item in package.infolist():
                sanitized.writestr(item, replacements.get(item.filename, package.read(item.filename)))
    return True


def extract_ooxml_images(
    source: Path,
    document_id: str,
    source_unit_id: str,
    asset_dir: Path,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Extract referenced OOXML images and return assets, occurrence ids, warnings.

    Package members are read as bytes and written under stable generated names;
    archive paths are never used as filesystem destinations.
    """
    source = Path(source)
    if source.suffix.lower() not in OOXML_SUFFIXES or not zipfile.is_zipfile(source):
        return [], [], []

    warnings: list[dict[str, Any]] = []
    warned: set[tuple[str, str, str]] = set()

    def warn(code: str, message: str, content_loss: bool, part: str = "", ref: str = "") -> None:
        key = (code, part, ref)
        if key not in warned:
            warned.add(key)
            warnings.append(_warning(code, message, source_unit_id, content_loss))

    with zipfile.ZipFile(source) as package:
        names = set(package.namelist())
        media_parts = {
            name for name in names
            if "/media/" in name
            and not name.endswith("/")
            and _media_type(name).startswith("image/")
        }
        xml_parts = sorted(
            (
                name for name in names
                if name.endswith(".xml") and _relationship_part(name) in names
            ),
            key=_part_priority,
        )
        occurrences: list[tuple[str, str]] = []

        for part in xml_parts:
            relationships: dict[str, tuple[str | None, bool, bool]] = {}
            try:
                relationship_root = ElementTree.fromstring(package.read(_relationship_part(part)))
                for item in relationship_root:
                    relationship_id = str(item.attrib.get("Id") or "")
                    target = str(item.attrib.get("Target") or "")
                    relationship_type = str(item.attrib.get("Type") or "")
                    external = str(item.attrib.get("TargetMode") or "").lower() == "external"
                    normalized_type = relationship_type.rstrip("/").lower()
                    is_image = (
                        normalized_type == "image"
                        or normalized_type.endswith("/image")
                        or "/media/" in target.replace("\\", "/")
                    )
                    resolved = None if external else _resolve_relationship_target(part, target)
                    relationships[relationship_id] = (resolved, external, is_image)
                document_root = ElementTree.fromstring(package.read(part))
            except (ElementTree.ParseError, KeyError, OSError) as exc:
                warn(
                    "office_image_relationship_parse_failed",
                    f"Could not inspect image relationships in {part}: {type(exc).__name__}: {exc}",
                    True,
                    part,
                )
                continue

            for relationship_id, alt in _relationship_references(document_root):
                relationship = relationships.get(relationship_id)
                if relationship is None:
                    warn(
                        "office_image_relationship_missing",
                        f"Image relationship {relationship_id} in {part} is missing",
                        True,
                        part,
                        relationship_id,
                    )
                    continue
                target, external, is_image = relationship
                if not is_image:
                    continue
                if external:
                    warn(
                        "office_external_image_not_exported",
                        f"External Office image {relationship_id} in {part} was not downloaded",
                        True,
                        part,
                        relationship_id,
                    )
                    continue
                if target is None:
                    warn(
                        "office_image_target_unsafe",
                        f"Office image {relationship_id} in {part} has an unsafe package target",
                        True,
                        part,
                        relationship_id,
                    )
                    continue
                if target not in names:
                    warn(
                        "office_image_target_missing",
                        f"Office image target {target} referenced by {part} is missing",
                        True,
                        part,
                        relationship_id,
                    )
                    continue
                occurrences.append((target, alt))

        assets: list[dict[str, Any]] = []
        asset_by_part: dict[str, str] = {}
        first_alt: dict[str, str] = {}
        for part, alt in occurrences:
            if alt and not first_alt.get(part):
                first_alt[part] = alt
        for part, _ in occurrences:
            if part in asset_by_part:
                continue
            media_type = _media_type(part)
            if not media_type.startswith("image/"):
                warn(
                    "office_image_media_type_unsupported",
                    f"Office image {part} has unsupported media type {media_type}",
                    True,
                    part,
                )
                continue
            try:
                payload = package.read(part)
                suffix = PurePosixPath(part).suffix.lower() or ".bin"
                locator = {"source_unit_id": source_unit_id, "package_part": part}
                asset_id = stable_id("asset", document_id, locator, "image", len(assets) + 1)
                relative_path = f"assets/images/{asset_id}{suffix}"
                asset_dir.mkdir(parents=True, exist_ok=True)
                target = asset_dir / f"{asset_id}{suffix}"
                target.write_bytes(payload)
                record = {
                    "asset_id": asset_id,
                    "type": "image",
                    "path": relative_path,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "media_type": media_type,
                    "source_locator": locator,
                    "alt": first_alt.get(part) or PurePosixPath(part).stem,
                    "caption": "",
                }
                assets.append(record)
                asset_by_part[part] = asset_id
            except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
                warn(
                    "office_image_extraction_failed",
                    f"Could not export Office image {part}: {type(exc).__name__}: {exc}",
                    True,
                    part,
                )

    occurrence_ids = [asset_by_part[part] for part, _ in occurrences if part in asset_by_part]
    return assets, occurrence_ids, warnings
