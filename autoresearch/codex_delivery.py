"""Local, source-bound delivery checks, separate from scientific measurements."""
from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .config import ROOT
from .lab import ExperimentRunner, assess_resources, hardware_profile
from .python_environments import apply_execution_preferences
from .storage import now, redact


def validate_project_dir(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError('Codex 项目工作目录不能为空')
    path = Path(value.strip()).expanduser()
    if not path.is_absolute() or not path.is_dir() or path == Path(path.anchor):
        raise ValueError('Codex 项目工作目录必须是已存在的绝对目录，不能是磁盘根目录')
    for part in [path, *path.parents]:
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('Codex 项目目录不能经由符号链接或目录联接')
    return str(path.resolve())


async def codex_status(project_dir: str | None = None) -> dict:
    """Version-only inspection: no login secret access, threads or model requests."""
    from .local_codex import spawn_codex, LocalCodexBlocked
    selected = Path(project_dir or ROOT)
    result = {'available': False, 'version': None, 'authenticated': None,
              'checked_at': now(), 'integration': 'app-server-stdio',
              'project': {'name': selected.name, 'root': str(selected)}, 'project_scope': 'saved_defaults',
              'service_project': {'name': ROOT.name, 'root': str(ROOT)}}
    proc = None
    try:
        proc, discovery = await spawn_codex('--version',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, 'CREATE_NO_WINDOW') else {}))
        result.update(executable=discovery['executable'], discovery_source=discovery['source'])
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
        except asyncio.TimeoutError:
            result['error'] = 'Codex 版本探测超时，尚未验证登录或模型可用性。'
            return result
        version = out.decode('utf-8', errors='replace').strip()[:200]
        if proc.returncode != 0 or not re.fullmatch(r'codex-cli [\w.+-]+', version):
            result['error'] = 'Codex 版本探测失败，尚未启动代码任务。'
            return result
        return {**result, 'available': True, 'version': version,
                'note': '仅验证本机程序可启动；登录、模型和侧栏项目归属需以实际会话回执为准。'}
    except LocalCodexBlocked as exc:
        result['error_code'] = exc.code
        result['error'] = ({'codex_not_installed': '未在 PATH 或本机安装目录找到 Codex。可在 .env.local 中设置 AUTORESEARCH_CODEX_EXECUTABLE 的绝对路径。',
            'codex_executable_invalid': '配置的 Codex 程序路径无效，请检查 AUTORESEARCH_CODEX_EXECUTABLE；未替换为其他版本。'}
            .get(exc.code, '无法启动本机 Codex 程序，请检查程序路径与执行权限。'))
        return result
    except OSError:
        result['error'] = '无法启动本机 Codex 程序。'
        return result
    finally:
        if proc is not None:
            from .local_codex import close_owned_process, LocalCodexError
            primary_error = sys.exception()
            try:
                await close_owned_process(proc)
            except (LocalCodexError, OSError):
                if primary_error is not None:
                    primary_error.add_note('codex_process_shutdown_failed')
                elif result.get('error'):
                    result['cleanup_error_code'] = 'codex_process_shutdown_failed'
                else:
                    raise


class CheckStore:
    """Reuse bounded execution without adding smoke scores to research trials."""
    names = {'experiments': 'codex_checks', 'experiment_attempts': 'codex_check_attempts'}

    def __init__(self, store):
        self.store = store

    def get(self, collection, *args, **kwargs):
        return self.store.get(self.names.get(collection, collection), *args, **kwargs)

    def list(self, collection, *args, **kwargs):
        return self.store.list(self.names.get(collection, collection), *args, **kwargs)

    def put(self, collection, *args, **kwargs):
        return self.store.put(self.names.get(collection, collection), *args, **kwargs)

    def batch(self, puts, deletes=()):
        return self.store.batch([(self.names.get(c, c), k, v) for c, k, v in puts],
                                [(self.names.get(c, c), k) for c, k in deletes])

    def event(self, run_id, kind, message, data):
        return self.store.event(run_id, kind.replace('experiment.', 'codex.check.'), message, data)


def inspect_delivery(directory: Path, spec: dict) -> dict:
    """Read actual bytes and declared requirement pointers; declarations are not proof."""
    directory = Path(directory)
    issues, files = [], []
    if not directory.is_dir():
        return {'approved': False, 'issues': ['尚未生成交付目录'], 'files': []}
    from .delivery_files import regular_files, identity
    try:
        files = [{'path': path.relative_to(directory).as_posix(), **identity(path), 'verified': True}
                 for path in regular_files(directory)]
    except (ValueError, OSError) as exc:
        issues.append(str(exc))
    code, manifest, runtime_bundle = '', {}, None
    known = {f['path']: f for f in files}
    if 'experiment.py' not in known or 'implementation.json' not in known:
        issues.append('必须实际交付 experiment.py 和 implementation.json')
    if not issues:
        try:
            code = (directory / 'experiment.py').read_bytes().decode('utf-8')
            manifest = json.loads((directory / 'implementation.json').read_text(encoding='utf-8'))
            if not isinstance(manifest, dict):
                raise ValueError('manifest')
            tree = ast.parse(code)
            if not tree.body:
                issues.append('实验脚本为空')
            from .code_bundle import source_issues
            issues.extend(source_issues(code))
            dependencies = manifest.get('dependencies')
            if not isinstance(dependencies, list) or len(dependencies) > 40 or any(not isinstance(d, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:[<>=!~]+[A-Za-z0-9.*+_-]+)?', d) for d in dependencies):
                issues.append('依赖清单缺失或含无效依赖声明')
            if not isinstance(manifest.get('explanation'), str) or not manifest['explanation'].strip():
                issues.append('缺少实际实现说明')
            required = {c['id'] for c in spec.get('acceptance_checks', []) if isinstance(c, dict) and isinstance(c.get('id'), str)}
            rows = manifest.get('requirements')
            if not isinstance(rows, list):
                rows = []
            mapped, seen = set(), set()
            for row in rows:
                if not isinstance(row, dict) or row.get('id') not in required:
                    issues.append('交付声明包含无效验收项')
                    continue
                if row['id'] in seen:
                    issues.append(f'交付声明重复验收项 {row["id"]}；每个验收项只能有一个文件映射记录')
                seen.add(row['id'])
                locations = row.get('locations')
                if not isinstance(locations, list) or not locations or any(not isinstance(loc, str) or loc.split(':', 1)[0] not in known for loc in locations):
                    issues.append(f'验收项 {row["id"]} 缺少实际文件位置')
                elif not isinstance(row.get('verification'), str) or not row['verification'].strip():
                    issues.append(f'验收项 {row["id"]} 缺少验证说明')
                else:
                    mapped.add(row['id'])
            if not required or required - mapped:
                issues.append('尚未对所有规范验收项提供文件映射与验证说明')
            from .code_bundle import describe
            runtime_bundle = describe(directory, manifest, files)
        except (UnicodeError, ValueError, SyntaxError, OSError) as exc:
            issues.append('交付文件解析或语法检查失败：' + type(exc).__name__ + ': ' + str(exc))
    delivery_hash = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'approved': not issues, 'issues': issues, 'files': files, 'delivery_hash': delivery_hash,
            'code_sha256': hashlib.sha256(code.encode()).hexdigest() if code else None,
            'code': code, 'dependencies': manifest.get('dependencies', []),
            'explanation': manifest.get('explanation', ''), 'requirements': manifest.get('requirements', []),
            'runtime_bundle': runtime_bundle,
            'scope': '文件与声明核对；映射、静态检查和运行成功均不能单独证明方法正确。'}


async def verify_codex_delivery(engine, run_id, workspace, spec, *, cancel=None, on_progress=None, yaml_protocol=0) -> dict:
    def report(step, label, **details):
        if on_progress:
            on_progress({'step': step, 'label': label, **details})
    report('files', '核对交付文件、清单和代码语法')
    engine.check_active(run_id)
    from .architecture_validation import operation
    if hasattr(engine, '_read_consensus_check'):
        from .codex_workflow import _read_execution_gate
        await _read_execution_gate(engine, run_id)
    else:
        await asyncio.to_thread(operation(engine._execution_gate), run_id)
        engine.check_active(run_id)
    directory = Path(workspace)
    root = engine.run_dir(run_id).resolve()
    if not directory.resolve().is_relative_to(root):
        raise ValueError('Codex 验收只接受当前研究的不可变交付快照')
    for part in [directory, *directory.parents]:
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('交付快照不能通过符号链接或目录联接')
        if part == root:
            break
    result = await asyncio.to_thread(inspect_delivery, directory, spec)
    result['smoke'] = []
    result['runtime_sha256'] = (result.get('runtime_bundle') or {}).get('sha256') or result.get('code_sha256')
    if not result['approved']:
        return result
    yaml_program = None
    if yaml_protocol == 1:
        from .yaml_migration import inspect_contract
        try:
            contract = inspect_contract(result.get('runtime_bundle') or {'files': []})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return {**result, 'approved': False, 'issues': ['YAML 超参数交付合同未通过：' + str(exc)]}
        yaml_program = {'code': result['code'], 'revision': 0,
            'hyperparameter_yaml_protocol': 1, 'hyperparameter_contract': contract}
    from . import code_verification
    admission = code_verification.policy(engine, run_id)
    if admission:
        from . import definition_code_followup as followup
        state = engine.store.get('codex_workflows',admission['workflow_id'],{})
        specification = next((r for r in state.get('specifications',[]) if r.get('approved')
                              and r.get('spec')==spec),{})
        if followup.applicable(state,specification):
            result.update(approved=False, definition_checks_deferred=True,
                scope='补充定义交付的静态检查；独立入口复审和新增非训练计时仍待完成，未重跑旧测试。')
            result['issues'].append('新增资源计时代码等待独立复审和宿主一次性取证。')
            return result
        report('bounded_checks', '宿主执行已批准的有界代码检查，不启动完整模型训练')
        receipt = await code_verification.run(engine, run_id, directory, cancel=cancel)
        after = await asyncio.to_thread(inspect_delivery, directory, spec)
        result['host_verification'] = receipt
        result['bounded_checks_only'] = True
        result['scope'] = '同冻结源码的宿主有界断言；不是完整分支训练、资源探针或科研成功。'
        if not receipt['passed']:
            result['issues'].append('宿主有界代码检查未通过：'+receipt.get('error',receipt['status']))
        if after['delivery_hash'] != result['delivery_hash']:
            result['issues'].append('有界检查期间冻结交付发生变化')
        result['approved'] = not result['issues']
        return result
    run = engine.store.get('runs', run_id)
    report('plan', '核对冻结实验计划与共同任务参数')
    @operation
    def read_inputs():
        return engine.outputs(run_id)['design'], engine.experiment_plan(run_id)
    design, plan = await asyncio.to_thread(read_inputs)
    engine.check_active(run_id)
    if not plan or not plan.get('validation_pairs') or not plan.get('ablation_candidates'):
        return {**result, 'approved': False, 'issues': ['缺少完整冻结实验计划，不能选择代码验收参数。']}
    from .fair_comparison import validate_plan, validate_invocation, strict_equal
    try:
        validate_plan(plan, design)
    except ValueError as exc:
        return {**result, 'approved': False, 'issues': ['验收的共同任务条件未对齐：' + str(exc)]}
    from . import comparison_transport as comparison
    transport = await asyncio.to_thread(operation(comparison.runtime_transport), engine, run_id, spec=spec)
    result['protocol_repair'] = transport.get('protocol_repair') if transport else None
    result['validation_policy_sha256'] = comparison.digest({'contract': transport.get('contract') if transport else None,
        'evaluation_contract_version': run['config'].get('evaluation_contract_version'), 'delivery_verifier': 2})
    engine.check_active(run_id)
    resource = {**design['resource_plan'], 'required_packages': sorted(set(
        [*design['resource_plan'].get('required_packages', []), *result['dependencies']]))}
    report('resources', '检查本机资源和运行依赖')
    measured = await asyncio.to_thread(hardware_profile)
    measured = await asyncio.to_thread(apply_execution_preferences, run['config'], measured)
    admitted = assess_resources(resource, measured)
    if not admitted['approved']:
        return {**result, 'approved': False, 'issues': ['当前资源或依赖不满足验收要求', *admitted.get('reasons', [])]}
    limits = {**admitted.get('limits', {}), 'execution_mode': run['config'].get('execution_mode', 'local'),
              'required_packages': resource['required_packages']}
    from . import codex_execution_reuse as reuse
    replay = reuse.replay_policy(engine, run_id, result)
    if replay:
        limits['historical_execution_policy'] = replay
    # These are the frozen full branch invocations, not reduced smoke inputs.
    # Respect the admitted plan and user ceiling; an unrelated 120-second cap
    # made correctly scoped training fail before its approved resource deadline.
    limits['timeout_seconds'] = min(float(limits.get('timeout_seconds', 120)), run['config'].get('experiment_timeout', 120))
    if result.get('runtime_bundle'):
        limits['code_bundle'] = result['runtime_bundle']
    if transport:
        try:
            comparison.io_limits(limits)
        except ValueError as exc:
            return {**result, 'approved': False, 'issues': [str(exc)]}
        limits['comparison_transport'] = transport
    runner = ExperimentRunner(CheckStore(engine.store), root / 'codex-acceptance')
    # Use one prespecified validation configuration only. Held-out tests remain
    # reserved for the existing frozen final-evaluation stage.
    first_pair = plan['validation_pairs'][0]
    first_ablation = plan['ablation_candidates'][0]
    pair_seed = first_pair.get('seed', plan['selection_seed'])
    checks = [(first_pair['baseline'], pair_seed), (first_pair['proposed'], pair_seed),
              (first_ablation, first_ablation.get('seed', plan['selection_seed']))]
    if transport:
        # Smoke each branch only as a real producer; do not fabricate reference
        # records or execute the entire scientific matrix inside acceptance.
        producers = [params for _, params in comparison.ordered_validation(plan, transport['contract'])[:-1]]
        if len(producers) == 2:
            checks = [(p, p['seed']) for _, p in comparison.ordered_validation(plan, transport['contract'])]
        else:
            checks = []
            for variant in ('baseline', 'proposed', 'ablation'):
                candidate = next((p for p in producers if p['variant'] == variant), None)
                if candidate is None:
                    return {**result, 'approved': False, 'issues': ['运输计划没有用于三分支固定验收的生产者调用，不能伪造比较参照。']}
                checks.append((candidate, candidate['seed']))
    fingerprints = None
    transport_records = []
    for check_number, (frozen_params, seed) in enumerate(checks, 1):
        engine.check_active(run_id)
        if cancel and cancel.is_set():
            raise asyncio.CancelledError()
        params = {**copy.deepcopy(frozen_params), 'phase': 'validation', 'seed': seed}
        try:
            validate_invocation(plan, params)
        except ValueError as exc:
            result['issues'].append('验收的共同任务条件未对齐：' + str(exc))
            break
        if transport:
            try:
                params = await asyncio.to_thread(comparison.prepare_inputs, transport, params, transport_records)
            except ValueError as exc:
                result['issues'].append('运输验收输入未闭合：' + str(exc))
                break
            engine.check_active(run_id)
        # A repaired host runtime must not reuse an old failed execution receipt.
        # Its identity is separate; the previous immutable check remains history.
        from . import codex_execution_reuse as reuse
        if yaml_program:
            from .hyperparameter_yaml import prepare_trial
            limits['hyperparameter_yaml'] = prepare_trial(engine, run_id, yaml_program, params, use_pending=False)
        check_id = 'codex-' + reuse.execution_identity(result, params, limits)[:24]
        variant = params.get('variant', 'unknown')
        label = {'baseline': '基线', 'proposed': '候选方法', 'ablation': '消融'}.get(variant, variant)
        report('smoke', f'运行{label}分支验收（{check_number}/{len(checks)}）',
               check_id=check_id, variant=variant, completed=len(result['smoke']), total=len(checks),
               timeout_seconds=limits['timeout_seconds'], checks=copy.deepcopy(result['smoke']))
        try:
            record = await asyncio.to_thread(reuse.find, engine, run_id, result, params, limits)
        except (ValueError, OSError) as exc:
            result['issues'].append('已保存运行证据无法复用：' + str(exc))
            result['failure_kind'] = 'codex_execution_evidence_invalid'
            break
        if record:
            report('contract', f'复用{label}已保存结果，重新核验输出协议（{check_number}/{len(checks)}）',
                check_id=record['id'], variant=variant, completed=len(result['smoke']), total=len(checks))
        elif replay:
            result['issues'].append('仅复用核验未找到与当前交付、参数和原授权资源一致的完整证据；已停止，不启动新运行。')
            result['failure_kind'] = 'codex_execution_evidence_invalid'
            break
        else:
            record = await runner.run(run_id, result['code'], params, check_id, limits, cancel)
        report('contract', f'核对{label}分支的指标、数据划分和输出契约',
               check_id=check_id, variant=variant, completed=len(result['smoke']), total=len(checks))
        if transport:
            evidence, errors = await asyncio.to_thread(operation(engine.check_evaluation_contract), run_id, record, params)
            engine.check_active(run_id)
        else:
            evidence, errors = engine.check_evaluation_contract(run_id, record, params)
        if yaml_program:
            from .hyperparameter_yaml import check_result
            errors.extend(check_result(record, limits['hyperparameter_yaml']))
        from .execution_diagnostics import failure_diagnostic, failure_message
        diagnostic = failure_diagnostic(record)
        if diagnostic:
            # A deliberate resource/contract rejection is the root cause. Keep
            # the secondary protocol errors, but don't hide the actual failure
            # behind missing metrics from a process that exited unsuccessfully.
            errors = [failure_message(diagnostic), *errors]
        if transport and record.get('status') == 'completed':
            try:
                record['comparison_transport'] = await asyncio.to_thread(comparison.validate_result, transport, params, record)
                engine.check_active(run_id)
                if params['comparison_inputs']['role'] != 'producer':
                    for criterion in plan.get('acceptance', {}).get('criteria', []):
                        comparison.acceptance_value(evidence, criterion['metric'], criterion)
            except ValueError as exc:
                errors.append(str(exc))
        metric = design['primary_metric']
        value = record.get('metrics', {}).get(metric)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            errors.append(f'没有返回所需有限数值指标 {metric}')
        if any(k.startswith('test_') for k in record.get('metrics', {})):
            errors.append('验证阶段错误地返回独立测试指标')
        if isinstance(evidence, dict):
            if not strict_equal(evidence.get('actual_params'), params):
                errors.append('actual_params 与实际输入不一致')
            from .split_identity import compatible
            identity = evidence
            if fingerprints is not None and not compatible(identity, fingerprints):
                errors.append('不同分支的数据或划分不一致')
            fingerprints = identity
        else:
            errors.append('缺少数据与划分来源声明')
        if record['status'] != 'completed':
            errors.append('本地运行未完成：' + record['status'])
        if transport and not errors:
            transport_records.append(record)
        result['smoke'].append(redact({k: record.get(k) for k in
            ['id', 'status', 'exit_code', 'duration_seconds', 'stdout', 'stderr', 'error', 'params', 'metrics', 'artifact_path']} | {
                'command': [limits.get('python_executable') if limits['execution_mode'] == 'local' else 'python', '-I', 'bootstrap.py', 'bootstrap.json'], 'passed': not errors,
                'execution_reuse': record.get('execution_reuse'),
                'issues': errors, **({'failure_diagnostic': diagnostic} if diagnostic else {}),
                'scope': '验证分支运行检查，非科研实验结果、完整测试覆盖或最终测试。'}))
        result['issues'].extend(errors)
        report('branch_done', f'{label}分支验收' + ('未通过' if errors else '已通过'),
               completed=len(result['smoke']), total=len(checks), checks=copy.deepcopy(result['smoke']))
        if any(error.startswith('Undeclared comparison array:') for error in errors):
            result['failure_kind'] = 'codex_protocol_conflict'
            break  # Same host schema conflict affects all branches; do not retrain them.
        if errors:
            break  # The acceptance batch already failed; do not run more training.
    report('integrity', '复核验收前后的交付文件一致性',
           completed=len(result['smoke']), total=len(checks), checks=copy.deepcopy(result['smoke']))
    after = await asyncio.to_thread(inspect_delivery, directory, spec)
    if after['delivery_hash'] != result['delivery_hash']:
        result['issues'].append('验收期间交付文件发生变化，须重新提交评审')
    result['approved'] = not result['issues']
    return result


def local_codex_view(engine, run_id) -> dict:
    """Current indexed-generation metadata; paid request bodies stay offline."""
    from . import implementation_progress as progress
    run = engine.store.get('runs', run_id) or {}
    generation = run.get('state', {}).get('implementation_generation')
    generation_filter = " AND json_extract(data,'$.implementation_generation')=?" if generation else ''
    fields = ('id', 'run_id', 'cycle', 'kind', 'status', 'blocked_kind', 'proposal_hash', 'spec_hash',
        'implementation_generation', 'implementation_indexed_review_protocol', 'current_revision',
        'project_root', 'workspace', 'thread_id', 'model', 'reasoning_effort', 'created_at', 'updated_at')
    columns = ','.join(f"'{key}',json_extract(data,'$.{key}')" for key in fields)
    records = progress._project(engine.store, f"""SELECT json_object({columns},
        'reason',substr(json_extract(data,'$.reason'),1,2000)) FROM records
        WHERE collection='codex_workflows' AND json_extract(data,'$.run_id')=?
            AND json_type(data,'$.implementation_indexed_review_protocol')='integer'
            AND json_extract(data,'$.implementation_indexed_review_protocol')=1
            {generation_filter}
        ORDER BY json_extract(data,'$.cycle') DESC,updated_at DESC,key DESC LIMIT 20""",
        (run_id,generation) if generation else (run_id,))
    records.reverse()
    current = dict(records[-1]) if records else None
    checks = []
    if current:
        rows = progress._specifications(engine.store, current['id'], revision=current.get('current_revision'))
        # Only the active immutable document is exposed in the default panel.
        current['specifications'] = rows[-1:] if rows else []
        current['document'] = (rows[-1] if rows else {}).get('document')
        attempt = progress._attempt(engine.store, current['id'])
        current['attempts'] = []
        if attempt:
            checkpoint_fields = ('status', 'thread_id', 'turn_id', 'turn_dispatched_at', 'workspace',
                'reported_model', 'model', 'reported_effort', 'project_id', 'last_event_at', 'error_code',
                'recovered_read_only', 'usage', 'usage_observed_at')
            checkpoint_columns = ','.join(f"'{key}',json_extract(a.value,'$.checkpoint.{key}')"
                                          for key in checkpoint_fields)
            metadata = progress._project(engine.store, f"""SELECT json_object(
                'operation_id',json_extract(a.value,'$.operation_id'),
                'confirmed_terminal_at',json_extract(a.value,'$.confirmed_terminal_at'),
                'resumed_by_generation',json_extract(a.value,'$.resumed_by_generation'),
                'checkpoint',json_object({checkpoint_columns}))
                FROM records w,json_each(w.data,'$.attempts') a
                WHERE w.collection='codex_workflows' AND w.key=?
                ORDER BY CAST(a.key AS INTEGER) DESC LIMIT 1""", (current['id'],))
            attempt.update(metadata[0] if metadata else {})
            checkpoint = attempt.setdefault('checkpoint', {})
            checkpoint.update({key: attempt.pop(key) for key in ('activities', 'public_messages', 'event_counts')})
            attempt['review_votes'] = attempt.pop('reviews')
            current['attempts'] = [attempt]
            current['previous_attempt'] = progress.previous_code_attempt(engine.store, current['id'], attempt['index'])
            check_ids = [item['id'] for item in attempt.get('acceptance', {}).get('smoke', []) if item.get('id')]
            if check_ids:
                checks = progress._project(engine.store, """SELECT json_object('id',key,
                    'status',json_extract(data,'$.status'),'exit_code',json_extract(data,'$.exit_code'),
                    'duration_seconds',json_extract(data,'$.duration_seconds'),
                    'error',substr(json_extract(data,'$.error'),1,2000)) FROM records
                    WHERE collection='codex_checks' AND json_extract(data,'$.run_id')=?
                        AND key IN (SELECT value FROM json_each(?)) ORDER BY updated_at,key""",
                    (run_id,json.dumps(check_ids)))
    config = run.get('config', {})
    return redact({'backend': config.get('implementation_backend', 'local_codex'),
        'document_protocol_required': True,
        'implementation_indexed_review_protocol': config.get('implementation_indexed_review_protocol'),
        'execution_mode': 'full_codex_agent', 'project_dir': config.get('codex_project_dir', str(ROOT)),
        'configuration_locked': bool(records or engine.store.get('stage_contexts', f'{run_id}:implementation')),
        'workflows': records, 'current': current, 'checks': checks,
        'projection_only': True,
        'cost_note': 'Codex 使用本机登录；仅记录接口返回用量，金额未核实，不记为零费用，也不套用三路代理价格。'})


def cleanup_codex_workspaces(engine, run_id) -> None:
    """Delete only controller-owned task workspaces after the run deletion gate."""
    from .deletion import DeletionError, validate_tree
    if not engine.store.get('run_deletions', run_id):
        raise DeletionError('Codex 工作目录清理需要研究删除标记')
    targets = set()
    for row in engine.store.list('codex_workflows', run_id):
        if row.get('run_id') != run_id or not row.get('workspace'):
            continue
        project = Path(row.get('project_root', ''))
        workspace = Path(row['workspace'])
        target = workspace.parent
        validate_project_dir(str(project))
        if not target.exists():
            validate_tree(target, project)
            continue  # A retried deletion may have removed this owned version already.
        from .codex_workspace import checked
        try:
            checked(project, run_id, workspace)
        except ValueError as exc:
            raise DeletionError('Codex 清理拒绝不属于该研究的工作目录') from exc
        validate_tree(target, project)
        targets.add((target, project))
    for target, project in targets:
        validate_tree(target, project)
        if target.exists():
            shutil.rmtree(target)
