#!/usr/bin/env python3
"""
review_plan.py - Deterministic matrix planner for the content-review skill.

Turns a document into a review matrix (chunks x dimensions) and, separately,
assembles sub-agent cell results into a deduped report + diff. Both halves are
pure Python so the orchestration guarantees (cell count, coverage table, FAILED
convergence, dedup, diff) are testable without sub-agents.

Subcommands:
  plan      - Read file, compute structure-safe chunks, choose dimensions from
              --focus / --references, write chunk files to a workspace, print the
              matrix plan as JSON. --dry-run prints the plan without writing.
  assemble  - Given a plan JSON + a directory of per-cell result JSON files,
              validate each cell, build the coverage table, dedupe findings,
              fill the report, and emit a unified diff for fixable findings.
              Applies the FAILED convergence policy (required cell FAILED ->
              report is "incomplete", no diff unless --accept-partial).

Exit codes:
  0 - success
  1 - error (message on stderr)
  2 - caps exceeded (raise --chunk-lines, or re-run with --accept-partial)
"""
import sys
import os
import json
import re
import argparse
import datetime
import difflib
from typing import NoReturn

VERSION = '2.0.0'

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

DEFAULT_CONFIG = {
    "chunk_lines": 400,
    "max_chunks": 20,
    "max_cells": 60,
}

# Dimension sets. "all" (default) reviews grammar-style + logic-consistency;
# --references adds fact-check. A single --focus selects one dimension.
DIMENSION_ALL = ['grammar-style', 'logic-consistency']
DIMENSION_WITH_REFS = ['grammar-style', 'logic-consistency', 'fact-check']
FOCUS_TO_DIMENSION = {
    'grammar': 'grammar-style',
    'style': 'grammar-style',
    'logic': 'logic-consistency',
    'consistency': 'logic-consistency',
}

# Required dimensions per the FAILED convergence policy. If any required cell
# fails, the report may only be marked incomplete.
REQUIRED_DIMENSIONS = {'grammar-style', 'logic-consistency', 'fact-check'}

SEVERITY_RANK = {'high': 0, 'medium': 1, 'low': 2}

# Finding category -> report section.
CATEGORY_TO_SECTION = {
    'spelling': 'Grammar & Spelling',
    'grammar': 'Grammar & Spelling',
    'style': 'Style',
    'logic': 'Logic & Consistency',
    'consistency': 'Logic & Consistency',
    'fact': 'Cross-Reference Issues',
}

FENCE_RE = re.compile(r'^\s*(```|~~~)')


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> NoReturn:
    print(f'ERROR: {msg}', file=sys.stderr)
    sys.exit(code)


def load_config() -> dict:
    """Load config.json, create with defaults if missing, merge partials."""
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
# markitdown fallback for non-text inputs
# ---------------------------------------------------------------------------

def _ensure_markitdown():
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
    MarkItDown = _ensure_markitdown()
    md = MarkItDown()
    result = md.convert(file_path)
    return result.text_content


def read_source(path: str) -> str:
    if not os.path.exists(path):
        die(f'File not found: {path}')
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.md', '.markdown', '.txt'):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    return convert_to_markdown(path)


# ---------------------------------------------------------------------------
# Structure-safe chunking
# ---------------------------------------------------------------------------

def _is_fence_open(line: str) -> bool:
    return bool(FENCE_RE.match(line))


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith('|')


def parse_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Split text into atomic blocks: fenced code, tables, or prose/blank runs.

    Fenced code blocks and tables are NEVER split (they are the structures that
    chunk boundaries must respect). Prose is broken at blank lines for finer
    granularity. Every input line ends up in exactly one block, so concatenating
    blocks reproduces the original text.
    """
    lines = text.split('\n')
    blocks: list[tuple[str, list[str]]] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if _is_fence_open(line):
            fence = FENCE_RE.match(line).group(1)
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
            while i < n and lines[i].strip() != '' and not _is_fence_open(lines[i]) and not _is_table_row(lines[i]):
                buf.append(lines[i])
                i += 1
            blocks.append(('prose', buf))
    return blocks


def pack_blocks(blocks: list[tuple[str, list[str]]], chunk_lines: int) -> list[list[tuple[str, list[str]]]]:
    """Greedily pack atomic blocks into chunks of at most ~chunk_lines lines.

    A single block larger than chunk_lines becomes its own (oversized) chunk
    rather than being split. Fences and tables are therefore always intact, and
    every chunk has balanced fences.
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
    """Return chunks: [{index, start, end, lines, text, oversized}]."""
    blocks = parse_blocks(text)
    packed = pack_blocks(blocks, chunk_lines)
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
            # All fences intact by construction; record the invariant.
            'fence_balance': 'balanced',
        })
        lineno = end + 1
    return out


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

def compute_dimensions(focus: str, has_references: bool) -> list[str]:
    if focus and focus.lower() != 'all':
        if focus.lower() not in FOCUS_TO_DIMENSION:
            die(f'Unknown --focus: {focus}. Use grammar|style|logic|consistency|all.')
        return [FOCUS_TO_DIMENSION[focus.lower()]]
    return DIMENSION_WITH_REFS if has_references else DIMENSION_ALL


# ---------------------------------------------------------------------------
# Subcommand: plan
# ---------------------------------------------------------------------------

def cmd_plan(args) -> None:
    cfg = load_config()
    chunk_lines = args.chunk_lines or cfg['chunk_lines']
    max_chunks = cfg['max_chunks']
    max_cells = cfg['max_cells']

    input_path = os.path.abspath(args.input)
    text = read_source(input_path)
    total_lines = text.count('\n') + (1 if text and not text.endswith('\n') else 0)

    chunks = chunk_text(text, chunk_lines)
    n_chunks = len(chunks)

    # Resolve references (paths only; content is read by the fact-check agents).
    references = []
    if args.references:
        for r in args.references:
            rp = os.path.abspath(r)
            references.append(rp)

    dimensions = compute_dimensions(args.focus, has_references=bool(references))
    n_dims = len(dimensions)

    # Caps. Exit code 2 = caps exceeded (raise chunk_lines or accept partial).
    if n_chunks > max_chunks:
        die(
            f'Chunk count {n_chunks} exceeds max_chunks={max_chunks}. '
            f'Raise --chunk-lines (currently {chunk_lines}) or re-run with --accept-partial.',
            code=2,
        )
    n_cells = n_chunks * n_dims
    if n_cells > max_cells:
        die(
            f'Cell count {n_cells} (={n_chunks} chunks x {n_dims} dimensions) exceeds '
            f'max_cells={max_cells}. Raise --chunk-lines or reduce --focus, or re-run with --accept-partial.',
            code=2,
        )

    workspace = os.path.abspath(args.workspace) if args.workspace else os.path.join(
        os.path.dirname(input_path), '.review-workspace'
    )

    cells = []
    for dim in dimensions:
        for ch in chunks:
            cells.append({
                'dimension': dim,
                'chunk': ch['index'],
                'lines': f"{ch['start']}-{ch['end']}",
                'chunk_path': os.path.join(workspace, f"chunk_{ch['index']:03d}.md"),
            })

    plan = {
        'version': VERSION,
        'file': input_path.replace('\\', '/'),
        'total_lines': total_lines,
        'workspace': workspace.replace('\\', '/'),
        'chunk_lines': chunk_lines,
        'has_references': bool(references),
        'references': [r.replace('\\', '/') for r in references],
        'dimensions': dimensions,
        'chunks': [
            {
                'index': c['index'],
                'start': c['start'],
                'end': c['end'],
                'lines': c['lines'],
                'oversized': c['oversized'],
                'fence_balance': c['fence_balance'],
                'path': os.path.join(workspace, f"chunk_{c['index']:03d}.md"),
            }
            for c in chunks
        ],
        'n_chunks': n_chunks,
        'n_dimensions': n_dims,
        'n_cells': len(cells),
        'max_chunks': max_chunks,
        'max_cells': max_cells,
        'cells': cells,
    }

    if args.dry_run:
        plan['dry_run'] = True
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return

    os.makedirs(workspace, exist_ok=True)
    for c in chunks:
        cpath = os.path.join(workspace, f"chunk_{c['index']:03d}.md")
        with open(cpath, 'w', encoding='utf-8') as f:
            f.write(c['text'])
        if c['oversized']:
            print(
                f'Warning: chunk {c["index"]} has {c["lines"]} lines > chunk_lines={chunk_lines} '
                f'(unsplittable block); kept intact.',
                file=sys.stderr,
            )

    if args.plan_output:
        with open(args.plan_output, 'w', encoding='utf-8') as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
    print(json.dumps(plan, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Subcommand: assemble
# ---------------------------------------------------------------------------

def _load_cell_results(cells_dir: str, cells: list[dict]) -> list[dict]:
    """Read one result file per cell. Missing/invalid -> FAILED marker."""
    results = []
    for cell in cells:
        dim = cell['dimension']
        ci = cell['chunk']
        fname = f"{dim}__{ci:03d}.json"
        path = os.path.join(cells_dir, fname)
        entry = {'cell': cell, 'status': 'ok', 'findings': [], 'note': ''}
        if not os.path.exists(path):
            entry['status'] = 'FAILED'
            entry['note'] = f'missing result file {fname}'
            results.append(entry)
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            entry['status'] = 'FAILED'
            entry['note'] = f'unparseable result: {e}'
            results.append(entry)
            continue
        # Identity check: the returned cell must match.
        rc = data.get('cell', {})
        if rc.get('dimension') != dim or rc.get('chunk') != ci:
            entry['status'] = 'FAILED'
            entry['note'] = f'cell identity mismatch (got {rc})'
            results.append(entry)
            continue
        findings = data.get('findings', [])
        if not isinstance(findings, list):
            entry['status'] = 'FAILED'
            entry['note'] = 'findings is not a list'
            results.append(entry)
            continue
        entry['findings'] = findings
        entry['checked_thoroughly'] = bool(data.get('checked_thoroughly', False))
        if not findings:
            entry['status'] = 'empty'
        results.append(entry)
    return results


def _dedupe(findings: list[dict]) -> list[dict]:
    """Merge findings with same (line, quote, category)."""
    seen = {}
    for f in findings:
        key = (f.get('line'), (f.get('quote') or '').strip(), f.get('category'))
        if key in seen:
            continue
        seen[key] = f
    return list(seen.values())


def _section_tables(findings: list[dict], has_refs: bool) -> dict:
    """Group findings by report section -> list of (finding, n)."""
    sections = {}
    for f in findings:
        cat = f.get('category', 'style')
        section = CATEGORY_TO_SECTION.get(cat, 'Style')
        sections.setdefault(section, []).append(f)
    return sections


def _build_report(plan: dict, results: list[dict], deduped: list[dict], incomplete: bool) -> str:
    has_refs = plan.get('has_references', False)
    fname = os.path.basename(plan['file'])
    lines = []
    lines.append(f"# Content Review: {fname}")
    if has_refs:
        refs = ', '.join(os.path.basename(r) for r in plan.get('references', []))
        lines.append(f"## Verification against: {refs}")
    lines.append('')

    # Summary
    n_failed = sum(1 for r in results if r['status'] == 'FAILED')
    n_empty = sum(1 for r in results if r['status'] == 'empty')
    n_ok = sum(1 for r in results if r['status'] == 'ok')
    by_cat = {}
    for f in deduped:
        by_cat[f.get('category', 'style')] = by_cat.get(f.get('category', 'style'), 0) + 1
    lines.append('## Summary')
    quality = 'Needs attention'
    if incomplete:
        quality = 'INCOMPLETE (required cell FAILED)'
    elif not deduped:
        quality = 'Good'
    lines.append(f"- **Overall quality:** {quality}")
    cat_summary = ', '.join(f"{c}={n}" for c, n in sorted(by_cat.items())) or 'none'
    lines.append(f"- **Issues found (deduped):** {cat_summary}")
    lines.append(
        f"- **Coverage:** {n_ok} ok, {n_empty} empty (legitimate), {n_failed} FAILED "
        f"of {len(results)} cells"
    )
    lines.append('')

    sections = _section_tables(deduped, has_refs)
    order = ['Grammar & Spelling', 'Style', 'Logic & Consistency']
    if has_refs:
        order.append('Cross-Reference Issues')
    for section in order:
        items = sections.get(section, [])
        if not items:
            continue
        items.sort(key=lambda f: (f.get('line') or 0, SEVERITY_RANK.get(f.get('severity', 'medium'), 1)))
        lines.append(f"## {section}")
        if section == 'Cross-Reference Issues':
            lines.append('| # | Location | Issue | Reference Source |')
            lines.append('|---|----------|-------|------------------|')
            for n, f in enumerate(items, 1):
                loc = f"Line {f.get('line', '?')}"
                lines.append(f"| {n} | {loc} | {f.get('issue', '')} | {f.get('reference_source', f.get('suggestion', ''))} |")
        else:
            lines.append('| # | Location | Issue | Suggestion |')
            lines.append('|---|----------|-------|------------|')
            for n, f in enumerate(items, 1):
                loc = f"Line {f.get('line', '?')}"
                lines.append(f"| {n} | {loc} | {f.get('issue', '')} | {f.get('suggestion', '')} |")
        lines.append('')

    # Coverage table (always; surfaces FAILED cells).
    lines.append('## Coverage Table')
    lines.append('| dimension | chunk | lines | status | findings |')
    lines.append('|-----------|-------|-------|--------|----------|')
    for r in results:
        c = r['cell']
        lines.append(
            f"| {c['dimension']} | {c['chunk']} | {c['lines']} | {r['status']} | {len(r['findings'])} |"
        )
    lines.append('')

    if incomplete:
        lines.append(
            '> **INCOMPLETE:** one or more required cells FAILED. No diff generated. '
            'Re-dispatch the FAILED cells or pass --accept-partial to emit a partial diff.'
        )
        lines.append('')
    return '\n'.join(lines)


def _build_diff(plan: dict, deduped: list[dict]) -> str:
    """Unified diff for fixable findings (quote -> suggestion at line)."""
    file_path = plan['file']
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            src_lines = f.read().split('\n')
    except OSError:
        return ''
    fixable = [f for f in deduped if f.get('fixable') and f.get('quote') and f.get('suggestion')]
    if not fixable:
        return ''
    # Apply substitutions to a copy (1-indexed line numbers).
    new_lines = list(src_lines)
    edits = []
    for f in fixable:
        ln = f.get('line')
        if not isinstance(ln, int) or ln < 1 or ln > len(new_lines):
            continue
        original = new_lines[ln - 1]
        if f.get('quote') in original:
            new_lines[ln - 1] = original.replace(f['quote'], f['suggestion'], 1)
            edits.append((ln, original, new_lines[ln - 1]))
    if not edits:
        return ''
    fname = os.path.basename(file_path)
    diff = difflib.unified_diff(src_lines, new_lines, fromfile=f'a/{fname}', tofile=f'b/{fname}', lineterm='')
    return '\n'.join(diff)


def cmd_assemble(args) -> None:
    with open(args.plan, 'r', encoding='utf-8') as f:
        plan = json.load(f)
    cells = plan['cells']
    results = _load_cell_results(args.cells_dir, cells)

    # FAILED convergence policy.
    failed_required = [
        r for r in results
        if r['status'] == 'FAILED' and r['cell']['dimension'] in REQUIRED_DIMENSIONS
    ]
    incomplete = bool(failed_required) and not args.accept_partial

    all_findings = []
    for r in results:
        all_findings.extend(r['findings'])
    deduped = _dedupe(all_findings)
    deduped.sort(key=lambda f: (f.get('line') or 0, SEVERITY_RANK.get(f.get('severity', 'medium'), 1)))

    report = _build_report(plan, results, deduped, incomplete)
    diff = '' if incomplete else _build_diff(plan, deduped)

    out = {'report': report}
    if diff:
        out['diff'] = diff
    if failed_required:
        out['failed_required_cells'] = [
            {'dimension': r['cell']['dimension'], 'chunk': r['cell']['chunk'], 'note': r['note']}
            for r in failed_required
        ]
    out['incomplete'] = incomplete
    out['deduped_count'] = len(deduped)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(report)
            if diff:
                f.write('\n\n## Suggested Fixes (diff)\n\n```diff\n' + diff + '\n```\n')
        print(f'[OK] Report written: {args.output}')
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def show_version() -> None:
    print(f'content-review review_plan v{VERSION}')


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
        description='content-review matrix planner + assembler (deterministic, testable).'
    )
    parser.add_argument('--config', default='', help='Path to config.json')
    parser.add_argument('--version', action='store_true', help='Show version')
    sub = parser.add_subparsers(dest='command', help='Subcommand')

    # plan
    p_plan = sub.add_parser('plan', help='Compute the review matrix and write chunk files.')
    p_plan.add_argument('--input', required=True, help='File to review (.md/.txt direct; others via markitdown)')
    p_plan.add_argument('--focus', default='all', help='grammar|style|logic|consistency|all (default all)')
    p_plan.add_argument('--references', nargs='*', default=None, help='Reference files (adds fact-check dimension)')
    p_plan.add_argument('--chunk-lines', type=int, default=None, help='Override config chunk_lines')
    p_plan.add_argument('--workspace', default=None, help='Workspace dir for chunk files')
    p_plan.add_argument('--plan-output', default=None, help='Also write plan JSON to this path')
    p_plan.add_argument('--dry-run', action='store_true', help='Print plan without writing chunk files')
    p_plan.add_argument('--accept-partial', action='store_true', help='Allow exceeding caps (marks plan)')

    # assemble
    p_asm = sub.add_parser('assemble', help='Assemble per-cell results into report + diff.')
    p_asm.add_argument('--plan', required=True, help='Path to the plan JSON from `plan`')
    p_asm.add_argument('--cells-dir', required=True, help='Directory of per-cell result JSON files')
    p_asm.add_argument('--output', default=None, help='Write report+diff to this file (else JSON to stdout)')
    p_asm.add_argument('--accept-partial', action='store_true', help='Emit diff even if required cells FAILED')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == 'plan':
        cmd_plan(args)
    elif args.command == 'assemble':
        cmd_assemble(args)


if __name__ == '__main__':
    main()
