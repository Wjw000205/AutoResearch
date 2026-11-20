"""Agent-authored dataset selection and Codex work orders with durable reviews."""
from __future__ import annotations

import asyncio
import copy

from jsonschema import ValidationError

from .prose_schema import validate

from .experiment_workflow import sha
from .experiment_requirements import SEED_GUIDANCE
from .storage import now
from . import independent_full_review as full_review

COLLECTION = 'dataset_agent_plans'
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1600}
TEXTS = {'type': 'array', 'items': TEXT, 'maxItems': 12}


def obj(properties):
    return {'type': 'object', 'additionalProperties': False,
        'required': list(properties), 'properties': properties}


from .dataset_contract import name_schema
TARGET = obj({'note_id': TEXT, 'dataset': name_schema(**TEXT), 'asset_id': TEXT, 'manifest_sha256': TEXT})
SELECTION = obj({'candidates': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': obj({
    'target': TARGET, 'selected': {'type': 'boolean'}, 'task_fit': TEXT,
    'selection_reason': TEXT, 'protocol_basis': TEXT, 'limitations': TEXTS})}, 'coverage_rationale': TEXT})
VOTE = obj({'approve': {'type': 'boolean'}, 'rationale': TEXT, 'issues': TEXTS})
PROTOCOL_VOTE = obj({**VOTE['properties'], 'execution_conditions': TEXTS})


class ProtocolEvidenceNeeded(ValueError):
    """The work-order author needs a Scout evidence task before another draft."""
    def __init__(self, plan_id, message):
        self.plan_id = plan_id
        super().__init__(message)
READ_TOOLS = {'datasets.discover', 'datasets.inspect', 'datasets.download', 'sources.read', 'sources.links',
    'sources.fetch', 'web.search', 'literature.search', 'literature.read_papers', 'literature.protocols'}


def tool_limit(progress):
    # Successful, distinct source work earns a bounded continuation, including
    # discovery/download receipts that have no experiment receipt_sha256.
    tools = READ_TOOLS | {'experiments.compare', 'experiments.inspect_module',
        'literature.record_notes', 'literature.record_protocol', 'literature.link_source'}
    receipts = {sha(row['call']) for row in progress.get('tool_results', [])
        if row.get('call', {}).get('name') in tools and isinstance(row.get('result'), dict)
        and row['result'] and not row['result'].get('error')}
    return min(32, 5 + len(receipts))


def approval(store, plan):
    votes = plan.get('votes', [])
    if (sha(plan['result']) != plan.get('result_sha256') or not full_review.members(votes)
            or not all(v['approve'] and not v['issues'] for v in votes)):
        raise ValueError('Dataset planning approval identity changed')
    if full_review.reviewers(votes) == full_review.REVIEWERS:
        full_review.authenticate_complete(store, plan['run_id'], votes)
        for vote in votes:
            full_review.validate_delivery(store, plan['run_id'], vote)
            frozen=full_review.authenticate(store,plan['run_id'],vote)
            if full_review.source_context(frozen['context'])!=full_review.source_context(plan['review_context']):
                raise ValueError('Dataset review received another frozen plan scope')
            ref=frozen['context'].get('validation_evidence',{})
            archive=store.get('experiment_evidence',ref.get('id','')) or {}
            if (archive.get('run_id')!=plan['run_id'] or sha(archive.get('summary'))!=ref.get('sha256')
                    or archive['summary'].get('proposal')!=plan['result']
                    or frozen['context'].get('dataset_planning',{}).get('scope')!=plan['scope']):
                raise ValueError('Dataset review is not bound to this exact authored result')
        author=store.get('decisions',plan.get('author_handoff_id','')) or {}
        if author.get('role')!='engineer':raise ValueError('New dataset full review requires an independent Engineer author')
    schema = PROTOCOL_VOTE if plan.get('protocol_evidence') else VOTE
    records = [(plan['author_handoff_id'], plan['result'])] + [
        (v.get('handoff_id') or v[full_review.KEY]['handoff_id'], {k: v[k] for k in schema['properties']}) for v in votes]
    for key, result in records:
        actual = store.get('decisions', key) or {}
        if actual.get('run_id') != plan['run_id'] or actual.get('result') != result:
            raise ValueError('Dataset planning approval requires matching saved agent decisions')
    if plan.get('protocol_evidence'):
        bundle = plan['protocol_evidence']
        for vote in votes:
            frozen = store.get('handoff_inputs', vote.get('handoff_id') or vote[full_review.KEY]['handoff_id']) or {}
            ctx = frozen.get('context', {}).get('dataset_planning', {})
            if ctx.get('protocol_evidence') != bundle or ctx.get('reviewed_result_sha256') != plan['result_sha256']:
                raise ValueError('Dataset reviewer did not receive this work order and protocol addendum')

SELECT = '''根据本任务研究问题、方法假设、baseline、评估目标和资源约束选择实验数据集。
先用 experiments.compare 读取 /task_requirements、/method、/requested_targets，再用 datasets.discover 查看本任务论文笔记。
候选不足时检索并阅读相关论文或官方数据说明，保存来源笔记；沿 sources.links/fetch 解析真实下载地址，
调用 datasets.download 保存公开数据，或 datasets.inspect 核对已有资产。可使用内置目录，也可使用工具支持的来源链 URL。
比较数据的任务匹配度、规模、输入输出、标签、划分、许可/可获得性、泄漏风险及资源成本，说明选择与排除原因。
requested_targets 非空时它们是本轮已指定的数据范围：核查依据并全部覆盖，不能用别的数据替换；空时自行决定与任务相符的数据组合。
仅对有实际下载回执且来源有效的资产填写 target；不要编造 URL、资产、版本或论文未报告的参数。
protocol_basis 区分论文设置和需要工程师提出的项目设置；本步不运行训练、不查看留出集指标、不改模型。
候选、判断和引用落盘；只返回紧凑的选型结果，不粘贴论文全文或数据样本。'''

REVIEW = '''独立核验当前 dataset_planning 任务的交付。先用 experiments.compare 读取 /proposal 及相关任务依据。
experiment_review_evidence 保留少量本次已读的关键原文及回执；其余页面按归档引用重读，不能将上下文归档视为方案缺项。
选型时核对数据与研究目标的对应关系、来源资产、覆盖范围、协议依据、公平性及资源可行性；
实现任务单核对数据配置、执行策略、必要接口修改、保留的模型、验收方法和本轮修复依据。
dataset_budget.status=unlimited 时不要求预算方案；有限额度时核对预算与任务的对应关系。不得要求所有指标或所有子集同时改善。
原文或接口缺项按工具定向补读；仅缺少尚待执行的兼容性检查不是拒绝编写配置的理由。
不要重做已完成的想法共识或架构讨论。只给出本版本必要的具体修正，批准不等于执行或科学成功。'''


REVIEW += SEED_GUIDANCE


def _context(engine, run_id, scope, evidence, phase):
    from .experiment_decisions import _archive
    ref = _archive(engine, run_id, evidence)
    return {'training_context_protocol': 1, 'experiment_workflow_protocol': 1,
        'source_tool_protocol': 1, 'literature_notes_protocol': 1, 'dataset_tools_protocol': 1,
        'experiment_protocol_tools': 1, 'research_cycle': engine.cycle(run_id),
        'stage': engine.store.get('runs', run_id)['stage'],
        'validation_evidence': ref, 'evidence_sha256': ref['sha256'],
        'dataset_planning': {'scope': scope, 'phase': phase,
            'read_paths': ['/task_requirements', '/method', '/requested_targets', '/proposal']}}


async def _reviewed(engine, harness, run_id, *, scope, evidence, author, instruction, schema, checker,
                    author_request=None, retained=None, route_protocol_gaps=False):
    from .engine import ResearchBlocked, Harness
    key = run_id + ':dataset-plan:' + sha({'version': 1, 'scope': scope, 'evidence': sha(evidence)})
    state = engine.store.get(COLLECTION, key)
    if state is None:
        state = {'id': key, 'run_id': run_id, 'cycle': engine.cycle(run_id), 'scope': scope,
            'status': 'authoring', 'round': 0, 'stalled': 0, 'created_at': now(),
            'context': _context(engine, run_id, scope, evidence, 'selection' if author == 'scout' else 'work_order')}
        if retained:
            validate(retained['result'], schema)
            state.update(result=copy.deepcopy(retained['result']), result_sha256=retained['result_sha256'],
                         author_handoff_id=retained['author_handoff_id'], source_plan_id=retained['plan_id'],
                         status='reviewing', votes=[], protocol_evidence=evidence['protocol_evidence'])
        engine.store.put(COLLECTION, key, state)
    limit = max(1, engine.store.get('runs', run_id)['config'].get('stagnation_rounds', 3))
    while True:
        engine.check_active(run_id)
        if state['status'] == 'approved':
            approval(engine.store, state)
            await asyncio.to_thread(checker, state['result'])
            return state
        if state.get('waiting_for_protocol_evidence'):
            raise ProtocolEvidenceNeeded(key, state['issues'][0])
        if state['stalled'] >= limit:
            raise ResearchBlocked('数据选型或任务单连续未解决相同问题：' + '; '.join(state.get('issues', []))[:1200])
        try:
            if 'result' in state and not (full_review.members(state.get('votes',[])) and full_review.reviewers(state['votes'])==full_review.LEGACY_REVIEWERS):
                actual=engine.store.get('decisions',state.get('author_handoff_id','')) or {}
                if actual.get('role')!='engineer':
                    from .experiment_decisions import _archive
                    original=copy.deepcopy(state)
                    source=_archive(engine,run_id,{'binding':{'plan_id':key},'proposal':state['result'],
                        'original_author_handoff_id':state['author_handoff_id'],'original_result_sha256':state['result_sha256']})
                    ctx=copy.deepcopy(state['context']);ctx.update(validation_evidence=source,evidence_sha256=source['sha256'])
                    ctx['dataset_planning']['retained_author_candidate']={'source':source,'handoff_id':state['author_handoff_id'],
                        'result_sha256':state['result_sha256'],'instruction':'保留原候选与真实来源，按原要求统一整理为工程师当前交付，再交两位独立全文审核。'}
                    task=instruction+'\n工程师读取retained_author_candidate的原交付，统一整理或按需修订；不得冒充原作者或更改原记录。'
                    purpose='dataset_work_order:engineer-author-v1:'+sha([key,state['round'],state['result_sha256']])
                    result=await harness.ask(run_id,'engineer',purpose,task,ctx,schema)
                    validate(result,schema);await asyncio.to_thread(checker,result)
                    state.setdefault('legacy_author_history',[]).append(original)
                    state.update(result=result,result_sha256=sha(result),author_role='engineer',
                        author_handoff_id=Harness.decision_key(run_id,'engineer',purpose,task,ctx,schema),votes=[])
                    state.pop('review_context',None)
                    engine.store.put(COLLECTION,key,state)
            if 'result' not in state:
                purpose = 'dataset_select:' if author == 'scout' else 'dataset_work_order:'
                purpose += key.rsplit(':', 1)[-1] + ':' + str(state['round'])
                task, context, output_schema = instruction, state['context'], schema
                if author_request:
                    purpose, task, context, output_schema = author_request(
                        engine, state, purpose, task, context, output_schema)
                if state.get('protocol_evidence') and state.get('previous'):
                    from .dataset_protocol_evidence import retained_order
                    previous = retained_order(engine.store, state)
                    context = copy.deepcopy(context)
                    context['dataset_planning'].update(protocol_evidence=state['protocol_evidence'],
                        prior_work_order=previous['result'], operator_transition=evidence.get('operator_transition'))
                    task = ('依据已保留工单、已登记的 protocol_evidence 和本轮 repair_issues，只改具体被否决的实现条款。'
                            '保留数据、预算及其余已成立的约束；项目选择和论文报告值分别注明。返回修订后的工单。')
                    output_schema = copy.deepcopy(schema)
                    for field in ('dataset', 'budget_plan'):
                        output_schema['properties'][field] = {'const': previous['result'][field]}
                dispatch_role='engineer'
                if author=='scout':
                    legacy_id=Harness.decision_key(run_id,'scout',purpose,task,context,output_schema)
                    if engine.store.get('handoffs',legacy_id):dispatch_role='scout'
                result = await harness.ask(run_id, dispatch_role, purpose, task, context, output_schema)
                engine.check_active(run_id)
                validate(result, output_schema)
                validate(result, schema)
                state.update(result=result, result_sha256=sha(result), status='reviewing', votes=[],
                    author_role=dispatch_role,author_handoff_id=Harness.decision_key(run_id, dispatch_role, purpose, task, context, output_schema))
                engine.store.put(COLLECTION, key, state)
                if dispatch_role!='engineer':continue
            await asyncio.to_thread(checker, state['result'])
            if 'review_context' not in state:
                state['review_context'] = _context(engine, run_id, scope,
                    {**evidence, 'proposal': state['result']}, state['context']['dataset_planning']['phase'])
                if state.get('protocol_evidence'):
                    state['review_context']['dataset_planning'].update(protocol_evidence=state['protocol_evidence'],
                        reviewed_result_sha256=state['result_sha256'], operator_transition=evidence.get('operator_transition'))
                engine.store.put(COLLECTION, key, state)
            if not full_review.members(state['votes']):
                purpose = 'dataset_plan_review:' + key.rsplit(':',1)[-1] + ':' + str(state['round'])
                review, vote_schema = REVIEW, VOTE
                if state.get('protocol_evidence'):
                    vote_schema = PROTOCOL_VOTE
                    review += ('\n本次审原工单与随附 protocol_evidence 的组合。以已登记证据更新原文中的旧待办。'
                        '区分论文报告设置与已标明的项目选择；未报告的列名或指标尺度不要求冒充论文事实。'
                        'operator_transition 是已发生的队列转场及其限定。只判断当前实现任务能否下发。'
                        '执行前仍需做的测试、资源核验及科研限制列 execution_conditions。'
                        '只有必须修改本工单才能开始实现的具体问题列 issues，注明位置、原因、改法。')
                if state['votes']:
                    state.setdefault('review_history', []).append(copy.deepcopy(state['votes']))
                state['votes'] = await full_review.group(harness, run_id, purpose, review, state['review_context'], vote_schema)
                engine.check_active(run_id)
                engine.store.put(COLLECTION, key, state)
            issues = [issue for vote in state['votes'] if not vote['approve'] or vote['issues']
                for issue in (vote['issues'] or [vote['rationale']])]
            if issues:
                raise ValueError('; '.join(issues)[:2400])
        except (ValueError, ValidationError) as exc:
            from .experiment_decisions import _archive
            from .dataset_codex import delivery_issue
            message = delivery_issue(exc)
            if ('result' not in state
                    and state.get('identity_correction', {}).get('round') == state['round']):
                raise ResearchBlocked('数据集名称限定补正未通过；保留原任务单及答复：' + message) from exc
            previous = _archive(engine, run_id, {'binding': {'plan_id': key}, 'plan': state, 'issues': [message]})
            # Wording/proposal changes are not progress on the same failed gate.
            failure = ({'path': list(exc.absolute_path), 'rule': exc.validator,
                'constraint': exc.validator_value} if isinstance(exc, ValidationError) else message)
            signature = sha({'issues': failure})
            stalled = state['stalled'] + 1 if signature == state.get('failure_signature') else 1
            state = {k: v for k,v in state.items() if k not in {'result','result_sha256','votes','review_context'}}
            state.update(status='authoring', round=state['round'] + 1, stalled=stalled,
                failure_signature=signature, issues=[message], previous=previous)
            state['context'] = _context(engine, run_id, scope,
                {**evidence, 'previous_plan': previous, 'repair_issues': [message]},
                'selection' if author == 'scout' else 'work_order')
            state['context']['dataset_planning']['repair_issues'] = [message]
            engine.check_active(run_id)
            if route_protocol_gaps:
                from .dataset_protocol_evidence import needs_evidence
                if needs_evidence([message]):
                    state['waiting_for_protocol_evidence'] = True
                    engine.store.put(COLLECTION, key, state)
                    raise ProtocolEvidenceNeeded(key, message) from exc
            engine.store.put(COLLECTION, key, state)
            continue
        approval(engine.store, state)
        state.update(status='approved', updated_at=now())
        engine.check_active(run_id)
        engine.store.put(COLLECTION, key, state)
        engine.store.event(run_id, 'experiment.agent_plan_approved', '数据选型或实现任务单已完成三方核验。',
            {'plan_id': key, 'scope': scope, 'result_sha256': state['result_sha256']})


def selection_check(engine, run_id, result, targets):
    from .dataset_targets import bind
    selected = [row for row in result['candidates'] if row['selected']]
    if not selected or len({row['target']['dataset'].casefold() for row in result['candidates']}) != len(result['candidates']):
        raise ValueError('Select at least one dataset and compare each dataset only once')
    for row in result['candidates']:
        bind(engine, run_id, row['target'])
    if targets and {t['dataset'] for t in targets} != {row['target']['dataset'] for row in selected}:
        raise ValueError('Selection must preserve every explicitly requested dataset')
    for target in targets:
        row = next(c['target'] for c in selected if c['target']['dataset'] == target['dataset'])
        if row['note_id'] != target['note_id']:
            raise ValueError('Selection changed the requested source note')
        if 'asset_id' in target and row != target:
            raise ValueError('Selection changed the requested dataset asset')


async def select(engine, harness, run_id, *, scope, method, targets=()):
    run = engine.store.get('runs', run_id)
    from .experiment_attempt_policy import current as attempt_policy
    policy = attempt_policy(engine.store, run_id)
    evidence = {'binding': {'scope': scope, 'cycle': engine.cycle(run_id)},
        'task_requirements': run.get('objective', ''), 'method': method,
        'requested_targets': list(targets),
        'execution_constraints': {k:v for k,v in run['config'].items() if k in {
            'max_trials','experiment_timeout','ram_limit_gb','disk_limit_gb','cpu_limit_cores','vram_limit_gb'}}}
    if policy:
        evidence['execution_constraints'].update(max_trials=None, attempt_policy=policy['body'])
    return await _reviewed(engine, harness, run_id, scope=scope, evidence=evidence,
        author='scout', instruction=SELECT, schema=SELECTION,
        checker=lambda result: selection_check(engine, run_id, result, targets))


def selected(plan):
    return [copy.deepcopy(row) for row in plan['result']['candidates'] if row['selected']]


def check_design(result, plan, *, proposal=None):
    chosen = {row['target']['dataset']: row['target'] for row in selected(plan)}
    tasks = result.get('benchmark_tasks', [])
    actual = {t.get('dataset') for t in tasks}
    if set(chosen) != actual:
        missing = sorted(chosen.keys() - actual, key=repr)
        unexpected = sorted(actual - chosen.keys(), key=repr)
        raise ValueError('Design must cover the agent-approved dataset selection: '
            f'missing={missing!r}; unexpected={unexpected!r}. '
            'Dataset metadata must be an exact identity; put protocol status in limitations.')
    for task in tasks:
        target = chosen[task['dataset']]
        expected = [{'asset_id': target['asset_id'], 'manifest_sha256': target['manifest_sha256']}]
        for field in ('baseline_params', 'proposed_params', 'ablation_params'):
            if task.get(field, {}).get('dataset_assets') != expected:
                raise ValueError('Design must preserve selected dataset asset identities for every method')


def view(store, run_id, setup_id):
    from .run_views import _rows
    setup = store.get('dataset_experiment_setups', setup_id, {})
    if setup.get('direct_repair'):
        return []  # Historical planning remains stored; native delivery is current.
    bound = (setup.get('dataset_binding') or {}).get('kind') == 'queued_dataset_asset'
    where = "substr(json_extract(data,'$.scope'),1,?)=?"
    args = (len(setup_id) + 1, setup_id + ':')
    if bound:
        # Prior selection attempts remain audit records, not current work.
        where += " AND json_extract(data,'$.scope')<>?"
        args += (setup_id + ':selection',)
    return _rows(store, COLLECTION, ['id','scope','status','round','result_sha256','author_handoff_id',
        'result.dataset','result.implementation_brief','result.coverage_rationale','votes','issues'],
        run_id=run_id, where=where, args=args, limit=2)
