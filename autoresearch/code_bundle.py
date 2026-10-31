"""Hash-bound runtime modules for a self-contained research project directory."""
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath

from .delivery_files import copy_verified, identity, io_path
from .delivery_manifest import INSTRUCTION as DELIVERY_INSTRUCTION

INSTRUCTION = (
    '代码必须按职责目录组织，交付完整、自包含的项目。实现代码统一放在 src/ 下：'
    'src/data/（数据生成、处理与读取）、src/models/（模型）、src/training/（训练）、'
    'src/evaluation/（评估）、src/configs/（配置解析代码）、src/utils/（工具）；'
    'src/runner.py 编排流程。src 同级的 configs/ 只放数据集及方法配置文件，'
    'data/ 只放数据集，不放 Python 实现；tests/ 放测试，test-results/ 放测试结果。'
    '根目录 experiment.py 只负责加载 src/ 并启动 runner。已有对应目录时直接维护，'
    '不要再创建一套平行目录或把各职责全部堆进一个扁平包。experiment.py 只保留启动入口，'
    'runner.py 只编排流程；具体算法放在对应目录。README 给出目录树及运行、测试入口。'
    '按实际职责拆分，不为增加文件数制造空模块、无必要的抽象层或重复转发文件；不要把全部实现塞进入口，'
    '也不要仅把原文件搬到另一个大文件，或用 exec/base64 动态拼接源码规避拆分。'
    'implementation.json 必须声明 code_layout="modular" 以及 runtime_files 相对路径数组，'
    '并声明 directory_layout_version=2；交付检查会核对上述目录中实际的功能代码、配置、测试和 README，'
    '不接受只建空目录、空文件或只做转发的目录作为完成证据。'
    '完整列出 experiment.py、实际导入模块和运行所需配置。tests 和开发输出不放入运行清单。'
    '保持已有算法、数值、随机流、冻结参数、输出契约和资源门禁不变，只改代码组织。'
    '历史文档中“自包含单脚本”在代码组织层面更新为“完整交付目录自包含”；入口和参数接口不变。'
    '宿主从冻结快照按 SHA256 复制清单中的文件，在隔离运行目录执行入口；不得依赖工作区外的源码。' + DELIVERY_INSTRUCTION
)
RESERVED = {'bootstrap.py', 'bootstrap.json', 'params.json', 'environment.json', '.start', 'tmp', '.git', '__pycache__'}
DIRECTORY_LAYOUT_VERSION = 2


def validate_directory_layout(directory, manifest=None):
    """Check the directory contract of new dispatches; retain legacy snapshots."""
    if manifest is None:
        manifest = json.loads(io_path(Path(directory) / 'implementation.json').read_text(encoding='utf-8'))
    if (not isinstance(manifest, dict) or manifest.get('code_layout') != 'modular'
            or type(manifest.get('directory_layout_version')) is not int
            or manifest['directory_layout_version'] not in (1, 2)):
        raise ValueError('implementation.json 必须声明 code_layout=modular 和受支持的 directory_layout_version')
    prefix = 'src/' if manifest['directory_layout_version'] == 2 else ''
    runtime = manifest.get('runtime_files', [])
    delivery = manifest.get('delivery_files', [])
    if not isinstance(runtime, list) or not isinstance(delivery, list):
        raise ValueError('目录架构需要完整的 runtime_files 和 delivery_files 清单')
    for name in runtime:
        relative_file(name)
    from .delivery_manifest import _relative
    for name in delivery:
        _relative(name)
    if 'experiment.py' not in runtime or not set(runtime).issubset(delivery):
        raise ValueError('目录架构的交付清单必须包含 experiment.py 和全部运行文件')

    def substantive(name):
        path = io_path(Path(directory) / name)
        if name.endswith('.py') and not name.endswith('/__init__.py'):
            return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                       for node in ast.parse(path.read_bytes()).body)
        return False

    for folder in ('data', 'models', 'training', 'evaluation', 'utils'):
        if not any(name.startswith(prefix + folder + '/') and substantive(name) for name in runtime):
            raise ValueError(f'目录架构缺少 {prefix}{folder}/ 中的实际功能代码；空模块或仅转发导入不算实现')
    def configuration(name):
        if substantive(name):
            return True
        if name.endswith('.json'):
            value = json.loads(io_path(Path(directory) / name).read_bytes())
            return isinstance(value, dict) and bool(value)
        return False

    if not any(name.startswith('configs/') and configuration(name) for name in runtime):
        raise ValueError('目录架构缺少 configs/ 中的实际配置或配置解析代码')
    if prefix + 'runner.py' not in runtime or not substantive(prefix + 'runner.py'):
        raise ValueError('目录架构需要 ' + prefix + 'runner.py 编排入口')
    if prefix and any(name.endswith('.py') and not name.startswith('src/') and name != 'experiment.py' for name in runtime):
        raise ValueError('运行实现代码必须放在 src/；根目录 configs/ 和 data/ 只保存配置文件及数据集')
    if 'README.md' not in delivery or not io_path(Path(directory) / 'README.md').read_text(encoding='utf-8').strip():
        raise ValueError('目录架构需要 README.md 说明目录、运行和测试入口')
    if not any(name.startswith('tests/') and substantive(name) for name in delivery):
        raise ValueError('目录架构需要 tests/ 中的实际测试代码')
    entry = ast.parse(io_path(Path(directory) / 'experiment.py').read_bytes())
    if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.For, ast.While))
           for node in ast.walk(entry)):
        raise ValueError('experiment.py 只能启动 runner，算法、类与循环应放入职责目录')
    if not any(isinstance(node, ast.Import) and any(a.name == 'runner' for a in node.names)
               or isinstance(node, ast.ImportFrom) and node.module == 'runner' for node in entry.body):
        raise ValueError('experiment.py 必须使用 runner 的流程入口')


def source_issues(source):
    """Apply the same placeholder checks to the entry and its runtime modules."""
    tree = ast.parse(source)
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                body = body[1:]
            if not body or all(isinstance(n, ast.Pass) or (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and n.value.value is Ellipsis) for n in body):
                issues.append(f'函数 {node.name} 尚未实现')
        if isinstance(node, ast.Raise) and isinstance(node.exc, (ast.Name, ast.Call)):
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(target, ast.Name) and target.id == 'NotImplementedError':
                issues.append('脚本仍包含 NotImplementedError 占位实现')
    return issues


def relative_file(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or '\x00' in value:
        raise ValueError('运行文件必须是规范的项目内相对路径')
    path = PurePosixPath(value)
    if value in {'hyperparameters.yaml','hyperparameters.receipt.json'}:
        raise ValueError('运行文件不能覆盖宿主绑定的 YAML 输入或回执')
    if path.is_absolute() or path.as_posix() != value or '..' in path.parts or any(part in RESERVED for part in path.parts):
        raise ValueError('运行文件路径越界或与宿主控制文件冲突')
    return path


def digest(files):
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def describe(directory, manifest, files):
    """Legacy receipts remain readable; new modular deliveries bind every module."""
    declared = manifest.get('runtime_files')
    if declared is None and manifest.get('code_layout') != 'modular':
        return None
    if not isinstance(declared, list) or not declared or len(set(str(p) for p in declared)) != len(declared):
        raise ValueError('runtime_files 必须是无重复的运行文件清单')
    known = {row['path']: row for row in files}
    runtime = []
    for value in declared:
        path = relative_file(value)
        if value not in known:
            raise ValueError('运行文件未实际交付：' + value)
        if path.suffix == '.py':
            source = io_path(Path(directory) / value).read_bytes()
            compile(source, value, 'exec')
            issues = source_issues(source)
            if issues:
                raise ValueError(value + ': ' + '; '.join(issues))
        runtime.append({key: known[value][key] for key in ('path', 'sha256', 'bytes')})
    if 'experiment.py' not in declared or not any(p.endswith('.py') and p != 'experiment.py' and not p.endswith('/__init__.py') for p in declared):
        raise ValueError('结构化交付需要入口及实际功能模块')
    if 'directory_layout_version' in manifest:
        validate_directory_layout(directory, manifest)
    runtime.sort(key=lambda row: row['path'])
    return {'version': 1, 'root': str(Path(directory).resolve()), 'files': runtime, 'sha256': digest(runtime)}


def validate(bundle, code):
    if not isinstance(bundle, dict) or bundle.get('version') != 1 or not isinstance(bundle.get('files'), list):
        raise ValueError('无效的运行代码目录清单')
    files = bundle['files']
    if digest(files) != bundle.get('sha256') or len({row['path'] for row in files}) != len(files):
        raise ValueError('运行代码目录清单哈希不一致')
    root = Path(bundle['root'])
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('冻结运行目录不存在')
    for part in (root, *root.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('冻结运行目录不能通过链接读取')
    entry = next((row for row in files if row['path'] == 'experiment.py'), None)
    if not entry or entry['sha256'] != hashlib.sha256(code.encode()).hexdigest():
        raise ValueError('运行入口与已批准代码不一致')
    for row in files:
        relative_file(row['path'])
        path = root / row['path']
        for part in (path, *path.parents):
            if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
                raise ValueError('运行模块不能通过链接读取')
            if part == root:
                break
        if identity(path) != {key: row[key] for key in ('sha256', 'bytes')}:
            raise ValueError('冻结运行模块发生变化：' + row['path'])
    return sum(row['bytes'] for row in files)


def stage(bundle, code, destination, disk_bytes):
    size = validate(bundle, code)
    if size > disk_bytes:
        raise ValueError('运行代码目录超过已批准磁盘资源预算')
    for row in bundle['files']:
        copy_verified(Path(bundle['root']) / row['path'], Path(destination) / row['path'],
                      {key: row[key] for key in ('sha256', 'bytes')})
