#!/usr/bin/env python3
"""
translate_pipeline.py - Translation pipeline for the file-processing plugin.

prepare writes bounded chunk/passage inputs and a compact manifest handoff.
assemble validates expected run-bound translation or semantic-QA partials and
writes the existing full-stage publication payload. publish-stage, qa, resume
and write retain schema-3.0 state/QA gates. write defaults to bilingual JSON;
Markdown and a delivered glossary are explicit options.

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
import tempfile
from typing import NoReturn

VERSION = '4.0.0'

# Import from sibling module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from glossary_utils import (
    die,
    collect_reference_files,
    load_reference_content,
    load_glossary,
    load_glossary_structured,
    load_glossary_v3,
    partition_relevance_batches,
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
from v3_runtime import (
    SCHEMA_VERSION,
    atomic_write_json,
    atomic_write_text,
    build_occurrence_ledger,
    fingerprint,
    invalidate_downstream,
    load_manifest,
    manifest_digest,
    make_manifest,
    new_run_id,
    publish_cache_json,
    runtime_fingerprint,
    sha256_file,
    sha256_text,
    stage_input_hash,
    strict_ready,
    update_stage,
    validate_occurrence_ledger,
    validate_translation_occurrences,
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
    "v3_schema_version": SCHEMA_VERSION,
    "agent_timeout_seconds": 120,
    "agent_retry_limit": 2,
    "reference_batch_size": 20,
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
            'sha256': sha256_file(path),
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
    stem_counts: dict[str, int] = {}
    for name, _content in ref_texts:
        stem = os.path.splitext(name)[0]
        stem_counts[stem] = stem_counts.get(stem, 0) + 1
    stem_seen: dict[str, int] = {}
    for name, content in ref_texts:
        passages = chunk_text(content, chunk_lines)
        stem = os.path.splitext(name)[0]
        stem_seen[stem] = stem_seen.get(stem, 0) + 1
        passage_stem = stem if stem_counts[stem] == 1 else f'{stem}-r{stem_seen[stem]}'
        for p in passages:
            pid = f'{passage_stem}#p{p["index"]}'
            ppath = os.path.join(workspace, f'passage_{passage_stem}_p{p["index"]:03d}.md')
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
    """Return a deterministic lexical baseline with source diversity.

    Semantic matching is agent-owned in v3. This stdlib-only layer supplies
    exact/normalised matching plus a small title boost and ensures that one
    high-frequency reference cannot consume every grounding slot.
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
        title = m.get('ref', '') if isinstance(m, dict) else ''
        normalized_title = title if cjk else normalize_target(title)
        if needle and needle in normalized_title:
            score += 1
        if score > 0:
            scored.append({'id': m['id'], 'score': score, '_ref': title})
    scored.sort(key=lambda x: (-x['score'], x['id']))
    selected = []
    seen_refs = set()
    for item in scored:
        if item['_ref'] not in seen_refs:
            selected.append(item)
            seen_refs.add(item['_ref'])
    for item in scored:
        if len(selected) >= top_k:
            break
        if item not in selected:
            selected.append(item)
    return [{'id': item['id'], 'score': item['score']} for item in selected[:top_k]]


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
# v3 run / manifest helpers
# ---------------------------------------------------------------------------

EXIT_ERROR = 1
EXIT_RECOVERABLE = 2
EXIT_REPORT_ONLY = 3
EXIT_RUNTIME_UNAVAILABLE = 4


def _read_source_text(input_path: str) -> str:
    ext = os.path.splitext(input_path)[1].lower()
    if ext in ('.md', '.markdown', '.txt'):
        with open(input_path, 'r', encoding='utf-8') as f:
            return f.read()
    return convert_to_markdown(input_path)


def _manifest_path(workspace: str) -> str:
    return os.path.join(workspace, 'run_manifest.json')


def _save_manifest(path: str, manifest: dict) -> str:
    digest = manifest_digest(manifest)
    manifest['manifest_sha256'] = digest
    atomic_write_json(path, manifest)
    return digest


def _manifest_artifact_path(manifest: dict, key: str) -> str | None:
    value = manifest.get('artifacts', {}).get(key)
    if isinstance(value, dict):
        return value.get('path')
    return None


def _add_artifact(manifest: dict, name: str, path: str, stage: str) -> dict:
    record = {'path': os.path.abspath(path), 'sha256': sha256_file(path)}
    update_stage(manifest, stage, manifest['stages'][stage]['state'], artifacts={name: record})
    return record


def _fail_runtime_if_needed(args) -> None:
    if args.quality_mode == 'strict' and args.runtime_mode != 'orchestrated':
        print('ERROR: strict v3 translation requires an orchestrated agent runtime. '
              'Use --quality-mode report-only for an INCOMPLETE diagnostic run.', file=sys.stderr)
        sys.exit(EXIT_RUNTIME_UNAVAILABLE)


def _load_v3_manifest_or_die(path: str) -> dict:
    try:
        manifest = load_manifest(path)
        if runtime_fingerprint(manifest.get('runtime_mode', 'unavailable')) != manifest.get('runtime_fingerprint'):
            raise ValueError('translation runtime changed; re-run prepare (old runs are preserved)')
        return manifest
    except (OSError, ValueError, json.JSONDecodeError) as e:
        die(f'Invalid v3 manifest {path}: {e}')


def _quality_mode_from_manifest(manifest: dict) -> str:
    return manifest.get('quality_mode', 'strict')


def _read_json(path: str) -> object:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _manifest_json_artifact(manifest: dict, name: str) -> object:
    path = _manifest_artifact_path(manifest, name)
    if not path or not os.path.isfile(path):
        raise ValueError(f'missing manifest artifact: {name}')
    if sha256_file(path) != manifest['artifacts'][name].get('sha256'):
        raise ValueError(f'hash mismatch for manifest artifact: {name}')
    return _read_json(path)


def _chunk_indexes(manifest: dict) -> list[int]:
    plan = _manifest_json_artifact(manifest, 'chunk_plan')
    if not isinstance(plan, dict) or not isinstance(plan.get('chunks'), list):
        raise ValueError('invalid manifest chunk plan')
    return [int(chunk['index']) for chunk in plan['chunks'] if isinstance(chunk, dict) and 'index' in chunk]


def _source_chunks(manifest: dict) -> list[dict]:
    plan = _manifest_json_artifact(manifest, 'chunk_plan')
    if not isinstance(plan, dict) or not isinstance(plan.get('chunks'), list):
        raise ValueError('invalid manifest chunk plan')
    return plan['chunks']


def _stage_payload(manifest: dict, stage: str, artifact: str) -> dict:
    record = manifest.get('stages', {}).get(stage, {}).get('artifacts', {}).get(artifact)
    if not isinstance(record, dict) or not record.get('path'):
        raise ValueError(f'missing {stage}/{artifact} artifact')
    if sha256_file(record['path']) != record.get('sha256'):
        raise ValueError(f'hash mismatch for {stage}/{artifact}')
    value = _read_json(record['path'])
    if not isinstance(value, dict):
        raise ValueError(f'{stage}/{artifact} must be a JSON object')
    return value


def _validate_reference_memory_payload(manifest: dict, payload: dict) -> list[str]:
    errors: list[str] = []
    planned = _manifest_json_artifact(manifest, 'passage_manifest')
    if not isinstance(planned, list):
        return ['passage manifest is not a list']
    expected_ids = {str(item.get('id')) for item in planned if isinstance(item, dict)}
    passages = payload.get('passages')
    if not isinstance(passages, list):
        return ['reference_memory requires a passages list']
    seen: set[str] = set()
    for entry in passages:
        if not isinstance(entry, dict):
            errors.append('reference_memory passage is not an object')
            continue
        pid = str(entry.get('id') or '')
        if not pid or pid in seen:
            errors.append(f'duplicate or missing reference passage id: {pid!r}')
        seen.add(pid)
        if entry.get('status') != 'completed':
            errors.append(f'reference passage {pid} is not completed')
        if not isinstance(entry.get('evidence', []), list):
            errors.append(f'reference passage {pid} evidence must be a list')
    if seen != expected_ids:
        errors.append('reference_memory passages do not exactly match the passage manifest')
    semantic = payload.get('semantic_retrieval')
    if not isinstance(semantic, dict):
        errors.append('reference_memory requires semantic_retrieval metadata')
    elif expected_ids:
        if semantic.get('status') != 'completed' or not semantic.get('provider'):
            errors.append('semantic retrieval must be completed with provider provenance')
        if not isinstance(semantic.get('results'), list):
            errors.append('semantic retrieval results must be a list')
    elif semantic.get('status') != 'not_applicable' or not semantic.get('reason'):
        errors.append('reference-free run must explicitly mark semantic retrieval not_applicable with a reason')
    for field in ('terms', 'expressions', 'style_rules'):
        if not isinstance(payload.get(field, []), list):
            errors.append(f'reference_memory {field} must be a list')
    return errors


def _validate_source_matching_payload(manifest: dict, payload: dict) -> list[str]:
    errors: list[str] = []
    scan = payload.get('scan_chunks')
    expected_chunks = set(_chunk_indexes(manifest))
    scanned = set()
    if not isinstance(scan, list):
        errors.append('occurrence_ledger requires scan_chunks')
    else:
        for item in scan:
            if not isinstance(item, dict) or item.get('status') != 'completed':
                errors.append('every source scan chunk must be completed')
                continue
            try:
                scanned.add(int(item['chunk']))
            except (KeyError, TypeError, ValueError):
                errors.append('source scan chunk is missing an integer chunk id')
        if scanned != expected_chunks:
            errors.append('source scan does not acknowledge every planned chunk')

    candidates = payload.get('source_candidates')
    if not isinstance(candidates, list):
        return errors + ['occurrence_ledger requires source_candidates']
    if not candidates:
        reasons = payload.get('empty_scan_reasons')
        if not isinstance(reasons, dict) or {str(k) for k in reasons} != {str(x) for x in expected_chunks}:
            errors.append('empty source scan requires an explicit reason for every chunk')

    source_text = _read_source_text(manifest['source']['path'])
    expected = build_occurrence_ledger(source_text, _source_chunks(manifest), candidates)
    occurrences = payload.get('occurrences')
    if not isinstance(occurrences, list):
        return errors + ['occurrence_ledger requires an occurrences list']
    expected_by_id = {item['occurrence_id']: item for item in expected}
    actual_by_id = {item.get('occurrence_id'): item for item in occurrences if isinstance(item, dict)}
    if set(actual_by_id) != set(expected_by_id) or len(actual_by_id) != len(occurrences):
        errors.append('occurrence ids do not exactly match deterministic source candidates')
    for oid, expected_item in expected_by_id.items():
        actual = actual_by_id.get(oid)
        if not actual:
            continue
        for field in ('term_id', 'source', 'source_hash', 'source_offset', 'source_length',
                      'source_line', 'chunk', 'chunk_line', 'key_item'):
            if actual.get(field) != expected_item.get(field):
                errors.append(f'occurrence {oid} has stale or invalid {field}')
        if actual.get('origin') == 'reference' and actual.get('disposition') == 'applied' and not actual.get('evidence'):
            errors.append(f'occurrence {oid} is applied without reference evidence')
        if actual.get('disposition') == 'not_applicable':
            applicability = actual.get('applicability')
            if not isinstance(applicability, dict) or not applicability.get('source_span_mismatch'):
                errors.append(f'occurrence {oid} not_applicable lacks source-span mismatch evidence')
    errors.extend(validate_occurrence_ledger(occurrences))
    batches = payload.get('relevance_batches')
    if not isinstance(batches, list) or any(not isinstance(batch, list) for batch in batches):
        errors.append('occurrence_ledger requires deterministic relevance_batches')
    else:
        expected_batches = partition_relevance_batches(
            occurrences, int(manifest.get('config', {}).get('max_terms_per_chunk_prompt', 120))
        )
        expected_ids = [[item['occurrence_id'] for item in batch] for batch in expected_batches]
        actual_ids = [[str(value) for value in batch] for batch in batches]
        if actual_ids != expected_ids:
            errors.append('relevance_batches are incomplete, duplicated, or non-deterministic')
    return errors


def _validate_translation_payload(manifest: dict, payload: dict, ledger: list[dict] | None = None) -> list[str]:
    errors: list[str] = []
    try:
        prepared_chunks(manifest)
    except (OSError, ValueError, KeyError) as e:
        return [f'invalid prepared chunks: {e}']
    chunks = payload.get('chunks')
    if not isinstance(chunks, list):
        return ['translation artifact requires chunks list']
    expected_chunks = set(_chunk_indexes(manifest))
    by_chunk = {}
    ids: list[str] = []
    for item in chunks:
        if not isinstance(item, dict):
            errors.append('translation chunk is not an object')
            continue
        try:
            if type(item['chunk']) is not int:
                raise ValueError('chunk id must be an integer')
            chunk = item['chunk']
        except (KeyError, TypeError, ValueError):
            errors.append('translation chunk is missing an integer chunk id')
            continue
        if chunk in by_chunk:
            errors.append(f'duplicate translation chunk: {chunk}')
        by_chunk[chunk] = item
        if not isinstance(item.get('translated_markdown'), str) or not item['translated_markdown'].strip():
            errors.append(f'translation chunk {chunk} is empty')
        if not isinstance(item.get('occurrence_ids'), list) or any(not isinstance(x, str) for x in item.get('occurrence_ids', [])):
            errors.append(f'translation chunk {chunk} requires occurrence_ids')
        else:
            ids.extend(item['occurrence_ids'])
            if ledger is not None:
                errors.extend(validate_translation_occurrences(
                    [entry for entry in ledger if entry.get('chunk') == chunk], item))
    if set(by_chunk) != expected_chunks:
        errors.append('translation chunks do not exactly match the chunk plan')
    if not isinstance(payload.get('translation_sha256'), str):
        errors.append('translation artifact requires translation_sha256')
    else:
        try:
            canonical_digest = sha256_text(_assembled_translation_from_payload(payload))
            if payload['translation_sha256'] != canonical_digest:
                errors.append('translation_sha256 does not match canonical chunk assembly')
        except (KeyError, TypeError, ValueError):
            errors.append('translation artifact has no canonical chunk assembly')
    if ledger is not None:
        errors.extend(validate_translation_occurrences(ledger, {'occurrence_ids': ids}))
    return errors


def _assembled_translation_from_payload(payload: dict) -> str:
    """Return the only allowed assembled representation of chunk translations."""
    chunks = payload.get('chunks')
    if not isinstance(chunks, list):
        raise ValueError('translation artifact requires chunks list')
    if any(not isinstance(item, dict) for item in chunks):
        raise ValueError('translation artifact contains a non-object chunk')
    ordered = sorted(chunks, key=lambda item: int(item['chunk']))
    if any(not isinstance(item.get('translated_markdown'), str) for item in ordered):
        raise ValueError('translation artifact has invalid chunk markdown')
    return '\n\n'.join(item['translated_markdown'] for item in ordered)


SEMANTIC_CHECKS = ('reference_expression', 'chunk_seams', 'register_style',
                   'context_rules', 'source_residual')


def prepared_chunks(manifest: dict) -> list[dict]:
    """Read only planned chunks and prove exact prepared-source reconstruction."""
    chunks = _source_chunks(manifest)
    texts, result = [], []
    next_line = 1
    for index, chunk in enumerate(chunks, 1):
        expected_path = os.path.join(manifest['workspace'], f'chunk_{index:03d}.md')
        if chunk.get('index') != index or os.path.abspath(chunk.get('path', '')) != expected_path:
            raise ValueError('invalid planned chunk identity/path')
        if os.path.islink(expected_path) or os.path.realpath(expected_path) != os.path.abspath(expected_path):
            raise ValueError('planned chunk must be a local ordinary file')
        with open(expected_path, encoding='utf-8', newline='') as f:
            text = f.read().replace('\r\n', '\n')
        if sha256_file(expected_path) != chunk.get('sha256'):
            raise ValueError('prepared chunk hash mismatch')
        if chunk.get('start') != next_line or chunk.get('end') != next_line + len(text.split('\n')) - 1:
            raise ValueError('prepared source positions do not exactly cover source')
        next_line = chunk['end'] + 1
        texts.append(text)
        result.append({**chunk, 'text': text})
    if sha256_text('\n'.join(texts)) != manifest['source']['sha256']:
        raise ValueError('prepared chunks do not reconstruct source hash')
    return result


def scheduling_groups(manifest: dict) -> list[list[int]]:
    size = manifest['config']['max_chunks']
    if type(size) is not int or size <= 0:
        raise ValueError('max_chunks must be a positive scheduling batch size')
    ids = [c['index'] for c in prepared_chunks(manifest)]
    return [ids[i:i + size] for i in range(0, len(ids), size)]


def expected_partial_tasks(manifest: dict, stage: str) -> list[dict]:
    """Derive bounded tasks from verified inputs, never an agent denominator.

    Terminology comparisons form an exhaustive chain of adjacent occurrences
    for each shared term. Each request has at most two chunks; local checks and
    all neighboring seams are covered independently, including scheduling seams.
    """
    chunks = prepared_chunks(manifest)
    ledger = _stage_payload(manifest, 'source_matching', 'occurrence_ledger')['occurrences']
    input_hash = stage_input_hash(manifest, stage)
    translation_hash = None
    if stage == 'semantic_qa':
        translation = _stage_payload(manifest, 'translation', 'translation')
        errors = _validate_translation_payload(manifest, translation, ledger)
        if errors:
            raise ValueError('; '.join(errors))
        translation_hash = translation['translation_sha256']
    elif stage != 'translation':
        raise ValueError('unsupported partial stage')
    tasks = []

    def add(task_id, ids, occurrences, checks):
        binding = {'run_id': manifest['run_id'], 'stage_input_hash': input_hash,
                   'task_id': task_id, 'checked_chunk_ids': ids,
                   'checked_occurrence_ids': occurrences, 'required_checks': list(checks)}
        if translation_hash is not None:
            binding['translation_sha256'] = translation_hash
        binding['task_input_hash'] = fingerprint(**binding)
        binding['path'] = os.path.join(manifest['workspace'], 'partials', stage, task_id + '.json')
        tasks.append(binding)

    for chunk in chunks:
        idx = chunk['index']
        add(f'chunk-{idx:06d}', [idx],
            [x['occurrence_id'] for x in ledger if x['chunk'] == idx],
            () if stage == 'translation' else [c for c in SEMANTIC_CHECKS if c != 'chunk_seams'])
    if stage == 'semantic_qa':
        for left, right in zip(chunks, chunks[1:]):
            ids = [left['index'], right['index']]
            add(f'seam-{ids[0]:06d}-{ids[1]:06d}', ids,
                [x['occurrence_id'] for x in ledger if x['chunk'] in ids], ('chunk_seams',))
        terms = {}
        for entry in ledger:
            terms.setdefault(entry['term_id'], []).append(entry)
        for tid, entries in sorted(terms.items()):
            entries.sort(key=lambda x: (x['source_offset'], x['occurrence_id']))
            for i, (left, right) in enumerate(zip(entries, entries[1:]), 1):
                add(f'term-{tid}-{i:06d}', sorted({left['chunk'], right['chunk']}),
                    [left['occurrence_id'], right['occurrence_id']],
                    ('reference_expression', 'context_rules'))
    return tasks


def _partial_errors(manifest: dict, expected: dict, actual: dict) -> list[str]:
    if not isinstance(actual, dict):
        return ['partial must be an object']
    errors = []
    for key, value in expected.items():
        if key != 'path' and actual.get(key) != value:
            errors.append(f"partial {expected['task_id']} mismatches {key}")
    if (not isinstance(actual.get('checked_chunk_ids'), list) or
            any(type(x) is not int for x in actual['checked_chunk_ids'])):
        errors.append('partial checked_chunk_ids must be integer IDs')
    if actual.get('schema_version') != SCHEMA_VERSION:
        errors.append('partial requires schema_version 3.0')
    limit = manifest['config'].get('agent_retry_limit', 2) + 1
    if type(actual.get('attempt')) is not int or not 1 <= actual['attempt'] <= limit:
        errors.append('partial attempt exceeds bounded retries')
    if actual.get('status') != 'completed':
        errors.append('partial is not completed')
    if expected['required_checks']:
        checks = actual.get('checks')
        if not isinstance(checks, dict) or set(checks) != set(expected['required_checks']):
            errors.append('partial check categories do not match task')
        elif any(value != 'pass' for value in checks.values()):
            errors.append('partial semantic check did not pass')
        issues = actual.get('issues')
        if not isinstance(issues, list):
            errors.append('partial requires issues list')
        elif any(not isinstance(x, dict) or x.get('severity') not in ('warning', 'error') for x in issues):
            errors.append('partial issue has invalid severity')
        elif any(x['severity'] == 'error' for x in issues):
            errors.append('partial reports semantic error')
    return errors


def validate_semantic_coverage(manifest: dict, payload: dict) -> list[str]:
    try:
        expected = expected_partial_tasks(manifest, 'semantic_qa')
    except (OSError, ValueError, KeyError) as e:
        return [f'invalid semantic task inputs: {e}']
    actual = payload.get('task_coverage')
    if not isinstance(actual, list):
        return ['semantic_qa requires task_coverage for every local, seam and shared-term task']
    by_id = {x.get('task_id'): x for x in actual if isinstance(x, dict)}
    if len(by_id) != len(actual) or set(by_id) != {x['task_id'] for x in expected}:
        return ['semantic_qa task coverage is missing, duplicated or unknown']
    errors, issues = [], []
    for task in expected:
        result = by_id[task['task_id']]
        errors.extend(_partial_errors(manifest, task, result))
        if isinstance(result.get('issues'), list):
            issues.extend(result['issues'])
    if payload.get('issues') != issues:
        errors.append('semantic_qa issues do not aggregate every task issue in order')
    if payload.get('translation_sha256') != expected[0]['translation_sha256']:
        errors.append('semantic_qa translation hash is stale')
    return errors


def cmd_assemble(args) -> None:
    manifest = _load_v3_manifest_or_die(args.manifest)
    try:
        tasks = expected_partial_tasks(manifest, args.stage)
        results = []
        for task in tasks:
            path = task['path']
            if os.path.islink(path) or os.path.realpath(path) != os.path.abspath(path):
                raise ValueError('partial must be an ordinary file inside this run')
            result = _read_json(path)
            errors = _partial_errors(manifest, task, result)
            if errors:
                raise ValueError('; '.join(errors))
            results.append(result)
        payload = {'schema_version': SCHEMA_VERSION, 'run_id': manifest['run_id'],
                   'stage_input_hash': stage_input_hash(manifest, args.stage),
                   'attempt': max(x['attempt'] for x in results)}
        ledger = _stage_payload(manifest, 'source_matching', 'occurrence_ledger')['occurrences']
        if args.stage == 'translation':
            payload['chunks'] = [{'chunk': task['checked_chunk_ids'][0],
                                  'translated_markdown': result.get('translated_markdown'),
                                  'occurrence_ids': result.get('occurrence_ids')}
                                 for task, result in zip(tasks, results)]
            payload['translation_sha256'] = sha256_text(_assembled_translation_from_payload(payload))
            errors = _validate_translation_payload(manifest, payload, ledger)
        else:
            payload.update(translation_sha256=tasks[0]['translation_sha256'],
                           checked_occurrence_ids=[x['occurrence_id'] for x in ledger],
                           task_coverage=results, issues=[issue for x in results for issue in x['issues']],
                           checks={check: 'pass' for check in SEMANTIC_CHECKS})
            errors = _validate_semantic_qa_payload(payload, ledger, manifest)
        if errors:
            raise ValueError('; '.join(errors))
        output = os.path.join(manifest['workspace'], f'assembled.{args.stage}.json')
        atomic_write_json(output, payload)
        if args.stage == 'translation':
            atomic_write_text(os.path.join(manifest['workspace'], 'assembled.translation.md'),
                              _assembled_translation_from_payload(payload))
        print(json.dumps({'stage': args.stage, 'payload': output}, ensure_ascii=False))
    except (OSError, ValueError, TypeError, KeyError) as e:
        die(f'Cannot assemble {args.stage}: {e}')


def _validate_semantic_qa_payload(payload: dict, ledger: list[dict] | None = None, manifest: dict | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload.get('translation_sha256'), str):
        errors.append('semantic_qa requires translation_sha256')
    if manifest is not None:
        errors.extend(validate_semantic_coverage(manifest, payload))
    issues = payload.get('issues')
    if not isinstance(issues, list):
        errors.append('semantic_qa requires issues list')
    else:
        for issue in issues:
            if not isinstance(issue, dict) or issue.get('severity') not in {'warning', 'error'}:
                errors.append('semantic_qa issue has invalid severity')
            elif issue.get('severity') == 'error':
                errors.append('semantic_qa reports an error')
    checks = payload.get('checks')
    required_checks = {'reference_expression', 'chunk_seams', 'register_style', 'context_rules', 'source_residual'}
    if not isinstance(checks, dict):
        errors.append('semantic_qa requires explicit semantic checks')
    else:
        for check in sorted(required_checks):
            if checks.get(check) != 'pass':
                errors.append(f'semantic_qa check did not pass: {check}')
    if ledger is not None:
        errors.extend(validate_translation_occurrences(ledger, {'occurrence_ids': payload.get('checked_occurrence_ids')}))
    return errors


def _strict_artifact_errors(manifest: dict, translation_text: str | None = None) -> list[str]:
    """Re-validate published JSON at write time; hashes alone are insufficient."""
    errors: list[str] = []
    try:
        memory = _stage_payload(manifest, 'reference_mining', 'reference_memory')
        errors.extend(_validate_reference_memory_payload(manifest, memory))
        matching = _stage_payload(manifest, 'source_matching', 'occurrence_ledger')
        errors.extend(_validate_source_matching_payload(manifest, matching))
        ledger = matching.get('occurrences') if isinstance(matching.get('occurrences'), list) else None
        translation = _stage_payload(manifest, 'translation', 'translation')
        errors.extend(_validate_translation_payload(manifest, translation, ledger))
        semantic = _stage_payload(manifest, 'semantic_qa', 'semantic_qa')
        errors.extend(_validate_semantic_qa_payload(semantic, ledger, manifest))
        coverage = _stage_payload(manifest, 'deterministic_qa', 'coverage_report')
        if coverage.get('status') != 'pass':
            errors.append('coverage report did not pass')
        if (coverage.get('run_id') != manifest.get('run_id') or
                coverage.get('source_sha256') != manifest.get('source', {}).get('sha256') or
                coverage.get('occurrence_denominator') != len(ledger)):
            errors.append('coverage report is not bound to the strict occurrence universe')
        if translation_text is not None:
            digest = sha256_text(translation_text)
            if translation.get('translation_sha256') != digest:
                errors.append('translation artifact is not bound to the QA translation text')
            if semantic.get('translation_sha256') != digest:
                errors.append('semantic_qa is not bound to the QA translation text')
            if coverage.get('translation_sha256') != digest:
                errors.append('coverage report is not bound to the QA translation text')
            try:
                if sha256_text(_assembled_translation_from_payload(translation)) != digest:
                    errors.append('assembled translation is not the canonical concatenation of published chunks')
            except (KeyError, TypeError, ValueError) as e:
                errors.append(f'cannot assemble published translation chunks: {e}')
    except (OSError, ValueError, json.JSONDecodeError) as e:
        errors.append(f'invalid strict artifact: {e}')
    return errors


# ---------------------------------------------------------------------------
# Subcommand: prepare
# ---------------------------------------------------------------------------

def cmd_prepare(args) -> None:
    """Create an isolated v3 run workspace and manifest for agent orchestration."""
    _fail_runtime_if_needed(args)
    cfg = load_config()
    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        die(f'File not found: {input_path}')

    chunk_lines = args.chunk_lines or cfg['chunk_lines']
    max_chunks = cfg['max_chunks']
    max_workspace_mb = cfg['max_workspace_mb']
    if type(max_chunks) is not int or max_chunks <= 0:
        die('max_chunks must be a positive scheduling batch size')
    if type(chunk_lines) is not int or chunk_lines <= 0:
        die('chunk_lines must be positive')

    source_text = _read_source_text(input_path)

    # Load pre-made glossary if provided (flat seed)
    glossary = {}
    structured_terms: list[dict] = []
    if args.glossary:
        for gpath in args.glossary:
            glossary.update(load_glossary(gpath))
            structured_terms.extend(load_glossary_v3(gpath))

    # Collect and load references
    reference_texts = []
    reference_hashes = []
    if args.references:
        ref_files = collect_reference_files(args.references)
        if not ref_files:
            die('No reference files found in specified paths')
        for rpath in ref_files:
            name = os.path.basename(rpath)
            try:
                content = load_reference_content(rpath)
                if not content.strip():
                    die(f'Reference is empty: {name}')
                reference_texts.append((name, content))
                reference_hashes.append({'path': os.path.abspath(rpath), 'sha256': sha256_text(content)})
            except Exception as e:
                die(f'Could not load required reference {name}: {e}')

    target_lang = args.language or cfg.get('default_target_language', 'zh')

    # Workspace + chunk plan (structure-safe)
    run_id = new_run_id()
    workspace = os.path.abspath(args.workspace) if args.workspace else os.path.join(
        os.path.dirname(input_path), '.translate-runs', run_id
    )
    chunk_plan = plan_chunks(source_text, chunk_lines, workspace)
    # Workspace size guard
    try:
        ws_size = sum(
            os.path.getsize(os.path.join(workspace, fn))
            for fn in os.listdir(workspace) if os.path.isfile(os.path.join(workspace, fn))
        )
        if ws_size > max_workspace_mb * 1024 * 1024:
            die(f'Workspace size {ws_size // 1024}KB exceeds max_workspace_mb={max_workspace_mb}.')
    except OSError:
        pass

    # Reference passages + manifest
    passage_manifest = []
    if reference_texts:
        passage_manifest = chunk_references(reference_texts, chunk_lines, workspace)

    if sum(os.path.getsize(os.path.join(root, name))
           for root, dirs, files in os.walk(workspace) for name in files) > max_workspace_mb * 1024 * 1024:
        die(f'Workspace size exceeds max_workspace_mb={max_workspace_mb}.')

    # These artifacts are deterministic inputs to the agent-owned stages.  A
    # manifest begins with those stages pending; the SKILL orchestrator advances
    # them only after validating the corresponding JSON payloads.
    source_hash = sha256_text(source_text)
    ledger = build_occurrence_ledger(source_text, chunk_plan['chunks'], structured_terms)
    ledger_path = os.path.join(workspace, 'occurrence_ledger.json')
    atomic_write_json(ledger_path, ledger)
    memory_path = os.path.join(workspace, f'reference_memory.{target_lang}.json')
    atomic_write_json(memory_path, {
        'schema_version': SCHEMA_VERSION,
        'target_language': target_lang,
        'passages': [{**p, 'status': 'pending'} for p in passage_manifest],
        'terms': [], 'expressions': [], 'style_rules': [],
    })
    chunk_plan_path = os.path.join(workspace, 'chunk_plan.json')
    passage_manifest_path = os.path.join(workspace, 'passage_manifest.json')
    atomic_write_json(chunk_plan_path, chunk_plan)
    atomic_write_json(passage_manifest_path, passage_manifest)
    manifest = make_manifest(
        source_path=input_path, source_hash=source_hash, language=target_lang,
        runtime_mode=args.runtime_mode, quality_mode=args.quality_mode,
        workspace=workspace, reference_hashes=reference_hashes, config=cfg,
        stages=['prepare', 'reference_mining', 'source_matching', 'translation',
                'deterministic_qa', 'semantic_qa', 'write'], run_id=run_id,
    )
    manifest['glossary_output'] = os.path.abspath(args.glossary_output) if args.glossary_output else None
    cache_root = os.path.abspath(os.environ.get(
        'TRANSLATE_CACHE_DIR', os.path.join(tempfile.gettempdir(), 'file-processing-translate-cache')
    ))
    manifest['cache'] = {
        'root': cache_root,
        'reference_memory_key': fingerprint(
            input_fingerprint=manifest['input_fingerprint'], source_hash=source_hash,
            references=reference_hashes, language=target_lang, config_digest=manifest['config_digest'],
            runtime_fingerprint=manifest['runtime_fingerprint'],
            retrieval_revision=os.environ.get('TRANSLATE_RETRIEVAL_REVISION', 'semantic-v3'),
            validator_revision='v3', prompt_digest=os.environ.get('TRANSLATE_PROMPT_DIGEST', 'skill-managed'),
        ),
    }
    update_stage(manifest, 'prepare', 'completed', artifacts={
        'chunk_plan': {'path': chunk_plan_path, 'sha256': sha256_file(chunk_plan_path)},
        'passage_manifest': {'path': passage_manifest_path, 'sha256': sha256_file(passage_manifest_path)},
        'occurrence_ledger': {'path': ledger_path, 'sha256': sha256_file(ledger_path)},
        'reference_memory': {'path': memory_path, 'sha256': sha256_file(memory_path)},
    })
    manifest_path = _manifest_path(workspace)
    _save_manifest(manifest_path, manifest)

    # Compact handoff: source/reference bodies stay in bounded local files.
    print('=== RUN MANIFEST ===')
    print(json.dumps({
        'path': manifest_path, 'run_id': run_id, 'target_language': target_lang, 'chunks': chunk_plan['n_chunks'],
        'scheduling_batch_size': max_chunks,
        'scheduling_batches': (chunk_plan['n_chunks'] + max_chunks - 1) // max_chunks,
        'chunk_plan': chunk_plan_path, 'passage_manifest': passage_manifest_path,
        'reference_passages': len(passage_manifest),
        'chunk_files': os.path.join(workspace, 'chunk_NNN.md'),
        'glossary_output': manifest['glossary_output'],
    }, ensure_ascii=False, indent=2))


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
    if not args.manifest:
        die('v3 qa requires --manifest; re-run prepare and use its run manifest.')
    manifest = _load_v3_manifest_or_die(args.manifest)
    source_path = os.path.abspath(args.source)
    translation_path = os.path.abspath(args.translation)
    if not os.path.exists(source_path):
        die(f'Source file not found: {source_path}')
    if not os.path.exists(translation_path):
        die(f'Translation file not found: {translation_path}')

    source_text = _read_source_text(source_path)
    with open(translation_path, 'r', encoding='utf-8') as f:
        translation_text = f.read()

    if sha256_text(source_text) != manifest.get('source', {}).get('sha256'):
        die('Source hash does not match v3 manifest; re-run prepare.')

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

    # 5. Read only manifest-bound agent artifacts.  Never scan a workspace for
    # stale chunk files or a sibling legacy glossary.
    glossary_entries: list[dict] = []
    workspace = os.path.abspath(manifest['workspace'])
    if args.workspace and os.path.abspath(args.workspace) != workspace:
        die('Workspace does not match v3 manifest; do not scan another run workspace.')
    ledger: list[dict] = []
    chunk_translations: dict[int, str] = {}
    has_chunks = False
    try:
        memory = _stage_payload(manifest, 'reference_mining', 'reference_memory')
        errors.extend(f'Reference memory: {item}' for item in _validate_reference_memory_payload(manifest, memory))
        matching = _stage_payload(manifest, 'source_matching', 'occurrence_ledger')
        errors.extend(f'Occurrence ledger: {item}' for item in _validate_source_matching_payload(manifest, matching))
        ledger_value = matching.get('occurrences')
        if isinstance(ledger_value, list):
            ledger = ledger_value
        else:
            errors.append('Occurrence ledger artifact must contain occurrences list')
        translation_payload = _stage_payload(manifest, 'translation', 'translation')
        errors.extend(f'Translation artifact: {item}' for item in _validate_translation_payload(manifest, translation_payload, ledger))
        if translation_payload.get('translation_sha256') != sha256_text(translation_text):
            errors.append('Translation artifact is not bound to the supplied assembled translation')
        try:
            if _assembled_translation_from_payload(translation_payload) != translation_text:
                errors.append('Supplied assembled translation is not the canonical concatenation of published chunks')
        except (KeyError, TypeError, ValueError):
            errors.append('Translation artifact has no canonical chunk assembly')
        for item in translation_payload.get('chunks', []):
            if isinstance(item, dict) and isinstance(item.get('translated_markdown'), str):
                chunk_translations[int(item['chunk'])] = item['translated_markdown']
        has_chunks = bool(chunk_translations)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        errors.append(f'Invalid manifest-bound agent artifact: {e}')

    if ledger:
        ledger_errors = validate_occurrence_ledger(ledger)
        errors.extend(f'Occurrence ledger: {message}' for message in ledger_errors)
        for item in ledger:
            if item.get('disposition') != 'applied' or not item.get('target'):
                continue
            chunk_number = item.get('chunk')
            check_text = chunk_translations.get(chunk_number, translation_body)
            if not target_present(str(item['target']), check_text):
                errors.append(
                    f'Occurrence {item.get("occurrence_id")} target "{item["target"]}" '
                    f'is absent from its translated chunk'
                )

    # 6. Grounded-term forced application (legacy glossary is intentionally not
    # accepted here; v3 obligations are fully represented by the ledger).
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

    # 7. Source residual + per-occurrence disposition checks.  A source term is
    # never allowed to remain in the chunk when the ledger says it was applied.
    for item in ledger:
        if item.get('disposition') == 'applied' and item.get('source'):
            rendered_chunk = chunk_translations.get(item.get('chunk'), '')
            if str(item['source']) in rendered_chunk:
                errors.append(f'Occurrence {item.get("occurrence_id")} source residual remains in its translated chunk')

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

    # Deterministic QA may complete independently of the required agent semantic
    # QA.  Strict write still checks semantic_qa separately.
    qa_report = {
        'schema_version': SCHEMA_VERSION,
        'run_id': manifest['run_id'] if manifest else None,
        'source_sha256': manifest['source']['sha256'] if manifest else sha256_text(source_text),
        'translation_sha256': sha256_text(translation_text),
        'language': target_lang,
        'status': 'fail' if errors else 'pass',
        'errors': errors,
        'warnings': warnings,
        'fix_map': fix_map,
        'coverage': {
            'occurrences_total': len(ledger),
            'occurrences_terminal': sum(1 for x in ledger if x.get('disposition') is not None),
        },
    }
    coverage_report = {
        'schema_version': SCHEMA_VERSION,
        'run_id': manifest['run_id'],
        'source_sha256': manifest['source']['sha256'],
        'translation_sha256': sha256_text(translation_text),
        'candidates_total': len(matching.get('source_candidates', [])) if 'matching' in locals() else None,
        'occurrence_denominator': len(ledger),
        'occurrence_terminal': sum(1 for x in ledger if x.get('disposition') is not None),
        'dispositions': {state: sum(1 for x in ledger if x.get('disposition') == state)
                         for state in sorted({x.get('disposition') for x in ledger if x.get('disposition')})},
        'planned_passages': len(memory.get('passages', [])) if 'memory' in locals() else None,
        'completed_passages': sum(1 for x in memory.get('passages', []) if x.get('status') == 'completed')
                              if 'memory' in locals() else None,
        'status': 'fail' if errors else 'pass',
    }
    qa_report_path = os.path.join(workspace, 'qa_report.json')
    coverage_report_path = os.path.join(workspace, 'coverage_report.json')
    atomic_write_json(qa_report_path, qa_report)
    atomic_write_json(coverage_report_path, coverage_report)
    update_stage(manifest, 'deterministic_qa', 'completed' if not errors else 'failed_permanent', artifacts={
        'qa_report': {'path': qa_report_path, 'sha256': sha256_file(qa_report_path)},
        'coverage_report': {'path': coverage_report_path, 'sha256': sha256_file(coverage_report_path)},
        'translation': {'path': translation_path, 'sha256': sha256_file(translation_path)},
    })
    _save_manifest(args.manifest, manifest)

    status = 'fail' if errors else 'pass'
    print(f'\n=== SUMMARY ===')
    print(f'status: {status}')
    print(f'errors: {len(errors)}, warnings: {len(warnings)}')
    print(f'glossary_entries: {len(glossary_entries)}, per_chunk: {has_chunks}')

    if errors:
        sys.exit(EXIT_ERROR)


# ---------------------------------------------------------------------------
# Subcommand: write
# ---------------------------------------------------------------------------

def cmd_write(args) -> None:
    """Write only a manifest-bound v3 translation artifact."""
    if not args.manifest:
        die('v3 write requires --manifest; re-run prepare and complete QA first.')
    manifest = _load_v3_manifest_or_die(args.manifest)
    input_path = os.path.abspath(args.input)
    translation_path = os.path.abspath(args.translation)
    target_lang = args.language
    if target_lang != manifest.get('language'):
        die('Target language does not match v3 manifest.')

    if not os.path.exists(translation_path):
        die(f'Translation file not found: {translation_path}')

    with open(translation_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if sha256_text(_read_source_text(input_path)) != manifest.get('source', {}).get('sha256'):
        die('Source hash does not match v3 manifest; re-run prepare.')
    translation_record = manifest.get('artifacts', {}).get('translation', {})
    if translation_record.get('sha256') != sha256_file(translation_path):
        die('Translation hash is not the QA-bound artifact in the v3 manifest.')
    qa_record = manifest.get('artifacts', {}).get('qa_report', {})
    if (not qa_record.get('path') or not os.path.exists(qa_record['path']) or
            sha256_file(qa_record['path']) != qa_record.get('sha256')):
        die('Missing QA report in v3 manifest; run qa first.')
    with open(qa_record['path'], 'r', encoding='utf-8') as f:
        qa_report = json.load(f)
    if (qa_report.get('run_id') != manifest['run_id'] or
            qa_report.get('source_sha256') != manifest['source']['sha256'] or
            qa_report.get('translation_sha256') != sha256_text(content) or
            qa_report.get('language') != target_lang):
        die('QA report is not bound to this manifest/source/translation/language.')

    quality_mode = _quality_mode_from_manifest(manifest)
    if quality_mode == 'strict':
        ready, readiness_errors = strict_ready(manifest)
        if qa_report.get('status') != 'pass':
            readiness_errors.append('deterministic QA did not pass')
        readiness_errors.extend(_strict_artifact_errors(manifest, content))
        if readiness_errors:
            die('Strict write blocked: ' + '; '.join(readiness_errors))
        qa_status = 'strict-pass'
    else:
        qa_status = 'INCOMPLETE'

    output_format = args.output_format
    extension = '.json' if output_format == 'json' else '.md'
    default_path = os.path.splitext(derive_output_path(input_path, target_lang))[0] + extension
    output_path = os.path.abspath(args.output or default_path)
    if os.path.splitext(output_path)[1].lower() != extension:
        die(f'Output extension must be {extension} for {output_format}')
    if quality_mode == 'report-only':
        stem, ext = os.path.splitext(output_path)
        output_path = f'{stem}.incomplete{ext}'

    if output_format == 'json':
        try:
            sources = prepared_chunks(manifest)
            if quality_mode == 'strict' or manifest['stages']['translation']['state'] == 'completed':
                translated = _stage_payload(manifest, 'translation', 'translation')
                by_id = {x['chunk']: x['translated_markdown'] for x in translated['chunks']}
            elif len(sources) == 1:
                by_id = {1: content}
            else:
                raise ValueError('multi-chunk diagnostic JSON requires planned translated chunks')
            document = {
                'schema_version': 1,
                'source': {'name': os.path.basename(input_path),
                           'prepared_text_sha256': manifest['source']['sha256']},
                'target_language': target_lang, 'qa_status': qa_status,
                'segments': [{'id': c['index'], 'source_start_line': c['start'],
                              'source_end_line': c['end'],
                              'source': c['text'] + ('\n' if i < len(sources) - 1 else ''),
                              'translation': by_id[c['index']]}
                             for i, c in enumerate(sources)],
            }
            content = json.dumps(document, ensure_ascii=False, indent=2) + '\n'
        except (OSError, ValueError, KeyError) as e:
            die(f'Cannot write bilingual JSON: {e}')
    elif not args.no_frontmatter:
        now = manifest['created_at']
        output_manifest_hash = manifest['stages']['write'].get('artifacts', {}).get('output', {}).get(
            'manifest_sha256', manifest['manifest_sha256'])
        source_for_fm = input_path.replace('\\', '/').replace('"', '\\"')
        manifest_for_fm = os.path.abspath(args.manifest).replace('\\', '/')
        content = (
            '---\n'
            f'source: "{source_for_fm}"\n'
            f'translated_at: "{now}"\n'
            f'translated_by: "translate"\n'
            f'target_language: "{target_lang}"\n'
            f'run_id: "{manifest["run_id"]}"\n'
            f'qa_status: "{qa_status}"\n'
            f'manifest_sha256: "{output_manifest_hash}"\n'
            f'manifest_path: "{manifest_for_fm}"\n'
            '---\n\n' + content
        )
    elif quality_mode == 'report-only':
        content = f'<!-- INCOMPLETE run_id={manifest["run_id"]} qa_status=INCOMPLETE -->\n\n' + content

    glossary_path = manifest.get('glossary_output') if quality_mode == 'strict' else None
    glossary_terms = None
    if glossary_path:
        try:
            matching = _stage_payload(manifest, 'source_matching', 'occurrence_ledger')
            glossary_terms = []
            for candidate in matching['source_candidates']:
                entry = dict(candidate)
                occurrences = [x for x in matching['occurrences'] if x['source'] == entry.get('source')]
                if not isinstance(entry.get('source'), str) or not entry['source'].strip():
                    raise ValueError('glossary source must be nonempty text')
                if entry.get('target') is not None and not isinstance(entry['target'], str):
                    raise ValueError('glossary target must be text or null')
                targets = sorted({x['target'] for x in occurrences if isinstance(x.get('target'), str) and x['target']})
                if targets:
                    entry['target'] = targets[0] if len(targets) == 1 else None
                    if len(targets) > 1:
                        entry['alternatives'] = targets
                entry['occurrences'] = occurrences
                glossary_terms.append(entry)
        except (OSError, ValueError, KeyError) as e:
            die(f'Cannot export requested glossary: {e}')

    def aliases(left, right):
        return (os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right)) or
                (os.path.exists(left) and os.path.exists(right) and os.path.samefile(left, right)))

    protected = [input_path, manifest['source']['path'], translation_path, args.manifest]
    protected.extend(x['path'] for x in manifest.get('references', []))
    protected.extend(x['path'] for stage, record in manifest['stages'].items() if stage != 'write'
                     for x in record.get('artifacts', {}).values() if isinstance(x, dict) and x.get('path'))
    # The whole private run is reserved, including partials and future artifacts.
    def preflight(path, key, expected_hash):
        path = os.path.abspath(path)
        try:
            inside_run = os.path.commonpath([os.path.realpath(path), os.path.realpath(manifest['workspace'])]) == os.path.realpath(manifest['workspace'])
        except ValueError:  # different Windows volumes
            inside_run = False
        if inside_run:
            die('Output must not overwrite required run files')
        if any(aliases(path, x) for x in protected):
            die('Output aliases source or required run files')
        previous = manifest['stages']['write'].get('artifacts', {}).get(key, {})
        if (manifest['stages']['write'].get('state') == 'failed_transient'
                and previous.get('requested_path', previous.get('path')) == path
                and previous.get('sha256') == expected_hash
                and os.path.isfile(previous.get('path', ''))
                and sha256_file(previous['path']) == expected_hash):
            return previous['path'], True
        if os.path.exists(path):
            if args.overwrite:
                return path, False
            if args.rename:
                stem, ext = os.path.splitext(path)
                timestamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
                renamed = f'{stem}-{timestamp}{ext}'
                serial = 1
                while os.path.exists(renamed):
                    renamed = f'{stem}-{timestamp}-{serial}{ext}'
                    serial += 1
                return renamed, False
            die(f'Output file already exists: {path}; use --overwrite or --rename')
        return path, False

    if glossary_path and aliases(output_path, glossary_path):
        die('Translation and glossary destinations must be distinct')
    requested_output_path = output_path
    requested_glossary_path = glossary_path
    output_path, reuse_output = preflight(output_path, 'output', sha256_text(content))
    reuse_glossary = False
    if glossary_path:
        glossary_hash = sha256_text(json.dumps({'terms': glossary_terms}, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
        glossary_path, reuse_glossary = preflight(glossary_path, 'glossary_output', glossary_hash)
        if aliases(output_path, glossary_path):
            die('Translation and glossary destinations must be distinct')
    published = {}
    try:
        if not reuse_output:
            atomic_write_text(output_path, content)
        published['output'] = {'path': output_path, 'sha256': sha256_file(output_path),
                               'requested_path': requested_output_path,
                               'manifest_sha256': locals().get('output_manifest_hash', manifest['manifest_sha256'])}
        if glossary_path:
            if not reuse_glossary:
                save_glossary_structured(glossary_terms, glossary_path)
            published['glossary_output'] = {'path': glossary_path, 'sha256': sha256_file(glossary_path),
                                            'requested_path': requested_glossary_path}
    except OSError as e:
        update_stage(manifest, 'write', 'failed_transient', artifacts=published, detail=str(e))
        _save_manifest(args.manifest, manifest)
        die(f'Write/export failed: {e}; published paths: ' + ', '.join(x['path'] for x in published.values()))
    update_stage(manifest, 'write', 'completed', artifacts=published)
    _save_manifest(args.manifest, manifest)
    print(json.dumps({'status': qa_status, 'published_paths': [x['path'] for x in published.values()]}, ensure_ascii=False))
    if quality_mode == 'report-only':
        sys.exit(EXIT_REPORT_ONLY)


def cmd_write_legacy(args) -> None:
    """Explicit v2 migration boundary; v3 never emits an unbound formal file."""
    die('Legacy write is not available in Translate v3; run prepare and pass --manifest.')


def cmd_resume(args) -> None:
    """Inspect a v3 run and report the first stage requiring orchestration."""
    manifest = _load_v3_manifest_or_die(args.manifest)
    source_path = manifest.get('source', {}).get('path')
    if not source_path or not os.path.exists(source_path):
        print('ERROR: source is unavailable; run cannot be resumed', file=sys.stderr)
        sys.exit(EXIT_ERROR)
    if sha256_text(_read_source_text(source_path)) != manifest['source'].get('sha256'):
        print('ERROR: source changed; manifest is stale and must be rebuilt', file=sys.stderr)
        sys.exit(EXIT_ERROR)
    if fingerprint(config=load_config()) != manifest.get('config_digest'):
        print('ERROR: translation configuration changed; manifest must be rebuilt', file=sys.stderr)
        sys.exit(EXIT_ERROR)
    if runtime_fingerprint(manifest.get('runtime_mode', 'unavailable')) != manifest.get('runtime_fingerprint'):
        print('ERROR: agent/prompt/model/retrieval runtime changed; manifest must be rebuilt', file=sys.stderr)
        sys.exit(EXIT_ERROR)
    for reference in manifest.get('references', []):
        ref_path = reference.get('path') if isinstance(reference, dict) else None
        try:
            if not ref_path or sha256_text(load_reference_content(ref_path)) != reference.get('sha256'):
                raise ValueError('reference hash mismatch')
        except (OSError, ValueError) as e:
            print(f'ERROR: reference changed or unavailable ({ref_path}): {e}; manifest must be rebuilt', file=sys.stderr)
            sys.exit(EXIT_ERROR)
    if manifest.get('quality_mode') == 'strict' and manifest.get('runtime_mode') != 'orchestrated':
        print('ERROR: strict resume requires orchestrated runtime', file=sys.stderr)
        sys.exit(EXIT_RUNTIME_UNAVAILABLE)
    stage_order = manifest.get('stage_order') or list(manifest['stages'])
    # A completed stage is trusted only if every manifest-bound artifact still
    # exists and hashes correctly. Reset the damaged stage and its dependents.
    for name in stage_order:
        record = manifest['stages'].get(name, {})
        if record.get('state') == 'completed':
            stale = [artifact for artifact in record.get('artifacts', {}).values()
                     if not os.path.exists(artifact.get('path', '')) or
                     sha256_file(artifact['path']) != artifact.get('sha256')]
            if stale:
                invalidate_downstream(manifest, name, 'artifact hash mismatch during resume')
                _save_manifest(args.manifest, manifest)
                break
    incomplete = next((name for name in stage_order
                       if manifest['stages'].get(name, {}).get('state') != 'completed'), None)
    if incomplete:
        print(json.dumps({'run_id': manifest['run_id'], 'resume_from': incomplete,
                          'state': manifest['stages'][incomplete]['state']}, ensure_ascii=False))
        sys.exit(EXIT_RECOVERABLE)
    print(json.dumps({'run_id': manifest['run_id'], 'status': 'completed'}, ensure_ascii=False))


def cmd_publish_stage(args) -> None:
    """Atomically promote a validated agent artifact into a v3 run.

    The orchestration layer invokes this after an agent produces JSON.  Keeping
    this boundary in Python prevents an agent from directly changing manifest
    state or accidentally mixing artifacts from different runs.
    """
    manifest = _load_v3_manifest_or_die(args.manifest)
    if args.stage not in manifest['stages']:
        die(f'Unknown v3 stage: {args.stage}')
    order = manifest.get('stage_order') or list(manifest['stages'])
    stage_index = order.index(args.stage)
    for upstream in order[:stage_index]:
        if manifest['stages'][upstream].get('state') != 'completed':
            die(f'Cannot publish {args.stage} before completed upstream stage {upstream}')
    try:
        with open(args.input, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        die(f'Invalid stage artifact: {e}')
    if not isinstance(payload, dict) or payload.get('schema_version') != SCHEMA_VERSION:
        die(f'Stage artifact must be an object with schema_version={SCHEMA_VERSION}')
    if payload.get('run_id') != manifest['run_id']:
        die('Stage artifact run_id does not match manifest')
    expected_input = payload.get('stage_input_hash')
    expected_stage_input = stage_input_hash(manifest, args.stage)
    if expected_input != expected_stage_input:
        die('Stage artifact stage_input_hash does not match this stage inputs')
    try:
        attempt = int(payload.get('attempt', 1))
    except (TypeError, ValueError):
        die('Stage artifact attempt must be an integer')
    retry_limit = int(manifest.get('config', {}).get('agent_retry_limit', DEFAULT_CONFIG['agent_retry_limit']))
    if attempt < 1 or attempt > retry_limit + 1:
        die(f'Stage artifact attempt must be within 1..{retry_limit + 1}')

    if args.state == 'completed':
        validation_errors: list[str] = []
        if args.stage == 'reference_mining' and args.artifact == 'reference_memory':
            validation_errors = _validate_reference_memory_payload(manifest, payload)
        elif args.stage == 'source_matching' and args.artifact == 'occurrence_ledger':
            validation_errors = _validate_source_matching_payload(manifest, payload)
        elif args.stage == 'translation' and args.artifact == 'translation':
            try:
                matching = _stage_payload(manifest, 'source_matching', 'occurrence_ledger')
                ledger = matching.get('occurrences') if isinstance(matching.get('occurrences'), list) else None
            except (OSError, ValueError, json.JSONDecodeError) as e:
                die(f'Cannot validate translation artifact: {e}')
            validation_errors = _validate_translation_payload(manifest, payload, ledger)
        elif args.stage == 'semantic_qa' and args.artifact == 'semantic_qa':
            try:
                matching = _stage_payload(manifest, 'source_matching', 'occurrence_ledger')
                ledger = matching.get('occurrences') if isinstance(matching.get('occurrences'), list) else None
            except (OSError, ValueError, json.JSONDecodeError) as e:
                die(f'Cannot validate semantic QA artifact: {e}')
            validation_errors = _validate_semantic_qa_payload(payload, ledger, manifest)
        else:
            validation_errors = [f'unsupported required artifact {args.stage}/{args.artifact}']
        if validation_errors:
            die('Invalid stage artifact: ' + '; '.join(validation_errors))
    previous = manifest['stages'][args.stage]
    if previous.get('state') == args.state and args.artifact in previous.get('artifacts', {}):
        try:
            if _stage_payload(manifest, args.stage, args.artifact) == payload:
                print(json.dumps({'stage': args.stage, 'state': args.state, 'idempotent': True}))
                return
        except (OSError, ValueError):
            pass
    artifact_dir = os.path.join(manifest['workspace'], 'artifacts', args.stage)
    artifact_path = os.path.join(artifact_dir, f'{args.artifact}.json')
    raw_response_sha256 = sha256_file(args.input)
    atomic_write_json(artifact_path, payload)
    if manifest['stages'][args.stage].get('state') == 'completed':
        invalidate_downstream(manifest, args.stage, 'upstream artifact republished')
    update_stage(manifest, args.stage, 'running', detail=f'agent attempt {attempt}')
    update_stage(manifest, args.stage, args.state, artifacts={
        args.artifact: {
            'path': artifact_path,
            'sha256': sha256_file(artifact_path),
            'parsed_artifact_sha256': sha256_file(artifact_path),
            'raw_response_sha256': raw_response_sha256,
            'stage_input_hash': expected_stage_input,
            'attempt': attempt,
        },
    })
    if args.stage == 'reference_mining' and args.artifact == 'reference_memory' and args.state == 'completed':
        cache = manifest.get('cache', {})
        if cache.get('root') and cache.get('reference_memory_key'):
            cached = publish_cache_json(cache['root'], cache['reference_memory_key'], payload)
            manifest['cache']['reference_memory'] = {
                'path': cached['path'], 'sha256': cached['sha256'], 'hit': cached['hit'],
            }
    _save_manifest(args.manifest, manifest)
    print(json.dumps({'run_id': manifest['run_id'], 'stage': args.stage,
                      'state': args.state, 'artifact': artifact_path}, ensure_ascii=False))


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
    p_prepare.add_argument('--quality-mode', choices=('strict', 'report-only'), default='strict',
                           help='strict requires orchestration; report-only creates an INCOMPLETE run')
    p_prepare.add_argument('--runtime-mode', choices=('orchestrated', 'unavailable'), default='unavailable',
                           help='Set by the SKILL orchestrator after its runtime preflight')

    # --- qa ---
    p_qa = subparsers.add_parser('qa', help='Quality check translation against source')
    p_qa.add_argument('--source', required=True, help='Original source file')
    p_qa.add_argument('--translation', required=True, help='Translated file to check')
    p_qa.add_argument('--language', '-l', default='', help='Target language code')
    p_qa.add_argument('--glossary', nargs='*', help='Glossary files to check coverage (else auto-discover)')
    p_qa.add_argument('--workspace', default=None, help='Workspace with chunk_NNN.<lang>.md files')
    p_qa.add_argument('--audits-dir', default=None, help='Dir with self_audit_NNN.json files')
    p_qa.add_argument('--manifest', required=True, help='v3 run_manifest.json created by prepare')

    p_assemble = subparsers.add_parser('assemble', help='Validate expected local partials and assemble a full-stage payload')
    p_assemble.add_argument('--manifest', required=True)
    p_assemble.add_argument('--stage', required=True, choices=('translation', 'semantic_qa'))

    # --- write ---
    p_write = subparsers.add_parser('write', help='Write translated file')
    p_write.add_argument('--input', required=True, help='Original source file (for naming)')
    p_write.add_argument('--translation', required=True, help='Translated content file')
    p_write.add_argument('--language', '-l', required=True, help='Target language code')
    p_write.add_argument('--no-frontmatter', action='store_true', help='Skip frontmatter injection')
    p_write.add_argument('--overwrite', action='store_true', help='Overwrite existing output')
    p_write.add_argument('--rename', action='store_true', help='Rename if output exists')
    p_write.add_argument('--output-format', choices=('json', 'markdown'), default='json')
    p_write.add_argument('--output', default='', help='Optional output path')
    p_write.add_argument('--manifest', required=True, help='v3 run_manifest.json bound to QA')

    # --- resume ---
    p_resume = subparsers.add_parser('resume', help='Report the next incomplete v3 run stage')
    p_resume.add_argument('--manifest', required=True, help='v3 run_manifest.json')

    # --- publish-stage (orchestrator-only artifact boundary) ---
    p_publish = subparsers.add_parser('publish-stage', help='Validate and atomically publish an agent-stage JSON artifact')
    p_publish.add_argument('--manifest', required=True, help='v3 run_manifest.json')
    p_publish.add_argument('--stage', required=True, help='Manifest stage to advance')
    p_publish.add_argument('--artifact', required=True, help='Artifact name within the stage')
    p_publish.add_argument('--input', required=True, help='Agent JSON artifact to publish')
    p_publish.add_argument('--state', choices=('completed', 'failed_transient', 'failed_permanent', 'blocked_budget'),
                           default='completed', help='Resulting stage state')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == 'prepare':
        cmd_prepare(args)
    elif args.command == 'qa':
        cmd_qa(args)
    elif args.command == 'assemble':
        cmd_assemble(args)
    elif args.command == 'write':
        cmd_write(args)
    elif args.command == 'resume':
        cmd_resume(args)
    elif args.command == 'publish-stage':
        cmd_publish_stage(args)


if __name__ == '__main__':
    main()
