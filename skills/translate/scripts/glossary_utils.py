"""
glossary_utils.py - Glossary and reference file handling for translate pipeline.

Functions for:
  - Loading glossary files (JSON, CSV/TSV, MD tables)
  - Collecting reference files from paths (files and directories)
  - Converting non-.md references via markitdown
"""
import os
import csv
import json
import io
import re
import sys
from typing import NoReturn


def die(msg: str) -> NoReturn:
    print(f'ERROR: {msg}', file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Supported extensions (reuse from markdown-conversion)
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    '.pdf', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls',
    '.html', '.csv', '.json', '.jsonl', '.xml', '.epub',
    '.jpg', '.jpeg', '.png', '.gif',
    '.mp3', '.wav', '.mp4',
    '.zip', '.txt', '.rtf', '.odt', '.ods', '.odp',
    '.md', '.markdown',
}


def _ensure_markitdown():
    """Import markitdown, auto-install if missing."""
    try:
        from markitdown import MarkItDown
        return MarkItDown
    except ImportError:
        import subprocess
        print('Installing markitdown...', file=sys.stderr)
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'markitdown'])
        from markitdown import MarkItDown
        return MarkItDown


def convert_to_markdown(file_path: str) -> str:
    """Convert a file to markdown using markitdown. Returns text content."""
    MarkItDown = _ensure_markitdown()
    md = MarkItDown()
    result = md.convert(file_path)
    return result.text_content


# ---------------------------------------------------------------------------
# Glossary loading
# ---------------------------------------------------------------------------

# Column name patterns for auto-detecting source/target columns in CSV/TSV
_SOURCE_HEADERS = {'source', 'english', 'en', 'original', 'from', 'key', 'term'}
_TARGET_HEADERS = {'target', 'chinese', 'zh', 'translation', 'to', 'value', 'translated'}


def load_glossary_json(path: str) -> dict[str, str]:
    """Load a JSON glossary file. Expects dict of source -> target."""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if isinstance(data, dict):
        # Simple source -> target mapping
        return {str(k): str(v) for k, v in data.items()}

    if isinstance(data, list):
        # List of dicts with source/target keys
        result = {}
        for item in data:
            if isinstance(item, dict):
                src = _find_key(item, _SOURCE_HEADERS)
                tgt = _find_key(item, _TARGET_HEADERS)
                if src and tgt:
                    result[str(src)] = str(tgt)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                result[str(item[0])] = str(item[1])
        return result

    die(f'Unexpected JSON structure in glossary: {path}')


def load_glossary_csv(path: str) -> dict[str, str]:
    """Load a CSV/TSV glossary. Auto-detects delimiter and source/target columns."""
    with open(path, 'r', encoding='utf-8') as f:
        sample = f.read(4096)
        f.seek(0)

        # Detect delimiter
        sniffer = csv.Sniffer()
        try:
            dialect = sniffer.sniff(sample, delimiters=',\t;|')
        except csv.Error:
            # Default to comma
            dialect = csv.excel

        reader = csv.reader(f, dialect)
        rows = list(reader)

    if not rows:
        return {}

    # Try to detect source/target columns from header
    header = [h.strip().lower() for h in rows[0]]
    src_col = _find_col_index(header, _SOURCE_HEADERS)
    tgt_col = _find_col_index(header, _TARGET_HEADERS)

    if src_col is not None and tgt_col is not None:
        # Use header-detected columns
        result = {}
        for row in rows[1:]:
            if len(row) > max(src_col, tgt_col):
                src = row[src_col].strip()
                tgt = row[tgt_col].strip()
                if src and tgt:
                    result[src] = tgt
        return result

    # Fallback: first two columns (no header detected — treat all rows as data)
    if len(rows[0]) >= 2:
        result = {}
        for row in rows:
            if len(row) >= 2:
                src = row[0].strip()
                tgt = row[1].strip()
                if src and tgt:                    result[src] = tgt
        return result

    return {}


def load_glossary_md(path: str) -> dict[str, str]:
    """Load a markdown table glossary. Returns empty dict if not a table."""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Try to parse as markdown table
    table_entries = _parse_md_table(content)
    if table_entries:
        return dict(table_entries)

    # Not a table — return empty (will be treated as reference text)
    return {}


def load_glossary(path: str) -> dict[str, str]:
    """Load a glossary file based on extension. Returns source -> target dict."""
    ext = os.path.splitext(path)[1].lower()

    if ext == '.json':
        return load_glossary_json(path)
    elif ext in ('.csv', '.tsv'):
        return load_glossary_csv(path)
    elif ext in ('.md', '.markdown'):
        return load_glossary_md(path)
    else:
        # Try JSON first, then CSV
        try:
            return load_glossary_json(path)
        except (json.JSONDecodeError, ValueError):
            return load_glossary_csv(path)


# ---------------------------------------------------------------------------
# Reference file collection
# ---------------------------------------------------------------------------

def collect_reference_files(paths: list[str]) -> list[str]:
    """Expand a list of file/directory paths into a flat list of file paths.

    Directories are walked recursively, collecting all supported file types.
    """
    files = []
    for path in paths:
        path = os.path.abspath(path)
        if os.path.isfile(path):
            files.append(path)
        elif os.path.isdir(path):
            for root, _dirs, filenames in os.walk(path):
                for fname in sorted(filenames):
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in SUPPORTED_EXTENSIONS:
                        files.append(os.path.join(root, fname))
        else:
            print(f'Warning: reference path not found, skipping: {path}', file=sys.stderr)
    return files


def load_reference_content(path: str) -> str:
    """Load a reference file. Converts to markdown if not already .md."""
    ext = os.path.splitext(path)[1].lower()

    if ext in ('.md', '.markdown', '.txt'):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    # Convert via markitdown
    return convert_to_markdown(path)


# ---------------------------------------------------------------------------
# Glossary I/O
# ---------------------------------------------------------------------------

def save_glossary(glossary: dict[str, str], path: str) -> None:
    """Save a glossary dict to a JSON file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(glossary, f, indent=2, ensure_ascii=False)


def load_glossary_file(path: str) -> dict[str, str]:
    """Load a glossary from any supported format. Alias for load_glossary."""
    return load_glossary(path)


# ---------------------------------------------------------------------------
# Output path generation
# ---------------------------------------------------------------------------

# Common language suffixes to strip before adding a new one
_LANG_SUFFIXES = {
    '.zh', '.en', '.ja', '.ko', '.fr', '.de', '.es', '.pt', '.ru', '.ar',
    '.zh-cn', '.zh-tw', '.zh-hans', '.zh-hant', '.en-us', '.en-gb',
}


def derive_output_path(input_path: str, target_lang: str) -> str:
    """Generate output filename with language suffix.

    report.md -> report.zh.md
    report.zh.md -> report.en.md (strips existing lang suffix)
    """
    directory = os.path.dirname(os.path.abspath(input_path))
    basename = os.path.basename(input_path)
    stem, ext = os.path.splitext(basename)

    # Strip existing language suffix
    stem_lower = stem.lower()
    for suffix in sorted(_LANG_SUFFIXES, key=len, reverse=True):
        if stem_lower.endswith(suffix):
            stem = stem[:-len(suffix)]
            break

    # Also handle double extension like .zh.md
    if ext.lower() == '.md':
        inner_stem, inner_ext = os.path.splitext(stem)
        if f'.{inner_ext.lower()}' in _LANG_SUFFIXES or inner_ext.lower() in {'.zh', '.en', '.ja', '.ko'}:
            stem = inner_stem

    return os.path.join(directory, f'{stem}.{target_lang}.md')


def derive_glossary_path(input_path: str, target_lang: str) -> str:
    """Generate glossary output filename.

    report.md -> report.glossary.zh.json
    """
    directory = os.path.dirname(os.path.abspath(input_path))
    basename = os.path.basename(input_path)
    stem = os.path.splitext(basename)[0]
    return os.path.join(directory, f'{stem}.glossary.{target_lang}.json')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_key(d: dict, key_set: set) -> str | None:
    """Find a key in dict matching any of the given key names (case-insensitive)."""
    for key in d:
        if str(key).strip().lower() in key_set:
            return d[key]
    return None


def _find_col_index(headers: list[str], key_set: set) -> int | None:
    """Find column index matching any of the given header names."""
    for i, h in enumerate(headers):
        if h.strip().lower() in key_set:
            return i
    return None


def _parse_md_table(content: str) -> list[tuple[str, str]]:
    """Parse a markdown table into (source, target) pairs."""
    lines = content.strip().split('\n')
    rows = []
    for line in lines:
        line = line.strip()
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        # Skip separator rows (e.g., |---|---|)
        if all(set(c) <= {'-', ':', ' '} for c in cells):
            continue
        rows.append(cells)

    if len(rows) < 2:
        return []

    # Detect source/target columns from header
    header = [h.lower() for h in rows[0]]
    src_col = _find_col_index(header, _SOURCE_HEADERS)
    tgt_col = _find_col_index(header, _TARGET_HEADERS)

    if src_col is not None and tgt_col is not None:
        return [
            (row[src_col], row[tgt_col])
            for row in rows[1:]
            if len(row) > max(src_col, tgt_col) and row[src_col] and row[tgt_col]
        ]

    # Fallback: first two columns
    return [
        (row[0], row[1])
        for row in rows[1:]
        if len(row) >= 2 and row[0] and row[1]
    ]
