"""Return disputed edit requests to their authors on the same frozen delivery.

Original votes and paid handoffs remain immutable. A clarification may retract
or correct an original objection, but cannot add another objection or approve a
different SPEC. Any surviving edit still needs the independent repair audits.
"""
from __future__ import annotations

import copy
import json

from . import code_repair_contract as repair
from .architecture_validation import operation, once

FIELD = 'code_repair_clarification'
CONTEXT_VERSION = 2
MAPPING_DIAGNOSTIC_VERSION = 2
PARTITION = 'clarification_partition'


def enabled(context):
    version = context.get(FIELD, {}).get('version')
    return type(version) is int and version in (1, CONTEXT_VERSION)


TASK = '''本轮是同一冻结代码交付的修改意见澄清，不是新一轮扩大验收范围。
code_repair_clarification 保存原始意见和工程师逐项核对结果；只处理 code_repair_scope 中本角色的原 issue_id。
原映射可能引用了不对应本验收项的已有证据，必须实际补查后重新选择；旧映射不是代码修改授权。
先补读所缺实际文件及批准文档。可用 artifacts.list/read 读取本次 delivery_artifacts 中漏登的文件和
implementation.json；清单遗漏不能让实际文件永远无法核验。目录、摘要或别人的读取不能替代本人原文读取。
已证实的问题保留原修改及验证标准；证据反驳或已经满足的问题撤回，并解释依据。
原修改要求与批准规范冲突、混入不受支持子项时，仅纠正原意见使其忠实于批准条款，禁止扩大标准、改变
科学方案或修改批准文档。每个原 issue_id 必须有 clarification_responses：retained、corrected 或 withdrawn。
retained/corrected 的问题继续列入 issues/fixes/repair_findings，origin_id 使用原 issue_id，requirement_id 不变。
retained 可改写措辞和整理验证说明；实质纠正原要求时标为 corrected 并解释批准依据，不要求逐字复制。
同一原问题可以拆成多个有独立处置的子问题，分别填写 issue_index，沿用同一 origin_id 和 requirement_id；不得重复同一问题或扩大原要求。
withdrawn 不再列入 issues。具体代码修改必须绑定本次实际代码依据；待复核、文档依赖或缺执行回执可说明缺口。
仍不能判定时保留 needs_review/spec_conflict 并明确缺少什么，不能把猜测写成代码修改要求。
新发现的问题可用 origin_id=null、needs_review 记录并交独立复核，不得冒充已授权修改或严重回归。
所有原问题闭合且完整代码验收均有依据才可 approve=true。
本轮不执行科研实验，不把历史 smoke 宣称为重新运行。'''


def schema(base):
    result = copy.deepcopy(base)
    result['properties']['clarification_responses'] = {'type': 'array', 'maxItems': 90,
        'items': repair.obj({'issue_id': repair.TEXT,
            'resolution': {'enum': ['retained', 'corrected', 'withdrawn']}, 'reason': repair.TEXT,
            'evidence_indices': {'type': 'array', 'uniqueItems': True,
                'items': {'type': 'integer', 'minimum': 0}}})}
    result['required'].append('clarification_responses')
    return result


def request_schema(prepared, context, role, progress):
    """Guide issue identity without freezing draft prose, order or judgment."""
    if not context.get(FIELD):
        return prepared
    from .review_response_policy import acceptance_schema, FIELD as POLICY, VERSION
    known = {i['issue_id']: i['requirement_id'] for i in context[repair.FIELD]['issues'] if i['role'] == role}
    schema = acceptance_schema(prepared['schema'])
    for branch in schema.get('anyOf', [schema]):
        fields = branch.get('properties', {})
        replies = fields.get('clarification_responses', {})
        if 'items' not in replies or 'code_evidence' not in fields:
            continue
        replies.update(minItems=len(known), maxItems=len(known))
        replies.setdefault('allOf', []).extend(
            {'contains': {'properties': {'issue_id': {'const': key}}}, 'minContains': 1}
            for key in known)
        if not known:
            replies.pop('allOf', None)
        fields['repair_findings']['items'].setdefault('allOf', []).append({'oneOf': [
            {'properties': {'origin_id': {'const': key}, 'requirement_id': {'const': req}}}
            for key, req in known.items()] + [{'properties': {'origin_id': {'type': 'null'},
                'disposition': {'enum': ['needs_review', 'spec_conflict', 'needs_execution', 'needs_definition']},
                'regression': {'type': 'null'}}}]})
    return {**prepared, 'schema': schema,
            'context': {**prepared.get('context', {}), POLICY: VERSION}}


def validate_responses(value, context, role):
    known = {i['issue_id']: i for i in context[repair.FIELD]['issues'] if i['role'] == role}
    rows = value.get('clarification_responses', [])
    responses = {r['issue_id']: r for r in rows}
    if len(rows) != len(known) or set(responses) != set(known):
        from .code_repair_response_correction import fail
        fail('意见澄清必须逐项回应本角色原问题，不得遗漏、重复或新增。', value, context, role)
    findings = value.get('repair_findings', [])
    retained = {r.get('origin_id') for r in findings if r.get('origin_id') is not None}
    expected = {key for key, row in responses.items() if row['resolution'] != 'withdrawn'}
    if retained != expected:
        repair.fail('意见澄清的撤回与保留结果未对应原问题。')
    for key, row in responses.items():
        if row.get('resolution') not in {'retained', 'corrected', 'withdrawn'} or not row.get('reason', '').strip():
            repair.fail('意见澄清缺少明确结论和依据。')
        # A dependency or a withdrawal is useful without a manufactured code
        # citation. Only an actionable code request needs matching code proof.
        refs = value.get('code_evidence', [])
        indices = row.get('evidence_indices', [])
        from .review_response_policy import evidence_required
        required = any(evidence_required(f) for f in findings if f.get('origin_id') == key)
        if (required and not indices or any(type(i) is not int or not 0 <= i < len(refs)
                or required and refs[i]['requirement_id'] != known[key]['requirement_id'] for i in indices)):
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


def context_for(source, proposal, *, host_verification=None, acceptance_scope=None, deferred_resolution=None,
                context_version=CONTEXT_VERSION, host_constraints=None, evidence_work_item=None, role=None):
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
    context = copy.deepcopy(source['source_contexts'][role or 'director'])
    context.pop(FIELD, None)
    context[repair.FIELD] = {'version': repair.VERSION, 'spec_hash': source['spec_hash'],
        'changed_paths': [], 'previous_delivery_hash': source['delivery_hash'], 'issues': [
            {**copy.deepcopy(item), 'requirement_id': mapped[item['issue_id']]['requirement_id']}
            for item in source['issues']]}
    if type(context_version) is not int or context_version not in (1, CONTEXT_VERSION):
        raise ValueError('Unsupported clarification context version')
    context[FIELD] = {'version': context_version, 'workflow_id': source['workflow_id'],
        'source_sha256': repair.sha(source), 'proposal': copy.deepcopy(proposal),
        'mapping_problems': mapping_problems(source, proposal)}
    if context_version >= 2:
        context[FIELD]['mapping_diagnostic_version'] = MAPPING_DIAGNOSTIC_VERSION
    if host_verification is not None:
        if host_verification.get('identity',{}).get('invocation_version')==2:
            previous=context.get('host_verification')
            if previous and previous.get('id')!=host_verification.get('id'):
                context.setdefault('host_execution_receipts',[]).append(copy.deepcopy(previous))
        context['host_verification'] = copy.deepcopy(host_verification)
        context[repair.FIELD]['document_memory_handoffs'] = copy.deepcopy(
            host_verification.get('document_memory_handoffs', []))
    if acceptance_scope is not None:
        from .definition_execution_followup import validate_acceptance_scope
        validate_acceptance_scope(acceptance_scope, context)
        context['definition_execution_acceptance'] = copy.deepcopy(acceptance_scope)
        context[repair.FIELD]['document_memory_handoffs'] += copy.deepcopy(acceptance_scope['prior_reviews'])
    if deferred_resolution is not None:
        context['deferred_code_repair'] = copy.deepcopy(deferred_resolution)
    if host_constraints is not None:
        context['host_constraint_reconciliation'] = copy.deepcopy(host_constraints)
    if evidence_work_item is not None:
        context['implementation_evidence_work_item'] = copy.deepcopy(evidence_work_item)
        if evidence_work_item.get('evidence',{}).get('source_identity_receipts'):
            context['implementation_evidence_work_item']['review_instruction'] = (
                '本组由文献研究员主动检索了 source_identity_receipts，独立核对其提交的配置、原表引用、PDF字节、来源版本和身份关联。'
                '工具只返回已有记录，不代表身份链或科学结论已获批准；保留你的独立判断。'
                '复用已有视觉和文字结论，不重复看图，不将论文分数变成本地测量或可比提升；未知比较条件继续保留。')
    if role is not None:
        context[repair.FIELD]['issues'] = [i for i in context[repair.FIELD]['issues'] if i['role'] == role]
        context[repair.FIELD]['document_memory_handoffs'] = [
            row for row in context[repair.FIELD].get('document_memory_handoffs', []) if row['role'] == role]
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
        unrelated = [i for i in row['evidence_indices'] if row['disposition'] == 'repair_code'
                     and type(i) is int and 0 <= i < len(refs)
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


def _binding_context(context):
    """The original wire hash authenticates diagnostics; replay binds their inputs.

    Diagnostics are advisory host interpretations. Recomputing them after a
    deployment cannot invalidate a paid request whose actual frozen input hash
    and scientific sources have already been authenticated.
    """
    result = {**context, FIELD: dict(context[FIELD])}
    for key in ('mapping_problems', 'mapping_diagnostic_version'):
        result[FIELD].pop(key, None)
    return result


def _changed_paths(expected, actual, path=''):
    if type(expected) is not type(actual):
        return [path or '/']
    if isinstance(expected, dict):
        return [p for key in sorted(expected.keys() | actual.keys()) for p in (
            [path + '/' + key] if key not in expected or key not in actual else
            _changed_paths(expected[key], actual[key], path + '/' + key))][:24]
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [path]
        return [p for i, (left, right) in enumerate(zip(expected, actual))
                for p in _changed_paths(left, right, path + '/' + str(i))][:24]
    return [] if expected == actual else [path]


def _frozen_bindings(engine, run_id, purpose):
    from .codex_workflow import _frozen_spec_assignment
    bindings = {}
    for role in repair.ROLES:
        frozen = _frozen_spec_assignment(engine, run_id, purpose, role)
        if frozen:
            bindings[role] = {'handoff_id': frozen['id'], 'context_sha256': repair.sha(frozen['context']),
                              'context_version': frozen['context'].get(FIELD, {}).get('version')}
    return bindings


def partition_dispatch(engine, run_id, purpose, context, source, proposal, parent_purpose, options):
    """Freeze a deterministic successor for the known wrong-role legacy packet."""
    from . import code_review_partition as partition
    from .codex_workflow import _frozen_spec_assignment
    from .implementation_failures import ImplementationReviewReplayBlocked
    if not any(partition.FIELD in c for c in source['source_contexts'].values()):
        return purpose, context  # Explicit pre-partition v1/v2 compatibility.
    try:
        parent = partition.clarification_parent(engine, run_id, parent_purpose, source)
        request = {'version': 1, 'parent_purpose': parent_purpose, 'parent_batch_sha256': repair.sha(parent),
            'source_sha256': repair.sha(source), 'source': source, 'proposal': proposal, 'options': options}
        successor = purpose + ':partition-v1'
        previous_batch = partition.load(engine.store, run_id, purpose)
        if previous_batch is not None:
            # A previously correct partition packet must have our known contract.
            if previous_batch.get('clarification_parent') != {k: request[k] for k in (
                    'version', 'parent_purpose', 'parent_batch_sha256', 'source_sha256')}:
                raise ValueError('Unknown legacy clarification batch contract')
            return purpose, {**context, PARTITION: request}
        old = {}
        for role in repair.ROLES:
            frozen = _frozen_spec_assignment(engine, run_id, purpose, role)
            if not frozen:
                continue
            actual = frozen['context']
            from .code_review_catalog import PROTOCOL, VERSION
            expected = context_for(source, proposal, context_version=actual.get(FIELD, {}).get('version'), **options)
            if actual.get(PROTOCOL) in (1, VERSION) and actual[PROTOCOL] >= expected.get(PROTOCOL, 0):
                expected[PROTOCOL] = actual[PROTOCOL]
            if _binding_context(actual) != _binding_context(expected):
                raise ValueError('Unknown legacy clarification packet; source repair required')
            hid = frozen['id']
            hand = engine.store.get('handoffs', hid)
            progress = engine.store.get('handoff_progress', hid, {})
            if hand['status'] not in {'blocked', 'interrupted', 'completed', 'queued'}:
                raise ValueError('Legacy clarification is active or unknown')
            for slot in ('normal_request', 'finalization'):
                pending = progress.get(slot)
                if pending and pending.get('status') not in {'ready', 'response_saved'}:
                    raise ValueError('Legacy clarification has an unsettled request')
            old[role] = {'handoff': hand, 'input': frozen, 'progress': progress,
                         'decision': engine.store.get('decisions', hid)}
        if old:
            key = f'{run_id}:{successor}'
            saved = engine.store.get('clarification_successors', key)
            body = {'version': 1, 'source_sha256': repair.sha(source), 'parent_batch_sha256': repair.sha(parent),
                    'old_purpose': purpose, 'new_purpose': successor, 'failed_assignments': old,
                    'budget_at_freeze': saved['body']['budget_at_freeze'] if saved else engine.store.get('budgets', run_id),
                    'recovery': 'refreeze_role_scopes', 'scientific_criteria_changed': False}
            if saved and (saved['body'] != body or saved['sha256'] != repair.sha(body)
                          or saved['id'] != key or saved['run_id'] != run_id):
                raise ValueError('Legacy clarification failure changed after successor freeze')
            if not saved:
                row = {'id': key, 'run_id': run_id, 'body': body, 'sha256': repair.sha(body)}
                def preserve(previous):
                    if previous and previous != row:
                        raise ValueError('Concurrent clarification successor freeze conflict')
                    return previous or row
                engine.store.mutate('clarification_successors', key, preserve)
        return successor, {**context, PARTITION: request}
    except (ValueError, KeyError, TypeError) as exc:
        raise ImplementationReviewReplayBlocked(str(exc), diagnostic='clarification_parent_contract',
            purpose=purpose, parent_purpose=parent_purpose) from exc


@once
def verify(engine, run_id, state, specification, attempt, original, record):
    from .codex_workflow import _frozen_spec_assignment
    from .implementation_failures import ImplementationReviewReplayBlocked
    replay_version = record.get('replay_contract_version', 1)
    details = {'workflow_id': state['id'], 'attempt': attempt['index'], 'purpose': record['purpose']}
    if type(replay_version) is not int or replay_version not in (1, CONTEXT_VERSION):
        raise ImplementationReviewReplayBlocked('不支持已保存澄清记录的回放版本。',
            diagnostic='clarification_replay_version_unsupported', replay_contract_version=replay_version, **details)
    bindings = record.get('frozen_context_bindings')
    if ((bindings is not None and (not isinstance(bindings, dict) or set(bindings) != set(repair.ROLES)))
            or replay_version == CONTEXT_VERSION and bindings is None):
        raise ImplementationReviewReplayBlocked('已保存澄清记录缺少完整的三方交接绑定。',
            diagnostic='clarification_saved_bindings_incomplete', **details)
    current = repair.build_source(engine, run_id, state, specification, attempt)
    if (repair.sha(current) != repair.sha(original) or record['source_sha256'] != repair.sha(current)):
        repair.fail('意见澄清期间原评审、文档或代码版本发生变化。')
    if record.get('host_verification'):
        from .code_followup import validate_evidence
        validate_evidence(engine, run_id, attempt, record['host_verification'])
    if record.get('deferred_resolution'):
        from .code_repair_deferred import validate_scope
        validate_scope(engine, run_id, current, record['deferred_resolution'])
    if record.get('host_constraint_reconciliation'):
        from .host_constraint_reconciliation import context as host_context
        value = record['host_constraint_reconciliation']
        if host_context(engine, run_id, value, current) != value:
            repair.fail('宿主约束补查没有绑定真实回执及原代码问题。')
    if record.get('evidence_work_item'):
        from .code_evidence_queue import validate_reference
        validate_reference(engine,run_id,record['evidence_work_item'],current)
    expected_contexts = {}
    for role in repair.ROLES:
        frozen = _frozen_spec_assignment(engine, run_id, record['purpose'], role)
        from .code_review_catalog import PROTOCOL, VERSION
        details = {'workflow_id': state['id'], 'attempt': attempt['index'],
                   'role': role, 'purpose': record['purpose']}
        if not frozen:
            raise ImplementationReviewReplayBlocked('缺少 ' + role + ' 的原冻结交接。',
                diagnostic='clarification_handoff_missing', **details)
        actual = frozen['context']
        version = actual.get(FIELD, {}).get('version')
        if type(version) is not int or version not in (1, CONTEXT_VERSION):
            raise ImplementationReviewReplayBlocked('不支持 ' + role + ' 的交接版本。',
                diagnostic='clarification_context_version_unsupported', context_version=version, **details)
        marker = actual.get(PARTITION)
        projection = (role, version, actual.get(PROTOCOL), repair.sha(marker))
        if projection not in expected_contexts:
            expected_contexts[projection] = context_for(current, record['proposal'],
                host_verification=record.get('host_verification'), acceptance_scope=record.get('acceptance_scope'),
                deferred_resolution=record.get('deferred_resolution'), context_version=version,
                host_constraints=record.get('host_constraint_reconciliation'),
                evidence_work_item=record.get('evidence_work_item'), role=role if marker else None)
        expected = expected_contexts[projection]
        if marker:
            from . import code_review_partition as partition
            parent = partition.clarification_parent(engine, run_id, marker['parent_purpose'], current)
            expected = {**expected, PARTITION: {'version': 1, 'role': role,
                'parent_purpose': parent['purpose'], 'parent_batch_sha256': repair.sha(parent),
                'source_sha256': repair.sha(current)}}
            partition.authenticate(engine, run_id, role, frozen['id'], actual)
        elif actual.get('code_review_assignment'):
            raise ImplementationReviewReplayBlocked('旧澄清缺少认证批次，需要新的宿主角色分派。',
                diagnostic='clarification_partition_successor_required', **details)
        matched = expected
        if (expected.get('delivery_directory_protocol') == 1 and expected.get('implementation_alignment_protocol') == 1
                and actual.get(PROTOCOL) in (1, VERSION) and actual[PROTOCOL] >= expected.get(PROTOCOL, 0)):
            matched = {**expected, PROTOCOL: actual[PROTOCOL]}
        expected_binding, actual_binding = _binding_context(matched), _binding_context(actual)
        if actual_binding != expected_binding:
            paths = _changed_paths(expected_binding, actual_binding)
            raise ImplementationReviewReplayBlocked(role + ' 的冻结交接内容不一致：' + '、'.join(paths),
                diagnostic='clarification_context_mismatch', handoff_id=frozen['id'],
                context_version=version, changed_paths=paths,
                expected_binding_sha256=repair.sha(expected_binding),
                actual_binding_sha256=repair.sha(actual_binding), **details)
        if bindings is not None and bindings.get(role) != {
                'handoff_id': frozen['id'], 'context_sha256': repair.sha(actual), 'context_version': version}:
            raise ImplementationReviewReplayBlocked(role + ' 的已保存交接绑定发生变化。',
                diagnostic='clarification_saved_binding_changed', handoff_id=frozen['id'], **details)
    source = repair.build_source(engine, run_id, state, specification, attempt,
        votes=record['votes'], purpose=record['purpose'])
    if repair.sha(source) != record['resolved_source_sha256']:
        repair.fail('意见澄清结论发生变化。')
    if not source['issues'] and not all(v.get('approve') is True for v in record['votes']):
        repair.fail('没有代码修改项不等于三方批准。')
    return source


@operation
def _finalize(engine,run_id,state,specification,attempt,source,proposal,votes,purpose,metadata):
    """Build and authenticate a delivered review in one bounded proof scope."""
    resolved=repair.build_source(engine,run_id,state,specification,attempt,votes=votes,purpose=purpose)
    record={'purpose':purpose,'source_sha256':repair.sha(source),'proposal':copy.deepcopy(proposal),
        'votes':votes,'resolved_source_sha256':repair.sha(resolved),'status':'completed',
        'specification':{k:copy.deepcopy(specification[k]) for k in ('spec_hash','document')},
        'acceptance_checks':copy.deepcopy(source['acceptance_checks']),
        'replay_contract_version':CONTEXT_VERSION,
        'frozen_context_bindings':_frozen_bindings(engine,run_id,purpose)}
    record.update({k:copy.deepcopy(v) for k,v in metadata.items() if v is not None})
    verify(engine,run_id,state,specification,attempt,source,record)
    return resolved,record


@operation
def verify_saved(engine, run_id, state, attempt, record):
    spec = {**record['specification'], 'spec': {'acceptance_checks': record['acceptance_checks']}}
    original = repair.build_source(engine, run_id, state, spec, attempt)
    return verify(engine, run_id, state, spec, attempt, original, record)
