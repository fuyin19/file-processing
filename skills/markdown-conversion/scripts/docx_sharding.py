"""Bounded structural DOCX sharding for an upstream max_xml_nodes capacity limit."""
from __future__ import annotations

from copy import deepcopy
import io
from xml.etree import ElementTree
import zipfile


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
TARGET_NODES = 400_000
MAX_SINGLE_BLOCK_NODES = 1_500_000


def _subtree_nodes(element: ElementTree.Element) -> int:
    count = 0
    for item in element.iter():
        count += 1
        if item.text and item.text.strip():
            count += 1
    return count


def shard_docx_bytes(raw: bytes) -> list[tuple[int, int, bytes]]:
    """Return (first_block, last_block, bytes) shards with exact ordered coverage."""
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        names = set(package.namelist())
        if "word/document.xml" not in names:
            raise RuntimeError("DOCX capacity recovery requires word/document.xml")
        root = ElementTree.fromstring(package.read("word/document.xml"))
        body = root.find(f"{{{WORD_NS}}}body")
        if body is None:
            raise RuntimeError("DOCX capacity recovery could not find the Word body")
        blocks = list(body)
        if not blocks:
            raise RuntimeError("DOCX capacity recovery found no structural blocks")
        groups: list[tuple[int, int, list[ElementTree.Element]]] = []
        current: list[ElementTree.Element] = []
        current_nodes = 0
        first = 1
        for index, block in enumerate(blocks, 1):
            size = _subtree_nodes(block)
            if size > MAX_SINGLE_BLOCK_NODES:
                raise RuntimeError(
                    f"DOCX block {index} exceeds the indivisible recovery budget of {MAX_SINGLE_BLOCK_NODES} nodes"
                )
            if current and current_nodes + size > TARGET_NODES:
                groups.append((first, index - 1, current))
                current = []
                current_nodes = 0
                first = index
            current.append(block)
            current_nodes += size
        if current:
            groups.append((first, len(blocks), current))
        if len(groups) < 2:
            raise RuntimeError(
                "AnyDoc max_xml_nodes did not originate in a shardable Word body part"
            )

        shards: list[tuple[int, int, bytes]] = []
        covered: list[int] = []
        for first_index, last_index, group in groups:
            shard_root = deepcopy(root)
            shard_body = shard_root.find(f"{{{WORD_NS}}}body")
            if shard_body is None:  # pragma: no cover - proved above
                raise RuntimeError("DOCX capacity recovery lost the Word body")
            for child in list(shard_body):
                shard_body.remove(child)
            for child in group:
                shard_body.append(deepcopy(child))
            document_xml = ElementTree.tostring(shard_root, encoding="utf-8", xml_declaration=True)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as shard:
                for info in package.infolist():
                    payload = document_xml if info.filename == "word/document.xml" else package.read(info.filename)
                    shard.writestr(info, payload)
            shards.append((first_index, last_index, output.getvalue()))
            covered.extend(range(first_index, last_index + 1))
        if covered != list(range(1, len(blocks) + 1)):
            raise RuntimeError("DOCX capacity recovery could not prove exact ordered block coverage")
        return shards
