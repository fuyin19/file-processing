#!/usr/bin/env python3
"""Repeatable in-process AnyDoc-vs-MarkItDown benchmark.

The benchmark intentionally requires an operator-provided, real corpus.  It
never downloads, synthesizes, or silently substitutes fixtures.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "markdown-conversion" / "scripts"))
from adapters import AnyDocAdapter, MarkItDownAdapter, anydoc_capability_check

MANDATORY_CORPUS_EXTENSIONS = frozenset({".docx", ".pptx", ".xls", ".xlsx", ".epub", ".csv"})


def _timed_extract(adapter, path: Path) -> float:
    started = time.perf_counter()
    result = adapter.extract(str(path), "sha256:" + "0" * 64, "preserve", None)
    elapsed = (time.perf_counter() - started) * 1000.0
    content = result.get("content", []) if isinstance(result, dict) else []
    visible = any(
        str(node.get("text", node.get("normalized_text", node.get("raw_text", "")))).strip()
        for node in content
        if isinstance(node, dict)
    )
    if not visible and isinstance(result, dict):
        for table in result.get("tables", []) or []:
            for row in table.get("raw_rows", []) or []:
                if any(str(cell).strip() for cell in row):
                    visible = True
                    break
            if visible:
                break
    if not content or not visible:
        raise RuntimeError(f"content sentinel failed for {path.name}: adapter returned no visible content")
    return elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--max-slowdown", type=float, default=2.0)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "fixtures" / "anydoc",
        help="Real fixture corpus directory (defaults to tests/markdown-conversion/fixtures/anydoc)",
    )
    args = parser.parse_args()
    if args.iterations < 2:
        parser.error("--iterations must be at least 2")
    if not args.input_dir.is_dir():
        print(f"ERROR: benchmark corpus directory not found: {args.input_dir}")
        return 1
    try:
        anydoc_capability_check()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    paths = sorted(
        path for path in args.input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in MANDATORY_CORPUS_EXTENSIONS
    )
    available = {path.suffix.lower() for path in paths}
    missing = sorted(MANDATORY_CORPUS_EXTENSIONS - available)
    if missing:
        print("ERROR: mandatory benchmark corpus extensions are missing: " + ", ".join(missing))
        return 1
    anydoc = AnyDocAdapter()
    markitdown = MarkItDownAdapter()
    per_format_anydoc: list[float] = []
    per_format_markitdown: list[float] = []
    print(f"iterations={args.iterations} files={','.join(path.name for path in paths)}")
    for path in paths:
        # Exactly one warmup per adapter/file; all timed runs are in-process.
        _timed_extract(anydoc, path)
        _timed_extract(markitdown, path)
        any_values: list[float] = []
        mark_values: list[float] = []
        for index in range(args.iterations):
            if index % 2 == 0:
                any_values.append(_timed_extract(anydoc, path))
                mark_values.append(_timed_extract(markitdown, path))
            else:
                mark_values.append(_timed_extract(markitdown, path))
                any_values.append(_timed_extract(anydoc, path))
        any_median = statistics.median(any_values)
        mark_median = statistics.median(mark_values)
        per_format_anydoc.append(any_median)
        per_format_markitdown.append(mark_median)
        ratio = any_median / mark_median if mark_median else float("inf")
        print(f"{path.name}: anydoc_median_ms={any_median:.3f} markitdown_median_ms={mark_median:.3f} ratio={ratio:.3f}")
    aggregate_anydoc = statistics.median(per_format_anydoc)
    aggregate_markitdown = statistics.median(per_format_markitdown)
    ratio = aggregate_anydoc / aggregate_markitdown if aggregate_markitdown else float("inf")
    print(f"aggregate_anydoc_median_ms={aggregate_anydoc:.3f}")
    print(f"aggregate_markitdown_median_ms={aggregate_markitdown:.3f}")
    print(f"aggregate_ratio={ratio:.3f}")
    return 0 if ratio <= args.max_slowdown else 1


if __name__ == "__main__":
    raise SystemExit(main())
