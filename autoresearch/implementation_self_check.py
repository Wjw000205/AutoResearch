"""Durable, evidence-bound author checkpoints; never independent approval."""
from __future__ import annotations

import copy
from collections import Counter

from .architecture_validation import operation, once
from .implementation_review_routing import digest
from .storage import now

TOOL = 'implementation.record_self_check'
COLLECTION = 'implementation_author_self_checks'
KEY = 'implementation_author_self_check'
HELP = ('Record a batch of author self-checks against the current saved document: '
    'document_id, expected_sha256, base_spec_hash, checks:[{issue_id,index,status,explanation,evidence}]. '
    'status is complete/incomplete/blocked. Complete requires actual delivered current-document reading. '
    'Each valid condition is persisted separately; rejected items do not discard valid ones. '
    'Not a document save, scientific approval, issue closure or independent review.')
GUIDANCE = ('\n逐条自检必须落地：每核对一批条件，立即用 implementation.record_self_check 保存结论，'
    '再转向下一批，不能等整篇看完才一次报告。先完成必要修改，避免仅为同步措辞不断换版。'
    'implementation_author_self_check.valid 保留当前依赖仍有效的作者结论；complete 不必重读，'
    'blocked 无新依据时不重复搜索或登记，先处理 remaining 中其他条件。'
    '依赖按冻结验收合同及引用章节保守绑定；变化时 invalidated 要重新核对。'
    '继承结论保留原始证据版本，不伪造当前版本阅读。最终 acceptance_completion 逐条复制仍有效结论，'
    '并给齐累计 patch_reports；没有 incomplete/未登记项时立即最终报告，真实 blocked 必须保留，'
    '不能通过改成 complete 绕过来源缺口。自检全部 complete 后才进入独立审核。')
GUIDANCE += ('\n每累计8次成功读取而没有保存新的自检记录，下一请求先登记本批结论（可如实为 incomplete/blocked），'
    '或保存已完成修改；登记之前暂不继续扩展阅读。失败或相同调用重放不重置此计数。')
from .implementation_condition_roles import GUIDANCE as ROLE_GUIDANCE
GUIDANCE += ROLE_GUIDANCE


def schema():
    from . import implementation_acceptance as p
    sha = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    return p.obj({'document_id': p.TEXT, 'expected_sha256': sha, 'base_spec_hash': sha,
        'checks': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': p.obj({
            'issue_id': p.TEXT, 'index': {'type': 'integer', 'minimum': 0},
            'status': {'enum': ['complete', 'incomplete', 'blocked']},
            'explanation': p.TEXT, 'evidence': p.array(p.REF)})}})


def enabled(context):
    from .implementation_acceptance_author import enabled as author
    return author(context) and bool(context.get('acceptance_contract'))


def _seal(record):
    if record and record.get('sha256') != digest({k:v for k,v in record.items() if k != 'sha256'}):
        raise ValueError('Author self-check checkpoint checksum mismatch')
    return record


def _records(engine, hid, context):
    """Ancestors are frozen audit payloads, not a lookup of a mutable sibling."""
    from .implementation_acceptance_author import CONTINUATION
    own = _seal(engine.store.get(COLLECTION, hid, {}))
    records = [own] if own else []
    ref = context.get(CONTINUATION)
    seen = set()
    while ref:
        if ref['audit_id'] in seen: raise ValueError('Self-check ancestry cycle')
        seen.add(ref['audit_id'])
        audit = engine.store.get('stage_contexts', ref['audit_id'], {})
        payload = audit.get('payload', {})
        if audit.get('sha256') != ref['sha256'] or digest(payload) != ref['sha256']:
            raise ValueError('Self-check ancestry changed')
        source = payload['source']
        record = _seal(source.get(COLLECTION, {}))
        if record: records.append(record)
        ref = source['handoff_inputs']['context'].get(CONTINUATION)
    return records


def _dependency(context, row, issue):
    from .implementation_acceptance import fingerprint
    sources = sorted((d['document_id'],d['sha256']) for d in context.get('indexed_documents',[])
        if d.get('kind') not in {'implementation_specification','document_working_notes'})
    return digest({'issue': issue, 'source_scope':sources, 'contract_dependency': fingerprint(
        context['acceptance_contract'], row['spec'], issue['criterion_id'])})


def _source_versions(context):
    return {(d['document_id'], d['sha256'], d.get('kind'))
        for d in context.get('indexed_documents', [])
        if d.get('kind') not in {'implementation_specification', 'document_working_notes'}}


@once
def _adopted_sources(engine, run_id, workflow_id, adoption):
    """Authenticate an explicit source extension once per read-only check."""
    from .implementation_supplement_repair import validate_adoption
    binding = {k: adoption.get(k) for k in ('record_id', 'payload_sha256', 'rechecks_sha256')}
    state = engine.store.get('codex_workflows', workflow_id, {})
    reviewed = validate_adoption(engine, run_id, state, binding)
    context = reviewed['payload']['context']
    if (adoption.get('document') != context['source_supplement']['document']
            or adoption.get('scientific_approval') is not False
            or adoption.get('execution_authorized') is not False):
        raise ValueError('Supplement adoption differs from its authenticated candidate')
    return _source_versions(context)


def _unchanged_check_with_added_sources(engine, run_id, context, row, issue, entry):
    """A reviewed additional source alone must not erase a completed check.

    Retain the original conclusion/evidence only when its original dependency
    fingerprint still matches the current SPEC and every old source is intact.
    Incomplete/blocked conclusions must be reconsidered when new evidence arrives.
    Changed SPEC fields or cited sections still invalidate the original check.
    """
    adoption = context.get('source_supplement_adoption')
    if not isinstance(adoption, dict) or entry['check']['status'] != 'complete':
        return False
    origin_id = entry.get('origin_handoff_id', '')
    inputs = engine.store.get('handoff_inputs', origin_id, {})
    assignment = engine.store.get('handoffs', origin_id, {})
    origin = inputs.get('context', {})
    workflow_id = context.get('implementation_document_authoring', {}).get('workflow_id')
    if (inputs.get('run_id') != run_id or assignment.get('run_id') != run_id
            or assignment.get('to_role') != 'engineer'
            or assignment.get('input_hash') != digest({k: inputs.get(k) for k in ('instruction','context','schema')})
            or origin.get('implementation_document_authoring', {}).get('workflow_id') != workflow_id):
        return False
    before, after = _source_versions(origin), _source_versions(context)
    if not before < after or entry['dependency_sha256'] != _dependency(origin, row, issue):
        return False
    return after - before <= _adopted_sources(engine, run_id, workflow_id, adoption)


def _sections(engine, run_id, doc):
    from .document_index import load
    manifest, text = load(engine, run_id, doc['document_id'], doc['sha256'])
    return {s['id']: digest(text[s['start']:s['end']]) for s in manifest['sections']}


def _effective(engine, run_id, hid, context, row):
    issues = {i['issue_id']: i for i in context['acceptance_issues']}
    latest = {}
    for record in _records(engine, hid, context):
        if record['run_id'] != run_id or record['workflow_id'] != context['implementation_document_authoring']['workflow_id']:
            raise ValueError('Self-check owner mismatch')
        for batch in reversed(record['batches']):
            for entry in batch['entries']:
                latest.setdefault((entry['check']['issue_id'], entry['check']['index']), entry)
    sections = _sections(engine, run_id, row['document']) if latest else {}
    valid, invalid = [], []
    for pair, entry in latest.items():
        issue = issues.get(pair[0])
        if (issue and pair[1] < len(issue['close_when'])
                and (entry['dependency_sha256'] == _dependency(context, row, issue)
                    or _unchanged_check_with_added_sources(engine, run_id, context, row, issue, entry))
                and all(sections.get(k) == v for k,v in entry['section_sha256'].items())):
            valid.append(entry)
        else:
            invalid.append({'issue_id':pair[0], 'index':pair[1], 'reason':'依赖内容或原意见变化，须重新核对。'})
    return valid, invalid


@operation
def project(engine, run_id, hid, context, row):
    if not enabled(context): return None
    valid, invalid = _effective(engine, run_id, hid, context, row)
    checks = [copy.deepcopy(e['check']) for e in valid]
    from .implementation_condition_roles import review_pending
    review = review_pending(context)
    reviewer_conditions = {(r['issue_id'], r['index']) for r in review}
    author_checks = [c for c in checks if (c['issue_id'], c['index']) not in reviewer_conditions]
    done = {(c['issue_id'], c['index']) for c in checks if c['status'] in {'complete', 'blocked'}}
    remaining = [{'issue_id':i['issue_id'], 'index':n, 'requirement':requirement}
        for i in context['acceptance_issues'] for n,requirement in enumerate(i['close_when'])
        if (i['issue_id'],n) not in done and (i['issue_id'],n) not in reviewer_conditions]
    ledger = engine.store.get('handoff_progress',hid,{}).get('tool_results',[])
    own = engine.store.get(COLLECTION,hid,{})
    boundary = max([0, *[b.get('ledger_count',0) for b in own.get('batches',[])],
        *[n+1 for n,r in enumerate(ledger) if r.get('call',{}).get('name')=='implementation.edit_document'
          and r.get('result',{}).get('saved') is True]])
    reads = sum(r.get('call',{}).get('name')=='documents.read' and 'text' in r.get('result',{})
                for r in ledger[boundary:])
    return {'version':1, 'document_id':row['document']['document_id'], 'spec_hash':row['spec_hash'],
        'valid':checks, 'invalidated':invalid, 'remaining':remaining,
        'reads_since_checkpoint':reads,'checkpoint_due':reads>=8,
        'counts':dict(Counter(c['status'] for c in author_checks)), 'review_pending':review,
        'ready_to_report':bool(checks) and not remaining,
        'scientific_approval':False, 'reading_credit':False}


@operation
def record(engine, run_id, role, hid, args):
    from .prose_schema import validate
    from .implementation_authoring import _assignment, _checked, COLLECTION as DRAFT
    from .implementation_acceptance_flow import validate_refs
    from .implementation_revision_batch import status
    from .implementation_review_clock import _get, _put
    validate(args, schema())
    assignment, context, state, binding = _assignment(engine, run_id, role, hid)
    if not enabled(context): raise ValueError('Self-check requires an acceptance author assignment')
    if assignment.get('status') == 'completed': raise ValueError('Completed author assignment is immutable')
    engine.check_active(run_id)
    old = _seal(engine.store.get(COLLECTION, hid, {}))
    batch_id = digest(args)
    prior = next((b for b in old.get('batches',[]) if b['id']==batch_id), None)
    if prior: return copy.deepcopy(prior['receipt'])
    draft = engine.store.get(DRAFT, hid)
    source_guards = None
    if draft:
        row = _checked(engine, run_id, draft)['row']
    else:
        from .implementation_acceptance_author import CONTINUATION, continuation_base, _source_records
        ref = context.get(CONTINUATION)
        if not ref: raise ValueError('Save the current draft before recording self-checks')
        row = continuation_base(engine, run_id, state, context)
        source_guards = (ref, engine.store.get('stage_contexts',ref['audit_id']),
                         _source_records(engine.store,ref['source_handoff_id']))
    doc = row['document']
    if (args['document_id'],args['expected_sha256'],args['base_spec_hash']) != (doc['document_id'],doc['sha256'],row['spec_hash']):
        return {'recorded':False, 'error':'自检版本已过期，请使用当前已保存文档。', 'scientific_approval':False}
    issues = {i['issue_id']:i for i in context['acceptance_issues']}
    reading_context = {**context, 'indexed_documents':[*context['indexed_documents'],doc]}
    saved = status(engine,hid,context,draft)
    missing = {(c['issue_id'],c['condition_index']) for c in saved.get('missing_conditions',[])}
    entries, rejected, seen = [], [], set()
    from .implementation_condition_roles import review_keys
    reviewer_conditions = review_keys(context)
    sections = _sections(engine,run_id,doc)
    for check in args['checks']:
        pair = (check['issue_id'],check['index'])
        try:
            if pair in seen: raise ValueError('同一批条件重复')
            seen.add(pair)
            issue = issues.get(pair[0])
            if not issue or pair[1] >= len(issue['close_when']): raise ValueError('未知原意见或关闭条件')
            if pair in reviewer_conditions and check['status']=='complete':
                raise ValueError('该条件属于后续独立复核，作者不能登记为完成；请保留待职责审查者复核。')
            if check['status']=='complete' and pair in missing: raise ValueError('该条件尚未完成修订登记')
            if check['status']=='complete' or check['evidence']:
                validate_refs(engine,run_id,hid,reading_context,check['evidence'],
                    current=doc if check['status']=='complete' else None,field='self_check.evidence')
            cited = {r['section_id'] for r in check['evidence'] if r['document_id']==doc['document_id']}
            entries.append({'check':copy.deepcopy(check), 'origin_handoff_id':hid,
                'document':copy.deepcopy(doc), 'dependency_sha256':_dependency(context,row,issue),
                'section_sha256':{k:sections[k] for k in cited}})
        except (ValueError, RuntimeError) as exc:
            rejected.append({'issue_id':pair[0], 'index':pair[1], 'error':str(exc)})
    receipt = {'recorded':bool(entries), 'accepted':[e['check'] for e in entries], 'rejected':rejected,
        'document_id':doc['document_id'], 'document_sha256':doc['sha256'],
        'scientific_approval':False, 'reading_credit':False,
        'instruction':'仅作者自检记录已保存；继续剩余条件，外部阻塞不重复登记。全部核对后正式报告。'}
    if not entries: return receipt
    batch = {'id':batch_id, 'entries':entries, 'receipt':copy.deepcopy(receipt), 'created_at':now(),
        'ledger_count':len(engine.store.get('handoff_progress',hid,{}).get('tool_results',[]))}
    value = {'id':hid,'run_id':run_id,'workflow_id':state['id'],
        'batches':[*old.get('batches',[]),batch]}
    value['sha256']=digest(value)
    run = engine.store.get('runs',run_id)
    inputs = engine.store.get('handoff_inputs',hid)
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        current_run = _get(db,'runs',run_id,{})
        if (current_run.get('status')!='running'
                or any(current_run.get(k)!=run.get(k) for k in ('config','state'))
                or _get(db,DRAFT,hid)!=draft or _get(db,'handoff_inputs',hid)!=inputs
                or _get(db,'codex_workflows',state['id'])!=state
                or _get(db,COLLECTION,hid,{})!=old):
            raise ValueError('自检保存期间任务、文档或检查点变化，未写入')
        if source_guards:
            from .implementation_acceptance_author import _canonical_source
            ref, audit, source = source_guards
            if (_get(db,'stage_contexts',ref['audit_id']) != audit
                    or any(_canonical_source(col,_get(db,col,ref['source_handoff_id'])) != value
                           for col,value in source.items())):
                raise ValueError('自检保存期间继承草稿来源变化，未写入')
        _put(engine.store,db,COLLECTION,hid,value,now())
    return receipt


def reuse_candidates(engine, run_id, hid, context, row):
    return _effective(engine,run_id,hid,context,row)[0] if enabled(context) else []


@operation
def reusable(engine, run_id, hid, context, row, iid, answer, *, candidates=None):
    """Explicit reuse of an author attestation, never synthetic current-SHA reading."""
    from .implementation_acceptance_flow import validate_refs
    if not enabled(context): return False
    valid = reuse_candidates(engine,run_id,hid,context,row) if candidates is None else candidates
    check = {'issue_id':iid, **answer}
    entry = next((e for e in valid if e['check']==check),None)
    if not entry or answer.get('status')!='complete': return False
    origin = engine.store.get('handoff_inputs',entry['origin_handoff_id'],{})['context']
    validate_refs(engine,run_id,entry['origin_handoff_id'],
        {**origin,'indexed_documents':[*origin['indexed_documents'],entry['document']]},
        answer['evidence'],current=entry['document'],field='persisted_self_check.evidence')
    return True


def ready_to_report(engine, run_id, role, hid, context):
    from .implementation_authoring import COLLECTION as DRAFT, _checked
    if role != 'engineer' or not enabled(context) or not engine.store.get(COLLECTION,hid):
        return False
    draft = engine.store.get(DRAFT,hid)
    if draft:
        row = _checked(engine,run_id,draft)['row']
    else:
        from .implementation_acceptance_author import CONTINUATION, continuation_base
        if CONTINUATION not in context: return False
        from .implementation_authoring import _assignment
        _, frozen, state, _ = _assignment(engine,run_id,role,hid)
        row = continuation_base(engine,run_id,state,frozen)
    return project(engine,run_id,hid,context,row)['ready_to_report']
