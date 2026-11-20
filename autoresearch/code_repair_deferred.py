"""Route an empty edit list to evidence work, never to vacuous code approval."""
from __future__ import annotations

import copy

from . import code_repair_contract as repair, code_repair_amendment as amendment
from .architecture_validation import operation, once

ERROR = '代码修改依据待核实：原作者核实后没有可独立执行的修改项，保留全部未决意见。'
FAILED_ERROR = "KeyError: 'delivery_hash'"
RECORD = 'deferred_repair_clarification'
EXECUTION_RECORD = 'deferred_repair_execution'


def findings(packet):
    rows = []
    for item in packet.get('deferred_items', []):
        row = {k:copy.deepcopy(item[k]) for k in
            ('issue_id','role','issue','requirement_id','fixes','disposition')}
        if item.get('amendment'):
            row['amendment'] = {k:copy.deepcopy(v) for k,v in item['amendment'].items() if k != 'read_evidence'}
        row['evidence_locations'] = [{'path':e['path'],'code_reference':copy.deepcopy(e['code_reference'])}
            for e in item['host_evidence']]
        rows.append(row)
    return rows


def scope_for(packet):
    return {'packet_sha256': repair.sha(packet), 'packet': {
        **{k:packet[k] for k in ('source_sha256','spec_hash','delivery_hash')},'deferred_items':findings(packet)},
        'instruction': '本轮全部修改暂缓，不能视作批准。只补查原问题；区分缺定义、缺执行和真实缺陷。'
        '保留原作者暂缓理由和已撤回结论，不重复提出已满足要求；不修改批准文档或执行科研实验。'}


def proposal_for(source, packet, attempt):
    previous = attempt.get('repair_clarification', {})
    if previous.get('source_sha256') == repair.sha(source):
        return copy.deepcopy(previous['proposal'])
    known = {i['issue_id']: i for i in packet.get('deferred_items', [])}
    rows = []
    for issue in source['issues']:
        row = issue.get('binding') or known.get(issue['issue_id'])
        if not row:
            repair.fail('暂缓修改缺少完整的原问题映射。')
        rows.append({'issue_id': issue['issue_id'], **{k:copy.deepcopy(row[k]) for k in
            ('requirement_id','disposition','evidence_indices')},
            'reason':'原修改已暂缓，请结合暂缓理由补查并分类。'})
    return {'source_sha256':repair.sha(source),'items':rows}


@once
def validate_scope(engine, run_id, source, scope):
    packet = engine.store.get('code_repair_packets', scope['packet_sha256'], {}).get('packet')
    if (not packet or packet.get('items') or not packet.get('deferred_items')
            or repair.sha(packet) != scope['packet_sha256'] or scope_for(packet) != scope
            or packet['spec_hash'] != source['spec_hash'] or packet['delivery_hash'] != source['delivery_hash']):
        repair.fail('暂缓修改的后续处理没有绑定同一冻结交付。')
    amendment.verify(engine, run_id, packet)
    if packet.get('clarification'):
        from .code_repair_clarification import verify_saved
        state = engine.store.get('codex_workflows', source['workflow_id'])
        if not state or state.get('run_id') != run_id:
            repair.fail('暂缓修改缺少同一任务的工作流。')
        attempts = [a for a in state.get('attempts',[]) if isinstance(a,dict)
            and a.get('index') == source['attempt_index']]
        if len(attempts) != 1 or attempts[0].get('delivery_hash') != source['delivery_hash']:
            repair.fail('暂缓修改没有唯一对应的当前交付；旧失败记录不能代替当前 attempt。')
        attempt = attempts[0]
        resolved = verify_saved(engine, run_id, state, attempt, packet['clarification'])
        if repair.sha(resolved) != packet['source_sha256']:
            repair.fail('暂缓修改的原补查结论已变化。')
    elif packet['source_sha256'] != repair.sha(source):
        repair.fail('暂缓修改的原评审已变化。')
    return packet


@operation
def _validate_current_source(engine,run_id,state,specification,attempt,original,scope):
    """Authenticate the scope and its current source in one read-only call."""
    validate_scope(engine,run_id,original,scope)
    current=repair.build_source(engine,run_id,state,specification,attempt)
    if repair.sha(current)!=repair.sha(original):
        repair.fail('暂缓修改期间原输入变化。')
    return current


def approval_record(attempt):
    key = attempt.get('repair_resolution_record_key')
    if key in {RECORD, EXECUTION_RECORD, 'deferred_evidence_clarification'}:
        return attempt.get(key, {})
    return attempt.get('verification_followup', attempt.get('repair_clarification', {}))


@operation
def recoverable_blocked_run(engine, run):
    """Read-only admission of the old empty-list stop under original intent."""
    eligible = ((run.get('status') == 'blocked' and run.get('error') == ERROR)
                or (run.get('status') == 'failed' and run.get('error') == FAILED_ERROR))
    if not eligible or run.get('stage') != 'implementation':
        return False
    from .engine import ResearchBlocked
    from .code_repair_tool_review import authenticate, semantic
    from .code_repair_clarification import verify_saved
    for state in engine.store.list('codex_workflows', run['id']):
        if state.get('status') != 'code_review' or not state.get('attempts'):
            continue
        try:
            attempt = state['attempts'][-1]
            spec = [r for r in state['specifications'] if r.get('approved')][-1]
            original = repair.build_source(engine, run['id'], state, spec, attempt)
            if run.get('status') == 'failed':
                # Preserve exact JSON types in revision metadata (e.g. 1 vs
                # true): reconstructing a normalized audit changes its SHA.
                keys = engine.store.record_keys('code_repair_packets',run['id'],
                    predicate="json_array_length(data,'$.packet.items')=0")
                for key in keys:
                    saved = engine.store.get('code_repair_packets',key,{})
                    candidate = saved.get('packet')
                    if (saved.get('run_id') != run['id'] or not candidate or repair.sha(candidate) != key
                            or candidate.get('spec_hash') != original['spec_hash']
                            or candidate.get('delivery_hash') != original['delivery_hash']):
                        continue
                    validate_scope(engine,run['id'],original,scope_for(candidate))
                    return True
                continue
            keys = engine.store.record_keys('handoffs', run['id'],
                predicate="json_extract(data,'$.purpose') LIKE 'code_repair_amend:%' "
                          "AND json_extract(data,'$.status')='completed'")
            for hid in reversed(keys):
                frozen = engine.store.get('handoff_inputs', hid, {})
                context = frozen.get('context', {})
                src = context.get('amendment_source', {})
                packet = engine.store.get('code_repair_packets', src.get('source_packet_sha256', ''), {}).get('packet')
                if (not packet or repair.sha(packet) != src.get('source_packet_sha256')
                        or packet['spec_hash'] != original['spec_hash']
                        or packet['delivery_hash'] != original['delivery_hash']):
                    continue
                amendment.verify(engine, run['id'], packet)
                resolved = verify_saved(engine, run['id'], state, attempt, packet['clarification']) if packet.get('clarification') else original
                if repair.sha(resolved) != packet['source_sha256']:
                    continue
                audit = src['audit']
                raw = engine.store.get('decisions', audit['handoff_id'], {}).get('result', {})
                audit_context = engine.store.get('handoff_inputs',audit['handoff_id'],{}).get('context',{})
                meta = {k:audit[k] for k in ('role','handoff_id')}
                if audit_context.get('code_repair_tool_review') == 1:
                    meta['tool_review'] = True
                vote = authenticate(engine, run['id'], packet, {**meta, 'result': semantic(raw)})
                dissent = {c['issue_id'] for c in vote['checks'] if not c['supported']}
                if dissent != {i['issue_id'] for i in packet['items']}:
                    continue
                restored = amendment.restore(engine, run['id'], packet,
                    {**meta,'result':vote})
                if restored['items'] or not restored.get('deferred_items'):
                    continue
                return True
        except (ResearchBlocked, KeyError, TypeError, ValueError, OSError, IndexError):
            continue
    return False
