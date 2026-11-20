"""Local, source-bound delivery checks, separate from scientific measurements."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .config import ROOT
from .storage import now, redact
from .delivery_files import io_path


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


def inspect_delivery(directory: Path, spec: dict) -> dict:
    """Read actual bytes and declared requirement pointers; declarations are not proof."""
    directory = Path(directory)
    issues, files = [], []
    if not io_path(directory).is_dir():
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
            code = io_path(directory / 'experiment.py').read_bytes().decode('utf-8')
            manifest = json.loads(io_path(directory / 'implementation.json').read_text(encoding='utf-8'))
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


async def verify_codex_delivery(engine, run_id, workspace, spec, *, cancel=None, on_progress=None, yaml_protocol=0,
                                workflow_id=None) -> dict:
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
    state = engine.store.get('codex_workflows',workflow_id,{})
    specification = next((r for r in reversed(state.get('specifications', [])) if r.get('approved') is True), {})
    if state.get('run_id') != run_id or not specification or specification.get('spec') != spec:
        raise ValueError('Delivery verification requires its own approved implementation workflow')
    manifest = json.loads(io_path(directory / 'implementation.json').read_bytes())
    if manifest.get('implementation_document_sha256') != specification['document']['sha256']:
        return {**result, 'approved': False, 'issues': ['实际交付没有绑定批准的实现 Markdown SHA256']}
    if yaml_protocol == 1:
        from .yaml_migration import inspect_contract
        try:
            scope = state.get('yaml_configuration_scope')
            inspect_contract(result.get('runtime_bundle') or {'files':[]},
                             **({'expected_scope':scope} if scope else {}))
        except (ValueError,KeyError,TypeError,OSError) as exc:
            return {**result,'approved':False,'issues':['YAML 超参数交付合同未通过：'+str(exc)[:1000]]}
    from . import code_verification
    admission = code_verification.policy(engine, run_id, workflow_id=workflow_id)
    if admission:
        report('bounded_checks', '宿主执行已批准的有界代码检查，不启动完整模型训练')
        receipt = await code_verification.run(engine, run_id, directory, cancel=cancel, workflow_id=workflow_id)
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
    if workflow_id is not None and state.get('implementation_acceptance_protocol') == 1:
        # New document contracts approve definitions, not the legacy three-fit
        # smoke protocol. Keep code conformance and execution admission separate:
        # reviewers see the actual frozen source/reports, while experiment
        # dispatch still requires the existing source-bound runtime admission.
        from .implementation_documents import validate_review_snapshot
        await engine._read_consensus_check(run_id, validate_review_snapshot,
            engine, run_id, state, specification)
        engine.check_active(run_id)
        report('code_review', '文件与语法核对完成，交独立代码审查；宿主运行检查仍待准入')
        result.update(code_conformance_only=True,
            resource_admission={'approved': False, 'reason': 'current_delivery_execution_evidence_pending'},
            host_checks={'status': 'not_run', 'requirement_ids': [c['id'] for c in spec['acceptance_checks']],
                         'training_authorized': False, 'scientific_approval': False},
            scope='当前交付的文件、清单与语法核对；独立代码审查待完成。开发者测试摘要保留为原始证据，'
                  '不代表宿主 CPU/CUDA 检查、资源准入、正式训练或科研验收通过。')
        return result
    result['scope']='文件、清单和语法检查；真实实验在独立代码审核后按冻结计划派发。'
    return result


def local_codex_view(engine, run_id) -> dict:
    """Current indexed-generation metadata; paid request bodies stay offline."""
    from . import implementation_progress as progress
    run = engine.store.get('runs', run_id) or {}
    generation = run.get('state', {}).get('implementation_generation')
    generation_filter = " AND json_extract(data,'$.implementation_generation')=?" if generation else ''
    fields = ('id', 'run_id', 'cycle', 'kind', 'status', 'blocked_kind', 'proposal_hash', 'spec_hash',
        'implementation_generation', 'implementation_review_strategy','implementation_indexed_review_protocol', 'current_revision',
        'project_root', 'workspace', 'thread_id', 'model', 'reasoning_effort', 'created_at', 'updated_at')
    columns = ','.join(f"'{key}',json_extract(data,'$.{key}')" for key in fields)
    records = progress._project(engine.store, f"""SELECT json_object({columns},
        'reason',substr(json_extract(data,'$.reason'),1,2000)) FROM records
        WHERE collection='codex_workflows' AND json_extract(data,'$.run_id')=?
            AND (json_extract(data,'$.implementation_review_strategy')='document_code' OR
                 (json_type(data,'$.implementation_indexed_review_protocol')='integer'
                 AND json_extract(data,'$.implementation_indexed_review_protocol')=1))
            {generation_filter}
        ORDER BY json_extract(data,'$.cycle') DESC,updated_at DESC,key DESC LIMIT 20""",
        (run_id,generation) if generation else (run_id,))
    records.reverse()
    current = dict(records[-1]) if records else None
    checks = []
    if current:
        from .code_model_migration import projection as worker_model_projection
        current.update(worker_model_projection(engine.store, run, current))
        rows = (progress._specifications(engine.store,current['id'],latest=True)
                if current.get('implementation_review_strategy')=='document_code' else
                progress._specifications(engine.store,current['id'],revision=current.get('current_revision')))
        # Only the active immutable document is exposed in the default panel.
        current['specifications'] = rows[-1:] if rows else []
        current['document'] = (rows[-1] if rows else {}).get('document')
        if current.get('implementation_review_strategy')=='document_code':
            details = progress._cycle_detail(engine.store,run,{'workflow':current})['details']
            current['document_reviews'] = details['document_audit']['roles']
            current['code_reviews'] = details['code_delivery']['reviews']
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
            from .delivery_files import io_path
            shutil.rmtree(io_path(target))
