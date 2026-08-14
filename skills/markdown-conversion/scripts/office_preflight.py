"""Bounded, read-only preflight shared by local Office/AnyDoc inputs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import zipfile
from xml.parsers import expat


MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_MEMBERS = 20_000
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 250.0
MAX_XML_DEPTH = 256
MAX_IMAGE_COUNT = 5_000
MAX_IMAGE_BYTES = 100 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 512 * 1024 * 1024
_XML_SUFFIXES = {".xml", ".rels", ".xhtml", ".svg"}
_IMAGE_SUFFIXES = {".bmp", ".emf", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp", ".wmf"}


@dataclass(frozen=True)
class OfficePreflight:
    source_bytes: int
    package_members: int
    expanded_bytes: int
    image_count: int
    image_bytes: int
    xml_nodes_by_part: dict[str, int]


def _safe_member_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name:
        return False
    value = PurePosixPath(name)
    return not value.is_absolute() and ".." not in value.parts and not re.match(r"^[A-Za-z]:", name)


def _count_xml(stream, part: str) -> int:
    """Count elements plus non-empty element text without building an XML tree."""
    depth = 0
    count = 0
    # Each frame is [non-empty text seen before first child, first child seen].
    # This matches the previous ElementTree element.text count while avoiding
    # hundreds of thousands of temporary Python element objects on long DOCX.
    stack: list[list[bool]] = []
    parser = expat.ParserCreate()

    def start(_name: str, _attributes: dict[str, str]) -> None:
        nonlocal depth, count
        if stack:
            parent = stack[-1]
            if parent[0] and not parent[1]:
                count += 1
            parent[1] = True
        depth += 1
        count += 1
        if depth > MAX_XML_DEPTH:
            raise RuntimeError(f"Office XML part {part} exceeds maximum depth {MAX_XML_DEPTH}")
        stack.append([False, False])

    def characters(value: str) -> None:
        if stack and not stack[-1][1] and value.strip():
            stack[-1][0] = True

    def end(_name: str) -> None:
        nonlocal depth, count
        frame = stack.pop()
        if frame[0] and not frame[1]:
            count += 1
        depth -= 1

    def inspect_doctype(_name, _system_id, _public_id, has_internal_subset) -> None:
        # EPUB XHTML commonly carries a plain HTML doctype. The dangerous
        # capability is an internal subset that can define expanding entities.
        if has_internal_subset:
            raise RuntimeError(f"Office XML part {part} contains a prohibited internal document type subset")

    parser.StartElementHandler = start
    parser.CharacterDataHandler = characters
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = inspect_doctype
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.ParseFile(stream)
    except expat.ExpatError as exc:
        raise RuntimeError(f"Office XML part {part} is malformed: {exc}") from exc
    return count


def preflight_office(path: Path) -> OfficePreflight:
    """Validate bounded container/resource properties without changing the source."""
    path = Path(path)
    source_bytes = path.stat().st_size
    if source_bytes > MAX_SOURCE_BYTES:
        raise RuntimeError(f"Office source exceeds {MAX_SOURCE_BYTES} bytes")
    if not zipfile.is_zipfile(path):
        return OfficePreflight(source_bytes, 0, 0, 0, 0, {})

    with zipfile.ZipFile(path) as package:
        infos = package.infolist()
        if len(infos) > MAX_PACKAGE_MEMBERS:
            raise RuntimeError(f"Office package exceeds {MAX_PACKAGE_MEMBERS} members")
        names: set[str] = set()
        expanded = 0
        image_count = 0
        image_bytes = 0
        xml_nodes: dict[str, int] = {}
        for info in infos:
            name = info.filename
            if not _safe_member_name(name):
                raise RuntimeError(f"Office package contains an unsafe member path: {name!r}")
            if name in names:
                raise RuntimeError(f"Office package contains a duplicate member: {name}")
            names.add(name)
            if info.flag_bits & 0x1:
                raise RuntimeError(f"Office package contains an encrypted member: {name}")
            if info.file_size > MAX_MEMBER_BYTES:
                raise RuntimeError(f"Office package member {name} exceeds {MAX_MEMBER_BYTES} expanded bytes")
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise RuntimeError(f"Office package exceeds {MAX_EXPANDED_BYTES} expanded bytes")
            if info.file_size and info.compress_size == 0:
                raise RuntimeError(f"Office package member {name} has an invalid compression size")
            ratio = info.file_size / max(info.compress_size, 1)
            if info.file_size >= 1024 * 1024 and ratio > MAX_COMPRESSION_RATIO:
                raise RuntimeError(f"Office package member {name} exceeds compression ratio {MAX_COMPRESSION_RATIO:g}")
            suffix = PurePosixPath(name).suffix.lower()
            if not info.is_dir() and suffix in _IMAGE_SUFFIXES:
                image_count += 1
                image_bytes += info.file_size
                if image_count > MAX_IMAGE_COUNT:
                    raise RuntimeError(f"Office package exceeds {MAX_IMAGE_COUNT} images")
                if info.file_size > MAX_IMAGE_BYTES:
                    raise RuntimeError(f"Office image {name} exceeds {MAX_IMAGE_BYTES} bytes")
                if image_bytes > MAX_IMAGE_TOTAL_BYTES:
                    raise RuntimeError(f"Office package images exceed {MAX_IMAGE_TOTAL_BYTES} bytes")

        for info in infos:
            if info.is_dir() or PurePosixPath(info.filename).suffix.lower() not in _XML_SUFFIXES:
                continue
            with package.open(info, "r") as stream:
                xml_nodes[info.filename] = _count_xml(stream, info.filename)

    return OfficePreflight(source_bytes, len(infos), expanded, image_count, image_bytes, xml_nodes)
