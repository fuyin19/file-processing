#!/usr/bin/env python3
"""
pipeline.py - markdown-conversion pipeline.

Steps:
  1. Convert document using markitdown Python API (basic)
  2. Fix encoding (single-pass chardet + compiled regex mojibake scan)
  3. Convert Traditional → Simplified Chinese (two-pass opencc with stability gate)
  4. Inject legacy YAML frontmatter (unless --no-frontmatter/--okf)
  5. Write directly, or stage an OKF run for reviewed Plan -> Apply

Exit codes:
  0 - success
  1 - gate failure or error (message on stderr)
  2 - output file exists and neither --overwrite nor --rename passed
  3 - OKF conversion staged; metadata review and apply are required
  4 - Cortex CLI unavailable/incompatible or active policy invalid
"""
import sys
import os
import re
import json
import argparse
import datetime
import urllib.parse
import importlib.metadata
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NoReturn

VERSION = '4.1.0'

# (pkg_import_name, pip_install_name, required)
DEPS = [
    ('markitdown', 'markitdown', True),
    ('chardet', 'chardet', True),
    ('opencc', 'opencc-python-reimplemented', True),
    ('doc2docx', 'doc2docx', False),
]

MOJIBAKE_PATTERNS = ['ï¿½', 'Ã¤', 'â€', 'Ã©', 'Ã¨', 'Ã ', 'Ã¹', 'Ã»']
_mojibake_re = re.compile('|'.join(re.escape(p) for p in MOJIBAKE_PATTERNS))

# Regex for markdown image syntax: ![alt text](url)
_image_re = re.compile(r'!\[[^\]]*\]\([^)]+\)')
# Regex for orphaned image filename lines: just "something.jpg" on its own line
_orphan_image_re = re.compile(
    r'^\s*\S+\.(?:jpg|jpeg|png|gif|bmp|svg|webp|tiff?)\s*$', re.MULTILINE | re.IGNORECASE
)
# Regex for collapsing 3+ blank lines into 2
_blank_lines_re = re.compile(r'\n{3,}')


def is_url(path: str) -> bool:
    """Return True if path is an HTTP/HTTPS URL."""
    return path.lower().startswith(('http://', 'https://'))


def url_to_slug(url: str) -> str:
    """Derive a filename-safe slug from a URL."""
    parsed = urllib.parse.urlparse(url)
    # Combine netloc + path, strip auth if present
    raw = (parsed.netloc.split('@')[-1] + parsed.path).strip('/')
    # URL-decode
    raw = urllib.parse.unquote(raw)
    # Replace non-alphanumeric chars with dashes
    slug = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]+', '-', raw)
    slug = slug.strip('-')
    # Fallback to netloc if empty
    if not slug:
        slug = parsed.netloc.split('@')[-1].replace(':', '-')
    # Truncate
    if len(slug) > 120:
        slug = slug[:120].rstrip('-')
    return slug or 'untitled'


def strip_images(text: str) -> str:
    """Remove markdown image syntax and orphaned image filename references."""
    text = _image_re.sub('', text)
    text = _orphan_image_re.sub('', text)
    text = _blank_lines_re.sub('\n\n', text)
    return text

FRONTMATTER_TEMPLATE = (
    '---\n'
    'source: "{source}"\n'
    'converted_at: "{converted_at}"\n'
    'converted_by: "markitdown"\n'
    '---\n\n'
)


def die(msg: str) -> NoReturn:
    print(f'ERROR: {msg}', file=sys.stderr)
    sys.exit(1)


def die_code(msg: str, code: int) -> NoReturn:
    """Print a gate error and exit with an explicit public exit code."""
    print(f'ERROR: {msg}', file=sys.stderr)
    sys.exit(code)


def _ensure_package(pkg: str, install_name: str | None = None):
    """Try to import pkg. On ImportError, run pip install and re-import. Returns the module."""
    try:
        return __import__(pkg)
    except ImportError:
        install_name = install_name or pkg
        import subprocess, sys
        print(f'Installing {install_name}...', file=sys.stderr)
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', install_name])
        return __import__(pkg)


def convert_doc_to_docx(doc_path: str) -> str:
    """Convert legacy .doc to .docx using doc2docx. Returns temp .docx path."""
    import tempfile, uuid, os
    _ensure_package('doc2docx')
    from doc2docx import convert
    stem = os.path.splitext(os.path.basename(doc_path))[0]
    tmpdir = tempfile.gettempdir()
    tmp_path = os.path.join(tmpdir, f'{stem}_temp_{uuid.uuid4().hex}.docx')
    convert(doc_path, tmp_path)
    return tmp_path


def fix_encoding(raw_bytes: bytes) -> str:
    """Detect encoding, decode to Unicode, verify UTF-8 validity."""
    chardet = _ensure_package('chardet')

    # Try UTF-8 first (most common case)
    try:
        text = raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        detected = chardet.detect(raw_bytes)
        encoding = detected.get('encoding')
        confidence = detected.get('confidence', 0.0)

        if not encoding or confidence < 0.7:
            encodings_to_try = ['gbk', 'gb2312', 'gb18030', 'big5', 'shift_jis', 'euc-jp', 'euc-kr']
            for enc in encodings_to_try:
                try:
                    text = raw_bytes.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                die('Could not detect file encoding')
        else:
            try:
                text = raw_bytes.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                die(f'Could not decode file encoding: {encoding}')

    # Gate: re-encode as UTF-8 must succeed
    try:
        text.encode('utf-8').decode('utf-8')
    except UnicodeDecodeError:
        die('Could not produce valid UTF-8 output after encoding fix')

    # Gate: scan for mojibake patterns (single regex pass over all patterns)
    if _mojibake_re.search(text):
        die('Encoding fix produced garbled output. Manual intervention needed.')

    return text


def convert_chinese(text: str) -> str:
    """Convert Traditional Chinese → Simplified Chinese using two-pass opencc."""
    opencc = _ensure_package('opencc', 'opencc-python-reimplemented')

    converter = opencc.OpenCC('t2s')
    pass1 = converter.convert(text)
    pass2 = converter.convert(pass1)  # stabilise edge-case chars like 於→于

    return pass2


def inject_frontmatter(text: str, source: str, converted_at: str) -> str:
    """Prepend YAML frontmatter. Normalizes backslashes in source to forward slashes."""
    source = source.replace('\\', '/').replace('"', '\\"')
    fm = FRONTMATTER_TEMPLATE.format(source=source, converted_at=converted_at)
    result = fm + text

    # Gate: verify required fields present
    if not result.startswith('---'):
        die('Frontmatter injection failed: output does not start with ---')
    for field in ('source:', 'converted_at:', 'converted_by:'):
        if field not in result:
            die(f'Frontmatter injection failed: missing field {field}')

    return result


def write_to_vault(text: str, output_path: str, overwrite: bool, rename: bool) -> str:
    """
    Write text to output_path. Returns the final path written to.
    Exits with code 2 if file exists and neither overwrite nor rename is set.
    """
    if os.path.exists(output_path):
        if overwrite:
            pass  # write unconditionally below
        elif rename:
            stem, ext = os.path.splitext(output_path)
            timestamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
            output_path = f'{stem}-{timestamp}{ext}'
        else:
            print(f'ERROR: Output file already exists: {output_path}\n'
                  f'Re-run with --overwrite to replace or --rename to save as new file.',
                  file=sys.stderr)
            sys.exit(2)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(text)

    # Gate: verify file written
    if not os.path.exists(output_path):
        die(f'Vault write failed: file not found at {output_path} after write')

    return output_path


def convert_basic(source_path: str) -> str:
    """Convert document using markitdown Python API (basic, no LLM image descriptions)."""
    from markitdown import MarkItDown

    if not is_url(source_path) and not os.path.exists(source_path):
        die(f'Source file not found: {source_path}')

    md = MarkItDown()
    result = md.convert(source_path)
    return result.text_content


DEFAULT_CONFIG = {}

# Path to config.json, resolved relative to this script's directory
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')


def load_config() -> dict:
    """Load config.json. Create with defaults if missing. Returns dict."""
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f'Warning: config.json parse error ({e}), using defaults', file=sys.stderr)
        return dict(DEFAULT_CONFIG)
    # Merge with defaults (fill missing keys)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


SUPPORTED_EXTENSIONS = {
    '.pdf', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls',
    '.html', '.csv', '.json', '.jsonl', '.xml', '.epub',
    '.jpg', '.jpeg', '.png', '.gif',
    '.mp3', '.wav', '.mp4',
    '.zip', '.txt', '.rtf', '.odt', '.ods', '.odp',
}


def collect_files(input_dir: str, recursive: bool, types: list[str] | None) -> list[str]:
    """Walk input_dir and return list of files matching criteria."""
    if not os.path.isdir(input_dir):
        die(f'Directory not found: {input_dir}')

    # Build the set of extensions to collect (always a superset including
    # explicitly-requested types even if unsupported — those will fail during
    # conversion, which batch mode handles gracefully).
    if types is not None:
        normalized = set()
        for t in types:
            ext = t if t.startswith('.') else f'.{t}'
            ext = ext.lower()
            normalized.add(ext)
    else:
        normalized = None

    files = []
    if recursive:
        for root, _dirs, filenames in os.walk(input_dir):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if normalized is None or ext in normalized:
                    if ext in SUPPORTED_EXTENSIONS or (normalized and ext in normalized):
                        files.append(os.path.join(root, fname))
    else:
        for fname in os.listdir(input_dir):
            fpath = os.path.join(input_dir, fname)
            if os.path.isfile(fpath):
                ext = os.path.splitext(fname)[1].lower()
                if normalized is None or ext in normalized:
                    if ext in SUPPORTED_EXTENSIONS or (normalized and ext in normalized):
                        files.append(fpath)
    return sorted(files)


def run_batch(args) -> None:
    """Batch convert all files in input_dir."""
    recursive = not args.no_recursive
    types = [t.strip() for t in args.types.split(',')] if args.types else None

    # Validate types: if ALL requested types are unsupported, error immediately
    if types is not None:
        all_unsupported = True
        for t in types:
            ext = t if t.startswith('.') else f'.{t}'
            if ext.lower() in SUPPORTED_EXTENSIONS:
                all_unsupported = False
        if all_unsupported:
            bad = ', '.join(t if t.startswith('.') else f'.{t}' for t in types)
            die(f'Unsupported file type in --types: {bad}. '
                f'Supported types: {", ".join(sorted(SUPPORTED_EXTENSIONS))}')

    files = collect_files(args.input_dir, recursive, types)

    if not files:
        print('[BATCH] 0 converted, 0 failed, 0 skipped')
        return

    converted = 0
    failed = 0
    skipped = 0
    input_dir_abs = os.path.abspath(args.input_dir)

    for fpath in files:
        rel = os.path.relpath(fpath, input_dir_abs)
        stem = os.path.splitext(rel)[0]
        out_file = os.path.join(args.output_path, f'{stem}.md')

        try:
            # Handle .doc -> .docx
            input_path = fpath
            if input_path.lower().endswith('.doc') and not input_path.lower().endswith('.docx'):
                input_path = convert_doc_to_docx(input_path)

            # Convert
            text = convert_basic(input_path)

            # Strip images unless explicitly kept
            if not args.keep_images:
                text = strip_images(text)

            # Fix encoding
            text = fix_encoding(text.encode('utf-8'))

            # T->S conversion
            text = convert_chinese(text)

            # Frontmatter
            if not args.no_frontmatter:
                source = fpath.replace('\\', '/')
                converted_at = args.converted_at or datetime.datetime.now().isoformat(timespec='seconds')
                text = inject_frontmatter(text, source, converted_at)

            # Write -- skip if exists (batch mode treats as skip, not fatal)
            if os.path.exists(out_file) and not args.overwrite and not args.rename:
                print(f'[SKIP] {rel} — output already exists: {out_file}', file=sys.stderr)
                skipped += 1
                continue

            final_path = write_to_vault(text, out_file, args.overwrite, args.rename)
            print(f'[OK] {rel} -> {final_path}')
            converted += 1

            # Cleanup temp .docx if created
            if input_path != fpath and os.path.exists(input_path):
                os.unlink(input_path)

        except SystemExit as e:
            if e.code == 2:
                # File-exists from write_to_vault -- treat as skip
                print(f'[SKIP] {rel} — output already exists', file=sys.stderr)
                skipped += 1
            else:
                print(f'[FAIL] {rel}', file=sys.stderr)
                failed += 1
        except Exception as e:
            print(f'[FAIL] {rel} — {e}', file=sys.stderr)
            failed += 1

    print(f'[BATCH] {converted} converted, {failed} failed, {skipped} skipped')


def _okf_pipeline_script() -> str:
    skills_dir = Path(__file__).resolve().parents[2]
    script = skills_dir / 'okf-frontmatter' / 'scripts' / 'frontmatter_pipeline.py'
    if not script.is_file():
        die('okf-frontmatter pipeline is unavailable; reinstall the file-processing plugin')
    return str(script)


def _okf_run_dir(args) -> Path:
    if args.okf_run_dir:
        path = Path(args.okf_run_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix='file-processing-okf-conversion-')).resolve()


def _write_okf_stage(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def _write_okf_metadata(run_dir: Path, items: list[dict]) -> Path:
    path = run_dir / 'conversion-metadata.json'
    path.write_text(json.dumps({'items': items}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return path


def _invoke_okf_prepare(args, staged_input: Path, target: str, metadata_path: Path, run_dir: Path) -> NoReturn:
    command = [
        sys.executable,
        _okf_pipeline_script(),
        'prepare',
        '--input', str(staged_input),
        '--target', str(target),
        '--metadata-json', str(metadata_path),
        '--run-dir', str(run_dir),
    ]
    if args.workspace:
        command += ['--workspace', args.workspace]
    if args.no_recursive:
        command.append('--no-recursive')
    if args.accept_partial:
        command.append('--accept-partial')
    if args.overwrite:
        command.append('--overwrite')
    if args.rename:
        command.append('--rename')
    result = subprocess.run(command, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode in {0, 2}:
        state_path = run_dir / 'run.json'
        print(f'[READY] OKF conversion staged; awaiting metadata review: {state_path}')
        sys.exit(3)
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode == 4:
        sys.exit(4)
    if result.returncode == 3 and 'Target already exists' in result.stderr:
        sys.exit(2)
    print(f'[RECOVERY] OKF run retained at {run_dir}', file=sys.stderr)
    sys.exit(1)


def _convert_text_for_okf(source_path: str, keep_images: bool) -> tuple[str, str | None]:
    """Convert one input and return text plus an optional temporary docx path."""
    input_path = source_path
    cleanup_path = None
    if not is_url(input_path) and input_path.lower().endswith('.doc') and not input_path.lower().endswith('.docx'):
        input_path = convert_doc_to_docx(input_path)
        cleanup_path = input_path
    text = convert_basic(input_path)
    if not keep_images:
        text = strip_images(text)
    text = fix_encoding(text.encode('utf-8'))
    text = convert_chinese(text)
    return text, cleanup_path


def run_single_okf(args) -> NoReturn:
    """Convert one source into an isolated run and hand it to okf-frontmatter."""
    run_dir = _okf_run_dir(args)
    staged_dir = run_dir / 'converted'
    staged_name = Path(args.output_path).name or 'converted.md'
    staged = staged_dir / staged_name
    cleanup_path = None
    try:
        text, cleanup_path = _convert_text_for_okf(args.input, args.keep_images)
        _write_okf_stage(staged, text)
        source = args.source if args.source else args.input
        if not is_url(source):
            source = os.path.abspath(source)
        source = source.replace('\\', '/')
        converted_at = args.converted_at or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        metadata = _write_okf_metadata(
            run_dir,
            [{
                'path': str(staged),
                'fields': {'source': source, 'converted_at': converted_at, 'converted_by': 'markitdown'},
            }],
        )
        _invoke_okf_prepare(args, staged, args.output_path, metadata, run_dir)
    except SystemExit:
        if not (run_dir / 'run.json').exists():
            print(f'[RECOVERY] OKF run retained at {run_dir}', file=sys.stderr)
        raise
    except Exception as exc:
        print(f'[RECOVERY] OKF run retained at {run_dir}', file=sys.stderr)
        die(str(exc))
    finally:
        if cleanup_path and os.path.exists(cleanup_path):
            os.unlink(cleanup_path)


def run_batch_okf(args) -> NoReturn:
    """Convert a batch into one isolated OKF review run without final writes."""
    recursive = not args.no_recursive
    types = [item.strip() for item in args.types.split(',')] if args.types else None
    if types is not None:
        requested = {item.lower() if item.startswith('.') else f'.{item.lower()}' for item in types}
        if requested.isdisjoint(SUPPORTED_EXTENSIONS):
            die(f'Unsupported file type in --types: {", ".join(sorted(requested))}')
    files = collect_files(args.input_dir, recursive, types)
    if not files:
        print('[BATCH] 0 converted, 0 failed, 0 skipped')
        sys.exit(0)
    run_dir = _okf_run_dir(args)
    staged_root = run_dir / 'converted'
    input_root = os.path.abspath(args.input_dir)
    metadata_items = []
    failed = []
    for source_path in files:
        relative = os.path.relpath(source_path, input_root)
        staged = staged_root / f'{os.path.splitext(relative)[0]}.md'
        cleanup_path = None
        try:
            text, cleanup_path = _convert_text_for_okf(source_path, args.keep_images)
            _write_okf_stage(staged, text)
            converted_at = args.converted_at or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
            metadata_items.append({
                'path': str(staged),
                'fields': {
                    'source': os.path.abspath(source_path).replace('\\', '/'),
                    'converted_at': converted_at,
                    'converted_by': 'markitdown',
                },
            })
        except SystemExit:
            failed.append(relative)
        except Exception as exc:
            print(f'[FAIL] {relative} - {exc}', file=sys.stderr)
            failed.append(relative)
        finally:
            if cleanup_path and os.path.exists(cleanup_path):
                os.unlink(cleanup_path)
    if failed and not args.accept_partial:
        die(f'OKF batch conversion failed for {len(failed)} file(s); staged run retained at {run_dir}')
    if not metadata_items:
        die(f'OKF batch conversion produced no staged files; run retained at {run_dir}')
    metadata = _write_okf_metadata(run_dir, metadata_items)
    _invoke_okf_prepare(args, staged_root, args.output_path, metadata, run_dir)


def precheck(args):
    """Validate inputs before any conversion. Exits with error on failure."""
    # 1. Mutual exclusivity
    if args.input and args.input_dir:
        die('--input and --input-dir are mutually exclusive')
    if not args.input and not args.input_dir:
        die('Either --input <file> or --input-dir <directory> is required')
    if args.workspace:
        args.okf = True
    if args.okf and args.no_frontmatter:
        die('--okf/--workspace and --no-frontmatter are mutually exclusive')
    if args.overwrite and args.rename:
        die('--overwrite and --rename are mutually exclusive')
    if args.workspace:
        if not os.path.isdir(args.workspace):
            die_code(f'Cortex workspace not found: {args.workspace}', 4)
        if shutil.which('cortex') is None:
            die_code('Cortex CLI is not available on PATH', 4)

    # 2. Input path validation (skip for URLs — markitdown handles fetching)
    if args.input and not is_url(args.input) and not os.path.exists(args.input):
        die(f'File not found: {args.input} — verify the path is correct and the file exists.')
    if args.input_dir and not os.path.isdir(args.input_dir):
        die(f'Directory not found: {args.input_dir}')


def resolve_output_path(args) -> str:
    """Derive a default output path when --output-path is not given.

    Defaults to writing alongside the source file:
      - Single local file   -> <source_dir>/<stem>.md
      - URL                 -> ./<slug>.md (current working directory)
      - Batch (--input-dir) -> the input directory itself (each .md beside its source)
    """
    if args.input:
        if is_url(args.input):
            slug = url_to_slug(args.input)
            return os.path.join('.', f'{slug}.md')
        source_dir = os.path.dirname(os.path.abspath(args.input))
        stem = os.path.splitext(os.path.basename(args.input))[0]
        return os.path.join(source_dir, f'{stem}.md')
    else:
        # Batch: write .md files next to each source, inside the input directory.
        return args.input_dir


def show_version():
    """Print skill version and dependency status, then exit."""
    print(f'markdown-conversion v{VERSION}')
    print('Dependencies:')
    for import_name, pip_name, required in DEPS:
        try:
            mod = __import__(import_name)
            ver = getattr(mod, '__version__', 'ok')
            label = f'{ver}' if ver != 'ok' else 'installed'
        except ImportError:
            label = 'NOT INSTALLED' if required else 'not installed (optional)'
        print(f'  {pip_name}: {label}')
    try:
        ruamel_version = importlib.metadata.version('ruamel.yaml')
        ruamel_label = ruamel_version if ruamel_version.startswith('0.17.') else f'{ruamel_version} (incompatible; need >=0.17,<0.18)'
    except importlib.metadata.PackageNotFoundError:
        ruamel_label = 'not installed (required for --okf)'
    print(f'  ruamel.yaml: {ruamel_label}')
    print(f'  cortex: {"available" if shutil.which("cortex") else "not installed (required for --workspace)"}')


def main():
    # Pre-scan for --config before full argparse (needed because load_config()
    # runs before other defaults are set from config values).
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument('--config', default='', help='Path to config.json (default: scripts/config.json)')
    _pre.add_argument('--version', action='store_true')
    _early, _ = _pre.parse_known_args()

    if _early.version:
        show_version()
        sys.exit(0)

    global CONFIG_PATH
    if _early.config:
        CONFIG_PATH = os.path.abspath(_early.config)

    load_config()  # honor --config / create config.json (markdown-conversion has no settings)

    parser = argparse.ArgumentParser(description='markdown-conversion pipeline.')
    parser.add_argument('--config', default='', help='Path to config.json (default: scripts/config.json)')
    parser.add_argument('--version', action='store_true', help='Show version and dependency status')
    parser.add_argument('--input', dest='input', help='Path to source file to convert')
    parser.add_argument('--input-dir', dest='input_dir', help='Directory of files to batch convert')
    parser.add_argument('--source', default='', help='Original file path (for frontmatter)')
    parser.add_argument('--output-path', dest='output_path', default='',
                        help='Output path (optional — defaults to the source file directory if not given)')
    parser.add_argument('--no-frontmatter', action='store_true', dest='no_frontmatter')
    parser.add_argument('--okf', action='store_true', help='Stage conversion for reviewed OKF frontmatter')
    parser.add_argument('--workspace', default='', help='Cortex workspace; implies --okf and requires Cortex CLI')
    parser.add_argument('--okf-run-dir', default='', dest='okf_run_dir',
                        help='Retained OKF run directory (default: system temporary directory)')
    parser.add_argument('--accept-partial', action='store_true', dest='accept_partial',
                        help='Allow complete items from a partially converted OKF batch')
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--rename', action='store_true')
    parser.add_argument('--keep-images', action='store_true', dest='keep_images',
                        help='Preserve markdown image links (default: images are stripped)')
    parser.add_argument('--converted-at', dest='converted_at', default='')
    # Batch-only
    parser.add_argument('--no-recursive', action='store_true', dest='no_recursive')
    parser.add_argument('--types', default='', help='Comma-separated extensions to include (batch only)')
    args = parser.parse_args()

    # Pre-flight validation (file existence)
    precheck(args)

    # Resolve default output path (next to source) if not explicitly given
    if not args.output_path:
        args.output_path = resolve_output_path(args)

    if args.input_dir:
        if args.okf:
            run_batch_okf(args)
        else:
            os.makedirs(args.output_path, exist_ok=True)
            run_batch(args)
    else:
        if args.okf:
            run_single_okf(args)
        # Single-file mode (existing logic)
        input_path = args.input
        if not is_url(input_path) and input_path.lower().endswith('.doc') and not input_path.lower().endswith('.docx'):
            docx_path = convert_doc_to_docx(input_path)
            input_path = docx_path

        text = convert_basic(input_path)

        # Strip images unless explicitly kept
        if not args.keep_images:
            text = strip_images(text)

        text = fix_encoding(text.encode('utf-8'))
        text = convert_chinese(text)

        source = args.source if args.source else args.input
        converted_at = args.converted_at or datetime.datetime.now().isoformat(timespec='seconds')

        if not args.no_frontmatter:
            text = inject_frontmatter(text, source, converted_at)

        final_path = write_to_vault(text, args.output_path, args.overwrite, args.rename)
        print(f'[OK] Converted {source} -> {final_path}')


if __name__ == '__main__':
    main()
