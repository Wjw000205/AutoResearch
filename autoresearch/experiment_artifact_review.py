"""One independent, read-only review of an engineer's artifact assessment."""
from __future__ import annotations

import copy

from . import experiment_evidence_handoff as handoff
from .experiment_workflow import sha
from .training_adjustment import obj, _group
from . import artifact_fact_review as facts
from . import artifact_review_pages as pages


def author_reads(engine, run_id, record, assignment):
    """Reuse the author's authenticated reads of this exact frozen material."""
    saved = record[handoff.KEY].get('independent_review', {})
    source = saved.get('author_assignment', assignment)
    from . import architecture_validation_batch as validation_batch
    correction = source['context']['experiment_decision'].get(validation_batch.CORRECTION)
    if correction:
        owner = correction['parent']
        inputs = engine.store.get('handoff_inputs', owner)
        parent = {k: inputs[k] for k in ('instruction', 'context', 'schema')}
        parent['purpose'] = engine.store.get('handoffs', owner)['purpose']
        ref = record['evidence']
        archived = engine.store.get('experiment_evidence', ref['id'])
        if archived['run_id'] != run_id or sha(archived['summary']) != ref['sha256']:
            raise ValueError('Architecture comparison correction lost its frozen evidence')
        evidence = archived['summary']
        expected = validation_batch.correction(engine, run_id, parent, correction['answer'],
                                                evidence, correction['rejection'])
        if expected != source:
            raise ValueError('Architecture comparison correction lost its original artifact assignment')
        return handoff.require_reads(engine, run_id, 'engineer', **parent)
    from . import scoped_parameter_author
    marker = source['context']['experiment_decision'].get(scoped_parameter_author.KEY)
    if marker:
        return scoped_parameter_author.original_reads(engine, run_id, record, source, marker)
    return handoff.require_reads(engine, run_id, 'engineer', source['purpose'],
                                 source['instruction'], source['context'], source['schema'])


def require_binding(record):
    from .independent_full_review import members, reviewers
    saved = record.get(handoff.KEY, {}).get('independent_review')
    if not saved:
        if record.get(handoff.KEY, {}).get(facts.KEY):
            return False
        return not record.get('proposal', {}).get('artifact_review_sha256')
    return (members(saved.get('results', []))
            and {r.get('role') for r in saved.get('reads', [])} == set(reviewers(saved.get('results', [])))
            and record.get('proposal', {}).get('artifact_review_sha256') == sha(saved['results']))


def vote_instruction(engine, run_id, purpose, base, context, schema):
    """Keep already paid vote requests unchanged when correcting the read gate."""
    from .engine import Harness
    legacy = base + '\n批准前必须用 experiments.compare 逐页读完 artifact_evidence_handoff.required_paths 的补充产物摘要，独立核验工程师结论。'
    current = base + ('\n独立核验工程师结论。若本角色已在 artifact_review_results 对应的同一产物快照'
        '完成逐页复核，可复用该角色原始读取回执；宿主会重新核验角色、快照 SHA、路径覆盖和提案绑定。'
        '没有匹配的原始读取时，须用 experiments.compare 逐页读完 required_paths。'
        '复用读取不继承批准：仍须针对当前 decision_proposal 和新补读观察独立投票。')
    reused = base + ('\n宿主已逐项验证本次补充产物和 parameter_resolution 来源任务的产物内容一致，'
        '并已核验你本人对全部 required_paths 的原始读取回执。可复用这些页，直接审查当前候选、'
        '主证据变化和补读观察，按需读取具体疑点。请针对当前 proposal_sha256 独立投票。')
    for text in (legacy, current, reused):
        for role in ('director', 'engineer', 'scout'):
            key = Harness.decision_key(run_id, role, purpose, text, context, schema)
            if engine.store.get('handoffs', key):
                handoff.assignment_key(engine, run_id, role, purpose, text, context, schema)
                return text
    from . import artifact_vote_reuse
    source = artifact_vote_reuse.predecessor(engine, run_id, context)
    if source:
        try:
            for role in ('director', 'engineer', 'scout'):
                artifact_vote_reuse.role_read(engine, run_id, role, source)
        except (ValueError, KeyError, StopIteration):
            return current
        return reused
    return current


def vote_reads(engine, run_id, record, role, purpose, instruction, context, schema):
    """Reuse actual same-role artifact reads, never another role's read or vote."""
    vote_key = handoff.assignment_key(engine, run_id, role, purpose, instruction, context, schema)
    saved = record[handoff.KEY]
    review = saved.get('independent_review')
    if not review:
        try:
            return handoff.require_reads(engine, run_id, role, purpose, instruction, context, schema)
        except handoff.MissingArtifactPages as missing:
            from . import artifact_vote_reuse
            from .diagnostic_planning_recovery import authenticate
            if (record.get('evidence') != context.get('validation_evidence')
                    or record.get('proposal') != context.get('decision_proposal')
                    or record.get('proposal_sha256') != context.get('proposal_sha256')
                    or handoff.packet(saved, for_review=True) != context['experiment_decision'][handoff.KEY]):
                raise
            source = artifact_vote_reuse.predecessor(engine, run_id, context)
            if not source:
                raise
            vote = (engine.store.get('decisions', vote_key) or {}).get('result') or {}
            authenticate(engine, run_id, role, purpose,
                {'instruction': instruction, 'context': context, 'schema': schema}, vote)
            if vote.get('proposal_sha256') != record['proposal_sha256'] or vote.get('evidence_sha256') != record['evidence']['sha256']:
                raise ValueError('Artifact reads do not authorize a vote on another proposal')
            try:
                prior = artifact_vote_reuse.role_read(engine, run_id, role, source)
            except (ValueError, KeyError, StopIteration) as exc:
                raise missing from exc
            return {**prior, 'vote_handoff_id': vote_key}
    request = review['assignment']
    prior_context = request['context']
    packet = context['experiment_decision'][handoff.KEY]
    prior_packet = prior_context['experiment_decision'][handoff.KEY]
    results = context.get('artifact_review_results', {})
    if (not require_binding(record)
            or record['proposal_sha256'] != sha(record['proposal'])
            or context.get('proposal_sha256') != record['proposal_sha256']
            or context.get('decision_proposal') != record['proposal']
            or context.get('validation_evidence') != record['evidence']
            or prior_context.get('validation_evidence') != record['evidence']
            or context.get('evidence_sha256') != record['evidence']['sha256']
            or prior_context.get('evidence_sha256') != record['evidence']['sha256']
            or packet['evidence'] != facts.reference(saved) or prior_packet['evidence'] != facts.reference(saved)
            or record['proposal'].get('artifact_evidence_sha256') != facts.reference(saved)['sha256']
            or not set(packet['required_paths']).issubset(prior_packet['required_paths'])
            or results.get('results') != review['results'] or results.get('sha256') != sha(review['results'])):
        raise ValueError('Retained artifact reads do not match this vote, proposal and frozen evidence')
    # Recompute coverage from the immutable parent's actual tool results.
    # A cached read flag or the reviewer's statement is insufficient.
    proof = pages.verified_reads(engine, run_id, role, review)
    original = next(r for r in review['reads'] if r['role'] == role)
    if original.get('handoff_id') != proof['handoff_id']:
        raise ValueError('Retained artifact reads belong to a different reviewer assignment')
    return {**proof, 'vote_handoff_id': vote_key, 'read_origin': 'independent_artifact_review',
            'review_sha256': sha(review['results'])}


async def finish_votes(engine, harness, run_id, record, votes, request, checkpoint):
    from . import independent_full_review as full
    results, reads = [], []
    for vote in votes:
        role = vote['role']
        if full.KEY in vote:
            def check(current):
                frozen=full.authenticate(engine.store,run_id,current)
                ids=full.reading_handoffs(engine.store,run_id,current)
                hid=current[full.KEY]['handoff_id']
                try:
                    return handoff.require_reads(engine,run_id,role,
                        engine.store.get('handoffs',hid)['purpose'],frozen['instruction'],frozen['context'],frozen['schema'],
                        prior_handoffs=ids[1:])
                except handoff.MissingArtifactPages as exc:
                    exc.missing_read_targets=exc.pages
                    exc.covered_chars=full.read_progress(engine.store,run_id,current,{'experiments.compare'})
                    if record.get('reuse_fact_review_pages'):
                        try:
                            return vote_reads(engine, run_id, record, role, engine.store.get('handoffs',hid)['purpose'],
                                              frozen['instruction'], frozen['context'], frozen['schema'])
                        except (ValueError, KeyError, StopIteration) as prior_error:
                            raise exc from prior_error  # This voter can still read the exact missing pages.
                    raise
            current = await full.ask(harness,run_id,role,request['purpose'],request['instruction'],
                request['context'],request['schema'],binding=vote[full.KEY]['policy'],validator=check)
            results.append(current)
            reads.append(check(current))
            continue
        history = record.get('artifact_vote_page_assignments', {}).get(role, [])
        if not history:
            try:
                proof = await engine._read_consensus_check(run_id, vote_reads, engine, run_id, record, role, **request)
            except handoff.MissingArtifactPages:
                pass
            else:
                results.append(vote)
                reads.append(proof)
                continue
        async def save(assignments):
            await checkpoint(artifact_vote_page_assignments={**record.get('artifact_vote_page_assignments', {}),
                                                             role: assignments}, status='reviewing')
            engine.store.event(run_id, 'experiment.artifact_vote_continuation',
                '本轮投票仍有缺页，已交原评审定向补读。', {'role': role, 'decision_id': record['id'],
                'missing_pages': assignments[-1]['context']['artifact_review_request']['page_continuation']['missing_pages']})
        result, proof = await pages.finish_pages(engine, harness, run_id, role, request,
            {k: v for k, v in vote.items() if k != 'role'}, history, save)
        results.append(result)
        reads.append({**proof, 'vote_handoff_id': proof['handoff_id']})
    return results, reads


def _include_host_checks(context, source):
    from .diagnostic_check_evidence import matching
    packet = context['experiment_decision']
    contract = source.get('cpu_behavior_check', {}).get('staged_observations', {})
    if not contract or packet.get('architecture_revision_workflow'):
        return
    packet['cpu_behavior_check']['staged_observations'] = copy.deepcopy(contract)
    if source.get('diagnostic_host_checks'):
        packet['diagnostic_host_checks'] = copy.deepcopy(source['diagnostic_host_checks'])
        return
    if 'diagnostic_host_checks' not in packet and contract.get('host_source_sha256'):
        reports = matching(contract['host_source_sha256'])
        if reports:
            packet['diagnostic_host_checks'] = reports


async def _include_prior_observations(engine, run_id, record, context):
    packet = context['experiment_decision']
    if (packet.get('recent_revision') or {}).get('status') not in {'rejected', 'admission_blocked'} or 'as_observed_in_prior_review' in (packet.get('prior_diagnostic_review') or {}):
        return
    from .experiment_review_reuse import prior_artifact_observations
    base = engine.store.get('experiment_evidence', record['evidence']['id'])
    prior = await engine._read_consensus_check(run_id, prior_artifact_observations, engine, run_id, record, base['summary'])
    if prior:
        if packet.get('architecture_revision_workflow'):
            prior = {k:prior[k] for k in ('decision_id', 'proposal_sha256', 'evidence', 'artifact_evidence_sha256',
                     'artifact_trial_ids', 'as_observed_in_prior_review')}
        packet['prior_diagnostic_review'] = prior


async def advance(engine, harness, run_id, record, context, instruction, schema, checkpoint,
                  *, proposal=None, assignment=None):
    """Persist each transition; never recollect artifacts or authorize execution."""
    current = record[handoff.KEY]
    review = current.get('independent_review')
    if not review:
        fact_spec = current.get(facts.KEY)
        if assignment is None or (not fact_spec and not handoff.requested(proposal or {})):
            raise ValueError('An artifact review requires the assigned engineer assessment')
        if fact_spec:
            facts.authenticate(engine, run_id, current, proposal, assignment)
        else:
            from .diagnostic_planning_recovery import authenticate
            authenticate(engine, run_id, 'engineer', assignment['purpose'],
                         {k: assignment[k] for k in ('instruction', 'context', 'schema')}, proposal)
        if proposal.get('artifact_evidence_sha256') != current['evidence']['sha256']:
            raise ValueError('Engineer assessment is not bound to the supplied artifacts')
        read = author_reads(engine, run_id, record, assignment)
        from .training_investigation import validate_reads
        merged = handoff.merge_reads(record['analysis_reads'], read)
        archive = engine.store.get('experiment_evidence', current['evidence']['id']) or {}
        if archive.get('run_id') != run_id or sha(archive.get('summary')) != current['evidence']['sha256']:
            raise ValueError('Read-only artifact evidence changed')
        base = engine.store.get('experiment_evidence', context['validation_evidence']['id']) or {}
        if base.get('run_id') != run_id or sha(base.get('summary')) != context['evidence_sha256']:
            raise ValueError('Original decision evidence changed')
        validate_reads(merged, proposal, base['summary'], supporting_source_paths=read['source_paths'])
        review_context = copy.deepcopy(context)
        _include_host_checks(review_context, assignment['context']['experiment_decision'])
        await _include_prior_observations(engine, run_id, record, review_context)
        from .independent_full_review import REVIEWERS
        if context.get('experiment_decision', {}).get('architecture_review_protocol') == 1:
            from .paper_baselines import assessment_results
            from .training_investigation import fit_histories
            review_context['experiment_decision']['measured_results'] = assessment_results(engine.store, run_id, base['summary'])
            review_context['experiment_decision']['fit_histories'] = await engine._read_consensus_check(
                run_id, fit_histories, engine, run_id, base['summary'], current['request'].get('artifact_trial_ids'))
        packet = handoff.packet(current, for_review=True)
        packet['reviewers'] = list(REVIEWERS)
        # Initialization RNG records are omitted from the compact summary.
        # Reviewers must actually read the existing full provenance, too.
        if (not fact_spec and current.get('kind') != 'yaml_candidate_audit'
                and not archive['summary'].get('paired_review_protocol')):
            packet['required_paths'] += [
                f'/trials/{i}/model_provenance/data'
                for i, trial in enumerate(archive['summary']['trials'])
                if trial.get('model_provenance', {}).get('status') == 'available']
        review_context['experiment_decision'][handoff.KEY] = packet
        review_context['artifact_review_request'] = {
            'assessment_sha256': sha(proposal),
            'assessment': {k: proposal[k] for k in ('rationale', 'hypothesis', 'verification', 'stop_condition')},
            'execution_authorized': False, 'scientific_approval': False}
        text = {'type': 'string', 'minLength': 1, 'maxLength': 900}
        review_schema = obj({
            'evidence_sha256': {'const': context['evidence_sha256']},
            'artifact_evidence_sha256': {'const': packet['evidence']['sha256']},
            'assessment_sha256': {'const': sha(proposal)},
            'conclusion': {'enum': ['consistent_within_scope', 'conflict', 'unresolved']},
            'findings': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': obj({
                'check': text, 'outcome': {'enum': ['equal', 'different', 'unknown', 'not_applicable']},
                'evidence_paths': {'type': 'array', 'minItems': 1, 'maxItems': 6, 'items': text},
                'detail': text})},
            'remaining_gaps': {'type': 'array', 'maxItems': 5, 'items': text},
            'next_step': text})
        review_instruction = (
            '完成工程师交来的既有产物独立复核。这是只读事实核验，不是批准训练或架构准入。\n'
            '先用 experiments.compare 按 artifact_evidence_handoff.required_paths 逐页读完既有快照；'
            '必须完成具体核对，不能只建议“以后交给三方复核”。按需读取 evaluation_details/data 和原冻结参数。\n'
            '围绕原 request.verification 核对所选试次及已有材料，将实际发现写入 findings。'
            'evidence_paths 只引用补充快照中的真实 JSON 路径。区分文件字节哈希、程序自报和独立执行证明。\n'
            '未读取不等于缺失；核对结果为相等、不同、不适用或真实无法判定。'
            '说明哪些未知实际阻止下一步、哪些只是证据等级限制。不得重跑训练、修改代码或访问留出集。'
            '三方结果将交工程师汇总具体下一步，该提案仍需独立投票。')
        if current.get('kind') == 'parameter_search_audit':
            review_instruction = (
                '独立核验宿主参数搜索拒绝审计；这是只读事实复核，不批准执行。'
                '用 experiments.compare 逐页读完 artifact_evidence_handoff.required_paths，'
                '核对原控制与 prospective 配置各自SHA、种子和逐字段差异，'
                '确认拒绝发生在候选构造之前；重建配置不是历史运行回执。'
                '路径错误与 remaining_proposal_gate 分开判断，未知训练根因保持未知。'
                'findings.evidence_paths 引用补充快照中的 /parameter_search_audit 真实路径。'
                '不要重复核验无关的初始化/容量产物，不重跑训练或修改代码。'
                '评价工程师是否回应实际剩余门禁，列明真正阻塞项与下一步。'
                '三方结果交工程师汇总具体提案后，仍须另行独立投票。')
        if current.get('kind') == 'yaml_candidate_audit':
            review_instruction = ('只读核验已保存的候选与控制YAML，复审配置正文和单因素差异问题。'
                '用experiments.compare读完required_paths，两份正文、解析结果及校验均在补充快照。'
                '独立核对精确版本与SHA，逐字段比较唯一变化及其他字段相等性，拒绝未声明变化或校验失败。'
                'findings.evidence_paths只引用/yaml_candidate_audit下真实路径。'
                '不再索取无关训练产物，不新建同一候选，不将静态核验当作运行消费、训练或稳定性结论。'
                '三方事实复核后仍须对具体参数批次独立投票和资源准入。')
        if fact_spec:
            review_instruction = (
                '只读复核一次明确的检查点身份问题，不批准执行。原工程师答复中的完成优化建议'
                '尚未通过冻结判据和训练门禁；保留原答复，不把它视为批准。\n'
                '按 artifact_evidence_handoff.required_paths 分页读完 /checkpoint_identity、'
                '/host_checkpoint_audit、/fact_review_scope。独立判断文件字节、保存张量指纹与'
                '历史程序自报恢复后指纹的对应性；区分文件一致性与历史实际执行证明。'
                '此次复核不要求重新审查八个实验的初始化或容量，原始产物仍可按具体冲突定向读取。\n'
                '根因未知不等于可忽略告警；恢复对应核验也不能认证稳定。逐项说明真实已关闭的疑问、'
                '仍缺失的历史观测，以及哪个剩余问题实际阻止当前下一步。findings 引用本次快照真实路径。'
                '给出唯一最小后续与区分竞争解释的条件；若没有合规方案，明确具体阻塞，'
                '不得建议重读同一材料、重做已完成测量、重跑训练、改阈值或扩大网格。'
                '三方意见交工程师综合，后续提案仍接受原全部门禁及独立投票。')
            if fact_spec.get('kind') == 'paired_initialization_fact_review':
                review_instruction = (
                    '只读复核新增的精确同种子初始化配对证据，不批准执行。旧复核已核验的容量和指标记录保留。'
                    '宿主原来只给消融端，现已补上对应的已完成共享控制；这不是新训练。'
                    '用 experiments.compare 完整读取 required_paths 的 pair_scope、pair_comparisons 和 fact_review_scope。'
                    '核对每个配对的种子、双方trial_id、来源SHA、22项或实际公共名称及七类字段的比较；'
                    '字段数和名称以实际记录为准，缺失、重复、歧义或不同必须明确保留。'
                    'pair_comparisons 是宿主逐字段计算的保存记录对照，原始双方记录均在对应 trial_path/control_path 下，'
                    '具体疑问定向读取，不重复全文核对已关闭的其他项目。'
                    '区分保存记录一致与独立历史执行见证，不由前者推断机制有效或参数稳定。'
                    '只裁定新增证据是否关闭原配对缺口，并指出真实剩余门禁。'
                    '不重跑、不改码、不改阈值、不访问留出集。三方结果交工程师提出下一步并另行独立投票。')
        review_instruction = review_instruction.replace('三方', '本次独立评审')
        from . import experiment_review_reuse as prior_reading
        proof = await engine._read_consensus_check(run_id, prior_reading.prepare_after_analysis, engine, run_id, record, review_context, 3)
        if proof:
            review_context[prior_reading.KEY] = proof
        review = {'assessment': copy.deepcopy(proposal), 'author_assignment': copy.deepcopy(assignment),
                  'author_read': read, 'assignment': {
                      'purpose': 'experiment_diagnose:' + context['evidence_sha256'] + ':artifact-review-1:' + sha(proposal),
                      'instruction': review_instruction, 'context': review_context, 'schema': review_schema}}
        await checkpoint(**{handoff.KEY: {**current, 'status': 'reviewing_evidence',
            'owner': 'reviewers', 'independent_review': review}}, analysis_reads=merged,
            status='reviewing_evidence')
        engine.store.event(run_id, 'experiment.artifact_review',
            '工程师的产物核验已交独立评审，沿用已有快照。',
            {'decision_id': record['id'], 'assessment_sha256': sha(proposal),
             'owners': list(REVIEWERS), 'execution_authorized': False})
    if not review.get('results'):
        request = review['assignment']
        from . import independent_full_review as full
        def complete_reads(vote):
            frozen = full.authenticate(engine.store, run_id, vote)
            hid = vote[full.KEY]['handoff_id']
            ids = full.reading_handoffs(engine.store, run_id, vote)
            try:
                return handoff.require_reads(engine, run_id, vote['role'],
                    engine.store.get('handoffs',hid)['purpose'], frozen['instruction'], frozen['context'], frozen['schema'],
                    prior_handoffs=ids[1:])
            except handoff.MissingArtifactPages as exc:
                exc.missing_read_targets = exc.pages
                spans = {}
                for own in ids:
                    for row in engine.store.get('handoff_progress',own,{}).get('tool_results',[]):
                        page=row.get('result',{});args=row.get('call',{}).get('arguments',{})
                        if row.get('call',{}).get('name')=='experiments.compare' and type(page.get('offset')) is int:
                            spans.setdefault((args.get('path'),page.get('content_sha256')),[]).append((page['offset'],page['end']))
                count=0
                for ranges in spans.values():
                    cursor=0
                    for a,b in sorted(ranges):
                        if b>max(cursor,a): count+=b-max(cursor,a)
                        cursor=max(cursor,b)
                exc.covered_chars = len(full.material_text(frozen['context'])) + count
                raise
        originals = await full.group(harness, run_id, request['purpose'], request['instruction'],
            request['context'], request['schema'], validator=complete_reads)
        results, reads = [], []
        for original in originals:
            role = original['role']
            if full.KEY in original:
                frozen = full.authenticate(engine.store,run_id,original)
                actual_request = {k:copy.deepcopy(frozen[k]) for k in ('instruction','context','schema')}
                actual_request['purpose'] = engine.store.get('handoffs',original[full.KEY]['handoff_id'])['purpose']
                review.setdefault('full_assignments',{})[role] = actual_request
                results.append(original)
                reads.append(complete_reads(original))
                continue
            answer = {k: v for k, v in original.items() if k != 'role'}
            async def save(assignments):
                nonlocal review
                review = {**review, 'page_assignments': {**review.get('page_assignments', {}), role: assignments}}
                await checkpoint(**{handoff.KEY: {**record[handoff.KEY],
                    'independent_review': review}}, status='reviewing_evidence')
                engine.store.event(run_id, 'experiment.artifact_read_continuation',
                    '必读页尚未读完，已派回原评审继续读取。', {'role': role, 'decision_id': record['id'],
                    'missing_pages': assignments[-1]['context']['artifact_review_request']['page_continuation']['missing_pages']})
            result, proof = await pages.finish_pages(engine, harness, run_id, role, request, answer,
                review.get('page_assignments', {}).get(role, []), save)
            results.append(result)
            reads.append(proof)
        from .experiment_decisions import _pointer
        archive = engine.store.get('experiment_evidence', facts.reference(record[handoff.KEY])['id'])
        for result in results:
            for finding in result['findings']:
                for path in finding['evidence_paths']:
                    _pointer(archive['summary'], path)
        review = {**review, 'results': results, 'reads': reads}
        await checkpoint(**{handoff.KEY: {**record[handoff.KEY], 'status': 'synthesizing',
            'owner': 'engineer', 'independent_review': review}}, status='proposing')
    if not review.get('synthesis_assignment'):
        synthesis_context = copy.deepcopy(context)
        reviewed_context = review['assignment']['context']['experiment_decision']
        for field in ('fit_histories', 'measured_results', 'prior_diagnostic_review'):
            if field in reviewed_context:
                synthesis_context['experiment_decision'][field] = copy.deepcopy(reviewed_context[field])
        await _include_prior_observations(engine, run_id, record, synthesis_context)
        synthesis_context['experiment_decision'][handoff.KEY] = handoff.packet(record[handoff.KEY], for_review=True)
        if 'selected_validation_runs' not in synthesis_context['experiment_decision']:
            from .experiment_consensus import validation_runs
            selected = await engine._read_consensus_check(run_id, validation_runs, engine, run_id, record)
            if selected:
                synthesis_context['experiment_decision']['selected_validation_runs'] = selected
                if record.get('cpu_check_work_order_identity') in (2, 3):
                    from .scoped_work_order_contract import describe_cpu_check
                    describe_cpu_check(engine, run_id, synthesis_context, work_order_identity=record['cpu_check_work_order_identity'])
        _include_host_checks(synthesis_context, reviewed_context)
        synthesis_context['artifact_review_results'] = {
            'sha256': sha(review['results']), 'results': review['results'],
            'execution_authorized': False, 'scientific_approval': False}
        synthesis_schema = copy.deepcopy(schema)
        if (record.get('proposal_request_contract_version') in (2, 3)
                and context['experiment_decision'].get('tuning_scope', {}).get('status') == 'accepted'):
            from .proposal_request_contract import bind_search_scope
            ref = record['evidence']
            archived = engine.store.get('experiment_evidence', ref['id']) or {}
            if archived.get('run_id') != run_id or sha(archived.get('summary')) != ref['sha256']:
                raise ValueError('Synthesis needs the unchanged scoped parameter evidence')
            bind_search_scope(synthesis_schema, archived['summary'])
        for field, value in (('artifact_evidence_sha256', facts.reference(record[handoff.KEY])['sha256']),
                             ('artifact_review_sha256', sha(review['results']))):
            synthesis_schema['properties'][field] = {'const': value}
            synthesis_schema['required'].append(field)
        if record[handoff.KEY].get(facts.KEY):
            base = engine.store.get('experiment_evidence', record['evidence']['id'])
            synthesis_schema = facts.synthesis_schema(synthesis_schema, base['summary'])
        review = {**review, 'synthesis_assignment': {
            'purpose': 'experiment_decide:' + context['evidence_sha256'] + ':artifact-synthesis-1:' + sha(review['results']),
            'instruction': instruction + '\n已收到既有产物的独立核验。复用你已保存的核验与读取回执，'
                '根据 artifact_review_results 汇总具体下一步，不重复索取原产物。'
                '各方意见不自动等于架构通过：核对事实、冲突与当前门禁，保留真实未知。'
                '若仍无法提出可执行下一步，使用只读 diagnose 并在 rationale 和 verification 明确'
                '具体缺项、为何阻塞及需由谁补齐。此时保留具体阻塞，不自动再次循环。'
                '新提案仍需独立投票，取证完成后继续执行相应的架构、参数或科学验收。',
            'context': synthesis_context, 'schema': synthesis_schema}}
        await checkpoint(**{handoff.KEY: {**record[handoff.KEY], 'independent_review': review}})
    return _synthesis_request(review)


def _synthesis_request(review):
    from .experiment_source_followup import KEY as SOURCE_CALLS
    assignments = [review[key] for key in ('host_revision_followup', 'configuration_originals', SOURCE_CALLS, 'reviewer_read_followup', 'configuration_followup',
                   'weight_followup', 'synthesis_assignment') if key in review]
    # A follow-up appends its suffix to its actual parent's frozen purpose.
    return next(a for a in assignments if not any(
        other['purpose'].startswith(a['purpose'] + ':') for other in assignments))


async def host_revision_followup(engine, run_id, record, proposal, assignment, checkpoint):
    """Supply a changed, locally tested host to the engineer who requested more evidence."""
    from .diagnostic_planning_recovery import authenticate
    from .diagnostic_check_evidence import matching, source_locations
    from .scoped_work_order_contract import staged_contract
    current = record[handoff.KEY]
    review = current['independent_review']
    if not assignment or assignment != _synthesis_request(review) or not handoff.requested(proposal):
        return None
    packet = assignment['context']['experiment_decision']
    old = packet.get('cpu_behavior_check', {}).get('staged_observations', {}).get('host_source_sha256')
    if not old or 'host_revision_followup' in review:
        return None
    requested = proposal['verification']
    named_host = any(name in requested or digest in requested for name, digest in old.items())
    host_requested = '宿主' in requested and '宿主' in current['request']['verification']
    if not named_host and not host_requested:
        return None
    contract = staged_contract()
    sources = contract['host_source_sha256']
    if sources == old:
        return None
    reports = await engine._read_consensus_check(run_id, matching, sources)
    if not reports:
        return None
    owner = await engine._read_consensus_check(run_id, authenticate, engine, run_id, 'engineer',
        assignment['purpose'], {k: assignment[k] for k in ('instruction', 'context', 'schema')}, proposal)
    ctx = copy.deepcopy(assignment['context'])
    updated = ctx['experiment_decision']
    updated['cpu_behavior_check']['staged_observations'] = contract
    updated['diagnostic_host_checks'] = reports
    updated['diagnostic_host_source_locations'] = source_locations(sources)
    updated['previous_diagnostic_host'] = {'handoff_id': owner, 'host_source_sha256': old,
        'required_evidence': proposal['verification']}
    new = {**assignment, 'context': ctx, 'purpose': assignment['purpose'] + ':diagnostic-host:' + sha(sources),
        'instruction': assignment['instruction'] + '\n宿主实现已更新。按 staged_observations 和 diagnostic_host_checks 核对当前版本、'
            '实测用例及尚缺内容。diagnostic_host_source_locations 提供实际调用位置及精确分页。检查宿主时复用独立 CPU 测试，'
            '研究夹具执行回执须待新案通过评审后取得。原两席意见针对此前版本。可据当前能力另提具体方案，由两席独立审核后再执行。'}
    await checkpoint(**{handoff.KEY: {**current, 'status': 'synthesizing',
        'independent_review': {**review, 'host_revision_followup': new}}}, status='proposing')
    return new


async def configuration_followup(engine, run_id, record, evidence, proposal, assignment, checkpoint):
    from .diagnostic_planning_recovery import authenticate
    from .saved_validation_configuration import collect
    current = record[handoff.KEY]
    review = current['independent_review']
    selected = current['request'].get('artifact_trial_ids')
    earlier = review.get('configuration_followup')
    if (not selected or not review.get('results') or review.get('configuration_originals')
            or assignment != _synthesis_request(review)):
        return None
    supplement = handoff.bound_evidence(engine, run_id, record, proposal)
    material = await engine._read_consensus_check(run_id, collect, engine, run_id, evidence, supplement,
                                                  selected, proposal['verification'], originals=True)
    packet = assignment['context']['experiment_decision']
    if material is None or sha(material) == (packet.get('saved_validation_configuration') or {}).get('sha256'):
        return None
    if earlier:
        old = earlier['context']['experiment_decision']['saved_validation_configuration']['result']
        previously_supplied = {r['trial_id'] for r in old['trials'] if r.get('staged_inputs')}
        if not any(r.get('staged_inputs') and r['trial_id'] not in previously_supplied for r in material['trials']):
            return None
    owner = await engine._read_consensus_check(run_id, authenticate, engine, run_id, 'engineer',
        assignment['purpose'], {k: assignment[k] for k in ('instruction', 'context', 'schema')}, proposal)
    ctx = copy.deepcopy(assignment['context'])
    ctx['experiment_decision']['saved_validation_configuration'] = {
        'source_handoff_id': owner, 'previous_assessment': proposal, 'result': material, 'sha256': sha(material)}
    new = {**assignment, 'context': ctx, 'purpose': assignment['purpose'] + ':configuration:' + sha(material),
        'instruction': assignment['instruction'] + '\n本次按你点名的字段提供 saved_validation_configuration。'
            '逐项核对原 YAML、获准入参和运行回报；缺失的 reported_actual_value 仍是未知。'
            '如有 staged_inputs，这是对应试次归档目录中的输入原件。text 保留原文，sha256 按文件字节计算；'
            'params_sha256 按准入参数规范化计算。使用原输入和已核对的加载代码重建配置，区分重建值和历史内存实载。'
            '引用对应 records 或 job_parameters 的原路径，继续原修改单和独立复审。'
            '配置回执不能证明未记录的历史内部状态。请区分当前验证所需的条件和仅限制历史归因的条件。'}
    await checkpoint(**{handoff.KEY: {**current, 'status': 'synthesizing',
        'independent_review': {**review, ('configuration_originals' if earlier else 'configuration_followup'): new}}}, status='proposing')
    return new


async def weight_followup(engine, run_id, record, evidence, proposal, assignment, checkpoint):
    """Continue a completed fact review once when newly inspectable saved files exist."""
    from .diagnostic_planning_recovery import authenticate
    from .saved_validation_weights import bind_prior_files, collect
    current = record[handoff.KEY]
    review = current['independent_review']
    trial_ids = current['request'].get('artifact_trial_ids')
    if (not trial_ids or not review.get('results') or review.get('weight_followup')
            or assignment != review.get('synthesis_assignment')
            or not assignment['context']['experiment_decision'].get('architecture_review_protocol')):
        return None
    payload = {key: assignment[key] for key in ('instruction', 'context', 'schema')}
    source = await engine._read_consensus_check(run_id, authenticate, engine, run_id, 'engineer',
                                                assignment['purpose'], payload, proposal)
    prior = assignment['context']['experiment_decision'].get('prior_diagnostic_review')
    bound = await engine._read_consensus_check(run_id, bind_prior_files, engine, run_id, record,
        evidence, proposal, trial_ids, prior)
    material = await engine._read_consensus_check(run_id, collect, engine, run_id, evidence, trial_ids,
        proposal['verification'], bound)
    if material is None or sha(material) == (assignment['context']['experiment_decision'].get('saved_weight_audit') or {}).get('sha256'):
        return None
    packet = copy.deepcopy(assignment['context'])
    packet['experiment_decision']['saved_weight_audit'] = {
        'source_handoff_id': source, 'previous_assessment': proposal, 'result': material, 'sha256': sha(material)}
    new = {**assignment, 'purpose': assignment['purpose'] + ':saved-weights:' + sha(material),
           'context': packet, 'instruction': assignment['instruction'] +
           '\n宿主已按原取证范围读取现存权重文件。核对 saved_weight_audit、测量摘要和批准的架构/实现规范，'
           '在理由中引用该摘要及其 SHA；evidence_paths 和 training_response.evidence_paths 仍引用各自已冻结快照中的路径。'
           '分别说明已观察的实现偏差、仍未知的历史行为和可检验的结构假设。原运行未留存的绑定如实记为未知。'
           '核对哪些证据是当前代码诊断或可行性验证的前置要求，哪些仅限制后续机制归因。'
           '依据已有代码和结果提出可执行的一步；可以提出落实已批规范的代码纠正、单机制修订或有区分力的诊断。'
           '新执行仍须独立评审。若确实无法推进，指出冻结规范的具体条款和无法获得的必要证据。'
           + ('\n已把本轮所引清单行与上轮同试次的原件按字节比对。matches_prior_file 为真时才可引用原张量审计，'
              '历史内存实载及评分输入仍按原核验记录。分别说明历史归因和当前性能诊断各自需要的证据。'
              '回到冻结的研究目标，核对当前实现与验证口径，评估在现有资源权限内可检验的下一步。'
              '原方案的硬限制继续适用；不同设计独立送审。若所有可行路径均受阻，说明各自缺少的具体前置条件。'
              if 'prior_audit_sha256' in material else '')}
    await checkpoint(**{handoff.KEY: {**current, 'status': 'synthesizing', 'independent_review': {
        **review, 'weight_followup': new}}}, status='proposing')
    return new
