"""Explicit operator repair: preserve paid author work and resume a new binding.

This is never a model tool or an automatic migration. Review votes, scientific
inputs, accounting and the existing acceptance budget remain unchanged.
"""
from __future__ import annotations

import copy
import json

from .engine import ResearchBlocked, digest
from .storage import now
from . import implementation_acceptance_author as author

COLLECTION = 'implementation_operator_continuations'


def enable_identity(engine, run_id, expected_config_sha256):
    """One explicit paused-task opt-in, with the old configuration archived."""
    from .implementation_review_clock import _get, _put
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        run = _get(db, 'runs', run_id, {})
        if (run.get('status') != 'paused' or run.get('stage') != 'implementation'
                or digest(run.get('config')) != expected_config_sha256):
            raise ResearchBlocked('身份协议迁移要求已暂停的实现任务及匹配的当前配置。')
        if engine.store.count('experiments', run_id):
            raise ResearchBlocked('已有实验记录，不能使用此执行前协议迁移。')
        version = run['config'].get('evaluation_contract_version', 1)
        if type(version) is not int or version not in (1, 2):
            raise ResearchBlocked('不支持的原身份协议版本。')
        if version == 2:
            return {'changed': False, 'evaluation_contract_version': 2}
        before = copy.deepcopy(run['config'])
        run['config']['evaluation_contract_version'] = 2
        receipt = {'run_id': run_id, 'before': before, 'after': copy.deepcopy(run['config']),
            'created_at': now(), 'authorization': 'explicit_operator_request',
            'scientific_approval': False, 'execution_authorized': False}
        receipt['sha256'] = digest(receipt)
        _put(engine.store, db, 'implementation_identity_migrations', run_id, receipt, now())
        _put(engine.store, db, 'runs', run_id, run, now())
        db.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
            (run_id, 'implementation.identity_protocol_enabled',
             '当前任务已启用阶段身份协议2；旧配置已留存，仍须新SPEC独立审核。',
             json.dumps({'receipt_sha256': receipt['sha256'], 'evaluation_contract_version': 2}), now()))
    return {'changed': True, 'evaluation_contract_version': 2, 'receipt_sha256': receipt['sha256']}


def prepare(engine, run_id, state, source_id, capability, capability_document):
    """Archive the repaired draft and bind a fresh author assignment."""
    from .implementation_authoring import COLLECTION as DRAFTS, _checked
    run = engine.store.get('runs', run_id)
    if (run['status'] != 'paused' or run['stage'] != 'implementation'
            or run['config'].get('evaluation_contract_version') != 2
            or state.get('status') != 'specification'):
        raise ResearchBlocked('操作员收尾需要暂停的未批准实现文档和已启用的身份协议。')
    source = author._source_records(engine.store, source_id)
    draft = _checked(engine, run_id, source[DRAFTS])
    if (draft['status'] != 'draft' or draft['row']['spec']['comparison_transport'].get('split_identity_protocol') not in (None, 2)
            or not capability.get('split_identity', {}).get('host_enabled')):
        raise ResearchBlocked('新稿和当前宿主必须同时明确启用阶段身份协议。')
    context = copy.deepcopy(source['handoff_inputs']['context'])
    for key in ('implementation_document_completion_protocol', 'implementation_document_completion',
                'implementation_phase_context_protocol', 'implementation_phase_context', 'implementation_document_repair_reads'):
        context.pop(key, None)
    payload = {'source': source, 'workflow': author._workflow_scope(state),
        'pending': [], 'operator_requested_repair': True}
    checksum = digest(payload)
    audit_id = source_id + ':operator-repair:' + checksum
    context[author.CONTINUATION] = {'audit_id': audit_id, 'sha256': checksum,
        'source_handoff_id': source_id, 'spec_hash': draft['row']['spec_hash'],
        'document': draft['row']['document'],
        'depth': context.get(author.CONTINUATION, {}).get('depth', 0) + 1}
    context['acceptance_author_saved_progress'] = copy.deepcopy(draft['revision_progress'])
    context['acceptance_author_pending'] = []
    context['implementation_capability_evidence'] = copy.deepcopy(capability)
    context['implementation_capability_document'] = copy.deepcopy(capability_document)
    # Older paid author inputs froze a schema before phase identity existed.
    # Extend only the explicitly requested transport opt-in in this NEW input.
    transport = context['implementation_document_authoring']['specification_schema']['properties']['comparison_transport']
    objects = transport.get('anyOf', [transport])
    for obj in objects:
        if obj.get('type') == 'object':
            obj['properties']['split_identity_protocol'] = {'type': 'integer', 'const': 2}
    docs = {d['document_id']: d for d in context['indexed_documents']}
    docs.update({d['document_id']: d for d in (draft['row']['document'], capability_document)})
    context['indexed_documents'] = list(docs.values())
    instruction = ('操作员已按用户要求统一替换残留旧结论，并实际启用本任务身份协议2。'
        '继续当前新稿，保留原问题、科学定义、预算、模型和推理强度。'
        '先核对新宿主能力文档及当前稿，不能继续引用旧宿主未启用的状态。'
        '只补读失效自检对应范围；按当前证据登记剩余条件。不要重写算法或恢复旧缺失声明。'
        'G02属于后续既定调用内资源核验，不要求现在运行训练或额外探针。'
        '完成后立即提交完整累计patch_reports和acceptance_completion进入原三方独立复核；'
        '作者不关闭问题或宣称批准。' + author.GUIDANCE)
    record = {'run_id': run_id, 'source_id': source_id, 'payload': payload, 'audit_id': audit_id,
        'sha256': checksum, 'context': context, 'instruction': instruction,
        'purpose': 'implementation_spec:operator-repair:' + checksum,
        'configuration_sha256': digest(run['config'])}
    record['record_sha256'] = digest(record)
    existing = engine.store.get(COLLECTION, run_id)
    if existing and existing != record:
        raise ResearchBlocked('已有不同的操作员修订续接，未覆盖。')
    engine.store.put(COLLECTION, run_id, record)
    engine.store.event(run_id, 'implementation.operator_repair_prepared',
        '旧结论与协议已保存新稿；原回执和预算保留，等待作者核验后进入独立审核。',
        {'document': draft['row']['document'], 'record_sha256': record['record_sha256']})
    return record


def stage_paused(engine, h, run_id, state, record):
    """Create the saved successor while paused, without dispatching a model."""
    from .implementation_authoring import submission_schema
    from .implementation_review_clock import _get, _put
    child = h._handoff(run_id, 'engineer', record['purpose'], record['instruction'],
        record['context'], submission_schema(record['context']))
    audit = {'id': record['audit_id'], 'run_id': run_id, 'payload': record['payload'],
        'sha256': record['sha256'], 'successor_id': child['id'],
        'successor_context_sha256': digest(record['context']), 'scientific_approval': False}
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        run = _get(db, 'runs', run_id, {})
        if (run.get('status') != 'paused' or digest(run.get('config')) != record['configuration_sha256']
                or author._workflow_scope(_get(db, 'codex_workflows', state['id'], {})) != record['payload']['workflow']
                or any(author._canonical_source(col, _get(db, col, record['source_id'])) != value
                       for col,value in record['payload']['source'].items())):
            raise ResearchBlocked('暂停修订期间来源或配置已改变。')
        existing = _get(db, 'stage_contexts', record['audit_id'])
        if existing and existing != audit:
            raise ResearchBlocked('已有不同的操作员续接绑定。')
        _put(engine.store, db, 'stage_contexts', record['audit_id'], audit, now())
    return child


def pending_plan(engine, run_id, state, hid):
    record = engine.store.get(COLLECTION, run_id)
    if not record:
        return None
    if record.get('record_sha256') != digest({k:v for k,v in record.items() if k != 'record_sha256'}):
        raise ResearchBlocked('操作员修订续接记录校验失败。')
    from .implementation_authoring import COLLECTION as DRAFTS
    source_revision = (record['payload']['source'].get(DRAFTS) or {}).get('revision')
    if (state.get('id') != record['payload']['workflow'].get('id')
            or source_revision is not None and any(r.get('index') == source_revision
                for r in state.get('specifications', []))):
        # The saved repair has reached the review ledger (or another workflow
        # is running). It must never redirect a later review/repair cycle.
        return None
    # A result from this successor (or its later normal continuation) proceeds
    # through ordinary self-check and independent-review validation.
    cursor = engine.store.get('handoff_inputs', hid, {}).get('context', {})
    seen = set()
    while cursor.get(author.CONTINUATION):
        ref = cursor[author.CONTINUATION]
        if ref.get('audit_id') == record['audit_id']:
            return None
        key = ref.get('source_handoff_id')
        if key in seen:
            raise ResearchBlocked('作者续接来源循环。')
        seen.add(key)
        cursor = engine.store.get('handoff_inputs', key, {}).get('context', {})
    run = engine.store.get('runs', run_id)
    if (record['payload']['workflow'] != author._workflow_scope(state)
            or record['payload']['source'] != author._source_records(engine.store, record['source_id'])
            or digest(run['config']) != record['configuration_sha256']):
        raise ResearchBlocked('操作员修订的来源、工作流或配置已改变，未重放旧作者请求。')
    return {**copy.deepcopy(record), 'run_guard': {k: run.get(k) for k in ('status', 'config', 'state')}}
