#!/usr/bin/env python3
"""Isolated native-provider entry point. Its JSON result carries no authority."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import sys

from adapters import AnyDocAdapter, MarkItDownAdapter, convert_basic
from canonical import sha256_bytes
from ocr_provider import NullOcrProvider, OcrSettings, RapidOcrProvider
from pdf_inspector_adapter import PdfInspectorAdapter
from safe_url import download_url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    result_path = Path(args.result)
    try:
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
            provider = RapidOcrProvider(settings) if settings.engine in {"rapidocr", "auto"} else NullOcrProvider(settings)
            value = PdfInspectorAdapter(provider, ocr_mode=request["ocr_mode"]).extract(
                request["source"], request["document_id"], request["mode"], asset_dir
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
