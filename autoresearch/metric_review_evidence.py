"""Freeze actual source-bound CPU receipts once per capability/source identity.

These are implementation checks, never model approval or scientific results.
Unrelated report updates cannot change a frozen review on replay.
"""
from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .metric_contract import signature
from .storage import now

REPORTS=('metric-dual-review-final','independent-metric-dual-review-final')


def identity(engine,run_id,plan,record,current):
    source={'version':1,'run_id':run_id,'cycle':engine.cycle(run_id),'plan_sha256':signature(plan),
        'source_work_order':record['id'],'source_sha256':signature(record['source']),
        'capability_sha256':signature(current)}
    return run_id+':metric-capability-check-evidence:'+signature(source),source


def current(engine,run_id,plan,record,sources):
    key,source=identity(engine,run_id,plan,record,sources)
    value=engine.store.get('stage_contexts',key)
    if value is None:return None
    if (value.get('id')!=key or value.get('version') not in (1,2) or value.get('kind')!='metric_capability_check_evidence' or value.get('run_id')!=run_id
            or value.get('source')!=source or value.get('scientific_approval') is not False
            or value.get('execution_authorized') is not False
            or value.get('sha256')!=signature({k:v for k,v in value.items() if k!='sha256'})):
        raise ValueError('Frozen capability check evidence changed its source identity')
    return value


def _document(root, path, expected=None):
    """Read one original byte version, without granting general file access."""
    from .deletion import _plain_path
    path = Path(path).absolute()
    directory = root / 'outputs' / 'diagnostics'
    if ('..' in path.parts or not path.is_relative_to(directory)
            or not path.resolve().is_relative_to(directory.resolve())):
        raise ValueError('Check evidence must stay inside outputs/diagnostics')
    _plain_path(path, root)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError('Check evidence bytes differ from the original report hash')
    return {'path': path.relative_to(root).as_posix(), 'sha256': digest,
            'encoding': 'utf-8', 'text': raw.decode('utf-8')}, raw


def _junit(raw):
    tree = ET.fromstring(raw)
    suites = [{k: int(s.attrib[k]) for k in ('tests','failures','errors','skipped')}
              for s in tree.iter('testsuite')]
    if not suites or any(v < 0 for s in suites for v in s.values()):
        raise ValueError('JUnit must contain nonnegative actual suite counts')
    failures = []
    for case in tree.iter('testcase'):
        for issue in case:
            if issue.tag in ('failure', 'error'):
                failures.append({'classname': case.get('classname'), 'name': case.get('name'),
                    'outcome': issue.tag, 'message': issue.get('message'), 'traceback': issue.text or ''})
    return suites, failures


def _feedback(engine, run_id, source):
    """Freeze authentic prior objections once, never borrow their approval."""
    from .metric_contract_recovery import CAPABILITY_REVIEWS
    from . import independent_full_review as full
    expected = {key: source[key] for key in
                ('cycle', 'plan_sha256', 'source_work_order', 'source_sha256')}
    result = []
    for row in sorted(engine.store.list(CAPABILITY_REVIEWS, run_id), key=lambda r: r['id']):
        if row.get('status') != 'revision_required':
            continue
        votes = []
        for role, review in sorted(row['reviews'].items()):
            # Scope selection grants no trust. Unrelated historical protocols
            # need not be migrated to review this source; selected predecessors
            # must then pass every original authentication check below.
            previous = engine.store.get('handoff_inputs', review.get('handoff_id'), {}) or {}
            context = previous.get('context', {})
            if (any(context.get(key) != value for key, value in expected.items())
                    or context.get('metric_contract_sha256') != source['metric_contract_sha256']
                    or context.get('current_capability_sha256') == source['capability_sha256']):
                continue
            vote = review.get('full_vote')
            if not vote:
                raise ValueError('Historical capability feedback requires its explicit full-review receipt')
            frozen = full.authenticate(engine.store, run_id, vote)
            context = frozen['context']
            if (any(context.get(key) != value for key, value in expected.items())
                    or context.get('metric_contract_sha256') != source['metric_contract_sha256']
                    or context.get('current_capability_sha256') == source['capability_sha256']):
                continue
            binding = row.get('binding_sha256')
            owner = engine.store.get('handoffs', review['handoff_id']) or {}
            if (row.get('run_id') != run_id or row.get('id') != run_id + ':' + str(binding)
                    or role != vote.get('role') or review['handoff_id'] != vote[full.KEY]['handoff_id']
                    or review.get('result') != full.answer(vote)
                    or review['result'].get('binding_sha256') != binding
                    or not full.purpose_matches(owner.get('purpose'), 'metric_contract_capability_review:' + binding)):
                raise ValueError('Historical capability feedback changed its exact source or review receipt')
            votes.append({'role': role, 'handoff_id': review['handoff_id'],
                'source_capability_sha256': context['current_capability_sha256'], 'result': review['result']})
        if votes:
            if {v['role'] for v in votes} != set(full.REVIEWERS):
                raise ValueError('Historical capability rejection lacks its complete authenticated reviewer group')
            result.append({'review_id': row['id'], 'review_sha256': signature(row), 'votes': votes,
                           'scientific_approval': False, 'reading_credit_granted': False})
    return result


def freeze(engine,run_id,plan,record,sources):
    saved=current(engine,run_id,plan,record,sources)
    if saved is not None:return saved
    key,source=identity(engine,run_id,plan,record,sources)
    root=Path(__file__).resolve().parents[1];reports=[]
    expected={row['path']:row['sha256'] for row in sources}
    directory=root/'outputs'/'diagnostics'
    paths={directory/(name+'.json') for name in REPORTS}
    # New executions publish immutable UUID-named receipts. Retain legacy
    # receipts and consider all exact-source matches, never the newest report.
    for name in REPORTS:
        paths.update(directory.glob(name+'-*.json'))
    controls = set(directory.glob('*baseline-control-*.json'))
    paths.update(controls)  # Diagnostic controls can never count as a passing acceptance check.
    for path in sorted(paths):
        if not path.is_file():continue
        raw=None
        try:
            report_document, raw = _document(root, path)
            report=json.loads(raw)
            if not isinstance(report,dict) or not isinstance(report.get('source_hashes',{}),dict):
                raise ValueError('Check report must be an object with a source hash mapping')
            if any(report.get(field) is not None and not isinstance(report[field], str)
                   for field in ('junit_path', 'log_path')):
                raise ValueError('Check report evidence paths must be strings')
        except (OSError,ValueError,TypeError) as exc:
            reports.append({'path':path.relative_to(root).as_posix(),
                'sha256':hashlib.sha256(raw).hexdigest() if raw is not None else None,
                'applicable':False,'diagnostic':{'code':'invalid_check_report',
                    'error_type':type(exc).__name__,'message':str(exc)[:500]}})
            continue
        hashes=report.get('source_hashes',{})
        mismatch=[p for p,h in expected.items() if hashes.get(p)!=h]
        documents = {'report': report_document}
        diagnostics, suites, failures = [], [], []
        authentic = False
        log_authentic = False
        if report.get('junit_path') and isinstance(report.get('junit_sha256'), str):
            try:
                document, raw_junit = _document(root, report['junit_path'], report['junit_sha256'])
                documents['junit'] = document
                suites, failures = _junit(raw_junit)
                authentic = True
            except (OSError, KeyError, ValueError, ET.ParseError) as exc:
                diagnostics.append({'kind': 'junit', 'error_type': type(exc).__name__, 'message': str(exc)})
        if report.get('log_path') and isinstance(report.get('log_sha256'), str):
            try:
                document, _ = _document(root, report['log_path'], report['log_sha256'])
                documents['log'] = document
                log_authentic = True
            except (OSError, ValueError) as exc:
                diagnostics.append({'kind': 'log', 'error_type': type(exc).__name__, 'message': str(exc)})
        if (path in controls and report.get('version') == 1
                and report.get('provenance') == 'independent_isolated_offline_cpu_baseline_control_no_live_model_no_research_writes'
                and isinstance(report.get('runs'), list)):
            # Explicit diagnostic adapter: preserve each original control log
            # and JUnit body. It never counts as a passing acceptance report.
            for index, control in enumerate(report['runs']):
                for kind in ('junit', 'log'):
                    try:
                        if not isinstance(control, dict) or not isinstance(control.get(kind + '_path'), str):
                            raise ValueError('Baseline control has no typed original evidence path')
                        expected_hash = control.get(kind + '_sha256')
                        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
                            raise ValueError('Baseline control has no original evidence hash')
                        document, original_control = _document(root, control[kind + '_path'], expected_hash)
                        documents['control.' + str(index) + '.' + kind] = document
                        if kind == 'junit':
                            _, original_failures = _junit(original_control)
                            failures.extend([{**case, 'baseline_control': index} for case in original_failures])
                    except (OSError, ValueError, ET.ParseError) as exc:
                        diagnostics.append({'kind': 'baseline_control.' + str(index) + '.' + kind,
                            'error_type': type(exc).__name__, 'message': str(exc)})
        reports.append({'path':path.relative_to(root).as_posix(),'sha256':hashlib.sha256(raw).hexdigest(),
            'kind': 'baseline_control' if path in controls else 'source_bound_check',
            'documents': documents, 'failure_cases': failures, 'evidence_diagnostics': diagnostics,
            'provenance':report.get('provenance'),'exit_code':report.get('exit_code'),
            'summary':report.get('summary',report.get('pytest_summary')),'actual_junit_summary':suites,'command':report.get('command'),
            'source_unchanged_during_checks':report.get('source_unchanged_during_checks'),
            'source_mismatch_paths':mismatch,'junit_sha256':report.get('junit_sha256'),
            'actual_junit_authenticated':bool(authentic), 'actual_log_authenticated':log_authentic,
            'applicable':path not in controls and not mismatch and authentic and log_authentic
                and report.get('source_unchanged_during_checks') is True
                and report.get('provenance')=='actual_isolated_cpu_checks_not_research_measurements'
                and type(report.get('exit_code')) is int})
    feedback_source = {**source, 'metric_contract_sha256': signature(plan.get('metric_contract'))}
    body={'id':key,'run_id':run_id,'kind':'metric_capability_check_evidence','version':2,'source':source,
        'prior_capability_feedback': _feedback(engine, run_id, feedback_source),
        'reports':reports,'status':'matching_checks_available' if any(r['applicable'] and r['exit_code']==0
            and sum(s['tests']-s['skipped'] for s in r['actual_junit_summary'])>0
            and all(s['errors']==0 and s['failures']==0 for s in r['actual_junit_summary']) for r in reports)
            else 'no_matching_successful_checks','scientific_approval':False,'execution_authorized':False,'created_at':now()}
    value={**body,'sha256':signature(body)}
    engine.store.mutate('stage_contexts',key,lambda previous:previous or value)
    return current(engine,run_id,plan,record,sources)
