#!/usr/bin/env python3
"""
cleanup_pipeline.py - markdown-cleanup pipeline.

Cleans formatting artifacts from markitdown-converted .md files.
Runs a series of fixers (removal/repair functions) in order.

Exit codes:
  0 - success
  1 - error (message on stderr)
"""
import sys
import os
import json
import argparse
import difflib
from typing import NoReturn

VERSION = '1.0.0'

# Import fixers from sibling module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixers import (
    FIXER_MAP, FIXERS,
    protect_code_blocks, restore_code_blocks,
    extract_frontmatter,
)


def die(msg: str) -> NoReturn:
    print(f'ERROR: {msg}', file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "fixers": {name: default for name, _, default in FIXERS},
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
    # Merge fixer defaults
    merged = dict(DEFAULT_CONFIG)
    if 'fixers' in cfg:
        merged['fixers'].update(cfg['fixers'])
    return merged


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def collect_md_files(path: str, recursive: bool) -> list[str]:
    """Collect .md files from a file or directory path."""
    if os.path.isfile(path):
        if not path.lower().endswith('.md'):
            die(f'Input file is not a markdown file: {path}')
        return [path]

    if not os.path.isdir(path):
        die(f'Path not found: {path}')

    files = []
    if recursive:
        for root, _dirs, filenames in os.walk(path):
            for fname in filenames:
                if fname.lower().endswith('.md'):
                    files.append(os.path.join(root, fname))
    else:
        for fname in os.listdir(path):
            fpath = os.path.join(path, fname)
            if os.path.isfile(fpath) and fname.lower().endswith('.md'):
                files.append(fpath)
    return sorted(files)


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def resolve_fixers(cfg: dict, only: list[str] | None, disable: list[str] | None) -> list[tuple]:
    """Resolve which fixers to run based on config and CLI overrides.

    Returns list of (name, function) tuples.
    """
    fixer_config = cfg.get('fixers', {})

    if only is not None:
        # Run ONLY the specified fixers
        for name in only:
            if name not in FIXER_MAP:
                die(f'Unknown fixer: {name}. Available: {", ".join(FIXER_MAP.keys())}')
        return [(name, FIXER_MAP[name][0]) for name in only]

    # Start with config defaults
    enabled = {}
    for name, (_, default) in FIXER_MAP.items():
        enabled[name] = fixer_config.get(name, default)

    # Apply --disable overrides
    if disable:
        for name in disable:
            if name not in FIXER_MAP:
                die(f'Unknown fixer: {name}. Available: {", ".join(FIXER_MAP.keys())}')
            enabled[name] = False

    return [(name, FIXER_MAP[name][0]) for name, on in enabled.items() if on]


def process_file(input_path: str, output_path: str, fixers_to_run: list[tuple],
                 dry_run: bool, show_diff: bool) -> dict:
    """Process a single .md file through the fixer pipeline.

    Returns dict with keys: input, output, changes_by_fixer, total_changes, diff.
    """
    # Read
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            original = f.read()
    except (OSError, UnicodeDecodeError) as e:
        die(f'Could not read {input_path}: {e}')

    text = original

    # Extract frontmatter
    frontmatter, text = extract_frontmatter(text)

    # Protect code blocks
    text, placeholders = protect_code_blocks(text)

    # Run fixers
    changes_by_fixer = {}
    for name, fn in fixers_to_run:
        text, count = fn(text)
        if count > 0:
            changes_by_fixer[name] = count

    # Restore code blocks
    text = restore_code_blocks(text, placeholders)

    # Re-attach frontmatter
    if frontmatter:
        text = frontmatter + '\n' + text

    total = sum(changes_by_fixer.values())

    # Compute diff if requested
    diff_lines = []
    if show_diff or dry_run:
        diff_lines = list(difflib.unified_diff(
            original.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=f'a/{os.path.basename(input_path)}',
            tofile=f'b/{os.path.basename(input_path)}',
        ))

    # Write (unless dry run)
    if not dry_run and total > 0:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text)

    return {
        'input': input_path,
        'output': output_path,
        'changes_by_fixer': changes_by_fixer,
        'total_changes': total,
        'diff': ''.join(diff_lines) if diff_lines else '',
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def list_fixers(cfg: dict) -> None:
    """Print available fixers and their enabled/disabled status."""
    fixer_config = cfg.get('fixers', {})
    print('Available fixers:')
    for name, (_, default) in FIXER_MAP.items():
        status = 'enabled' if fixer_config.get(name, default) else 'disabled'
        print(f'  {name}: {status} (default: {"enabled" if default else "disabled"})')


def show_version() -> None:
    """Print version and exit."""
    print(f'markdown-cleanup v{VERSION}')


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

    cfg = load_config()

    parser = argparse.ArgumentParser(
        description='markdown-cleanup: fix formatting artifacts in markitdown-converted .md files.'
    )
    parser.add_argument('--config', default='', help='Path to config.json')
    parser.add_argument('--version', action='store_true', help='Show version')
    parser.add_argument('--input', default='',
                        help='Path to .md file or directory to clean up')
    parser.add_argument('--output-path', default='',
                        help='Output path (default: write to same directory as source)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show changes without writing files')
    parser.add_argument('--diff', action='store_true',
                        help='Show unified diff of changes')
    parser.add_argument('--disable', default='',
                        help='Comma-separated fixer names to disable')
    parser.add_argument('--only', default='',
                        help='Run ONLY these fixers (comma-separated)')
    parser.add_argument('--no-recursive', action='store_true',
                        help='Only process top-level .md files (no subdirectories)')
    parser.add_argument('--list-fixers', action='store_true',
                        help='List available fixers and exit')
    args = parser.parse_args()

    if args.list_fixers:
        list_fixers(cfg)
        sys.exit(0)

    if not args.input:
        die('--input is required. Specify a .md file or directory path.')

    # Resolve fixers
    only = [n.strip() for n in args.only.split(',') if n.strip()] if args.only else None
    disable = [n.strip() for n in args.disable.split(',') if n.strip()] if args.disable else None
    fixers_to_run = resolve_fixers(cfg, only, disable)

    if not fixers_to_run:
        die('No fixers enabled. Use --only to specify fixers, or check config.')

    # Collect files
    recursive = not args.no_recursive
    files = collect_md_files(args.input, recursive)

    if not files:
        print('[CLEANUP] No .md files found')
        sys.exit(0)

    # Process each file
    total_processed = 0
    total_changed = 0

    for fpath in files:
        # Determine output path
        if args.output_path and len(files) == 1:
            out_path = args.output_path
        else:
            # Default: same directory as source
            out_path = fpath

        result = process_file(fpath, out_path, fixers_to_run, args.dry_run, args.diff)

        total_processed += 1
        if result['total_changes'] > 0:
            total_changed += 1

        # Print summary
        rel = os.path.basename(fpath)
        if result['total_changes'] == 0:
            print(f'[OK] {rel}: no changes needed')
        else:
            parts = ', '.join(
                f'{name}: {count}' for name, count in result['changes_by_fixer'].items()
            )
            action = 'would clean' if args.dry_run else 'cleaned'
            print(f'[OK] {rel}: {action} ({result["total_changes"]} changes — {parts})')

        if result['diff']:
            print(result['diff'])

    # Final summary
    if len(files) > 1:
        action = 'would clean' if args.dry_run else 'cleaned'
        print(f'[BATCH] {total_processed} files, {total_changed} {action}')


if __name__ == '__main__':
    main()
