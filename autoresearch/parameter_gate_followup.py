"""One resumable, read-only successor to a completed parameter artifact review.

The successor interprets the original gates; it cannot change them, suppress
alerts, certify stability, or inherit an approval from the artifact review.
"""
from __future__ import annotations

import copy

from . import experiment_evidence_handoff as artifacts
from .artifact_fact_review import reference
from .diagnostic_planning_recovery import authenticate
from .experiment_workflow import sha
from .training_adjustment import obj

KEY = 'parameter_gate_followup'
ROLES = ('director', 'engineer', 'scout')
ASSESSMENT_PATH = '/dataset_optimization/architecture_certificate/body/assessment'
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1200}
PATHS = {'type': 'array', 'minItems': 1, 'maxItems': 6,
         'items': {'type': 'string', 'minLength': 1, 'maxLength': 400}}


def eligible(record, evidence, proposal):
    scope = evidence.get('dataset_optimization') or {}
    review = record.get(artifacts.KEY, {}).get('independent_review') or {}
    return (scope.get('phase') == 'parameters' and bool(scope.get('architecture_certificate'))
            and artifacts.requested(proposal) and not record.get('proposal') and not record.get('votes')
            and len(review.get('results', [])) == 3
            and {r.get('role') for r in review['results']} == set(ROLES)
            and proposal.get('artifact_review_sha256') == sha(review['results']))


def _answer_owner(engine, run_id, role, assignment, answer):
    return authenticate(engine, run_id, role, assignment['purpose'],
                        {k: assignment[k] for k in ('instruction', 'context', 'schema')}, answer)


def director_references(evidence, ref, director):
    """Resolve only verified host summary aliases; retain the paid answer verbatim."""
    from .experiment_decisions import _pointer

    context, answer = director['context'], director['answer']
    if (sha(evidence) != ref['sha256'] or context.get('validation_evidence') != ref
            or context.get('evidence_sha256') != ref['sha256']
            or answer.get('evidence_sha256') != ref['sha256']):
        raise ValueError('Parameter gate reference evidence binding changed')
    if {r['criterion_path'] for r in answer['clauses']} != {
            ASSESSMENT_PATH + '/criteria', ASSESSMENT_PATH + '/stability_rationale'}:
        raise ValueError('Parameter gate assessment must address both frozen criteria and training stability clauses')
    aliases = {'/counts': ('/ledger/counts', 'identity'),
               '/failure_count': ('/failure_categories', 'length')}
    from .parameter_gate_references import resolve
    resolved, context_refs, errors = [], [], []
    for index, clause in enumerate(answer['clauses']):
        for path in [clause['criterion_path'], *clause['evidence_paths']]:
            source, operation = aliases.get(path, (path, 'identity'))
            try:
                derived = resolve(path, evidence, director)
                if derived is not None:
                    target = context_refs if 'context_path' in derived else resolved
                    target.append({'clause_index': index, **derived})
                    continue
                value = _pointer(evidence, source)
                projected = len(value) if operation == 'length' else value
                if path in aliases:
                    supplied = context['experiment_decision'][path[1:]]
                    if sha(supplied) != sha(projected):
                        raise ValueError('summary value differs from its frozen source')
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                errors.append(f'Parameter gate clause {index} cites invalid evidence: '
                              f'{path} (source {source}): {exc}')
                continue
            if path in aliases:
                resolved.append({'clause_index': index, 'cited_path': path,
                    'evidence_path': source, 'operation': operation, 'value_sha256': sha(projected)})
    if errors:
        raise ValueError('\n'.join(errors))
    return {'version': 1, 'evidence': copy.deepcopy(ref), 'answer_sha256': sha(answer),
            'summary_aliases': resolved,
            **({'context_references': context_refs} if context_refs else {}),
            'execution_authorized': False, 'scientific_approval': False}


def _verify(engine, run_id, record, evidence, saved):
    """Reauthenticate paid answers and same-role reads, never trust saved flags."""
    from .experiment_artifact_review import require_binding

    source = saved['source_proposal']
    review = record[artifacts.KEY]['independent_review']
    artifact_ref = reference(record[artifacts.KEY])
    material = engine.store.get('experiment_evidence', artifact_ref['id']) or {}
    base = engine.store.get('experiment_evidence', record['evidence']['id']) or {}
    assessment = evidence['dataset_optimization']['architecture_certificate']['body']['assessment']
    if (record.get('run_id') != run_id or saved['evidence'] != record['evidence']
            or sha(evidence) != record['evidence']['sha256']
            or base.get('run_id') != run_id or sha(base.get('summary')) != sha(evidence)
            or sha(source) != saved['source_proposal_sha256']
            or saved['source_assignment'] != review['synthesis_assignment']
            or saved['assessment'] != assessment
            or material.get('run_id') != run_id
            or sha(material.get('summary')) != artifact_ref['sha256']
            or material['summary'].get('source_evidence') != record['evidence']
            or source.get('artifact_evidence_sha256') != artifact_ref['sha256']
            or not require_binding({**record, 'proposal': source})):
        raise ValueError('Parameter gate follow-up source/evidence/criteria binding changed')
    _answer_owner(engine, run_id, 'engineer', saved['source_assignment'], source)
    assignment = review['assignment']
    for role in ROLES:
        answer = next(r for r in review['results'] if r['role'] == role)
        _answer_owner(engine, run_id, role, assignment, {k: v for k, v in answer.items() if k != 'role'})
        read = artifacts.require_reads(engine, run_id, role, assignment['purpose'],
            assignment['instruction'], assignment['context'], assignment['schema'])
        old = next(r for r in review['reads'] if r['role'] == role)
        if old['handoff_id'] != read['handoff_id']:
            raise ValueError('Parameter gate follow-up lost the original role-owned artifact reads')


def context_for(record, context):
    from .parameter_gate_references import host_gate_requirements
    saved = record.get(KEY)
    if not saved:
        return context
    result = copy.deepcopy(context)
    result['experiment_decision'][artifacts.KEY] = artifacts.packet(record[artifacts.KEY], for_review=True)
    review = record[artifacts.KEY]['independent_review']
    result['artifact_review_results'] = {'sha256': sha(review['results']), 'results': review['results'],
        'execution_authorized': False, 'scientific_approval': False}
    result['experiment_decision'][KEY] = {
        'source_proposal': saved['source_proposal'], 'source_proposal_sha256': saved['source_proposal_sha256'],
        'frozen_assessment': saved['assessment'], 'assessment_sha256': sha(saved['assessment']),
        'director_assessment': saved.get('director', {}).get('answer'),
        **({'director_reference_resolution': saved['director_reference_resolution']}
           if 'director_reference_resolution' in saved else {}),
        'original_role_reads': review['reads'],
        'execution_authorized': False, 'scientific_approval': False,
        'host_gate_requirements': host_gate_requirements()}
    return result


async def advance(engine, harness, run_id, record, evidence, context, instruction, checkpoint,
                  *, proposal=None, assignment=None):
    """Dispatch director once, then a new engineer proposal with current contracts."""
    saved = copy.deepcopy(record.get(KEY))
    if not saved:
        if not eligible(record, evidence, proposal or {}):
            return None
        saved = {'version': 1, 'source_proposal': copy.deepcopy(proposal),
            'source_proposal_sha256': sha(proposal), 'source_assignment': copy.deepcopy(assignment),
            'source_rejection': record.get('rejection'), 'evidence': copy.deepcopy(record['evidence']),
            'assessment': copy.deepcopy(evidence['dataset_optimization']['architecture_certificate']['body']['assessment']),
            'execution_authorized': False, 'scientific_approval': False}
    _verify(engine, run_id, record, evidence, saved)
    if not record.get(KEY):
        await checkpoint(**{KEY: saved}, status='reviewing_parameter_gates')
        engine.store.event(run_id, 'experiment.parameter_gate_followup',
            '产物复核已完成；将剩余原判据处置交 director，再交工程师及独立投票。',
            {'decision_id': record['id'], 'source_proposal_sha256': saved['source_proposal_sha256'],
             'owner': 'director', 'execution_authorized': False})
    if 'director' not in saved:
        from .parameter_gate_references import GUIDANCE as reference_guidance
        schema = obj({
            'evidence_sha256': {'const': record['evidence']['sha256']},
            'source_proposal_sha256': {'const': saved['source_proposal_sha256']},
            'assessment_sha256': {'const': sha(saved['assessment'])},
            'clauses': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': obj({
                'criterion_path': {'enum': [ASSESSMENT_PATH + '/criteria', ASSESSMENT_PATH + '/stability_rationale']},
                'outcome': {'enum': ['met', 'unmet', 'undetermined']},
                'evidence_paths': PATHS, 'reason': TEXT})},
            'remaining_blockers': {'type': 'array', 'maxItems': 6, 'items': TEXT},
            'next_step': TEXT, 'execution_authorized': {'const': False}, 'scientific_approval': {'const': False}})
        schema['properties']['clauses']['allOf'] = [
            {'contains': {'properties': {'criterion_path': {'const': ASSESSMENT_PATH + suffix}},
                          'required': ['criterion_path']}}
            for suffix in ('/criteria', '/stability_rationale')]
        saved['director'] = {
            'purpose': 'experiment_diagnose:' + record['evidence']['sha256'] + ':parameter-gates-1:' + saved['source_proposal_sha256'],
            'instruction': (
                '你是 director，继续处理已完成产物复核后的原判据适用性，不重做事实复核。'
                '逐项核对原 criteria 的真实参数搜索、同种子不退化，以及原 stability_rationale 中的'
                '训练退化、失败记录、未执行网格点和停止依据。clauses 必须同时覆盖 criteria 与 stability_rationale。'
                '复用已绑定的三方产物结论和本角色原读取回执；只在具体条款需要时定向 compare 原冻结证据。'
                '说明每项已满足、不满足或原条款适用性未决，并引用原快照真实 evidence_paths。'
                '摘要 counts 的原始路径是 /ledger/counts；failure_count 是 /failure_categories 的条目数，'
                '引用失败记录请用 /failure_categories，不把摘要字段名当成原始路径。'
                '区分最佳点选优与后期轨迹，未知根因保持未知。不得把描述性告警自动当成已证实缺陷，'
                '也不能用最佳分数或文件一致性抹去训练告警。host_gate_requirements 是仍生效的宿主约束，'
                '本次解释不能豁免。若没有合法下一步，明确阻塞条款、缺项与责任，不虚构支持性结论。'
                '不新增或修改阈值，不重读全部产物、不训练、改代码、扩大网格或访问留出集。'
                '你的意见将交工程师形成提案并独立投票，本次没有执行授权或稳定性批准。') + reference_guidance,
            'context': context_for({**record, KEY: saved}, context), 'schema': schema}
        await checkpoint(**{KEY: saved}, status='reviewing_parameter_gates')
    director = saved['director']
    if 'answer' not in director:
        director['answer'] = await harness.ask(run_id, 'director', director['purpose'],
            director['instruction'], director['context'], director['schema'])
        await checkpoint(**{KEY: saved}, status='reviewing_parameter_gates')
    _answer_owner(engine, run_id, 'director', director, director['answer'])
    from .parameter_gate_references import verify_context
    verify_context(saved)
    resolution = director_references(evidence, record['evidence'], director)
    if saved.get('director_reference_resolution', resolution) != resolution:
        raise ValueError('Parameter gate director reference resolution changed')
    saved['director_reference_resolution'] = resolution
    if 'synthesis' not in saved:
        from .experiment_decisions import PROPOSAL
        from .training_investigation import proposal_schema
        from .proposal_request_contract import schema as contract, VERSION, guidance
        schema = contract(proposal_schema(PROPOSAL, evidence), evidence, VERSION)
        for field, value in (('artifact_evidence_sha256', reference(record[artifacts.KEY])['sha256']),
                             ('artifact_review_sha256', sha(record[artifacts.KEY]['independent_review']['results']))):
            schema['properties'][field] = {'const': value}
            schema['required'].append(field)
        saved['synthesis'] = {
            'purpose': 'experiment_decide:' + record['evidence']['sha256'] + ':parameter-gates-1:' + sha(director['answer']),
            'instruction': instruction + guidance(VERSION) + (
                '\n本次是已完成产物复核后的原门禁处置后继任务。结合 parameter_gate_followup 中 director '
                '逐条判断形成唯一具体下一步，复用已完成读取，不再次请求同一产物复核。'
                'director_reference_resolution 记录旧答复摘要引用对应的原始路径；'
                '本次 evidence_paths 使用 summary_aliases 中的 evidence_path，不复用摘要别名。'
                'context_references 是已认证请求中的宿主规则或历史提案，仅供文字说明，'
                '不能放入本次 evidence_paths 或 training_response.evidence_paths 充当实测证据。'
                'director 意见没有批准权限，不覆盖 host_gate_requirements，不修改原判据或告警。'
                '若有合法执行或完成提案，仍经过所有宿主门禁及三方独立投票。若证据仍不足，'
                '使用 diagnose、diagnosis=insufficient_evidence、unit_id=null、changes=[]，'
                '在 rationale/verification 写明具体未满足条款、所需证据或新协议及责任方。'
                '这份只读处置将交三方投票并保存为明确阻塞，不再被当作重复取证。'
                '不为通过字段约束把 unknown 改成 supported，不新增训练或补造修改需求。'),
            'context': context_for({**record, KEY: saved}, context), 'schema': schema}
    await checkpoint(**{KEY: saved}, status='proposing')
    return saved['synthesis']


def disposition_vote_schema(evidence_sha256, proposal_sha256):
    schema = obj({'evidence_sha256': {'const': evidence_sha256},
        'proposal_sha256': {'const': proposal_sha256}, 'approve': {'type': 'boolean'},
        'rationale': TEXT, 'issues': {'type': 'array', 'maxItems': 6, 'items': TEXT}})
    schema['allOf'] = [{'if': {'properties': {'approve': {'const': True}}},
        'then': {'properties': {'issues': {'maxItems': 0}}},
        'else': {'properties': {'issues': {'minItems': 1}}}}]
    return schema


async def disposition_vote(engine, harness, run_id, record, saved, role, checkpoint):
    """Ask the same reviewer once about ambiguous legacy fields; never infer approval."""
    original = saved['votes'][role]
    owner = _answer_owner(engine, run_id, role, saved['vote_assignment'], original)
    if not original['approve'] or not original['issues']:
        if role in saved.get('vote_clarifications', {}):
            raise ValueError('Parameter gate clarification source vote changed')
        return original
    clarifications = saved.setdefault('vote_clarifications', {})
    if role not in clarifications:
        context = copy.deepcopy(saved['vote_assignment']['context'])
        context['experiment_decision'][KEY]['disposition_vote_clarification'] = {
            'prior_vote': copy.deepcopy(original), 'original_handoff_id': owner,
            'approval_inherited': False, 'execution_authorized': False}
        clarifications[role] = {
            'source_handoff_id': owner, 'source_answer_sha256': sha(original),
            'purpose': saved['vote_assignment']['purpose'] + ':issue-semantics-1:' + sha(original),
            'instruction': '仅澄清你对同一只读阻塞登记的投票字段，不重新展开科研评审。'
                '原答复 approve=true 且 issues 非空，宿主不能推断其中是否有反对本次登记的异议。'
                '若同意该登记，approve=true、issues=[]，原有科研阻塞、证据缺口和后续门禁保留在 rationale；'
                '若反对该登记或仍有具体可派发任务被遗漏，approve=false，并在 issues 明列异议。'
                '允许明确拒绝，不继承原批准。保持原提案、原证据和未知结论；不新增阈值、训练或科学批准。',
            'context': context,
            'schema': disposition_vote_schema(record['evidence']['sha256'], saved['disposition_sha256'])}
        await checkpoint(**{KEY: saved}, status='clarifying_parameter_gate_disposition')
    clarification = clarifications[role]
    if (clarification['source_handoff_id'] != owner
            or clarification['source_answer_sha256'] != sha(original)):
        raise ValueError('Parameter gate clarification source binding changed')
    if 'answer' not in clarification:
        clarification['answer'] = await harness.ask(run_id, role, clarification['purpose'],
            clarification['instruction'], clarification['context'], clarification['schema'])
        await checkpoint(**{KEY: saved}, status='clarifying_parameter_gate_disposition')
    _answer_owner(engine, run_id, role, clarification, clarification['answer'])
    return clarification['answer']


async def finish_hold(engine, harness, run_id, record, proposal, assignment, checkpoint):
    """Independently review the read-only disposition; never approve execution."""
    from .engine import ResearchBlocked
    previous_status = record[KEY].get('status')
    saved = copy.deepcopy(record[KEY])
    if assignment != saved['synthesis']:
        raise ValueError('Parameter gate disposition must belong to its successor assignment')
    _answer_owner(engine, run_id, 'engineer', assignment, proposal)
    if 'disposition' not in saved:
        saved['disposition'] = copy.deepcopy(proposal)
        saved['disposition_sha256'] = sha(proposal)
        saved['votes'] = {}
    if saved['disposition'] != proposal or saved['disposition_sha256'] != sha(proposal):
        raise ValueError('Parameter gate disposition changed after independent review started')
    if 'vote_assignment' not in saved:
        context = copy.deepcopy(assignment['context'])
        context.update(decision_proposal=proposal, proposal_sha256=sha(proposal))
        saved['vote_assignment'] = {
            'purpose': 'experiment_vote:' + sha(proposal) + ':parameter-gate-disposition-1',
            'instruction': '独立评审原门禁的只读阻塞处置。复用本角色已核验读取，检查 director 条款判断和工程师'
                '处置是否准确保留原判据、告警、未知和真正缺项。批准仅表示同意该阻塞登记，不是训练、'
                '参数稳定或数据集切换授权。若仍有具体可派发任务未处理或条款被曲解，拒绝并写出具体异议。'
                'issues 仅填写阻止同意本次登记的异议；同意时 approve=true、issues=[]，'
                '原有科研阻塞、证据缺口和后续门禁保留在 rationale。',
            'context': context, 'schema': disposition_vote_schema(record['evidence']['sha256'], sha(proposal))}
        await checkpoint(**{KEY: saved}, status='reviewing_parameter_gate_disposition')
    request = saved['vote_assignment']
    votes = {}
    for role in ROLES:
        if role not in saved['votes']:
            saved['votes'][role] = await harness.ask(run_id, role, request['purpose'],
                request['instruction'], request['context'], request['schema'])
            await checkpoint(**{KEY: saved}, status='reviewing_parameter_gate_disposition')
        votes[role] = await disposition_vote(engine, harness, run_id, record, saved, role, checkpoint)
    agreed = all(v['approve'] and not v['issues'] for v in votes.values())
    saved['status'] = 'blocked' if agreed else 'disputed'
    message = ('原判据处置已完成独立复核，仍有科研阻塞：' if agreed else '原判据处置仍有独立复核异议：')
    detail = proposal['verification'] if agreed else '; '.join(
        role + ': ' + v['rationale'] + ' ' + '; '.join(v['issues'])
        for role, v in votes.items() if not v['approve'] or v['issues'])
    message += proposal['rationale'] + '\n下一步所需：' + detail
    changed = previous_status != saved['status']
    await checkpoint(**{KEY: saved}, status='blocked_parameter_gates', rejection=message)
    if changed:
        engine.store.event(run_id, 'experiment.parameter_gate_disposition', message,
            {'decision_id': record['id'], 'status': saved['status'], 'execution_authorized': False})
    raise ResearchBlocked(message)
