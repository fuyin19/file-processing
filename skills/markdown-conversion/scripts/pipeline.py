#!/usr/bin/env python3
"""Unified PDF/Office-to-canonical/Markdown pipeline (v6)."""
from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from adapters import MarkItDownAdapter, convert_basic, markdown_to_canonical, markitdown_version
from canonical import (
    CanonicalValidationError,
    convert_chinese,
    fix_encoding,
    frontmatter,
    inject_frontmatter,
    quality_from_warnings,
    render_markdown,
    sha256_bytes,
    sha256_file,
    strip_images,
    title_from_markdown,
    validate_canonical,
)
from canonical import MOJIBAKE_PATTERNS
from ocr_provider import NullOcrProvider, OcrSettings, RapidOcrProvider
from pdf_adapter import PdfAdapter, transform_bbox_for_orientation


VERSION = "6.2.0"
DEFAULT_CONFIG: dict[str, Any] = {
    "pdf_ocr": {
        "mode": "auto",
        "engine": "rapidocr",
        "language": "ch",
        "dpi": 300.0,
        "max_long_edge": 4096,
        "min_confidence": 0.5,
    }
}
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".html", ".csv", ".json", ".jsonl", ".xml", ".epub", ".md",
    ".jpg", ".jpeg", ".png", ".gif", ".mp3", ".wav", ".mp4",
    ".zip", ".txt", ".rtf", ".odt", ".ods", ".odp",
}
DEPS = [
    ("markitdown", "markitdown", True),
    ("opencc", "opencc-python-reimplemented", True),
    ("markdown_it", "markdown-it-py", True),
    ("jsonschema", "jsonschema", True),
    ("pypdfium2", "pypdfium2", True),
    ("chardet", "chardet", True),
    ("doc2docx", "doc2docx", False),
    ("rapidocr", "rapidocr", False),
    ("onnxruntime", "onnxruntime", False),
]
_rfc3339_datetime_re = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class PipelineError(RuntimeError):
    pass


class OutputCollision(PipelineError):
    pass


@dataclass(frozen=True)
class Target:
    mode: str
    path: Path
    stem: str


def die(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def is_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def url_to_slug(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    raw = (parsed.netloc.split("@")[-1] + parsed.path).strip("/")
    raw = urllib.parse.unquote(raw)
    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", raw).strip("-")
    if not slug:
        slug = parsed.netloc.split("@")[-1].replace(":", "-")
    return (slug[:120].rstrip("-") or "untitled")


def _source_stem(source: str) -> str:
    if is_url(source):
        path = urllib.parse.unquote(urllib.parse.urlparse(source).path)
        return Path(path).stem or url_to_slug(source)
    return Path(source).stem or "untitled"


def _ensure_package(package: str, install_name: str | None = None):
    try:
        return __import__(package)
    except ImportError:
        import subprocess

        install_name = install_name or package
        print(f"Installing {install_name}...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", install_name])
        return __import__(package)


def convert_doc_to_docx(doc_path: str) -> str:
    _ensure_package("doc2docx")
    from doc2docx import convert

    target = Path(tempfile.gettempdir()) / f"{Path(doc_path).stem}_temp_{uuid.uuid4().hex}.docx"
    convert(doc_path, str(target))
    return str(target)


def resolve_timestamp(value: str) -> str:
    if not value:
        return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            die("--timestamp must be an ISO date or a timezone-aware ISO datetime")
        return value
    if _rfc3339_datetime_re.fullmatch(value) is None:
        die("--timestamp must be an ISO date or RFC3339 timezone-aware datetime")
    try:
        parsed = datetime.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        die("--timestamp must be an ISO date or a timezone-aware ISO datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        die("--timestamp datetime must include a timezone offset")
    return value


def load_config() -> dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        Path(CONFIG_PATH).write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    try:
        value = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: config.json parse error ({exc}), using defaults", file=sys.stderr)
        return dict(DEFAULT_CONFIG)
    result = dict(DEFAULT_CONFIG)
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(DEFAULT_CONFIG.get(key), dict):
            result[key] = {**DEFAULT_CONFIG[key], **item}
        else:
            result[key] = item
    return result


def resolve_ocr_settings(args, config: dict[str, Any]) -> OcrSettings:
    raw = config.get("pdf_ocr", {})
    if not isinstance(raw, dict):
        die("config pdf_ocr must be an object")
    values = dict(raw)
    overrides = {
        "mode": getattr(args, "ocr", None),
        "engine": getattr(args, "ocr_engine", None),
        "language": getattr(args, "ocr_language", None),
        "dpi": getattr(args, "ocr_dpi", None),
        "max_long_edge": getattr(args, "ocr_max_long_edge", None),
        "min_confidence": getattr(args, "ocr_min_confidence", None),
    }
    values.update({key: value for key, value in overrides.items() if value is not None})
    try:
        settings = OcrSettings.from_mapping(values)
    except (TypeError, ValueError) as exc:
        die(f"Invalid PDF OCR configuration: {exc}")
    if settings.mode != "off" and settings.engine != "rapidocr":
        die(f"Unsupported OCR engine: {settings.engine}")
    return settings


def create_ocr_provider(settings: OcrSettings):
    if settings.mode == "off":
        return NullOcrProvider(settings)
    if settings.engine == "rapidocr":
        return RapidOcrProvider(settings)
    raise PipelineError(f"Unsupported OCR engine: {settings.engine}")


def _normalize_mode(args) -> None:
    if args.output_path and args.output_dir:
        die("--output-path and --output-dir are mutually exclusive")
    if args.input_dir and args.output_path:
        args.output_dir = args.output_path
        args.output_path = ""
        print("Warning: batch --output-path is deprecated; use --output-dir", file=sys.stderr)
    if args.output_mode is None:
        args.output_mode = "markdown" if args.input and args.output_path else "bundle"
    if args.output_path and args.output_mode != "markdown":
        die("--output-path is valid only for single-file --output-mode markdown")


def precheck(args) -> None:
    if bool(args.input) == bool(args.input_dir):
        die("Exactly one of --input or --input-dir is required")
    if args.overwrite and args.rename:
        die("--overwrite and --rename are mutually exclusive")
    _normalize_mode(args)
    if args.input and not is_url(args.input) and not Path(args.input).is_file():
        die(f"File not found: {args.input}")
    if args.input_dir and not Path(args.input_dir).is_dir():
        die(f"Directory not found: {args.input_dir}")


def _batch_root(args) -> Path:
    return Path(args.output_dir).resolve() if args.output_dir else (Path(args.input_dir).resolve() / "_converted")


def resolve_target(args, source: str, relative_path: Path | None = None) -> Target:
    stem = _source_stem(source)
    if args.input and args.output_path:
        return Target("markdown", Path(args.output_path).resolve(), Path(args.output_path).stem)
    if args.input_dir:
        relative_path = relative_path or Path(source).name
        parent = relative_path.parent
        root = _batch_root(args)
        base = root / parent / stem
    else:
        root = Path(args.output_dir).resolve() if args.output_dir else (
            Path.cwd() if is_url(source) else Path(source).resolve().parent
        )
        base = root / stem
    path = base if args.output_mode == "bundle" else base.parent / f"{stem}.md"
    return Target(args.output_mode, path, stem)


def resolve_output_path(args) -> str:
    """Compatibility helper returning the resolved single/batch output root."""
    _normalize_mode(args)
    if args.input_dir:
        return str(_batch_root(args))
    return str(resolve_target(args, args.input).path)


def _renamed_target(target: Target) -> Target:
    if not target.path.exists():
        return target
    for index in range(1, 10000):
        stem = f"{target.stem}_{index}"
        suffix = "" if target.mode == "bundle" else target.path.suffix
        path = target.path.with_name(f"{stem}{suffix}")
        if not path.exists():
            return Target(target.mode, path, stem)
    raise PipelineError(f"Could not find an available renamed target for {target.path}")


def _preflight_target(target: Target, source: str, overwrite: bool, rename: bool) -> Target:
    if not is_url(source) and target.path.resolve() == Path(source).resolve():
        raise PipelineError("Refusing to overwrite the source file")
    if target.path.exists():
        if rename:
            return _renamed_target(target)
        if not overwrite:
            raise OutputCollision(f"Output already exists: {target.path}")
    return target


def collect_files(
    input_dir: str,
    recursive: bool,
    types: list[str] | None,
    exclude_root: Path | None = None,
) -> list[str]:
    root = Path(input_dir).resolve()
    if not root.is_dir():
        die(f"Directory not found: {input_dir}")
    normalized = None
    if types is not None:
        normalized = {value.lower() if value.startswith(".") else f".{value.lower()}" for value in types}
    files: list[str] = []
    iterator = root.rglob("*") if recursive else root.iterdir()
    for path in iterator:
        if not path.is_file():
            continue
        if exclude_root is not None:
            try:
                path.resolve().relative_to(exclude_root.resolve())
                continue
            except ValueError:
                pass
        suffix = path.suffix.lower()
        if normalized is not None and suffix not in normalized:
            continue
        if suffix in SUPPORTED_EXTENSIONS or (normalized and suffix in normalized):
            files.append(str(path))
    return sorted(files)


def _source_record(source: str, adapter_text: str | None = None) -> tuple[dict[str, Any], str]:
    if is_url(source):
        if adapter_text is None:
            raise PipelineError("URL source identity requires adapter text")
        digest = sha256_bytes(adapter_text.encode("utf-8"))
        return (
            {
                "kind": "url",
                "file_name": _source_stem(source),
                "locator": source,
                "sha256": digest,
                "hash_basis": "adapter_text",
                "size_bytes": None,
                "media_type": None,
            },
            f"sha256:{digest}",
        )
    path = Path(source).resolve()
    digest = sha256_file(path)
    return (
        {
            "kind": "file",
            "file_name": path.name,
            "locator": str(path),
            "sha256": digest,
            "hash_basis": "source_bytes",
            "size_bytes": path.stat().st_size,
            "media_type": mimetypes.guess_type(path.name)[0],
        },
        f"sha256:{digest}",
    )


def _extract(
    source: str,
    mode: str,
    asset_dir: Path | None,
    identity_source: str | None = None,
    ocr_provider=None,
    ocr_mode: str = "off",
) -> tuple[dict[str, Any], dict[str, Any], str]:
    identity_source = identity_source or source
    if is_url(source):
        markdown = convert_basic(source)
        source_record, document_id = _source_record(source, markdown)
        result = markdown_to_canonical(
            markdown,
            document_id,
            mode,
            "url",
            warn_unexported_images=asset_dir is not None,
        )
        result["adapter"] = {
            "name": "markitdown",
            "version": markitdown_version(),
            "limitations": ["remote_source_hash_uses_adapter_text", "embedded_images_may_not_be_exported"],
        }
        return result, source_record, document_id
    source_record, document_id = _source_record(identity_source)
    if Path(source).suffix.lower() == ".pdf":
        result = PdfAdapter(ocr_provider, ocr_mode=ocr_mode).extract(
            source, document_id, mode, asset_dir
        )
    else:
        result = MarkItDownAdapter().extract(source, document_id, mode, asset_dir)
    return result, source_record, document_id


def _build_document(
    source: str,
    timestamp: str,
    normalization: str,
    output_mode: str,
    asset_dir: Path | None,
    identity_source: str | None = None,
    ocr_provider=None,
    ocr_mode: str = "off",
) -> dict[str, Any]:
    identity_source = identity_source or source
    extracted, source_record, document_id = _extract(
        source,
        normalization,
        asset_dir,
        identity_source,
        ocr_provider,
        ocr_mode,
    )
    title = extracted.get("title") or _source_stem(identity_source)
    if title == "untitled":
        title = _source_stem(identity_source)
    title = convert_chinese(title, normalization)
    warnings = extracted.get("warnings", [])
    return {
        "schema_version": "1.0",
        "source": source_record,
        "document": {
            "document_id": document_id,
            "title": title,
            "conversion_timestamp": timestamp,
            "language_normalization": normalization,
        },
        "adapter": extracted["adapter"],
        "source_units": extracted.get("source_units", []),
        "content": extracted.get("content", []),
        "tables": extracted.get("tables", []),
        "assets": extracted.get("assets", []),
        "relationships": extracted.get("relationships", []),
        "quality": {"status": quality_from_warnings(warnings), "warnings": warnings},
        "outputs": {"mode": output_mode, "markdown": {"path": "pending", "sha256": "0" * 64}, "assets": []},
    }


def _remove_path(path: Path) -> None:
    """Remove one filesystem entry without assuming it is a directory."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _publish_directory(stage: Path, target: Path, overwrite: bool) -> None:
    backup: Path | None = None
    try:
        if target.exists():
            if not overwrite:
                raise OutputCollision(f"Output already exists: {target}")
            backup = target.with_name(f".{target.name}.backup-{uuid.uuid4().hex}")
            os.replace(target, backup)
        os.replace(stage, target)
    except Exception:
        if backup is not None and backup.exists():
            if target.exists():
                _remove_path(target)
            os.replace(backup, target)
        raise
    else:
        if backup is not None and backup.exists():
            try:
                _remove_path(backup)
            except OSError as exc:
                # The second replace is the commit point. Reporting failure after
                # it would tell callers that no output was published even though
                # the new target is already live. Keep the recoverable backup and
                # report cleanup as a non-fatal maintenance warning instead.
                print(
                    f"Warning: published {target}, but could not remove backup "
                    f"{backup}: {exc}",
                    file=sys.stderr,
                )


def _write_markdown_file(markdown: str, target: Path, overwrite: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{target.stem}.staging-", suffix=".md", dir=target.parent)
    os.close(descriptor)
    stage = Path(name)
    try:
        stage.write_text(markdown, encoding="utf-8", newline="\n")
        if target.exists() and not overwrite:
            raise OutputCollision(f"Output already exists: {target}")
        os.replace(stage, target)
    finally:
        if stage.exists():
            stage.unlink()


def convert_one(args, source: str, relative_path: Path | None = None) -> tuple[Path, str, list[dict[str, Any]]]:
    target = _preflight_target(resolve_target(args, source, relative_path), source, args.overwrite, args.rename)
    ocr_settings = getattr(args, "ocr_settings", OcrSettings(mode="off", engine="none"))
    ocr_provider = getattr(args, "ocr_provider", None)
    temporary_docx: str | None = None
    actual_source = source
    if not is_url(source) and Path(source).suffix.lower() == ".doc":
        temporary_docx = convert_doc_to_docx(source)
        actual_source = temporary_docx
    try:
        if target.mode == "bundle":
            target.path.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=f".{target.path.name}.staging-", dir=target.path.parent))
            try:
                asset_dir = stage / "assets" / "images"
                document = _build_document(
                    actual_source, args.timestamp, args.language_normalization, "bundle", asset_dir,
                    identity_source=source,
                    ocr_provider=ocr_provider,
                    ocr_mode=ocr_settings.mode,
                )
                markdown_name = f"{target.stem}.md"
                json_name = f"{target.stem}.json"
                markdown = render_markdown(document, not args.no_frontmatter, "bundle")
                markdown_path = stage / markdown_name
                markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
                document["outputs"] = {
                    "mode": "bundle",
                    "markdown": {"path": markdown_name, "sha256": sha256_file(markdown_path)},
                    "assets": [{"path": item["path"], "sha256": item["sha256"]} for item in document["assets"]],
                }
                validate_canonical(document, stage)
                json_path = stage / json_name
                json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
                persisted = json.loads(json_path.read_text(encoding="utf-8"))
                # Full JSON Schema validation already ran on the identical object;
                # the persisted round-trip only needs semantic/hash verification.
                validate_canonical(persisted, stage, validate_schema=False)
                _publish_directory(stage, target.path, args.overwrite)
            except Exception:
                if stage.exists():
                    shutil.rmtree(stage)
                raise
        else:
            document = _build_document(
                actual_source, args.timestamp, args.language_normalization, "markdown", None,
                identity_source=source,
                ocr_provider=ocr_provider,
                ocr_mode=ocr_settings.mode,
            )
            markdown = render_markdown(document, not args.no_frontmatter, "markdown")
            document["outputs"] = {
                "mode": "markdown",
                "markdown": {"path": target.path.name, "sha256": sha256_bytes(markdown.encode("utf-8"))},
                "assets": [],
            }
            validate_canonical(document, validate_schema=False)
            _write_markdown_file(markdown, target.path, args.overwrite)
        return target.path, document["quality"]["status"], document["quality"]["warnings"]
    finally:
        if temporary_docx and Path(temporary_docx).exists():
            Path(temporary_docx).unlink()


def write_to_vault(text: str, output_path: str, overwrite: bool, rename: bool) -> str:
    target = Path(output_path)
    if target.exists() and rename:
        target = _renamed_target(Target("markdown", target, target.stem)).path
    elif target.exists() and not overwrite:
        print(f"ERROR: Output file already exists: {target}", file=sys.stderr)
        raise SystemExit(2)
    _write_markdown_file(text, target, True)
    return str(target)


def _validate_types(types: list[str] | None) -> None:
    if not types:
        return
    normalized = {value.lower() if value.startswith(".") else f".{value.lower()}" for value in types}
    if not normalized.intersection(SUPPORTED_EXTENSIONS):
        die(f"Unsupported file type in --types: {', '.join(sorted(normalized))}")


def run_batch(args) -> int:
    types = [value.strip() for value in args.types.split(",") if value.strip()] or None
    _validate_types(types)
    root = Path(args.input_dir).resolve()
    output_root = _batch_root(args)
    files = collect_files(args.input_dir, not args.no_recursive, types, output_root)
    converted = failed = skipped = 0
    for source in files:
        relative = Path(source).resolve().relative_to(root)
        try:
            path, status, warnings = convert_one(args, source, relative)
            label = "PARTIAL" if status == "partial" else "WARN" if status == "complete_with_warnings" else "OK"
            print(f"[{label}] {relative.as_posix()} -> {path} ({len(warnings)} warnings)")
            converted += 1
        except OutputCollision as exc:
            print(f"[SKIP] {relative.as_posix()} — {exc}", file=sys.stderr)
            skipped += 1
        except (PipelineError, CanonicalValidationError, OSError, ValueError) as exc:
            print(f"[FAIL] {relative.as_posix()} — {exc}", file=sys.stderr)
            failed += 1
        except Exception as exc:  # adapter boundary
            print(f"[FAIL] {relative.as_posix()} — {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
    print(f"[BATCH] {converted} converted, {failed} failed, {skipped} skipped")
    return 1 if failed else 2 if skipped else 0


def show_version() -> None:
    print(f"markdown-conversion v{VERSION}")
    print("Dependencies:")
    for import_name, install_name, required in DEPS:
        try:
            __import__(import_name)
            try:
                version = importlib.metadata.version(install_name)
            except importlib.metadata.PackageNotFoundError:
                version = "installed"
        except ImportError:
            version = "NOT INSTALLED" if required else "not installed (optional)"
        print(f"  {install_name}: {version}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified PDF/Office canonical document conversion pipeline")
    parser.add_argument("--config", default="")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--input-dir")
    parser.add_argument("--output-mode", choices=["bundle", "markdown"], default=None)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--language-normalization", choices=["simplified", "preserve", "traditional"], default="simplified")
    parser.add_argument("--no-frontmatter", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--rename", action="store_true")
    parser.add_argument("--timestamp", default="")
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--types", default="")
    parser.add_argument(
        "--ocr",
        choices=["off", "auto", "force"],
        default=None,
        help="PDF OCR policy; defaults to config pdf_ocr.mode (auto)",
    )
    parser.add_argument("--ocr-engine", choices=["rapidocr"], default=None)
    parser.add_argument("--ocr-language", default=None)
    parser.add_argument("--ocr-dpi", type=float, default=None)
    parser.add_argument("--ocr-max-long-edge", type=int, default=None)
    parser.add_argument("--ocr-min-confidence", type=float, default=None)
    return parser


def main() -> int:
    global CONFIG_PATH
    args = build_parser().parse_args()
    if args.version:
        show_version()
        return 0
    if args.config:
        CONFIG_PATH = str(Path(args.config).resolve())
    config = load_config()
    args.ocr_settings = resolve_ocr_settings(args, config)
    args.ocr_provider = create_ocr_provider(args.ocr_settings)
    precheck(args)
    args.timestamp = resolve_timestamp(args.timestamp)
    if args.input_dir:
        return run_batch(args)
    try:
        path, status, warnings = convert_one(args, args.input)
    except OutputCollision as exc:
        print(f"ERROR: {exc}\nRe-run with --overwrite or --rename.", file=sys.stderr)
        return 2
    except (PipelineError, CanonicalValidationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    label = "PARTIAL" if status == "partial" else "WARN" if status == "complete_with_warnings" else "OK"
    print(f"[{label}] Converted {args.input} -> {path} ({len(warnings)} warnings)")
    if warnings:
        for warning in warnings:
            print(f"  - {warning['code']}: {warning['message']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
