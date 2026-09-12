"""M2 sc006–012: controlled payloads, no model, network or converter calls."""
import copy
import json
import os
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills/translate/scripts'))
import translate_pipeline as pipeline
from v3_runtime import atomic_write_json, build_occurrence_ledger, sha256_file, sha256_text, stage_input_hash


class ControlledRun:
    def __init__(self, root, monkeypatch, text='hello\n\nhello', *, glossary=False, references=False, chunk_lines=2):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.source = root / 'source.md'
        self.source.write_text(text, encoding='utf-8', newline='\n')
        self.workspace = root / 'run'
        self.path = self.workspace / 'run_manifest.json'
        self.translation = self.workspace / 'assembled.translation.md'
        self.glossary = root / 'requested.glossary.json'
        self.config = dict(pipeline.DEFAULT_CONFIG, chunk_lines=chunk_lines)
        monkeypatch.setattr(pipeline, 'load_config', lambda: self.config)
        monkeypatch.setenv('TRANSLATE_CACHE_DIR', str(root / 'cache'))
        refs = []
        if references:
            ref = root / 'reference.md'
            ref.write_text('hello means 你好', encoding='utf-8')
            refs = [str(ref)]
        pipeline.cmd_prepare(Namespace(input=str(self.source), language='zh', workspace=str(self.workspace),
                                      glossary=[], references=refs, glossary_output=str(self.glossary) if glossary else '',
                                      chunk_lines=chunk_lines, quality_mode='strict', runtime_mode='orchestrated'))
        self.groups = pipeline.scheduling_groups(self.manifest)

    @property
    def manifest(self):
        return pipeline._load_v3_manifest_or_die(str(self.path))

    def publish(self, stage, payload, *, state='completed'):
        manifest = self.manifest
        payload = dict(payload)
        payload.update(schema_version='3.0', run_id=manifest['run_id'],
                       stage_input_hash=stage_input_hash(manifest, stage), attempt=1)
        source = self.workspace / ('input.' + stage + '.json')
        atomic_write_json(source, payload)
        pipeline.cmd_publish_stage(Namespace(manifest=str(self.path), stage=stage, artifact={
            'reference_mining': 'reference_memory', 'source_matching': 'occurrence_ledger'
        }.get(stage, stage), input=str(source), state=state))

    def grounding(self):
        passages = pipeline._manifest_json_artifact(self.manifest, 'passage_manifest')
        self.publish('reference_mining', {
            'passages': [{**p, 'status': 'completed', 'evidence': []} for p in passages],
            'terms': [], 'expressions': [], 'style_rules': [],
            'semantic_retrieval': {'status': 'completed', 'provider': 'controlled', 'results': []} if passages else {
                'status': 'not_applicable', 'reason': 'no references'},
        })
        candidates = [{'source': 'hello', 'target': '你好', 'origin': 'user_seed', 'confidence': 'high'}]
        chunks = pipeline.prepared_chunks(self.manifest)
        ledger = build_occurrence_ledger(self.source.read_text(encoding='utf-8'), chunks, candidates)
        for entry in ledger:
            entry['disposition'] = 'applied'
        self.ledger = ledger
        self.publish('source_matching', {
            'scan_chunks': [{'chunk': c['index'], 'status': 'completed'} for c in chunks],
            'source_candidates': candidates, 'occurrences': ledger,
            'relevance_batches': [[x['occurrence_id'] for x in batch]
                                  for batch in pipeline.partition_relevance_batches(ledger, 120)],
        })

    def partials(self, stage):
        tasks = pipeline.expected_partial_tasks(self.manifest, stage)
        chunks = {c['index']: c['text'] for c in pipeline.prepared_chunks(self.manifest)}
        submissions = []
        # Exactly the SKILL scheduling loop: all consecutive groups, no prompt
        # about continuing. Model payloads here are deterministic test doubles.
        groups = self.groups if stage == 'translation' else [tasks[i:i + 30] for i in range(0, len(tasks), 30)]
        for group in groups:
            selected = [t for t in tasks if t['checked_chunk_ids'][0] in group] if stage == 'translation' else group
            submissions.append([])
            for task in selected:
                payload = {**task, 'schema_version': '3.0', 'attempt': 1, 'status': 'completed'}
                if stage == 'translation':
                    payload.update(translated_markdown=chunks[task['checked_chunk_ids'][0]].replace('hello', '你好'),
                                   occurrence_ids=task['checked_occurrence_ids'])
                else:
                    payload.update(checks={c: 'pass' for c in task['required_checks']}, issues=[])
                atomic_write_json(task['path'], payload)
                submissions[-1].append(task)
        return tasks, submissions

    def assemble(self, stage):
        pipeline.cmd_assemble(Namespace(manifest=str(self.path), stage=stage))
        payload = pipeline._read_json(str(self.workspace / f'assembled.{stage}.json'))
        self.publish(stage, payload)
        return payload

    def qa(self):
        pipeline.cmd_qa(Namespace(manifest=str(self.path), source=str(self.source), translation=str(self.translation),
                                 language='zh', workspace=str(self.workspace), glossary='', self_audits=''))

    def complete(self):
        self.grounding()
        self.partials('translation')
        self.assemble('translation')
        self.qa()
        self.partials('semantic_qa')
        self.assemble('semantic_qa')

    def write(self, **kwargs):
        args = dict(manifest=str(self.path), input=str(self.source), translation=str(self.translation), language='zh',
                    output='', output_format='json', no_frontmatter=False, overwrite=False, rename=False)
        args.update(kwargs)
        pipeline.cmd_write(Namespace(**args))
        return self.root / 'source.zh.json'


def expect_blocked(call):
    with pytest.raises(SystemExit) as err:
        call()
    assert err.value.code == 1


def test_sc006_bilingual_exact_unicode_order_repeats_tables_code(tmp_path, monkeypatch):
    text = '# 标题 "quoted"\n\nhello\n\nhello\n\n| 列 | 值 |\n|---|---|\n|hello|保留|\n\n```python\nx = "保留"\n```\n'
    run = ControlledRun(tmp_path, monkeypatch, text, chunk_lines=4)
    run.complete()
    output = run.write()
    raw = output.read_bytes()
    document = json.loads(raw)
    assert raw.endswith(b'\n') and b'\\u' not in raw and not raw.startswith(b'---')
    assert ''.join(x['source'] for x in document['segments']) == text
    assert sha256_text(text) == document['source']['prepared_text_sha256']
    assert [x['id'] for x in document['segments']] == list(range(1, len(document['segments']) + 1))
    assert [x['source_start_line'] for x in document['segments']] == [c['start'] for c in pipeline.prepared_chunks(run.manifest)]
    assert sum(x['source'].count('hello') for x in document['segments']) == 3
    assert '```python\nx = "保留"\n```' in ''.join(x['translation'] for x in document['segments'])
    assert document['qa_status'] == 'strict-pass'


@pytest.mark.parametrize('count,groups', [(31, 2), (61, 3)])
def test_sc007_automatic_bounded_batch_harness(tmp_path, monkeypatch, count, groups):
    run = ControlledRun(tmp_path, monkeypatch, '\n\n'.join(['hello'] * count))
    run.grounding()
    tasks, submissions = run.partials('translation')
    assert len(submissions) == groups
    assert all(len(x) <= 30 for x in submissions)
    assert all(len(t['checked_chunk_ids']) == 1 for t in tasks)
    run.assemble('translation')
    run.qa()
    tasks, submissions = run.partials('semantic_qa')
    assert all(len(x) <= 30 for x in submissions)
    assert all(len(t['checked_chunk_ids']) <= 2 for t in tasks)
    assert len([t for t in tasks if t['task_id'].startswith('seam-')]) == count - 1
    assert len([t for t in tasks if t['task_id'].startswith('term-')]) == count - 1
    run.assemble('semantic_qa')
    assert len(json.loads(run.write().read_text(encoding='utf-8'))['segments']) == count
    assert run.manifest['stages']['write']['state'] == 'completed'


@pytest.mark.parametrize('mutation', ['missing', 'wrong_run', 'hash', 'id', 'occurrence', 'duplicate', 'failed', 'retry'])
def test_sc008_partial_failures_preserve_stage(tmp_path, monkeypatch, mutation):
    run = ControlledRun(tmp_path, monkeypatch)
    run.grounding()
    tasks, _ = run.partials('translation')
    path = Path(tasks[0]['path'])
    data = json.loads(path.read_text(encoding='utf-8'))
    if mutation == 'missing':
        path.unlink()
    else:
        changes = {'wrong_run': ('run_id', 'another-run'), 'hash': ('stage_input_hash', 'bad'),
                   'id': ('task_id', 'unknown'), 'occurrence': ('occurrence_ids', tasks[1]['checked_occurrence_ids']),
                   'duplicate': ('occurrence_ids', data['occurrence_ids'] * 2),
                   'failed': ('status', 'failed_transient'), 'retry': ('attempt', 4)}
        key, value = changes[mutation]
        data[key] = value
        atomic_write_json(path, data)
    before = run.path.read_bytes()
    expect_blocked(lambda: run.assemble('translation'))
    assert run.path.read_bytes() == before
    assert not (run.workspace / 'assembled.translation.json').exists()


def test_sc008_exact_replay_and_correction_invalidation(tmp_path, monkeypatch):
    run = ControlledRun(tmp_path, monkeypatch)
    run.complete()
    payload = pipeline._stage_payload(run.manifest, 'translation', 'translation')
    before = run.path.read_bytes()
    run.publish('translation', payload)
    assert run.path.read_bytes() == before
    payload['chunks'][0]['translated_markdown'] += '\n'
    payload['translation_sha256'] = sha256_text(pipeline._assembled_translation_from_payload(payload))
    run.publish('translation', payload)
    assert run.manifest['stages']['semantic_qa']['state'] == 'pending'
    expect_blocked(run.write)


@pytest.mark.parametrize('references,requested', [(False, False), (True, False), (True, True), (False, True)])
def test_sc009_glossary_only_when_requested(tmp_path, monkeypatch, references, requested):
    run = ControlledRun(tmp_path, monkeypatch, references=references, glossary=requested)
    run.complete()
    run.write()
    assert run.glossary.exists() == requested
    assert not (tmp_path / 'source.glossary.zh.json').exists()
    if requested:
        terms = json.loads(run.glossary.read_text(encoding='utf-8'))['terms']
        assert terms[0]['source'] == 'hello' and terms[0]['target'] == '你好'
        assert len(terms[0]['occurrences']) == 2


def test_sc009_late_glossary_failure_honest_retry(tmp_path, monkeypatch, capsys):
    run = ControlledRun(tmp_path, monkeypatch, glossary=True)
    run.complete()
    writer = pipeline.save_glossary_structured
    def failing(*args):
        raise OSError('controlled late glossary failure')
    monkeypatch.setattr(pipeline, 'save_glossary_structured', failing)
    expect_blocked(run.write)
    output = tmp_path / 'source.zh.json'
    original = output.read_bytes()
    assert str(output) in capsys.readouterr().err
    assert run.manifest['stages']['write']['state'] == 'failed_transient'
    monkeypatch.setattr(pipeline, 'save_glossary_structured', writer)
    run.write()
    assert output.read_bytes() == original and run.glossary.exists()
    assert run.manifest['stages']['write']['state'] == 'completed'


def test_sc009_retry_protects_changed_user_output(tmp_path, monkeypatch):
    run = ControlledRun(tmp_path, monkeypatch, glossary=True)
    run.complete()
    monkeypatch.setattr(pipeline, 'save_glossary_structured', lambda *a: (_ for _ in ()).throw(OSError('failure')))
    expect_blocked(run.write)
    output = tmp_path / 'source.zh.json'
    output.write_text('user bytes', encoding='utf-8')
    expect_blocked(run.write)
    assert output.read_text(encoding='utf-8') == 'user bytes'


@pytest.mark.parametrize('kind', ['chunk-', 'seam-', 'term-'])
def test_sc010_mandatory_task_coverage_direct_publish_and_strict_write(tmp_path, monkeypatch, kind):
    run = ControlledRun(tmp_path, monkeypatch)
    run.complete()
    payload = pipeline._stage_payload(run.manifest, 'semantic_qa', 'semantic_qa')
    payload['task_coverage'] = [t for t in payload['task_coverage'] if not t['task_id'].startswith(kind)]
    expect_blocked(lambda: run.publish('semantic_qa', payload))
    # Even a forged stage record with internally correct file hashes cannot
    # waive the common strict-write semantic coverage validator.
    manifest = run.manifest
    record = manifest['stages']['semantic_qa']['artifacts']['semantic_qa']
    atomic_write_json(record['path'], payload)
    record['sha256'] = sha256_file(record['path'])
    manifest['artifacts']['semantic_qa'] = record
    pipeline._save_manifest(str(run.path), manifest)
    expect_blocked(run.write)
    assert not (tmp_path / 'source.zh.json').exists()


@pytest.mark.parametrize('prefix', ['seam-000030-', 'term-'])
def test_sc010_cross_batch_errors_block_then_valid_control(tmp_path, monkeypatch, prefix):
    run = ControlledRun(tmp_path, monkeypatch, '\n\n'.join(['hello'] * 31))
    run.grounding()
    run.partials('translation')
    run.assemble('translation')
    run.qa()
    tasks, _ = run.partials('semantic_qa')
    task = next(t for t in tasks if t['task_id'].startswith(prefix))
    path = Path(task['path'])
    good = json.loads(path.read_text(encoding='utf-8'))
    bad = copy.deepcopy(good)
    bad['issues'] = [{'severity': 'error', 'issue': 'controlled seam or terminology drift'}]
    atomic_write_json(path, bad)
    expect_blocked(lambda: run.assemble('semantic_qa'))
    expect_blocked(run.write)
    atomic_write_json(path, good)
    run.assemble('semantic_qa')
    assert run.write().exists()


def test_sc011_outputs_collisions_aliases_markdown(tmp_path, monkeypatch):
    run = ControlledRun(tmp_path, monkeypatch, glossary=True)
    run.complete()
    # Both destinations are preflighted before either is published.
    run.glossary.write_text('existing glossary', encoding='utf-8')
    expect_blocked(run.write)
    assert not (tmp_path / 'source.zh.json').exists()
    run.glossary.unlink()
    expect_blocked(lambda: run.write(output=str(run.source), output_format='markdown', overwrite=True))
    expect_blocked(lambda: run.write(output=str(run.glossary)))
    expect_blocked(lambda: run.write(output=str(run.workspace / 'reserved.json')))
    expect_blocked(lambda: run.write(output=str(tmp_path / 'wrong.md')))
    output = run.write(no_frontmatter=True)
    expect_blocked(run.write)
    run.write(output_format='markdown', overwrite=True)
    assert 'qa_status: "strict-pass"' in (tmp_path / 'source.zh.md').read_text(encoding='utf-8')
    assert json.loads(output.read_text(encoding='utf-8'))['qa_status'] == 'strict-pass'
    run.write(rename=True)
    assert len(list(tmp_path.glob('source.zh-*.json'))) == 1


@pytest.mark.parametrize('command', ['assemble', 'publish_stage', 'qa', 'write', 'resume'])
def test_sc011_old_runtime_rejected_without_mutation(tmp_path, monkeypatch, command):
    run = ControlledRun(tmp_path, monkeypatch)
    run.complete()
    manifest = run.manifest
    manifest['runtime_fingerprint'] = 'old-runtime'
    pipeline._save_manifest(str(run.path), manifest)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    expect_blocked(lambda: getattr(pipeline, 'cmd_' + command)(Namespace(manifest=str(run.path))))
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


def test_sc012_real_resource_guard_and_indivisible_blocks(tmp_path, monkeypatch):
    run = ControlledRun(tmp_path, monkeypatch, '```\n' + 'hello\n' * 12 + '```', chunk_lines=2)
    chunks = pipeline.prepared_chunks(run.manifest)
    assert len(chunks) == 1 and chunks[0]['oversized']
    assert chunks[0]['text'] == run.source.read_text(encoding='utf-8')
    # Sparse file exercises the genuine default 100 MiB guard without a model.
    sentinel = run.workspace / 'resource.bin'
    with sentinel.open('wb') as f:
        f.truncate(100 * 1024 * 1024 + 1)
    old_manifest = run.path.read_bytes()
    expect_blocked(lambda: pipeline.cmd_prepare(Namespace(input=str(run.source), language='zh',
        workspace=str(run.workspace), glossary=[], references=[], glossary_output='', chunk_lines=2,
        quality_mode='strict', runtime_mode='orchestrated')))
    assert run.path.read_bytes() == old_manifest


@pytest.mark.parametrize('size', [0, -1, '30', True])
def test_sc012_positive_batch_size_required(tmp_path, monkeypatch, size):
    source = tmp_path / 'source.md'
    source.write_text('hello', encoding='utf-8')
    monkeypatch.setattr(pipeline, 'load_config', lambda: dict(pipeline.DEFAULT_CONFIG, max_chunks=size))
    expect_blocked(lambda: pipeline.cmd_prepare(Namespace(input=str(source), language='zh', workspace=str(tmp_path / 'run'),
        glossary=[], references=[], glossary_output='', chunk_lines=2, quality_mode='strict', runtime_mode='orchestrated')))
    assert not (tmp_path / 'run').exists()


@pytest.mark.parametrize('output_format,rename', [('markdown', False), ('json', True)])
def test_sc009_retry_reuses_exact_markdown_or_renamed_result(tmp_path, monkeypatch, output_format, rename):
    run = ControlledRun(tmp_path, monkeypatch, glossary=True)
    run.complete()
    if rename:
        (tmp_path / 'source.zh.json').write_text('existing user file', encoding='utf-8')
    writer = pipeline.save_glossary_structured
    monkeypatch.setattr(pipeline, 'save_glossary_structured', lambda *a: (_ for _ in ()).throw(OSError('late failure')))
    expect_blocked(lambda: run.write(output_format=output_format, rename=rename))
    record = run.manifest['stages']['write']['artifacts']['output']
    output = Path(record['path'])
    original = output.read_bytes()
    monkeypatch.setattr(pipeline, 'save_glossary_structured', writer)
    run.write(output_format=output_format, rename=rename)
    assert output.read_bytes() == original
    assert run.manifest['stages']['write']['artifacts']['output']['path'] == str(output)
    if rename:
        assert len(list(tmp_path.glob('source.zh-*.json'))) == 1
        assert (tmp_path / 'source.zh.json').read_text(encoding='utf-8') == 'existing user file'


@pytest.mark.parametrize('mutation', ['duplicate', 'unknown', 'input_hash', 'translation_hash', 'checks', 'incomplete'])
def test_sc010_semantic_evidence_rejects_forged_coverage(tmp_path, monkeypatch, mutation):
    run = ControlledRun(tmp_path, monkeypatch)
    run.complete()
    payload = pipeline._stage_payload(run.manifest, 'semantic_qa', 'semantic_qa')
    task = payload['task_coverage'][0]
    if mutation == 'duplicate':
        payload['task_coverage'].append(copy.deepcopy(task))
    elif mutation == 'unknown':
        task['task_id'] = 'invented-task'
    elif mutation == 'input_hash':
        task['task_input_hash'] = 'stale'
    elif mutation == 'translation_hash':
        task['translation_sha256'] = 'stale'
    elif mutation == 'checks':
        task['checks'].pop('context_rules')
    else:
        task['status'] = 'running'
    before = run.path.read_bytes()
    expect_blocked(lambda: run.publish('semantic_qa', payload))
    assert run.path.read_bytes() == before


def test_sc008_prepared_chunk_tamper_rejected_at_assembly(tmp_path, monkeypatch):
    run = ControlledRun(tmp_path, monkeypatch)
    run.grounding()
    run.partials('translation')
    Path(pipeline.prepared_chunks(run.manifest)[0]['path']).write_text('tampered', encoding='utf-8')
    before = run.path.read_bytes()
    expect_blocked(lambda: run.assemble('translation'))
    assert run.path.read_bytes() == before
