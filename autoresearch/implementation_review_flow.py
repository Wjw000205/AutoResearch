"""Three independent current-version full-text implementation opinions."""

INDEXED_ALIGNMENT_GUIDANCE = '''
本协议的每一个角色均独立完整审核当前版本，不存在章节组或分组阅读豁免。
完整读取冻结 indexed_documents 中所有必读原文；批准架构、当前 SPEC 与本角色回复缺一不可。
逐项核对当前 SPEC 的全部 implementation_alignment_acceptance_ids，按本次实际读取返回当前版本的对齐证据，不借用其他角色的结论。
alignment_binding 必须原样回传 implementation_alignment_contract 中的 design_hash、experiment_plan_hash、architecture_document_sha256。
每个 alignment_findings.requirement_id 使用当前验收项的完整 ID，给出具体 implementation_evidence，
source_refs 必须定位批准架构原文的 document_id/document_sha256/section_id/章节相对 offset/limit，且已在本交接真实读取。
对照架构核实算法关键步骤、张量与参数、数据真实性与固定划分、泄漏、公平基线和消融、资源以及可执行验收。
完整阅读和逐项对齐不表示每轮重新扩充修改清单；异议范围遵守冻结 implementation_indexed_review_contract.convergence。
首轮一次列出已核实的全部实质阻塞；后续按本人原异议及原核验标准复核修订，不提高原要求，不为已解决或未修改的正确内容另找普通问题。
不把文档计划当作已有代码或实验结果；原实质冲突仍未解决时保留 mismatch 或 pending，并延续原异议及具体修改。
修订新引入的严重回归仅按收敛合同规定提交，不把上一轮已存在却未提出的普通缺项作为新回归。
approve=true 要求所有当前验收项已经独立核实为 aligned，且所有原实质异议经你本人逐项验证解决。
'''


def _assert_assignment_slots(engine, h, run_id, purpose, prepared):
    """Never create a fresh assignment over a partially persisted old one."""
    from .engine import ResearchBlocked

    for role, payload in prepared:
        hid = h.decision_key(run_id, role, purpose, **payload)
        prefix = f'{run_id}:{purpose}:{role}:'
        with engine.store._read_db() as db:
            rows = db.execute(
                "SELECT collection,key FROM records WHERE collection IN "
                "('handoffs','handoff_inputs','handoff_progress','decisions') "
                "AND key>=? AND key<?", (prefix, prefix + '\uffff')).fetchall()
        slots = {}
        for collection, key in rows:
            slots.setdefault(key, set()).add(collection)
        if (any(key != hid for key in slots) or
                any(not {'handoffs', 'handoff_inputs'} <= kinds for kinds in slots.values())):
            raise ResearchBlocked('实现文档索引审核：冻结交接记录不完整或身份不一致；保留原记录，未派发请求。')


async def run_indexed(engine, h, run_id, state, row, context, persist):
    """Collect the three current independent opinions before any revision.

    Frozen inputs and completed decisions are replayed exactly. A peer's normal
    failure does not discard another admitted opinion; pause cancels every peer.
    """
    import copy
    import json
    from .codex_workflow import REVIEW, REVIEW_TASK_V2, _frozen_spec_assignment
    from .intelligence import parse_json
    from . import implementation_documents as documents
    from . import implementation_indexed_review as indexed, implementation_indexed_replies as replies
    from . import implementation_spec_progress as progress, implementation_review_routing as routing
    from . import implementation_issue_responses as responses
    from . import implementation_review_convergence as convergence

    purpose = indexed.purpose(state, row)
    response_documents = await engine._read_consensus_check(run_id,
        replies.save_documents, engine, h, run_id, state, row)
    engine.check_active(run_id)
    row[indexed.PROTOCOL] = indexed.VERSION
    prepared = []
    for role in indexed.ROLES:
        frozen = await engine._read_consensus_check(run_id,
            _frozen_spec_assignment, engine, run_id, purpose, role)
        if frozen:
            instruction, value, schema = frozen['instruction'], frozen['context'], frozen['schema']
            indexed.assert_context(state, row, value, role=role)
        else:
            value = {**context, **progress.context(state, row, role),
                'implementation_review_routing_protocol': routing.VERSION}
            value = responses.review_context(row, role, value)
            value = await engine._read_consensus_check(run_id, indexed.frozen_context,
                engine, run_id, state, row, value, role=role, response_documents=response_documents)
            schema = indexed.review_schema(routing.schema(progress.schema(REVIEW)))
            if value.get(responses.PROTOCOL) == responses.VERSION:
                schema = responses.review_schema(schema, value)
            schema = convergence.schema(schema, value)
            instruction = REVIEW_TASK_V2 + indexed.GUIDANCE + INDEXED_ALIGNMENT_GUIDANCE + routing.GUIDANCE
            if value.get(responses.PROTOCOL) == responses.VERSION:
                instruction += responses.REVIEW_GUIDANCE
            instruction += convergence.guidance(value)
        engine.check_active(run_id)
        prepared.append((role, {'instruction': instruction, 'context': value, 'schema': schema}))
    # The first batch preview runs the same immutable identity and closeout
    # guards as dispatch. Freeze each role before that preview, rather than
    # asking those guards to authenticate a not-yet-created assignment. This
    # records intent only: no tools, model request, verdict or delivery credit.
    _assert_assignment_slots(engine, h, run_id, purpose, prepared)
    for role, payload in prepared:
        engine.check_active(run_id)
        h._handoff(run_id, role, purpose, **payload)
    # Admit all currently missing requests as one batch. Replays incur no new
    # model request and keep the exact original schema/instruction/context.
    if hasattr(h.intelligence, 'preflight_batch'):
        pending = []
        for role, payload in prepared:
            await h.complete_pending_tools(run_id, role, purpose, **payload)
            hid = h.decision_key(run_id, role, purpose, **payload)
            if not engine.store.get('decisions', hid):
                pending.append(await h.next_request_async(run_id, role, purpose, **payload))
        if pending:
            await engine._read_consensus_check(run_id, h.intelligence.preflight_batch, run_id, pending)
    row.setdefault('review_votes', [])
    row['review_delivery'] = {'version': 1, 'mode': 'indexed_full_round', 'complete': False,
        'collection_complete': False, 'scientific_approval': False}
    persist()

    async def collect(role, payload):
        result = await h.ask(run_id, role, purpose, payload['instruction'], payload['context'], payload['schema'])
        parse_json(json.dumps(result, ensure_ascii=False), payload['schema'])
        hid = h.decision_key(run_id, role, purpose, **payload)
        proof = await engine._read_consensus_check(run_id, indexed.validate_vote,
            engine, run_id, state, row, role, hid, result, payload['context'])
        engine.check_active(run_id)
        vote = {'role': role, 'handoff_id': hid, **copy.deepcopy(result), 'reading_proof': proof}
        previous = [item for item in row['review_votes'] if item.get('role') == role]
        if previous and (len(previous) != 1 or previous[0] != vote):
            from .engine import ResearchBlocked
            raise ResearchBlocked('实现文档索引审核：已保存的角色意见与真实冻结交付不一致。')
        if not previous:
            row['review_votes'].append(vote)
            row['review_votes'].sort(key=lambda item: indexed.ROLES.index(item['role']))
        persist()

    await documents._collect_admitted(collect, prepared)
    engine.check_active(run_id)
    if len(row['review_votes']) != 3 or {vote['role'] for vote in row['review_votes']} != set(indexed.ROLES):
        from .engine import ResearchBlocked
        raise ResearchBlocked('实现文档索引审核：尚未收齐三个角色的完整独立意见。')
    receipt = {'version': indexed.VERSION, 'binding': indexed.binding(state, row),
        'votes': [{'role': vote['role'], 'handoff_id': vote['handoff_id'],
            'result_sha256': routing.digest({key: value for key, value in vote.items()
                if key not in {'role', 'handoff_id', 'reading_proof'}}),
            'context_sha256': routing.digest(engine.store.get('handoff_inputs', vote['handoff_id'])['context'])}
            for vote in row['review_votes']], 'scientific_approval': False}
    row[indexed.FIELD] = {**receipt, 'sha256': routing.digest(receipt)}
    row['review_delivery'].update(complete=True, collection_complete=True)
    row['approved'] = all(vote['approve'] is True for vote in row['review_votes'])
    persist()
    return row['review_votes']

