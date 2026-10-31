"""Bounded experiment reads and audited cleanup of completed attempt caches."""
from __future__ import annotations

import copy
from collections import Counter

from .experiment_workflow import PROTOCOL, encoded, failure, ledger, sha, validation_records
from .storage import redact

GUIDANCE = ('\n实验工作流：先核对 experiments.status 的计划覆盖、失败和预算；'
    '仅按需用 experiments.read 读取单个验证实验或接口检查的参数、指标或诊断，'
    '用 experiments.compare 分页读取本次冻结的统计证据。先取目录和 SHA，再按 SHA 读取。'
    '每批读取后，用 experiments.record_notes 保存带 receipt_sha256 的简短观察、反证和未决问题，'
    '再继续下一批；请求仅保留最近两个原文窗口，旧窗口以明确标记和回执引用归档，需核查时重读。'
    '未写笔记的旧窗口会标为尚待归纳，不能把归档当作已理解。笔记是作者观察，不是宿主验证的事实或审批。'
    '失败、缺失、重复和过期版本不能当作负结果；不丢弃失败种子，不扩大搜索，不用留出集调参。'
    '预算不足、环境/数据问题先保留现场并阻断，不擅自安装依赖或放宽资源限制。'
    '实验完成后可调用 experiments.cleanup(action=preview,trial_id=...) 查看可重建缓存，'
    '再用返回的 preview_id、sha256 和选定的 file_ids 执行 delete。每次最多20个文件；'
    '不删除代码、数据、配置、指标、日志、权重或未知用途文件。清理不能代替归档和诊断。'
    '仅返回本次任务需要的结论及引用；原始运行日志和完整试验台账留在持久化存储。')


def _context(harness, run_id, role, owner, name, args):
    from .tool_authorization import authorize
    authorize(harness, run_id, role, owner, name, args)
    frozen = harness.engine.store.get('handoff_inputs', owner) or {}
    context = frozen.get('context') or {}
    if context.get(PROTOCOL) != 1 or context.get('research_cycle') != harness.engine.cycle(run_id):
        raise ValueError('Experiment tools require the current frozen workflow assignment')
    return context


def _page(value, args):
    text = encoded(value)
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 6000:
        raise ValueError('A bounded character window is required')
    end = min(len(text), offset + limit)
    return {'text': text[offset:end], 'offset': offset, 'end': end, 'total_chars': len(text),
        'next_offset': end if end < len(text) else None, 'complete': offset == 0 and end == len(text),
        'content_sha256': sha(value)}


def status(engine, run_id, args):
    offset, limit = args.get('offset', 0), args.get('limit', 8)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError('Status pagination requires offset>=0 and limit<=25')
    from .dataset_experiments import route, current
    from .dataset_configuration_queue import context_view as queue_view, batch as configuration_batch, budget as queue_budget
    if route(engine, run_id) and (current(engine, run_id) or {}).get('status') != 'ready':
        # Configuration precedes a frozen runnable plan. Report that state and
        # actual accounting instead of treating the missing plan as a tool error.
        from .dataset_transition import current as transition
        run = engine.store.get('runs', run_id)
        with engine.store._read_db() as db:
            attempts = db.execute("SELECT coalesce(sum(coalesce(json_extract(data,'$.attempt'),1)),0) "
                "FROM records WHERE collection='experiments' AND json_extract(data,'$.run_id')=? "
                "AND coalesce(json_extract(data,'$.research_cycle'),1)=?", (run_id, engine.cycle(run_id))).fetchone()[0]
        value = {'phase': 'dataset_configuration', 'plan_status': 'not_frozen', 'complete': False,
            'counts': {}, 'jobs': [], 'planned_trials': None,
            'datasets': [t['dataset'] for t in transition(engine.store, run_id)['request']['targets']],
            'budget': {'attempts_used': attempts, 'attempt_limit': run['config']['max_trials'],
                'remaining_attempts': max(0, run['config']['max_trials'] - attempts), 'configuration_check_reserve': 1},
            'next_action': 'configure_and_review_data_interface', 'execution_authorized': False}
        queued = configuration_batch(engine.store, run_id)
        if queued:
            value['datasets'] = [queued['dataset']]
            value['budget'].update(queue_budget(engine.store, run_id))
            value['configuration_queue'] = queue_view(engine.store, run_id)
        fingerprint = sha(value)
        if (args.get('expected_sha256') and args['expected_sha256'] != fingerprint
                or offset and not args.get('expected_sha256')):
            raise ValueError('Configuration ledger changed or continuation has no exact SHA256')
        return {**value, 'sha256': fingerprint, 'offset': offset, 'next_offset': None}
    rows = validation_records(engine.store, run_id)
    value = ledger(engine, run_id, rows=rows)
    fingerprint = sha(value)
    if args.get('expected_sha256') and args['expected_sha256'] != fingerprint:
        raise ValueError('Experiment ledger changed; restart pagination from its current snapshot')
    if offset and not args.get('expected_sha256'):
        raise ValueError('Continuation requires the exact ledger SHA256')
    run = engine.store.get('runs', run_id)
    from .execution_contract import REPAIR_ATTEMPT_ALLOWANCE
    # Count every phase and failed/interrupted/superseded attempt. Final-test
    # metrics are neither selected nor loaded by this accounting query.
    with engine.store._read_db() as db:
        attempts = db.execute("SELECT coalesce(sum(coalesce(json_extract(data,'$.attempt'),1)),0) "
            "FROM records WHERE collection='experiments' AND json_extract(data,'$.run_id')=? "
            "AND coalesce(json_extract(data,'$.research_cycle'),1)=?", (run_id, engine.cycle(run_id))).fetchone()[0]
    result = {k: v for k, v in value.items() if k not in {'jobs', 'unexpected_trial_ids'}}
    result.update(sha256=fingerprint, jobs=value['jobs'][offset:offset + limit], offset=offset,
        next_offset=offset + limit if offset + limit < len(value['jobs']) else None,
        unexpected_trial_count=len(value['unexpected_trial_ids']),
        failure_counts=dict(Counter((r.get('failure_diagnostic') or failure(r) or {}).get('category') for r in rows
            if not r.get('superseded') and (r.get('research_cycle') or 1) == engine.cycle(run_id)
            and r.get('status') not in {'completed', 'running'})),
        budget={'attempts_used': attempts, 'attempt_limit': run['config']['max_trials'] + REPAIR_ATTEMPT_ALLOWANCE,
            'remaining_attempts': max(0, run['config']['max_trials'] + REPAIR_ATTEMPT_ALLOWANCE - attempts),
            'scope': 'Per-cycle runner attempts; provider call/cost and hardware admission remain independent gates.'},
        next_action=('review_validation_evidence' if value['complete'] else
                     'observe_active_job' if value['counts'].get('running') else 'resolve_missing_or_invalid_jobs'),
        execution_authorized=False)
    queued = queue_view(engine.store, run_id)
    if queued:
        result['budget'].update(queue_budget(engine.store, run_id))
        result['configuration_queue'] = queued
    from .experiment_decisions import session
    decision_state = session(engine, run_id)
    if decision_state:
        pending = decision_state.get('pending') or {}
        result['decision_control'] = {'session_id': decision_state['id'],
            'last_decision_id': decision_state.get('last_decision'),
            'pending_unit_id': pending.get('unit_id'), 'pending_status': pending.get('status'),
            'history_url': f'/api/runs/{run_id}/experiment-decisions'}
        result['next_action'] = 'observe_active_batch' if pending else 'review_next_experiment_decision'
    from .experiment_memory import recall
    from .rapid_experiment_revision import latest
    result['experiment_memory'] = recall(engine, run_id)
    revision = latest(engine, run_id)
    result['rapid_revision'] = ({k: revision.get(k) for k in ('id', 'status', 'decision_id', 'completed_at')}
        if revision else None)
    from .rapid_continuation import view as continuation_view
    continuation = continuation_view(engine.store, run_id)
    result['rapid_continuation'] = ({k: continuation.get(k) for k in
        ('id', 'scope', 'status', 'phase', 'sha256', 'error')} if continuation else None)
    return result


def _trial(engine, run_id, trial_id, cycle):
    from .experiment_views import _projection
    key = run_id + ':' + trial_id
    with engine.store._read_db() as db:
        head = db.execute("SELECT updated_at FROM records WHERE collection='experiments' AND key=? "
            "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.params.phase') IN ('validation','code_check') "
            "AND coalesce(json_extract(data,'$.research_cycle'),1)=?", (key, run_id, cycle)).fetchone()
    if not head:
        raise ValueError('No validation or interface-check trial in this run and cycle')
    row = _projection(engine.store, key, head[0])
    if row is None:
        raise ValueError('Trial changed during read; refresh its manifest')
    # Read only a bounded diagnostic tail. Never parse full metrics, arrays or
    # stdout in Python. Truncation is explicit and not a full-log reading claim.
    with engine.store._read_db() as db:
        saved = db.execute("SELECT substr(json_extract(data,'$.stderr'),-4000), "
            "length(json_extract(data,'$.stderr')) FROM records "
            "WHERE collection='experiments' AND key=? AND updated_at=?", (key, head[0])).fetchone()
    if saved is None:
        raise ValueError('Trial changed during diagnostic read')
    stderr = saved[0] or ''
    # A rejected validation program may have emitted held-out fields. Do not
    # expose those values through a debugging backdoor.
    withheld = any(str(k).startswith('test_') for k in row['metrics']) or 'test_' in stderr
    diagnostic = {'routing': row.get('failure_diagnostic') or failure({**row, 'stderr': stderr}),
        'error': redact(row.get('error') or '') if not withheld else 'Held-out fields withheld',
        'stderr_tail': redact(stderr) if not withheld else 'Held-out fields withheld',
        'stderr_total_chars': saved[1] or 0, 'stderr_truncated': (saved[1] or 0) > 4000,
        'evaluation_errors': row.get('evaluation_errors') if not withheld else [],
        'source': 'saved_validation_record', 'root_cause_verified': False}
    return {'trial_id': row['id'], 'status': row['status'], 'attempt': row.get('attempt'),
        'research_cycle': row.get('research_cycle') or 1, 'code_revision': row.get('code_revision'),
        'code_sha256': row.get('code_sha256'), 'plan_sha256': row.get('experiment_plan_sha256'),
        'superseded': bool(row.get('superseded')), 'provenance': row.get('provenance'),
        'params_complete': row.get('params_complete'), 'metrics_complete': row.get('metrics_complete'),
        'params': row['params'], 'metrics': {k: v for k, v in row['metrics'].items() if k.startswith('val_')},
        'resources': {k: row.get(k) for k in ('duration_seconds', 'peak_memory_mb', 'cpu_seconds', 'time_limits',
            'pid', 'process_started', 'exit_code', 'started_at')},
        'identity': {**{k: v for k, v in row['evaluation'].items() if k in {'dataset_id', 'phase', 'split_identity_protocol'}},
            'split_fingerprints': {k: v for k, v in (row['evaluation'].get('split_fingerprints') or {}).items()
                                   if k in {'train', 'validation'}}},
        'diagnostic': diagnostic}


def read(engine, run_id, cycle, args):
    value = _trial(engine, run_id, args['trial_id'], cycle)
    digest = sha(value)
    if args.get('expected_sha256') and args['expected_sha256'] != digest:
        raise ValueError('Validation read projection changed; refresh its manifest')
    section = args.get('section', 'manifest')
    if section == 'manifest':
        result = {k: v for k, v in value.items() if k not in {'params', 'metrics', 'identity', 'diagnostic', 'resources'}}
        return {**result, 'sha256': digest, 'hash_scope': 'bounded_validation_read_projection',
                'sections': ['params', 'metrics', 'identity', 'diagnostic', 'resources'], 'scientific_approval': False}
    if section not in {'params', 'metrics', 'identity', 'diagnostic', 'resources'} or not args.get('expected_sha256'):
        raise ValueError('Read a named section with the manifest SHA256')
    return {'trial_id': args['trial_id'], 'sha256': digest, 'section': section,
            'source': 'bounded_validation_read_projection', **_page(value[section], args)}


def compare(engine, run_id, context, args):
    ref = context.get('validation_evidence') or {}
    from .experiment_evidence_handoff import KEY
    supplement = context.get('experiment_decision', {}).get(KEY, {}).get('evidence') or {}
    if args['evidence_id'] == supplement.get('id'):
        ref = supplement
    if args['evidence_id'] != ref.get('id') or args['expected_sha256'] != ref.get('sha256'):
        raise ValueError('Comparison is outside the frozen review evidence')
    from .scope_read_session import verified_record
    error = 'Frozen validation comparison is missing or changed'
    record = verified_record(engine, 'experiment_evidence', args['evidence_id'], run_id, ref['sha256'], error=error)
    if record is None:
        record = engine.store.get('experiment_evidence', args['evidence_id'])
        if not record or record.get('run_id') != run_id or sha(record['summary']) != ref['sha256']:
            raise ValueError(error)
    value = record['summary']
    path = args.get('path', '')
    if path:
        if not path.startswith('/'):
            raise ValueError('Comparison path must be a JSON pointer')
        try:
            for token in path[1:].split('/'):
                token = token.replace('~1', '/').replace('~0', '~')
                if isinstance(value, list):
                    if not token.isdecimal():
                        raise ValueError('Invalid list index')
                    value = value[int(token)]
                else:
                    value = value[token]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError('Comparison path does not exist') from exc
    return {'evidence_id': record['id'], 'sha256': record['sha256'], 'path': path,
            'source': record['provenance'], **_page(value, args)}


def execute(harness, run_id, role, owner, name, args):
    context = _context(harness, run_id, role, owner, name, args)
    if name == 'experiments.status':
        return status(harness.engine, run_id, args)
    if name == 'experiments.cleanup':
        from .experiment_cleanup import execute as cleanup
        return cleanup(harness.engine, run_id, role, owner, context['research_cycle'], args)
    if name == 'experiments.record_notes':
        return record_notes(harness.engine, run_id, role, owner, args)
    if name == 'experiments.read':
        result = read(harness.engine, run_id, context['research_cycle'], args)
    elif name in {'experiments.analyze', 'experiments.inspect_module'}:
        from .experiment_analysis import analyze, inspect_module
        result = (analyze if name == 'experiments.analyze' else inspect_module)(harness.engine, run_id, context, args)
    else:
        result = compare(harness.engine, run_id, context, args)
    return {**result, 'receipt_sha256': sha({'call': {'name': name, 'arguments': args}, 'result': result})}


def record_notes(engine, run_id, role, owner, args):
    """Keep small author observations with real same-handoff read provenance."""
    progress = engine.store.get('handoff_progress', owner) or {}
    from .dataset_review_protocol import inherited_rows
    context = (engine.store.get('handoff_inputs',owner) or {}).get('context',{})
    inherited = inherited_rows(engine,run_id,role,context)
    from .training_read_continuation import retained_rows, retained_handoff
    training_inherited = retained_rows(engine, run_id, role, context)
    valid = set()
    for row in [*inherited, *training_inherited, *progress.get('tool_results', [])]:
        call, result = row.get('call') or {}, row.get('result') or {}
        if call.get('name') not in {'experiments.read', 'experiments.compare', 'experiments.analyze', 'experiments.inspect_module', 'hyperparameters.read', 'hyperparameters.revise'} or not result.get('text'):
            continue
        receipt = result.get('receipt_sha256')
        if receipt and receipt == sha({'call': call, 'result': {k: v for k, v in result.items() if k != 'receipt_sha256'}}):
            valid.add(receipt)
    notes = args['notes']
    if any(not set(note['receipt_sha256s']).issubset(valid) for note in notes):
        raise ValueError('Experiment notes require actual reads from this role and handoff')
    key = run_id + ':' + owner
    def save(old):
        if old and (old['role'] != role or old['handoff_id'] != owner or old['run_id'] != run_id):
            raise ValueError('Experiment note ownership changed')
        initial = context.get('experiment_notes',{}).get('notes',[]) if inherited else []
        items = {note['key']: note for note in (old or {}).get('notes', initial)}
        items.update({note['key']: copy.deepcopy(note) for note in notes})
        if len(items) > 8:
            raise ValueError('At most eight active notes; update existing keys to consolidate observations')
        used_inherited = {ref for note in items.values() for ref in note['receipt_sha256s']} & {
            row['result']['receipt_sha256'] for row in training_inherited}
        origin = ({'handoff_id': retained_handoff(context),
                   'receipt_sha256s': sorted(used_inherited), 'new_reading_credit': False}
                  if used_inherited else None)
        return {'id': key, 'run_id': run_id, 'role': role, 'handoff_id': owner,
            'notes': list(items.values()), 'scientific_approval': False,
            **({'retained_training_read_origin': origin} if origin else {}),
            'provenance': 'agent_observation_bound_to_actual_validation_reads'}
    value = engine.store.mutate('experiment_notes', key, save)
    return {'saved': True, 'notes': value['notes'], 'scientific_approval': False,
            **({'retained_training_read_origin': value['retained_training_read_origin']}
               if value.get('retained_training_read_origin') else {}),
            'provenance': value['provenance']}


def _review_evidence(context, rows):
    """Keep only already-read task anchors, with a shared 6000-character cap."""
    planning = context.get('dataset_planning') or {}
    paths = planning.get('read_paths', [])[:4]
    ref = context.get('validation_evidence') or {}
    latest = {}
    previous = (context.get('experiment_review_evidence') or {}).get('pages', [])
    for row in [*previous, *rows]:
        call, result = row.get('call', {}), row.get('result', {})
        args = call.get('arguments', {})
        if (call.get('name') != 'experiments.compare' or args.get('path') not in paths
                or args.get('evidence_id') != ref.get('id') or args.get('expected_sha256') != ref.get('sha256')
                or not isinstance(result.get('text'), str) or not result['text'] or result.get('error')
                or result.get('receipt_sha256') != sha({'call': call,
                    'result': {k:v for k,v in result.items() if k != 'receipt_sha256'}})):
            continue
        latest[args['path']] = row
    pages, size = [], 0
    for path in sorted(latest, key=lambda p: (p != '/proposal', paths.index(p))):
        row = latest[path]
        if size + len(row['result']['text']) <= 6000:
            pages.append(row)
            size += len(row['result']['text'])
    return {'pages': pages, 'source': 'same_assignment_exact_reads', 'scientific_approval': False}


def _implementation_source_windows(context, rows):
    """Keep exact, already-read edit operands, not the experiment transcript.

    Two ordinary read windows cannot hold a multi-part patch after reading a
    dependency. Retain at most four source windows (24k characters) from this
    amendment's targets; receipt ownership and byte checks still run at commit.
    """
    if str(context.get('_purpose', '')).split(':', 1)[0] != 'rapid_implement':
        return set()
    revision = context.get('rapid_revision') or {}
    targets = [c.get('target', '') for c in revision.get('changes', [])]
    # Training repairs also declare exact module paths. A human-readable target
    # may join multiple paths with Chinese prose; it is not a path grammar.
    targets += (revision.get('training_response') or {}).get('module_paths', [])
    ref = context.get('validation_evidence') or {}
    kept, seen, versions, size = set(), set(), {}, 0
    candidates = []
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        call, result = row.get('call', {}), row.get('result', {})
        args = call.get('arguments', {})
        path = args.get('path', '')
        if (call.get('name') != 'experiments.inspect_module' or args.get('view') != 'source'
                or args.get('snapshot', 'current') != 'current' or not result.get('source_read')
                or result.get('error') or not isinstance(result.get('text'), str)
                or args.get('evidence_id') != ref.get('id') or args.get('expected_sha256') != ref.get('sha256')
                or not path or not any(t == path or t.startswith(tuple(path + d for d in ('::', ':', '：', ' ', '\t', '\n'))) for t in targets)):
            continue
        version = result.get('source_sha256')
        if not version or versions.setdefault(path, version) != version:
            continue
        identity = (path, version, result.get('offset'), result.get('end'))
        if identity in seen:
            continue
        seen.add(identity)
        length = len(result['text'])
        candidates.append((index, path, result.get('offset', 0), result.get('end', 0), length))
    covered = {}
    while candidates and len(kept) < 4:
        ranked = []
        for index, path, start, end, length in candidates:
            if size + length > 24000:
                continue
            spans = [(start, end)]
            for lo, hi in covered.get(path, []):
                spans = [(a, b) for left, right in spans for a, b in
                         ((left, min(right, lo)), (max(left, hi), right)) if a < b]
            gain = sum(b - a for a, b in spans)
            if gain > 0:
                ranked.append((gain, index, path, start, end, length))
        if not ranked:
            break
        _, index, path, start, end, length = max(ranked)
        kept.add(index)
        size += length
        covered.setdefault(path, []).append((start, end))
        candidates = [c for c in candidates if c[0] != index]
    return kept


def project_receipts(context):
    """Only the newest read window stays active. All receipts stay in progress.

    Hashes identify prior pages; a new explicit read can restore them. Never
    compact the Python execution receipt or an unresolved tool error.
    """
    result = copy.deepcopy(context)
    rows = result.get('tool_results') or []
    from .training_read_inventory import project as training_inventory
    inventory = training_inventory(context, rows)
    if inventory is not None:
        result.setdefault('experiment_decision', {})['delivered_training_reads'] = inventory
    recovery = (result.get('rapid_revision') or {}).get('source_context_recovery') or {}
    inherited = recovery.get('retained_material', [])
    if inherited:
        from .rapid_context import _valid
        if (sha(inherited) != recovery.get('source_receipts_sha256')
                or not all(_valid(context, row) for row in inherited)):
            raise ValueError('Recovered patch working material changed')
        rows = copy.deepcopy(inherited) + rows
        # The source receipts appear once in tool_results; the durable request
        # keeps the full envelope, but the model sees only its provenance here.
        recovery.pop('retained_material', None)
    note_receipts = [r for r in rows if r.get('call', {}).get('name') == 'experiments.record_notes'
                     and (r.get('result') or {}).get('saved') is True]
    notes = note_receipts[-1]['result']['notes'] if note_receipts else result.get('experiment_notes',{}).get('notes',[])
    noted = {ref for receipt in note_receipts for note in receipt['result']['notes'] for ref in note['receipt_sha256s']}
    read_tools = {'experiments.status', 'experiments.read', 'experiments.compare', 'experiments.analyze', 'experiments.inspect_module', 'experiments.cleanup', 'hyperparameters.read', 'hyperparameters.revise'}
    if context.get('dataset_planning'):
        from .dataset_planning import READ_TOOLS
        read_tools |= READ_TOOLS
        anchors = _review_evidence(context, rows)
        # Put archived anchors alongside the two active windows, without
        # duplicating pages still visible in those windows.
        result['experiment_review_evidence'] = anchors
    indexes = [i for i, row in enumerate(rows) if row.get('call', {}).get('name') in read_tools
               and 'result' in row and not row['result'].get('error')]
    evict = set(indexes[:-2])
    edit_windows = _implementation_source_windows(context, rows)
    evict -= edit_windows
    from .rapid_context import support_windows
    support = support_windows(context, rows, edit_windows)
    evict -= support
    from .decision_working_material import windows as decision_windows
    retained_decision = decision_windows(context, rows)
    evict -= retained_decision
    if retained_decision:
        result['decision_working_material'] = {
            'retained_receipts': [rows[i]['result']['receipt_sha256'] for i in sorted(retained_decision)],
            'guidance': '本轮已读关键参数、统计和源码正文在 tool_results 中持续保留；相同或重叠分页已合并。'
                '直接核对这些真实正文并提交当前任务单、提案或独立评审。不要因旧笔记写着未读而重读已覆盖页面；'
                '仅针对缺少的范围或具体未决差异补读。已读不等于批准，统计不等于已证实根因。',
            'new_reading_credit': False}
    if edit_windows or support:
        result['rapid_working_material'] = {
            'retained_receipts': [rows[i]['result'].get('receipt_sha256') for i in sorted(edit_windows | support)],
            'guidance': '当前 tool_results 已保留本轮目标源码、已读依赖和旧补丁正文。直接使用这些原文与回执完成已批准补丁；'
                '不要用改变 limit/offset 重读已覆盖范围，不重复查询未变化的 status。只读取当前确实缺少的范围。'
                '旧补丁仍是未执行候选，保留不等于通过审查。old、new和checks不设字符上限；最多6个互不重叠的原文锚点，遵循当前响应schema。',
            'new_reading_credit': False}
    from .dataset_review_protocol import enabled as configuration_review, read_state
    if (str(context.get('_purpose','')).split(':',1)[0]=='rapid_review'
            and (context.get('rapid_revision') or {}).get('changed_ranges')):
        # Reading credit is not scientific approval. Keep a compact, exact range
        # ledger so old "still to read" notes do not restart completed reads.
        _, all_sources=read_state(context,rows)
        all_sources += copy.deepcopy(context['rapid_revision'].get('inherited_source_coverage',[]))
        required=context['rapid_revision']['changed_ranges'];basis=[]
        for target in required:
            choices=[r for r in all_sources if r['path']==target['path'] and r['source_sha256']==target['sha256']]
            cursor=target['start']
            while cursor<target['end']:
                candidates=[r for r in choices if r['offset']<=cursor<r['end']]
                if not candidates:
                    future=[r['offset'] for r in choices if cursor<r['offset']<target['end']]
                    if not future:break
                    cursor=min(future);continue
                chosen=max(candidates,key=lambda r:(r['end'],r['offset']))
                if chosen not in basis:basis.append(chosen)
                cursor=chosen['end']
        from .dataset_review_protocol import remaining_ranges
        missing=remaining_ranges(required,basis)
        result['experiment_review_evidence']={'source_reads':basis,'unread_required_ranges':missing,
            'all_required_source_ranges_read':not missing,'scientific_approval':False,
            'guidance':'This ledger shows actual source reads, not understanding or approval. Reconcile stale pending notes with later receipts; update existing note keys instead of restarting whole-file reads. Read only missing ranges or a specific unresolved detail. Independently approve or reject with concrete issues; never infer approval from coverage.'}
    if configuration_review(context):
        pending, source_reads = read_state(context, rows)
        # The controller requires consolidation before another read batch.
        # All three results in the current batch must be visible at least once.
        evict -= set(pending[-3:])
        result['experiment_review_evidence'] = {'notes_required': bool(pending),
            'pending_receipts': [rows[i]['result']['receipt_sha256'] for i in pending[-3:]],
            'source_reads': source_reads, 'scientific_approval': False}
        from .dataset_review_protocol import remaining_ranges
        result['dataset_configuration']['required_read_ranges'] = remaining_ranges(
            context['dataset_configuration']['changed_ranges'],source_reads)
    if notes:
        result['experiment_notes'] = {'notes': notes, 'provenance': 'agent_observations_not_independent_verification'}
    if evict:
        result['experiment_read_history'] = {'archived_pages': len(evict),
            'retained_edit_windows': len(edit_windows),
            'receipt_set_sha256': sha([rows[i] for i in sorted(evict)]),
            'unconsolidated_pages': sum(bool(rows[i]['result'].get('text'))
                and rows[i]['result'].get('receipt_sha256') not in noted for i in evict),
            'recent_archived_refs': [{'call': rows[i]['call'],
                'receipt_sha256': rows[i]['result'].get('receipt_sha256'),
                'observation_saved': rows[i]['result'].get('receipt_sha256') in noted}
                for i in sorted(evict)[-4:]],
            'caution': 'Unconsolidated pages are archived readings, not verified conclusions. Re-read or save anchored notes before relying on their details.',
            'restore': 'Repeat the exact hash-bound read if earlier evidence is needed; complete receipts remain in handoff_progress and execution_archives.'}
    result['tool_results'] = [row for i, row in enumerate(rows) if i not in evict and row not in note_receipts]
    if context.get('dataset_planning'):
        active = {r.get('result',{}).get('receipt_sha256') for r in result['tool_results']}
        result['experiment_review_evidence']['pages'] = [r for r in anchors['pages']
            if r['result']['receipt_sha256'] not in active]
    return result


def retained_source_reference(context, call):
    """Reuse only source bytes actually delivered in this model request.

    The original receipt remains authoritative; this grants no new read credit.
    Never infer visibility from historical progress or a future projection.
    """
    if not context.get('rapid_revision'):
        return None
    from .rapid_context import patch_reference
    patch = patch_reference(context, call)
    if patch is not None:
        return patch
    if call.get('name') != 'experiments.inspect_module':
        return None
    args = call.get('arguments', {})
    if args.get('view') != 'source' or args.get('snapshot', 'current') != 'current':
        return None
    start = args.get('offset', 0)
    end = start + args.get('limit', 4000)
    candidates = []
    for row in context.get('tool_results', []):
        previous, result = row.get('call', {}), row.get('result', {})
        old = previous.get('arguments', {})
        if (previous.get('name') != call['name'] or old.get('view') != 'source'
                or old.get('snapshot', 'current') != 'current' or not result.get('source_read')
                or result.get('error') or not result.get('receipt_sha256')
                or not result.get('source_sha256') or not isinstance(result.get('text'), str)
                or any(old.get(k) != args.get(k) for k in ('path', 'evidence_id', 'expected_sha256'))):
            continue
        lo = result.get('offset', 0)
        hi = result.get('end', lo)
        if hi - lo != len(result['text']):
            continue
        candidates.append((lo, hi, result))
    for version in {r['source_sha256'] for _, _, r in candidates}:
        cursor, refs = start, []
        for lo, hi, result in sorted(candidates, key=lambda r: r[:2]):
            if result['source_sha256'] != version or lo > cursor or hi <= cursor:
                continue
            cursor = hi
            refs.append(result['receipt_sha256'])
            if cursor >= end or result.get('total_chars') == cursor:
                return {'path': args['path'], 'source_sha256': version, 'offset': start,
                        'end': min(end, cursor), 'receipt_sha256s': refs}
    return None


def archived_reread(context, progress, call):
    """Restoring evicted evidence is not a repeated action without progress."""
    restorable = {'experiments.read', 'experiments.compare', 'experiments.inspect_module', 'hyperparameters.read'}
    if context.get('dataset_planning'):
        restorable |= {'sources.read', 'sources.links', 'datasets.discover', 'datasets.inspect', 'literature.protocols'}
    if context.get(PROTOCOL) != 1 or call.get('name') not in restorable:
        return False
    rows = [r for r in progress.get('tool_results', []) if r.get('call', {}).get('name') in {
        'experiments.status', 'experiments.read', 'experiments.compare', 'experiments.analyze',
        'experiments.inspect_module', 'experiments.cleanup', *restorable} and r.get('result') is not None
        and not r['result'].get('error')]
    def signature(value):
        args = dict(value.get('arguments', {}))
        args.setdefault('offset', 0)
        args.setdefault('limit', 6000 if value.get('name') == 'hyperparameters.read' else 4000)
        if value.get('name') == 'experiments.inspect_module':
            args.setdefault('view', 'symbols')
        return sha({'name': value.get('name'), 'arguments': args})
    wanted = signature(call)
    visible = project_receipts({**context, 'tool_results': rows})['tool_results']
    return (not any(signature(r.get('call', {})) == wanted for r in visible)
        and any(signature(r['call']) == wanted and (r['result'].get('receipt_sha256')
            or context.get('dataset_planning') and r['call']['name'] in restorable) for r in rows[:-2]))
