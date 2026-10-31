"""Host-owned capabilities shared by planning, authoring and candidate assembly."""
import ast
import hashlib
from pathlib import Path

from .experiment_workflow import sha

VERSION = 2
CAPS = {'cpu_cores': 2, 'memory_mb': 4096, 'timeout_seconds': 1800, 'cpu_seconds': 1800, 'disk_mb': 256}


def host_files():
    root = Path(__file__).parent
    guard = (root / 'controlled_diagnostic_guard.py').read_text(encoding='utf-8')
    # The bounded adapter must never dispatch the historical diagnostic probe.
    guard = guard.split('\ndef launch():', 1)[0] + '''
def launch():
    raise RuntimeError('Legacy diagnostic entry is disabled in this bounded bundle')
'''
    return {'experiment.py': b'from bounded_host import main\nmain()\n',
            'bounded_host.py': (root / 'bounded_diagnostic_runtime.py').read_bytes(),
            'diagnostic_guard.py': guard.encode('utf-8')}


def capabilities():
    body = {'version': VERSION, 'limits': CAPS, 'max_updates': 512, 'gpu_allowed': False,
        'host_owned_files': {p: hashlib.sha256(v).hexdigest() for p, v in host_files().items()},
        'author_file_pattern': 'bounded_diagnostic(?:_[a-z0-9]+)?.py',
        'entrypoint': 'experiment.py -> bounded_host.main -> bounded_diagnostic.run(protocol, params)',
        'legacy_entry': 'diagnostic_guard.launch rejects before any input/output or old probe import',
        'resource_policy': 'These are upper bounds, not an allocation; retain stricter task and independently approved limits.',
        'execution_authorized': False}
    return {**body, 'sha256': sha(body)}


def reject_stub(files):
    """Reject explicit failure-only deliveries before spending independent reviews."""
    source = next((f['content'] for f in files if f['path'] == 'bounded_diagnostic.py'), '')
    tree = ast.parse(source)
    run = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 'run'), None)
    def returns(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return False
        return isinstance(node, ast.Return) or any(returns(child) for child in ast.iter_child_nodes(node))
    if run and run.body and isinstance(run.body[-1], ast.Raise) and not any(returns(n) for n in run.body):
        raise ValueError('Diagnostic delivery only raises an error and cannot produce measurements; submit a blocked delivery, not an implementation')


GUIDANCE = '''
bounded_diagnostic_capabilities 描述宿主真实支持的实现通道。入口、bounded_host和禁止旧探针的guard由宿主生成，
诊断作者只能新增bounded_diagnostic*.py；不能把宿主入口改动再次分配给该受限作者。
该通道仅CPU，资源不超过公布caps及本任务更严格限额；需要GPU、更大资源或修改模型/宿主文件的方案，
不能作为本通道可执行diagnose批准。应通过正常提案明确工程修订需求，或设计cap内的不同可检验批次。
若依据现有训练证据可以评审范围内单因素参数对照，可提出search_parameters并保留未定根因；
不要把所有训练根因先验查明新增为开始参数对照的条件，也不能因此宣称告警已解决或优化完成。
'''
