"""Resume the host phase after a real author save, independently of error prose."""
from __future__ import annotations

import copy

from .architecture_validation import operation
from .storage import now

CHECKPOINT = 'post_save_checkpoint'
LEGACY_REVIEW_ERROR = '代码修改依据待核实：实现文档定向补定义：审核必须逐项闭合原缺口或给出可修订的具体意见。'


def legacy_review_block(run):
    return (run.get('status') == 'blocked' and run.get('stage') == 'implementation'
            and run.get('error') == LEGACY_REVIEW_ERROR)


@operation
def recoverable_review(engine, run):
    """Admit an old review checkpoint only if every original vote now validates."""
    from . import implementation_definition_repair as definitions
    from .codex_workflow import _frozen_spec_assignment
    from .engine import ResearchBlocked
    from .intelligence import ProviderError

    if not legacy_review_block(run):
        return False
    rid = run['id']
    with engine.store.read_snapshot() as store:
        reader = copy.copy(engine)
        reader.store = store
        keys = store.record_keys('codex_workflows', rid, predicate=
            "json_extract(data,'$.status')='spec_review' AND "
            "json_extract(data,'$.implementation_definition_repair.status')='reviewing'")
        if len(keys) != 1:
            return False
        state = store.get('codex_workflows', keys[0])
        try:
            record = state[definitions.FIELD]
            row = state['specifications'][-1]
            index = row['index']
            purpose = f'implementation_definition_review:{record["id"]}:{index}'
            if (index != state['current_revision'] or index != record['revision_indices'][-1]
                    or row.get('definition_repair_id') != record['id']
                    or row.get('definition_review_purpose') != purpose or row.get('approved') is not None):
                return False
            for role in definitions.repair.ROLES:
                frozen = _frozen_spec_assignment(reader, rid, purpose, role)
                if not frozen:
                    return False
                hid = frozen['id']
                decision = store.get('decisions', hid)
                if not decision:
                    return False
                definitions.validate_vote(reader, rid, state, row, {
                    'role':role, 'handoff_id':hid, 'context':frozen['context'], 'result':decision['result']})
        except (ResearchBlocked, ProviderError, KeyError, IndexError, TypeError, ValueError, OSError):
            return False
        return True


def pending(store, run):
    if run.get('stage') != 'implementation' or run.get('status') not in {'blocked','failed'}:
        return False
    with store._read_db() as db:
        return db.execute("""SELECT 1 FROM records w JOIN records d
            ON d.collection='implementation_document_drafts'
                AND json_extract(d.data,'$.workflow_id')=w.key
                AND json_extract(d.data,'$.revision')=json_extract(w.data,'$.current_revision')
            WHERE w.collection='codex_workflows' AND json_extract(w.data,'$.run_id')=?
                AND json_extract(d.data,'$.run_id')=? AND json_extract(d.data,'$.status')='submitted'
                AND json_extract(w.data,'$.status')='specification'
                AND json_extract(w.data,'$.implementation_definition_repair.status') IN ('authoring','author_validation')
                AND NOT EXISTS(SELECT 1 FROM json_each(w.data,'$.implementation_definition_repair.revision_indices') i
                    WHERE i.value=json_extract(d.data,'$.revision')) LIMIT 1""",(run['id'],run['id'])).fetchone() is not None


def binding(engine, run_id, state, row):
    from . import implementation_definition_repair as definitions
    hid = row['authoring_handoff_id']
    assignment = engine.store.get('handoffs',hid,{})
    return {'version':1,'workflow_id':state['id'],'repair_id':state[definitions.FIELD]['id'],
        'handoff_id':hid,'revision':row['index'],'spec_hash':row['spec_hash'],
        'document_id':row['document']['document_id'],'document_sha256':row['document']['sha256'],
        'input_hash':assignment.get('input_hash'),'output_hash':assignment.get('output_hash')}


@operation
def saved_row(engine, run_id, state, index):
    """Authenticate the actual saved submission; never infer any review vote."""
    from . import implementation_authoring as author, implementation_definition_repair as definitions
    from .engine import ResearchBlocked
    record=state[definitions.FIELD]
    marker=record.get(CHECKPOINT)
    if marker and marker.get('status')=='pending':
        keys=[marker['binding']['handoff_id']]
    else:
        keys=engine.store.record_keys(author.COLLECTION,run_id,
            predicate="json_extract(data,'$.workflow_id')=? AND json_extract(data,'$.revision')=? "
                      "AND json_extract(data,'$.status')='submitted'",
            parameters=(state['id'],index))
    if not keys:
        return None
    if len(keys)!=1:
        raise ResearchBlocked('同一补定义版本存在多份已提交草稿，不能猜测当前交付。')
    hid=keys[0]
    draft=engine.store.get(author.COLLECTION,hid,{})
    frozen=engine.store.get('handoff_inputs',hid,{})
    ctx=frozen.get('context',{})
    if (draft.get('status')!='submitted' or draft.get('revision')!=index
            or draft.get('workflow_id')!=state['id'] or ctx.get(definitions.PROTOCOL)!=1
            or ctx.get('definition_repair',{}).get('id')!=record['id']):
        raise ResearchBlocked('保存后续接与当前补定义交付不一致。')
    result=engine.store.get('decisions',hid,{}).get('result')
    from .code_repair_amendment import seal
    assignment=engine.store.get('handoffs',hid,{})
    if seal(engine,run_id,'engineer',assignment['purpose'],frozen['instruction'],ctx,frozen['schema'],hid)!=result:
        raise ResearchBlocked('保存后续接的原作者结论发生变化。')
    # _prepare_submission verifies frozen inputs, the completed decision,
    # exact saved bytes and the successful real write-tool receipt. No commit.
    row=author._prepare_submission(engine,run_id,state,hid,result)['row']
    definitions.validate_candidate(row['spec'],state,definitions.binding(state))
    if marker and marker.get('status')=='pending' and marker.get('binding')!=binding(engine,run_id,state,row):
        raise ResearchBlocked('保存后检查点与真实原交付不一致。')
    return row


def checkpoint(engine, run_id, state, row, save):
    from . import implementation_definition_repair as definitions
    record=state[definitions.FIELD]
    value=binding(engine,run_id,state,row)
    previous=record.get(CHECKPOINT)
    if previous and previous.get('binding')==value and previous.get('status')=='pending':
        return
    if previous:
        record.setdefault('post_save_history',[]).append(copy.deepcopy(previous))
    record[CHECKPOINT]={'binding':value,'status':'pending','created_at':now()}
    record['status']='author_validation'
    save(status='specification',blocked_kind=None,reason=None)
    engine.store.event(run_id,'implementation.definition_saved_checkpoint',
        '作者交付已保存，继续宿主核验；中断后复用同一文档与真实回执，不重新生成。',
        {'workflow_id':state['id'],'repair_id':record['id'],'revision':row['index'],
         'document_sha256':row['document']['sha256']})


@operation
def recoverable(engine, run):
    """State-based admission, including migration of old post-save blockers."""
    from . import implementation_definition_repair as definitions
    from .engine import ResearchBlocked
    if not pending(engine.store,run):
        return False
    if hasattr(engine.store,'read_snapshot') and not getattr(engine.store,'is_read_snapshot',False):
        with engine.store.read_snapshot() as store:
            reader=copy.copy(engine);reader.store=store
            if getattr(getattr(engine,'sources',None),'store',None) is engine.store:
                reader.sources=copy.copy(engine.sources);reader.sources.store=store
            return recoverable(reader,run)
    for key in engine.store.record_keys('codex_workflows',run['id'],
            predicate="json_extract(data,'$.status')='specification'"):
        try:
            state=engine.store.get('codex_workflows',key)
            record=state.get(definitions.FIELD,{})
            if record.get('status') not in {'authoring','author_validation'}:
                continue
            index=state['current_revision']
            if index in record['revision_indices']:
                continue
            definitions._source(engine,run['id'],state,record)
            row=saved_row(engine,run['id'],state,index)
            if row is not None:
                # Verify host-generated pages and any existing read receipts.
                # Missing reading is bounded follow-up work, never approval.
                from .definition_readback import coverage
                coverage(engine,run['id'],state,row)
                return True
        except (ResearchBlocked,KeyError,TypeError,ValueError,OSError):
            continue
    return False
