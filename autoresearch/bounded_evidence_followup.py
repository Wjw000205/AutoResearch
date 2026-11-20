"""Acquire named missing evidence and re-dispatch to the original reviewers.

An additive controller extension: the reviewed diagnostic, host enforcement,
old packets and old votes stay byte-for-byte unchanged. Only freshly approved
supplemental judgments can admit the exact old candidate to its original gate.
"""
import copy
import hashlib
import json
from pathlib import Path

from . import bounded_evidence_review as base
from .experiment_workflow import sha
from .parameter_resolution import owner
from .storage import now
from .training_adjustment import obj

COLLECTION = 'bounded_evidence_followups'
WORK = 'bounded_evidence_work_items'
SELECT = obj({'packet_sha256': base.HASH,
    'action': {'enum': ['read', 'host_work_required']},
    'document_ids': {'type': 'array', 'maxItems': 12, 'uniqueItems': True, 'items': base.HASH},
    'rationale': base.TEXT,
    'host_work': {'type': 'array', 'maxItems': 8, 'items': obj({
        'issue_id': {'type': 'string'}, 'kind': {'enum': ['bounded_host_check', 'source_change', 'missing_asset']},
        'requirement': base.TEXT, 'acceptance': base.TEXT})}})
SELECT_INSTRUCTION = '''你是提出证据缺口的原独立评审角色。现在自动派发定向补证，不重做完整审查。
请将本角色仍未闭合的具体问题映射到 payload.catalog 中的原始材料，选择 document_ids 让宿主完整取回。
目录只有名称、身份和范围，不是已经读取。优先补当前候选未交付的数据、评估和模型依赖，以及宿主执行边界的精确源码。
允许首次定点回看原先大包中的细节；同一补证工作已交付的文档不会重复列出。
action=read 时必须选择至少一份目录材料，host_work=[]；材料随后自动交回本角色，不需要请求用户点击恢复。
只有目录中确实没有可补的材料、必须产生新运行级证据或修订实现时才用host_work_required，document_ids=[]，
列出具体issue_id、宿主工作类型、操作要求及验收条件。不能把源码当运行证明，也不能为了结束而批准。
不得要求重训已有实验、扩大种子/参数/数据集、访问test或放宽资源约束。只返回结构化补证需求。'''


def controller_sha():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _doc(path, text, provenance):
    return {'id': sha([path, hashlib.sha256(text.encode()).hexdigest()]), 'path': path,
            'sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text, 'provenance': provenance}


def catalog(engine, source):
    ref = source['candidate']['code_sources']
    saved = engine.store.get('experiment_sources', ref['id']) or {}
    if saved.get('run_id') != source['run_id'] or sha(saved.get('files')) != ref['sha256']:
        raise ValueError('Supplement source archive changed')
    docs = []
    for path, entry in saved['files'].items():
        text = entry.get('text')
        if not text:
            continue
        if hashlib.sha256(text.encode()).hexdigest() != entry['sha256']:
            raise ValueError('Supplement source bytes changed')
        docs.append(_doc('candidate/' + path, text, ref))
    proof = source.get('resource_evidence', {})
    for key, value in proof.get('source', {}).items():
        docs.append(_doc('host/' + key, value, {'resource_evidence_sha256': proof['sha256']}))
    for key in ('closeout_checks', 'offline_checks'):
        if proof.get(key):
            docs.append(_doc('host/' + key, json.dumps(proof[key], ensure_ascii=False, indent=2),
                             {'resource_evidence_sha256': proof['sha256']}))
    for key in ('continuation_author_material', 'candidate_io_checks', 'protocol'):
        if source.get(key):
            docs.append(_doc('bound/' + key, json.dumps(source[key], ensure_ascii=False, indent=2),
                             {'diagnostic_id': source['id']}))
    return docs


def original(engine, work):
    body = work['body']
    if sha(body) != work['sha256'] or body['controller_sha256'] != controller_sha():
        raise ValueError('Supplement controller or frozen scope changed')
    source = engine.store.get('bounded_diagnostics', body['source_id'])
    if sha(source) != body['source_sha256']:
        raise ValueError('Original incomplete review changed')
    if source['candidate']['bundle']['sha256'] != body['candidate_sha256']:
        raise ValueError('Supplement candidate binding changed')
    return source


def original_reading(engine, source, role):
    plan = base.checked_plan(source)
    state = source[base.KEY]['roles'][role]
    observations, ids = [], []
    for i, sections in enumerate(plan['packets']):
        req = state['packets'][str(i)]
        expected = base.request(source, 'read', {'sections': sections})
        if any(req[k] != expected[k] for k in expected):
            raise ValueError('Original same-role packet changed')
        ids.append(base.authenticate(engine, source, role, req))
        observations += base.annotated(req)
    final = state['final']
    expected = base.request(source, 'final', {'issues': plan['issues'], 'observations': observations,
        'source_ranges': plan['source_ranges'], 'all_packets_reviewed_by_this_role': True})
    if any(final[k] != expected[k] for k in expected):
        raise ValueError('Original role judgment detached from reading')
    ids.append(base.authenticate(engine, source, role, final))
    return observations, final['answer'], ids


def prepare(engine, source):
    if source.get('status') != 'review_incomplete' or source.get('trial_id') or source.get('outcome'):
        return None
    key = source['id'] + ':evidence-followup-v1'
    existing = engine.store.get(COLLECTION, key)
    if existing:
        original(engine, existing)
        return existing
    for role in base.ROLES:
        _, answer, _ = original_reading(engine, source, role)
        if answer['verdict'] == 'code_defect':
            return None
    from .bounded_diagnostics import source_task
    source_task(engine, source)
    docs = catalog(engine, source)
    body = {'source_id': source['id'], 'source_sha256': sha(source),
            'candidate_sha256': source['candidate']['bundle']['sha256'],
            'controller_sha256': controller_sha(), 'documents': docs,
            'policy': 'Same-role acquisition only; no inherited approval; no training until fresh unanimous judgments.'}
    work = {'id': key, 'run_id': source['run_id'], 'body': body, 'sha256': sha(body),
            'roles': {}, 'status': 'collecting_evidence', 'created_at': now()}
    engine.store.batch(puts=[(COLLECTION, key, work)],
                       expected=[('bounded_diagnostics', source['id'], source), (COLLECTION, key, None)])
    engine.store.event(source['run_id'], 'review.evidence_followup_created',
        '证据缺口已转为原评审角色的定向补证任务，保留原批次和投票。', {'work_id': key})
    return work


def make_request(source, work, role, phase, payload):
    req = base.request(source, 'read' if phase == 'read' else 'final', payload)
    packet = req['context'][base.KEY]
    packet.update(followup_id=work['id'], followup_sha256=work['sha256'], role=role, followup_phase=phase)
    packet.pop('packet_sha256')
    packet['packet_sha256'] = sha(packet)
    req['purpose'] = 'rapid_review:bounded-supplement:' + packet['packet_sha256']
    if phase == 'select':
        req.update(instruction=SELECT_INSTRUCTION, schema=copy.deepcopy(SELECT))
    elif phase == 'read':
        req['instruction'] += '\n本批为定点补证，请针对payload.open_issues逐项核对正文；原来已解决的事项无需重做。'
    else:
        req['instruction'] += '\n这是补证后的新独立判断。结合本角色原判断与新增正文引用，明确缺口是否闭合，旧票保留但不继承批准。'
    # Later acquisition rounds extend observations; never mutate an earlier
    # frozen/paid payload through a shared Python list reference.
    return copy.deepcopy(req)


def check_request(saved, expected):
    if any(saved[k] != expected[k] for k in expected):
        raise ValueError('Supplement request changed or crossed roles')


def validate_selection(req, documents, seen, issues):
    from .prose_schema import validate
    answer = req['answer']
    validate(answer, SELECT)
    if answer['packet_sha256'] != req['context'][base.KEY]['packet_sha256']:
        raise ValueError('Selection packet identity changed')
    chosen = answer['document_ids']
    if answer['action'] == 'read':
        if not chosen or answer['host_work'] or not set(chosen) <= set(documents) - seen:
            raise ValueError('Evidence acquisition needs new, exact catalog documents')
    elif chosen or not answer['host_work'] or any(w['issue_id'] not in issues for w in answer['host_work']):
        raise ValueError('Host work requires a concrete remaining issue and acceptance condition')


def packets(documents, chosen):
    sections = [section for key in chosen for section in base._document(documents[key]['path'], documents[key]['text'])]
    result, batch, size = [], [], 0
    for section in sections:
        if batch and (size + len(section['text']) > base.PACKET_CHARS or len(batch) >= 16):
            result.append(batch); batch, size = [], 0
        batch.append(section); size += len(section['text'])
    if batch:
        result.append(batch)
    return result


async def role_review(engine, harness, source, work, role, save):
    from .engine import ResearchBlocked
    observations, previous, handoffs = original_reading(engine, source, role)
    state = work['roles'].setdefault(role, {'rounds': []})
    documents = {d['id']: d for d in work['body']['documents']}
    seen = set()
    plan = base.checked_plan(source)

    async def ask(req, expected, phase):
        check_request(req, expected)
        save()
        if 'answer' not in req:
            req['answer'] = await harness.ask(source['run_id'], role, req['purpose'], req['instruction'], req['context'], req['schema'])
            save()
        if phase != 'select':
            base.validate_answer(req)
        hid = owner(engine, source['run_id'], role, req)
        handoffs.append(hid)
        return req['answer']

    # Each round must acquire previously unrequested exact documents. The finite
    # catalog and normal provider budget bound work; identical gaps cannot spin.
    for index in range(len(documents) + 1):
        if previous['verdict'] == 'approve':
            # Even a previously approving role must issue a fresh confirmation.
            if index or state.get('confirmation'):
                if index:
                    return previous, handoffs
            expected = make_request(source, work, role, 'final', {
                'issues': plan['issues'], 'observations': observations, 'source_ranges': plan['source_ranges'],
                'all_packets_reviewed_by_this_role': True, 'prior_role_judgment': previous})
            req = state.setdefault('confirmation', expected)
            previous = await ask(req, expected, 'final')
            if previous['verdict'] == 'approve':
                return previous, handoffs
        if previous['verdict'] == 'code_defect':
            return previous, handoffs
        open_issues = [i for i in previous['issues'] if i['state'] == 'evidence_incomplete']
        available = [{k: d[k] for k in ('id', 'path', 'sha256', 'provenance')} | {'chars': len(d['text'])}
                     for key, d in documents.items() if key not in seen]
        if len(state['rounds']) <= index:
            state['rounds'].append({})
        rd = state['rounds'][index]
        expected = make_request(source, work, role, 'select', {'catalog': available,
            'open_issues': open_issues, 'prior_role_judgment': previous, 'already_acquired_ids': sorted(seen)})
        req = rd.setdefault('selection', expected)
        choice = await ask(req, expected, 'select')
        validate_selection(req, documents, seen, {i['issue_id'] for i in open_issues})
        if choice['action'] == 'host_work_required':
            item = {'id': work['id'] + ':' + role + ':' + str(index), 'run_id': work['run_id'],
                'status': 'requires_host_capability', 'source_work_id': work['id'], 'role': role,
                'source_handoff_id': handoffs[-1], 'candidate_sha256': work['body']['candidate_sha256'],
                'requirements': choice['host_work'], 'execution_authorized': False,
                'scope': 'Requires explicit implementation/check admission; not a queued or completed experiment.'}
            engine.store.put(WORK, item['id'], item)
            rd['host_work_id'] = item['id']; save()
            return previous, handoffs
        chosen = choice['document_ids']
        rd.setdefault('packets', {})
        for number, sections in enumerate(packets(documents, chosen)):
            expected = make_request(source, work, role, 'read', {'sections': sections, 'open_issues': open_issues,
                'acquisition_round': index, 'selected_document_ids': chosen})
            req = rd['packets'].setdefault(str(number), expected)
            await ask(req, expected, 'read')
            observations += base.annotated(req)
        seen.update(chosen)
        expected = make_request(source, work, role, 'final', {'issues': plan['issues'], 'observations': observations,
            'source_ranges': plan['source_ranges'], 'all_packets_reviewed_by_this_role': True,
            'prior_role_judgment': previous, 'acquisition_round': index})
        req = rd.setdefault('final', expected)
        previous = await ask(req, expected, 'final')
        engine.store.event(work['run_id'], 'review.evidence_followup_reviewed',
            '定向补证已由原角色复核，旧票与原始回执保留。',
            {'work_id': work['id'], 'role': role, 'round': index, 'verdict': previous['verdict']})
    raise ResearchBlocked('补证目录已耗尽，缺口需具体宿主能力处理；不重复派发相同材料。')


async def execute(engine, harness, run_id):
    from . import bounded_diagnostics as bounded
    from .engine import ResearchBlocked
    source = bounded.current(engine, run_id)
    if source and source.get('evidence_followup_id'):
        work = engine.store.get(COLLECTION, source['evidence_followup_id'])
        source = original(engine, work)
    else:
        work = prepare(engine, source) if source else None
    if work is None:
        return None
    source = original(engine, work)
    def save(**changes):
        engine.check_active(run_id)
        work.update(changes, updated_at=now())
        engine.store.put(COLLECTION, work['id'], work)
    save(status='collecting_evidence')
    decisions, receipts = {}, []
    for role in base.ROLES:
        decision, handoffs = await role_review(engine, harness, source, work, role, save)
        decisions[role] = decision
        receipts.append({'role': role, 'kind': 'same_role_evidence_followup', 'handoff_ids': handoffs,
                         'work_sha256': work['sha256'], 'approval_inherited': False})
    if any(a['verdict'] != 'approve' for a in decisions.values()):
        code = any(a['verdict'] == 'code_defect' for a in decisions.values())
        save(status='code_defect' if code else 'host_work_required', final_decisions=decisions)
        raise ResearchBlocked('定向补证复核发现具体代码缺陷，须按原工程门禁修订。' if code else
            '已自动派发定向补证并复核；剩余项需要新宿主检查、实现或资产，具体要求已保存，不能用重复读取冒充新证据。')
    c = source['candidate']
    votes = [{'role': role, 'approve': True, 'issues': [], 'rationale': a['rationale'],
              'candidate_sha256': c['bundle']['sha256'], 'amendment_sha256': c['amendment_sha256'],
              'checks_sha256': c['checks_sha256']} for role, a in decisions.items()]
    execution_id = work['id'] + ':execution'
    record = engine.store.get(bounded.COLLECTION, execution_id)
    if record is None:
        # The old review remains immutable. New approvals get a distinct owner;
        # its byte-identical candidate still undergoes the original runtime gate.
        record = copy.deepcopy(source)
        record.pop(base.KEY); record.pop('failure', None)
        record.update(id=execution_id, status='reviewing_code', votes=votes, review_reads=receipts,
            evidence_followup_id=work['id'], created_at=now(), updated_at=now())
        from .bounded_review_reading import COLLECTION as RECOVERIES
        audit = {'id': source['id'], 'run_id': run_id, 'source_sha256': sha(source), 'target_id': execution_id,
                 'created_at': now(), 'approval_inherited': False, 'evidence_followup_id': work['id']}
        engine.store.batch(puts=[(bounded.COLLECTION, execution_id, record), (RECOVERIES, source['id'], audit)],
            expected=[(bounded.COLLECTION, source['id'], source), (bounded.COLLECTION, execution_id, None),
                      (RECOVERIES, source['id'], None)])
    if record['votes'] != votes or record['review_reads'] != receipts or record['candidate'] != source['candidate']:
        raise ValueError('Execution is detached from fresh supplemental approvals')
    def save_execution(**changes):
        engine.check_active(run_id)
        record.update(changes, updated_at=now())
        engine.store.put(bounded.COLLECTION, execution_id, record)
        task = engine.store.get('parameter_resolution_tasks', record['source_task_id'])
        task['diagnostic_successor'] = task['successor'] = {'diagnostic_id': execution_id,
            'status': record['status'], 'action': 'diagnose', 'trial_id': record.get('trial_id')}
        engine.store.put('parameter_resolution_tasks', task['id'], task)
    await engine._read_consensus_check(run_id, bounded.source_task, engine, record)
    from .controlled_training_checks import unchanged
    await engine._read_consensus_check(run_id, unchanged, engine, record)
    await bounded.execute_once(engine, record, save_execution)
    if record['status'] == 'reviewing_results':
        from .experiment_decisions import _archive
        from .fixed_state_diagnostic import validate_reads
        ref = _archive(engine, run_id, {'binding': record['binding'], 'bounded_protocol': record['protocol'],
                                       'bounded_result': record['outcome']})
        context = {'training_context_protocol': 1, 'experiment_workflow_protocol': 1, 'research_cycle': record['cycle'],
                   'validation_evidence': ref, 'evidence_sha256': ref['sha256'],
                   'experiment_decision': {'bounded_result_review': True}}
        requests = record.setdefault('result_requests', {})
        for role in base.ROLES:
            req = requests.setdefault(role, {'purpose': 'experiment_diagnose:bounded-result:' + ref['sha256'],
                'instruction': bounded.RESULT_INSTRUCTION, 'context': copy.deepcopy(context), 'schema': bounded.RESULT})
            save_execution()
            if 'answer' not in req:
                req['answer'] = await harness.ask(run_id, role, req['purpose'], req['instruction'], req['context'], req['schema'])
                save_execution()
            owner(engine, run_id, role, req)
            if req['answer']['evidence_sha256'] != ref['sha256']:
                raise ValueError('Supplement result review is detached from execution')
            req['read'] = validate_reads(engine, record, role, req, bounded.RESULT_READS)
            save_execution()
        save_execution(status='completed', analyses=[{'role': role, **req['answer']} for role, req in requests.items()],
                       completed_at=now())
    save(status=record['status'], execution_id=execution_id)
    return {'tuning_pending': True, 'next_node': 'tuning', 'activity': 'bounded_evidence_followup',
            'diagnostic_id': record['id']}


async def dispatch(engine, harness, run_id):
    """Route a newly produced evidence gap without stopping the worker first."""
    from .bounded_diagnostics import execute as original_execute
    from .engine import ResearchBlocked
    result = await execute(engine, harness, run_id)
    if result is not None:
        return result
    try:
        return await original_execute(engine, harness, run_id)
    except ResearchBlocked:
        result = await execute(engine, harness, run_id)
        if result is None:
            raise
        return result
