"""Select a reviewable source delivery without collecting development outputs."""
import json
from pathlib import Path, PurePosixPath

from .delivery_files import io_path, regular_files

INSTRUCTION = (
    '交付范围仅含源码、运行必需配置、测试代码及其最小输入夹具、测试结果摘要和使用说明。'
    'implementation.json 必须用 delivery_files 相对路径数组明确列出交付项，并用 test_result_files 列出测试结果摘要。'
    'delivery_files 必须包含 implementation.json 和全部 runtime_files。'
    '不要交付生成数据、模型权重、训练检查点、运行缓存、原始运行日志、旧源码备份和逐轮开发输出目录。'
    '运行产物留在工作目录；正式实验和参数微调产物由后续阶段管理，不是代码交付项。'
    '测试只保留复现所需的最小输入夹具，历史算法回归参考只提取相关函数，不携带整份旧实现。'
)
OUTPUT_DIRS = {'__pycache__', '.git', '.pytest_cache', '.mypy_cache', '.ruff_cache',
               '.venv', 'venv', 'node_modules', 'development-host-replay', 'development-host-probes',
               'artifacts', 'checkpoints', 'runs', 'logs'}
OUTPUT_SUFFIXES = {'.pyc', '.pyo', '.pt', '.pth', '.ckpt', '.npy', '.npz', '.safetensors', '.log'}


def _relative(value):
    if not isinstance(value, str) or not value or any(c in value for c in ('\\', ':', '\x00')):
        raise ValueError('交付项必须是项目内的规范相对文件路径')
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or '..' in path.parts:
        raise ValueError('交付项路径越界')
    if (any(p.lower() in OUTPUT_DIRS for p in path.parts) or path.suffix.lower() in OUTPUT_SUFFIXES
            or path.name.lower() in {'stdout.txt', 'stderr.txt', '.start'} or '-before-' in path.name.lower()):
        raise ValueError('运行产物或历史备份不能列为源码交付项：' + value)
    return path


def select(directory):
    """Explicit deliveries inspect listed files only; legacy snapshots stay valid."""
    root = Path(directory)
    manifest_path = root / 'implementation.json'
    # Do not follow a manifest link before it has been checked.
    _plain(manifest_path, root)
    if not manifest_path.is_file():
        return regular_files(root), None
    manifest = json.loads(io_path(manifest_path).read_text(encoding='utf-8'))
    declared = manifest.get('delivery_files') if isinstance(manifest, dict) else None
    if declared is None:
        if isinstance(manifest, dict) and manifest.get('code_layout') == 'modular':
            raise ValueError('结构化项目必须明确声明 delivery_files，不能将整个工作目录作为交付')
        return regular_files(root), None
    if not isinstance(declared, list) or not declared or any(not isinstance(p, str) for p in declared) or len(set(declared)) != len(declared):
        raise ValueError('delivery_files 必须是无重复的相对文件路径数组')
    runtime = manifest.get('runtime_files') or ['experiment.py']
    if not isinstance(runtime, list) or any(not isinstance(p, str) for p in runtime):
        raise ValueError('runtime_files 必须是相对文件路径数组')
    if not {'experiment.py', 'implementation.json', *runtime} <= set(declared):
        raise ValueError('交付清单必须包含入口、manifest 和全部运行模块')
    results = manifest.get('test_result_files', [])
    if not isinstance(results, list) or any(not isinstance(p, str) for p in results) or not set(results) <= set(declared):
        raise ValueError('测试结果摘要必须实际列入交付清单')
    paths = []
    for name in sorted(declared):
        path = root / _relative(name)
        _plain(path, root)
        if not io_path(path).is_file():
            raise ValueError('交付清单中的文件不存在：' + name)
        paths.append(path)
    summary = {'scope': 'source_and_test_results', 'file_count': len(paths),
               'runtime_file_count': len(runtime), 'test_result_count': len(set(results)),
               'test_file_count': sum(p.startswith('tests/') and p not in results for p in declared)}
    return paths, summary


def _plain(path, root):
    for part in (path, *path.parents):
        target = io_path(part)
        if target.is_symlink() or getattr(target, 'is_junction', lambda: False)():
            raise ValueError('交付项不能通过符号链接或目录联接读取')
        if part == root:
            break
