#!/usr/bin/env python3
"""
translate_pipeline.py - Translation pipeline for the file-processing plugin.

Provides three subcommands:
  prepare  - Read source file and references, output structured text for Claude
  qa       - Compare source vs translation for structural completeness
  write    - Write translated file with proper naming

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

VERSION = '1.0.0'

# Import from sibling module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from glossary_utils import (
    die,
    collect_reference_files,
    load_reference_content,
    load_glossary,
    save_glossary,
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
        # Detect English sentence fragments (3+ consecutive words) in ZH output
        for m in re.finditer(r'[A-Za-z]+(?:\s+[A-Za-z]+){2,}', protected):
            fragment = m.group(0)
            if re.match(r'^https?://', fragment):
                continue
            if len(fragment) < 10:
                continue
            fragments.append(fragment)

    elif target_lang.lower() in ('en', 'english'):
        # Detect Chinese character runs in EN output
        for m in re.finditer(r'[\u4e00-\u9fff]{2,}', protected):
            fragments.append(m.group(0))

    return fragments


def _detect_language(text: str) -> str:
    """Rough language detection: CJK vs Latin."""
    cjk = len(re.findall(r'[\u4e00-\u9fff]', text))
    latin = len(re.findall(r'[A-Za-z]', text))
    return 'zh' if cjk > latin else 'en'


# ---------------------------------------------------------------------------
# Subcommand: prepare
# ---------------------------------------------------------------------------

def cmd_prepare(args) -> None:
    """Read source file and references, output structured text for Claude."""
    input_path = os.path.abspath(args.input)

    if not os.path.exists(input_path):
        die(f'File not found: {input_path}')

    # Read source content
    ext = os.path.splitext(input_path)[1].lower()
    if ext in ('.md', '.markdown', '.txt'):
        with open(input_path, 'r', encoding='utf-8') as f:
            source_text = f.read()
    else:
        source_text = convert_to_markdown(input_path)

    # Load pre-made glossary if provided
    glossary = {}
    if args.glossary:
        for gpath in args.glossary:
            glossary.update(load_glossary(gpath))

    # Collect and load reference files
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

    # Determine target language
    target_lang = args.language or 'zh'

    # Output structured sections
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

def cmd_qa(args) -> None:
    """Compare source and translation for structural completeness."""
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

    # Strip frontmatter for comparison
    _, source_body = extract_frontmatter(source_text)
    _, translation_body = extract_frontmatter(translation_text)

    # Protect code blocks
    source_protected, _ = protect_code_blocks(source_body)
    translation_protected, _ = protect_code_blocks(translation_body)

    issues = []

    # 1. Heading comparison
    src_headings = count_headings(source_protected)
    tgt_headings = count_headings(translation_protected)
    for level in sorted(set(list(src_headings.keys()) + list(tgt_headings.keys()))):
        src_count = src_headings.get(level, 0)
        tgt_count = tgt_headings.get(level, 0)
        if src_count != tgt_count:
            issues.append(f'H{level} heading count mismatch: source={src_count}, translation={tgt_count}')

    # 2. Paragraph count
    src_paras = count_paragraphs(source_protected)
    tgt_paras = count_paragraphs(translation_protected)
    if abs(src_paras - tgt_paras) > max(2, int(src_paras * 0.1)):
        issues.append(f'Paragraph count mismatch: source={src_paras}, translation={tgt_paras}')

    # 3. Table count
    src_tables = count_tables(source_protected)
    tgt_tables = count_tables(translation_protected)
    if src_tables != tgt_tables:
        issues.append(f'Table count mismatch: source={src_tables}, translation={tgt_tables}')

    # 4. Untranslated fragments
    source_lang = _detect_language(source_body)
    target_lang = args.language or _detect_language(translation_body)
    fragments = detect_untranslated_fragments(translation_body, source_lang, target_lang)
    if fragments:
        issues.append(f'Possible untranslated fragments ({len(fragments)} found):')
        for frag in fragments[:10]:
            issues.append(f'  - "{frag}"')
        if len(fragments) > 10:
            issues.append(f'  ... and {len(fragments) - 10} more')

    # 5. Glossary coverage
    if args.glossary:
        glossary = {}
        for gpath in args.glossary:
            glossary.update(load_glossary(gpath))
        uncovered = []
        for term in glossary:
            if re.search(r'[\u4e00-\u9fff]', term):
                # CJK term: use direct substring match (no word boundaries in CJK)
                if term in translation_body:
                    uncovered.append(term)
            else:
                # Latin term: use word boundaries to avoid partial matches
                if re.search(r'\b' + re.escape(term) + r'\b', translation_body):
                    uncovered.append(term)
        if uncovered:
            issues.append(f'Glossary terms not translated ({len(uncovered)} found):')
            for term in uncovered[:10]:
                issues.append(f'  - "{term}" (should be "{glossary[term]}")')
            if len(uncovered) > 10:
                issues.append(f'  ... and {len(uncovered) - 10} more')

    # Output report
    print('=== QA Report ===')
    if issues:
        print(f'Issues found: {len(issues)}')
        for issue in issues:
            print(f'  {issue}')
    else:
        print('All checks passed.')


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

    # Generate output path
    output_path = derive_output_path(input_path, target_lang)

    # Inject frontmatter unless disabled
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

    # Handle existing output
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

    # Write
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)

    # Verify
    if not os.path.exists(output_path):
        die(f'Write failed: file not found at {output_path} after write')

    print(f'[OK] Written: {output_path}')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def show_version() -> None:
    print(f'translate v{VERSION}')


def main():
    # Pre-scan for --config and --version
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

    # Load config (ensures config.json exists, applies --config override)
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

    # --- qa ---
    p_qa = subparsers.add_parser('qa', help='Quality check translation against source')
    p_qa.add_argument('--source', required=True, help='Original source file')
    p_qa.add_argument('--translation', required=True, help='Translated file to check')
    p_qa.add_argument('--language', '-l', default='', help='Target language code')
    p_qa.add_argument('--glossary', nargs='*', help='Glossary files to check coverage')

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
