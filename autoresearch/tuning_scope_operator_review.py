"""One explicitly authorized replacement review; retain the ambiguous request.

This is an operator-only repair, never a model tool or automatic retry. Actual
same-role pages may survive an unfinished handoff; its missing vote cannot.
"""
import copy

from .experiment_workflow import sha
from .storage import now

COLLECTION = 'tuning_scope_operator_reviews'
CLAIMS = 'tuning_scope_operator_review_claims'
MARKER = 'tuning_scope_operator_review'


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('一次性调参复核：' + message)


def _key(run_id, role, request):
    from .engine import Harness
    return Harness.decision_key(run_id, role, request['purpose'], request['instruction'],
                               request['context'], request['schema'])


def _input(engine, run_id, role, hid):
    frozen = engine.store.get('handoff_inputs', hid) or {}
    handoff = engine.store.get('handoffs', hid) or {}
    body = {k: frozen.get(k) for k in ('instruction', 'context', 'schema')}
    if (frozen.get('run_id') != run_id or handoff.get('run_id') != run_id
            or handoff.get('to_role') != role or handoff.get('input_hash') != sha(body)
            or _key(run_id, role, {**body, 'purpose': handoff.get('purpose')}) != hid):
        _fail('原读取交接身份不匹配。')
    return frozen, handoff


def prepare(engine, run_id, scope_id, role, native_id, *, expected_plan_sha256, authorization, preview=False):
    """Read-only preflight. It neither starts a model nor alters old records."""
    from . import tuning_scope as scope, tuning_scope_reads as reads, tuning_scope_context as contract
    from .record_archive import fingerprint
    run = engine.store.get('runs', run_id) or {}
    record = engine.store.get(scope.COLLECTION, scope_id) or {}
    allowed_states = {'paused', 'blocked'} | ({'running'} if preview else set())
    if (run.get('status') not in allowed_states or run.get('stage') != 'tuning'
            or record.get('run_id') != run_id or role not in {'engineer', 'scout'}
            or (not isinstance(authorization, str) or not authorization.strip()) and not preview):
        _fail('要求用户明确授权、已暂停或阻塞的同一调参任务。')
    latest = record['rounds'][-1]; plan = latest['author']['answer']
    old = latest['votes'][role]
    if (sha(plan) != expected_plan_sha256 or old.get('answer') or old.get('operator_review')
            or old.get('read_correction') or old.get('contract_clarification')):
        _fail('只适用于当前未答复的原复核，不能替换已有独立判断。')
    hid = _key(run_id, role, old)
    frozen, handoff = _input(engine, run_id, role, hid)
    if engine.store.get('decisions', hid) or handoff.get('output_hash'):
        _fail('已有原始最终答复，必须恢复真实答复而非替换。')
    progress = engine.store.get('handoff_progress', hid) or {}
    native = engine.store.get('codex_reasoning', native_id) or {}
    checkpoint = native.get('checkpoint') or {}
    binding = native.get('memory_capture_binding') or {}
    if (native.get('run_id') != run_id or native.get('role') != role
            or binding.get('handoff_id') != hid or native.get('result')
            or checkpoint.get('thread_id') or checkpoint.get('turn_id')
            or checkpoint.get('error_code') != 'codex_dispatch_without_thread_id'
            or progress.get('normal_request', {}).get('status') != 'blocked'):
        _fail('缺会话ID的原始未知回执不匹配，未安排新请求。')
    # Authenticate tools from completed native turns of this unfinished handoff.
    # A stopped assignment is not a vote, but real earlier tool calls still exist.
    requested, native_sources = set(), []
    for call in handoff.get('model_calls', []):
        tid = call.get('transcript_id', '')
        if not tid.endswith(':output'):
            continue
        key = tid[:-7]; result = engine.store.get('codex_reasoning', key) or {}
        output = result.get('result') or {}
        if (result.get('run_id') != run_id or result.get('role') != role
                or result.get('memory_capture_binding', {}).get('handoff_id') != hid
                or output.get('request_id') != call.get('request_id') or output.get('transcript_id') != tid):
            _fail('原读取对应的已完成模型回执无法验证。')
        requested.update(sha(c) for c in output.get('json', {}).get('tool_calls', []))
        native_sources.append(fingerprint(engine.store, 'codex_reasoning', key))
    ctx = contract.context(engine, run_id, old['context'])
    if sha(reads._snapshot(engine, run_id, ctx).get('tuning_scope_plan')) != expected_plan_sha256:
        _fail('原复核未绑定当前完整计划。')
    directory = reads.retained_index(engine, run_id, role, record, ctx, plan)
    located, refs, origin_inputs = {}, {}, {}

    def include(source_id, row, index):
        source_input, _ = _input(engine, run_id, role, source_id)
        body = reads._replay(engine, run_id, source_input['context'], row)
        if not reads._current_content(engine, run_id, ctx, row, body):
            return
        receipt = row['result']['receipt_sha256']
        origin_inputs[source_id] = sha(source_input)
        located[receipt] = (source_id, source_input['context'], row)
        refs[receipt] = {'handoff_id': source_id, 'index': index, 'row_sha256': sha(row)}

    grouped = {}
    for page in directory['pages']:
        grouped.setdefault(page['handoff_id'], set()).add(page['receipt_sha256'])
    for source_id, receipts in grouped.items():
        for index, row in enumerate((engine.store.get('handoff_progress', source_id) or {}).get('tool_results', [])):
            if (row.get('result') or {}).get('receipt_sha256') in receipts:
                include(source_id, row, index)
    notes = []
    for index, row in enumerate(progress.get('tool_results', [])):
        call, result = row.get('call') or {}, row.get('result') or {}
        if sha(call) not in requested or result.get('error'):
            continue
        if call.get('name') in {'experiments.compare', 'experiments.inspect_module',
                               'experiments.read', 'experiments.analyze', 'hyperparameters.read'}:
            if result.get('receipt_sha256'):
                include(hid, row, index)
        elif call.get('name') == 'experiments.record_notes':
            notes.append(copy.deepcopy(row))
    proof = reads.verify_pages(engine, run_id, hid, ctx, plan, located, set(located), voting=True)
    payload = {'run_id': run_id, 'scope_id': scope_id, 'round_index': len(record['rounds']) - 1,
        'role': role, 'plan_sha256': sha(plan), 'source_handoff_id': hid,
        'source_request_sha256': sha(old), 'source_progress_sha256': sha(progress),
        'source_native': fingerprint(engine.store, 'codex_reasoning', native_id),
        'completed_native_sources': native_sources, 'source_inputs': origin_inputs,
        'reads': refs, 'reading_proof': proof, 'authorization': None if preview else authorization,
        'max_new_model_calls': 1, 'old_request_status': 'unknown_preserved',
        'provider_id': native['reservation']['provider'], 'model': native['reservation']['model'],
        'approval_inherited': False, 'experiment_execution_authorized': False}
    audit_id = run_id + ':tuning-operator-review:' + sha(payload)
    ctx[MARKER] = {'id': audit_id, 'sha256': sha(payload)}
    # Deliver the actual previously read pages again, with their true origins.
    # Do not install them as new tool executions or turn notes into source proof.
    ctx['operator_review_material'] = {
        'pages': [{'source_handoff_id': source_id, **copy.deepcopy(row)}
                  for source_id, _, row in located.values()],
        'previous_working_notes': notes, 'reading_proof': proof,
        'note_status': 'Historical reasoning, not a final vote; reassess using the corrected stage contract.'}
    request = {'purpose': old['purpose'] + ':operator-review:' + sha(payload), 'context': ctx,
        'schema': copy.deepcopy(old['schema']),
        'instruction': ('用户明确允许保留缺少会话ID的未知回执，另外进行一次同角色独立复核。'
            '只对同一份调参范围给出最终判断，不请求工具、不重写计划、不运行实验。'
            'operator_review_material包含本角色已有真实读取的完整分页及原笔记；宿主已逐页核验原来源和必读覆盖。'
            '请阅读全文并复用真正用于当前判断的receipt_sha256，最终read_receipts仍须覆盖全部必读计划、inventory和源码。'
            '原工作笔记中缺失CPU指标、误用120秒预算、把fixed硬编码项当成必须实现的结论，须按补齐的阶段合同重新核对。'
            '不能为了推进而批准；存在实质问题就approve=false并列具体issues，证据不足时明确保留问题。'
            '本次只允许一个最终答复调用；范围批准不等于后续实验执行或科研成功。\n' + contract.GUIDANCE)}
    audit = {'id': audit_id, 'run_id': run_id, 'payload': payload, 'request': request,
        'successor_id': _key(run_id, role, request), 'created_at': now()}
    audit['sha256'] = sha(audit)
    return audit


def register(engine, audit):
    """Install only the additive successor, atomically while execution is held."""
    from . import tuning_scope as scope
    from .implementation_review_clock import _get, _put
    payload = audit['payload']; rid = audit['run_id']
    if not isinstance(payload.get('authorization'), str) or not payload['authorization'].strip():
        _fail('当前仅为预览，尚未取得这一次新复核的用户授权。')
    if sha({k:v for k,v in audit.items() if k != 'sha256'}) != audit.get('sha256'):
        _fail('登记记录摘要不匹配。')
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        run = _get(db, 'runs', rid, {})
        record = _get(db, scope.COLLECTION, payload['scope_id'], {})
        if (run.get('status') not in {'paused', 'blocked'} or run.get('stage') != 'tuning'
                or len(record.get('rounds', [])) != payload['round_index'] + 1):
            _fail('登记期间任务已推进，未更改原复核。')
        old = record['rounds'][-1]['votes'][payload['role']]
        existing = _get(db, COLLECTION, audit['id'])
        if existing == audit and old.get('operator_review') == audit['request']:
            return audit
        if (existing or sha(old) != payload['source_request_sha256']
                or sha(record['rounds'][-1]['author']['answer']) != payload['plan_sha256']
                or sha(_get(db, 'handoff_progress', payload['source_handoff_id'])) != payload['source_progress_sha256']):
            _fail('原请求、计划或已读记录发生变化。')
        old['operator_review'] = copy.deepcopy(audit['request'])
        _put(engine.store, db, COLLECTION, audit['id'], audit, now())
        _put(engine.store, db, scope.COLLECTION, payload['scope_id'], record, now())
    engine.store.event(rid, 'tuning.operator_review_authorized',
        '用户允许一次新的同角色复核；原未知回执保留，复用真实读取，不重跑实验。',
        {'audit_id': audit['id'], 'source_handoff_id': payload['source_handoff_id'],
         'successor_id': audit['successor_id'], 'max_new_model_calls': 1})
    return audit


def _audit(engine, run_id, role, request):
    ref = request.get('context', {}).get(MARKER)
    if not ref:
        return None
    from . import tuning_scope as scope
    audit = engine.store.get(COLLECTION, ref.get('id')) or {}
    p = audit.get('payload') or {}
    if (audit.get('run_id') != run_id or p.get('role') != role
            or sha(p) != ref.get('sha256') or audit.get('sha256') != sha({k:v for k,v in audit.items() if k != 'sha256'})
            or audit.get('request') != {k:request[k] for k in ('purpose', 'instruction', 'context', 'schema')}):
        _fail('新复核不匹配用户授权的冻结请求。')
    record = engine.store.get(scope.COLLECTION, p['scope_id']) or {}
    row = record.get('rounds', [])[p['round_index']]
    old = row['votes'][role]
    if (record.get('run_id') != run_id or sha(row['author']['answer']) != p['plan_sha256']
            or sha({k:v for k,v in old.items() if k != 'operator_review'}) != p['source_request_sha256']
            or _key(run_id, role, old['operator_review']) != audit['successor_id']):
        _fail('原计划或新旧请求绑定改变。')
    return audit


def retained_rows(engine, run_id, role, request):
    audit = _audit(engine, run_id, role, request)
    if audit is None:
        return {}
    from . import tuning_scope_reads as reads
    from .record_archive import fingerprint
    p = audit['payload']; result = {}; cached = {}
    if sha(engine.store.get('handoff_progress', p['source_handoff_id'])) != p['source_progress_sha256']:
        _fail('原未知请求的进度发生变化，须重新核对。')
    for native in [p['source_native'], *p['completed_native_sources']]:
        if fingerprint(engine.store, native['collection'], native['key']) != native:
            _fail('原生请求回执发生变化，未继承未确认的读取。')
    for receipt, locator in p['reads'].items():
        hid = locator['handoff_id']
        if hid not in cached:
            frozen, _ = _input(engine, run_id, role, hid)
            if sha(frozen) != p['source_inputs'][hid]:
                _fail('原读取输入发生变化。')
            cached[hid] = (frozen, (engine.store.get('handoff_progress', hid) or {}).get('tool_results', []))
        frozen, rows = cached[hid]
        row = rows[locator['index']] if len(rows) > locator['index'] else {}
        if sha(row) != locator['row_sha256'] or row.get('result', {}).get('receipt_sha256') != receipt:
            _fail('原读取回执发生变化。')
        body = reads._replay(engine, run_id, frozen['context'], row)
        if not reads._current_content(engine, run_id, request['context'], row, body):
            _fail('原读取已不适用于当前计划或源码。')
        result[receipt] = (hid, frozen['context'], row)
    return result


def activate(harness, run_id, role, key, context, progress):
    if not context.get(MARKER):
        return
    frozen = harness.engine.store.get('handoff_inputs', key) or {}
    handoff = harness.engine.store.get('handoffs', key) or {}
    request = {**frozen, 'purpose': handoff.get('purpose')}
    audit = _audit(harness.engine, run_id, role, request)
    if audit['successor_id'] != key or progress.get('pending_tool_calls') or progress.get('tool_results'):
        _fail('新复核只能直接作最终判断，不能派发工具。')
    harness._enter_final_only(run_id, key, progress,
        '用户授权一次同角色独立复核，原未知回执保留；复用已有读取，禁止新增工具和实验')


def claim(harness, run_id, key, prepared):
    """Durable one-send bound, including schema/HTTP retries and restarts."""
    frozen = harness.engine.store.get('handoff_inputs', key) or {}
    if not frozen.get('context', {}).get(MARKER):
        return False
    handoff = harness.engine.store.get('handoffs', key) or {}
    request = {**frozen, 'purpose': handoff.get('purpose')}
    audit = _audit(harness.engine, run_id, prepared['role'], request)
    retained_rows(harness.engine, run_id, prepared['role'], request)
    provider = harness.intelligence.provider_for(prepared['role'])
    control = prepared['context'].get('_harness_control', {})
    if (provider.get('id') != audit['payload']['provider_id'] or provider.get('model') != audit['payload']['model']
            or control.get('mode') != 'final_only' or not control.get('operation_id')
            or prepared['context'].get('operator_review_material') != frozen['context']['operator_review_material']):
        _fail('供应方、最终答复模式或完整证据包发生变化，未发送。')
    def once(existing):
        if existing:
            _fail('已使用这一次新复核授权；只能恢复该请求的真实回执，不能自动追加调用。')
        return {'id': audit['id'], 'run_id': run_id, 'handoff_id': key,
            'prepared_sha256': sha(prepared), 'operation_id': control['operation_id'], 'claimed_at': now()}
    harness.engine.store.mutate(CLAIMS, audit['id'], once)
    return True
