"""Resolve explicitly named settings from a selected, completed validation run."""
import hashlib
import re
from pathlib import Path

from . import hyperparameter_yaml as yaml
from .experiment_workflow import sha


def staged_inputs(engine, run_id, trial, contract, verification, *, full_text=False):
    """Read only named configuration inputs from this exact completed attempt."""
    from .check_workspace import attempt_path
    from .comparison_transport import strict_loads
    from .experiment_views import _safe_file
    name = yaml.config_path(contract.get('config_file', yaml.RUNTIME_FILE))
    names = [name, 'params.json', yaml.RECEIPT_FILE]
    mentions = '|'.join(re.escape(n) for n in {*names, Path(name).name})
    if not re.search(r'(?<![a-z0-9_./-])(?:' + mentions + r')(?![a-z0-9_/-]|\.[a-z0-9_.-])',
                     verification.replace('\\', '/'), re.I | re.ASCII):
        return None
    expected = attempt_path(engine.runner.root, run_id, trial['id'], trial['attempt'], trial['params'], trial['limits'])
    if Path(trial.get('artifact_path') or '') != expected:
        raise ValueError('Selected validation input directory differs from its historical attempt')
    files = {}
    for filename in names:
        path = _safe_file(engine, run_id, trial, filename)
        if path is None:
            files[filename] = {'status': 'unavailable'}
            continue
        with path.open('rb') as stream:
            raw = stream.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            files[filename] = {'status': 'unavailable', 'reason': 'exceeds 512 KiB input read limit'}
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if filename == name:
            valid = digest == contract['yaml_sha256']
        elif filename == 'params.json':
            valid = sha(strict_loads(raw)) == contract['params_sha256']
        else:
            valid = strict_loads(raw) == yaml.trial_receipt(contract)
        if not valid:
            raise ValueError('Saved validation input differs from admission: ' + filename)
        item = {'sha256': digest, 'bytes': len(raw), 'status': 'available'}
        if full_text or len(raw) <= 6000:
            item['text'] = raw.decode('utf-8')
        files[filename] = item
    return files


def collect(engine, run_id, evidence, supplement, selected, verification, *, include_identity=False, originals=False):
    from .experiment_files import _validation_trial
    from .fair_comparison import strict_equal
    binding = evidence['binding']
    summaries = {t['summary']['trial_id']: t for t in supplement.get('trials', [])}
    result = []
    for index, row in enumerate(evidence['records']):
        if row['id'] not in selected or row['id'] not in summaries or not _validation_trial(row, run_id):
            continue
        trial = engine.store.get('experiments', run_id + ':' + row['id']) or {}
        contract = trial.get('limits', {}).get('hyperparameter_yaml')
        if not contract:
            continue
        revision = yaml.read_version(engine, run_id, contract['revision_id'], contract['yaml_sha256'])
        body = revision['body']
        profile = body['document']['profiles'][contract['profile']]
        fields = {}
        for path, value in yaml.leaves(profile):
            name = path.rsplit('/', 1)[1].replace('~1', '/').replace('~0', '~')
            pattern = '(?:' + re.escape(path) + '|' + re.escape(name) + ')'
            if re.search(r'(?<![\w/.-])' + pattern + r'(?![\w/-]|\.[\w.-])', verification, re.ASCII):
                fields[path] = {'yaml_value': value}
        if not fields and not include_identity and not originals:
            continue
        runtime = trial['limits'].get('experiment_binding') or {}
        frozen = summaries[row['id']]
        details = frozen['evaluation_details'].get('data') or {}
        reported = trial.get('experiment_metadata', {}).get('evaluation') or {}
        evaluated = trial.get('evaluation') or {}
        if (trial.get('id') != row['id'] or trial.get('run_id') != run_id or trial['status'] != row['status']
                or not _validation_trial(trial, run_id)
                or trial.get('fingerprint') != row['fingerprint'] or trial.get('attempt') != row['attempt']
                or any(trial.get(key) != row[key] or runtime.get(key) != binding[bound] or row[key] != binding[bound]
                       for key, bound in [('research_cycle', 'cycle'), ('code_revision', 'revision'),
                                          ('code_sha256', 'code_sha256'), ('experiment_plan_sha256', 'plan_sha256')])
                or any(body['binding'].get(k) != binding[k] for k in ('cycle', 'revision', 'code_sha256', 'plan_sha256'))
                or body['yaml'] != contract['yaml'] or yaml.compact_sha(profile) != contract['effective_profile_sha256']
                or contract['profile'] != trial['params']['variant'] or sha(trial['params']) != contract['params_sha256']
                or sha(row['params']) != contract['params_sha256'] or frozen['summary']['params_sha256'] != contract['params_sha256']
                or yaml.check_result(trial, contract)
                or any(key in details and details[key] != contract[field] for key, field in (
                    ('hyperparameter_yaml_sha256', 'yaml_sha256'), ('effective_hyperparameters_sha256', 'effective_profile_sha256')))
                or not strict_equal(reported.get('actual_params'), evaluated.get('actual_params'))
                or ('fit_completion' in details and not strict_equal(reported.get('fit_completion'), details['fit_completion']))):
            raise ValueError('Saved validation configuration differs from its frozen trial or runtime attestation')
        params = dict(yaml.leaves(trial['params']))
        actual_params = reported.get('actual_params')
        if actual_params is not None and not isinstance(actual_params, dict):
            raise ValueError('Validation output has an invalid actual parameter report')
        actual = dict(yaml.leaves(actual_params)) if actual_params is not None else {}
        for path, item in fields.items():
            if path in params:
                if not strict_equal(item['yaml_value'], params[path]) or (
                        path in actual and not strict_equal(actual[path], params[path])):
                    raise ValueError('Requested configuration field conflicts with the admitted validation input: ' + path)
                item['admitted_value'] = params[path]
            if path in actual:
                item['reported_actual_value'] = actual[path]
        jobs = [job['name'] for job in evidence['ledger']['jobs'] if row['id'] in job['trial_ids']
                and job['params_sha256'] == contract['params_sha256']]
        if not jobs or any(sha(evidence['job_parameters'][name]) != contract['params_sha256'] for name in jobs):
            raise ValueError('Selected validation trial is not bound to its frozen job parameters')
        identity = {}
        if include_identity:
            projected = row.get('evaluation') or {}
            splits = projected.get('split_fingerprints') or {}
            if (frozen['summary'].get('record_projection_sha256') != sha(row)
                    or any(not strict_equal(projected[k], src.get(k)) for k in ('dataset_id', 'phase') if k in projected
                           for src in (evaluated, reported))
                    or any(not strict_equal(value, (src.get('split_fingerprints') or {}).get(k))
                           for k, value in splits.items() for src in (evaluated, reported))
                    or any(k in reported and not strict_equal(reported[k], trial['params'].get(k)) for k in ('seed', 'phase'))):
                raise ValueError('Saved validation split or trial identity differs from the frozen record')
            conflicts = sorted(path for path in params.keys() & actual.keys() if not strict_equal(params[path], actual[path]))
            parameter_status = ('not_reported' if actual_params is None else 'match' if strict_equal(actual_params, trial['params'])
                                else 'conflict' if conflicts else 'different_field_set')
            from .experiment_tools import _page
            display = _page(profile, {'offset': 0, 'limit': 6000})
            profile_view = {'read_with': {'name': 'hyperparameters.read', 'arguments': {
                'revision_id': contract['revision_id'], 'expected_sha256': contract['yaml_sha256'],
                'offset': 0, 'limit': 6000}}, 'profile': contract['profile'],
                'admitted_parameters_path': f'/records/{index}/params'}
            if display['complete']:
                profile_view['values'] = profile
            admitted = {'sha256': contract['params_sha256'], 'path': profile_view['admitted_parameters_path']}
            if _page(row['params'], {'offset': 0, 'limit': 6000})['complete']:
                admitted['values'] = row['params']
            identity = {'dataset_id': projected.get('dataset_id'), 'phase': projected.get('phase'),
                'effective_profile': profile_view, 'admitted_params': admitted,
                'reported_split_fingerprints': splits, 'reported_actual_params_sha256': sha(actual_params) if actual_params is not None else None,
                'actual_parameter_comparison': {'status': parameter_status, 'conflicting_paths': conflicts,
                    'missing_paths': sorted(params.keys() - actual.keys()) if actual_params is not None else [],
                    'additional_paths': sorted(actual.keys() - params.keys())},
                'fit_completion': details.get('fit_completion'), 'artifact_trial_path': '/trials/' + str(
                    next(i for i, t in enumerate(supplement['trials']) if t['summary']['trial_id'] == row['id'])),
                'record_projection_sha256': sha(row)}
        if originals:
            files = staged_inputs(engine, run_id, trial, contract, verification)
            if files:
                identity['staged_inputs'] = files
            elif not fields and not include_identity:
                continue
        result.append({'trial_id': row['id'], 'record_path': '/records/' + str(index),
            'units': [unit['id'] for unit in evidence['units'] if any(j in jobs for j in unit['jobs'])],
            'jobs': jobs, 'attempt': trial['attempt'], 'fingerprint': trial['fingerprint'],
            'seed': trial['params'].get('seed'), 'fit_id': (reported.get('fit_completion') or {}).get('fit_id'),
            'yaml_receipt': yaml.trial_receipt(contract), 'fields': fields, **identity})
    return {'binding': binding, 'trials': result} if result else None


def admits_historical_read(engine, run_id, context, evidence, version, args):
    """The version admitted for a selected validation trial may have the earlier, shorter binding."""
    from .experiment_evidence_handoff import KEY
    decision = context.get('experiment_decision') or {}
    selected = decision.get('selected_validation_runs') or {}
    summary = selected.get('result') or {}
    original = version['body']['binding']
    ref = selected.get('artifact_evidence')
    if (args.get('revision_id') != version['id'] or args.get('expected_sha256') != version['body']['yaml_sha256']
            or selected.get('evidence') != context.get('validation_evidence') or summary.get('binding') != evidence['binding']
            or not ref or ref != decision.get(KEY, {}).get('evidence')
            or set(original) != {'cycle', 'revision', 'code_sha256', 'plan_sha256'}
            or any(evidence['binding'].get(k) != value for k, value in original.items())):
        return False
    candidates = [r for r in summary['trials'] if r['yaml_receipt']['revision_id'] == version['id']]
    if not candidates:
        return False
    archive = engine.store.get('experiment_evidence', ref['id']) or {}
    material = archive.get('summary')
    if (archive.get('run_id') != run_id or sha(material) != ref['sha256']
            or material.get('source_evidence') != selected['evidence']):
        return False
    verified = collect(engine, run_id, evidence, material, [r['trial_id'] for r in candidates], '', include_identity=True)
    keys = ('trial_id', 'attempt', 'fingerprint', 'record_projection_sha256', 'yaml_receipt')
    return bool(verified and len(verified['trials']) == len(candidates) and all(
        all(old[k] == new[k] for k in keys) for old, new in zip(candidates, verified['trials'])))


def frozen_selection(decision):
    from .experiment_evidence_handoff import KEY
    review = decision.get(KEY, {}).get('independent_review', {})
    packet = review.get('synthesis_assignment', {}).get('context', {}).get('experiment_decision', {})
    return packet.get('selected_validation_runs') or decision.get('reviewed_revision_material', {}).get('selected_validation_runs')


def reviewed_configurations(packet):
    audits = {}
    if packet.get('saved_validation_configuration'):
        audits['saved_validation_configuration'] = packet['saved_validation_configuration']
    prior = (packet.get('prior_validation_configuration') or
             packet.get('prior_diagnostic_review', {}).get('as_observed_in_prior_review', {}).get('saved_validation_configuration'))
    if prior:
        audits['prior_validation_configuration'] = prior
    return audits


def frozen_configurations(decision):
    from .experiment_evidence_handoff import KEY
    review = decision.get(KEY, {}).get('independent_review', {})
    latest = review.get('configuration_originals') or review.get('configuration_followup') or review.get('synthesis_assignment', {})
    return {**reviewed_configurations(decision.get('reviewed_revision_material', {})),
            **reviewed_configurations(latest.get('context', {}).get('experiment_decision', {}))}


def work_order_inputs(engine, run_id, record):
    """Stage the configuration and parameter representation reviewed for each trial."""
    from .experiment_evidence_handoff import bound_evidence
    decision = engine.store.get('experiment_decisions', record.get('decision_id', '')) or {}
    selected = frozen_selection(decision)
    if not selected:
        return None
    if (decision.get('run_id') != run_id or decision.get('status') != 'approved'
            or decision.get('id') != record['decision_id'] or decision.get('evidence') != record['evidence']
            or selected['evidence'] != record['evidence'] or decision.get('proposal') != record['decision']
            or sha(record['decision']) != record['decision_sha256'] or decision['proposal_sha256'] != record['decision_sha256']):
        raise ValueError('CPU work order does not match the approved validation decision')
    base = engine.store.get('experiment_evidence', record['evidence']['id']) or {}
    if base.get('run_id') != run_id or sha(base.get('summary')) != record['evidence']['sha256']:
        raise ValueError('CPU work order validation snapshot changed')
    evidence = base['summary']
    supplement = bound_evidence(engine, run_id, decision, decision['proposal'])
    if not supplement or selected['artifact_evidence']['sha256'] != sha(supplement):
        raise ValueError('CPU work order selected a different validation supplement')
    trials = selected['result']['trials']
    verified = collect(engine, run_id, evidence, supplement, [r['trial_id'] for r in trials], '', include_identity=True)
    if not verified or len(verified['trials']) != len(trials):
        raise ValueError('Referenced validation input is unavailable')
    audits = frozen_configurations(decision)
    archived = {}
    for settings in audits.values():
        if settings['sha256'] != sha(settings['result']) or settings['result']['binding'] != evidence['binding']:
            raise ValueError('Archived input files require the approved validation review')
        for row in settings['result']['trials']:
            if row.get('staged_inputs'):
                archived.setdefault(row['trial_id'], []).append(row)
    if archived or decision.get('votes'):
        from .experiment_consensus import vote_request
        from .independent_full_review import members
        votes = decision.get('votes', [])
        if not members(votes) or not archived.keys() <= {r['trial_id'] for r in trials}:
            raise ValueError('Archived input files require the approved validation review')
        for vote in votes:
            ctx = vote_request(engine, run_id, decision, vote)['context']
            if (not vote['approve'] or vote['proposal_sha256'] != decision['proposal_sha256']
                    or ctx['decision_proposal'] != decision['proposal'] or ctx['validation_evidence'] != record['evidence']
                    or reviewed_configurations(ctx['experiment_decision']) != audits):
                raise ValueError('Archived input files differ from the approved vote material')
    inputs = []
    for old, current in zip(trials, verified['trials']):
        if any(old[k] != current[k] for k in ('trial_id', 'attempt', 'fingerprint', 'record_projection_sha256', 'yaml_receipt')):
            raise ValueError('Referenced validation trial or YAML changed')
        receipt = current['yaml_receipt']
        version = yaml.read_version(engine, run_id, receipt['revision_id'], receipt['yaml_sha256'])
        row = next(r for r in evidence['records'] if r['id'] == current['trial_id'])
        item = {'trial_id': row['id'], 'attempt': row['attempt'], 'record_path': current['record_path'],
                'params': row['params'], 'yaml': version['body']['yaml'], 'yaml_receipt': receipt}
        if row['id'] in archived:
            trial = engine.store.get('experiments', run_id + ':' + row['id'])
            files = staged_inputs(engine, run_id, trial, trial['limits']['hyperparameter_yaml'], 'params.json', full_text=True)
            for saved in archived[row['id']]:
                if any(saved[k] != current[k] for k in ('attempt', 'fingerprint', 'yaml_receipt', 'record_path')):
                    raise ValueError('Reviewed input files belong to a different validation attempt')
                if set(saved['staged_inputs']) != set(files):
                    raise ValueError('Reviewed input set differs from the archived attempt')
                for name, info in saved['staged_inputs'].items():
                    observed = files[name]
                    if (observed['status'] != 'available' or info.get('sha256') != observed['sha256']
                            or info.get('bytes') != observed['bytes'] or any(observed.get(k) != v for k,v in info.items())):
                        raise ValueError('Original validation input changed or is unavailable: ' + name)
            item['archived_json'] = {name: files[name]['text'] for name in ('params.json', yaml.RECEIPT_FILE)}
        item['files'] = {name: {'path': path, 'sha256': hashlib.sha256(raw).hexdigest()}
                         for name, (path, raw) in _input_files(len(inputs), item).items()}
        inputs.append(item)
    return {'evidence': record['evidence'], 'file_protocol': 2 if archived else 1, 'trials': inputs,
            'serialization': ('Trials with archived_json preserve the three reviewed input files byte for byte. '
                              'Other trials use the canonical admission encoding for params and trial staging encoding for the receipt.'
                              if archived else 'YAML preserves the original UTF-8 bytes. The receipt uses the trial staging encoding. The parameter file uses the canonical admission encoding.')}


def _input_files(index, trial):
    from .experiment_workflow import encoded
    root = '_validation_inputs/' + str(index) + '/'
    receipt = trial['yaml_receipt']
    params_name, params = 'admitted-params.json', encoded(trial['params']).encode()
    report = yaml.receipt_bytes(receipt)
    if 'archived_json' in trial:
        from .comparison_transport import strict_loads
        subset = trial['archived_json']
        params_name, params, report = 'params.json', subset['params.json'].encode(), subset[yaml.RECEIPT_FILE].encode()
        if (sha(strict_loads(params)) != receipt['params_sha256'] or sha(trial['params']) != receipt['params_sha256']
                or strict_loads(report) != receipt):
            raise ValueError('Diagnostic input file differs from the admitted validation configuration')
    return {'params': (root + params_name, params),
            'yaml': (root + yaml.config_path(receipt.get('config_file', yaml.RUNTIME_FILE)), trial['yaml'].encode()),
            'receipt': (root + yaml.RECEIPT_FILE, report)}


def input_files(work_order):
    originals = work_order.get('selected_validation_inputs') or {}
    if 'file_protocol' not in originals:
        return {}
    protocol = originals['file_protocol']
    if (type(protocol) is not int or protocol not in (1, 2)
            or (protocol == 2) != any('archived_json' in t for t in originals['trials'])):
        raise ValueError('Unsupported validation input file protocol')
    files = {}
    for index, trial in enumerate(originals['trials']):
        for name, (path, raw) in _input_files(index, trial).items():
            digest = hashlib.sha256(raw).hexdigest()
            if (trial['files'][name] != {'path': path, 'sha256': digest} or
                    ((name == 'yaml' or name == 'params' and 'archived_json' not in trial)
                     and trial['yaml_receipt'][name + '_sha256'] != digest)):
                raise ValueError('Diagnostic input file differs from the admitted validation configuration')
            files[path] = raw
    return files


def archived_workspace_inputs(files):
    """Recover the original inputs when copying a frozen review or CPU directory."""
    from .code_bundle import archived_params_path
    from .comparison_transport import strict_loads
    paths = {n for n in files if archived_params_path(n)}
    for prefix in {n.split('_validation_inputs/', 1)[0] for n in paths}:
        originals = input_files(strict_loads(files[prefix + 'work_order.json']))
        if ({prefix + n for n in originals if archived_params_path(n)} != {n for n in paths if n.startswith(prefix + '_validation_inputs/')}
                or any(files.get(prefix + n) != raw for n, raw in originals.items())):
            raise ValueError('Copied diagnostic input differs from the frozen work order')
    return paths
