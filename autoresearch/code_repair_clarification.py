"""Return disputed edit requests to their authors on the same frozen delivery.

Original votes and paid handoffs remain immutable. A clarification may retract
or correct an original objection, but cannot add another objection or approve a
different SPEC. Any surviving edit still needs the independent repair audits.
"""
from __future__ import annotations

import copy
import json

from . import code_repair_contract as repair
from .architecture_validation import operation

FIELD = 'code_repair_clarification'
TASK = '''本轮是同一冻结代码交付的修改意见澄清，不是新一轮扩大验收范围。
code_repair_clarification 保存原始意见和工程师逐项核对结果；只处理 code_repair_scope 中本角色的原 issue_id。
原映射可能引用了不对应本验收项的已有证据，必须实际补查后重新选择；旧映射不是代码修改授权。
先补读所缺实际文件及批准文档。可用 artifacts.list/read 读取本次 delivery_artifacts 中漏登的文件和
implementation.json；清单遗漏不能让实际文件永远无法核验。目录、摘要或别人的读取不能替代本人原文读取。
已证实的问题保留原修改及验证标准；证据反驳或已经满足的问题撤回，并解释依据。
原修改要求与批准规范冲突、混入不受支持子项时，仅纠正原意见使其忠实于批准条款，禁止扩大标准、改变
科学方案或修改批准文档。每个原 issue_id 必须有 clarification_responses：retained、corrected 或 withdrawn。
retained/corrected 的问题继续列入 issues/fixes/repair_findings，origin_id 使用原 issue_id，requirement_id 不变。
retained 的 fixes.change/verification 必须逐字沿用原标准，不能改写为大意；纠正要求只能标为 corrected 并说明批准依据。
withdrawn 不再列入 issues。每条保留或纠正的意见必须绑定本次真实读取的代码和批准实现文档。
仍不能判定时保留 needs_review/spec_conflict 并明确缺少什么，不能把猜测写成代码修改要求。
禁止新增问题或把旧问题冒充回归。所有原问题闭合且完整代码验收均有依据才可 approve=true。
本轮不执行科研实验，不把历史 smoke 宣称为重新运行。'''


def schema(base):
    result = copy.deepcopy(base)
    result['properties']['clarification_responses'] = {'type': 'array', 'maxItems': 90,
        'items': repair.obj({'issue_id': repair.TEXT,
            'resolution': {'enum': ['retained', 'corrected', 'withdrawn']}, 'reason': repair.TEXT,
            'evidence_indices': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                'items': {'type': 'integer', 'minimum': 0}}})}
    result['required'].append('clarification_responses')
    return result


def validate_responses(value, context, role):
    known = {i['issue_id']: i for i in context[repair.FIELD]['issues'] if i['role'] == role}
    rows = value.get('clarification_responses', [])
    responses = {r['issue_id']: r for r in rows}
    if len(rows) != len(known) or set(responses) != set(known):
        repair.fail('意见澄清必须逐项回应本角色原问题，不得遗漏、重复或新增。')
    findings = value.get('repair_findings', [])
    retained = {r.get('origin_id') for r in findings}
    expected = {key for key, row in responses.items() if row['resolution'] != 'withdrawn'}
    if retained != expected or len(findings) != len(expected):
        repair.fail('意见澄清的撤回与保留结果未对应原问题。')
    for key, row in responses.items():
        if row.get('resolution') not in {'retained', 'corrected', 'withdrawn'} or not row.get('reason', '').strip():
            repair.fail('意见澄清缺少明确结论和依据。')
        # The mapper's provisional repair_code is not independent authorization.
        # Original-role corrections still need same-requirement read evidence
        # and the two semantic audits before any code change can be dispatched.
        refs = value.get('code_evidence', [])
        indices = row.get('evidence_indices', [])
        if not indices or any(type(i) is not int or not 0 <= i < len(refs)
                or refs[i]['requirement_id'] != known[key]['requirement_id'] for i in indices):
            from .code_repair_response_correction import fail
            fail('保留、纠正或撤回原问题都必须引用本次实际读取的同一验收项依据。', value, context, role)
    return responses


def withdrawn_role(value, context, role):
    """A role can withdraw its own objections without approving other roles' work."""
    if (not context.get(FIELD) or value.get('approve') is not False
            or value.get('issues') or value.get('fixes') or value.get('repair_findings')):
        return False
    responses = validate_responses(value, context, role)
    return bool(responses) and all(r['resolution'] == 'withdrawn' for r in responses.values())


def context_for(source, proposal, *, host_verification=None, acceptance_scope=None):
    from .intelligence import parse_json
    parse_json(json.dumps(proposal, ensure_ascii=False), repair.PLAN_SCHEMA)
    known = {i['issue_id']: i for i in source['issues']}
    mapped = {i['issue_id']: i for i in proposal['items']}
    if (proposal['source_sha256'] != repair.sha(source) or len(proposal['items']) != len(known)
            or set(mapped) != set(known)):
        repair.fail('意见澄清没有绑定完整的原修改映射。')
    requirements = {r['id'] for r in source['acceptance_checks']}
    for key, row in mapped.items():
        issue = known[key]
        binding = issue.get('binding')
        if (row['requirement_id'] not in requirements
                or binding and binding['requirement_id'] != row['requirement_id']):
            repair.fail('意见澄清引用了无效或已改变的原问题依据。')
    context = copy.deepcopy(source['source_contexts']['director'])
    context.pop(FIELD, None)
    context[repair.FIELD] = {'version': repair.VERSION, 'spec_hash': source['spec_hash'],
        'changed_paths': [], 'previous_delivery_hash': source['delivery_hash'], 'issues': [
            {**copy.deepcopy(item), 'requirement_id': mapped[item['issue_id']]['requirement_id']}
            for item in source['issues']]}
    context[FIELD] = {'version': 1, 'workflow_id': source['workflow_id'],
        'source_sha256': repair.sha(source), 'proposal': copy.deepcopy(proposal),
        'mapping_problems': mapping_problems(source, proposal)}
    if host_verification is not None:
        context['host_verification'] = copy.deepcopy(host_verification)
        context[repair.FIELD]['document_memory_handoffs'] = copy.deepcopy(
            host_verification.get('document_memory_handoffs', []))
    if acceptance_scope is not None:
        from .definition_execution_followup import validate_acceptance_scope
        validate_acceptance_scope(acceptance_scope, context)
        context['definition_execution_acceptance'] = copy.deepcopy(acceptance_scope)
        context[repair.FIELD]['document_memory_handoffs'] += copy.deepcopy(acceptance_scope['prior_reviews'])
    return context


def mapping_problems(source, proposal):
    """Bad provisional pointers are work for clarification, never edit authority."""
    issues = {i['issue_id']: i for i in source['issues']}
    problems = []
    for row in proposal['items']:
        issue = issues.get(row['issue_id'])
        if issue is None:
            continue  # context_for/materialize reject unknown identities.
        refs = source['role_evidence'][issue['role']]
        invalid = [i for i in row['evidence_indices'] if type(i) is not int or not 0 <= i < len(refs)]
        unrelated = [i for i in row['evidence_indices'] if type(i) is int and 0 <= i < len(refs)
                     and refs[i]['requirement_id'] != row['requirement_id']]
        binding = issue.get('binding')
        changed = [key for key in ('disposition', 'evidence_indices')
                   if binding and binding[key] != row[key]]
        if invalid or unrelated or changed:
            problem = {'issue_id': row['issue_id'], 'role': issue['role'],
                'invalid_evidence_indices': invalid, 'unrelated_evidence_indices': unrelated}
            if changed:
                # The mapper cannot replace the original role's binding. Route
                # its provisional mismatch back to that role, keeping both the
                # original vote and proposal intact for independent review.
                problem['changed_binding_fields'] = changed
            problems.append(problem)
    return problems


def mapping(source):
    return {'source_sha256': repair.sha(source), 'items': [
        {'issue_id': item['issue_id'], **{k: item['binding'][k] for k in
            ('requirement_id', 'disposition', 'evidence_indices')},
         'reason': '原评审已在同一交付上补查：' + item['issue']}
        for item in source['issues']]}


@operation
def verify(engine, run_id, state, specification, attempt, original, record):
    from .codex_workflow import _frozen_spec_assignment
    current = repair.build_source(engine, run_id, state, specification, attempt)
    if (repair.sha(current) != repair.sha(original) or record['source_sha256'] != repair.sha(current)):
        repair.fail('意见澄清期间原评审、文档或代码版本发生变化。')
    if record.get('host_verification'):
        from .code_followup import validate_evidence
        validate_evidence(engine, run_id, attempt, record['host_verification'])
    expected = context_for(current, record['proposal'], host_verification=record.get('host_verification'),
                           acceptance_scope=record.get('acceptance_scope'))
    for role in repair.ROLES:
        frozen = _frozen_spec_assignment(engine, run_id, record['purpose'], role)
        if not frozen or frozen['context'] != expected:
            repair.fail('意见澄清缺少同版本原角色的冻结交接。')
    source = repair.build_source(engine, run_id, state, specification, attempt,
        votes=record['votes'], purpose=record['purpose'])
    if repair.sha(source) != record['resolved_source_sha256']:
        repair.fail('意见澄清结论发生变化。')
    if not source['issues'] and not all(v.get('approve') is True for v in record['votes']):
        repair.fail('没有代码修改项不等于三方批准。')
    return source


async def clarify(engine, h, run_id, state, specification, attempt, source, proposal, save,
                  *, host_verification=None, record_key='repair_clarification', acceptance_scope=None):
    from .codex_workflow import _reviews, _digest
    from .storage import now
    context = context_for(source, proposal, host_verification=host_verification, acceptance_scope=acceptance_scope)
    identity = [repair.sha(source), proposal]
    if host_verification is not None:
        identity.append(host_verification)
    if acceptance_scope is not None:
        identity.append(acceptance_scope)
    purpose = f'code_review:{_digest(state["id"])}:clarify-{repair.sha(identity)}'
    progress = attempt.get(record_key, {})
    new_clarification = progress.get('purpose') != purpose
    if new_clarification:
        attempt[record_key] = {'purpose': purpose, 'status': 'reviewing'}
        if acceptance_scope is not None:
            attempt[record_key].update(acceptance_scope=copy.deepcopy(acceptance_scope),
                                      host_verification=copy.deepcopy(host_verification))
    if new_clarification or attempt.get('execution_progress', {}).get('step') != 'repair_clarification':
        attempt['execution_progress'] = {'step': 'repair_clarification',
            'label': '原评审补查修改依据、澄清冲突；保留已确认的问题和原标准',
            'started_at': now(), 'updated_at': now()}
        save()
    if new_clarification:
        engine.store.event(run_id, 'implementation.repair_clarification',
            '修改意见已交回原评审补查，确认后继续修复。',
            {'workflow_id': state['id'], 'attempt': attempt['index'],
             'repair_count': sum(r['disposition'] == 'repair_code' for r in proposal['items']),
             'clarification_count': sum(r['disposition'] != 'repair_code' for r in proposal['items'])})
    votes = await _reviews(engine, h, run_id, purpose, context, delivery=True)
    resolved = await engine._read_consensus_check(run_id, repair.build_source,
        engine, run_id, state, specification, attempt, votes=votes, purpose=purpose)
    record = {'purpose': purpose, 'source_sha256': repair.sha(source),
        'proposal': copy.deepcopy(proposal), 'votes': votes,
        'resolved_source_sha256': repair.sha(resolved), 'status': 'completed',
        'specification': {k: copy.deepcopy(specification[k]) for k in ('spec_hash', 'document')},
        'acceptance_checks': copy.deepcopy(source['acceptance_checks'])}
    if host_verification is not None:
        record['host_verification'] = copy.deepcopy(host_verification)
    if acceptance_scope is not None:
        record['acceptance_scope'] = copy.deepcopy(acceptance_scope)
    await engine._read_consensus_check(run_id, verify,
        engine, run_id, state, specification, attempt, source, record)
    engine.check_active(run_id)
    attempt[record_key] = record
    save()
    return resolved, mapping(resolved), record


def verify_saved(engine, run_id, state, attempt, record):
    spec = {**record['specification'], 'spec': {'acceptance_checks': record['acceptance_checks']}}
    original = repair.build_source(engine, run_id, state, spec, attempt)
    return verify(engine, run_id, state, spec, attempt, original, record)
