"""One read-only planning round for an authenticated, non-executable diagnosis.

An empty change list is not a code amendment. Preserve that answer and let the
three roles design a concrete next diagnostic, or retain a scientific blocker.
Nothing here approves a proposal, alters evidence, or authorizes training.
"""
from __future__ import annotations

import copy

from .prose_schema import validate

from .experiment_workflow import sha

KEY = 'diagnostic_planning_recovery'
ERROR = 'A new architecture/diagnostic needs concrete reviewed changes'
ROLES = ('director', 'engineer', 'scout')


def is_hold(proposal):
    response = proposal.get('training_response') or {}
    return (proposal.get('action') == 'diagnose' and proposal.get('diagnosis') == 'training'
            and proposal.get('unit_id') is None and proposal.get('changes') == []
            and not proposal.get('parameter_search')
            and response.get('cause') == 'unknown' and response.get('cause_status') == 'hypothesis'
            and response.get('disposition') == 'architecture_assessment')


def eligible(record, evidence):
    proposal = record.get('rejected_proposal') or {}
    return (record.get('status') == 'invalid' and record.get('rejection') == ERROR
            and not record.get('proposal') and not record.get('votes') and not record.get(KEY)
            and evidence.get('dataset_optimization')
            and evidence.get('training_investigation', {}).get('required')
            and proposal.get('evidence_sha256') == record.get('evidence', {}).get('sha256')
            and is_hold(proposal))


def authenticate(engine, run_id, role, purpose, payload, answer):
    """Require the completed original answer, exact request, and owner hashes."""
    from .engine import Harness, digest
    hid = Harness.decision_key(run_id, role, purpose, **payload)
    handoff = engine.store.get('handoffs', hid) or {}
    inputs = engine.store.get('handoff_inputs', hid) or {}
    decision = engine.store.get('decisions', hid) or {}
    if (handoff.get('id') != hid or handoff.get('run_id') != run_id
            or handoff.get('to_role') != role or handoff.get('purpose') != purpose
            or handoff.get('status') != 'completed'
            or handoff.get('input_hash') != digest(payload)
            or handoff.get('output_hash') != digest(answer)
            or inputs.get('id') != hid or inputs.get('run_id') != run_id
            or any(inputs.get(k) != v for k, v in payload.items())
            or decision.get('role') != role or decision.get('purpose') != purpose
            or decision.get('result') != answer):
        raise ValueError('Diagnostic planning lost its completed author/request binding')
    validate(answer, payload['schema'])
    return hid


def original(engine, run_id, record, proposal, *, handoff_id=None):
    prefix = 'experiment_decide:' + record['evidence']['sha256']
    keys = [handoff_id] if handoff_id else engine.store.record_keys('decisions', run_id,
        predicate="json_extract(data,'$.role')=? AND json_extract(data,'$.purpose') LIKE ?",
        parameters=('engineer', prefix + '%'))
    for hid in reversed(keys):
        answer = engine.store.get('decisions', hid) or {}
        if answer.get('result') != proposal:
            continue
        inputs = engine.store.get('handoff_inputs', hid) or {}
        payload = {k: inputs.get(k) for k in ('instruction', 'context', 'schema')}
        purpose = answer.get('purpose', '')
        if ((purpose != prefix and not purpose.startswith(prefix + ':'))
                or (payload['context'] or {}).get('validation_evidence') != record['evidence']):
            raise ValueError('Diagnostic planning source changed the frozen evidence')
        if authenticate(engine, run_id, 'engineer', purpose, payload, proposal) != hid:
            raise ValueError('Diagnostic planning source has a different identity')
        return hid
    raise ValueError('Diagnostic planning requires the authentic original engineer answer')


def planning_reads(engine, run_id, record, saved, role, assignment):
    """Validate cited old reads against their original completed role assignment.

    The model was told to reuse earlier observations. Its original answer stays
    intact; only the validation view separates old and current receipt owners.
    Old receipts must already belong to this decision's verified initial reads.
    """
    from .engine import ResearchBlocked
    from .experiment_decisions import analysis_receipts, analysis_schema

    answer = assignment['answer']
    payload = {k: assignment[k] for k in ('instruction', 'context', 'schema')}
    hid = authenticate(engine, run_id, role, assignment['purpose'], payload, answer)
    current = engine.store.get('handoff_progress', hid) or {}
    current_ids = {r.get('result', {}).get('receipt_sha256') for r in current.get('tool_results', [])}
    missing = set(answer['read_receipts']) - current_ids
    retained = None
    if missing:
        initial = next((r for r in saved['initial_reads'] if r.get('role') == role), {})
        parent_id = initial.get('handoff_id', '')
        parent = engine.store.get('handoffs', parent_id) or {}
        inputs = engine.store.get('handoff_inputs', parent_id) or {}
        parent_payload = {k: inputs.get(k) for k in ('instruction', 'context', 'schema')}
        parent_answer = engine.store.get('decisions', parent_id) or {}
        origin = next((a for a in record['analyses'] if a.get('role') == role), {})
        original_answer = {k: v for k, v in origin.items() if k != 'role'}
        purpose = parent.get('purpose', '')
        prefix = 'experiment_diagnose:' + record['evidence']['sha256']
        if (not missing.issubset(initial.get('receipt_sha256s', []))
                or (purpose != prefix and not purpose.startswith(prefix + ':'))
                or (parent_payload['context'] or {}).get('validation_evidence') != record['evidence']
                or parent_answer.get('result') != original_answer
                or parent_payload['schema'] != analysis_schema(parent_payload['context'] or {})
                or authenticate(engine, run_id, role, purpose, parent_payload, original_answer) != parent_id):
            raise ResearchBlocked('Planning citations must belong to this role\'s original frozen analysis')
        verified = analysis_receipts(engine, run_id, {**original_answer, 'role': role}, parent_payload['context'],
            purpose=purpose, instruction=parent_payload['instruction'])
        if not missing.issubset(verified['receipt_sha256s']):
            raise ResearchBlocked('Planning citations lack verified original read receipts')
        retained = {'handoff_id': parent_id, 'input_sha256': sha(inputs),
            'answer_sha256': sha(original_answer), 'validation_sha256': sha(verified),
            'receipt_sha256s': sorted(missing)}
    # This is a host validation projection, never a replacement model answer.
    current_answer = {**answer, 'role': role,
        'read_receipts': [r for r in answer['read_receipts'] if r not in missing]}
    receipt = analysis_receipts(engine, run_id, current_answer, assignment['context'],
        purpose=assignment['purpose'], instruction=assignment['instruction'])
    if retained:
        receipt['receipt_sha256s'] = sorted(set(receipt['receipt_sha256s']) | missing)
        receipt['retained_read_origin'] = retained
    return receipt


async def advance(engine, harness, run_id, record, evidence, context, instruction, schema, checkpoint):
    from .engine import ResearchBlocked
    from .experiment_decisions import analysis_schema
    from .experiment_evidence_handoff import merge_reads

    saved = copy.deepcopy(record.get(KEY))
    if not saved and not eligible(record, evidence):
        return None
    if not saved:
        rejected = copy.deepcopy(record['rejected_proposal'])
        owner = original(engine, run_id, record, rejected)
        saved = {'version': 1, 'source_handoff_id': owner, 'source_proposal': rejected,
            'source_proposal_sha256': sha(rejected), 'source_rejection': record['rejection'],
            'evidence': copy.deepcopy(record['evidence']), 'initial_reads': copy.deepcopy(record['analysis_reads']),
            'assignments': {}, 'execution_authorized': False, 'scientific_approval': False}
        await checkpoint(**{KEY: saved}, status='planning_diagnostic')
    bindings = {'evidence_reference': saved['evidence'] == record['evidence'],
        'evidence_sha256': sha(evidence) == saved['evidence']['sha256'],
        'source_sha256': sha(saved['source_proposal']) == saved['source_proposal_sha256'],
        'source_rejection': saved['source_rejection'] == ERROR,
        'source_hold': is_hold(saved['source_proposal']),
        'source_owner': original(engine, run_id, record, saved['source_proposal'],
                                 handoff_id=saved['source_handoff_id']) == saved['source_handoff_id']}
    if not all(bindings.values()):
        raise ResearchBlocked('Diagnostic planning source/evidence binding changed: '
                              + ', '.join(k for k, v in bindings.items() if not v))
    digest = saved['source_proposal_sha256']
    reads = copy.deepcopy(saved['initial_reads'])
    if len(reads) != 3 or {r.get('role') for r in reads} != set(ROLES):
        raise ResearchBlocked('Diagnostic planning requires the original three independent analyses')
    for role in ROLES:
        assignment = saved['assignments'].get(role)
        if not assignment:
            request = copy.deepcopy(context)
            request['experiment_decision'][KEY] = {
                'source_proposal': saved['source_proposal'], 'source_rejection': saved['source_rejection'],
                'source_proposal_sha256': digest, 'execution_authorized': False,
                'scientific_approval': False, 'scope': 'one independent diagnostic design; no execution'}
            assignment = {'purpose': 'experiment_diagnose:' + context['evidence_sha256'] + ':diagnostic-planning-1:' + digest,
                'instruction': instruction + '\n本次只规划下一项诊断，不重复上一轮诊断结论。'
                    '原工程师已撤回完成建议，现有统计可读，但未知原因与已完成检查的 inconclusive 必须保留。'
                    '复用原三方分析与已读统计；用 experiments.analyze 及 compare 核对完成检查的预声明条件、'
                    '真实结果和所需轨迹，必要时 inspect_module(source) 核对具体实现，返回本角色真实读取回执。'
                    '在 hypotheses 中提出至多一个不同于已完成检查的可证伪设计，明确竞争解释、唯一干预或缺少的'
                    '观测、具体模块、控制及预声明判别/失败条件；observations 写证据依据，missing_evidence 写'
                    '尚缺的协议或真实阻塞。不得只因门禁要求 changes 而制造插桩需求。'
                    '禁止重复 result.json 提取、已有日志接口、已完成训练或因果检查，禁止事后延长原检查窗口、'
                    '改旧阈值/种子/数据划分、扩大参数网格或访问留出集。有限新设计仍须单种子、硬资源上限与独立审批。'
                    '这次没有执行授权，不能安排训练、激活候选或解除稳定性门禁。若无法提出合规且可区分原因的设计，'
                    '明确保留阻塞，不为推进而声称稳定或要求重复检查。suggested_action=diagnose。',
                'context': request, 'schema': analysis_schema(request)}
            saved['assignments'][role] = assignment
            await checkpoint(**{KEY: saved}, status='planning_diagnostic')
        if 'answer' not in assignment:
            assignment['answer'] = await harness.ask(run_id, role, assignment['purpose'],
                assignment['instruction'], assignment['context'], assignment['schema'])
            # Persist before validation: a bad or inconclusive answer is not retried forever.
            await checkpoint(**{KEY: saved}, status='planning_diagnostic')
        answer = assignment['answer']
        payload = {k: assignment[k] for k in ('instruction', 'context', 'schema')}
        authenticate(engine, run_id, role, assignment['purpose'], payload, answer)
        if answer.get('evidence_sha256') != context['evidence_sha256'] or answer.get('suggested_action') != 'diagnose':
            raise ResearchBlocked('Diagnostic planning cannot change evidence or certify optimization')
        receipt = planning_reads(engine, run_id, record, saved, role, assignment)
        assignment['read'] = receipt
        reads = merge_reads(reads, receipt)
        await checkpoint(**{KEY: saved})
    if 'synthesis' not in saved:
        request = vote_context({**record, KEY: saved}, context)
        narrowed = copy.deepcopy(schema)
        narrowed['properties']['action'] = {'const': 'diagnose'}
        narrowed['properties']['unit_id'] = {'const': None}
        narrowed['properties']['evidence_sha256'] = {'const': context['evidence_sha256']}
        narrowed['properties']['parameter_search'] = False
        narrowed['properties']['optimization_assessment'] = False
        response = narrowed['properties']['training_response']['properties']
        response['cause'] = {'const': 'unknown'}
        response['cause_status'] = {'const': 'hypothesis'}
        response['disposition'] = {'enum': ['instrument', 'architecture_assessment']}
        response['factor_paths'] = {'const': []}
        saved['synthesis'] = {'purpose': 'experiment_decide:' + context['evidence_sha256'] + ':diagnostic-planning-1:' + digest,
            'instruction': instruction + '\n综合本次 diagnostic_planning_recovery 中三方独立设计，仅提出一个'
                '可审核的局部诊断修改单；明确具体新增观测/模块、控制、判别、失败和硬资源限制。'
                '这是未知原因的诊断设计，不是已证实的修复。不得重复已有提取、插桩、训练或完成的受控检查，'
                '不得扩大网格或放宽稳定性标准。若没有具体可区分原因且符合现有局部修改通道的方案，'
                '保持 action=diagnose、diagnosis=training、unit_id=null、changes=[]、'
                'disposition=architecture_assessment，在 rationale/verification 写出真实阻塞和所需新协议。'
                '宿主会保留该意见而不执行，不要为满足 changes 字段编造修改需求。'
                '若存在具体方案，disposition=instrument；仍须原有三方投票、候选差异审查和 CPU 检查，'
                '规划不授权真实训练或候选激活。',
            'context': request, 'schema': narrowed}
    await checkpoint(**{KEY: saved}, analysis_reads=reads, status='proposing')
    return saved['synthesis']


def vote_context(record, context):
    saved = record.get(KEY)
    if not saved:
        return context
    result = copy.deepcopy(context)
    result['experiment_decision'][KEY] = {
        'source_proposal_sha256': saved['source_proposal_sha256'],
        'source_proposal': copy.deepcopy(saved['source_proposal']),
        'analyses': [{'role': role, **copy.deepcopy(a['answer'])}
                     for role, a in saved['assignments'].items() if a.get('read')],
        'approval_inherited': False, 'execution_authorized': False, 'scientific_approval': False}
    return result


def hold_message(proposal):
    return ('训练诊断仍阻塞：' + proposal['rationale'] + '\n下一步所需：' + proposal['verification']
            + '\n本次仅完成诊断规划；未批准训练、候选激活或参数稳定。')
