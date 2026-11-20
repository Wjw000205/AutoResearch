"""Readable code workspaces, with task identity in metadata instead of names."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

OUTPUTS = '代码输出'
CODE = '代码'
OWNER = 'workspace-owner.json'
KINDS = {'implementation': '代码实现', 'runtime_repair': '运行修复'}


def _plain(path):
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('Codex工作目录及父目录不能使用链接或junction')


def task_name(title):
    value = unicodedata.normalize('NFKC', str(title or '研究任务'))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')[:32].rstrip(' .') or '研究任务'
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value, re.I):
        value = '任务-' + value
    return value


def checked(project_root, run_id, workspace, *, create=False):
    project, workspace = Path(project_root), Path(workspace)
    if not project.is_absolute() or not workspace.is_absolute() or not project.is_dir():
        raise ValueError('Codex项目必须是已存在的绝对目录')
    _plain(project)
    _plain(workspace)
    project, workspace = project.resolve(), workspace.resolve()
    legacy = project / '.autoresearch' / run_id
    if workspace.is_relative_to(legacy):
        parts = workspace.relative_to(legacy).parts
        if len(parts) != 2 or parts[-1] != 'workspace' or not re.fullmatch(r'[0-9a-f]{64}', parts[0]):
            raise ValueError('Codex工作目录缺少任务工作流隔离标识')
    else:
        base = project / OUTPUTS
        if not workspace.is_relative_to(base):
            raise ValueError('Codex工作目录不属于当前项目和任务')
        parts = workspace.relative_to(base).parts
        if (len(parts) != 3 or parts[-1] != CODE or task_name(parts[0]) != parts[0]
                or not re.fullmatch(r'第[1-9]\d*轮-(代码实现|运行修复)-版本[1-9]\d*', parts[1])):
            raise ValueError('Codex工作目录缺少任务工作流隔离标识')
        marker = workspace.parent / OWNER
        _plain(marker)
        try:
            owner = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError('Codex可读工作目录缺少有效归属记录') from exc
        if (not isinstance(owner, dict) or type(owner.get('version')) is not int or owner['version'] != 1
                or owner.get('run_id') != run_id or not isinstance(owner.get('workflow_id'), str)
                or not owner['workflow_id'] or type(owner.get('cycle')) is not int or owner['cycle'] < 1
                or owner.get('kind') not in KINDS
                or not parts[1].startswith(f"第{owner['cycle']}轮-{KINDS[owner['kind']]}-版本")):
            raise ValueError('Codex可读工作目录归属与当前研究不符')
    if create:
        workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def allocate(project_root, run_id, workflow_id, title, cycle, kind):
    project = Path(project_root)
    if not project.is_absolute() or not project.is_dir():
        raise ValueError('Codex项目必须是已存在的绝对目录')
    _plain(project)
    if type(cycle) is not int or cycle < 1 or kind not in KINDS or not workflow_id:
        raise ValueError('Codex工作目录需要有效任务、轮次和阶段')
    parent = project.resolve() / OUTPUTS / task_name(title)
    _plain(parent)
    parent.mkdir(parents=True, exist_ok=True)
    owner = {'version': 1, 'run_id': run_id, 'workflow_id': workflow_id, 'cycle': cycle, 'kind': kind}
    for version in range(1, 10001):
        directory = parent / f'第{cycle}轮-{KINDS[kind]}-版本{version}'
        try:
            directory.mkdir()
        except FileExistsError:
            _plain(directory)
            marker = directory / OWNER
            _plain(marker)
            try:
                existing = json.loads(marker.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue  # Never claim a pre-existing or incompletely created directory.
            if existing != owner:
                continue
        else:
            with (directory / OWNER).open('x', encoding='utf-8') as stream:
                json.dump(owner, stream, ensure_ascii=False, indent=2)
        return checked(project, run_id, directory / CODE, create=True)
    raise ValueError('Codex可读工作目录版本数量超限')
 
 
def isolate_busy_writer(engine, run_id, state, attempt):
    """Recover repeated pre-submission writer conflicts from the last reviewed files."""
    import hashlib
    from .codex_workflow import _verify_snapshot, _fail
    from .local_codex import LocalCodexClient
    from .storage import uid
    index=attempt['index']
    key,cycle,kind=state['id'],state['cycle'],state['kind']
    run=engine.store.get('runs',run_id)
    checkpoint=attempt.get('checkpoint') or {}
    original=checkpoint.get('code_resume_source') or {}
    errors = checkpoint.get('rpc_errors') or []
    if (index>0 and attempt.get('undispatched_writer') is None and checkpoint.get('status')=='blocked'
            and errors and errors[-1].get('method')=='thread/resume' and errors[-1].get('code')==-32600
            and errors[-1] not in original.get('rpc_errors',[]) and checkpoint.get('error_code')=='codex_thread_writer_busy'
            and original.get('error_code')=='codex_thread_writer_busy' and LocalCodexClient.unsubmitted_code_resume(checkpoint)
            and LocalCodexClient.unsubmitted_code_resume(original)):
        # The native thread refused two resumes before turn/start. Continue
        # from the last reviewed bytes without writing to the occupied tree.
        from .delivery_files import copy_verified
        from .code_model_migration import check_receipt, profile as writer_profile
        migration=check_receipt(engine,run_id,state)
        if (migration and migration['version']==2 and migration['workflow_id']==key
                and index==migration['source_snapshot']['workflow']['current_revision']):
            from .engine import ResearchBlocked
            raise ResearchBlocked('这次模型迁移已绑定原写码操作，需原线程释放写入后恢复。')
        expected_profile=writer_profile(engine,run_id,state,attempt)
        prior=state['attempts'][index-1]
        if (prior.get('status')!='rejected' or not prior.get('review_votes') or not prior.get('snapshot')
                or any(checkpoint.get(k)!=original.get(k) for k in ('run_id','operation_id','thread_id','workspace',
                    'project_root','requested_model','requested_effort','instruction_sha256'))
                or checkpoint['operation_id']!=attempt['operation_id'] or checkpoint['instruction_sha256']!=attempt['instruction_sha256']
                or checkpoint.get('project_root')!=state['project_root'] or checkpoint.get('workspace')!=attempt.get('writer_workspace',state['workspace'])
                or checkpoint.get('requested_model')!=(expected_profile['model'] or None)
                or checkpoint.get('requested_effort')!=(expected_profile['effort'] or None)
                or checkpoint['run_id']!=run_id or checkpoint['thread_id']!=state['thread_id']):
            _fail('占用的写码请求无法与其未提交回执和上一份审核快照绑定。')
        snapshot=prior['snapshot']
        _verify_snapshot(engine,run_id,snapshot)
        token=key+':writer:'+attempt['operation_id']
        workspace=allocate(state['project_root'],run_id,token,run.get('title'),cycle,kind)
        for file in snapshot['files']:
            copy_verified(Path(snapshot['workspace'])/file['path'],workspace/file['path'],
                                  {k:file[k] for k in ('sha256','bytes')})
        attempt['undispatched_writer']={'checkpoint':checkpoint,'instruction':attempt['instruction'],
            'snapshot_sha256':snapshot['files_hash']}
        attempt.pop('checkpoint')
        instruction=attempt['instruction']+'\n本次修订的可写工作目录为 '+str(workspace)+'。已有源码来自上一份审核快照；原工作目录和批准文档只读。'
        attempt.update(operation_id=uid(),writer_workspace=str(workspace),instruction=instruction,
                       instruction_sha256=hashlib.sha256(instruction.encode()).hexdigest())
        return str(workspace)
