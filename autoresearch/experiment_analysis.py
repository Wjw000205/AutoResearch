"""Read-only research analysis over immutable validation evidence and executed code.

Reports separate observations, possible explanations and missing discriminating
experiments. Neither a metric difference nor an AST edge proves a causal defect.
"""
import ast
import hashlib
import json
import math
from pathlib import Path

from .experiment_workflow import sha, encoded
from .delivery_files import io_path

TOOLS = ('experiments.analyze', 'experiments.inspect_module')


def freeze_sources(engine, run_id, program, bundle=None, *, delivery_support=False):
    """Copy verified source bytes once; subsequent reads never follow live paths."""
    if bundle:
        from .code_bundle import validate
        validate(bundle, program['code'])
        manifest = bundle['files']
        identity = bundle['sha256']
    else:
        manifest = [{'path': 'experiment.py', 'sha256': hashlib.sha256(program['code'].encode()).hexdigest(),
                     'bytes': len(program['code'].encode())}]
        identity = sha(manifest)
    key = f'{run_id}:analysis-source:{identity}'
    if delivery_support:
        # A new archive policy must not rewrite old SHA-bound read receipts.
        key += ':delivery-support-v2'
    saved = engine.store.get('experiment_sources', key)
    if saved:
        if sha(saved['files']) != saved['sha256']:
            raise ValueError('Archived experiment source changed')
        return {'id': key, 'sha256': saved['sha256'], 'file_count': len(saved['files'])}
    files, used = {}, 0
    for row in manifest:
        name = row['path']
        item = {k: row[k] for k in ('path', 'sha256', 'bytes')}
        from .candidate_repair_source import file_limit
        limit = file_limit(name) if delivery_support else 1024 * 1024
        if (not name.endswith(('.py', '.json', '.yaml', '.yml', '.toml'))
                or row['bytes'] > limit or used + row['bytes'] > 16 * 1024 * 1024):
            item['unavailable_reason'] = 'Non-source or exceeds the bounded source archive; no reading claim.'
        else:
            raw = io_path(Path(bundle['root']) / name).read_bytes() if bundle else program['code'].encode()
            if hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise ValueError('Executed source changed during archive')
            try:
                item['text'] = raw.decode('utf-8')
                used += len(raw)
            except UnicodeDecodeError:
                item['unavailable_reason'] = 'Source is not UTF-8; cannot inspect.'
        files[name] = item
    record = {'id': key, 'run_id': run_id, 'sha256': sha(files), 'files': files,
              'provenance': 'hash_verified_source_snapshot',
              'execution_claim': 'Source identity only; execution requires a separate runner receipt.'}
    def keep(old):
        if old and old != record:
            raise ValueError('Source identity already has different archived bytes')
        return old or record
    engine.store.mutate('experiment_sources', key, keep)
    return {'id': key, 'sha256': record['sha256'], 'file_count': len(files)}


def frozen(engine, run_id, context, args, *, source_page=False):
    ref = context.get('validation_evidence') or {}
    if args['evidence_id'] != ref.get('id') or args['expected_sha256'] != ref.get('sha256'):
        raise ValueError('Analysis must use this exact frozen decision evidence')
    if source_page:
        from .scope_read_session import verified_record
        record = verified_record(engine, 'experiment_evidence', ref['id'], run_id, ref['sha256'],
                                 error='Decision evidence is missing, changed or not an analysis snapshot')
        if record is not None:
            if 'binding' not in record['summary']:
                raise ValueError('Decision evidence is missing, changed or not an analysis snapshot')
            return record['summary']
    record = engine.store.get('experiment_evidence', ref['id']) or {}
    evidence = record.get('summary')
    if record.get('run_id') != run_id or sha(evidence) != ref['sha256'] or 'binding' not in evidence:
        raise ValueError('Decision evidence is missing, changed or not an analysis snapshot')
    return evidence


def _rows(evidence, unit_id):
    unit = next((u for u in evidence['units'] if u['id'] == unit_id), None)
    if not unit:
        raise ValueError('Unknown analysis batch; inspect /units first')
    keys = set(unit['params_sha256'])
    return unit, [r for r in evidence['records'] if sha(r['params']) in keys]


def _finite(x):
    return type(x) in (int, float) and math.isfinite(x)


def _paired(left, right):
    by_seed = lambda rows: {encoded(r['params']['seed']): r for r in rows}
    a, b = by_seed(left), by_seed(right)
    if len(a) != len(left) or len(b) != len(right):
        return [{'unavailable': 'Duplicate seed records; no arbitrary winner or paired difference selected.'}]
    seeds = sorted(set(a) & set(b))
    metrics = sorted(set(k for r in left + right for k in r['metrics'] if k.startswith('val_')))
    result = []
    for metric in metrics:
        pairs = [{'seed': a[s]['params']['seed'], 'reference_trial': a[s]['id'], 'candidate_trial': b[s]['id'],
                  'reference': a[s]['metrics'][metric], 'candidate': b[s]['metrics'][metric],
                  'candidate_minus_reference': b[s]['metrics'][metric] - a[s]['metrics'][metric]}
                 for s in seeds if _finite(a[s]['metrics'].get(metric)) and _finite(b[s]['metrics'].get(metric))]
        # Non-finite arithmetic is unavailable, never encoded as a metric.
        for pair in pairs:
            if not _finite(pair['candidate_minus_reference']):
                pair['candidate_minus_reference'] = None
        result.append({'metric': metric, 'pairs': pairs, 'pair_count': len(pairs),
            'complete_metric_coverage': len(pairs) == len(a) == len(b) and bool(pairs),
            'interpretation': 'Signed raw difference only; metric direction/threshold must come from the frozen acceptance contract.'})
    return result


def _param_diff(a, b, path=''):
    changes = []
    for key in sorted(set(a) | set(b)):
        if not path and key in {'seed', 'phase', 'variant'}:
            continue
        name = path + '/' + key.replace('~', '~0').replace('/', '~1')
        if isinstance(a.get(key), dict) and isinstance(b.get(key), dict):
            changes.extend(_param_diff(a[key], b[key], name))
        elif a.get(key) != b.get(key) or (key in a) != (key in b):
            changes.append({'path': name, 'reference': a.get(key), 'candidate': b.get(key)})
    return changes


def analysis_result(evidence, args):
    kind = args['kind']
    result = {'kind': kind, 'evidence_sha256': args['expected_sha256'],
              **{k: evidence[k] for k in ('performance_policy', 'performance_assessment') if k in evidence},
              'root_cause_verified': False, 'scientific_approval': False}
    if kind == 'comparison':
        unit, rows = _rows(evidence, args.get('unit_id'))
        good = [r for r in rows if r['status'] == 'completed']
        reference = [r for r in good if r['params']['variant'] == 'baseline']
        candidate = [r for r in good if r['params']['variant'] == 'proposed']
        result.update(comparison=evidence['comparisons'][unit['id']], metrics=_paired(reference, candidate),
            acceptance=evidence['acceptance'], missing_conditions=[] if unit['complete'] else ['Frozen seed/config batch incomplete'],
            next_evidence=['Compare loss/validation trends and read the affected module.',
                           'Use a controlled ablation/diagnostic to distinguish architecture, optimization, data and implementation.'])
    elif kind == 'ablation':
        ablation, right = _rows(evidence, args.get('unit_id'))
        if not args.get('reference_unit_id'):
            references = [u['id'] for u in evidence['units']
                          if u['task_id'] == ablation['task_id'] and u['kind'] != 'ablation']
            raise ValueError('Ablation analysis requires reference_unit_id for the proposed control batch '
                f'and unit_id for the ablation batch. Same-task reference candidates: {references}. '
                'Choose the matching frozen control explicitly; no training or new logs are needed.')
        unit, left = _rows(evidence, args['reference_unit_id'])
        if unit['task_id'] != ablation['task_id'] or ablation['kind'] != 'ablation':
            raise ValueError('Ablation attribution must remain within the same benchmark task')
        left = [r for r in left if r['params']['variant'] == 'proposed' and r['status'] == 'completed']
        right = [r for r in right if r['params']['variant'] == 'ablation' and r['status'] == 'completed']
        changes = _param_diff(left[0]['params'], right[0]['params']) if left and right else []
        identities = {(r['evaluation'].get('dataset_id'), encoded(r['evaluation'].get('split_fingerprints'))) for r in left + right}
        result.update(metrics=_paired(left, right), parameter_changes=changes,
            comparison_usable=unit['complete'] and ablation['complete'] and len(identities) == 1,
            limitations=['An ablation difference alone does not localize a bug or prove causality.',
                        'Multiple parameter/module changes are confounded; verify the SPEC and implementation.'],
            next_evidence=['Read the changed module and its callers; define a single-factor diagnostic if attribution is ambiguous.'])
    elif kind == 'training':
        trial = args.get('trial_id')
        if trial not in evidence['diagnostics']:
            raise ValueError('Training analysis requires a trial from this frozen snapshot')
        result.update(trial_id=trial, diagnosis=evidence['diagnostics'][trial],
            next_evidence=['Inspect optimizer, loss scaling, detach/gradient paths and validation monitoring.',
                           'Missing gradient/activation evidence remains unknown; request instrumentation through a reviewed diagnostic.'])
        investigation = evidence.get('training_investigation')
        if investigation and investigation.get('version', 1) >= 2:
            from .training_statistics import critical_packet
            # History is read separately; it cannot displace current critical statistics.
            result = {'kind': kind, 'evidence_sha256': args['expected_sha256'],
                      'critical_training_packet': critical_packet(evidence, trial)}
        elif investigation:
            result['training_investigation'] = {
                'observations': investigation.get('observations', {}).get(trial),
                'followup': investigation.get('followup'),
                'required': investigation['required'],
                'scope': 'Identify a training cause, then instrument/repair or run one controlled parameter batch. A generic contribution ablation is not a noise diagnosis.'}
    elif kind == 'integrity':
        result.update(ledger=evidence['ledger'], failures=evidence['failure_categories'], budget=evidence['budget'],
            recent_revision={k: (evidence.get('rapid_revision') or {}).get(k) for k in ('id', 'status', 'check', 'error')},
            limitations=['Invalid code/plan/data/split binding or failed execution cannot establish architectural inferiority.'])
    else:
        raise ValueError('Unknown analysis kind')
    return result


def analyze(engine, run_id, context, args, *, persist=True):
    from .experiment_tools import _page
    evidence = frozen(engine, run_id, context, args)
    result = analysis_result(evidence, args)
    kind = args['kind']
    identifier = run_id + ':analysis:' + sha(result)
    if persist:
        engine.store.mutate('experiment_analyses', identifier, lambda old: old or {
            'id': identifier, 'run_id': run_id, 'result': result, 'sha256': sha(result),
            'source_evidence_id': args['evidence_id'], 'source_evidence_sha256': args['expected_sha256']})
    return {'analysis_id': identifier, 'kind': kind, 'evidence_sha256': args['expected_sha256'],
            'source': 'frozen_validation_analysis', **_page(result, args)}


def inspect_module(engine, run_id, context, args):
    from .experiment_tools import _page
    evidence = frozen(engine, run_id, context, args, source_page=True)
    if args.get('snapshot') == 'candidate':
        ref = evidence.get('candidate_code_sources') or (evidence.get('rapid_revision') or {}).get('code_sources')
        # Scoped review archives contain the candidate itself as code_sources.
        # Only the explicit frozen candidate marker permits this alias; normal
        # experiment evidence must never substitute executed source for a candidate.
        if not ref and evidence.get('candidate_not_executed') is True:
            ref = evidence.get('code_sources')
    else:
        ref = evidence['code_sources']
    if not ref:
        raise ValueError('No candidate source snapshot is available in this evidence')
    from .scope_read_session import verified_record
    error = 'Executed source archive is missing or changed'
    record = verified_record(engine, 'experiment_sources', ref['id'], run_id, ref['sha256'], error=error)
    if record is None:
        record = engine.store.get('experiment_sources', ref['id']) or {}
        if record.get('run_id') != run_id or sha(record.get('files')) != ref['sha256']:
            raise ValueError(error)
    files, path = record['files'], args.get('path')
    if path is None:
        value = [{k: v for k, v in row.items() if k != 'text'} for row in files.values()]
        result = _page(value, args)
        return {**result, 'view': 'index', 'source_read': False, 'evidence_sha256': args['expected_sha256']}
    if path not in files or 'text' not in files[path]:
        raise ValueError('Requested module is absent/unreadable in the executed source snapshot')
    item, view = files[path], args.get('view', 'symbols')
    source = item['text']
    if hashlib.sha256(source.encode()).hexdigest() != item['sha256']:
        raise ValueError('Archived module bytes changed')
    if view == 'source':
        offset, limit = args.get('offset', 0), args.get('limit', 4000)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 6000:
            raise ValueError('Module source reads require a bounded character window')
        end = min(len(source), offset + limit)
        result = {'text': source[offset:end], 'offset': offset, 'end': end, 'total_chars': len(source),
            'next_offset': end if end < len(source) else None, 'start_line': source.count('\n', 0, offset) + 1,
            'end_line': source.count('\n', 0, end) + 1, 'complete': offset == 0 and end == len(source)}
    else:
        entries = []
        try:
            tree = ast.parse(source) if path.endswith('.py') else None
        except (SyntaxError, ValueError):
            tree = None
        query = args.get('query', '').casefold()
        for node in ast.walk(tree) if tree else []:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (not query or query in node.name.casefold()):
                calls = sorted({ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)})
                entries.append({'symbol': node.name, 'line': node.lineno, 'end_line': node.end_lineno,
                    'offset': sum(len(line) for line in source.splitlines(keepends=True)[:node.lineno-1]),
                    'calls': calls, 'resolution': 'Static syntactic names, not a runtime call trace or gradient proof.'})
        result = _page({'symbols': entries, 'parsed': tree is not None,
            'matching_lines': [{'line': i+1, 'text': line[:240]} for i, line in enumerate(source.splitlines())
                               if query and query in line.casefold()][:40]}, args)
    return {**result, 'path': path, 'view': view, 'source_sha256': item['sha256'],
        'evidence_sha256': args['expected_sha256'], 'source_read': view == 'source' and bool(result['text']),
        'root_cause_verified': False}
