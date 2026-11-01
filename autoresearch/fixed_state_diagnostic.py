"""Explicitly requested, independently reviewed analysis of saved diagnostic states.

This is a new observation protocol, not a retry of a failed scientific decision.
Protocol approval does not authorize code execution or alter the research gates.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
from pathlib import Path

from .experiment_workflow import sha
from .storage import now
from .training_adjustment import obj

COLLECTION = 'fixed_state_diagnostics'
ROLES = ('director', 'engineer', 'scout')
PATHS = ('/fixed_state_protocol', '/fixed_state_basis')
INSTRUCTION = '''用户明确要求分析最终诊断回执，提出新诊断并经三方独立评审；不得重跑或放宽门禁。
本次独立评审一个具体的新观测协议，不再无约束重复设计。先用 experiments.compare 分页完整读取
/fixed_state_protocol 和 /fixed_state_basis，返回这两项真实 compare 回执的全部 receipt_sha256。
read_receipts只列这两条路径的compare回执。必要时可另读原证据或源码，但不要把这些附加回执列入该字段。
核对：已有零状态及64步两分支末态的真实文件身份；只测固定状态的新梯度与方向，不续训/重跑旧检查；
竞争解释的可区分性及局部范围；固定误差规则、混合/无定论分支、资源硬上限和未保存历史末态限制。
特别审查归一化Adam历史矩方向与完整训练梯度之间的区别，不能把局部方向错配叫作长期数值不稳定，
也不能把负梯度内积直接当作历史过拟合根因。既有32/64步结论和科学验收原封不动。
approve_for_implementation只批准据此实现候选，绝不批准执行、模型激活、稳定性或科学验收。
若协议存在实质缺口则拒绝，明确issues和最小required_changes；不要求为了得到阳性结果重跑旧工作。
不继承任何其他角色批准，不能把当前未执行的测量写成已观测结果。'''


def schema(protocol_sha):
    text = {'type': 'string', 'minLength': 1}
    return obj({'protocol_sha256': {'const': protocol_sha},
        'approve_for_implementation': {'type': 'boolean'}, 'rationale': text,
        'issues': {'type': 'array', 'items': text},
        'required_changes': {'type': 'array', 'items': text},
        'read_receipts': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
            'items': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}}})


def current(engine, run_id):
    return engine.store.get(COLLECTION, f'{run_id}:{engine.cycle(run_id)}')


def verify_inputs(engine, record):
    for col, key, expected in record['protected_sources']:
        value = engine.store.get(col, key)
        if value is None or sha(value) != expected:
            raise ValueError('Fixed-state protocol source record changed: ' + col + ':' + key)
    for name, expected in record['protected_files'].items():
        path = Path(name)
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Fixed-state protocol input file changed: ' + name)
    if sha(record['protocol']) != record['protocol_sha256']:
        raise ValueError('Fixed-state protocol changed after registration')


def register(engine, run_id, request):
    """Operator supplies the new explicit human request, never inferred from a hold."""
    if not request.get('authorization') or request.get('run_id') != run_id:
        raise ValueError('An explicit user request for this run is required')
    old = current(engine, run_id)
    if old:
        if old['request_sha256'] != sha(request):
            raise ValueError('A registered protocol cannot be silently replaced')
        return old
    run = engine.store.get('runs', run_id)
    if run['status'] not in ('blocked', 'paused'):
        raise ValueError('Register the new protocol only while the research is idle')
    for collection in ('experiments', 'handoffs'):
        # Historical queued author assignments are not executing workers. They
        # remain untouched; a running handoff or queued runner job is active.
        active = ('running', 'queued') if collection == 'experiments' else ('running',)
        if any(r.get('status') in active for r in engine.store.list(collection, run_id)):
            raise ValueError('Do not interrupt an active experiment or handoff')
    causal = engine.store.get('causal_training_followups', f'{run_id}:{engine.cycle(run_id)}')
    if not causal or causal['status'] != 'completed':
        raise ValueError('Saved completed diagnostic states are required')
    record = {**copy.deepcopy(request), 'id': f'{run_id}:{engine.cycle(run_id)}',
        'cycle': engine.cycle(run_id), 'request_sha256': sha(request),
        'protocol_sha256': sha(request['protocol']), 'status': 'reviewing_protocol',
        'created_at': now(), 'assignments': {}, 'execution_authorized': False,
        'scientific_approval': False}
    verify_inputs(engine, record)
    engine.store.put(COLLECTION, record['id'], record)
    engine.store.event(run_id, 'fixed_state.requested', '已登记固定状态诊断协议，待三方独立评审，尚未授权执行',
        {'protocol_sha256': record['protocol_sha256']})
    return record


def validate_reads(engine, record, role, assignment, paths=PATHS):
    from .diagnostic_planning_recovery import authenticate
    from .experiment_tools import compare
    answer = assignment['answer']
    payload = {k: assignment[k] for k in ('instruction', 'context', 'schema')}
    owner = authenticate(engine, record['run_id'], role, assignment['purpose'], payload, answer)
    refs = set(answer['read_receipts'])
    seen, windows = set(), {p: [] for p in paths}
    for row in (engine.store.get('handoff_progress', owner) or {}).get('tool_results', []):
        call, result = row.get('call', {}), row.get('result', {})
        receipt = result.get('receipt_sha256')
        if receipt not in refs:
            continue
        body = {k: v for k, v in result.items() if k != 'receipt_sha256'}
        path = call.get('arguments', {}).get('path')
        if (call.get('name') != 'experiments.compare' or path not in windows
                or receipt != sha({'call': call, 'result': body})
                or compare(engine, record['run_id'], assignment['context'], call['arguments']) != body):
            raise ValueError('Protocol review has a foreign or invalid read receipt')
        seen.add(receipt)
        windows[path].append(result)
    if seen != refs:
        raise ValueError('Protocol review cites missing reads')
    for path, pages in windows.items():
        end = 0
        for page in sorted(pages, key=lambda p: p['offset']):
            if page['offset'] > end:
                break
            end = max(end, page['end'])
        if not pages or end != pages[0]['total_chars']:
            raise ValueError('Protocol review must read the entire ' + path)
    return {'role': role, 'handoff_id': owner, 'receipt_sha256s': sorted(refs), 'complete': True}


def approved(assignments):
    def vote(a):
        clarification = a.get('clarification', {})
        return clarification['answer'] if clarification.get('authenticated') else a.get('answer', {})
    return set(assignments) == set(ROLES) and all(
        a.get('read', {}).get('complete') is True
        and vote(a).get('approve_for_implementation') is True
        and not vote(a)['issues'] and not vote(a)['required_changes']
        for a in assignments.values())


def clarification_request(record, assignment):
    """A single same-author clarification; no host editing of an ambiguous vote."""
    answer = assignment['answer']
    if not (answer['approve_for_implementation'] is True and answer['issues']
            and not answer['required_changes'] and assignment.get('read', {}).get('complete')):
        raise ValueError('Only an authenticated approval with caveats can be clarified')
    strict = copy.deepcopy(assignment['schema'])
    for field in ('protocol_sha256', 'read_receipts'):
        strict['properties'][field] = {'const': answer[field]}
    strict['allOf'] = [{'if': {'properties': {'approve_for_implementation': {'const': True}}},
        'then': {'properties': {'issues': {'maxItems': 0}, 'required_changes': {'maxItems': 0}}}}]
    context = copy.deepcopy(assignment['context'])
    context['experiment_decision']['approval_clarification'] = {
        'original_answer': copy.deepcopy(answer), 'original_answer_sha256': sha(answer),
        'original_read_owner': assignment['read']['handoff_id'], 'approval_inherited': False}
    return {'purpose': assignment['purpose'] + ':approval-clarification-1',
        'instruction': '请澄清你本人的已保存协议评审，原答复在experiment_decision.approval_clarification。'
            '你返回approve_for_implementation=true、required_changes=[]但issues非空。'
            '宿主不会替你判断问题是否阻断。若存在阻止实现该协议的具体缺陷，返回false并保留具体issues和修改要求；'
            '若仅是已声明局限及后续门禁，将它们保留在rationale，返回true且issues=[]、required_changes=[]。'
            '协议SHA和原read_receipts锁定，不重新设计、不变更范围、不需要重读；原付费答复永久保留。'
            '此次仍只决定是否允许实现，绝不批准执行、激活、稳定性或科学验收。',
        'context': context, 'schema': strict, 'original_answer_sha256': sha(answer)}


async def clarify_approvals(engine, harness, record, save):
    from .diagnostic_planning_recovery import authenticate
    async def one(role, a):
        original = a['answer']
        if not (original['approve_for_implementation'] and original['issues'] and not original['required_changes']):
            return
        # Reauthenticate original reads and owner before issuing or reusing a clarification.
        await engine._read_consensus_check(record['run_id'], validate_reads, engine, record, role, a)
        expected = clarification_request(record, a)
        if not a.get('clarification'):
            a['clarification'] = expected
            save()
        c = a['clarification']
        if any(c.get(k) != v for k, v in expected.items()):
            raise ValueError('Protocol clarification lost its original author/protocol binding')
        if 'answer' not in c:
            c['answer'] = await harness.ask(record['run_id'], role, c['purpose'], c['instruction'], c['context'], c['schema'])
            save()
        payload = {k:c[k] for k in ('instruction','context','schema')}
        c['handoff_id'] = authenticate(engine, record['run_id'], role, c['purpose'], payload, c['answer'])
        c['authenticated'] = True
        c['read_origin'] = a['read']['handoff_id']
        save()
    results = await asyncio.gather(*(one(role,a) for role,a in record['assignments'].items()), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def execute(engine, harness, run_id):
    from .engine import ResearchBlocked
    from .experiment_decisions import _archive
    record = current(engine, run_id)
    if not record or record['status']=='completed':
        return None
    await engine._read_consensus_check(run_id, verify_inputs, engine, record)
    def save(**changes):
        record.update(changes, updated_at=now())
        engine.store.put(COLLECTION, record['id'], record)
    if record['status'] == 'reviewing_protocol':
        if not record.get('review_context'):
            original = engine.store.get('experiment_evidence', record['original_evidence']['id'])
            summary = copy.deepcopy(original['summary'])
            summary.update(fixed_state_protocol=record['protocol'], fixed_state_basis=record['basis'])
            ref = _archive(engine, run_id, summary)
            save(review_context={'training_context_protocol': 1, 'experiment_workflow_protocol': 1,
                'research_cycle': record['cycle'], 'evidence_sha256': ref['sha256'],
                'validation_evidence': ref, 'fixed_state_protocol_sha256': record['protocol_sha256'],
                'experiment_decision': {'fixed_state_protocol_review': True,
                    'evidence_paths': list(PATHS), 'scientific_approval': False}})
        # Persist each exact request before dispatch. Each role gets the same
        # protocol and no other role's answer; resume retains completed votes.
        for role in ROLES:
            if role not in record['assignments']:
                record['assignments'][role] = {
                    'purpose': 'experiment_diagnose:fixed-state-protocol:' + record['protocol_sha256'],
                    'instruction': INSTRUCTION, 'context': copy.deepcopy(record['review_context']),
                    'schema': schema(record['protocol_sha256'])}
        save()
        async def one(role):
            a = record['assignments'][role]
            if 'answer' not in a:
                a['answer'] = await harness.ask(run_id, role, a['purpose'], a['instruction'], a['context'], a['schema'])
                save()
            if not a.get('read'):
                a['read'] = await engine._read_consensus_check(run_id, validate_reads, engine, record, role, a)
                save()
        results = await asyncio.gather(*(one(r) for r in ROLES), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        save(status='protocol_approved' if approved(record['assignments']) else 'protocol_rejected')
    if record['status'] in ('protocol_rejected', 'protocol_approved'):
        await clarify_approvals(engine, harness, record, save)
        save(status='protocol_approved' if approved(record['assignments']) else 'protocol_rejected')
    if record['status'] == 'protocol_rejected':
        raise ResearchBlocked('固定状态新诊断协议未获三方一致批准；保留具体异议，不执行。')
    if record['status'] == 'protocol_approved':
        if not record.get('test_evidence'):
            raise ResearchBlocked('固定状态协议已获三方批准，等待候选CPU回归回执；尚未执行。')
    if record['status'] in ('protocol_approved','reviewing_code','code_rejected','code_approved','executing','reviewing_results','execution_failed'):
        from .fixed_state_execution import continue_diagnostic
        return await continue_diagnostic(engine,harness,record,save)
    raise ResearchBlocked('固定状态诊断等待明确的下一项已审查工作；不自动重跑或跳过门禁。')
