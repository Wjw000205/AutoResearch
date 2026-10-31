"""Explicit pre-code implementation restart, retaining every paid source.

The controller owns its lifecycle lock. This module neither starts a worker nor
reads back a native session. Archived workflows remain immutable history; a
receipt-bound generation separates all subsequent assignments and graph state.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from .storage import now
from .config import RETIRED_IMPLEMENTATION_REVIEW_FIELDS

COLLECTION = 'implementation_restarts'
MARKER = 'implementation_restart'
GENERATION = 'implementation_generation'
GRAPH_THREAD = 'implementation_graph_thread_id'
ROLES = ('director', 'engineer', 'scout')
SCIENTIFIC_COLLECTIONS = ('architecture_reviews', 'architecture_revisions',
                          'architecture_definitions', 'experiment_plans')
PAID_COLLECTIONS = ('codex_workflows', 'implementation_document_drafts', 'handoffs',
    'handoff_inputs', 'handoff_progress', 'decisions', 'codex_reasoning',
    'transcripts', 'context_snapshots', 'context_commits', 'context_receipts')
LEGACY_TARGET_CONFIG = {'implementation_indexed_review_protocol': 1,
    'implementation_review_strategy': 'whole_alignment',
    'implementation_document_protocol': 2,
    'implementation_document_authoring_protocol': 1,
    'implementation_review_max_revisions': None,
    'implementation_review_max_seconds': None,
    'implementation_review_max_calls': None,
    'implementation_review_time_basis': 'per_revision_review'}
TARGET_CONFIG = {**LEGACY_TARGET_CONFIG, 'implementation_review_strategy': 'acceptance_contract',
    'implementation_review_max_revisions': 2, 'implementation_review_max_seconds': 14400,
    'implementation_review_max_calls': 240, 'implementation_review_time_basis': 'policy_active_time'}
WALL_TARGET_CONFIG = {**TARGET_CONFIG, 'implementation_review_time_basis': 'policy_wall_time'}
ARCHIVE_ID_FIELDS = ('archived_implementation_handoff_ids', 'archived_implementation_artifact_ids')
IMPLEMENTATION_PURPOSES = ('implementation', 'implementation_spec', 'spec_review',
    'implementation_acceptance_contract', 'implementation_acceptance_review',
    'implementation_acceptance_response', 'implementation_acceptance_adjudication',
    'implementation_acceptance_recheck',
    'implementation_document_section', 'implementation_document_sections_v1',
    'implementation_document_review', 'code_review')
IMPLEMENTATION_DOCUMENT_KINDS = ('implementation_specification', 'implementation_review_response', 'implementation_revision_diff',
                                 'implementation_review_history')


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked(message)


def _sha(value):
    from .engine import digest
    return digest(value)


def _get(db, collection, key, default=None):
    row = db.execute('SELECT data FROM records WHERE collection=? AND key=?',
                     (collection, key)).fetchone()
    return json.loads(row['data']) if row else default


def _keys(db, collection, run_id, *, predicate='1', parameters=()):
    """Scoped metadata only; retain the legacy root-key ownership fallback."""
    from .storage import Store
    selector = Store.__new__(Store)
    from contextlib import contextmanager
    @contextmanager
    def connection():
        yield db
    selector._read_db = connection
    keys = set(selector.record_keys(collection, run_id, predicate=predicate, parameters=parameters))
    if run_id is not None and db.execute('SELECT 1 FROM records WHERE collection=? AND key=? AND (' + predicate + ')',
                  (collection, run_id, *parameters)).fetchone():
        keys.add(run_id)
    return sorted(keys)


def _activity_archive_ids(db, run_id):
    """Archive exact implementation IDs without loading any paid record body.

    A scientific input may live under an implementation directory. Its path or
    use as a dependency never makes it an implementation document.
    """
    purpose = "json_extract(data,'$.purpose')"
    head = f"substr({purpose},1,instr({purpose} || ':',':')-1)"
    admitted = ','.join('?' for _ in IMPLEMENTATION_PURPOSES)
    kinds = ','.join('?' for _ in IMPLEMENTATION_DOCUMENT_KINDS)
    return {'archived_implementation_handoff_ids': _keys(db, 'handoffs', run_id,
        predicate=f"json_extract(data,'$.stage')='implementation' OR {head} IN ({admitted})",
        parameters=IMPLEMENTATION_PURPOSES),
        'archived_implementation_artifact_ids': _keys(db, 'artifacts', run_id,
        predicate=f"json_extract(data,'$.kind') IN ({kinds})", parameters=IMPLEMENTATION_DOCUMENT_KINDS)}


def _raw_rows(db, collection, run_id):
    for key in _keys(db, collection, run_id):
        row = db.execute('SELECT data FROM records WHERE collection=? AND key=?',
                         (collection, key)).fetchone()
        yield key, row['data']


def _rows(db, collection, run_id):
    for key, raw in _raw_rows(db, collection, run_id):
        yield key, json.loads(raw)


def _truthy(path):
    # Exact JSON equivalents of Python's truthiness without projecting bodies.
    return (f"CASE json_type(data,'{path}') "
        f"WHEN 'array' THEN json_array_length(data,'{path}') > 0 "
        f"WHEN 'object' THEN EXISTS(SELECT 1 FROM json_each(data,'{path}')) "
        f"WHEN 'text' THEN length(json_extract(data,'{path}')) > 0 "
        f"ELSE coalesce(json_extract(data,'{path}') != 0,0) END")


def _workflow_headers(db, run_id):
    headers = []
    for key in _keys(db, 'codex_workflows', run_id):
        row = db.execute("SELECT json_type(data,'$.cycle') cycle_type,"
            "json_extract(data,'$.cycle') cycle," + _truthy('$.attempts') + ' attempted,'
            + _truthy('$.result') + ' delivered FROM records '
            "WHERE collection='codex_workflows' AND key=?", (key,)).fetchone()
        if row['cycle_type'] not in {None, 'integer', 'real'}:
            _fail('原实现工作流周期格式异常；不能重开后绕过原记录。')
        headers.append({'id': key, 'cycle': 1 if row['cycle_type'] is None else row['cycle'],
                        'attempted': row['attempted'], 'delivered': row['delivered']})
    return headers


def _bindings(db, run_id):
    identities = [('consensus', run_id), ('steps', run_id + ':debate'),
                  ('steps', run_id + ':design')]
    identities += [(collection, key) for collection in SCIENTIFIC_COLLECTIONS
                   for key in _keys(db, collection, run_id)]
    return [{'collection': collection, 'key': key,
             'sha256': _sha(_get(db, collection, key))}
            for collection, key in identities]


def _files(engine, db, run_id):
    """Bind original files in place; never move or delete an evidence file."""
    root = (Path(engine.data_dir) / 'runs' / run_id).resolve()
    files = []
    for key, artifact in _rows(db, 'artifacts', run_id):
        name = artifact.get('name', '')
        kind = artifact.get('kind', '')
        if not ('/implementation/' in '/' + name
                or kind.startswith('implementation_') or kind == 'codex_delivery'):
            continue
        path = Path(artifact.get('path', ''))
        if not path.is_absolute() or not path.resolve().is_relative_to(root):
            _fail('实现归档发现不属于本任务的文件；原记录保持不变。')
        if not path.is_file() or path.is_symlink():
            _fail('实现归档原文件不存在或不是普通任务文件；原记录保持不变。')
        from .deletion import _plain_path
        _plain_path(path, Path(engine.data_dir).resolve())
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        # Markdown artifacts bind canonical Unicode text; on Windows their
        # physical bytes may contain CRLF. Retain both actual byte and original
        # document hashes rather than changing either source contract.
        canonical = digest
        if path.suffix == '.md':
            canonical = hashlib.sha256(path.read_text(encoding='utf-8').encode()).hexdigest()
        if artifact.get('sha256') and artifact['sha256'] != canonical:
            _fail('实现归档原文件与已保存SHA不一致；原记录保持不变。')
        files.append({'artifact_id': key, 'path': str(path.resolve()),
                      'sha256': digest, 'document_sha256': canonical, 'bytes': len(content)})
    return files


def _native_guard(db, run_id):
    """A stopped worker does not prove an original dispatched turn stopped."""
    for row in db.execute("SELECT key,json_extract(data,'$.selection.purpose') purpose,"
            "json_extract(data,'$.stage') stage,json_type(data,'$.checkpoint') checkpoint_type,"
            "json_extract(data,'$.checkpoint.status') status FROM records "
            "WHERE collection='codex_reasoning' AND json_extract(data,'$.run_id')=?", (run_id,)):
        purpose = row['purpose'] or ''
        if not (purpose.startswith(('implementation', 'codex_'))
                or row['stage'] == 'implementation'):
            continue
        if row['checkpoint_type'] != 'object' or row['status'] not in {
                'completed', 'failed', 'interrupted', 'cancelled'}:
            _fail('原实现原生请求仍无确定终态；须先核实原会话，不能清理后另派请求。')


def _snapshot(engine, db, run_id):
    run = _get(db, 'runs', run_id, {})
    if run.get('status') not in {'paused', 'blocked', 'failed'}:
        _fail('重开实现文档前须暂停任务并等待工作进程停止。')
    if run.get('stage') not in {'implementation', 'revision'}:
        _fail('只可从尚未交付代码的实现阶段重开实现文档。')
    if _get(db, 'runtime', 'worker', {}).get('held'):
        _fail('工作进程仍持有执行租约；不能重开实现文档。')
    if any(not task.done() for task in getattr(engine, 'tasks', {}).values()):
        _fail('工作进程仍未停止；不能重开实现文档。')
    cycle = run.get('state', {}).get('research_cycle', 1)
    design = _get(db, 'steps', run_id + ':design', {})
    consensus = _get(db, 'consensus', run_id, {})
    if (design.get('status') != 'completed' or design.get('cycle', 1) != cycle
            or consensus.get('cycle', 1) != cycle or consensus.get('unanimous') is not True
            or _sha(consensus.get('proposal')) != consensus.get('proposal_hash')):
        _fail('缺少同一科研周期已保存架构与批准方案，不能重开实现文档。')
    workflows = _workflow_headers(db, run_id)
    if any(row['attempted'] or row['delivered'] for row in workflows if row['cycle'] >= cycle):
        _fail('已有实际代码交付尝试；此入口只清理代码派发前的实现文档。')
    for collection in ('programs', 'experiments', 'codex_checks', 'codex_check_attempts',
                       'experiment_attempts', 'revision_plans'):
        if any(row.get('research_cycle', row.get('cycle', 1)) >= cycle
               for _, row in _rows(db, collection, run_id)):
            _fail('已有代码、实验或科研修订记录，不能使用实现文档重开入口。')
    for stage in ('experiments', 'tuning', 'validation_review', 'final_tests', 'report'):
        if _get(db, 'steps', run_id + ':' + stage):
            _fail('已有后续阶段检查点；不能使用代码派发前的文档重开入口。')
    _native_guard(db, run_id)
    prefix = run_id + ':implementation-'
    implementation_contexts = _keys(db, 'stage_contexts', None,
        predicate='key=? OR substr(key,1,?)=?',
        parameters=(run_id + ':implementation', len(prefix), prefix))
    active = [('steps', run_id + ':implementation'),
              *[('stage_contexts', key) for key in implementation_contexts],
              *[('contexts', run_id + ':' + role) for role in ROLES]]
    cycle_key = f'{run_id}:{cycle}'
    failure = _get(db, 'research_cycles', cycle_key)
    if failure:
        if (failure.get('failure_stage') != 'implementation'
                or failure.get('next_cycle') or failure.get('next_proposal_hash')):
            _fail('当前科研周期已推进或失败来源不是实现文档；不能清理其科研记录。')
        active += [('research_cycles', cycle_key), ('steps', run_id + ':revision'),
                   ('stage_contexts', run_id + ':revision')]
    original = [{'collection': collection, 'key': key, 'value': value}
                for collection, key in active
                if (value := _get(db, collection, key)) is not None]
    paid = []
    for collection in PAID_COLLECTIONS:
        # Hash original SQLite JSON without copying every paid transcript into
        # another giant archive. Original records stay in their collections.
        for key, raw in _raw_rows(db, collection, run_id):
            paid.append({'collection': collection, 'key': key,
                'stored_json_sha256': hashlib.sha256(raw.encode()).hexdigest()})
    return {'version': 1, 'run_id': run_id, 'cycle': cycle, 'run': run,
        'approved': _approved(engine, run_id, db),
        'active_records': original, 'bindings': _bindings(db, run_id),
        'target_config': copy.deepcopy(TARGET_CONFIG),
        **_activity_archive_ids(db, run_id),
        'removed_config_fields': {key: copy.deepcopy(run['config'][key])
            for key in sorted(RETIRED_IMPLEMENTATION_REVIEW_FIELDS) if key in run['config']},
        'archived_workflow_ids': [row['id'] for row in workflows],
        'workflow_bindings': [{'id': row['key'], 'stored_json_sha256': row['stored_json_sha256']}
            for row in paid if row['collection'] == 'codex_workflows'],
        'paid_records': paid, 'source_files': _files(engine, db, run_id),
        'budget_sha256': _sha(_get(db, 'budgets', run_id)),
        'graph_status': _get(db, 'graph_status', run_id),
        'approved_common_sha256': _sha(_get(db, 'stage_contexts', run_id + ':implementation'))}


def _approved(engine, run_id, db):
    """Reuse real proposal/architecture/plan guards; no approval is fabricated."""
    from .architecture_validation import operation
    from .architecture_documents import gate
    from .storage import _ReadSnapshot
    engine = copy.copy(engine)
    source_store = engine.store
    engine.store = _ReadSnapshot(source_store, db)
    if getattr(getattr(engine, 'sources', None), 'store', None) is source_store:
        engine.sources = copy.copy(engine.sources)
        engine.sources.store = engine.store
    @operation
    def check():
        engine._consensus_gate(run_id)
        outputs = engine.outputs(run_id)
        gate(engine, run_id, outputs.get('design'))
        return {'proposal_hash': _sha(engine.store.get('consensus', run_id)['proposal']),
                'design_hash': _sha(outputs.get('design')),
                'experiment_plan_hash': _sha(engine.experiment_plan(run_id))}
    return check()


def preview(engine, run_id):
    with engine.store._read_db() as db:
        db.execute('BEGIN')
        snapshot = _snapshot(engine, db, run_id)
    return {'run_id': run_id, 'cycle': snapshot['cycle'], 'expect_sha': _sha(snapshot),
        'target_config': snapshot['target_config'],
        'removed_config_fields': snapshot['removed_config_fields'],
        **{key: snapshot[key] for key in ARCHIVE_ID_FIELDS},
        'approved': snapshot['approved'], 'archived_workflow_ids': snapshot['archived_workflow_ids'],
        'source_file_count': len(snapshot['source_files']),
        'retained_paid_record_count': len(snapshot['paid_records']),
        'scientific_approval': False, 'execution_authorized': False}


def _put(store, db, collection, key, value):
    store._guard_write(db, collection, key, value)
    db.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(collection,key) '
        'DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
        (collection, key, json.dumps(value, ensure_ascii=False, allow_nan=False), now()))


def _archive_path(engine, run_id, identity):
    from .deletion import _plain_path
    root = Path(engine.data_dir).resolve()
    path = root / 'runs' / run_id / 'implementation-restarts' / _sha(identity)[:16] / 'archive.json'
    _plain_path(path, root)
    if not path.resolve().is_relative_to(root / 'runs' / run_id):
        _fail('实现重开归档目录不属于本任务。')
    return path.resolve()


def _validate(engine, db, run_id, receipt):
    if (not isinstance(receipt, dict) or receipt.get('version') != 1
            or receipt.get('run_id') != run_id
            or _sha({k: v for k, v in receipt.items() if k != 'sha256'}) != receipt.get('sha256')
            or receipt.get('scientific_approval') is not False
            or receipt.get('execution_authorized') is not False):
        _fail('实现重开回执缺失或完整性校验失败；不得复用旧实现。')
    generation = _sha({'id': receipt['id'], 'expect_sha': receipt['expect_sha']})
    if (receipt.get('source') != 'explicit_user_request'
            or receipt.get('generation') != generation
            or receipt.get('graph_thread_id') != f'{run_id}:implementation:{generation}'):
        _fail('实现重开generation不来自真实归档身份；不得复用旧实现。')
    path = _archive_path(engine, run_id, receipt['id'])
    if receipt.get('archive_path') != str(path) or not path.is_file():
        _fail('实现重开归档缺失或不属于本任务；保留原记录。')
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != receipt.get('archive_sha256'):
        _fail('实现重开归档SHA发生变化；保留原记录。')
    archived = json.loads(content)
    for field in ARCHIVE_ID_FIELDS:
        # Old receipts did not carry these navigation lists. Keep their real
        # archive and hash valid; consumers may use metadata compatibility.
        if field not in receipt and field not in archived:
            continue
        values = receipt.get(field)
        if (not isinstance(values, list) or values != sorted(set(item for item in values if isinstance(item, str)))
                or any(not item for item in values) or archived.get(field) != values):
            _fail('实现归档活动入口清单缺失或绑定已改变；保留原记录。')
    if (_sha(archived) != receipt.get('expect_sha')
            or archived.get('bindings') != receipt.get('bindings')
            or archived.get('approved') != receipt.get('approved')
            or archived.get('archived_workflow_ids') != receipt.get('archived_workflow_ids')
            or _bindings(db, run_id) != receipt['bindings']):
        _fail('重开绑定的批准架构、方案或冻结计划发生变化；不得沿用回执。')
    run = _get(db, 'runs', run_id, {})
    state = run.get('state', {})
    target = receipt.get('target_config')
    if target is not None:
        if _sha(target) not in {_sha(TARGET_CONFIG), _sha(WALL_TARGET_CONFIG), _sha(LEGACY_TARGET_CONFIG)} or archived.get('target_config') != target:
            _fail('重开后的索引式全文审核协议配置与回执不一致；不得复用旧实现。')
        from .implementation_review_clock import effective_restart_config
        try:
            effective = effective_restart_config(db, run, target)
        except ValueError as exc:
            _fail(str(exc))
        if any(k not in run.get('config', {}) or type(run['config'][k]) is not type(value)
               or run['config'][k] != value for k, value in effective.items()):
            _fail('重开后的索引式全文审核协议配置与回执不一致；不得复用旧实现。')
    if target is not None:
        removed = receipt.get('removed_config_fields')
        expected_removed = {key: archived.get('run', {}).get('config', {})[key]
            for key in sorted(RETIRED_IMPLEMENTATION_REVIEW_FIELDS)
            if key in archived.get('run', {}).get('config', {})}
        if (not isinstance(removed, dict) or _sha(removed) != _sha(expected_removed)
                or archived.get('removed_config_fields') != removed
                or RETIRED_IMPLEMENTATION_REVIEW_FIELDS.intersection(run.get('config', {}))):
            _fail('重开后的实现配置仍包含旧审查约束或清理绑定不一致；不得重新启用旧规则。')
    if (state.get(MARKER) != receipt['id'] or state.get(GENERATION) != receipt['generation']
            or state.get(GRAPH_THREAD) != receipt['graph_thread_id']
            or state.get('research_cycle', 1) != receipt['cycle']):
        _fail('活动实现generation与真实重开回执不一致；不得命中旧工作流。')
    return receipt


def restart(engine, run_id, *, expect_sha, idempotency_key):
    """Archive then atomically exit active pointers; never admit new work."""
    if (not isinstance(expect_sha, str) or not re.fullmatch(r'[a-f0-9]{64}', expect_sha)
            or not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128):
        raise ValueError('重开实现需要有效的预览SHA与幂等标识。')
    identity = f'{run_id}:implementation-restart:{_sha(idempotency_key)}'
    with engine.store._read_db() as db:
        previous = _get(db, COLLECTION, identity)
        if previous:
            if previous.get('expect_sha') != expect_sha:
                _fail('同一幂等标识绑定了其他重开预览；不能重复清理。')
            return copy.deepcopy(_validate(engine, db, run_id, previous))
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        engine.store._guard_run(db, run_id)
        if _get(db, COLLECTION, identity):
            _fail('重开请求在准备期间已由另一操作提交；请重读已有回执。')
        snapshot = _snapshot(engine, db, run_id)
        if _sha(snapshot) != expect_sha:
            _fail('任务、原请求或文件在重开预览后发生变化；须重新核对。')
        path = _archive_path(engine, run_id, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                             allow_nan=False, indent=2).encode()
        if path.exists():
            if path.read_bytes() != content:
                _fail('已有重开归档与本次原记录不同；原入口保持不变。')
            # A prior failed fsync/SQL transaction may have left this exact
            # file. Prove durable storage again before clearing active pointers.
            with path.open('ab') as stream:
                stream.flush()
                import os
                os.fsync(stream.fileno())
        else:
            with path.open('xb') as stream:
                stream.write(content)
                stream.flush()
                import os
                os.fsync(stream.fileno())
        generation = _sha({'id': identity, 'expect_sha': expect_sha})
        receipt = {'id': identity, 'run_id': run_id, 'version': 1,
            'cycle': snapshot['cycle'], 'source': 'explicit_user_request',
            'generation': generation, 'graph_thread_id': f'{run_id}:implementation:{generation}',
            'expect_sha': expect_sha, 'archive_path': str(path),
            'archive_sha256': hashlib.sha256(content).hexdigest(),
            'bindings': snapshot['bindings'], 'approved': snapshot['approved'],
            'target_config': snapshot['target_config'],
            'removed_config_fields': snapshot['removed_config_fields'],
            **{key: snapshot[key] for key in ARCHIVE_ID_FIELDS},
            'archived_workflow_ids': snapshot['archived_workflow_ids'],
            'created_at': now(), 'scientific_approval': False, 'execution_authorized': False,
            'paid_evidence_retained': True}
        receipt['sha256'] = _sha(receipt)
        run = snapshot['run']
        state = {k: v for k, v in run.get('state', {}).items() if k not in {
            'resume_generation', 'implementation_specification_return',
            'protocol_review_return', 'research_brief_restart'}}
        state.update({MARKER: identity, GENERATION: generation, GRAPH_THREAD: receipt['graph_thread_id']})
        from .orchestration import STAGES
        current = {k: v for k, v in run.items() if k not in {'owner_pid', 'owner_started', 'owner_instance'}}
        current.update(status='paused', stage='implementation', error=None,
            progress=round(STAGES.index('implementation') / len(STAGES) * 100),
            config={**{k: v for k, v in run['config'].items()
                if k not in RETIRED_IMPLEMENTATION_REVIEW_FIELDS}, **snapshot['target_config']},
            state=state, updated_at=now())
        _put(engine.store, db, COLLECTION, identity, receipt)
        _put(engine.store, db, 'runs', run_id, current)
        for row in snapshot['active_records']:
            db.execute('DELETE FROM records WHERE collection=? AND key=?',
                       (row['collection'], row['key']))
        _put(engine.store, db, 'graph_status', run_id, {'id': run_id, 'run_id': run_id,
            'cycle': snapshot['cycle'], 'node': 'implementation', 'status': 'waiting',
            'waiting_for': 'implementation_restart', 'restart_id': identity,
            'generation': generation, 'updated_at': now()})
        db.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
            (run_id, 'implementation.restarted', '原实现文档及付费证据已归档；准备从已批准架构重新编写实现文档，尚未派发代码。',
             json.dumps({'restart_id': identity, 'generation': generation,
                'scientific_approval': False, 'execution_authorized': False}), now()))
        return copy.deepcopy(receipt)


def identity_generation(engine, run_id, common):
    """Only a real durable restart receipt may change deterministic identity."""
    from .implementation_code_recovery import active
    recovered = active(engine, run_id, common)
    if recovered:
        return recovered['implementation_generation']
    with engine.store._read_db() as db:
        run = _get(db, 'runs', run_id, {})
        state = run.get('state', {})
        if not any(state.get(k) for k in (MARKER, GENERATION, GRAPH_THREAD)):
            return None
        receipt = _validate(engine, db, run_id, _get(db, COLLECTION, state.get(MARKER, '')))
        if (_sha(common.get('proposal')) != receipt['approved']['proposal_hash']
                or _sha(common.get('design')) != receipt['approved']['design_hash']
                or _sha(common.get('experiment_plan')) != receipt['approved']['experiment_plan_hash']):
            _fail('新实现输入不再是回执绑定的批准架构、方案或冻结计划。')
        return receipt['generation']


def graph_identity(engine, run_id):
    from .implementation_code_recovery import active
    recovered = active(engine, run_id)
    if recovered:
        return recovered['graph_thread_id']
    with engine.store._read_db() as db:
        run = _get(db, 'runs', run_id, {})
        state = run.get('state', {})
        if not any(state.get(k) for k in (MARKER, GENERATION, GRAPH_THREAD)):
            return run_id
        return _validate(engine, db, run_id,
                         _get(db, COLLECTION, state.get(MARKER, '')))['graph_thread_id']
