"""File-based Codex delivery of dataset configuration and runtime adapters.

This stays inside the experiment stage. Source, requirements, partial files and
native receipts are durable; the coordinator only receives delivery identities.
"""
from __future__ import annotations
from .async_ownership import to_thread as owned_to_thread
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


def _base_adapters(state):
    from .code_bundle import digest
    name = '_experiment_input/source_index.json'
    if name not in state.get('protected', {}):
        return {}
    raw = _bytes(Path(state['workspace']) / name)
    if hashlib.sha256(raw).hexdigest() != state['protected'][name]:
        raise ValueError('Frozen source index changed')
    index = json.loads(raw)
    if digest(index['files']) != index['source_bundle_sha256'] or index['source_bundle_sha256'] != state['identity']['source_bundle_sha256']:
        raise ValueError('Frozen source index does not match the base bundle')
    return {row['path']: row['sha256'] for row in index['files']
            if row['path'].startswith('dataset_runtime/') and row['path'].endswith('.py')
            and row['path'] not in state['protected']}


def _file_draft(files, state):
    base = _base_adapters(state)
    return {'configuration_json': files['_experiment_delivery/configuration.json'],
        'checks': files['_experiment_delivery/checks.py'],
        'rationale': files['_experiment_delivery/rationale.txt'],
        **({'budget_plan': json.loads(files['_experiment_delivery/budget_plan.json'])}
           if '_experiment_delivery/budget_plan.json' in files else {}),
        'adapter_files': [{'path': name, 'source': source} for name, source in sorted(files.items())
            if name.startswith('dataset_runtime/') and name.endswith('.py')
            and base.get(name) != hashlib.sha256(source.encode()).hexdigest()]}


def _repair_packet(saved, repair, previous_job, *, previous_sources=None):
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
            files = previous_files(previous_job) if previous_sources is None else previous_sources
            configuration = json.loads(files['_experiment_delivery/configuration.json'])
            issues = [delivery_issue(e) for e in response_validator(configuration_schema(saved['dataset_packet'])).iter_errors(configuration)][:8]
            if issues:
                packet['configuration_issues'] = issues
                packet['configuration_instruction'] = '修正解析后的配置字段及相应 CPU 断言。预算只在 budget_plan.json 中交付；运行配置不可添加 trial_budget。'
        except (KeyError, ValueError):
            pass  # The ordinary delivery checker reports missing/invalid JSON.
    if packet['kind'] == 'codex_delivery' and previous_job and previous_job.get('partial_delivery'):
        from .dataset_experiments import DRAFT
        try:
            files = previous_files(previous_job) if previous_sources is None else previous_sources
            draft = _file_draft(files, previous_job)
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
    from . import code_model_migration as worker_models
    selected_profile = worker_models.run_profile(engine, run_id)
    if worker_models.FIELD in selected_profile:
        selection.update(model=selected_profile['model'] or None, effort=selected_profile['effort'] or None)
    identity = {'version': VERSION, 'setup_id': saved['id'], 'request_sha256': sha(saved['request']),
        'source_bundle_sha256': bundle['sha256'], 'selection': selection,
        'work_order_sha256': work_order['result_sha256'] if work_order else None}
    if work_order and work_order.get('protocol_evidence'):
        from .experiment_protocols import checked, _notes
        notes = _notes(engine, run_id)
        for ref in work_order['protocol_evidence']['protocols']:
            checked(engine, run_id, ref['id'], ref['sha256'], notes=notes)
        identity['protocol_evidence_sha256'] = sha(work_order['protocol_evidence'])
    if direct:
        identity['repair_assignment_sha256'] = sha(direct)
    if worker_models.FIELD in selected_profile:
        # Reconcile the same already-frozen setup even if a crash preceded the
        # setup's job pointer. Never replace a prepared/started native request.
        base_identity = {k: v for k, v in identity.items() if k != 'selection'}
        prior_jobs = []
        for prior_id in engine.store.record_keys(COLLECTION,
                predicate="json_extract(data,'$.run_id')=? AND json_extract(data,'$.setup_id')=?",
                parameters=[run_id, saved['id']]):
            previous = engine.store.get(COLLECTION, prior_id)
            frozen = previous.get('identity', {})
            if {k: v for k, v in frozen.items() if k not in {'selection', worker_models.FIELD}} == base_identity:
                if (previous.get('id') != prior_id or previous.get('run_id') != run_id
                        or prior_id != run_id + ':dataset-codex:' + sha(frozen)
                        or previous.get('selection') != frozen.get('selection')):
                    raise ValueError('Frozen dataset code job identity changed')
                worker_models.job_profile(engine, run_id, previous)
                prior_jobs.append(previous)
        if len(prior_jobs) > 1:
            raise ValueError('Multiple frozen code jobs own this dataset setup')
        if prior_jobs:
            return prior_jobs[0]
        identity[worker_models.FIELD] = copy.deepcopy(selected_profile[worker_models.FIELD])
    key = run_id + ':dataset-codex:' + sha(identity)
    prior = engine.store.get(COLLECTION, key)
    if prior:
        return prior
    if not selection['project_root']:
        raise ValueError('Dataset Codex delivery requires the configured project directory')
    workspace = allocate(selection['project_root'], run_id, key, run.get('title'), engine.cycle(run_id), 'runtime_repair')
    files = {}
    for row in bundle['files']:
        raw = _bytes(Path(bundle['root']) / row['path'])
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('Frozen source changed during workspace preparation: ' + row['path'])
        files[row['path']] = raw
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
        if direct and direct.get('protected_source_recovery'):
            prior_files, recovery = repair_inputs(engine, run_id, saved, previous_job, repair, bundle=bundle)
            if recovery != direct['protected_source_recovery'] or sha(prior_files) != direct['previous_files_sha256']:
                raise ValueError('Frozen predecessor repair inputs changed before staging')
            if any(protected.get(name) != item['expected_sha256'] for name, item in recovery['changed_files'].items()):
                raise ValueError('Successor workspace does not restore the exact protected file identities')
        else:
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
    task, repair_packet = _repair_packet(saved, repair, previous_job, previous_sources=prior_files)
    if direct and direct.get('protected_source_recovery'):
        repair_packet['protected_source_recovery'] = direct['protected_source_recovery']
    dispatch_samples = None
    if previous_draft.get('configuration_json'):
        from .dataset_experiments import compile_configuration
        from .dataset_runtime_contract import dispatch_samples as samples
        try:
            _, previous_plan = compile_configuration(engine, run_id,
                json.loads(previous_draft['configuration_json']), saved['dataset_packet'], previous_draft.get('budget_plan'),
                dataset_catalog=saved['request'].get('dataset_catalog'))
            dispatch_samples = samples(previous_plan)
        except ValueError:
            pass  # Invalid drafts retain their precise configuration repair issue.
    inputs = {'_experiment_input/task.json': _json(task),
        '_experiment_input/task_requirements.txt': str(evidence.get('task_requirements', '')),
        '_experiment_input/host_runtime_contract.json': _json(describe()),
        '_experiment_input/contract.md': WRITE,
        '_experiment_input/delivery_contract.json': _json({'schema': relax(DRAFT['oneOf'][0]),
            'configuration_schema': relax(configuration_schema(saved['dataset_packet'])),
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
            'task': work_order['result'],
            **({'protocol_evidence': work_order['protocol_evidence'],
                'execution_conditions': {v['role']: v['execution_conditions'] for v in work_order['votes']}}
               if work_order.get('protocol_evidence') else {})})
    if dispatch_samples is not None:
        inputs['_experiment_input/dispatch_samples.json'] = _json(dispatch_samples)
    failure = failure_receipt(engine, run_id, repair_packet.get('details') or {})
    if failure:
        inputs['_experiment_input/failure_receipt.json'] = _json(failure)
    if direct:
        inputs['_experiment_input/repair_assignment.json'] = _json(direct)
        if direct.get('project_protocol'):
            from .weather_project_protocol import verify, seed_configuration
            approved = verify(engine.store, run_id, direct['project_protocol'])
            inputs['_experiment_input/project_protocol.json'] = _json({
                'receipt': direct['project_protocol'], 'proposal': approved['proposal'], 'votes': approved['votes']})
            configuration = '_experiment_delivery/configuration.json'
            if configuration in files:
                try:
                    files[configuration] = seed_configuration(files[configuration], approved['proposal'])
                except (KeyError, TypeError, ValueError) as exc:
                    # A malformed previous delivery remains an editable input.
                    # The collector still requires the complete reviewed fields.
                    inputs['_experiment_input/project_configuration_issue.txt'] = str(exc)
        # Carry the exact reviewed protocol addendum and execution conditions
        # through repairs, including when the active setup now has a new request.
        name = '_experiment_input/agent_work_order.json'
        if previous_job and name in previous_job['protected'] and name not in inputs:
            raw = _bytes(Path(previous_job['workspace']) / name)
            if hashlib.sha256(raw).hexdigest() != previous_job['protected'][name]:
                raise ValueError('Previous approved work order changed')
            inputs[name] = raw.decode('utf-8')
    inputs['_experiment_input/write_scope.json'] = _json({
        'read_only_base_files': sorted(protected), 'read_only_inputs': '_experiment_input/',
        'editable': ['_experiment_delivery/configuration.json', '_experiment_delivery/checks.py',
                     '_experiment_delivery/rationale.txt', 'dataset_runtime/*.py (except the named read-only files)'],
        'budget_plan': 'Only when required by the current task',
        'adapter_edits': 'Only new or changed dataset_runtime Python modules count as edits; omitted inherited adapters remain in the assembled candidate.',
        'configuration_destination': 'Write configuration.json in _experiment_delivery; the host stages it at dataset_runtime/configurations.json.'})
    files.update({k: v.encode() for k, v in inputs.items()})
    protected.update({k: hashlib.sha256(v.encode()).hexdigest() for k, v in inputs.items()})
    _write_files(workspace, files)
    instruction = INSTRUCTION + ('\n\n研究 agent 本轮实现指令：\n' + work_order['result']['implementation_brief'] if work_order else '')
    if work_order and work_order.get('protocol_evidence'):
        instruction += '\n任务单的 protocol_evidence 是三方已审补证，按其字段更新旧待核实描述；保留 execution_conditions 作为执行前条件。'
    if direct:
        instruction += '\n\n' + direct['instruction']
        if direct.get('project_protocol'):
            instruction += ('\n先读 _experiment_input/project_protocol.json。三方已对该 SHA 的非论文等价 128→32 项目协议作出独立决定。'
                '该补充决定解决原工单中等待明确窗口授权的问题。把真实 CSV 的字段、半开区间、fit_rows、地板及异常时间边界策略'
                '落实到三分支与嵌套消融、comparison_task/task_dimensions。在每个 *_params 顶层写 project_protocol_sha256=receipt.sha256；'
                '项目窗口只适用于这份精确批准，保留两条原论文记录。落实 invalid_value_policy，真实表头中的替换字符按原字节解码保留。'
                '修改 dataset_runtime/contracts.py 的窗口判断以分别核验论文记录和项目协议；data.py 只允许列出的时间异常，'
                '排除跨越异常的完整输入+预测窗口；checks.py 验证真实前缀、异常两侧、合法窗口和留出隔离。'
                '保留未更改模型及其他有效检查。根据实际宿主 YAML 契约区分训练和 code_check。旧 dispatch_samples 是上版快照，'
                'CPU 检查运行时使用宿主从新计划生成的样本；不得以旧快照覆盖新配置。不需要重写工单或再次申请同一个项目窗口。')
    instruction += ('\n文件写入范围见 _experiment_input/write_scope.json。工单中“同步 YAML/schema”须通过适配层消费已冻结的契约，'
                    '所有 configs/ 文件均只读。配置参数写入 _experiment_delivery/configuration.json；'
                    '用原契约声明的运行时 YAML 与回执路径。若不兼容，报告具体冲突，不修改冻结文件。')
    if 'configs/hyperparameters.contract.json' in files:
        instruction += ('\n核对 dataset_runtime/hyperparameters.py 的 resolve_profile/load_runtime 及 checks.py：'
                        '逐试验 YAML 和摘要回执由宿主按 contract 的 runtime_file/receipt_file 交付，'
                        'configs/ 下的默认值只作对照。核验实际 YAML、参数与 profile 的绑定并返回约定的元数据；'
                        '不得要求原 schema 中不存在的 dataset_contract 或以 environment.json 替代该回执。')
    from .request_hash_policy import instruction as hash_checked_instruction
    instruction = hash_checked_instruction(instruction)
    state = {'id': key, 'run_id': run_id, 'setup_id': saved['id'], 'cycle': engine.cycle(run_id),
        'identity': identity, 'workspace': str(workspace), 'selection': selection, 'instruction': instruction,
        'agent_work_order': copy.deepcopy(saved.get('agent_work_order')),
        'instruction_sha256': hashlib.sha256(instruction.encode()).hexdigest(), 'protected': protected,
        'status': 'prepared', 'created_at': now()}
    if worker_models.FIELD in selected_profile:
        state['operation_id'] = key
        worker_models.bind_job(engine, run_id, state)
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
    changed = protected_changes(state)
    if changed:
        raise ProtectedSourceChanged('Codex changed protected model/source or input: ', changed)
    adapters = []
    base = _base_adapters(state)
    from .codex_workspace import _plain
    _plain(root / 'dataset_runtime')
    for path in sorted((root / 'dataset_runtime').rglob('*.py')):
        name = path.relative_to(root).as_posix()
        if name in state['protected']:
            continue
        raw = _bytes(path)
        if base.get(name) == hashlib.sha256(raw).hexdigest():
            continue
        if len(raw) > 96000:
            raise ValueError('Configuration delivery file exceeds its bounded size: ' + name)
        adapters.append({'path': name, 'source': raw.decode('utf-8-sig')})
    output = root / '_experiment_delivery'
    draft = {'configuration_json': _text(output / 'configuration.json', 320000),
        'checks': _text(output / 'checks.py', 48000), 'rationale': _text(output / 'rationale.txt', 320000),
        'adapter_files': adapters}
    if (output / 'budget_plan.json').is_file():
        draft['budget_plan'] = json.loads(_text(output / 'budget_plan.json', 24000))
    contract_path = '_experiment_input/delivery_contract.json'
    frozen_contract = (json.loads(_bytes(root / contract_path))
                       if contract_path in state['protected'] else {})
    validate(draft, frozen_contract.get('schema') or DRAFT['oneOf'][0])
    validate(json.loads(draft['configuration_json']), frozen_contract.get('configuration_schema') or configuration_schema())
    protocol = '_experiment_input/project_protocol.json'
    if protocol in state['protected']:
        from .weather_project_protocol import check_profiles
        value = json.loads(_bytes(root / protocol))
        if sha(value['proposal']) != value['receipt']['sha256']:
            raise ValueError('Staged Weather project protocol changed')
        check_profiles(json.loads(draft['configuration_json']), value['proposal'])
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


class ProtectedSourceChanged(ValueError):
    def __init__(self, prefix, changes):
        self.changes = changes
        super().__init__(prefix + ', '.join(changes))


def protected_changes(state):
    from .code_bundle import relative_file
    changed = {}
    root = Path(state['workspace'])
    for name, expected in state['protected'].items():
        relative_file(name)
        try:
            actual = hashlib.sha256(_bytes(root / name)).hexdigest()
        except FileNotFoundError:
            actual = None
        if actual != expected:
            changed[name] = {'expected_sha256': expected, 'observed_sha256': actual}
    return changed


def previous_files(state):
    """Read a completed local delivery as new repair input; keep its old receipt."""
    if (state.get('checkpoint', {}).get('status') in {'completed', 'failed', 'interrupted', 'unknown'}
            and Path(state['workspace']).is_dir()):
        changed = protected_changes(state)
        if changed:
            raise ProtectedSourceChanged('Previous Codex protected source/input changed: ', changed)
        # These are source snapshots for a new repair, not a terminal receipt
        # or approval of an interrupted/unknown native turn.
        return partial(state)
    return copy.deepcopy(state.get('partial_delivery') or {})


def repair_inputs(engine, run_id, saved, job, repair, *, bundle=None):
    """Salvage captured editable inputs from a rejected terminal job into a new workspace."""
    from .code_bundle import relative_file
    from .dataset_experiments import _base_bundle, _source_gate
    try:
        return previous_files(job), None
    except ProtectedSourceChanged as exc:
        changed = exc.changes
    ref = saved.get('previous_setup') or {}
    archive = engine.store.get('experiment_evidence', ref.get('id', '')) or {}
    history = archive.get('summary', {})
    old = history.get('setup', {})
    failure = history.get('failure', {})
    snapshot = job.get('partial_delivery', {})
    material = {k: v for k, v in (old.get('draft') or {}).items()
                if k in ('configuration_json', 'adapter_files', 'checks', 'budget_plan')}
    material['native_files'] = snapshot
    if (not snapshot or job.get('checkpoint', {}).get('status') != 'completed'
            or job.get('run_id') != run_id or job.get('setup_id') != saved['id']
            or saved.get('previous_codex_job_id') != job['id'] or old.get('codex_job_id') != job['id']
            or old.get('id') != saved['id'] or job['identity'].get('request_sha256') != sha(old.get('request'))
            or archive.get('run_id') != run_id or sha(history) != ref.get('sha256')
            or repair.get('previous_setup') != ref or repair.get('kind') != 'codex_delivery'
            or failure.get('kind') != repair['kind'] or failure.get('details', {}).get('job_id') != job['id']
            or not any('Codex changed protected model/source or input: ' + name in failure.get('message', '') for name in changed)
            or sha(material) != saved.get('repair_progress', {}).get('material_sha256')):
        raise ValueError('No matching archived protected-file rejection and captured editable delivery')
    bundle = bundle or _base_bundle(engine, run_id, _source_gate(engine, run_id))
    originals = {r['path']: r['sha256'] for r in bundle['files']}
    if (bundle['sha256'] != job['identity']['source_bundle_sha256']
            or any(originals.get(name) != info['expected_sha256'] or name.startswith('_experiment_input/')
                   for name, info in changed.items())):
        raise ValueError('Protected repair inputs cannot be rebuilt from the verified source bundle')
    editable = {}
    outputs = {'_experiment_delivery/' + name for name in ('configuration.json', 'checks.py', 'rationale.txt', 'budget_plan.json')}
    for name, text in snapshot.items():
        relative_file(name)
        if name in job['protected'] or not (name in outputs or name.startswith('dataset_runtime/') and name.endswith('.py')):
            continue
        if _text(Path(job['workspace']) / name, 320000) != text:
            raise ValueError('Previously captured editable file no longer matches disk: ' + name)
        editable[name] = text
    if not editable:
        raise ValueError('No authenticated editable inputs in the rejected delivery')
    return editable, {'source_bundle_sha256': bundle['sha256'], 'previous_job_id': job['id'],
                      'previous_setup': ref, 'captured_snapshot_sha256': sha(snapshot),
                      'changed_files': changed, 'retained_editable_files': sorted(editable),
                      'instruction': '新目录内列出的受保护文件均来自冻结原件；只修复适配层、配置交付及 CPU 断言对它们的消费。'}


async def deliver(engine, harness, run_id, saved):
    from .dataset_experiments import COLLECTION as SETUPS
    from .dataset_configuration_repair import NeedsRepair
    from .local_codex import LocalCodexClient, LocalCodexError
    from .codex_accounting import capture_usage
    from .engine import ResearchBlocked
    engine.check_active(run_id)
    state = engine.store.get(COLLECTION, saved.get('codex_job_id', ''))
    if state is None:
        state = await owned_to_thread(prepare, engine, run_id, saved)
    engine.check_active(run_id)
    saved.update(codex_job_id=state['id'], status='implementing_configuration')
    engine.store.put(SETUPS, saved['id'], saved)
    from . import code_model_migration as worker_models
    profile = worker_models.job_profile(engine, run_id, state)
    if state.get('draft'):
        return copy.deepcopy(state['draft'])
    selected = {**state['selection'], 'model': profile['model'] or None, 'effort': profile['effort'] or None}
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
    worker_models.job_profile(engine, run_id, state)
    engine.check_active(run_id)
    if receipt['status'] != 'completed':
        if receipt['status'] in {'failed', 'interrupted'}:
            state['partial_delivery'] = await owned_to_thread(partial, state)
            engine.store.put(COLLECTION, state['id'], state)
            harness.intelligence._settle(state['reservation'], {}, 0, verified=False, status=receipt['status'],
                provenance='local_codex_app_server', cost_status='not_reported_by_local_codex')
            raise NeedsRepair('Codex 配置实现回合已终止：' + str(receipt.get('error_code') or receipt['status']),
                kind='codex_delivery', details={'job_id': state['id'], 'terminal_status': receipt['status']})
        raise ResearchBlocked('本机 Codex 配置交付尚未完成；保留原回合及工作文件，恢复时读取原回执。')
    harness.intelligence._settle(state['reservation'], {}, 0, verified=False, status='completed',
        provenance='local_codex_app_server', cost_status='not_reported_by_local_codex')
    try:
        state['partial_delivery'] = await owned_to_thread(partial, state)
        engine.store.put(COLLECTION, state['id'], state)
        draft = await owned_to_thread(collect, state)
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
