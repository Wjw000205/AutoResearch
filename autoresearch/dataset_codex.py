"""File-based Codex delivery of dataset configuration and runtime adapters.

This stays inside the experiment stage. Source, requirements, partial files and
native receipts are durable; the coordinator only receives delivery identities.
"""
from __future__ import annotations
from .prose_schema import validator as response_validator

import asyncio
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import ValidationError, Draft202012Validator
from .prose_schema import validate,relax

from .experiment_workflow import sha
from .storage import now

COLLECTION = 'dataset_codex_jobs'
VERSION = 3


def enabled(engine, run_id):
    run = engine.store.get('runs', run_id)
    return run.get('mode') == 'live' and run['config'].get('implementation_backend') == 'local_codex'


def _bytes(path, limit=2_000_000):
    from .codex_workspace import _plain
    _plain(path)
    if path.stat().st_size > limit:
        raise ValueError('Configuration delivery file exceeds its bounded size: ' + path.name)
    return path.read_bytes()


def _text(path, limit=2_000_000):
    return _bytes(path, limit).decode('utf-8-sig')


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


INSTRUCTION = '''完成当前实验数据集的配置和运行接口代码，直接在本工作目录中读写文件。
先读 _experiment_input/task.json、repair.json、host_runtime_contract.json 和 source_index.json。存在 agent_work_order.json 时执行其中任务；存在 repair_assignment.json 时直接按已有代码和具体缺项修复。按需读取设计字段和源码。
host_runtime_contract.json 描述实际提供的参数、环境回执及 CPU 检查入口；按它实现，不要求宿主提供未定义的授权字段、清单文件或分区回执。
若有 _experiment_input/dispatch_samples.json，它是上一版实际派发参数，直接定位 CLI 与宿主扁平参数的差异。CPU 断言还必须读取 dataset_runtime/dispatch_samples.json 中本次真实派发参数，覆盖所有比较分支的配置、资源初始化及模型接口；不要只检查 CLI 从配置根节点合并出的参数。
若有 _experiment_input/failure_receipt.json，先读取实际失败堆栈、执行绑定和解释器信息，按该证据定位；不要把其他潜在问题当作本次已确认原因。文件身份使用宿主提供的 dataset_runtime.host_identity.portable_stat，保持全部身份字段校验。
这是实验阶段的增量实现任务。复用模型，不重新讨论想法、架构或完整实现文档。
已有接口可用就写配置；缺少数据读取、参数入口、训练编排或评估接口就实现 dataset_runtime/adapter.py 的 run(config) 以及必要的 dataset_runtime/*.py。入口 experiment.py 和配置解析器由宿主提供，不需修改。
原模型和其他原源码必须保持不变；调用已有 src 模型，禁止复制重写模型或 monkeypatch。
逐个文件落盘，读完相关接口立即编写对应部分；继续时从已保存文件着手，只补读具体缺项。无需让全部原文同时驻留上下文。
交付 _experiment_delivery/configuration.json、checks.py、rationale.txt，以及实际 dataset_runtime/*.py 文件。
完整配置、执行策略和输出约束见 _experiment_input/contract.md、delivery_contract.json 与 task.json。dataset_budget.status=unlimited 时不交付预算文件，按任务声明完整种子、对照与消融覆盖；否则同时交付 budget_plan.json。逐文件核对交付 schema 的必填字段、类型和值约束；JSON 字符串不设字符上限。配置不得写入授权标记。
用 delivery_contract.json 的 configuration_schema 校验解析后的 configuration.json；预算如需交付放在 budget_plan.json，不向配置添加 trial_budget 等额外字段。task_requirements.txt 保存本任务原始要求。
可以进行语法与静态检查；checks.py 写 CPU 兼容性断言，检查由宿主在独立评议及预算准入后运行。本回合不训练、不运行数据检查、不读取留出集、不下载或安装依赖。
修订任务只处理 _experiment_input/repair.json 中的缺项、评议或检查错误。原交付有问题时直接修改本地候选文件。
模型源码、输入材料和历史记录不得删除或修改。不要操作工作目录外的文件或调用其他任务。
最终回复简述修改文件和未解决问题，代码和配置只放文件中；完成声明不代表通过核验或实验成功。'''


def delivery_issue(exc):
    """Report the failing field without copying a configuration/source into context."""
    if not isinstance(exc, ValidationError):
        return str(exc)[:1200]
    path = '/' + '/'.join(map(str, exc.absolute_path))
    rule = exc.validator
    if rule in {'maxLength', 'minLength', 'maxItems', 'minItems'}:
        return f'{path}: {rule}={exc.validator_value}; actual_length={len(exc.instance)}'
    if rule == 'required':
        missing = [key for key in exc.validator_value if key not in exc.instance]
        return f'{path}: missing required fields {missing}'[:1200]
    if rule == 'additionalProperties' and exc.validator_value is False and isinstance(exc.instance, dict):
        extra = sorted(set(exc.instance) - set(exc.schema.get('properties', {})))
        return f'{path}: unexpected fields {extra}; see delivery_contract.json'[:1200]
    return f'{path}: validation failed ({rule}); see delivery_contract.json'[:1200]


def _file_draft(files):
    return {'configuration_json': files['_experiment_delivery/configuration.json'],
        'checks': files['_experiment_delivery/checks.py'],
        'rationale': files['_experiment_delivery/rationale.txt'],
        'budget_plan': json.loads(files['_experiment_delivery/budget_plan.json']),
        'adapter_files': [{'path': name, 'source': source} for name, source in sorted(files.items())
            if name.startswith('dataset_runtime/') and name.endswith('.py')]}


def _repair_packet(saved, repair, previous_job):
    packet = {k: copy.deepcopy(repair.get(k)) for k in ('kind', 'message', 'details', 'votes', 'notes')}
    if previous_job and '_experiment_input/host_runtime_contract.json' not in previous_job.get('protected', {}):
        packet['runtime_compatibility'] = {
            'contract_file': '_experiment_input/host_runtime_contract.json',
            'issue': 'Prior delivery was authored without the actual host runtime contract. Verify and repair its file paths, receipt fields, hash encoding and CPU check entry against this contract.',
            'checks': ['Read actual environment.json dataset_assets receipts; replace invented bootstrap binding or partition-hash requirements.',
                'CPU checks execute in trial-root experiment.py with phase=code_check; assertions must use Path.cwd() and distinguish check parameters from candidate training configurations.']}
    task = copy.deepcopy(saved['request'])
    if previous_job and previous_job.get('partial_delivery'):
        from .dataset_experiments import configuration_schema
        try:
            files = previous_files(previous_job)
            configuration = json.loads(files['_experiment_delivery/configuration.json'])
            issues = [delivery_issue(e) for e in response_validator(configuration_schema()).iter_errors(configuration)][:8]
            if issues:
                packet['configuration_issues'] = issues
                packet['configuration_instruction'] = '修正解析后的配置字段及相应 CPU 断言。预算只在 budget_plan.json 中交付；运行配置不可添加 trial_budget。'
        except (KeyError, ValueError):
            pass  # The ordinary delivery checker reports missing/invalid JSON.
    if packet['kind'] == 'codex_delivery' and previous_job and previous_job.get('partial_delivery'):
        from .dataset_experiments import DRAFT
        try:
            draft = _file_draft(previous_files(previous_job))
            issues = [delivery_issue(e) for e in response_validator(DRAFT['oneOf'][0]).iter_errors(draft)][:8]
        except (KeyError, ValueError) as exc:
            issues = ['Delivery file missing: ' + str(exc) if isinstance(exc, KeyError) else delivery_issue(exc)]
        if issues:
            packet.update(message='修复已保存文件中的具体交付错误，再由宿主重新核验。',
                details={'issues': issues, 'previous_job_id': previous_job['id'],
                    'files_sha256': sha(previous_job['partial_delivery']), 'previous_setup': saved.get('previous_setup')})
            task['dataset_configuration']['repair'] = {
                **task['dataset_configuration'].get('repair', {}), 'issues': issues}
    return task, packet


def prepare(engine, run_id, saved):
    from .dataset_experiments import _source_gate, source_evidence, _base_bundle, WRITE
    from .codex_workspace import allocate
    from .rapid_experiment_revision import _write_files
    from .experiment_config import ENTRY
    from . import experiment_config, dataset_file_identity
    run = engine.store.get('runs', run_id)
    config = run['config']
    if not config.get('allow_local_execution'):
        from .engine import ResearchBlocked
        raise ResearchBlocked('本机 Codex 配置实现需要已授权的本地代码执行。')
    original = _source_gate(engine, run_id)
    bundle = _base_bundle(engine, run_id, original)
    evidence = source_evidence(engine, run_id, saved)
    from .dataset_work_order import resolve
    work_order = resolve(engine.store, saved)
    direct = saved.get('direct_repair')
    if direct:
        from .dataset_work_order import repair_assignment
        if direct != repair_assignment(engine, run_id, saved):
            raise ValueError('Configuration repair assignment changed before dispatch')
    selection = {'project_root': config.get('codex_project_dir') or original['program'].get('codex_project_dir'),
        'model': config.get('codex_model') or None, 'effort': config.get('codex_reasoning_effort') or None,
        'timeout': config.get('codex_timeout_seconds', 1800)}
    identity = {'version': VERSION, 'setup_id': saved['id'], 'request_sha256': sha(saved['request']),
        'source_bundle_sha256': bundle['sha256'], 'selection': selection,
        'work_order_sha256': work_order['result_sha256'] if work_order else None}
    if direct:
        identity['repair_assignment_sha256'] = sha(direct)
    key = run_id + ':dataset-codex:' + sha(identity)
    prior = engine.store.get(COLLECTION, key)
    if prior:
        return prior
    if not selection['project_root']:
        raise ValueError('Dataset Codex delivery requires the configured project directory')
    workspace = allocate(selection['project_root'], run_id, key, run.get('title'), engine.cycle(run_id), 'runtime_repair')
    files = {r['path']: _bytes(Path(bundle['root']) / r['path']) for r in bundle['files']}
    # The reviewed host shim supplies CLI parsing; Codex owns only the adapter.
    files.update({'experiment.py': ENTRY.encode(), 'dataset_runtime/__init__.py': b'',
        'dataset_runtime/config.py': Path(experiment_config.__file__).read_bytes(),
        'dataset_runtime/host_identity.py': Path(dataset_file_identity.__file__).read_bytes()})
    protected = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()
                 if not name.startswith('dataset_runtime/') or name in {'dataset_runtime/config.py', 'dataset_runtime/__init__.py', 'dataset_runtime/host_identity.py'}}
    repair = evidence.get('configuration_repair', {})
    previous_draft = repair.get('draft') or {}
    # Reuse rejected candidate files instead of asking a new native task to
    # reconstruct its own previous code from commentary.
    for item in previous_draft.get('adapter_files', []):
        from .code_bundle import relative_file
        relative_file(item['path'])
        if item['path'].startswith('dataset_runtime/') and item['path'] not in protected:
            files[item['path']] = item['source'].encode()
    # A partial native delivery also survives a host schema/manifest rejection.
    previous_job_id = saved.get('previous_codex_job_id')
    previous_job = engine.store.get(COLLECTION, previous_job_id) if previous_job_id else None
    prior_files = {}
    if previous_job and previous_job.get('run_id') == run_id:
        prior_files = previous_files(previous_job)
        for name, text in prior_files.items():
            if name.startswith('_experiment_delivery/') or name.startswith('dataset_runtime/') and name not in protected:
                files[name] = text.encode()
    for field, name in [('configuration_json', 'configuration.json'), ('checks', 'checks.py'), ('rationale', 'rationale.txt')]:
        if previous_draft.get(field):
            files['_experiment_delivery/' + name] = previous_draft[field].encode()
    if previous_draft.get('budget_plan'):
        files['_experiment_delivery/budget_plan.json'] = _json(previous_draft['budget_plan']).encode()
    from .dataset_experiments import DRAFT, configuration_schema
    from .dataset_runtime_contract import describe
    task, repair_packet = _repair_packet(saved, repair, previous_job)
    dispatch_samples = None
    if previous_draft.get('configuration_json'):
        from .dataset_experiments import compile_configuration
        from .dataset_runtime_contract import dispatch_samples as samples
        try:
            _, previous_plan = compile_configuration(engine, run_id,
                json.loads(previous_draft['configuration_json']), saved['dataset_packet'], previous_draft.get('budget_plan'))
            dispatch_samples = samples(previous_plan)
        except ValueError:
            pass  # Invalid drafts retain their precise configuration repair issue.
    inputs = {'_experiment_input/task.json': _json(task),
        '_experiment_input/task_requirements.txt': str(evidence.get('task_requirements', '')),
        '_experiment_input/host_runtime_contract.json': _json(describe()),
        '_experiment_input/contract.md': WRITE,
        '_experiment_input/delivery_contract.json': _json({'schema': relax(DRAFT['oneOf'][0]),
            'configuration_schema': relax(configuration_schema()),
            'files': {'configuration_json': '_experiment_delivery/configuration.json',
                'checks': '_experiment_delivery/checks.py', 'rationale': '_experiment_delivery/rationale.txt',
                'budget_plan': '_experiment_delivery/budget_plan.json', 'adapter_files': 'dataset_runtime/*.py'},
            'request_sha256': sha(saved['request'])}),
        '_experiment_input/repair.json': _json(repair_packet),
        '_experiment_input/source_index.json': _json({
            'source_bundle_sha256': bundle['sha256'], 'files': bundle['files'],
            'previous_delivery_files': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in prior_files.items()},
            'design_fields': {k: '_experiment_input/design/' + k + '.json' for k in evidence['source_design']}}),
        **{'_experiment_input/design/' + k + '.json': _json(v) for k, v in evidence['source_design'].items()}}
    if work_order:
        inputs['_experiment_input/agent_work_order.json'] = _json({'plan_id': work_order['id'],
            'sha256': work_order['result_sha256'], 'author_handoff_id': work_order['author_handoff_id'],
            'task': work_order['result']})
    if dispatch_samples is not None:
        inputs['_experiment_input/dispatch_samples.json'] = _json(dispatch_samples)
    failure = failure_receipt(engine, run_id, repair_packet.get('details') or {})
    if failure:
        inputs['_experiment_input/failure_receipt.json'] = _json(failure)
    if direct:
        inputs['_experiment_input/repair_assignment.json'] = _json(direct)
    files.update({k: v.encode() for k, v in inputs.items()})
    protected.update({k: hashlib.sha256(v.encode()).hexdigest() for k, v in inputs.items()})
    _write_files(workspace, files)
    instruction = INSTRUCTION + ('\n\n研究 agent 本轮实现指令：\n' + work_order['result']['implementation_brief'] if work_order else '')
    if direct:
        instruction += '\n\n' + direct['instruction']
    state = {'id': key, 'run_id': run_id, 'setup_id': saved['id'], 'cycle': engine.cycle(run_id),
        'identity': identity, 'workspace': str(workspace), 'selection': selection, 'instruction': instruction,
        'agent_work_order': copy.deepcopy(saved.get('agent_work_order')),
        'instruction_sha256': hashlib.sha256(instruction.encode()).hexdigest(), 'protected': protected,
        'status': 'prepared', 'created_at': now()}
    engine.check_active(run_id)
    engine.store.put(COLLECTION, key, state)
    engine.store.event(run_id, 'experiment.configuration_codex_prepared',
        '已向本机 Codex 准备数据配置与接口实现任务，交付后在当前实验阶段核验。',
        {'job_id': key, 'setup_id': saved['id'], 'workspace': str(workspace)})
    return state


def failure_receipt(engine, run_id, details):
    """Supply bounded authentic execution evidence to the isolated repair job."""
    trial_id = details.get('trial_id')
    if not trial_id or not details.get('result_sha256'):
        return None
    result = engine.store.get('experiments', run_id + ':' + trial_id)
    if not result or result.get('run_id') != run_id or sha(result) != details['result_sha256']:
        raise ValueError('Configuration repair execution evidence changed')
    selected = {k: copy.deepcopy(result.get(k)) for k in ('id', 'status', 'provenance',
        'error', 'exit_code', 'params', 'research_cycle', 'code_revision', 'code_sha256',
        'experiment_plan_sha256', 'duration_seconds', 'dataset_assets')}
    selected.update(result_sha256=details['result_sha256'],
        stderr=str(result.get('stderr') or '')[-8000:], stdout=str(result.get('stdout') or '')[-2000:])
    return selected


def collect(state):
    from .dataset_experiments import DRAFT, configuration_schema
    root = Path(state['workspace'])
    for name, digest in state['protected'].items():
        if hashlib.sha256(_bytes(root / name)).hexdigest() != digest:
            raise ValueError('Codex changed protected model/source or input: ' + name)
    adapters = []
    from .codex_workspace import _plain
    _plain(root / 'dataset_runtime')
    for path in sorted((root / 'dataset_runtime').rglob('*.py')):
        name = path.relative_to(root).as_posix()
        if name in state['protected']:
            continue
        adapters.append({'path': name, 'source': _text(path, 96000)})
    output = root / '_experiment_delivery'
    draft = {'configuration_json': _text(output / 'configuration.json', 320000),
        'checks': _text(output / 'checks.py', 48000), 'rationale': _text(output / 'rationale.txt', 6000),
        'adapter_files': adapters}
    if (output / 'budget_plan.json').is_file():
        draft['budget_plan'] = json.loads(_text(output / 'budget_plan.json', 24000))
    validate(draft, DRAFT['oneOf'][0])
    validate(json.loads(draft['configuration_json']), configuration_schema())
    return draft


def partial(state):
    """Bounded files remain on disk and can seed a subsequent repair job."""
    root, result = Path(state['workspace']), {}
    candidates = []
    required = {'_experiment_delivery/' + name for name in ('configuration.json', 'budget_plan.json', 'checks.py', 'rationale.txt')}
    for folder in ('_experiment_delivery', 'dataset_runtime'):
        from .codex_workspace import _plain
        _plain(root / folder)
        for path in sorted((root / folder).rglob('*')):
            name = path.relative_to(root).as_posix()
            if path.is_file() and path.suffix in {'.py', '.json', '.txt'} and name not in state['protected']:
                candidates.append((name, path))
    # Old static reports must never crowd out the repairable implementation.
    candidates.sort(key=lambda row: (row[0] not in required,
        not (row[0].startswith('dataset_runtime/') and row[0].endswith('.py')), row[0]))
    for name, path in candidates[:16]:
        result[name] = _text(path, 320000)
    return result


def previous_files(state):
    """Read a completed local delivery as new repair input; keep its old receipt."""
    if (state.get('checkpoint', {}).get('status') in {'completed', 'failed', 'interrupted', 'unknown'}
            and Path(state['workspace']).is_dir()):
        for name, digest in state['protected'].items():
            if hashlib.sha256(_bytes(Path(state['workspace']) / name)).hexdigest() != digest:
                raise ValueError('Previous Codex protected source/input changed: ' + name)
        # These are source snapshots for a new repair, not a terminal receipt
        # or approval of an interrupted/unknown native turn.
        return partial(state)
    return copy.deepcopy(state.get('partial_delivery') or {})


async def deliver(engine, harness, run_id, saved):
    from .dataset_experiments import COLLECTION as SETUPS
    from .dataset_configuration_repair import NeedsRepair
    from .local_codex import LocalCodexClient, LocalCodexError
    from .codex_accounting import capture_usage
    from .engine import ResearchBlocked
    engine.check_active(run_id)
    state = engine.store.get(COLLECTION, saved.get('codex_job_id', ''))
    if state is None:
        state = await asyncio.to_thread(prepare, engine, run_id, saved)
    engine.check_active(run_id)
    saved.update(codex_job_id=state['id'], status='implementing_configuration')
    engine.store.put(SETUPS, saved['id'], saved)
    if state.get('draft'):
        return copy.deepcopy(state['draft'])
    selected = state['selection']
    def progress(receipt):
        state.update(checkpoint=copy.deepcopy(receipt), status=receipt['status'], updated_at=now())
        engine.store.put(COLLECTION, state['id'], state)
        usage = capture_usage(engine.store, run_id, receipt)
        if usage:
            usage.update(role='engineer', purpose='dataset_configuration', backend='implementation')
            engine.store.put('codex_usage', usage['id'], usage)
    if not state.get('reservation'):
        budget = engine.store.get('budgets', run_id, {})
        previous = next((r for r in [*budget.get('pending', {}).values(), *budget.get('records', [])]
                         if r.get('operation_id') == state['id']), None)
        state['reservation'] = previous or harness.intelligence._reserve(run_id,
            {'id': 'local_codex', 'protocol': 'local_codex', 'model': selected['model']},
            'engineer', state['instruction'], 2000, operation_id=state['id'])
        engine.store.put(COLLECTION, state['id'], state)
    secret_names = [p.get('api_key_env') for p in getattr(harness.intelligence, 'settings', {}).get('providers', []) if p.get('api_key_env')]
    client = getattr(engine, 'codex_client', None) or LocalCodexClient(
        timeout=selected['timeout'], blocked_env_names=secret_names)
    try:
        receipt = await client.run(run_id, Path(state['workspace']), state['instruction'], operation_id=state['id'],
            checkpoint=state.get('checkpoint'), project_root=selected['project_root'],
            model=selected['model'], effort=selected['effort'], reasoning_only=False,
            on_progress=progress, check_active=lambda: engine.check_active(run_id))
    except LocalCodexError as exc:
        raise ResearchBlocked('本机 Codex 配置交付：' + str(exc)) from exc
    progress(receipt)
    engine.check_active(run_id)
    if receipt['status'] != 'completed':
        if receipt['status'] in {'failed', 'interrupted'}:
            state['partial_delivery'] = await asyncio.to_thread(partial, state)
            engine.store.put(COLLECTION, state['id'], state)
            harness.intelligence._settle(state['reservation'], {}, 0, verified=False, status=receipt['status'],
                provenance='local_codex_app_server', cost_status='not_reported_by_local_codex')
            raise NeedsRepair('Codex 配置实现回合已终止：' + str(receipt.get('error_code') or receipt['status']),
                kind='codex_delivery', details={'job_id': state['id'], 'terminal_status': receipt['status']})
        raise ResearchBlocked('本机 Codex 配置交付尚未完成；保留原回合及工作文件，恢复时读取原回执。')
    harness.intelligence._settle(state['reservation'], {}, 0, verified=False, status='completed',
        provenance='local_codex_app_server', cost_status='not_reported_by_local_codex')
    try:
        state['partial_delivery'] = await asyncio.to_thread(partial, state)
        engine.store.put(COLLECTION, state['id'], state)
        draft = await asyncio.to_thread(collect, state)
    except (OSError, ValueError, ValidationError) as exc:
        detail = delivery_issue(exc)
        raise NeedsRepair('Codex 配置交付待修正：' + detail, kind='codex_delivery',
            details={'job_id': state['id'], 'issues': [detail]}) from exc
    engine.check_active(run_id)
    state.update(draft=draft, draft_sha256=sha(draft), status='delivered', updated_at=now())
    engine.store.put(COLLECTION, state['id'], state)
    engine.store.event(run_id, 'experiment.configuration_codex_delivered',
        'Codex 已保存配置与接口代码，开始独立核验和兼容性检查。',
        {'job_id': state['id'], 'draft_sha256': state['draft_sha256'], 'file_count': len(draft['adapter_files'])})
    return copy.deepcopy(draft)


def view(store, saved):
    job = store.get(COLLECTION, saved.get('codex_job_id', '')) or {}
    receipt = job.get('checkpoint') or {}
    if not job:
        return None
    return {'id': job['id'], 'status': job['status'], 'workspace': job['workspace'],
        'thread_id': receipt.get('thread_id'), 'turn_id': receipt.get('turn_id'),
        'activity': (receipt.get('activities') or [])[-3:], 'draft_sha256': job.get('draft_sha256')}
