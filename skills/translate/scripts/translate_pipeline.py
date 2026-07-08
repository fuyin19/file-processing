#!/usr/bin/env python3
"""
translate_pipeline.py - Translation pipeline for the file-processing plugin.

Provides three subcommands:
  prepare  - Read source file and references, chunk them (structure-safe), write
             chunk + passage files to a workspace, and output structured text for
             the orchestrator (chunk plan + passage manifest + the legacy
             SOURCE/REFERENCES/INSTRUCTIONS sections).
  qa       - Compare source vs translation: structural counts, untranslated
             fragments, and per-occurrence glossary forced-application with a
             convergence gate, confidence:none consistency, and a fix-loop map.
             Auto-discovers the prepared glossary via derive_glossary_path.
  write    - Write translated file with proper naming.

Exit codes:
  0 - success
  1 - error (message on stderr)
"""
import sys
import os
import json
import argparse
import re
import datetime
from typing import NoReturn

VERSION = '2.0.0'

# Import from sibling module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from glossary_utils import (
    die,
    collect_reference_files,
    load_reference_content,
    load_glossary,
    load_glossary_structured,
    save_glossary,
    save_glossary_structured,
    slice_glossary_for_chunk,
    target_present,
    normalize_target,
    chunk_text,
    derive_output_path,
    derive_glossary_path,
    convert_to_markdown,
    SUPPORTED_EXTENSIONS,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "default_target_language": "zh",
    "chunk_lines": 300,
    "max_chunks": 30,
    "max_terms": 800,
    "max_terms_per_chunk_prompt": 120,
    "max_reference_passages_per_term": 5,
    "max_workspace_mb": 100,
}

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
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


# ---------------------------------------------------------------------------
# Code block / frontmatter protection
# ---------------------------------------------------------------------------

_PLACEHOLDER = '\x00'


def protect_code_blocks(text: str) -> tuple[str, dict]:
    """Replace fenced and inline code blocks with placeholders."""
    placeholders = {}
    counter = [0]

    def _key(prefix):
        key = f'{_PLACEHOLDER}{prefix}_{counter[0]}{_PLACEHOLDER}'
        counter[0] += 1
        return key

    def _fenced(m):
        k = _key('FENCED')
        placeholders[k] = m.group(0)
        return k

    text = re.sub(
        r'(?m)^(?:```|~~~)[^\n]*\n(?:.*\n)*?(?:```|~~~)',
        _fenced, text, flags=re.DOTALL,
    )

    def _inline(m):
        k = _key('INLINE')
        placeholders[k] = m.group(0)
        return k

    text = re.sub(r'`[^`\n]+`', _inline, text)
    return text, placeholders


def extract_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body). frontmatter_block is '' if none."""
    if not text.startswith('---'):
        return '', text
    end = text.find('\n---', 3)
    if end == -1:
        return '', text
    return text[:end + 4], text[end + 4:].lstrip('\n')


# ---------------------------------------------------------------------------
# Structural analysis helpers
# ---------------------------------------------------------------------------

def count_headings(text: str) -> dict[int, int]:
    """Count headings by level."""
    counts = {}
    for m in re.finditer(r'^(#{1,6})\s+', text, re.MULTILINE):
        level = len(m.group(1))
        counts[level] = counts.get(level, 0) + 1
    return counts


def count_paragraphs(text: str) -> int:
    """Count non-empty, non-heading, non-table, non-code lines as paragraphs."""
    lines = text.split('\n')
    count = 0
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('```') or stripped.startswith('~~~'):
            in_block = not in_block
            continue
        if in_block:
            continue
        if not stripped or stripped.startswith('#') or stripped.startswith('|'):
            continue
        count += 1
    return count


def count_tables(text: str) -> int:
    """Count markdown table blocks."""
    count = 0
    in_table = False
    for line in text.split('\n'):
        if line.strip().startswith('|'):
            if not in_table:
                count += 1
                in_table = True
        else:
            in_table = False
    return count


def detect_untranslated_fragments(text: str, source_lang: str, target_lang: str) -> list[str]:
    """Detect likely untranslated fragments in the translation."""
    protected, _ = protect_code_blocks(text)
    protected = re.sub(r'`[^`\n]+`', '', protected)

    fragments = []

    if target_lang.lower() in ('zh', 'zh-cn', 'zh-hans', 'chinese'):
        for m in re.finditer(r'[A-Za-z]+(?:\s+[A-Za-z]+){2,}', protected):
            fragment = m.group(0)
            if re.match(r'^https?://', fragment):
                continue
            if len(fragment) < 10:
                continue
            fragments.append(fragment)

    elif target_lang.lower() in ('en', 'english'):
        for m in re.finditer(r'[一-鿿]{2,}', protected):
            fragments.append(m.group(0))

    return fragments


def _detect_language(text: str) -> str:
    """Rough language detection: CJK vs Latin."""
    cjk = len(re.findall(r'[一-鿿]', text))
    latin = len(re.findall(r'[A-Za-z]', text))
    return 'zh' if cjk > latin else 'en'


# ---------------------------------------------------------------------------
# Chunking + reference passage helpers
# ---------------------------------------------------------------------------

def write_chunk_files(text: str, chunk_lines: int, workspace: str) -> list[dict]:
    """Chunk text (structure-safe) and write chunk_NNN.md files. Return chunk metadata."""
    os.makedirs(workspace, exist_ok=True)
    chunks = chunk_text(text, chunk_lines)
    out = []
    for c in chunks:
        path = os.path.join(workspace, f"chunk_{c['index']:03d}.md")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(c['text'])
        out.append({
            'index': c['index'],
            'start': c['start'],
            'end': c['end'],
            'lines': c['lines'],
            'oversized': c['oversized'],
            'path': path,
        })
    return out


def plan_chunks(text: str, chunk_lines: int, workspace: str) -> dict:
    """Compute + write chunks; return the chunk-plan dict."""
    chunks = write_chunk_files(text, chunk_lines, workspace)
    return {
        'chunk_lines': chunk_lines,
        'workspace': workspace,
        'n_chunks': len(chunks),
        'chunks': chunks,
    }


def chunk_references(ref_texts: list[tuple[str, str]], chunk_lines: int, workspace: str) -> list[dict]:
    """Chunk each reference into passages with stable ids. Return passage manifest.

    ref_texts: list of (name, content). Passage id = '<stem>#p<index>'.
    """
    manifest = []
    for name, content in ref_texts:
        passages = chunk_text(content, chunk_lines)
        stem = os.path.splitext(name)[0]
        for p in passages:
            pid = f'{stem}#p{p["index"]}'
            ppath = os.path.join(workspace, f'passage_{stem}_p{p["index"]:03d}.md')
            with open(ppath, 'w', encoding='utf-8') as f:
                f.write(p['text'])
            manifest.append({
                'id': pid,
                'ref': name,
                'start': p['start'],
                'end': p['end'],
                'lines': p['lines'],
                'path': ppath,
            })
    return manifest


def select_reference_passages(term: str, manifest: list[dict], top_k: int = 5) -> list[dict]:
    """Score passages by occurrence of ``term``; return up to top_k with score > 0.

    CJK terms use raw substring count; Latin terms use normalized forms.
    """
    scored = []
    cjk = bool(re.search(r'[一-鿿]', term))
    needle = term if cjk else normalize_target(term)
    if not needle:
        return []
    for m in manifest:
        try:
            with open(m['path'], 'r', encoding='utf-8') as f:
                txt = f.read()
        except OSError:
            continue
        hay = txt if cjk else normalize_target(txt)
        score = hay.count(needle)
        if score > 0:
            scored.append({'id': m['id'], 'score': score})
    scored.sort(key=lambda x: (-x['score'], x['id']))
    return scored[:top_k]


def validate_translator_payload(payload, expected_chunk: int, workspace: str) -> tuple[bool, list[str]]:
    """Light pre-check of a translator's {translated_markdown, self_audit} return.

    The structural correctness (heading/table/paragraph counts) is enforced by qa;
    this only checks shape, non-emptiness, and chunk identity.
    """
    errors = []
    if not isinstance(payload, dict):
        return False, ['payload is not a JSON object']
    md = payload.get('translated_markdown')
    if not isinstance(md, str) or not md.strip():
        errors.append('translated_markdown is missing or empty')
    try:
        md.encode('utf-8')
    except (UnicodeEncodeError, AttributeError):
        errors.append('translated_markdown is not valid UTF-8')
    sa = payload.get('self_audit')
    if not isinstance(sa, dict):
        errors.append('self_audit is missing or not an object')
    else:
        if sa.get('chunk') is not None and sa.get('chunk') != expected_chunk:
            errors.append(f"self_audit chunk mismatch: got {sa.get('chunk')}, expected {expected_chunk}")
    return (len(errors) == 0), errors


# ---------------------------------------------------------------------------
# Subcommand: prepare
# ---------------------------------------------------------------------------

def cmd_prepare(args) -> None:
    """Read source + references, chunk them, write workspace files, output sections."""
    cfg = load_config()
    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        die(f'File not found: {input_path}')

    chunk_lines = args.chunk_lines or cfg['chunk_lines']
    max_chunks = cfg['max_chunks']
    max_workspace_mb = cfg['max_workspace_mb']

    # Read source content
    ext = os.path.splitext(input_path)[1].lower()
    if ext in ('.md', '.markdown', '.txt'):
        with open(input_path, 'r', encoding='utf-8') as f:
            source_text = f.read()
    else:
        source_text = convert_to_markdown(input_path)

    # Load pre-made glossary if provided (flat seed)
    glossary = {}
    if args.glossary:
        for gpath in args.glossary:
            glossary.update(load_glossary(gpath))

    # Collect and load references
    reference_texts = []
    if args.references:
        ref_files = collect_reference_files(args.references)
        if not ref_files:
            print('Warning: no reference files found in specified paths', file=sys.stderr)
        for rpath in ref_files:
            name = os.path.basename(rpath)
            try:
                content = load_reference_content(rpath)
                reference_texts.append((name, content))
            except Exception as e:
                print(f'Warning: could not load reference {name}: {e}', file=sys.stderr)

    target_lang = args.language or cfg.get('default_target_language', 'zh')

    # Workspace + chunk plan (structure-safe)
    workspace = os.path.abspath(args.workspace) if args.workspace else os.path.join(
        os.path.dirname(input_path), '.translate-workspace'
    )
    chunk_plan = plan_chunks(source_text, chunk_lines, workspace)
    if chunk_plan['n_chunks'] > max_chunks:
        die(
            f'Chunk count {chunk_plan["n_chunks"]} exceeds max_chunks={max_chunks}. '
            f'Raise --chunk-lines (currently {chunk_lines}) or split the document.',
        )

    # Workspace size guard
    try:
        ws_size = sum(
            os.path.getsize(os.path.join(workspace, fn))
            for fn in os.listdir(workspace) if os.path.isfile(os.path.join(workspace, fn))
        )
        if ws_size > max_workspace_mb * 1024 * 1024:
            print(
                f'Warning: workspace size {ws_size // 1024}KB exceeds max_workspace_mb={max_workspace_mb}.',
                file=sys.stderr,
            )
    except OSError:
        pass

    # Reference passages + manifest
    passage_manifest = []
    if reference_texts:
        passage_manifest = chunk_references(reference_texts, chunk_lines, workspace)

    # ---- Output (legacy sections preserved; new sections appended) ----
    print(f'=== SOURCE TEXT ({os.path.basename(input_path)}) ===')
    print(source_text)

    if glossary:
        print('\n=== GLOSSARY (pre-made) ===')
        print(json.dumps(glossary, indent=2, ensure_ascii=False))

    if reference_texts:
        print('\n=== REFERENCES ===')
        for name, content in reference_texts:
            print(f'\n--- Reference: {name} ---')
            print(content)

    print('\n=== CHUNK PLAN ===')
    print(json.dumps(chunk_plan, ensure_ascii=False, indent=2))

    if passage_manifest:
        print('\n=== PASSAGE MANIFEST ===')
        print(json.dumps(passage_manifest, ensure_ascii=False, indent=2))

    print(f'\n=== INSTRUCTIONS ===')
    print(f'Translate the SOURCE TEXT to {target_lang}.')

    if reference_texts:
        print(
            'Analyze ALL REFERENCES to generate a comprehensive glossary of EVERY terminology '
            'mapping before translating. Extract ALL domain-specific terms, proper nouns, '
            'technical jargon, and recurring expressions. Err on the side of including more '
            'terms rather than fewer. Save the glossary to a JSON file, then translate using it '
            'for consistent terminology throughout.'
        )
        if args.glossary_output:
            print(f'Save the auto-generated glossary to: {args.glossary_output}')

    if glossary:
        print('Apply the pre-made GLOSSARY terms consistently throughout the translation.')

    print('Preserve all markdown structure (headings, lists, tables, links, code blocks).')
    print('Do NOT translate code, URLs, file paths, or variable names.')


# ---------------------------------------------------------------------------
# Subcommand: qa
# ---------------------------------------------------------------------------

def _read_chunk_translations(workspace: str, target_lang: str) -> dict[int, str]:
    """Read chunk_NNN.<lang>..md files -> {chunk_index: text}."""
    out = {}
    if not os.path.isdir(workspace):
        return out
    pat = re.compile(rf'^chunk_(\d+)\.{re.escape(target_lang)}\.md$')
    for fn in os.listdir(workspace):
        m = pat.match(fn)
        if m:
            try:
                with open(os.path.join(workspace, fn), 'r', encoding='utf-8') as f:
                    out[int(m.group(1))] = f.read()
            except OSError:
                pass
    return out


def _load_self_audits(audits_dir: str) -> dict[int, dict]:
    """Read self_audit_NNN.json files -> {chunk_index: audit}."""
    out = {}
    if not os.path.isdir(audits_dir):
        return out
    pat = re.compile(r'^self_audit_(\d+)\.json$')
    for fn in os.listdir(audits_dir):
        m = pat.match(fn)
        if m:
            try:
                with open(os.path.join(audits_dir, fn), 'r', encoding='utf-8') as f:
                    out[int(m.group(1))] = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
    return out


def cmd_qa(args) -> None:
    """Compare source and translation; enforce per-occurrence glossary application."""
    source_path = os.path.abspath(args.source)
    translation_path = os.path.abspath(args.translation)
    if not os.path.exists(source_path):
        die(f'Source file not found: {source_path}')
    if not os.path.exists(translation_path):
        die(f'Translation file not found: {translation_path}')

    with open(source_path, 'r', encoding='utf-8') as f:
        source_text = f.read()
    with open(translation_path, 'r', encoding='utf-8') as f:
        translation_text = f.read()

    _, source_body = extract_frontmatter(source_text)
    _, translation_body = extract_frontmatter(translation_text)

    source_protected, _ = protect_code_blocks(source_body)
    translation_protected, _ = protect_code_blocks(translation_body)

    errors: list[str] = []
    warnings: list[str] = []
    fix_map: list[dict] = []

    # 1. Heading counts
    src_headings = count_headings(source_protected)
    tgt_headings = count_headings(translation_protected)
    for level in sorted(set(list(src_headings.keys()) + list(tgt_headings.keys()))):
        sc, tc = src_headings.get(level, 0), tgt_headings.get(level, 0)
        if sc != tc:
            errors.append(f'H{level} heading count mismatch: source={sc}, translation={tc}')

    # 2. Paragraph count (within tolerance = warning; beyond = error)
    src_paras = count_paragraphs(source_protected)
    tgt_paras = count_paragraphs(translation_protected)
    delta = abs(src_paras - tgt_paras)
    tol = max(2, int(src_paras * 0.1))
    if delta > tol:
        errors.append(f'Paragraph count mismatch: source={src_paras}, translation={tgt_paras}')
    elif delta > 0:
        warnings.append(f'Paragraph count differs within tolerance: source={src_paras}, translation={tgt_paras}')

    # 3. Table count
    src_tables = count_tables(source_protected)
    tgt_tables = count_tables(translation_protected)
    if src_tables != tgt_tables:
        errors.append(f'Table count mismatch: source={src_tables}, translation={tgt_tables}')

    # 4. Untranslated fragments
    source_lang = _detect_language(source_body)
    target_lang = args.language or _detect_language(translation_body)
    fragments = detect_untranslated_fragments(translation_body, source_lang, target_lang)
    if fragments:
        errors.append(f'Possible untranslated fragments ({len(fragments)} found):')
        for frag in fragments[:10]:
            errors.append(f'  - "{frag}"')
        if len(fragments) > 10:
            errors.append(f'  ... and {len(fragments) - 10} more')

    # 5. Load glossary (explicit --glossary, else auto-discover prepared glossary)
    glossary_entries: list[dict] = []
    if args.glossary:
        for gpath in args.glossary:
            glossary_entries.extend(load_glossary_structured(gpath))
    else:
        auto_path = derive_glossary_path(source_path, target_lang)
        if os.path.exists(auto_path):
            glossary_entries = load_glossary_structured(auto_path)

    # Per-chunk translations (for per-occurrence attribution)
    workspace = os.path.abspath(args.workspace) if args.workspace else os.path.join(
        os.path.dirname(source_path), '.translate-workspace'
    )
    chunk_translations = _read_chunk_translations(workspace, target_lang)
    has_chunks = bool(chunk_translations)

    # 6. Grounded-term forced application (per-occurrence where chunks exist)
    for entry in glossary_entries:
        conf = entry.get('confidence')
        if conf == 'none':
            continue  # handled by consistency check below
        src = entry.get('source')
        tgt = entry.get('target')
        if not src or not tgt:
            continue
        occ_chunks = entry.get('source_chunks') or [
            o.get('chunk') for o in (entry.get('occurrences') or []) if isinstance(o, dict) and o.get('chunk')
        ]
        if not has_chunks or not occ_chunks:
            # No per-chunk attribution: whole-translation check (target present, source not residual).
            if not target_present(tgt, translation_body):
                errors.append(f'Glossary term not applied: "{src}" -> "{tgt}"')
                fix_map.append({'term': src, 'target': tgt, 'chunk': None, 'reason': 'target absent'})
            elif target_present(src, translation_body):
                errors.append(f'Glossary term left untranslated: "{src}"')
                fix_map.append({'term': src, 'target': tgt, 'chunk': None, 'reason': 'source residual'})
            continue
        # Per-occurrence: require target in each chunk where the source occurs.
        for ci in sorted(set(occ_chunks)):
            ctext = chunk_translations.get(ci)
            if ctext is None:
                ctext = translation_body  # fall back if a chunk file is missing
            if not target_present(tgt, ctext):
                errors.append(f'Glossary term "{src}" -> "{tgt}" not applied in chunk {ci}')
                fix_map.append({'term': src, 'target': tgt, 'chunk': ci, 'reason': 'target absent in chunk'})

    # 7. confidence:none consistency (if translator self-audits are present)
    audits = _load_self_audits(args.audits_dir or workspace)
    rendered_by_source: dict[str, list[tuple[int, str]]] = {}
    if audits:
        for ci, audit in audits.items():
            for app in audit.get('glossary_applied') or []:
                if app.get('confidence') == 'none':
                    src = app.get('source')
                    rendered = app.get('rendered')
                    if src and (not rendered or not str(rendered).strip()):
                        errors.append(f'confidence:none term "{src}" has empty rendered in chunk {ci}')
                        fix_map.append({'term': src, 'chunk': ci, 'reason': 'rendered empty'})
                    # source-not-residual in this chunk
                    if src and ci in chunk_translations and src in chunk_translations[ci]:
                        errors.append(f'confidence:none term "{src}" still untranslated in chunk {ci}')
                        fix_map.append({'term': src, 'chunk': ci, 'reason': 'source residual'})
                    if src and rendered:
                        rendered_by_source.setdefault(src, []).append((ci, str(rendered)))
        # Cross-chunk consistency: same source must render the same, else need context_note
        for src, renders in rendered_by_source.items():
            distinct = {r for _, r in renders}
            if len(distinct) > 1:
                warnings.append(
                    f'confidence:none term "{src}" rendered inconsistently across chunks: {sorted(distinct)} '
                    f'(acceptable only if glossary carries a context_note)'
                )

    # ---- Output ----
    print('=== QA Report ===')
    if errors:
        print(f'Errors: {len(errors)}')
        for e in errors:
            print(f'  [error] {e}')
    if warnings:
        print(f'Warnings: {len(warnings)}')
        for w in warnings:
            print(f'  [warning] {w}')
    if not errors and not warnings:
        print('All checks passed.')

    if fix_map:
        print('\n=== FIX MAP ===')
        print(json.dumps(fix_map, ensure_ascii=False, indent=2))

    status = 'fail' if errors else 'pass'
    print(f'\n=== SUMMARY ===')
    print(f'status: {status}')
    print(f'errors: {len(errors)}, warnings: {len(warnings)}')
    print(f'glossary_entries: {len(glossary_entries)}, per_chunk: {has_chunks}')

    if errors:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommand: write
# ---------------------------------------------------------------------------

def cmd_write(args) -> None:
    """Write translated file with proper naming and optional frontmatter."""
    input_path = os.path.abspath(args.input)
    translation_path = os.path.abspath(args.translation)
    target_lang = args.language

    if not os.path.exists(translation_path):
        die(f'Translation file not found: {translation_path}')

    with open(translation_path, 'r', encoding='utf-8') as f:
        content = f.read()

    output_path = derive_output_path(input_path, target_lang)

    if not args.no_frontmatter:
        now = datetime.datetime.now().isoformat(timespec='seconds')
        source_for_fm = input_path.replace('\\', '/').replace('"', '\\"')
        fm = (
            '---\n'
            f'source: "{source_for_fm}"\n'
            f'translated_at: "{now}"\n'
            f'translated_by: "translate"\n'
            f'target_language: "{target_lang}"\n'
            '---\n\n'
        )
        content = fm + content

    if os.path.exists(output_path):
        if args.overwrite:
            pass
        elif args.rename:
            stem, ext = os.path.splitext(output_path)
            ts = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
            output_path = f'{stem}-{ts}{ext}'
        else:
            die(
                f'Output file already exists: {output_path}\n'
                f'Re-run with --overwrite to replace or --rename to save as new file.'
            )

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)

    if not os.path.exists(output_path):
        die(f'Write failed: file not found at {output_path} after write')

    print(f'[OK] Written: {output_path}')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def show_version() -> None:
    print(f'translate v{VERSION}')


def main():
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument('--config', default='')
    _pre.add_argument('--version', action='store_true')
    _early, _ = _pre.parse_known_args()

    if _early.version:
        show_version()
        sys.exit(0)

    global CONFIG_PATH
    if _early.config:
        CONFIG_PATH = os.path.abspath(_early.config)

    load_config()

    parser = argparse.ArgumentParser(
        description='translate: translate files with optional reference-guided terminology.'
    )
    parser.add_argument('--config', default='', help='Path to config.json')
    parser.add_argument('--version', action='store_true', help='Show version')

    subparsers = parser.add_subparsers(dest='command', help='Subcommand')

    # --- prepare ---
    p_prepare = subparsers.add_parser('prepare', help='Prepare source and references for translation')
    p_prepare.add_argument('--input', required=True, help='Source file to translate')
    p_prepare.add_argument('--language', '-l', default='', help='Target language (e.g., zh, en)')
    p_prepare.add_argument('--glossary', nargs='*', help='Pre-made glossary files (JSON/CSV/TSV)')
    p_prepare.add_argument('--references', nargs='*', help='Reference files or directories')
    p_prepare.add_argument('--glossary-output', default='', help='Path to save auto-generated glossary')
    p_prepare.add_argument('--chunk-lines', type=int, default=None, help='Override config chunk_lines')
    p_prepare.add_argument('--workspace', default=None, help='Workspace dir for chunk/passage files')

    # --- qa ---
    p_qa = subparsers.add_parser('qa', help='Quality check translation against source')
    p_qa.add_argument('--source', required=True, help='Original source file')
    p_qa.add_argument('--translation', required=True, help='Translated file to check')
    p_qa.add_argument('--language', '-l', default='', help='Target language code')
    p_qa.add_argument('--glossary', nargs='*', help='Glossary files to check coverage (else auto-discover)')
    p_qa.add_argument('--workspace', default=None, help='Workspace with chunk_NNN.<lang>.md files')
    p_qa.add_argument('--audits-dir', default=None, help='Dir with self_audit_NNN.json files')

    # --- write ---
    p_write = subparsers.add_parser('write', help='Write translated file')
    p_write.add_argument('--input', required=True, help='Original source file (for naming)')
    p_write.add_argument('--translation', required=True, help='Translated content file')
    p_write.add_argument('--language', '-l', required=True, help='Target language code')
    p_write.add_argument('--no-frontmatter', action='store_true', help='Skip frontmatter injection')
    p_write.add_argument('--overwrite', action='store_true', help='Overwrite existing output')
    p_write.add_argument('--rename', action='store_true', help='Rename if output exists')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == 'prepare':
        cmd_prepare(args)
    elif args.command == 'qa':
        cmd_qa(args)
    elif args.command == 'write':
        cmd_write(args)


if __name__ == '__main__':
    main()
