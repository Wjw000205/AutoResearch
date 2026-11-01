"""Repair missing delivery mappings without replaying or modifying implementation."""
import copy
import hashlib
import json
from pathlib import Path

from .experiment_workflow import sha
from .storage import now, uid
from .candidate_repair_source import read_file


def entries(raw, count):
    rows = json.loads(raw)
    if not isinstance(rows, list) or not rows or len(rows) > 128:
        raise ValueError('Codex delivery needs a bounded change manifest')
    from .code_bundle import relative_file
    names = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'path', 'change_index'}:
            raise ValueError('Invalid Codex change manifest entry')
        relative_file(row['path'])
        if row['path'] in names or type(row['change_index']) is not int or not 0 <= row['change_index'] < count:
            raise ValueError('Invalid or duplicate Codex change mapping')
        names.add(row['path'])
    return rows


async def repair(engine, harness, run_id, record, files):
    """One additive mapping correction, bound to the completed native delivery."""
    from .rapid_experiment_revision import COLLECTION, _write_files, _base_unchanged
    from .scoped_codex_recovery import run, current
    from .engine import ResearchBlocked
    count = len(record['decision']['changes'])
    original = entries(files['_delivery/changes.json'], count)
    missing = sorted(set(range(count)) - {r['change_index'] for r in original})
    if not missing:
        return files
    receipt = current(record['codex_work_item']).get('checkpoint', {})
    if receipt.get('status') != 'completed':
        raise ResearchBlocked('交付映射补正需要已完成的原始 Codex 回执。')
    binding = sha({'files': {n: hashlib.sha256(v).hexdigest() for n,v in files.items()},
                   'receipt': sha(receipt), 'decision': record['decision_sha256']})
    job = record.get('delivery_manifest_repair')
    if not job:
        workspace = engine.run_dir(run_id).resolve() / 'delivery-map-repairs' / binding
        _write_files(workspace, files)
        instruction = ('只补正 _delivery/changes.json 的缺失修改项映射，不修改任何其他文件。'
            '先读取 work_order.json、现有 changes.json 和已交付材料。原条目原样保留；'
            '只允许添加已存在的 _delivery/ 辅助文件路径及缺失的 change_index。'
            '每个路径唯一；不要重复登记已有路径，不要登记 changes.json 或 manifest.json 本身。'
            '必须读取并核对材料确实实现对应要求；文件存在不等于实现。若确有未实现修改项，保持缺失并说明，不能伪造覆盖。'
            '不得改源码、配置、检查脚本、批准单、manifest.json、断言范围或执行训练/CPU检查。'
            '宿主会在独立副本更新 changes.json 的派生摘要；原交付及回执保留。完成后仍须独立代码审查。'
            '\n缺失的 changes 索引：' + json.dumps(missing))
        job = {'operation_id': uid(), 'workspace': str(workspace), 'instruction': instruction,
               'source_sha256': binding, 'missing_indices': missing, 'created_at': now()}
        record['delivery_manifest_repair'] = job
        record.setdefault('delivery_original_patch', copy.deepcopy(record.get('patch')))
        engine.store.put(COLLECTION, record['id'], record)
        engine.store.event(run_id, 'rapid.delivery_manifest_repair_requested',
            '已保留原代码交付，定向补正未登记的修改项映射。',
            {'revision_id': record['id'], 'missing_indices': missing, 'source_sha256': binding})
    if job['source_sha256'] != binding:
        raise ResearchBlocked('交付映射补正的原始文件或回执发生变化。')
    workspace = Path(job['workspace'])
    def verify():
        _base_unchanged(engine, run_id, record)
        for name, raw in files.items():
            if name != '_delivery/changes.json' and read_file(workspace, name) != raw:
                raise ResearchBlocked('交付映射补正修改了受保护文件：' + name)
    def save():
        engine.store.put(COLLECTION, record['id'], record)
    await run(engine, harness, run_id, job, save, verify, label='Codex 交付映射补正')
    verify()
    corrected = read_file(workspace, '_delivery/changes.json')
    rows = entries(corrected, count)
    by_path = {r['path']: r for r in rows}
    if any(by_path.get(r['path']) != r for r in original):
        raise ResearchBlocked('交付映射补正改变了原有条目。')
    additions = [r for r in rows if r['path'] not in {x['path'] for x in original}]
    if any(r['path'] not in files or not r['path'].startswith('_delivery/')
           or r['path'] in {'_delivery/changes.json', '_delivery/manifest.json'}
           or r['change_index'] not in missing for r in additions):
        raise ResearchBlocked('交付映射补正超出原交付材料和缺失修改项范围。')
    if {r['change_index'] for r in rows} != set(range(count)):
        raise ResearchBlocked('原交付仍有未实现或未登记的批准修改项；保留阻塞等待实质修复。')
    result = {**files, '_delivery/changes.json': corrected}
    # Preserve the author's source hashes. Refresh only the changed metadata row.
    if '_delivery/manifest.json' in files:
        manifest = json.loads(files['_delivery/manifest.json'])
        for row in manifest.get('files', []):
            if row.get('path') == '_delivery/changes.json':
                if (row.get('sha256') != hashlib.sha256(files[row['path']]).hexdigest()
                        or row.get('bytes') != len(files[row['path']])):
                    raise ResearchBlocked('原交付 changes.json 摘要不匹配。')
                row.update(bytes=len(corrected), sha256=hashlib.sha256(corrected).hexdigest())
        result['_delivery/manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    job.update(status='completed', output_sha256=sha({n: hashlib.sha256(v).hexdigest() for n,v in result.items()}))
    save()
    return result
