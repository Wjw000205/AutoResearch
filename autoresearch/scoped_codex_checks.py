"""Correct a native CPU-check entry while retaining its delivered runtime files."""
import ast
import copy
import hashlib
from pathlib import Path

from .delivery_files import io_path
from .experiment_workflow import sha
from .storage import now, uid

INSTRUCTION = '''修正 _delivery/checks.py 的宿主入口契约，保留既有全部行为检查及候选运行文件。
检查由宿主在同一 Python 进程运行：必须有实际 Python assert，helper 可以使用 assert，但不能仅用 if/raise 代替所有 assert。
所有静态 assert 均须实际成功执行。预期异常未发生时用 raise AssertionError，不使用永远失败的 assert False。
不得 subprocess/multiprocessing、自启动 Python、sys.exit、os._exit 或 raise SystemExit；正常返回后由宿主追加执行计数及成功标记。
cwd 为复制后的候选根目录，src 在 sys.path，sys.argv[1] 是宿主 code_check 信封，不是训练配置；不要改宿主输入。
需要配置时另建隔离 CPU 夹具和合法 YAML/receipt。保留默认控制等价、深度宽度实际变化、优化器行为及全部非法输入检查。
资源上限是宿主 60 秒 wall/CPU、1 CPU、1 GiB RAM、256 MiB磁盘，不沿用原自启动脚本的180秒。不得削弱检查来凑限。
只改 _delivery/checks.py 与 _delivery/rationale.txt；不得改运行源码、默认配置、changes.json、work_order.json 或检查范围。
只做语法检查，不执行完整检查或训练；完成后仍须三方独立代码审查和宿主实际 CPU 验证。'''


def new_instruction(engine, run_id, record):
    """New repair work only; retained native turns keep their exact text."""
    text = INSTRUCTION
    if record.get('decision', {}).get('architecture_review'):
        text = text.replace('保留默认控制等价、深度宽度实际变化、优化器行为及全部非法输入检查。',
            '按 work_order 中获批结构变更保留目标机制行为断言、未改控制公平性、配置与全部非法输入检查；'
            '不能要求已获批改变的目标模块保持旧输出等价。')
    from .rapid_check_budget import future_policy
    value = future_policy(engine, run_id, record)
    if value:
        seconds = min(value['timeout_seconds'], engine.store.get('runs', run_id)['config']['experiment_timeout'])
        text = text.replace('资源上限是宿主 60 秒 wall/CPU、1 CPU、1 GiB RAM、256 MiB磁盘，不沿用原自启动脚本的180秒。',
            f'本任务已保存完整 CPU 验收时限：{seconds} 秒 wall/CPU、1 CPU、1 GiB RAM、256 MiB磁盘；'
            '新候选及该资源契约仍须三方独立审核。')
    return text


def issues(source):
    try:
        tree = ast.parse(source)
        compile(tree,'checks.py','exec')
    except SyntaxError as exc:
        return ['检查入口 Python 语法不符合约定：' + exc.msg]
    found = []
    if not any(isinstance(n, ast.Assert) for n in ast.walk(tree)):
        found.append('缺少 Python assert；现有 check/raise helper 应保留行为并改成可计数断言')
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split('.')[0] in {'subprocess','multiprocessing'} for a in node.names):
            found.append('宿主检查不得启动子进程')
        if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] in {'subprocess','multiprocessing'}:
            found.append('宿主检查不得启动子进程')
        if isinstance(node, ast.Raise):
            value = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(value, ast.Name) and value.id == 'SystemExit':
                found.append('SystemExit 会跳过宿主完成回执')
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            if (node.func.value.id, node.func.attr) in {('sys','exit'),('os','_exit')}:
                found.append('提前退出会跳过宿主完成回执')
    return sorted(set(found))


async def repair(engine, harness, run_id, record, files):
    from .rapid_experiment_revision import COLLECTION, _write_files, _base_unchanged
    from .engine import ResearchBlocked
    history = record.setdefault('codex_check_repairs', [])
    original = record['codex_work_item']
    checks = files['_delivery/checks.py'].decode('utf-8')
    if not history and not issues(checks):
        return checks, files['_delivery/rationale.txt'].decode('utf-8')
    binding = sha({n:hashlib.sha256(v).hexdigest() for n,v in files.items()})
    protected = {n:raw for n,raw in files.items() if n not in {'_delivery/checks.py','_delivery/rationale.txt'}}
    while True:
        _base_unchanged(engine, run_id, record)
        if not history or history[-1].get('status') == 'needs_correction':
            previous = history[-1] if history else None
            workspace = engine.run_dir(run_id).resolve() / 'check-repairs' / sha(record['id'])[:16] / str(len(history))
            content = dict(files)
            if previous:
                content['_delivery/checks.py'] = io_path(Path(previous['workspace'])/'_delivery/checks.py').read_bytes()
                content['_delivery/rationale.txt'] = io_path(Path(previous['workspace'])/'_delivery/rationale.txt').read_bytes()
            errors = issues(content['_delivery/checks.py'].decode('utf-8'))
            _write_files(workspace, content)
            work = {'operation_id':uid(), 'workspace':str(workspace), 'source_sha256':binding,
                'source_thread_id':original.get('checkpoint',{}).get('thread_id'),
                'issues':errors, 'instruction':new_instruction(engine,run_id,record)+'\n本次入口问题：'+'；'.join(errors),
                'status':'repairing', 'created_at':now()}
            history.append(work)
            record['status'] = 'repairing_checks'
            engine.store.put(COLLECTION, record['id'], record)
            engine.store.event(run_id,'rapid.check_entry_repair_requested',
                'Codex 已交付源码；检查入口不兼容，已派发定向补正，保留原交付和批准范围。',
                {'revision_id':record['id'],'operation_id':work['operation_id'],'issues':errors})
        work = history[-1]
        if work['source_sha256'] != binding:
            raise ResearchBlocked('检查入口补正的原始交付发生变化。')
        workspace = Path(work['workspace'])
        def verify_runtime():
            for name, raw in protected.items():
                path = io_path(workspace/name)
                for part in (path,*path.parents):
                    if part.is_symlink() or getattr(part,'is_junction',lambda:False)():
                        raise ResearchBlocked('检查补正目录包含链接。')
                    if part==io_path(workspace):break
                if path.read_bytes()!=raw:
                    raise ResearchBlocked('检查入口补正修改了候选源码或原交付控制文件：'+name)
        verify_runtime()
        checkpoint=work.get('checkpoint',{})
        if (checkpoint.get('status')=='blocked' and checkpoint.get('error_code')=='codex_thread_workspace_mismatch'
                and not checkpoint.get('turn_id') and not checkpoint.get('turn_dispatched_at')):
            # Codex threads keep their original cwd. This receipt proves that
            # no model turn was submitted, so retain it and initialize a fresh
            # session for the isolated repair copy instead of replaying a turn.
            work.setdefault('initialization_history',[]).append(copy.deepcopy(checkpoint))
            work['source_thread_id']=checkpoint.get('thread_id')
            work['operation_id']=uid()
            work.pop('checkpoint',None)
            engine.store.put(COLLECTION,record['id'],record)
        def save():
            work['updated_at']=now()
            engine.store.put(COLLECTION,record['id'],record)
        def verify():
            _base_unchanged(engine, run_id, record)
            verify_runtime()
        from .scoped_codex_recovery import run as run_work_item
        await run_work_item(engine, harness, run_id, work, save, verify, label='Codex 检查入口补正')
        _base_unchanged(engine,run_id,record)
        verify_runtime()
        def read(name):
            path=io_path(workspace/name)
            if path.is_symlink() or path.stat().st_size>1024*1024:
                raise ResearchBlocked('检查补正输出无效。')
            return path.read_bytes().decode('utf-8')
        checks=read('_delivery/checks.py');rationale=read('_delivery/rationale.txt')
        errors=issues(checks)
        work.update(status='needs_correction' if errors else 'completed',issues=errors,
                    checks_sha256=hashlib.sha256(checks.encode()).hexdigest())
        engine.store.put(COLLECTION,record['id'],record)
        if not errors:return checks,rationale
