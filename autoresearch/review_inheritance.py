"""Same-role, same-version evidence and explicit incremental review inheritance.

Paid inputs, responses and read ledgers remain immutable. Host reuse has its
own provenance; it never turns an unresolved opinion into an approval.
"""
from __future__ import annotations

import copy
import json

from .architecture_validation import operation, once, source_read

FIELD = 'code_review_inheritance'
VERSION = 1
ACK = 'inherit_unchanged'


def _fail(message):
    from .implementation_failures import ImplementationReviewReplayBlocked
    raise ImplementationReviewReplayBlocked(message, diagnostic='review_inheritance_invalid')


def _same_version(left, right):
    if left.get('code_review_assignment') != right.get('code_review_assignment'):
        return False
    fields = ('proposal_hash', 'spec_hash', 'code_sha256', 'delivery_hash',
              'implementation_alignment_contract')
    return (all(left.get(k) == right.get(k) for k in fields)
            and all(left.get(k) for k in ('spec_hash', 'delivery_hash'))
            and left.get('implementation_document', {}).get('sha256')
                == right.get('implementation_document', {}).get('sha256')
            and {d['document_id']: d['sha256'] for d in left.get('indexed_documents', [])}
                == {d['document_id']: d['sha256'] for d in right.get('indexed_documents', [])})


def authenticated(engine, run_id, role, key, context, *, sealed=False):
    from .engine import Harness, digest
    hand = engine.store.get('handoffs', key, {})
    frozen = engine.store.get('handoff_inputs', key, {})
    payload = {k: frozen.get(k) for k in ('instruction', 'context', 'schema')}
    if (hand.get('id') != key or hand.get('run_id') != run_id or hand.get('to_role') != role
            or frozen.get('id') != key or frozen.get('run_id') != run_id
            or not str(hand.get('purpose', '')).startswith('code_review:')
            or hand.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, role, hand['purpose'], **payload) != key
            or not _same_version(frozen['context'], context)):
        _fail('继承依据不是同一角色、同一版本的真实冻结交接。')
    decision = engine.store.get('decisions', key, {})
    if sealed and (hand.get('status') != 'completed' or decision.get('id') != key
            or decision.get('run_id') != run_id or decision.get('role') != role
            or decision.get('purpose') != hand['purpose']
            or hand.get('output_hash') != digest(decision.get('result'))):
        _fail('被复用的原评审结论或封存回执发生变化。')
    if sealed:
        verify_decision(engine, run_id, role, key, frozen['context'], decision)
    return hand, frozen, decision


def _meaning(row):
    binding = row.get('binding') or row
    return {'requirement_id': binding.get('requirement_id'), 'disposition': binding.get('disposition'),
            'fixes': [{k: f.get(k) for k in ('location', 'change', 'verification')}
                      for f in row.get('fixes', [])]}


def _delta(context, role, value, previous_context=None):
    from . import code_repair_contract as repair
    scope = context.get('deferred_code_repair', {}).get('packet')
    if not isinstance(value.get('clarification_responses'), list) or not isinstance(value.get('repair_findings'), list):
        return None
    known = {i['issue_id'] for i in context.get(repair.FIELD, {}).get('issues', []) if i['role'] == role}
    replies = value.get('clarification_responses', [])
    previous_ids = {r['issue_id'] for r in replies}
    if not scope or not previous_ids <= known:
        return None  # Reading reuse is still available outside this incremental contract.
    old = {i['issue_id']: i for i in repair.issues({'review_votes': [{'role': role, **value}],
                                                   'delivery_hash': context['delivery_hash']})}
    targets = [i for i in scope['deferred_items'] if i['role'] == role]
    changed = {i['issue_id'] for i in targets if i['issue_id'] not in old
               or _meaning(i) != _meaning(old[i['issue_id']])}
    changed |= known - previous_ids
    # Source text can be reused when a receipt/control changes, but the affected
    # unresolved judgments cannot be reused as if no new evidence existed.
    evidence_fields = ('host_verification', 'local_acceptance', 'execution_constraints',
                       'definition_execution_acceptance', 'definition_code_followup')
    if previous_context is not None and any(context.get(k) != previous_context.get(k) for k in evidence_fields):
        if not old and not context.get('implementation_evidence_work_item'):
            return None
        changed |= set(old)
    host = context.get('host_constraint_reconciliation')
    if host:
        # Host policy facts need no model vote. Only the remaining, explicitly
        # listed source-reading gaps receive one scoped supplement.
        changed = set(host['supplement_issue_ids']) & known
    work_item = context.get('implementation_evidence_work_item')
    if work_item:
        if work_item.get('review_target_version')==1:
            changed=set(work_item['active_review_issue_ids']) & known
            scope_recovery=work_item.get('review_scope_update') or work_item.get('host_read_scope_recovery')
            if scope_recovery and role not in scope_recovery['roles']:
                changed=set()
            previous_work=(previous_context or {}).get('implementation_evidence_work_item')
            if (previous_work and previous_work.get('id')==work_item['id']
                    and previous_work.get('sha256')==work_item['sha256']):
                previous_targets=previous_work.get('active_review_issue_ids',
                    previous_work['evidence']['active_issue_ids'])
                if changed==set(previous_targets)&known:
                    recovery=scope_recovery
                    previous_recovery=previous_work.get('review_scope_update') or previous_work.get('host_read_scope_recovery')
                    if (not recovery or recovery==previous_recovery
                            or role not in recovery['roles']):
                        changed=set()  # Same evidence already judged by this role.
        else:
            changed = set(work_item['evidence']['active_issue_ids']) & known
    return sorted(changed)


@operation
def make_plan(engine, run_id, role, key, context):
    from .engine import digest
    if context.get('implementation_alignment_protocol') != 1:
        return None
    with engine.store._read_db() as db:
        rows = db.execute("SELECT key FROM records WHERE collection='handoffs' "
            "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.to_role')=? "
            "AND json_extract(data,'$.purpose') LIKE 'code_review:%' AND key<>? "
            "AND COALESCE(json_extract(data,'$.created_at'),updated_at) < "
            "COALESCE((SELECT json_extract(data,'$.created_at') FROM records WHERE collection='handoffs' AND key=?),'9999') "
            "ORDER BY COALESCE(json_extract(data,'$.created_at'),updated_at) DESC,key", (run_id, role, key, key)).fetchall()
    owners, parent = [], None
    for row in rows:
        old_id = row[0]
        frozen = engine.store.get('handoff_inputs', old_id, {})
        if not _same_version(frozen.get('context', {}), context):
            continue
        hand, frozen, decision = authenticated(engine, run_id, role, old_id, context)
        owners.append({'handoff_id': old_id, 'input_hash': hand['input_hash']})
        if parent is None and decision and hand.get('status') == 'completed':
            authenticated(engine, run_id, role, old_id, context, sealed=True)
            delta = _delta(context, role, decision['result'], frozen['context'])
            if delta is not None:
                parent = {'handoff_id': old_id, 'input_hash': hand['input_hash'],
                          'output_hash': hand['output_hash'], 'active_issue_ids': delta}
    if not owners:
        return None
    return {'version': VERSION, 'role': role, 'handoff_id': key,
            'context_sha256': digest(context), 'reading_owners': owners, 'parent': parent}


def raw_plan(engine, key):
    def lookup():
        # Do not decode paid prompts and full reply history to read this small
        # host descriptor. The cache expires with the current validation.
        with engine.store._read_db() as db:
            row = db.execute("SELECT json_extract(data,'$.code_review_inheritance') FROM records "
                             "WHERE collection='handoff_progress' AND key=?", (key,)).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None
    return source_read(engine.store, ('review_inheritance_plan', key), lookup)


def plan(engine, run_id, role, key, context):
    from .engine import digest
    value = raw_plan(engine, key)
    if value is None:
        return None
    if (value.get('version') != VERSION or value.get('role') != role or value.get('handoff_id') != key
            or value.get('context_sha256') != digest(context)):
        _fail('增量评审的继承范围与原交接不一致。')
    return value


@once
def owners(engine, run_id, role, key, context):
    value = plan(engine, run_id, role, key, context)
    result = []
    for row in (value or {}).get('reading_owners', []):
        if row['handoff_id'] == key:
            _fail('评审读取继承不能引用自身。')
        hand, _, _ = authenticated(engine, run_id, role, row['handoff_id'], context)
        if hand['input_hash'] != row['input_hash']:
            _fail('继承读取的原交接哈希发生变化。')
        result.append(row['handoff_id'])
    return result


def consumed_receipts(engine, run_id, role, owner):
    """All acknowledged working commits, including pages evicted from the latest view."""
    def collect():
        from .working_context import validate_commit
        with engine.store._read_db() as db:
            row=db.execute("SELECT json_extract(data,'$.working_context_commit') FROM records "
                "WHERE collection='handoff_progress' AND key=?",(owner,)).fetchone()
        current = row[0] if row else None
        seen, result = set(), set()
        while current:
            if current in seen:
                _fail('继承读取的提交链存在循环。')
            seen.add(current)
            saved = validate_commit(engine.store, run_id, role, {'commit_id': current, 'handoff_id': owner})
            for identity in saved['payload']['receipt_ids']:
                row = engine.store.get('context_receipts', identity)
                result.add(row['sha256'])
                page = row['receipt'].get('result', {})
                if isinstance(page, dict):
                    for name in ('code_read_reference', 'consumed_read_reference'):
                        ref = page.get(name, {})
                        if ref.get('receipt_sha256'):
                            result.add(ref['receipt_sha256'])
            current = saved['payload'].get('previous_commit')
        return result
    return source_read(engine.store, ('inherited_consumed_receipts', run_id, role, owner), collect)


def parent(engine, run_id, role, key, context, value=None):
    from .engine import digest
    value = value or plan(engine, run_id, role, key, context)
    ref = (value or {}).get('parent')
    if not ref:
        return None
    if ref['handoff_id'] == key:
        _fail('评审结论继承不能引用自身。')
    hand, frozen, decision = authenticated(engine, run_id, role, ref['handoff_id'], context, sealed=True)
    if (hand['input_hash'] != ref['input_hash'] or hand['output_hash'] != ref['output_hash']
            or _delta(context, role, decision['result'], frozen['context']) != ref['active_issue_ids']):
        _fail('增量问题或原角色已完成结论发生变化。')
    return copy.deepcopy(decision['result'])


async def prepare(h, run_id, role, purpose, context, instruction, schema):
    if not hasattr(h, '_handoff'):
        return
    key = h._handoff(run_id, role, purpose, instruction, context, schema)['id']
    if h.engine.store.get('decisions', key):
        return
    progress = h.engine.store.get('handoff_progress', key, {})
    value = progress.get(FIELD)
    if value is None:
        value = await h.engine._read_consensus_check(run_id, make_plan, h.engine, run_id, role, key, context)
        if not value:
            return
        progress = h.engine.store.get('handoff_progress', key, {})
        h.engine.check_active(run_id)
        h.engine.store.put('handoff_progress', key, {'turn': 0, 'seen': {}, **progress, 'id': key, 'run_id': run_id,
                                                   'tool_results': progress.get('tool_results', []), FIELD: value})
    prior = await h.engine._read_consensus_check(run_id, parent, h.engine, run_id, role, key, context)
    if prior is None or value['parent']['active_issue_ids']:
        return
    # Reuse the actual sealed opinion, including rejection and all unresolved
    # issues. Old paid continuations remain untouched in handoff_progress.
    h.engine.check_active(run_id)
    from .implementation_alignment_evidence import validate_candidate
    await h.engine._read_consensus_check(run_id, validate_candidate,
        h.engine, run_id, role, key, prior, context, delivery=True)
    h.engine.check_active(run_id)
    h._commit_decision(run_id, role, purpose, key, prior, inheritance_audit={
        'kind': 'same_role_unchanged_conclusion', 'parent': value['parent'], 'plan_sha256': _sha(value)})
    h.engine.store.event(run_id, 'handoff.review_reused', '同角色、同版本的问题未变化，复用已完成结论及原读取；未重新派发模型。',
        {'handoff_id': key, 'role': role, 'parent_handoff_id': value['parent']['handoff_id'],
         'approve': prior['approve'], 'unresolved_issues': len(prior['issues']), 'model_dispatched': False})


def _sha(value):
    from .engine import digest
    return digest(value)


@once
def verify_decision(engine, run_id, role, key, context, decision):
    inherited = decision.get(FIELD)
    if not inherited:
        return
    value = plan(engine, run_id, role, key, context)
    if not value or inherited.get('plan_sha256') != _sha(value):
        _fail('被复用结论缺少原增量计划。')
    prior = parent(engine, run_id, role, key, context, value)
    if inherited.get('kind') == 'same_role_unchanged_conclusion':
        expected = prior
        if value['parent']['active_issue_ids']:
            _fail('变更问题不能冒充已完成结论。')
    elif inherited.get('kind') == 'explicit_incremental_response':
        expected = merge(prior, inherited['delta'], value['parent']['active_issue_ids'])
    elif inherited.get('kind') == 'scoped_incremental_response':
        raw=inherited['raw_candidate']
        progress=engine.store.get('handoff_progress',key,{})
        slot='finalization' if progress.get('mode')=='final_only' else 'normal_request'
        if progress.get(slot,{}).get('response',{}).get('json')!=raw:
            _fail('增量补全没有绑定原角色已保存答复。')
        delta=_scoped_delta(prior,raw,value['parent']['active_issue_ids'])
        if delta is None or delta!=inherited['delta']:
            _fail('增量补全超出原派发问题或改变未变更结论。')
        expected=merge(prior,delta,value['parent']['active_issue_ids'])
    else:
        _fail('未知的结论继承来源。')
    if expected != decision.get('result'):
        _fail('继承后的评审结论与真实原结论或本轮答复不一致。')


def merge(prior, delta, active):
    """Expand the author's explicit inheritance acknowledgement, retaining dissent."""
    active = set(active)
    result = copy.deepcopy(delta)
    result.pop(ACK, None)
    retained = [r for r in prior['repair_findings'] if r.get('origin_id') not in active]
    if any(r.get('origin_id') is not None and r['origin_id'] not in active for r in result['repair_findings']):
        _fail('增量答复不能重新裁决未变更的原问题。')
    old_indices = [r['issue_index'] for r in retained]
    old_map = {index: i for i, index in enumerate(old_indices)}
    shift, evidence_shift = len(old_indices), len(prior['code_evidence'])
    result['issues'] = [prior['issues'][i] for i in old_indices] + result['issues']
    result['fixes'] = [{**copy.deepcopy(f), 'issue_index': old_map[f['issue_index']]}
                       for f in prior['fixes'] if f['issue_index'] in old_map] + [
                       {**f, 'issue_index': f['issue_index'] + shift} for f in result['fixes']]
    result['repair_findings'] = [{**copy.deepcopy(f), 'issue_index': old_map[f['issue_index']]}
                                for f in retained] + [{**f, 'issue_index': f['issue_index'] + shift,
        'evidence_indices': [i + evidence_shift for i in f['evidence_indices']]} for f in result['repair_findings']]
    result['clarification_responses'] = [copy.deepcopy(r) for r in prior['clarification_responses']
        if r['issue_id'] not in active] + [{**r, 'evidence_indices': [i + evidence_shift for i in r['evidence_indices']]}
                                         for r in result['clarification_responses']]
    result['code_evidence'] = copy.deepcopy(prior['code_evidence']) + result['code_evidence']
    affected = {r['requirement_id'] for r in delta['repair_findings']}
    affected |= {r['requirement_id'] for r in prior['repair_findings'] if r.get('origin_id') in active}
    keep = {r['requirement_id'] for r in retained}
    grouped = {r['requirement_id']: copy.deepcopy(r) for r in prior['alignment_findings']
               if r['requirement_id'] not in affected or r['requirement_id'] in keep}
    strength = {'aligned': 0, 'pending': 1, 'mismatch': 2}
    for row in delta['alignment_findings']:
        key = row['requirement_id']
        if key in grouped:
            old = grouped[key]
            grouped[key] = {**copy.deepcopy(row),
                'source_refs': list({json.dumps(r, sort_keys=True): r for r in old['source_refs'] + row['source_refs']}.values()),
                'status': max((old['status'], row['status']), key=strength.get),
                'implementation_evidence': old['implementation_evidence'] + '\n\n' + row['implementation_evidence']}
        else:
            grouped[key] = copy.deepcopy(row)
    result['alignment_findings'] = list(grouped.values())
    if result['approve'] and result['issues']:
        _fail('继承的未决意见仍存在，不能把局部处理当作完整批准。')
    return result


def _scoped_delta(prior,candidate,active):
    """An explicitly scoped reply can omit unchanged rows and restate withdrawals."""
    if candidate.get(ACK) is True or not active:return None
    active=set(active)
    required={r['requirement_id'] for r in prior.get('alignment_findings',[])}
    actual={r['requirement_id'] for r in candidate.get('alignment_findings',[])}
    affected={r['requirement_id'] for r in prior.get('repair_findings',[]) if r.get('origin_id') in active}
    affected|={r['requirement_id'] for r in candidate.get('repair_findings',[])}
    if not actual or not actual<required or not actual<=affected:return None
    if any(r.get('origin_id') not in active for r in candidate.get('repair_findings',[])):return None
    replies={r['issue_id']:r for r in prior.get('clarification_responses',[])}
    delta=copy.deepcopy(candidate)
    for row in delta.get('clarification_responses',[]):
        if row['issue_id'] not in active and (row['issue_id'] not in replies
                or row.get('resolution')!=replies[row['issue_id']].get('resolution')):
            return None
    delta['clarification_responses']=[r for r in delta.get('clarification_responses',[]) if r['issue_id'] in active]
    return delta


@operation
def complete_scoped_partial(engine,run_id,role,key,result):
    frozen=engine.store.get('handoff_inputs',key,{})
    context=frozen.get('context',{})
    if not context.get('implementation_evidence_work_item') or result.get(ACK) is True:return result,None
    value=plan(engine,run_id,role,key,context)
    if not value or not value.get('parent'):return result,None
    progress=engine.store.get('handoff_progress',key,{})
    slot='finalization' if progress.get('mode')=='final_only' else 'normal_request'
    request=progress.get(slot,{})
    if (request.get('response',{}).get('json')!=result
            or request.get('prepared',{}).get('context',{}).get(FIELD,{}).get('plan_sha256')!=_sha(value)):
        return result,None
    prior=parent(engine,run_id,role,key,context,value)
    delta=_scoped_delta(prior,result,value['parent']['active_issue_ids'])
    if delta is None:return result,None
    merged=merge(prior,delta,value['parent']['active_issue_ids'])
    return merged,{'kind':'scoped_incremental_response','plan_sha256':_sha(value),
        'parent':copy.deepcopy(value['parent']),'delta':delta,'raw_candidate':copy.deepcopy(result),
        'policy':'Only unchanged rows from the sealed same-role/version parent are inherited; original paid response retained.'}


def project(engine, run_id, role, key, progress, prepared):
    from .full_review_contract import project as material_contract
    prepared = material_contract(engine, run_id, role, key, prepared)
    context = engine.store.get('handoff_inputs', key, {}).get('context', {})
    value = plan(engine, run_id, role, key, context)
    if not value:
        return prepared
    prior = parent(engine, run_id, role, key, context, value)
    result = copy.deepcopy(prepared)
    view = {'version': VERSION, 'plan_sha256': _sha(value), 'reading_handoffs': owners(engine, run_id, role, key, context),
            'new_reading_claimed': False, 'scientific_approval_granted': False}
    if prior is not None:
        active = value['parent']['active_issue_ids']
        view.update(parent_handoff_id=value['parent']['handoff_id'], active_issue_ids=active,
                    inherited_conclusions=prior)
        from .code_review_catalog import CATALOG
        partial = CATALOG in result['context']
        for branch in result['schema'].get('anyOf', [result['schema']]) if partial else []:
            fields = branch.get('properties', {})
            if 'clarification_responses' not in fields or 'repair_findings' not in fields:
                continue
            fields[ACK] = {'type': 'boolean', 'const': True}
            # Existing full replies are also accepted. Partial inheritance is
            # explicit and applies only to new, unsent request projections.
            replies = fields['clarification_responses']
            replies.pop('allOf', None)
            replies.update(minItems=0)
        if partial:
            result['task'] += ('\n增量评审：code_review_inheritance.inherited_conclusions 是你在同一版本的已完成结论。'
            '仅补查 active_issue_ids 中新增或变更的问题，未变更结论、撤回和未决项原样继承，无需重复阅读。'
            '返回 inherit_unchanged=true 时，只返回本轮变更项的 issues/fixes/repair_findings/clarification_responses，'
            'evidence_indices 按本轮 code_evidence 编号；宿主保留原答复并按原索引展开继承结论。'
                'approve 仍表示完整结论；继承的未决项存在时必须为 false。不得虚构关闭、批准或执行回执。')
        else:
            result['task'] += ('\n仅核对 code_review_inheritance.active_issue_ids 中的变更，'
                '复用 inherited_conclusions 中未变更的原结论；沿用原答复格式，不重复阅读。')
        if context.get('host_constraint_reconciliation'):
            result['task'] += ('\n宿主约束回执由 Harness 根据真实任务策略生成。'
                '不要再审核宿主账本、要求作者补写预算定义或改变执行许可。'
                '本轮只补查 active_issue_ids 中尚缺的实际代码/来源证据；'
                '已有读取、撤回项和缺执行回执结论复用。确认真实代码偏差时给出具体文件、函数或范围、'
                '原问题、最小修改和原验收方式；不能将缺阅读、缺运行或宿主依赖写成改码。')
        if context.get('implementation_evidence_work_item'):
            result['task'] += ('\n当前仅复核 implementation_evidence_work_item 本组新增的实际回执。'
                '不同组的未决问题和已闭合项原样继承，不要求本组完成其他组工作。'
                '只对实际执行失败或原来源矛盾提出具体文件、位置、最小修改和原验收方法；'
                '原图观察与CPU测试均不构成训练许可或科学批准。')
            from .host_diagnostic_boundary import FIELD as BOUNDARY, GUIDANCE
            if context['implementation_evidence_work_item'].get('evidence',{}).get(BOUNDARY):
                result['task'] += GUIDANCE
    result['context'][FIELD] = view
    result['task'] += '\n同角色同版本已验证读取可以复用；先使用继承依据和 documents.recall，只对实际证据缺口定向取回原文。'
    return result


def expand(h, run_id, role, key, prepared, result):
    if result.get(ACK) is not True:
        return result, None
    context = h.engine.store.get('handoff_inputs', key)['context']
    value = plan(h.engine, run_id, role, key, context)
    if prepared.get('context', {}).get(FIELD, {}).get('plan_sha256') != _sha(value):
        _fail('增量答复没有绑定实际派发的继承计划。')
    prior = parent(h.engine, run_id, role, key, context, value)
    if prior is None:
        _fail('增量答复缺少本角色原已完成结论。')
    merged = merge(prior, result, value['parent']['active_issue_ids'])
    return merged, {'kind': 'explicit_incremental_response', 'plan_sha256': _sha(value),
                    'parent': value['parent'], 'delta': copy.deepcopy(result)}
