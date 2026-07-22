#!/usr/bin/env python3
"""
pipeline.py - markdown-conversion pipeline.

Steps:
  1. Convert document using markitdown Python API (basic)
  2. Strip image markers
  3. Fix encoding (single-pass chardet + compiled regex mojibake scan)
  4. Convert Traditional → Simplified Chinese (two-pass opencc with stability gate)
  5. Inject deterministic draft YAML frontmatter (unless --no-frontmatter)
  6. Write the final Markdown directly

Exit codes:
  0 - success
  1 - gate failure or error (message on stderr)
  2 - output file exists and neither --overwrite nor --rename passed
"""
import sys
import os
import re
import json
import argparse
import datetime
import urllib.parse
from pathlib import Path
from typing import NoReturn

VERSION = '5.0.0'

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
_h1_re = re.compile(r'^ {0,3}#(?!#)\s+(.+?)(?:\s+#+\s*)?$')
_fence_re = re.compile(r'^ {0,3}(`{3,}|~{3,})')
_rfc3339_datetime_re = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$'
)


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


def die(msg: str) -> NoReturn:
    print(f'ERROR: {msg}', file=sys.stderr)
    sys.exit(1)


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


def _title_from_text_or_source(text: str, source: str) -> str:
    fence_char = None
    fence_length = 0
    for line in text.splitlines():
        if fence_char is not None:
            closing_fence = re.fullmatch(
                rf' {{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*', line
            )
            if closing_fence is not None:
                fence_char = None
                fence_length = 0
            continue
        fence = _fence_re.match(line)
        if fence is not None:
            marker = fence.group(1)
            fence_char = marker[0]
            fence_length = len(marker)
            continue
        match = _h1_re.match(line)
        if match is not None:
            title = match.group(1).strip()
            title = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', title)
            title = re.sub(r'[*_`]+', '', title).strip()
            if title:
                return title
    if is_url(source):
        path = urllib.parse.unquote(urllib.parse.urlparse(source).path)
        return Path(path).stem or url_to_slug(source)
    return Path(source).stem or 'untitled'


def resolve_timestamp(value: str) -> str:
    """Validate an override or return the timezone-aware conversion time.

    ISO dates and timezone-aware ISO datetimes are emitted byte-for-byte as
    supplied. Naive datetimes are rejected because their instant is ambiguous.
    """
    if not value:
        return datetime.datetime.now().astimezone().isoformat(timespec='seconds')

    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            die('--timestamp must be an ISO date or a timezone-aware ISO datetime')
        return value

    if _rfc3339_datetime_re.fullmatch(value) is None:
        die('--timestamp must be an ISO date or RFC3339 timezone-aware datetime')
    try:
        parsed = datetime.datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    except ValueError:
        die('--timestamp must be an ISO date or a timezone-aware ISO datetime')
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        die('--timestamp datetime must include a timezone offset')
    return value


def inject_frontmatter(text: str, source: str, timestamp: str) -> str:
    """Prepend the exact five-field deterministic draft frontmatter."""
    title = _title_from_text_or_source(text, source)
    fm = (
        '---\n'
        'type: ""\n'
        f'title: {json.dumps(title, ensure_ascii=False)}\n'
        'description: ""\n'
        'tags: []\n'
        f'timestamp: {json.dumps(timestamp, ensure_ascii=False)}\n'
        '---\n\n'
    )
    result = fm + text

    # Gate: verify required fields present
    if not result.startswith('---'):
        die('Frontmatter injection failed: output does not start with ---')
    for field in ('type:', 'title:', 'description:', 'tags:', 'timestamp:'):
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

            # Image markers are intentionally removed from portable text output.
            text = strip_images(text)

            # Fix encoding
            text = fix_encoding(text.encode('utf-8'))

            # T->S conversion
            text = convert_chinese(text)

            # Frontmatter
            if not args.no_frontmatter:
                source = fpath.replace('\\', '/')
                text = inject_frontmatter(text, source, args.timestamp)

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


def precheck(args):
    """Validate inputs before any conversion. Exits with error on failure."""
    # 1. Mutual exclusivity
    if args.input and args.input_dir:
        die('--input and --input-dir are mutually exclusive')
    if not args.input and not args.input_dir:
        die('Either --input <file> or --input-dir <directory> is required')
    if args.overwrite and args.rename:
        die('--overwrite and --rename are mutually exclusive')

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


def main():
    global CONFIG_PATH
    parser = argparse.ArgumentParser(description='markdown-conversion pipeline.')
    parser.add_argument('--config', default='', help='Path to config.json (default: scripts/config.json)')
    parser.add_argument('--version', action='store_true', help='Show version and dependency status')
    parser.add_argument('--input', dest='input', help='Path to source file to convert')
    parser.add_argument('--input-dir', dest='input_dir', help='Directory of files to batch convert')
    parser.add_argument('--output-path', dest='output_path', default='',
                        help='Output path (optional — defaults to the source file directory if not given)')
    parser.add_argument('--no-frontmatter', action='store_true', dest='no_frontmatter')
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--rename', action='store_true')
    parser.add_argument('--timestamp', default='', help='ISO date or RFC3339 timezone-aware datetime override')
    # Batch-only
    parser.add_argument('--no-recursive', action='store_true', dest='no_recursive')
    parser.add_argument('--types', default='', help='Comma-separated extensions to include (batch only)')
    args = parser.parse_args()

    # Parse the entire argv before honoring --version so removed/unknown options
    # cannot be silently accepted alongside it.
    if args.version:
        show_version()
        sys.exit(0)

    if args.config:
        CONFIG_PATH = os.path.abspath(args.config)
    load_config()  # honor --config / create config.json (markdown-conversion has no settings)

    # Pre-flight validation (file existence)
    precheck(args)
    args.timestamp = resolve_timestamp(args.timestamp)

    # Resolve default output path (next to source) if not explicitly given
    if not args.output_path:
        args.output_path = resolve_output_path(args)

    if args.input_dir:
        os.makedirs(args.output_path, exist_ok=True)
        run_batch(args)
    else:
        # Single-file mode (existing logic)
        input_path = args.input
        if not is_url(input_path) and input_path.lower().endswith('.doc') and not input_path.lower().endswith('.docx'):
            docx_path = convert_doc_to_docx(input_path)
            input_path = docx_path

        text = convert_basic(input_path)

        # Image markers are intentionally removed from portable text output.
        text = strip_images(text)

        text = fix_encoding(text.encode('utf-8'))
        text = convert_chinese(text)

        if not args.no_frontmatter:
            text = inject_frontmatter(text, args.input, args.timestamp)

        final_path = write_to_vault(text, args.output_path, args.overwrite, args.rename)
        print(f'[OK] Converted {args.input} -> {final_path}')


if __name__ == '__main__':
    main()
