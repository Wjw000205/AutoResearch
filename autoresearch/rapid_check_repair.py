"""Bounded repair of a check's parameter-file entry, retaining failed receipts."""
import ast
import copy

from .experiment_workflow import sha
from .storage import now

OLD = 'json.loads(sys.argv[1])'
NEW = "json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))"


def eligible(engine, run_id, record):
    from .rapid_host_check_repair import eligible as host_eligible
    if host_eligible(engine, run_id, record):
        return True
    if record.get('status') != 'check_failed' or record.get('check_repair'):
        return False
    saved = record.get('check', {})
    trial = engine.store.get('experiments', run_id + ':' + saved.get('id', '')) or {}
    return (record.get('run_id') == run_id and record.get('cycle') == engine.cycle(run_id)
        and trial.get('run_id') == run_id and trial.get('status') == 'failed'
        and trial.get('params', {}).get('phase') == 'code_check'
        and sha(trial) == saved.get('receipt_sha256')
        and trial.get('code_sha256') == record.get('candidate', {}).get('checks_sha256')
        and record.get('patch', {}).get('checks', '').count(OLD) == 1
        and 'JSONDecodeError: Expecting value' in trial.get('stderr', '')
        and OLD in trial.get('stderr', '') and 'AssertionError' not in trial.get('stderr', ''))


def pending(engine, run_id):
    from .rapid_experiment_revision import COLLECTION
    records = [r for r in engine.store.list(COLLECTION, run_id)
        if r.get('cycle') == engine.cycle(run_id)]
    record = max(records, key=lambda r:r.get('completed_at') or r.get('updated_at') or r['created_at'], default=None)
    if record and (eligible(engine, run_id, record) or (record.get('check_repair') or record.get('host_check_repair'))
            and record.get('status') in {'check_repairing', 'reviewing', 'checking'}):
        return record
    return None


def validate_edit(before, after):
    if before.count(OLD) != 1 or ast.dump(ast.parse(before.replace(OLD, NEW, 1))) != ast.dump(ast.parse(after)):
        raise ValueError('Check repair may only read the provided parameter file; assertions and all other code must remain unchanged')


async def repair(engine, harness, run_id, record):
    from . import rapid_host_check_repair
    if rapid_host_check_repair.eligible(engine, run_id, record) or record.get('host_check_repair'):
        return await rapid_host_check_repair.repair(engine, run_id, record)
    from . import rapid_experiment_revision as rapid
    from .experiment_decisions import _archive, units
    from .training_adjustment import _basis, obj
    rapid._base_unchanged(engine, run_id, record)
    if not record.get('check_repair'):
        if not eligible(engine, run_id, record) or not rapid._reviewed(record):
            raise ValueError('Scoped check-entry repair requires an authenticated parameter decoding failure')
        context = rapid._review_context(engine, run_id, record)
        for vote in record['votes']:
            rapid.review_receipts(engine, run_id, record, vote, context)
        trial = engine.store.get('experiments', run_id + ':' + record['check']['id'])
        archive = _archive(engine, run_id, {'rapid_revision_attempt': copy.deepcopy(record), 'failed_check': trial})
        record['check_repair'] = {'original': archive, 'failed_trial_id': trial['id'],
            'failed_receipt_sha256': sha(trial), 'created_at': now()}
        record['status'] = 'check_repairing'
        engine.store.put(rapid.COLLECTION, record['id'], record)
    pilot = next(u for u in units(_basis(engine, run_id)['plan']) if u['kind'] == 'pilot')
    rapid._budget(engine, run_id, 1 + len(pilot['jobs']))
    ref = record['check_repair']['original']
    archive = engine.store.get('experiment_evidence', ref['id']) or {}
    if archive.get('run_id') != run_id or sha(archive.get('summary')) != ref['sha256']:
        raise ValueError('Original check-repair evidence changed')
    original = archive['summary']['rapid_revision_attempt']
    trial = engine.store.get('experiments', run_id + ':' + record['check_repair']['failed_trial_id'])
    if sha(trial) != record['check_repair']['failed_receipt_sha256']:
        raise ValueError('Failed CPU check receipt changed')
    context = {'training_context_protocol': 1, 'stage': engine.store.get('runs', run_id)['stage'],
        'research_cycle': engine.cycle(run_id), 'rapid_revision': {
            'check_entry_repair': True, 'failed_trial_id': trial['id'], 'evidence': ref,
            'stderr_tail': trial.get('stderr', '')[-1800:], 'checks': original['patch']['checks'],
            'argument_contract': 'sys.argv[1] is the supplied params.json file path, not serialized JSON.',
            'required_change': {'old': OLD, 'new': NEW}, 'scope': 'Only this parameter read; preserve every assertion and model edit.'}}
    from .rapid_output_length import ask
    schema = obj({'checks': {'type':'string','minLength':1}, 'rationale': rapid.TEXT})
    response = await ask(engine, harness, run_id, 'rapid_implement:check-entry-repair:' + ref['sha256'],
        '修复当前 CPU 检查入口：按宿主契约从 sys.argv[1] 指向的 params.json 读取 JSON。只应用 required_change 中的表达式替换，返回完整 checks 与简短理由。不要删除、放宽或改变任何断言，不改模型、配置、预算和评估。失败回执保留，新检查仍需独立复核和实际执行。', context, schema)
    validate_edit(original['patch']['checks'], response['checks'])
    rapid._base_unchanged(engine, run_id, record)
    patch = {**copy.deepcopy(original['patch']), 'checks': response['checks']}
    candidate = await engine._read_consensus_check(run_id, rapid.build_candidate, engine, run_id, record, patch)
    if candidate['bundle']['sha256'] != original['candidate']['bundle']['sha256']:
        raise ValueError('Check-entry repair unexpectedly changed candidate runtime bytes')
    for key in ('check', 'review_reads', 'review_purpose', 'review_clarifications', 'source_read_recovery', 'review_history', 'error', 'completed_at'):
        record.pop(key, None)
    record.update(patch=patch, candidate=candidate, votes=[], status='reviewing')
    record['check_repair'].update(result_sha256=sha(response), completed_at=now())
    engine.store.put(rapid.COLLECTION, record['id'], record)
    engine.store.event(run_id, 'experiment.check_entry_repaired',
        'Codex 已修正检查脚本的参数文件读取；运行源码和全部断言保持不变，等待同版本复核与执行。',
        {'revision_id': record['id'], 'original_evidence': ref, 'amendment_sha256': candidate['amendment_sha256']})
