"""Return an interrupted implementation to the last approved specification."""
from __future__ import annotations

import copy
from pathlib import Path

from .implementation_restart import _approved, _bindings, _fail, _get, _rows, _sha

MODE = 'before_codex'
AUTHOR_MODE = 'before_document'
TERMINAL = {'completed', 'interrupted', 'failed', 'cancelled'}
REVIEW_PREFIXES = ('implementation_code_review:', 'code_review', 'code_repair', 'code_failure_review')


def workflow(engine, db, run_id):
    run = _get(db, 'runs', run_id, {})
    state = run.get('state', {})
    cycle = state.get('research_cycle', 1)
    if (run.get('status') != 'paused' or run.get('stage') != 'implementation'
            or _get(db, 'runtime', 'worker', {}).get('held')
            or any(not task.done() for task in engine.tasks.values())):
        _fail('回退写码前需要暂停当前实现并释放工作进程。')
    if any(any(True for _ in _rows(db, collection, run_id)) for collection in
           ('programs', 'experiments', 'experiment_attempts')):
        _fail('存在正式程序或实验记录，不能清理其源码。')
    matches = [row for _, row in _rows(db, 'codex_workflows', run_id)
               if row.get('cycle') == cycle and row.get('kind') == 'implementation'
               and row.get('implementation_generation') == state.get('implementation_generation')]
    if len(matches) != 1 or not matches[0].get('attempts') or matches[0].get('result'):
        _fail('当前控制器没有唯一可回退的写码工作流。')
    return run, matches[0]


def review_handoffs(db, run_id, source, *, mode=MODE):
    owner = _sha(source['id'])
    selected = {}
    for key, row in _rows(db, 'handoffs', run_id):
        purpose = row.get('purpose', '')
        from .implementation_restart import IMPLEMENTATION_PURPOSES
        prefixes = (*REVIEW_PREFIXES, *IMPLEMENTATION_PURPOSES, 'implementation_scope_review') if mode == AUTHOR_MODE else REVIEW_PREFIXES
        if not purpose.startswith(prefixes):
            continue
        packet = _get(db, 'handoff_inputs', key, {})
        context = packet.get('context', {})
        if (owner not in purpose.split(':') and source['id'] not in {
                context.get('workflow_id'), (context.get('code_repair_scope') or {}).get('workflow_id'),
                (context.get('implementation_document_authoring') or {}).get('workflow_id'),
                (context.get('implementation_scope_review') or {}).get('workflow_id'),
                context.get('acceptance_workflow_id')}):
            continue
        from .engine import Harness
        fields = {name: packet.get(name) for name in ('instruction', 'context', 'schema')}
        if (row.get('run_id') != run_id or packet.get('run_id') != run_id
                or _sha(fields) != row.get('input_hash')
                or Harness.decision_key(run_id, row['to_role'], purpose, **fields) != key
                or row.get('status') not in TERMINAL | {'blocked'}):
            _fail('旧评审交接身份无效或仍在执行。')
        selected[key] = {'purpose': purpose, 'status': row['status'],
                         'input_sha256': row['input_hash'],
                         'progress_sha256': _sha(_get(db, 'handoff_progress', key))}
    return selected


def unsettled(db, run_id, handoffs):
    pending = {}
    for key, row in _rows(db, 'codex_reasoning', run_id):
        owner = (row.get('memory_capture_binding') or {}).get('handoff_id')
        if owner not in handoffs:
            continue
        checkpoint = row.get('checkpoint') or {}
        if checkpoint.get('status') in TERMINAL:
            continue
        if (checkpoint.get('status') != 'unknown' or not checkpoint.get('thread_id')
                or not checkpoint.get('turn_id') or checkpoint.get('operation_id') != key
                or checkpoint.get('run_id') != run_id or not checkpoint.get('reasoning_only')
                or row.get('role') not in {'director', 'engineer', 'scout'}):
            _fail('旧评审仍存在无法核实的原生请求。')
        pending[key] = checkpoint
    for _, row in _rows(db, 'tool_invocations', run_id):
        if row.get('handoff_id') in handoffs and row.get('status') in {'running', 'unknown'}:
            _fail('旧评审仍有未结束的工具调用。')
    for key in handoffs:
        progress = _get(db, 'handoff_progress', key, {})
        if any(call.get('name') not in {'code.locate', 'code.read', 'artifacts.list', 'artifacts.read',
                'documents.read', 'documents.search', 'documents.list', 'documents.recall'}
               for call in progress.get('pending_tool_calls', [])[progress.get('tool_cursor', 0):]):
            _fail('旧评审仍有未对账的写入工具。')
    return pending


def approval_source(engine, db, run_id, source):
    from .codex_workflow import _validate_approved_spec
    from .implementation_documents import validate_review
    from .storage import _ReadSnapshot
    matches = [row for row in source['specifications'] if row.get('approved') and row['spec_hash'] == source.get('spec_hash')]
    if len(matches) != 1:
        _fail('无法唯一定位已经批准的实现规范。')
    spec = matches[0]
    from .implementation_cycle import active
    if not active(spec):
        _validate_approved_spec(spec, source['proposal_hash'])
    from .implementation_restart import COLLECTION
    visited = set()
    while source.get('pre_codex_spec_source') and not spec.get('implementation_definition_repair_protocol'):
        source_id = source['pre_codex_spec_source']
        if source_id == source['id'] or source_id in visited:
            _fail('实现规范继承形成循环。')
        visited.add(source['id'])
        receipts = [row for _, row in _rows(db, COLLECTION, run_id)
                    if row.get('generation') == source.get('implementation_generation') and row.get('mode') == MODE]
        if len(receipts) != 1:
            _fail('缺少继承实现规范的原回退回执。')
        receipt = receipts[0]
        ancestor = _get(db, 'codex_workflows', source_id, {})
        if (receipt.get('sha256') != _sha({k: value for k, value in receipt.items() if k != 'sha256'})
                or receipt.get('source_workflow_id') != source_id
                or receipt.get('source_workflow_sha256') != _sha(ancestor)
                or receipt.get('specification_sha256') != spec['spec_hash']
                or ancestor.get('run_id') != run_id or spec not in ancestor.get('specifications', [])):
            _fail('继承的实现规范与原回退批准绑定不一致。')
        source = ancestor
    reader = copy.copy(engine)
    class ApprovalSnapshot(_ReadSnapshot):
        def get(self, collection, key, default=None):
            row = super().get(collection, key, default)
            if collection == 'runs' and key == run_id:
                # Read-only view of the generation in which these original
                # author receipts were accepted. Writes use the live guard.
                state = {**row.get('state', {}), 'implementation_generation': source.get('implementation_generation')}
                return {**row, 'state': state}
            return row

    reader.store = ApprovalSnapshot(engine.store, db)
    if getattr(getattr(engine, 'sources', None), 'store', None) is engine.store:
        reader.sources = copy.copy(engine.sources)
        reader.sources.store = reader.store
    validate_review(reader, run_id, source, spec)
    return spec


def roots(engine, run_id, source):
    from .codex_workspace import checked, OWNER
    from .deletion import _plain_path
    workspace = checked(source['project_root'], run_id, source['workspace'])
    marker = workspace.parent / OWNER
    if marker.is_file():
        import json
        if json.loads(marker.read_bytes()).get('workflow_id') != source['id']:
            _fail('旧写码目录的所有权与控制器不同。')
    elif workspace.parent.name != _sha(source['id']):
        _fail('旧写码目录没有同一工作流的所有权凭据。')
    inputs = workspace.parent / 'inputs'
    _plain_path(inputs, Path(source['project_root']))
    snapshot_root = engine.run_dir(run_id) / 'codex' / _sha(source['id'])
    _plain_path(snapshot_root, Path(engine.data_dir))
    return (workspace, inputs, snapshot_root)


def resolve_file(engine, run_id, source, ref):
    from .deletion import _plain_path
    path = Path(ref['path'])
    if ref.get('code_root') is not None:
        owner = roots(engine, run_id, source)[ref['code_root']]
    else:
        owner = engine.run_dir(run_id)
    _plain_path(path, owner)
    if not path.is_absolute() or path == owner or not path.resolve().is_relative_to(owner):
        _fail('写码回退文件越出已验证的任务目录。')
    return path


def file_inventory(engine, db, run_id, source):
    from .delivery_files import identity, regular_files
    result = {}
    owned = roots(engine, run_id, source)
    for _, row in _rows(db, 'codex_workflows', run_id):
        if row['id'] == source['id']:
            continue
        other = row.get('workspace')
        if other and any(Path(other).resolve().is_relative_to(root) or root.is_relative_to(Path(other).resolve())
                         for root in owned):
            _fail('另一个工作流还在使用旧写码目录。')
    for index, root in enumerate(owned):
        if not root.exists():
            continue
        for path in regular_files(root, skip_git=False):
            value = str(path)
            result[value] = {'artifact_id': 'retired-file:' + _sha(value), 'path': value,
                             'code_root': index, **identity(path)}
    return result


def snapshot(engine, db, run_id, *, mode=MODE):
    from .code_review_partition import source_plan
    from .codex_workflow import ROLES, _verify_snapshot
    from .implementation_stage_cleanup import inventory
    from .implementation_restart import ARCHIVE_ID_FIELDS

    run, source = workflow(engine, db, run_id)
    for attempt in source['attempts']:
        checkpoint = attempt.get('checkpoint') or {}
        if (checkpoint.get('status') not in TERMINAL or checkpoint.get('operation_id') != attempt['operation_id']
                or checkpoint.get('run_id') != run_id or checkpoint.get('workspace') != source['workspace']
                or checkpoint.get('instruction_sha256') != attempt.get('instruction_sha256')):
            _fail('旧写码请求缺少绑定的已结束回执。')
        if attempt.get('snapshot'):
            _verify_snapshot(engine, run_id, attempt['snapshot'])
    if source['current_revision'] not in {attempt['index'] for attempt in source['attempts']}:
        _fail('当前写码游标与旧交付不一致。')
    spec = approval_source(engine, db, run_id, source)
    if mode == MODE:
        from . import implementation_scope_review as scope_review
        if source.get(scope_review.ARCHITECTURE_FIELD):
            scope_review.authenticate_release(engine, run_id, source, spec, ROLES)
        else:
            source_plan(source['id'], 0, spec, ROLES)
    handoffs = review_handoffs(db, run_id, source, mode=mode)
    pending = unsettled(db, run_id, handoffs)
    pointers = [{'collection': c, 'key': k} for c, k in (
        ('steps', run_id + ':implementation'), ('cycle_steps', f'{run_id}:{source["cycle"]}:implementation'))]
    purposes = {row['purpose'] for row in handoffs.values()}
    for role in ROLES:
        key = run_id + ':' + role
        context = _get(db, 'contexts', key, {})
        transcript = _get(db, 'transcripts', context.get('transcript_id'), {})
        if (transcript.get('context', {}).get('_purpose') or transcript.get('request_scope', {}).get('purpose')) in purposes:
            pointers.append({'collection': 'contexts', 'key': key})
    code_files = file_inventory(engine, db, run_id, source)
    if mode == AUTHOR_MODE:
        from .delivery_files import identity
        from .implementation_restart import IMPLEMENTATION_DOCUMENT_KINDS
        document_ids = {row['document']['artifact_id'] for row in
                        [*source['specifications'], *source.get('implementation_scope_revisions', [])]
                        if row.get('document')}
        for key, artifact in _rows(db, 'artifacts', run_id):
            if key in document_ids and artifact.get('kind') in {
                    *IMPLEMENTATION_DOCUMENT_KINDS, 'implementation_scope_revision'}:
                ref = {'artifact_id': key, 'path': artifact['path']}
                path = resolve_file(engine, run_id, source, ref)
                code_files[str(path)] = {**ref, **identity(path)}
    manifest = inventory(engine, db, run_id, handoffs, pointers, code_files=code_files)
    prior = _get(db, 'implementation_restarts', run.get('state', {}).get('implementation_restart'), {})
    archived_ids = {
        ARCHIVE_ID_FIELDS[0]: sorted(set(handoffs) | set(prior.get(ARCHIVE_ID_FIELDS[0], []))),
        ARCHIVE_ID_FIELDS[1]: sorted({row['key'] for row in manifest['records'] if row['collection'] == 'artifacts'}
                                    | set(prior.get(ARCHIVE_ID_FIELDS[1], [])))}
    return {'version': 1, 'mode': mode, 'run_id': run_id, 'cycle': source['cycle'], 'run': run,
            'source_workflow_id': source['id'], 'source_workflow_sha256': _sha(source),
            'specification_sha256': spec['spec_hash'], 'handoffs': handoffs,
            'native_checkpoint_hashes': {key: _sha(row) for key, row in pending.items()},
            'working_data': manifest, 'approved': _approved(engine, run_id, db), 'bindings': _bindings(db, run_id),
            'budget_sha256': _sha(_get(db, 'budgets', run_id)), 'graph_status': _get(db, 'graph_status', run_id),
            'archived_workflow_ids': sorted({source['id'], *prior.get('archived_workflow_ids', [])}), **archived_ids}


async def observe(engine, run_id, *, mode=MODE):
    """Only original threads are read. Receipts and accounting remain immutable."""
    from .local_codex import LocalCodexClient
    with engine.store._read_db() as db:
        db.execute('BEGIN')
        _, source = workflow(engine, db, run_id)
        checkpoints = unsettled(db, run_id, review_handoffs(db, run_id, source, mode=mode))
    observations = {}
    for key, checkpoint in checkpoints.items():
        client = LocalCodexClient(timeout=60, request_timeout=30)
        observed = await client.observe_original_turn(checkpoint)
        if not observed.get('recovered_read_only') or observed.get('status') not in TERMINAL:
            _fail('Codex 原评审回合尚无可核实终态，保留现有文件与上下文。')
        observations[key] = {'checkpoint_sha256': _sha(checkpoint), 'observed': observed}
    return observations


def check_observations(db, run_id, snapshot, observations):
    if set(observations) != set(snapshot['native_checkpoint_hashes']):
        _fail('回退缺少全部待核实原评审回合的终态。')
    for key, item in observations.items():
        checkpoint = _get(db, 'codex_reasoning', key, {}).get('checkpoint')
        row = item['observed']
        if (item['checkpoint_sha256'] != _sha(checkpoint)
                or item['checkpoint_sha256'] != snapshot['native_checkpoint_hashes'][key]
                or row.get('status') not in TERMINAL or not row.get('recovered_read_only')
                or any(row.get(field) != checkpoint.get(field) for field in
                       ('run_id', 'operation_id', 'thread_id', 'turn_id')) or row.get('run_id') != run_id):
            _fail('原评审回合终态与冻结记录不匹配。')


def inherited_spec(engine, run_id, workflow_id):
    from .implementation_restart import COLLECTION, MARKER, _validate
    run = engine.store.get('runs', run_id, {})
    receipt = engine.store.get(COLLECTION, run.get('state', {}).get(MARKER), {})
    if receipt.get('mode') != MODE:
        return None
    with engine.store._read_db() as db:
        db.execute('BEGIN')
        _validate(engine, db, run_id, receipt)
        source = _get(db, 'codex_workflows', receipt['source_workflow_id'], {})
        if not source or source['id'] == workflow_id or _sha(source) != receipt['source_workflow_sha256']:
            _fail('旧实现记录与回退时的源记录不一致。')
        spec = approval_source(engine, db, run_id, source)
        if spec['spec_hash'] != receipt['specification_sha256']:
            _fail('批准规范与回退绑定不一致。')
    return source, spec
