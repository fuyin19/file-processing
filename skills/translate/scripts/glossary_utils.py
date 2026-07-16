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
    """Load a JSON glossary file as a flat source -> target dict.

    Accepts three shapes (backward compatible):
      - flat dict {source: target}
      - list of dicts with source/target keys (or [source, target] pairs)
      - structured wrapper {"terms": [{source, target, ...}, ...]} (metadata is
        dropped here; use load_glossary_structured to keep it)
    """
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if isinstance(data, dict):
        # Structured wrapper -> extract source/target only.
        if 'terms' in data and isinstance(data['terms'], list):
            result = {}
            for item in data['terms']:
                if isinstance(item, dict):
                    src = item.get('source')
                    tgt = item.get('target')
                    if src and tgt:
                        result[str(src)] = str(tgt)
            return result
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

def save_glossary(glossary, path: str) -> None:
    """Save a glossary to JSON.

    Polymorphic by input (backward compatible):
      - flat dict {source: target} -> written as-is (legacy flat file)
      - list of structured entries -> written as {"terms": [...]} (structured)
      - {"terms": [...]} dict -> written as structured
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if isinstance(glossary, list):
        out = {'terms': glossary}
    elif isinstance(glossary, dict) and 'terms' in glossary and isinstance(glossary['terms'], list):
        out = glossary
    else:
        out = glossary
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


def load_glossary_structured(path: str) -> list[dict]:
    """Load a glossary as a list of structured entries.

    - Structured {"terms": [...]} input is returned (entries kept as-is).
    - Legacy flat dict / CSV / MD / list-of-dicts input is wrapped: each term
      becomes {"source": s, "target": t, "confidence": "high"}.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == '.json':
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and 'terms' in data and isinstance(data['terms'], list):
            return data['terms']
        if isinstance(data, list):
            entries = []
            for item in data:
                if isinstance(item, dict):
                    src = _find_key(item, _SOURCE_HEADERS)
                    tgt = _find_key(item, _TARGET_HEADERS)
                    if src and tgt:
                        entries.append({'source': str(src), 'target': str(tgt), 'confidence': 'high'})
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    entries.append({'source': str(item[0]), 'target': str(item[1]), 'confidence': 'high'})
            return entries
        if isinstance(data, dict):
            return [{'source': str(k), 'target': str(v), 'confidence': 'high'} for k, v in data.items()]
        return []
    # Legacy CSV / MD / flat -> wrap as confidence:high seeds.
    flat = load_glossary(path)
    return [{'source': s, 'target': t, 'confidence': 'high'} for s, t in flat.items()]


def load_glossary_v3(path: str) -> list[dict]:
    """Load any supported glossary as v3 user-seed entries.

    Legacy glossary formats contain no reference evidence.  Marking them as
    ``user_seed`` retains their authority without falsely improving reference
    coverage metrics.
    """
    entries = load_glossary_structured(path)
    result = []
    for entry in entries:
        copied = dict(entry)
        copied.setdefault('schema_version', '3.0')
        copied.setdefault('origin', 'user_seed')
        copied.setdefault('evidence', [])
        copied.setdefault('allowed_targets', [copied.get('target')] if copied.get('target') else [])
        result.append(copied)
    return result


def partition_relevance_batches(items: list[dict], max_items: int) -> list[list[dict]]:
    """Partition an already-complete relevance universe without dropping items.

    The ordering is deterministic by the stable id (or source as a fallback),
    which lets the orchestrator persist and audit every batch.  Unlike the v2
    glossary slice cap, no item is silently discarded.
    """
    if max_items <= 0:
        raise ValueError('max_items must be positive')
    ordered = sorted(items, key=lambda x: (str(x.get('occurrence_id') or x.get('term_id') or x.get('source') or ''),
                                            str(x.get('source') or '')))
    return [ordered[i:i + max_items] for i in range(0, len(ordered), max_items)]


def save_glossary_structured(terms: list[dict], path: str) -> None:
    """Write structured glossary {"terms": [...]}."""
    save_glossary(terms, path)


# ---------------------------------------------------------------------------
# Glossary slicing (per-chunk) and target matching
# ---------------------------------------------------------------------------

_CONF_RANK = {'high': 0, 'medium': 1, 'none': 2, None: 3}


def slice_glossary_for_chunk(
    terms: list[dict],
    chunk_index: int,
    protected_sources: list[str] | None = None,
    max_terms: int | None = None,
) -> dict:
    """Return the glossary entries relevant to one chunk.

    An entry is relevant if the chunk is in its ``source_chunks`` or in any of its
    ``occurrences`` [{chunk, line}], or if its source is in ``protected_sources``
    (high-frequency / proper-noun / user seed terms kept in every chunk).
    When the selection exceeds ``max_terms``, it is truncated by priority
    (confidence high>medium>none, then occurrence count, then seed membership).
    """
    protected = set(protected_sources or [])
    selected = []
    for t in terms:
        src = t.get('source')
        chunks = set(t.get('source_chunks') or [])
        occ_chunks = {
            o.get('chunk') for o in (t.get('occurrences') or []) if isinstance(o, dict)
        }
        if chunk_index in chunks or chunk_index in occ_chunks or src in protected:
            selected.append(t)
    truncated = False
    if max_terms and len(selected) > max_terms:
        selected.sort(
            key=lambda t: (
                _CONF_RANK.get(t.get('confidence'), 3),
                -len(t.get('occurrences') or []),
                0 if t.get('source') in protected else 1,
                t.get('source', ''),
            )
        )
        selected = selected[:max_terms]
        truncated = True
    return {'terms': selected, 'count': len(selected), 'truncated': truncated}


def _normalize_latin(s: str) -> str:
    import unicodedata
    nfkd = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _is_cjk(s: str) -> bool:
    return any('一' <= ch <= '鿿' for ch in s)


def normalize_target(target: str) -> str:
    """Normalized comparison form (lowercase + diacritics stripped)."""
    return _normalize_latin(target or '')


def target_present(target: str, text: str) -> bool:
    """True if ``target`` appears in ``text``.

    CJK targets -> exact substring match. Latin targets -> normalized form
    (lowercase + diacritics stripped) with word boundaries, so morphological
    variants match while avoiding partial-word false positives.
    """
    if not target:
        return False
    if _is_cjk(target):
        return target in text
    norm_target = _normalize_latin(target)
    if not norm_target:
        return False
    norm_text = _normalize_latin(text)
    return re.search(r'\b' + re.escape(norm_target) + r'\b', norm_text) is not None


# ---------------------------------------------------------------------------
# Structure-safe chunking (shared with content-review's review_plan; duplicated
# here so the translate pipeline does not cross-import another skill)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r'^\s*(```|~~~)')


def _is_fence_open(line: str) -> bool:
    return bool(_FENCE_RE.match(line))


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith('|')


def parse_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Split text into atomic blocks (fenced code / tables / prose+blank runs).

    Fenced code and tables are never split. Every input line lands in exactly one
    block, so concatenating blocks reproduces the source.
    """
    lines = text.split('\n')
    blocks: list[tuple[str, list[str]]] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if _is_fence_open(line):
            fence = _FENCE_RE.match(line).group(1)
            j = i + 1
            buf = [line]
            while j < n:
                buf.append(lines[j])
                if re.match(r'^\s*' + re.escape(fence) + r'\s*$', lines[j].strip()):
                    j += 1
                    break
                j += 1
            blocks.append(('code', buf))
            i = j
        elif _is_table_row(line):
            buf = []
            while i < n and _is_table_row(lines[i]):
                buf.append(lines[i])
                i += 1
            blocks.append(('table', buf))
        elif line.strip() == '':
            blocks.append(('prose', [line]))
            i += 1
        else:
            buf = []
            while (
                i < n and lines[i].strip() != ''
                and not _is_fence_open(lines[i]) and not _is_table_row(lines[i])
            ):
                buf.append(lines[i])
                i += 1
            blocks.append(('prose', buf))
    return blocks


def pack_blocks(blocks: list[tuple[str, list[str]]], chunk_lines: int) -> list[list[tuple[str, list[str]]]]:
    """Greedily pack atomic blocks into chunks of at most ~chunk_lines lines.

    A single block larger than chunk_lines becomes its own (oversized) chunk
    rather than being split.
    """
    chunks: list[list[tuple[str, list[str]]]] = []
    cur: list[tuple[str, list[str]]] = []
    cur_lines = 0
    for btype, blines in blocks:
        blen = len(blines)
        if cur and cur_lines + blen > chunk_lines:
            chunks.append(cur)
            cur, cur_lines = [], 0
        cur.append((btype, blines))
        cur_lines += blen
    if cur:
        chunks.append(cur)
    return chunks


def chunk_text(text: str, chunk_lines: int) -> list[dict]:
    """Return structure-safe chunks: [{index, start, end, lines, text, oversized}]."""
    packed = pack_blocks(parse_blocks(text), chunk_lines)
    out = []
    lineno = 1
    for idx, chunk in enumerate(packed, start=1):
        n_in = sum(len(blines) for _, blines in chunk)
        start = lineno
        end = lineno + n_in - 1
        body = '\n'.join(line for _, blines in chunk for line in blines)
        out.append({
            'index': idx,
            'start': start,
            'end': end,
            'lines': n_in,
            'text': body,
            'oversized': n_in > chunk_lines,
        })
        lineno = end + 1
    return out


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
