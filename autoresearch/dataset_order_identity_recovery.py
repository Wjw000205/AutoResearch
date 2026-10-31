"""One same-author field correction for legacy work-order naming loops.

Original proposals and rejected read requests remain immutable. The new answer
is independently reviewed; this does not authorize implementation or training.
"""
from __future__ import annotations

import copy

from .prose_schema import validate

from .experiment_workflow import sha

LEGACY_ISSUE = 'Work order must address the current single dataset'
INSTRUCTION = '''补正上一份已保存的任务单，只修改 dataset 字段为当前绑定数据集的精确名称。
schema 的其他字段 const 是你上一份完整原答复，必须逐字保留；本次不要重新检索、读取源码或修改方案。
返回完整任务单 JSON，不调用工具。补正后仍交研究总监、工程师、文献研究员分别评审；本步不批准配置或训练。'''


def author_request(engine, state, purpose, instruction, context, schema):
    from .dataset_planning import COLLECTION
    from .engine import Harness, digest

    ordinary = purpose, instruction, context, schema
    marker = state.get('identity_correction')
    if (state.get('status') != 'authoring' or 'result' in state
            or state.get('issues') != [LEGACY_ISSUE]
            or marker and marker.get('round') != state['round']):
        return ordinary
    store, rid = engine.store, state['run_id']
    ref = state.get('previous', {})
    record = store.get('experiment_evidence', ref.get('id'), {}) if ref.get('id') else {}
    summary = record.get('summary', {})
    previous = summary.get('plan', {})
    source = previous.get('result')
    key = previous.get('author_handoff_id')
    inputs = store.get('handoff_inputs', key, {}) if key else {}
    assignment = store.get('handoffs', key, {}) if key else {}
    decision = store.get('decisions', key, {}) if key else {}
    if (record.get('id') != ref.get('id') or record.get('run_id') != rid
            or not ref.get('sha256') or record.get('sha256') != ref['sha256']
            or sha(summary) != ref['sha256'] or summary.get('binding', {}).get('plan_id') != state['id']
            or summary.get('issues') != [LEGACY_ISSUE]
            or any(previous.get(k) != state.get(k) for k in ('id', 'run_id', 'cycle', 'scope'))
            or state['cycle'] != engine.cycle(rid)
            or previous.get('round') != state['round'] - 1 or previous.get('votes')
            or not isinstance(source, dict) or sha(source) != previous.get('result_sha256')
            or any(row.get('id') != key for row in (inputs, assignment, decision))
            or decision.get('run_id') != rid or decision.get('result') != source
            or assignment.get('to_role') != 'engineer' or assignment.get('status') != 'completed'
            or assignment.get('run_id') != rid or inputs.get('run_id') != rid
            or not assignment.get('purpose', '').startswith('dataset_work_order:')
            or key != Harness.decision_key(rid, 'engineer', assignment['purpose'],
                inputs.get('instruction'), inputs.get('context'), inputs.get('schema'))
            or assignment.get('input_hash') != digest({k: inputs.get(k) for k in ('instruction', 'context', 'schema')})):
        raise ValueError('Legacy dataset identity correction requires the exact saved engineer proposal')
    dataset = schema['properties']['dataset']['const']
    # Only this one field may fail the new contract. No repair of other content.
    corrected = {**source, 'dataset': dataset}
    validate(corrected, schema)
    proof = {'round': state['round'], 'previous': copy.deepcopy(ref),
        'source_handoff_id': key, 'source_sha256': sha(source), 'dataset': dataset}
    if marker and marker != proof:
        raise ValueError('Dataset identity correction binding changed')
    if not marker:
        engine.check_active(rid)
        state['identity_correction'] = proof
        store.put(COLLECTION, state['id'], state)
        store.event(rid, 'experiment.work_order_identity_correction',
            '将上一份完整任务单交原工程师补正数据集名称；其余字段锁定，随后重新三方评审。',
            {'plan_id': state['id'], **proof, 'execution_authorized': False})
    narrow = copy.deepcopy(schema)
    narrow['properties'] = {k: {'const': v} for k, v in corrected.items()}
    return (purpose + ':identity:' + sha(proof), INSTRUCTION,
        {'training_context_protocol': 1, 'dataset_identity_correction': proof,
         'research_cycle': state['cycle']}, narrow)
