"""One independent, read-only review of an engineer's artifact assessment."""
from __future__ import annotations

import copy

from . import experiment_evidence_handoff as handoff
from .experiment_workflow import sha
from .training_adjustment import obj, _group
from . import artifact_fact_review as facts


def author_reads(engine, run_id, record, assignment):
    """Reuse the author's authenticated reads of this exact frozen material."""
    saved = record[handoff.KEY].get('independent_review', {})
    source = saved.get('author_assignment', assignment)
    return handoff.require_reads(engine, run_id, 'engineer', source['purpose'],
                                 source['instruction'], source['context'], source['schema'])


def require_binding(record):
    saved = record.get(handoff.KEY, {}).get('independent_review')
    if not saved:
        if record.get(handoff.KEY, {}).get(facts.KEY):
            return False
        return not record.get('proposal', {}).get('artifact_review_sha256')
    return (len(saved.get('results', [])) == 3
            and {r.get('role') for r in saved['results']} == {'director', 'engineer', 'scout'}
            and {r.get('role') for r in saved.get('reads', [])} == {'director', 'engineer', 'scout'}
            and record.get('proposal', {}).get('artifact_review_sha256') == sha(saved['results']))


def vote_instruction(engine, run_id, purpose, base, context, schema):
    """Keep already paid vote requests unchanged when correcting the read gate."""
    from .engine import Harness
    legacy = base + '\n批准前必须用 experiments.compare 逐页读完 artifact_evidence_handoff.required_paths 的补充产物摘要，独立核验工程师结论。'
    current = base + ('\n独立核验工程师结论。若本角色已在 artifact_review_results 对应的同一产物快照'
        '完成逐页复核，可复用该角色原始读取回执；宿主会重新核验角色、快照 SHA、路径覆盖和提案绑定。'
        '没有匹配的原始读取时，须用 experiments.compare 逐页读完 required_paths。'
        '复用读取不继承批准：仍须针对当前 decision_proposal 和新补读观察独立投票。')
    for text in (legacy, current):
        for role in ('director', 'engineer', 'scout'):
            key = Harness.decision_key(run_id, role, purpose, text, context, schema)
            if engine.store.get('handoffs', key):
                handoff.assignment_key(engine, run_id, role, purpose, text, context, schema)
                return text
    return current


def vote_reads(engine, run_id, record, role, purpose, instruction, context, schema):
    """Reuse actual same-role artifact reads, never another role's read or vote."""
    vote_key = handoff.assignment_key(engine, run_id, role, purpose, instruction, context, schema)
    saved = record[handoff.KEY]
    review = saved.get('independent_review')
    if not review:
        return handoff.require_reads(engine, run_id, role, purpose, instruction, context, schema)
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
    proof = handoff.require_reads(engine, run_id, role, request['purpose'],
                                 request['instruction'], prior_context, request['schema'])
    original = next(r for r in review['reads'] if r['role'] == role)
    if original.get('handoff_id') != proof['handoff_id']:
        raise ValueError('Retained artifact reads belong to a different reviewer assignment')
    return {**proof, 'vote_handoff_id': vote_key, 'read_origin': 'independent_artifact_review',
            'review_sha256': sha(review['results'])}


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
        validate_reads(merged, proposal, base['summary'])
        review_context = copy.deepcopy(context)
        packet = handoff.packet(current, for_review=True)
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
            '按同一种子与原契约的模型配对，逐项核对公共初始化名称集合、形状、哈希、seed128、words、'
            'start_state、end_state，明确设计允许不同的参数；核对容量、模型与检查点/指标关联。'
            '不要把不同模型家族强行套用同一容量或初始化规则。'
            '逐项比对明细可存 experiments.record_notes，最终 findings 汇总各配对和具体差异。'
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
        review = {'assessment': copy.deepcopy(proposal), 'author_assignment': copy.deepcopy(assignment),
                  'author_read': read, 'assignment': {
                      'purpose': 'experiment_diagnose:' + context['evidence_sha256'] + ':artifact-review-1:' + sha(proposal),
                      'instruction': review_instruction, 'context': review_context, 'schema': review_schema}}
        await checkpoint(**{handoff.KEY: {**current, 'status': 'reviewing_evidence',
            'owner': 'reviewers', 'independent_review': review}}, analysis_reads=merged,
            status='reviewing_evidence')
        engine.store.event(run_id, 'experiment.artifact_review',
            '工程师的产物核验已交三方独立复核；沿用已有快照，不新增训练。',
            {'decision_id': record['id'], 'assessment_sha256': sha(proposal),
             'owners': ['director', 'engineer', 'scout'], 'execution_authorized': False})
    if not review.get('results'):
        request = review['assignment']
        results = await _group(harness, run_id, request['purpose'], request['instruction'],
                               request['context'], request['schema'])
        reads = [handoff.require_reads(engine, run_id, item['role'], request['purpose'],
                 request['instruction'], request['context'], request['schema']) for item in results]
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
        synthesis_context['experiment_decision'][handoff.KEY] = handoff.packet(record[handoff.KEY], for_review=True)
        synthesis_context['artifact_review_results'] = {
            'sha256': sha(review['results']), 'results': review['results'],
            'execution_authorized': False, 'scientific_approval': False}
        synthesis_schema = copy.deepcopy(schema)
        for field, value in (('artifact_evidence_sha256', facts.reference(record[handoff.KEY])['sha256']),
                             ('artifact_review_sha256', sha(review['results']))):
            synthesis_schema['properties'][field] = {'const': value}
            synthesis_schema['required'].append(field)
        if record[handoff.KEY].get(facts.KEY):
            base = engine.store.get('experiment_evidence', record['evidence']['id'])
            synthesis_schema = facts.synthesis_schema(synthesis_schema, base['summary'])
        review = {**review, 'synthesis_assignment': {
            'purpose': 'experiment_decide:' + context['evidence_sha256'] + ':artifact-synthesis-1:' + sha(review['results']),
            'instruction': instruction + '\n三方已完成既有产物独立复核。复用你已保存的核验与读取回执，'
                '根据 artifact_review_results 汇总具体下一步，不重复索取原产物或再次把同一任务交给三方。'
                '各方意见不自动等于架构通过：核对事实、冲突与当前门禁，保留真实未知。'
                '若仍无法提出可执行下一步，使用只读 diagnose 并在 rationale 和 verification 明确'
                '具体缺项、为何阻塞及需由谁补齐。此时保留具体阻塞，不自动再次循环。'
                '新提案仍需三方独立投票，不能以取证完成代替架构、参数或科学验收。',
            'context': synthesis_context, 'schema': synthesis_schema}}
        await checkpoint(**{handoff.KEY: {**record[handoff.KEY], 'independent_review': review}})
    return review['synthesis_assignment']
