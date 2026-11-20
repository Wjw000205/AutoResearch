"""Materialize Weather CSV metadata and review the frozen model's project window."""

from .async_ownership import gather as gather_owned
import asyncio
import csv
import copy
import hashlib
import io
import json
from datetime import datetime
from itertools import islice
from pathlib import Path

from . import dataset_planning
from .datasets import checked_asset
from .experiment_workflow import sha
from .prose_schema import validate

COLLECTION = 'dataset_project_protocol_reviews'
ROLES = ('director', 'engineer', 'scout')
VOTE = dataset_planning.obj({'approve': {'type': 'boolean'}, 'rationale': dataset_planning.TEXT,
                             'issues': dataset_planning.TEXTS})
REVIEW = '''只审 project_protocol 提案是否可用于 Weather 的项目内同条件比较。你收到的是已核对的资产元数据、论文窗口和冻结模型源码摘录。核对真实列、时间边界、异常边界窗口排除、训练拟合和 128→32 的工程依据。论文窗口仍按原文保存；这次表决可单独批准非论文等价的项目窗口和预处理，三方均批准才可实施。训练、CPU 回执、最终代码审批均在后续阶段，勿将其当成本次前置要求。不要改写工单。若同意请 approve=true 且 issues=[]，后续检查写入 rationale；否则列具体协议缺陷。'''


def csv_metadata(raw):
    """Inspect structural fields only. Never compute targets or metrics on held-out rows."""
    reader = csv.reader(io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8-sig', newline=''), strict=True)
    header = next(reader, [])
    if len(header) != 22 or header[0] != 'date' or len(set(header)) != len(header) or not all(header):
        raise ValueError('Weather CSV requires date and 21 distinct ordered feature columns')
    previous = None
    breaks = []
    count = 0
    for index, row in enumerate(reader):
        if len(row) != len(header):
            raise ValueError(f'Unexpected Weather CSV width at data row {index}')
        timestamp = datetime.fromisoformat(row[0])
        if previous is not None:
            interval = (timestamp - previous).total_seconds()
            if interval < 0:
                raise ValueError(f'Weather timestamp goes backwards at data row {index}')
            if interval != 600:
                breaks.append({'row': index, 'delta_seconds': interval})
        previous = timestamp
        count += 1
    # Keep the original rows. Truncate train and test counts independently,
    # put the remaining rows in validation, and use half-open bounds.
    train_end, test_start = count * 7 // 10, count - count * 2 // 10
    bounds = {'train': [0, train_end], 'validation': [train_end, test_start], 'test': [test_start, count]}
    if min(end-start for start, end in bounds.values()) < 160:
        raise ValueError('Too few rows for Weather project windows')
    train = csv.reader(io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8-sig', newline=''), strict=True)
    next(train)
    sentinel_counts = dict.fromkeys(header[1:], 0)
    for row in islice(train, train_end):
        for name, value in zip(header[1:], row[1:]):
            if float(value) == -9999:
                sentinel_counts[name] += 1
    return {'row_count': count, 'timestamp': header[0], 'target': header[1:],
            'training_minus9999_counts': {k: n for k, n in sentinel_counts.items() if n},
            'frequency_seconds': 600, 'discontinuities': breaks, 'split_rows': bounds}


def propose(engine, rid, saved, job):
    from .experiment_protocols import checked, _notes
    from .dataset_codex import _bytes
    datasets = saved.get('dataset_packet', {}).get('datasets', [])
    if len(datasets) != 1 or datasets[0]['target']['dataset'] != 'Weather':
        return None
    row = datasets[0]
    root = Path(job['workspace'])
    work_file = '_experiment_input/agent_work_order.json'
    if work_file not in job['protected']:
        return None
    work_raw = _bytes(root / work_file)
    if hashlib.sha256(work_raw).hexdigest() != job['protected'][work_file]:
        raise ValueError('Weather work order changed')
    work = json.loads(work_raw)
    plan = engine.store.get(dataset_planning.COLLECTION, work['plan_id']) or {}
    if (plan.get('run_id') != rid or plan.get('status') != 'approved' or plan.get('result') != work['task']
            or plan.get('result_sha256') != work['sha256'] or plan.get('protocol_evidence') != work.get('protocol_evidence')):
        raise ValueError('Weather protocol requires the original approved work order')
    dataset_planning.approval(engine.store, plan)
    paper = plan.get('protocol_evidence') or {}
    if not paper:
        return None
    notes = _notes(engine, rid)
    for ref in paper['protocols']:
        checked(engine, rid, ref['id'], ref['sha256'], notes=notes)
    fields = paper['fields']
    if (fields['channels']['values'] != [21] or '7:1:2' not in fields['split']['values']
            or '10min' not in fields['sampling_frequency']['values']):
        raise ValueError('Bound Weather paper has a different data protocol')
    # This amendment applies only to the frozen FreDN implementation.
    path = 'src/models/fredn.py'
    if path not in job['protected']:
        return None
    source = _bytes(root / path)
    if hashlib.sha256(source).hexdigest() != job['protected'][path]:
        raise ValueError('Frozen Weather model changed')
    anchors = [(i, line.strip()) for i, line in enumerate(source.decode().splitlines(), 1)
               if '(128, self.channels)' in line or 'irfft(output_spectrum, n=32' in line]
    if len(anchors) != 2:
        return None
    binding, = row['dataset_assets']
    record, directory = checked_asset(engine.store, engine.data_dir, rid, binding['asset_id'], binding['manifest_sha256'])
    manifest_file, = record['manifest']['files']
    bound, = row['files']
    if (bound['path'] != 'files/weather.csv' or
            any(bound[k] != manifest_file[k] for k in ('path', 'sha256', 'bytes'))):
        raise ValueError('Weather evidence file differs from the queued asset')
    raw = (directory / bound['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != bound['sha256']:
        raise ValueError('Weather CSV changed during protocol inspection')
    metadata = csv_metadata(raw)
    return {'version': 1, 'dataset': 'Weather', 'setup_id': saved['id'],
            'work_order_sha256': work['sha256'], 'source_program_sha256': saved['source_program_sha256'],
            'asset': {**binding, **bound}, 'csv': metadata,
            'paper_protocols': paper['protocols'], 'reported_fields': fields,
            'model_evidence': {'path': path, 'sha256': job['protected'][path],
                               'lines': [{'line': i, 'text': line} for i, line in anchors]},
            'project_window': {'L': 128, 'H': 32, 'C': 21, 'paper_equivalent': False},
            'policy': {'preprocessing': {'method': 'train_population_standardization',
                                          'fit_rows': metadata['split_rows']['train'], 'min_std': 1e-6},
                       'history_overlap': 'preceding_observed_rows', 'target_partition': 'all_forecast_rows_inside_split',
                       'gap_handling': 'Keep all source rows and their original indices. Exclude any window whose combined input/forecast interval crosses a listed discontinuity. No interpolation. Reject unlisted irregularities in the permitted prefix.',
                       'invalid_value_policy': 'Project choice: treat -9999 as invalid in any target channel. Fit each channel only on valid training values in fit_rows. Exclude windows containing an invalid input or target row. No imputation or interpolation. Reject nonfinite values. Apply the identical mask to all variants, record resulting counts and never access test targets for validation.',
                       'split_rounding': 'floor(0.7*N) train, floor(0.2*N) test, remainder validation; zero-based half-open intervals.',
                       'comparison': 'Same 21 columns, windows, split and train-only scaling for baseline, proposed and ablation. Preserve frozen model and seed 42. Physical-scale val_mse; no comparison to published scores or cross-seed claim.'}}


def verify(store, rid, proof):
    from . import independent_full_review as full
    """Votes only authorize this proposal, not a candidate, trial or script."""
    from .engine import Harness
    key = proof['id']
    row = store.get(COLLECTION, key) or {}
    body = row.get('proposal')
    if (row.get('run_id') != rid or not body or sha(body) != proof['sha256']
            or key != rid + ':weather-protocol:' + sha(body) or row.get('status') != 'approved'):
        raise ValueError('Weather project protocol does not have a bound approval')
    ctx = row['context']
    if ctx.get('dataset_planning', {}).get('project_protocol') != body:
        raise ValueError('Weather review context differs from proposal')
    roles = full.reviewers(row['full_votes']) if row.get('full_votes') else ROLES
    if row.get('full_votes'):
        full.authenticate_complete(store, rid, row['full_votes'])
        for vote in row['full_votes']:
            if full.KEY in vote:full.validate_delivery(store,rid,vote)
    for role in roles:
        receipt = row['votes'][role]
        owner = (next(v for v in row['full_votes'] if v['role']==role)[full.KEY]['handoff_id']
            if row.get('full_votes') else Harness.decision_key(rid, role, row['purpose'], REVIEW, ctx, VOTE))
        actual = store.get('decisions', owner) or {}
        if (receipt['handoff_id'] != owner or actual.get('result') != receipt['result']
                or actual.get('run_id') != rid or not receipt['result']['approve'] or receipt['result']['issues']):
            raise ValueError('Weather protocol requires three exact independent approvals')
    return row


async def confirm(engine, harness, rid, saved, job):
    from .engine import Harness, ResearchBlocked
    body = await asyncio.to_thread(propose, engine, rid, saved, job)
    if body is None:
        return None
    digest = sha(body)
    key = rid + ':weather-protocol:' + digest
    proof = {'id': key, 'sha256': digest}
    row = engine.store.get(COLLECTION, key)
    if not row:
        ctx = dataset_planning._context(engine, rid, key, {'project_protocol': body}, 'project_protocol')
        ctx['dataset_planning'].update(project_protocol=body, read_paths=['/project_protocol'])
        row = {'id': key, 'run_id': rid, 'proposal': body, 'context': ctx, 'status': 'reviewing',
               'purpose': 'dataset_project_protocol:' + digest, 'votes': {}}
        engine.store.put(COLLECTION, key, row)
        engine.store.event(rid, 'dataset.project_protocol_review', '真实 CSV 元数据与项目窗口已定向送交三方。', proof)
    if row['status'] != 'approved':
        from . import independent_full_review as full
        if not row.get('full_votes'):
            if row.get('votes'):
                row.setdefault('legacy_votes', []).append(copy.deepcopy(row['votes']))
            row['full_votes'] = await full.group(harness, rid, row['purpose'], REVIEW, row['context'], VOTE)
            row['votes'] = {v['role']: {'handoff_id': v[full.KEY]['handoff_id'] if full.KEY in v else
                Harness.decision_key(rid,v['role'],row['purpose'],REVIEW,row['context'],VOTE),
                'result': full.answer(v)} for v in row['full_votes']}
            engine.check_active(rid)
            engine.store.put(COLLECTION, key, row)
        return await _complete_review(engine, rid, row, proof)
    verify(engine.store, rid, proof)
    return proof


async def _legacy_confirm_votes(engine, harness, rid, row, key):
    from .engine import Harness, ResearchBlocked
    if row['status'] != 'approved':
        async def ask(role):
            result = await harness.ask(rid, role, row['purpose'], REVIEW, row['context'], VOTE)
            validate(result, VOTE)
            engine.check_active(rid)
            owner = Harness.decision_key(rid, role, row['purpose'], REVIEW, row['context'], VOTE)
            row['votes'][role] = {'handoff_id': owner, 'result': result}
            engine.store.put(COLLECTION, key, row)
        outcomes = await gather_owned(*(ask(r) for r in ROLES if r not in row['votes']), return_exceptions=True)
        for result in outcomes:
            if isinstance(result, BaseException):
                raise result
        objections = [f'{r}: ' + '；'.join(v['result']['issues'] or [v['result']['rationale']])
                      for r, v in row['votes'].items() if not v['result']['approve'] or v['result']['issues']]
        if objections:
            row['status'] = 'changes_requested'
            engine.store.put(COLLECTION, key, row)
            raise ResearchBlocked('Weather 项目协议有具体异议：' + ' / '.join(objections))
        row['status'] = 'approved'
        engine.store.put(COLLECTION, key, row)
    return row


async def _complete_review(engine, rid, row, proof):
    from .engine import ResearchBlocked
    objections = [r + ': ' + '；'.join(v['result']['issues'] or [v['result']['rationale']])
        for r,v in row['votes'].items() if not v['result']['approve'] or v['result']['issues']]
    row['status'] = 'changes_requested' if objections else 'approved'
    engine.check_active(rid)
    engine.store.put(COLLECTION, row['id'], row)
    if objections:
        raise ResearchBlocked('项目协议有具体独立异议：' + ' / '.join(objections))
    verify(engine.store, rid, proof)
    return proof


def _profiles(config):
    profiles = config['datasets']['Weather']
    for role in ('baseline', 'proposed', 'ablation'):
        params = profiles[role + '_params']
        yield role + '_params', params
        for name, variant in params.get('configurations', {}).items():
            yield role + '_params/configurations/' + name, variant


def seed_configuration(raw, body):
    """Fill only protocol fields in the new working copy after all three votes."""
    config = json.loads(raw)
    csv_spec = body['csv']
    for _, params in _profiles(config):
        spec = params['common']['data']
        spec.update({k: csv_spec[k] for k in ('target', 'timestamp', 'split_rows', 'frequency_seconds')})
        spec.update({k: body['policy'][k] for k in ('history_overlap', 'target_partition')})
        spec['preprocessing'].update(body['policy']['preprocessing'])
        params['project_protocol_sha256'] = sha(body)
        params['task_dimensions']['data_and_preprocessing'] = spec
        for dimension in params['comparison_task']['dimensions']:
            if dimension['paths'] == ['/common/data']:
                dimension['value'] = spec
    check_profiles(config, body)
    return json.dumps(config, ensure_ascii=False, indent=2).encode()


def check_profiles(config, body):
    """Keep an incomplete or stale implementation out of the paid code review."""
    csv_spec = body['csv']
    window = body['project_window']
    for role, params in _profiles(config):
        common = params['common']
        spec = common['data']
        expected = {k: csv_spec[k] for k in ('target', 'timestamp', 'split_rows', 'frequency_seconds')}
        expected.update({k: body['policy'][k] for k in ('history_overlap', 'target_partition')})
        fields = params.get('protocol_binding', {})
        checks = [(params, {'project_protocol_sha256': sha(body)}, ''),
                  (fields, {'fields': body['reported_fields'], 'protocols': body['paper_protocols']}, 'protocol_binding'),
                  (common, {k: window[k] for k in ('L','H','C')}, 'common'),
                  (spec, expected, 'common/data'),
                  (spec.get('preprocessing', {}), body['policy']['preprocessing'], 'common/data/preprocessing'),
                  (params.get('task_dimensions', {}), {'data_and_preprocessing': spec}, 'task_dimensions')]
        for actual, wanted, path in checks:
            for k, v in wanted.items():
                if actual.get(k) != v:
                    pointer = '/'.join(part for part in (role, path, k) if part)
                    raise ValueError(f'Weather {pointer} differs from the approved CSV project protocol')
        mirrors = [d['value'] for d in params['comparison_task']['dimensions'] if '/common/data' in d['paths']]
        if mirrors != [spec]:
            raise ValueError(f'Weather {role}/comparison_task differs from its bound data specification')
