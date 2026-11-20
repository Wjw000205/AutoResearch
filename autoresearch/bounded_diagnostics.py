"""Executable successors for independently reviewed new diagnostic protocols.

Unlike instrumentation checks, these are explicit new observation batches. They
never activate a model, alter a frozen experiment, or certify parameter stability.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .prose_schema import validate

from .experiment_workflow import sha
from .storage import now
from .training_adjustment import obj

COLLECTION = 'bounded_diagnostics'
ROLES = ('director', 'engineer', 'scout')
from . import independent_full_review as full
TERMINAL = {'completed', 'execution_failed', 'implementation_rejected', 'implementation_blocked'}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 2000}
from .bounded_diagnostic_contract import CAPS, VERSION, capabilities, host_files, reject_stub
AUTHOR = obj({'seed': {'type': 'integer', 'minimum': 0},
    'max_updates': {'type': 'integer', 'minimum': 0, 'maximum': 512},
    'limits': obj({k: {'type': 'integer', 'minimum': 1, 'maximum': v} for k, v in CAPS.items()}),
    'input_paths': {'type': 'array', 'minItems': 1, 'maxItems': 16, 'uniqueItems': True,
                    'items': {'type': 'string'}},
    'files': {'type': 'array', 'minItems': 1, 'maxItems': 6, 'items': obj({
        'path': {'type': 'string', 'pattern': '^bounded_diagnostic(?:_[a-z0-9]+)?\\.py$'},
        'content': {'type': 'string', 'minLength': 1, 'maxLength': 40000}})},
    'rationale': TEXT})
DELIVERY = {'anyOf': [AUTHOR, obj({'delivery_status': {'const': 'blocked'},
    'blockers': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': TEXT},
    'required_capability': TEXT, 'execution_authorized': {'const': False}})]}
WRITE = '''落实 bounded_diagnostic.next_task 为一个独立、单批、可执行的新诊断；不是重复旧训练或插桩检查。
用 experiments.inspect_module 读取已冻结运行源文件，必要时 compare 读原证据。返回完整诊断实现文件，
不得修改模型/运行源文件。主模块 bounded_diagnostic.py 提供 run(protocol, params) 函数；没有顶层执行。
protocol 包含 next_task、seed、max_updates、limits、inputs。inputs 每项给出真实相对 path、sha256 和原 params。
从宿主 input_catalog 选择确实需要的原始输入；只能使用一个固定模型种子，不访问 test。
需续接时严格使用原 checkpoint/Adam/RNG/后续批次，不重放已完成更新；若状态字段无法支持则明确失败，不能假定存在。
返回 {summary:对象, measurements:对象, metrics:至少一个实测有限数值指标, optimizer_steps:实际总更新数}。
summary 必须含预声明判别规则逐项测量、竞争解释及局限；未知保持未知。不得伪造测量、历史根因、稳定性或通过结果。
资源不得超过 next_task 草案和宿主 caps，max_updates 为所有分支总和。只用已有依赖和原验证数据，无网络、安装、子进程或 GPU。
入口experiment.py、bounded_host.py和diagnostic_guard.py由宿主生成，已在当前author源码快照中给出；
作者无需改动这些文件。不要把历史adapter入口当作本次诊断入口，或把上一失败交付的1秒/零步占位值当成合法预算。
本次数值上限必须与已审next_task及caps同时相容；不相容时不得擅自改写已审方案或调高资源。
无法实现时返回delivery_status=blocked、blockers、required_capability、execution_authorized=false，
不要提交只抛异常的文件、空指标或零步伪装完成。阻塞交付会留在当前阶段，不反复送三方审空实现。
协议及所有代码随后由三方独立完整读取审核；本次交付不授权执行。无需输出或改写原记录。'''
REVIEW = '''独立审核新诊断的完整候选实现和 /controlled_protocol、/controlled_admission。
必须通过 experiments.inspect_module(snapshot=candidate,view=source) 分页读完每个 changed_ranges，返回真实 read_receipts。
核对 next_task 与实际实现逐项相符：输入、单种子、续接位置、唯一干预、控制、测量、固定判别规则和数值资源上限。
拒绝旧训练/诊断重放、隐式多种子、test 读取、模型激活、原记录改写、伪造数据，或把 CPU 断言冒充诊断。
批准只授权这一份 SHA 绑定代码的一次资源受限诊断，不是选参、根因或稳定性批准。
有具体代码问题则 approve=false 并给出可实现的修正要求；允许诊断结论 inconclusive。'''
RESULT = obj({'evidence_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
    'valid_observation': {'type': 'boolean'}, 'finding': {'enum': ['supported_locally', 'refuted_locally', 'inconclusive']},
    'rationale': TEXT, 'limitations': TEXT, 'next_step': TEXT,
    'issues': {'type': 'array', 'maxItems': 6, 'items': TEXT},
    'read_receipts': {'type': 'array', 'minItems': 1, 'maxItems': 256, 'uniqueItems': True,
                      'items': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}}})
RESULT_READS = ('/bounded_protocol', '/bounded_result')
RESULT_INSTRUCTION = '''独立复核本次新诊断。使用 experiments.compare 分页完整读取 /bounded_protocol 与 /bounded_result，
read_receipts 只列这两条路径全部真实回执。按原预声明规则判断局部 supported_locally/refuted_locally/inconclusive；
不把局部改善作为历史根因或稳定性，不要求阳性结果，不重跑已完成任务。区分实测结果与解释，
明确有效性、局限、具体异议及下一步；三方意见一起返回正常参数决策。'''


def current(engine, run_id):
    from .tuning_progress_recovery import excluded
    from .bounded_review_reading import excluded as superseded
    return next((r for r in engine.store.list(COLLECTION, run_id) if r.get('cycle') == engine.cycle(run_id) and r['status'] not in TERMINAL
                 and not excluded(engine, run_id, diagnostic_id=r['id']) and not superseded(engine.store,r)), None)


def evidence(engine, run_id):
    rows = [r for r in engine.store.list(COLLECTION, run_id) if r.get('cycle') == engine.cycle(run_id) and r['status'] in TERMINAL]
    for row in rows:
        verify_evidence(engine, row)
    return [{k: copy.deepcopy(r.get(k)) for k in ('id', 'status', 'plan_sha256', 'protocol', 'outcome', 'analyses', 'failure')}
            for r in rows]


def verify_evidence(engine, record):
    from .parameter_resolution import owner
    source_task(engine, record)
    if record['status'] in {'implementation_rejected', 'implementation_blocked'}:
        for round_ in record['rounds']:
            owner(engine, record['run_id'], 'engineer', round_['author'])
            for role, request in round_.get('votes', {}).items():
                owner(engine, record['run_id'], role, request.get('read_correction') or request)
        return
    result = engine.store.get('experiments', record['run_id'] + ':' + record['trial_id']) or {}
    receipt = record.get('outcome') or record.get('failure') or {}
    if sha(result) != receipt.get('receipt_sha256'):
        raise ValueError('Diagnostic execution receipt changed')
    if record['status'] == 'completed':
        from .experiment_views import _safe_file
        from .fixed_state_diagnostic import validate_reads
        path = _safe_file(engine, record['run_id'], result, 'artifacts/bounded_diagnostic.json')
        if (path is None or hashlib.sha256(path.read_bytes()).hexdigest() != receipt['report_sha256']
                or json.loads(path.read_bytes()) != receipt['report']):
            raise ValueError('Diagnostic measured report changed')
        requests = record['result_requests']
        if not full.assignment_members({r: (a.get('read_correction') or a) for r,a in requests.items()}):
            raise ValueError('Diagnostic result review is incomplete')
        analyses = []
        for role, request in requests.items():
            selected = request.get('read_correction') or request
            validate_reads(engine, record, role, selected, RESULT_READS)
            analyses.append(selected.get('full_review_receipt') or {'role': role, **selected['answer']})
        if record['analyses'] != analyses:
            raise ValueError('Diagnostic independent result interpretations changed')


def source_task(engine, record):
    from .parameter_resolution import COLLECTION as TASKS, verified
    task = engine.store.get(TASKS, record['source_task_id']) or {}
    if task.get('status') != 'accepted':
        raise ValueError('Diagnostic source plan is not independently accepted')
    source, plan = verified(engine, record['run_id'], task)
    if (task['plan_sha256'] != record['plan_sha256'] or plan['next_task'] != record['next_task']
            or source['binding'] != record['binding']):
        raise ValueError('Diagnostic is detached from its reviewed source plan')


def register(engine, run_id):
    from .parameter_resolution import COLLECTION as TASKS, verified, basis
    from .experiment_decisions import snapshot, packet
    from .bounded_diagnostic_inputs import author_catalog, prepare
    for task in engine.store.list(TASKS, run_id):
        source_archive = engine.store.get('experiment_evidence', task['evidence']['id']) or {}
        if source_archive.get('summary', {}).get('binding', {}).get('cycle') != engine.cycle(run_id):
            continue
        key = task['id'] + ':diagnostic'
        if task['status'] != 'accepted':
            continue
        previous_delivery = engine.store.get(COLLECTION, key)
        predecessors = set()
        if previous_delivery:
            link = task.get('diagnostic_successor') or {}
            config_key = key + ':continuation-config-v1'
            source_key = key + ':continuation-source-v2'
            resource_key = key + ':resource-evidence-v3'
            deadline_key = key + ':total-deadline-v4'
            cpu_key = key + ':shared-cpu-v5'
            if link.get('diagnostic_id') in (config_key, source_key, resource_key, deadline_key, cpu_key):
                linked = engine.store.get(COLLECTION, link['diagnostic_id']) or {}
                parent = linked.get('previous_delivery', {})
                for ancestor in (deadline_key, resource_key, source_key, config_key):
                    if parent.get('id') != ancestor:
                        continue
                    intermediate = engine.store.get(COLLECTION, ancestor) or {}
                    if parent['sha256'] != sha(intermediate):
                        raise ValueError('Continuation repair lost its verified predecessor')
                    predecessors.add(ancestor)
                    parent = intermediate.get('previous_delivery', {})
                if parent != {'id': key, 'sha256': sha(previous_delivery)}:
                    raise ValueError('Continuation delivery lost its original source')
                predecessors.add(key)
                previous_delivery = linked
            blockers = ' '.join((previous_delivery.get('failure') or {}).get('blockers', []))
            resource_gap = (link.get('diagnostic_id') == source_key and '资源' in blockers
                            and 'runner' in blockers and '120秒' in previous_delivery['next_task']['resource_limits']
                            and '1680秒' in previous_delivery['next_task']['resource_limits'])
            deadline_gap = (link.get('diagnostic_id') == resource_key and previous_delivery.get('resource_evidence')
                            and 'process.wait' in blockers and 'store.put' in blockers)
            cpu_gap = (link.get('diagnostic_id') == deadline_key and previous_delivery.get('resource_evidence')
                       and '累计CPU' in blockers and '_seal' in blockers)
            terminal_gap = (link.get('diagnostic_id') == cpu_key and previous_delivery.get('resource_evidence')
                            and 'diagnostic_wall_confirmations' in blockers and '/issues' in blockers)
            if (previous_delivery.get('status') != 'implementation_blocked'
                    or link.get('diagnostic_id') != previous_delivery['id'] or link.get('status') != 'implementation_blocked'
                    or previous_delivery.get('trial_id') or previous_delivery.get('outcome')
                    or not (terminal_gap or cpu_gap or deadline_gap or resource_gap or not previous_delivery.get('continuation_author_material') and (
                            'config_path' in blockers or previous_delivery.get('continuation_inputs')
                            and 'diagnostic_probe.py' in blockers and 'diagnostic_persistence.py' in blockers))):
                continue
            # A separately addressed author handoff preserves the original
            # blocked answer and prevents repeated dispatch for the same repair.
            predecessors.add(previous_delivery['id'])
            key += (':terminal-commit-v6' if terminal_gap else ':shared-cpu-v5' if cpu_gap else ':total-deadline-v4' if deadline_gap
                    else ':resource-evidence-v3' if resource_gap else ':continuation-source-v2')
            if engine.store.get(COLLECTION, key):
                continue
        from .tuning_progress_recovery import excluded
        if excluded(engine, run_id, task_id=task['id']):
            continue
        source, plan = verified(engine, run_id, task)
        if plan['next_task']['kind'] != 'diagnose':
            continue
        prior_id = task.get('successor', {}).get('decision_id')
        if prior_id:
            from .experiment_decisions import _approved
            prior = engine.store.get('experiment_decisions', prior_id) or {}
            if _approved(prior):
                continue
        if basis(snapshot(engine, run_id)) != task['basis_sha256']:
            continue  # New measurements require a fresh plan, not this old protocol.
        files, catalog, originals = prepare(engine, run_id, source)
        from .diagnostic_continuation_inputs import bind
        continuation = bind(engine, run_id, plan['next_task'], catalog, files, originals)
        if previous_delivery:
            if not continuation:
                continue
            verify_evidence(engine, previous_delivery)
            answer = previous_delivery['rounds'][-1]['author']['answer']
            if (answer.get('delivery_status') != 'blocked' or answer.get('execution_authorized') is not False
                    or answer.get('blockers') != previous_delivery['failure']['blockers']
                    or answer.get('required_capability') != previous_delivery['failure'].get('required_capability')):
                raise ValueError('Blocked diagnostic handoff differs from its original answer')
            if not any(row['path'] == continuation['checkpoint_path'] and row['sha256'] == continuation['checkpoint']['sha256']
                       for row in previous_delivery['input_catalog']):
                raise ValueError('Previous delivery was not bound to the same continuation')
        # A new prose hash is not new evidence or a repaired execution channel.
        for previous in engine.store.list(COLLECTION, run_id):
            if previous['id'] in predecessors:
                continue
            if (previous.get('status') in {'implementation_rejected', 'implementation_blocked'}
                    and previous.get('host_capabilities', {}).get('sha256') == capabilities()['sha256']):
                prior_task = engine.store.get(TASKS, previous['source_task_id']) or {}
                if prior_task.get('basis_sha256') == task['basis_sha256']:
                    from .engine import ResearchBlocked
                    raise ResearchBlocked('相同实测依据和宿主能力下已有诊断实现阻塞；需要落实具体工程修复或新实测依据，不能仅更换任务措辞重新派发。')
        files.update(host_files())
        context = packet(source, task['evidence'])
        overview, by_trial = author_catalog(catalog, plan['next_task'])
        proof = None
        program = engine.store.get('programs', run_id)
        if continuation and all(s in plan['next_task']['resource_limits'] for s in ('1680秒', '120秒')):
            from .diagnostic_resource_evidence import capture
            proof = capture(engine, run_id, {'plan_sha256': task['plan_sha256'], 'binding':source['binding'], 'base_program':program})
            if previous_delivery and (terminal_gap or deadline_gap or cpu_gap) and not (proof.get('closeout_checks') and proof.get('offline_checks')):
                from .engine import ResearchBlocked
                raise ResearchBlocked('宿主总截止修复需要当前源码对应的资源及收尾故障探针回执。')
            files['host_evidence.json'] = json.dumps(proof, ensure_ascii=False, indent=2).encode()
            overview['resource_enforcement'] = {'path':'/bounded_resource_enforcement','source_file':'host_evidence.json',
                'sha256':proof['sha256'],'review_ceiling':proof['review_ceiling'],'compute_seconds':proof['compute_seconds']}
        if continuation:
            overview['continuation_inputs'] = {k: v for k, v in continuation.items() if k != 'config_text'}
            overview['continuation_inputs'].update(config_text_path='/continuation_inputs/config_text',
                instructions='input_paths 必须选择 checkpoint_path 和 config_path 两个精确路径。'
                    '用 experiments.compare 分页读取配置正文，用 inspect_module 读取本次已绑定的生产者源码。'
                    '续接文件由 diagnostic_persistence.snapshot 生成，按其 input、protocol 和哈希核验配置；'
                    '原 best.pt 的 state.config 检查属于较早的输入格式。运行时仍核验游标、分支、剩余批次和资产。')
        context['experiment_decision']['bounded_diagnostic'] = {
            'task_id': task['id'], 'plan_sha256': task['plan_sha256'], 'next_task': plan['next_task'],
            **overview,
            'caps': CAPS, 'host_capabilities': capabilities(),
            'scope': 'New diagnostic implementation, no execution approval'}
        # Author must be able to read prior checked utilities as well as the model.
        from .rapid_experiment_revision import _write_files
        from .experiment_analysis import freeze_sources
        from .experiment_decisions import _archive
        bundle = _write_files(engine.run_dir(run_id) / 'bounded-diagnostics' / sha([key, proof['sha256']] if proof else key) / 'source', files)
        code = files['experiment.py'].decode('utf-8')
        sources = freeze_sources(engine, run_id, {'code': code}, bundle)
        author_evidence = {**source, 'code_sources': sources, 'bounded_input_catalog': catalog}
        if proof:
            author_evidence['bounded_resource_enforcement'] = proof
        if continuation:
            author_evidence['continuation_inputs'] = continuation
        if by_trial:
            author_evidence['bounded_input_index'] = by_trial
        ref = _archive(engine, run_id, author_evidence)
        context.update(validation_evidence=ref, evidence_sha256=ref['sha256'])
        context['experiment_decision']['code_sources'] = sources
        record = {'id': key, 'run_id': run_id, 'cycle': engine.cycle(run_id), 'status': 'implementing',
            'created_at': now(), 'source_task_id': task['id'], 'plan_sha256': task['plan_sha256'],
            'next_task': copy.deepcopy(plan['next_task']), 'original_evidence': task['evidence'],
            'binding': source['binding'], 'base_program': program,
            'source_bundle': bundle, 'input_catalog': catalog, 'original_files': originals,
            'rounds': [], 'author_context': context, 'host_contract_version': VERSION,
            'host_capabilities': capabilities(), 'scientific_approval': False}
        if continuation:
            record['continuation_inputs'] = overview['continuation_inputs']
            from .diagnostic_author_material import material
            record['continuation_author_material'] = material(engine, run_id, continuation, catalog, files)
            if proof:
                record['resource_evidence'] = proof
                if proof.get('offline_checks'):
                    record['test_evidence'] = proof['offline_checks']
                if previous_delivery and previous_delivery.get('continuation_author_material'):
                    rejected = [rd for rd in previous_delivery['rounds'] if rd.get('build') and rd.get('votes')]
                    if rejected:
                        rd = rejected[-1]
                        record['prior_candidate_for_repair'] = {'files': rd['author']['answer']['files'],
                            'issues': {role:(v.get('read_correction') or v)['answer']['issues'] for role,v in rd['votes'].items()},
                            'scope': 'Rejected candidate for correction only. No inherited approvals.'}
                        if deadline_gap or cpu_gap or terminal_gap:
                            # The dissent concerns host closeout. Rebuild from the
                            # authenticated original module bytes, with fresh reviews.
                            record['rounds'] = [{'author': copy.deepcopy(rd['author'])}]
                        if cpu_gap or terminal_gap:
                            from .diagnostic_review_issues import collect, journal_receipt
                            record['prior_review_items'] = collect(engine, run_id, predecessors)
                            record['candidate_io_checks'] = journal_receipt(engine.store, previous_delivery)
        if previous_delivery:
            record['previous_delivery'] = {'id': previous_delivery['id'], 'sha256': sha(previous_delivery)}
        engine.check_active(run_id)
        engine.store.put(COLLECTION, key, record)
        return record


def build(engine, record, proposal):
    validate(proposal, AUTHOR)
    reject_stub(proposal['files'])
    from .bounded_diagnostic_inputs import selected
    from .rapid_experiment_revision import _write_files
    from .experiment_analysis import freeze_sources
    from .controlled_training_checks import admission
    files = {r['path']: (Path(record['source_bundle']['root']) / r['path']).read_bytes()
             for r in record['source_bundle']['files']}
    protected = {r['path']: r['sha256'] for r in record['source_bundle']['files'] if r['path'] != 'experiment.py'}
    extra, inputs, assets = selected(engine, record, proposal)
    files.update(extra)
    protected.update({p: hashlib.sha256(b).hexdigest() for p, b in extra.items()})
    names = [r['path'] for r in proposal['files']]
    if len(set(names)) != len(names) or 'bounded_diagnostic.py' not in names or any(n in files for n in names):
        raise ValueError('Diagnostic modules must be unique new files with a bounded_diagnostic entry')
    for row in proposal['files']:
        compile(row['content'], row['path'], 'exec')
        files[row['path']] = row['content'].encode('utf-8')
    protocol = {k: copy.deepcopy(proposal[k]) for k in ('seed', 'max_updates', 'limits')}
    protocol.update(next_task=record['next_task'], inputs=inputs, plan_sha256=record['plan_sha256'])
    if record.get('host_contract_version', 0) >= VERSION:
        protocol.update(host_contract_version=VERSION, required_updates=proposal['max_updates'])
    files['bounded_protocol.json'] = json.dumps(protocol, ensure_ascii=False).encode('utf-8')
    files['bounded_inputs.json'] = json.dumps(protected).encode('utf-8')
    files.update(host_files())
    # Host guard may supersede an older immutable diagnostic utility only in this new bundle.
    for path in ('bounded_host.py', 'diagnostic_guard.py'):
        protected[path] = hashlib.sha256(files[path]).hexdigest()
    files['bounded_inputs.json'] = json.dumps(protected).encode('utf-8')
    code = 'from bounded_host import main\nmain()\n'
    files['experiment.py'] = code.encode()
    proof = record.get('resource_evidence')
    if proof:
        from .diagnostic_resource_evidence import effective, verify
        verify(proof)
        ceiling = proof['review_ceiling']
        protocol['reviewed_limits'] = effective({'approved':True,'limits':ceiling}, protocol['limits'], ceiling)
        files['bounded_protocol.json'] = json.dumps(protocol, ensure_ascii=False).encode()
        protected['bounded_protocol.json'] = hashlib.sha256(files['bounded_protocol.json']).hexdigest()
        files['host_evidence.json'] = json.dumps(proof, ensure_ascii=False, indent=2).encode()
        protected['host_evidence.json'] = hashlib.sha256(files['host_evidence.json']).hexdigest()
        files['bounded_inputs.json'] = json.dumps(protected).encode()
    identity = sha({'plan_sha256': record['plan_sha256'], 'proposal': proposal,
                    'sources': {p: hashlib.sha256(v).hexdigest() for p, v in files.items()}})
    bundle = _write_files(engine.run_dir(record['run_id']) / 'bounded-diagnostics' / identity, files)
    sources = freeze_sources(engine, record['run_id'], {'code': code}, bundle)
    changed = [*names, 'experiment.py', 'bounded_host.py', 'bounded_protocol.json', 'bounded_inputs.json', 'diagnostic_guard.py']
    if proof:
        changed.append('host_evidence.json')
    source_manifest = {r['path']:r['sha256'] for r in record['source_bundle']['files']}
    return {'code': code, 'protocol': protocol, 'dataset_assets': assets,
        'admission': admission(engine, record['run_id']),
        'candidate': {'bundle': bundle, 'code_sources': sources, 'amendment_sha256': identity,
            'checks_sha256': hashlib.sha256(code.encode()).hexdigest(),
            **({'file_ownership': {p:{'owner':'engineer' if p in names else 'host',
                 'source_sha256':source_manifest.get(p), 'candidate_sha256':hashlib.sha256(files[p]).hexdigest()}
                 for p in changed}} if proof else {}),
            'changed_ranges': [{'path': p, 'start': 0, 'end': len(files[p].decode('utf-8')),
                                'sha256': hashlib.sha256(files[p]).hexdigest()} for p in changed]}}


async def execute_once(engine, record, save):
    from .engine import ResearchBlocked
    from .controlled_training_checks import unchanged, admission
    from .rapid_experiment_revision import _budget, _reviewed
    from .training_adjustment import _basis
    from .lab import assess_resources
    from .experiment_views import _safe_file
    if record.get('bounded_evidence_review'):
        from .bounded_evidence_review import verify
        votes, reads = verify(engine, record)
        if votes != record.get('votes') or reads != record.get('review_reads'):
            raise ResearchBlocked('Diagnostic packet reviews changed before dispatch')
    source_votes=record.get('bounded_evidence_review',{}).get('full_reviews') or record.get('votes',[])
    if not _reviewed(record) or len(record.get('review_reads', [])) != len(full.reviewers(source_votes)):
        raise ResearchBlocked('新诊断缺少三方代码批准或完整读取。')
    await engine._read_consensus_check(record['run_id'], source_task, engine, record)
    frozen_protocol = Path(record['candidate']['bundle']['root']) / 'bounded_protocol.json'
    if json.loads(frozen_protocol.read_bytes()) != record['protocol']:
        raise ValueError('Diagnostic protocol differs from independently reviewed code bundle')
    rid = record['run_id']; identifier = 'bounded-check-' + record['candidate']['amendment_sha256'][:24]
    params = {'phase': 'code_check', 'seed': record['protocol']['seed'],
              'bounded_protocol_sha256': sha(record['protocol'])}
    if record['dataset_assets']:
        params['dataset_assets'] = record['dataset_assets']
    result = engine.store.get('experiments', rid + ':' + identifier)
    supervised = record.get('resource_evidence', {}).get('supervised_closeout') == 1
    if supervised and (record.get('trial_id') or result):
        sealed = engine.store.get('diagnostic_wall_confirmations', rid + ':' + identifier) or {}
        if not result or not sealed.get('worker_exited') or sealed.get('receipt_sha256') != sha(result):
            raise ResearchBlocked('诊断收尾缺少总截止内的宿主确认；保留最后现场，禁止自动重跑。')
        if sealed.get('terminal_commit_protocol') == 2:
            if not sealed.get('final_checks_passed') or sealed.get('status') != 'confirmed':
                raise ResearchBlocked('诊断终态缺少原子监督确认，保留暂存记录。')
        elif (record['resource_evidence'].get('shared_cpu_job') and
                (record.get('runner_timing', {}).get('receipt_sha256') != sha(result)
                 or not 0 <= record['runner_timing'].get('total_job_cpu_seconds', -1) < sealed['cpu_limit_seconds'])):
            raise ResearchBlocked('缺少监督器核实的最终共享CPU计量；保留现场，禁止重新执行。')
        if not record.get('runner_timing', {}).get('worker_exited'):
            record['runner_timing'] = sealed
    if not result:
        if record.get('trial_id'):
            raise ResearchBlocked('新诊断已登记派发但缺回执；须核对原进程，禁止重复派发。')
        await engine._read_consensus_check(rid, unchanged, engine, record)
        await engine._read_consensus_check(rid, engine._execution_gate, rid)
        resource = _basis(engine, rid)['design']['resource_plan']
        required = sorted(set(resource.get('required_packages', []) + record['base_program'].get('dependencies', [])))
        assessed = assess_resources({**resource, 'required_packages': required}, await engine.execution_hardware(rid))
        if not assessed['approved']:
            raise ResearchBlocked('新诊断未通过原资源准入。')
        limits = {**assessed['limits'], 'gpu_allowed': False, 'gpu_memory_mb': 0, 'required_packages': required,
            'execution_mode': engine.store.get('runs', rid)['config'].get('execution_mode', 'local'),
            'code_bundle': record['candidate']['bundle'], 'experiment_binding': {
                'research_cycle': record['cycle'], 'code_revision': record['binding']['revision'],
                'code_sha256': record['candidate']['checks_sha256'], 'experiment_plan_sha256': record['binding']['plan_sha256']}}
        for k, cap in record['protocol']['limits'].items():
            limits[k] = min(limits.get(k) or cap, cap)
        if record.get('resource_evidence'):
            from .diagnostic_resource_evidence import effective, verify
            proof = record['resource_evidence']
            verify(proof)
            if (proof['plan_sha256'] != record['plan_sha256'] or proof['binding'] != record['binding']
                    or limits['execution_mode'] != 'local'
                    or hashlib.sha256((frozen_protocol.parent / 'host_evidence.json').read_bytes()).hexdigest()
                    != hashlib.sha256(json.dumps(proof, ensure_ascii=False, indent=2).encode()).hexdigest()):
                raise ResearchBlocked('Reviewed host resource evidence differs from the admitted candidate')
            limits.update(effective(assessed, record['protocol']['limits'], record['protocol']['reviewed_limits']))
        if admission(engine, rid) != record['admission']:
            raise ResearchBlocked('新诊断额度台账在独立审核后变化，须重新审核。')
        _budget(engine, rid, 1)
        engine.check_active(rid)
        save(status='executing', trial_id=identifier, effective_limits=limits, dispatch_at=now(),
             **({'runner_timing':{'status':'incomplete','retry_authorized':False}} if supervised else {}))
        if supervised:
            from .diagnostic_supervisor import supervise, Incomplete, PublicationUncertain
            try:
                result, timing = await supervise(engine.store.path, engine.runner.data_dir, rid, record['code'],
                    params, identifier, limits, engine.cancellations[rid])
            except Incomplete as exc:
                raise ResearchBlocked('诊断收尾未完成，原现场保留，禁止自动重试：' + str(exc)) from exc
            except PublicationUncertain as exc:
                from .diagnostic_supervisor import committed_publication
                try:
                    result,timing = await engine._read_consensus_check(
                        rid, committed_publication, engine.store, rid, identifier)
                except PublicationUncertain:
                    raise ResearchBlocked('诊断终态事务未确认，保留暂存现场，禁止重跑：' + str(exc)) from exc
            # The terminal receipt is already sealed durably. Carry the
            # end-to-end measurement into the next ordinary workflow checkpoint.
            record['runner_timing'] = timing
        else:
            result = await engine.runner.run(rid, record['code'], params, identifier, limits, engine.cancellations[rid])
    if (result.get('status') != 'completed' or result.get('code_sha256') != record['candidate']['checks_sha256']
            or result.get('params') != params or result.get('limits', {}).get('code_bundle') != record['candidate']['bundle']):
        save(status='execution_failed', failure={'trial_id': identifier, 'receipt_sha256': sha(result),
            'status': result.get('status'), 'error': result.get('error'), 'retry_authorized': False})
        return
    await engine._read_consensus_check(rid, unchanged, engine, record)
    path = _safe_file(engine, rid, result, 'artifacts/bounded_diagnostic.json')
    if path is None or path.stat().st_size > 1024 * 1024:
        raise ResearchBlocked('新诊断结果文件缺失或超过 1 MiB 证据上限。')
    raw = path.read_bytes(); report = json.loads(raw); digest = hashlib.sha256(raw).hexdigest()
    if (report.get('protocol') != record['protocol'] or report.get('protocol_sha256') != sha(record['protocol'])
            or report.get('input_hashes_unchanged') is not True or report.get('execution_status') != 'completed'
            or type(report.get('optimizer_steps')) is not int or not 0 <= report['optimizer_steps'] <= record['protocol']['max_updates']
            or result.get('experiment_metadata', {}).get('diagnostic_report_sha256') != digest):
        raise ResearchBlocked('新诊断结果未通过协议、输入或更新上限核验。')
    save(status='reviewing_results', outcome={'trial_id': identifier, 'receipt_sha256': sha(result),
         'report_sha256': digest, 'report': report, 'scientific_approval': False})


def author_context(engine, record, index):
    context = copy.deepcopy(record['author_context'])
    if index and record.get('prior_review_items') and record.get('candidate'):
        from .controlled_training_checks import review_context
        reviewed = review_context(engine, record)
        context.update(validation_evidence=reviewed['validation_evidence'],
                       evidence_sha256=reviewed['evidence_sha256'])
        context['experiment_decision']['code_sources'] = record['candidate']['code_sources']
        context['experiment_decision']['issue_table'] = reviewed['rapid_revision']['host_resource_contract']
    return context


async def execute(engine, harness, run_id):
    from .engine import ResearchBlocked
    from .parameter_resolution import owner, COLLECTION as TASKS
    from .controlled_training_checks import unchanged, review_context
    from .rapid_experiment_revision import VOTE, review_receipts, _reviewed
    code_vote_schema = copy.deepcopy(VOTE)
    code_vote_schema['properties']['read_receipts']['maxItems'] = 256
    record = current(engine, run_id)
    if not record:
        record = await engine._read_consensus_check(run_id, register, engine, run_id)
    if not record:
        return None
    from .bounded_review_reading import recover
    record = await engine._read_consensus_check(run_id,recover,engine,record)
    await engine._read_consensus_check(run_id, source_task, engine, record)
    def save(**changes):
        engine.check_active(run_id)
        record.update(changes, updated_at=now())
        engine.store.put(COLLECTION, record['id'], record)
        task = engine.store.get(TASKS, record['source_task_id'])
        task['successor'] = {'diagnostic_id': record['id'], 'status': record['status'], 'action': 'diagnose',
                             'trial_id': record.get('trial_id')}
        task['diagnostic_successor'] = copy.deepcopy(task['successor'])
        engine.store.put(TASKS, task['id'], task)
    async def ask(role, request):
        if 'answer' not in request:
            request['answer'] = await full.ask_assignment(harness, run_id, role, request)
            save()
        owner(engine, run_id, role, request)
        return request['answer']
    async def checked(role, request, checker):
        selected = request.get('read_correction') or request
        await ask(role, selected)
        try:
            return selected, await engine._read_consensus_check(run_id, checker, selected)
        except (ValueError, ResearchBlocked) as exc:
            if 'read_correction' in request:
                raise
            context = copy.deepcopy(request['context'])
            context.setdefault('experiment_decision', {})['read_correction'] = {
                'original_answer': request['answer'], 'error': str(exc)}
            request['read_correction'] = {'purpose': request['purpose'] + ':read-correction-1',
                'instruction': request['instruction'] + '\n补齐并纠正本角色真实读取回执，重新给出独立判断；不得继承批准或编造回执。',
                'context': context, 'schema': copy.deepcopy(request['schema'])}
            save()
            selected = request['read_correction']
            await ask(role, selected)
            return selected, await engine._read_consensus_check(run_id, checker, selected)
    save()
    if record.get('bounded_evidence_review'):
        from .bounded_evidence_review import review
        await review(engine, harness, record, save)
    else:
        for index in range(2):
            if len(record['rounds']) <= index:
                context = author_context(engine, record, index)
                author_instruction = WRITE
                if 'continuation_author_material' in record:
                    from .diagnostic_author_material import instruction
                    author_instruction += instruction(record['continuation_author_material'])
                if record.get('resource_evidence'):
                    author_instruction += ('\n以下宿主执行配置和监控源码随本轮冻结交付。运行时从 bootstrap.json '
                        '读取实际 compute_deadline_monotonic，须提早原子保存最后有效现场和 incomplete；保存失败和原异常均应可追溯。'
                        '复用所附旧候选，逐项修复原评审异议。只交付 bounded_diagnostic*.py。宿主生成入口、清单和证据。\n'
                        + json.dumps({'resource_evidence':record['resource_evidence'],
                                      'prior_candidate_for_repair':record.get('prior_candidate_for_repair')}, ensure_ascii=False))
                context['experiment_decision']['bounded_diagnostic']['feedback'] = [
                    {'error': r.get('error'), 'votes': {k: v.get('answer') for k, v in r.get('votes', {}).items()}} for r in record['rounds']]
                record['rounds'].append({'author': {'purpose': 'experiment_decide:bounded-implementation:' + sha(record['id']) + ':' + str(index),
                    'instruction': author_instruction, 'context': context, 'schema': DELIVERY}})
                save(status='implementing')
            round_ = record['rounds'][index]
            proposal = await ask('engineer', round_['author'])
            if proposal.get('delivery_status') == 'blocked':
                save(status='implementation_blocked', failure={
                    'blockers': proposal['blockers'], 'required_capability': proposal['required_capability'],
                    'execution_authorized': False})
                raise ResearchBlocked('诊断实现有具体能力阻塞，未派发审查或训练：' + '; '.join(proposal['blockers']))
            if 'build' not in round_ and not round_.get('error'):
                try:
                    round_['build'] = await engine._read_consensus_check(run_id, build, engine, record, proposal)
                except (ValueError, SyntaxError) as exc:
                    round_['error'] = str(exc)
                save()
            if round_.get('error'):
                continue
            record.update(round_['build'])
            context = review_context(engine, record)
            requests = round_.setdefault('votes', {})
            review_roles, review_binding = full.prepare_assignments(round_, 'votes', full.make_binding({
                'candidate_sha256': record['candidate']['bundle']['sha256'],
                'amendment_sha256': record['candidate']['amendment_sha256']}, 'bounded_diagnostic_code'))
            requests = round_['votes']
            effective, reads = {}, []
            for role in review_roles:
                requests.setdefault(role, {'purpose': 'rapid_review:bounded:' + record['candidate']['amendment_sha256'],
                    'instruction': REVIEW + ('\n逐项核对 experiments.compare 的 /issues 及各项 acceptance_evidence_path。'
                        '旧意见的来源回执在 /prior_review_receipts。未验证项必须明确保留，当前批准只由本轮独立判断产生。'
                        'compare 的回执不混入 changed_ranges 的代码读取回执。' if record.get('prior_review_items') else '')
                        + ('\n读取候选 host_evidence.json 的执行配置、监控源码；'
                        '/controlled_admission/resource_contract 提供候选SHA、有效限额和逐文件归属。'
                        '审查保存窗口、异常收尾、旧评审异议是否解决。' if record.get('resource_evidence') else ''),
                    'context': copy.deepcopy(context), 'schema': code_vote_schema})
                if review_binding and full.KEY not in requests[role]['context']:
                    requests[role]['context'] = full.bind(requests[role]['context'], review_binding)
                save(status='reviewing_code')
                selected, read = await checked(role, requests[role], lambda a: review_receipts(engine, run_id, record,
                    {'role': role, **a['answer']}, a['context'], purpose=a['purpose'], instruction=a['instruction'], schema=a['schema']))
                effective[role] = selected
                reads.append(read)
            record['votes'] = [a.get('full_review_receipt') or {'role': role, **a['answer']} for role, a in effective.items()]
            save(review_reads=reads)
            if _reviewed(record):
                break
        else:
            save(status='implementation_rejected', failure={'issues': [
                {'error': r.get('error'), 'votes': {k: v.get('answer') for k, v in r.get('votes', {}).items()}} for r in record['rounds']],
                'execution_authorized': False})
            raise ResearchBlocked('诊断实现经两轮修订仍被否决；原证据保留。须修复具体实现/宿主能力或提供新的实测依据，不能改写同一方案后自动循环。')
    await engine._read_consensus_check(run_id, unchanged, engine, record)
    await execute_once(engine, record, save)
    if record['status'] == 'reviewing_results':
        from .experiment_decisions import _archive
        from .fixed_state_diagnostic import validate_reads
        ref = _archive(engine, run_id, {'binding': record['binding'], 'bounded_protocol': record['protocol'],
                                     'bounded_result': record['outcome']})
        context = {'training_context_protocol': 1, 'experiment_workflow_protocol': 1, 'research_cycle': record['cycle'],
                   'validation_evidence': ref, 'evidence_sha256': ref['sha256'],
                   'experiment_decision': {'bounded_result_review': True}}
        requests = record.setdefault('result_requests', {})
        review_roles, review_binding = full.prepare_assignments(record, 'result_requests', full.make_binding({
            'result_evidence': ref, 'binding': record['binding']}, 'bounded_diagnostic_results'))
        requests = record['result_requests']
        for role in review_roles:
            requests.setdefault(role, {'purpose': 'experiment_diagnose:bounded-result:' + ref['sha256'],
                'instruction': RESULT_INSTRUCTION, 'context': copy.deepcopy(context), 'schema': RESULT})
            if review_binding and full.KEY not in requests[role]['context']:
                requests[role]['context'] = full.bind(requests[role]['context'], review_binding)
            save()
            def check(a):
                if a['answer']['evidence_sha256'] != ref['sha256']:
                    raise ValueError('Bounded result review is not bound to this result')
                return validate_reads(engine, record, role, a, RESULT_READS)
            selected, read = await checked(role, requests[role], check)
            requests[role]['read'] = read
            save()
        # Negative and differing interpretations are evidence for the next decision.
        # They do not authorize a rerun, nor erase the completed diagnostic.
        save(status='completed', analyses=[(a.get('read_correction') or a).get('full_review_receipt') or {'role': role, **(a.get('read_correction') or a)['answer']}
                                          for role, a in requests.items()], completed_at=now())
    return {'tuning_pending': True, 'next_node': 'tuning', 'activity': 'bounded_diagnostic', 'diagnostic_id': record['id']}
