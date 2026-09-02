#!/usr/bin/env python3
"""Isolated native-provider entry point. Its JSON result carries no authority."""
from __future__ import annotations

import os as _bootstrap_os
import sys as _bootstrap_sys

_SCRIPTS_DIR = _bootstrap_os.path.dirname(_bootstrap_os.path.realpath(__file__))
if _SCRIPTS_DIR not in _bootstrap_sys.path:
    _bootstrap_sys.path.insert(0, _SCRIPTS_DIR)
if __name__ == "__main__":
    for _stream in (_bootstrap_sys.stdout, _bootstrap_sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
del _bootstrap_os, _bootstrap_sys

import argparse
import json
import copy
import math
import stat
import time
from pathlib import Path, PurePosixPath
import tempfile
import sys

from adapters import AnyDocAdapter, MarkItDownAdapter, convert_basic
import native_paths as np
from canonical import CanonicalValidationError, quality_from_warnings, sha256_bytes, sha256_file, validate_canonical
from ocr_provider import NullOcrProvider, OcrSettings, RapidOcrProvider
from pdf_inspector_adapter import PdfInspectorAdapter
from safe_url import download_url


def _require_dependency(import_name: str, install_name: str) -> None:
    """Import one route capability without mutating the worker environment."""
    try:
        __import__(import_name)
    except ImportError as exc:
        raise RuntimeError(
            f"Required PDF route dependency {install_name} is unavailable; "
            "install a compatible provider in the active interpreter"
        ) from exc


def _require_pdf_route(ocr_mode: str) -> None:
    _require_dependency("pypdf", "pypdf")
    if ocr_mode == "force":
        _require_dependency("pypdfium2", "pypdfium2")
        _require_dependency("rapidocr", "rapidocr")
        _require_dependency("onnxruntime", "onnxruntime")
    else:
        _require_dependency("pdf_inspector", "pdf-inspector")



IMAGE_BODY_LIMIT = 64 * 1024 * 1024
IMAGE_METADATA_LIMIT = 16 * 1024 * 1024
IMAGE_ASSET_LIMIT = 4096
IMAGE_BYTES_LIMIT = 256 * 1024 * 1024


def _extraction_document(value: dict, document_id: str) -> dict:
    """Semantic validation envelope; the publisher supplies display metadata."""
    warnings = value.get("warnings", [])
    return {
        "schema_version": "1.0",
        "source": {"kind": "file", "file_name": "source.pdf", "locator": "source.pdf",
                   "sha256": document_id.removeprefix("sha256:"), "hash_basis": "source_bytes"},
        "document": {"document_id": document_id, "title": "PDF", "conversion_timestamp": "2000-01-01",
                     "language_normalization": "preserve"},
        "adapter": value["adapter"],
        **{key: value.get(key, []) for key in ("source_units", "content", "tables", "assets", "relationships")},
        "quality": {"status": quality_from_warnings(warnings), "warnings": warnings},
        "outputs": {"mode": "bundle", "markdown": {"path": "document.md", "sha256": "0" * 64},
                    "assets": [{"path": a["path"], "sha256": a["sha256"]} for a in value.get("assets", [])]},
    }


def _validate_pdf_body(value: dict, document_id: str) -> None:
    # Optional page previews cannot turn a body that failed extraction into a
    # publishable document. Keep this check inside the body worker.
    tables = {table["table_id"]: table for table in value.get("tables", [])}
    usable = False
    for node in value.get("content", []):
        if node.get("type") in {"heading", "paragraph", "list_item", "code"}:
            usable = usable or bool(str(node.get("normalized_text", node.get("text", ""))).strip())
        elif node.get("type") == "table":
            table = tables.get(node.get("table_id"), {})
            usable = usable or any(
                str(cell.get("normalized_text", cell.get("text", ""))).strip()
                for row in table.get("rows", []) for cell in row
            )
    if not usable:
        raise CanonicalValidationError("no usable content nodes were extracted")
    validate_canonical(_extraction_document(value, document_id), validate_schema=False)


def _read_image_body(path: Path) -> dict:
    info = np.lstat(path)
    if np.is_reparse(info) or not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= IMAGE_BODY_LIMIT:
        raise ValueError("PDF image base result exceeds its size or file-type limit")
    envelope = json.loads(np.read_text(path, encoding="utf-8"))
    if envelope.get("ok") is not True or not isinstance(envelope.get("result"), dict):
        raise ValueError("PDF image base result is invalid")
    return envelope["result"]


def _body_projection(value: dict) -> tuple[list, list]:
    tables = {item["table_id"]: item for item in value.get("tables", [])}
    nodes = []
    ordered_tables = []
    for node in value.get("content", []):
        if node.get("type") == "image":
            continue
        nodes.append({key: item for key, item in node.items() if key not in {"id", "table_id"}})
        if node.get("type") == "table":
            ordered_tables.append({key: item for key, item in tables[node["table_id"]].items() if key != "table_id"})
    return nodes, ordered_tables


def _validate_image_candidate(body: dict, candidate: dict, image_dir: Path, document_id: str) -> None:
    """Validate all enhancement-specific content and files before acceptance."""
    if _body_projection(body) != _body_projection(candidate):
        raise ValueError("PDF image enhancement changed the authoritative body or tables")
    if candidate.get("adapter") != body.get("adapter") or candidate.get("title") != body.get("title"):
        raise ValueError("PDF image enhancement changed body identity")
    if any(warning not in candidate.get("warnings", []) for warning in body.get("warnings", [])):
        raise ValueError("PDF image enhancement removed body warnings")
    body_units = [(unit["id"], unit["type"], unit["index"], unit["locator"]) for unit in body["source_units"]]
    candidate_units = [(unit["id"], unit["type"], unit["index"], unit["locator"]) for unit in candidate["source_units"]]
    if body_units != candidate_units:
        raise ValueError("PDF image enhancement changed source units")
    assets = candidate.get("assets", [])
    if len(assets) > IMAGE_ASSET_LIMIT:
        raise ValueError("PDF image asset count limit exceeded")
    delta = {"assets": assets,
             "content": [node for node in candidate.get("content", []) if node.get("type") == "image"],
             "warnings": candidate.get("warnings", []), "metrics": candidate.get("image_metrics", {})}
    if len(json.dumps(delta, ensure_ascii=False, allow_nan=False).encode("utf-8")) > IMAGE_METADATA_LIMIT:
        raise ValueError("PDF image metadata limit exceeded")
    page_units = {unit["locator"].get("page"): unit["id"] for unit in candidate["source_units"] if unit["type"] == "page"}
    expected_names = set()
    total_bytes = 0
    for asset in assets:
        relative = PurePosixPath(asset["path"])
        if len(relative.parts) != 3 or relative.parts[:2] != ("assets", "images"):
            raise ValueError("PDF image asset is outside the owned image directory")
        name = relative.parts[2]
        if name != f"{asset['asset_id']}.png":
            raise ValueError("PDF image asset name differs from its identity")
        if name in {".", ".."} or name.casefold() in expected_names or "\\" in name or ":" in name:
            raise ValueError("PDF image asset name is unsafe or duplicated")
        expected_names.add(name.casefold())
        path = image_dir / name
        info = np.lstat(path)
        if np.is_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise ValueError("PDF image asset is not a regular owned file")
        total_bytes += info.st_size
        if total_bytes > IMAGE_BYTES_LIMIT:
            raise ValueError("PDF image byte limit exceeded")
        if sha256_file(path) != asset["sha256"]:
            raise ValueError("PDF image asset hash differs from its record")
        locator = asset.get("source_locator", {})
        page = locator.get("page")
        if page not in page_units or locator.get("source_unit_id") != page_units[page]:
            raise ValueError("PDF image page provenance is invalid")
        bbox = locator.get("bbox", [])
        if bbox and (len(bbox) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox)):
            raise ValueError("PDF image bounds are invalid")
        if locator.get("extraction_method") == "pdfium_page_render" and (not bbox or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
            raise ValueError("PDF image bounds have no area")
    actual_names = {entry.name.casefold() for entry in image_dir.iterdir()}
    if actual_names != expected_names:
        raise ValueError("PDF image directory contains unaccepted files")
    validate_canonical(_extraction_document(candidate, document_id), validate_schema=True)


def _write_image_result(path: Path, value: dict) -> None:
    payload = json.dumps({"ok": True, "result": value}, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > IMAGE_BODY_LIMIT + IMAGE_METADATA_LIMIT:
        raise ValueError("PDF image result limit exceeded")
    np.write_bytes(path, payload)


def _run_image_enhancement(args) -> int:
    from pdf_images import enhance_pdf_images

    deadline = args.image_deadline
    if not math.isfinite(deadline) or time.monotonic() >= deadline:
        raise TimeoutError("PDF image deadline expired before reading the body")
    source = Path(args.image_source)
    if sha256_file(source) != args.image_document_id.removeprefix("sha256:"):
        raise ValueError("PDF image source identity differs from the body")
    body = _read_image_body(Path(args.image_body))
    _validate_pdf_body(body, args.image_document_id)
    frozen_body = copy.deepcopy(body)
    candidate = enhance_pdf_images(
        source, body, Path(args.image_dir),
        {"mode": args.image_mode, "document_id": args.image_document_id}, deadline,
    )
    _validate_image_candidate(frozen_body, candidate, Path(args.image_dir), args.image_document_id)
    if sha256_file(source) != args.image_document_id.removeprefix("sha256:"):
        raise ValueError("PDF image source changed during enhancement")
    if time.monotonic() >= deadline:
        raise TimeoutError("PDF image deadline expired before candidate acceptance")
    metrics = candidate.get("image_metrics", {})
    summary = {key: item for key, item in metrics.items()
               if isinstance(item, (int, float, str)) or key == "stages_seconds"}
    for key in ("candidate_pages", "processed_pages", "unprocessed_pages", "capped_pages"):
        if isinstance(metrics.get(key), list):
            summary[key + "_count"] = len(metrics[key])
    candidate["_image_metrics_log"] = json.dumps(summary, ensure_ascii=True, allow_nan=False)[:4000]
    _write_image_result(Path(args.result), candidate)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request")
    parser.add_argument("--image-body")
    parser.add_argument("--image-source")
    parser.add_argument("--image-dir")
    parser.add_argument("--image-mode", choices=["auto", "objects"])
    parser.add_argument("--image-document-id")
    parser.add_argument("--image-deadline", type=float)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    result_path = Path(args.result)
    try:
        if args.image_body:
            return _run_image_enhancement(args)
        if not args.request:
            raise ValueError("--request or --image-body is required")
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        adapter = request["adapter"]
        asset_dir = Path(request["asset_dir"]) if request.get("asset_dir") else None
        if adapter == "anydoc":
            value = AnyDocAdapter().extract(
                request["source"], request["document_id"], request["mode"], asset_dir
            )
        elif adapter == "markitdown":
            value = MarkItDownAdapter().extract(
                request["source"], request["document_id"], request["mode"], asset_dir
            )
        elif adapter == "pdf_inspector":
            settings = OcrSettings.from_mapping(request.get("ocr_settings"))
            _require_pdf_route(request["ocr_mode"])
            provider = RapidOcrProvider(settings) if settings.engine in {"rapidocr", "auto"} else NullOcrProvider(settings)
            value = PdfInspectorAdapter(provider, ocr_mode=request["ocr_mode"], image_mode="off").extract(
                request["source"], request["document_id"], request["mode"], None
            )
            _validate_pdf_body(value, request["document_id"])
            if request.get("image_fallback_path"):
                page_count = max((u.get("index", 0) for u in value["source_units"] if u.get("type") == "page"), default=0)
                warning = {
                    "code": "pdf_image_enhancement_incomplete",
                    "message": f"Optional PDF image work did not complete within its budget or failed validation; pages 1-{page_count} remain unprocessed; body retained",
                    "content_loss": True,
                }
                fallback = {**value, "warnings": [*value.get("warnings", []), warning]}
                Path(request["image_fallback_path"]).write_text(
                    json.dumps({"ok": True, "result": fallback}, ensure_ascii=False), encoding="utf-8"
                )
        elif adapter == "image_ocr":
            settings = OcrSettings.from_mapping(request.get("ocr_settings"))
            provider = RapidOcrProvider(settings)
            value = {
                "items": [
                    {"asset_id": item["asset_id"], **provider.extract_image_text(item["path"])}
                    for item in request.get("assets", [])
                ]
            }
        elif adapter == "url_markitdown":
            remote = download_url(request["source"])
            with tempfile.TemporaryDirectory(prefix=".remote-conversion-") as directory:
                temporary_remote = Path(directory) / f"source{remote.suffix}"
                temporary_remote.write_bytes(remote.payload)
                markdown = convert_basic(str(temporary_remote))
            value = {
                "markdown": markdown,
                "locator": remote.locator,
                "media_type": remote.media_type,
                "sha256": sha256_bytes(remote.payload),
                "size_bytes": len(remote.payload),
            }
        else:
            raise RuntimeError(f"Unsupported isolated adapter: {adapter}")
        envelope = {"ok": True, "result": value}
        exit_code = 0
    except BaseException as exc:
        envelope = {"ok": False, "error_type": type(exc).__name__, "message": str(exc)}
        exit_code = 1
    result_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
