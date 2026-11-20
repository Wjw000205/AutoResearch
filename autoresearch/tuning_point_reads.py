"""Carry role-owned validation pages between parameter points, without carrying decisions."""
from collections import defaultdict
from jsonschema import ValidationError

from .diagnostic_planning_recovery import authenticate
from .experiment_analysis import analysis_result, inspect_module
from .experiment_tools import _page, compare
from .experiment_workflow import sha
from .scope_read_session import reading, verified_record
from .training_read_dispatch import _gaps

KEY = 'tuning_point_reads'
ROLES = ('director', 'engineer', 'scout')
GUIDANCE = ('本参数点使用你自己的增量阅读清单 tuning_point_reads。通用流程要求读取的训练报告、源码与审计页，'
            '凡在 complete 内即已满足对应读取要求。直接使用自己的 prior_observations，避免重复取页或记笔记。'
            'partial 只补 missing_ranges。先读 pending_focus_trials、pending_audit_paths 和 latest_unit 的新对照，'
            '核对本点新 YAML、变化证据和反证。用本轮实际回执引用新增读取，宿主另列已核实的旧回执。'
            '自行评估当前点并独立投票。旧结论和投票不继承。')


def _summary(engine, run_id, ref):
    return verified_record(engine, 'experiment_evidence', ref['id'], run_id, ref['sha256'],
                           error='Invalid parameter review archive')['summary']


def _scope(evidence):
    return (evidence['binding'], (evidence.get('configuration_queue') or {}).get('active_dataset'),
            (evidence.get('tuning_scope') or {}).get('plan_sha256'))


def _page_identity(engine, run_id, old_ref, ref, old, new, call, result):
    """Replay the original delivered page, then compare the entire material to the new snapshot."""
    args, name = call['arguments'], call['name']
    before = {'validation_evidence': old_ref}
    after = {'validation_evidence': ref}
    if name == 'hyperparameters.read':
        from .hyperparameter_yaml import read_version, _view_with_schema
        revision = result['revision_id']
        row = read_version(engine, run_id, revision, result['sha256'])
        if (row['body']['binding'] != new['binding']
                or args.get('revision_id', revision) != revision
                or args.get('expected_sha256', result['sha256']) != result['sha256']):
            return
        if not args.get('revision_id') and (row['body']['source_evidence'] != old_ref
                or row['body']['parent'] or row['body']['controls'] != {
                    'base_unit_id': args['base_unit_id'], 'validation_seed': args['validation_seed']}):
            return
        if result == _view_with_schema(row, old, args) == _view_with_schema(row, new, args):
            return 'yaml', revision, result['sha256']
        return
    if name == 'experiments.files' and args.get('path'):
        from .experiment_files import _host_source, replay_page
        if args.get('area') == 'host' and result.get('source') == 'frozen_host_source':
            _, installed = _host_source(args['path'])
            if installed != result['file_sha256']:
                return
            after['experiment_decision'] = {'cpu_behavior_check': {'staged_observations': {
                'host_source_sha256': {args['path']: installed}}}}
        if replay_page(engine, run_id, after, args, result):
            return 'file', args['area'] + '/' + args['path'], result['sha256']
        return
    if name == 'experiments.read' and args.get('expected_sha256'):
        from .experiment_tools import read
        trial = args['trial_id']
        if (any(r['id'] == trial for r in old['records']) and any(r['id'] == trial for r in new['records'])
                and result == read(engine, run_id, new['binding']['cycle'], args)):
            return 'trial', trial + '/' + result['section'], result['content_sha256']
        return
    if args.get('evidence_id') != old_ref['id'] or args.get('expected_sha256') != old_ref['sha256']:
        return
    current = {**args, 'evidence_id': ref['id'], 'expected_sha256': ref['sha256']}
    if name == 'experiments.inspect_module' and args.get('view') == 'source' and args.get('snapshot','current') == 'current':
        if result == inspect_module(engine, run_id, before, args) and result['source_read']:
            if result['source_sha256'] == inspect_module(engine, run_id, after, current)['source_sha256']:
                return 'source', args['path'], result['source_sha256']
    if name == 'experiments.compare' and args.get('path'):
        if result == compare(engine, run_id, before, args):
            if result['content_sha256'] == compare(engine, run_id, after, current)['content_sha256']:
                return 'evidence', args['path'], result['content_sha256']
    if name == 'experiments.analyze' and args.get('kind') in ('training', 'comparison', 'ablation'):
        report = analysis_result(old, args)
        kind = args['kind']
        if result != {'analysis_id': run_id + ':analysis:' + sha(report), 'kind': kind,
                      'source': 'frozen_validation_analysis', 'evidence_sha256': old_ref['sha256'], **_page(report, args)}:
            return
        if kind == 'training' and report.get('critical_training_packet', {}).get('version') != 2:
            return
        updated = analysis_result(new, current)
        # Only the enclosing snapshot identifier may differ. Its fixed-width SHA
        # occupies the same character range. All scientific fields must match.
        if updated == {**report, 'evidence_sha256': ref['sha256']}:
            locator = args.get('trial_id') if kind == 'training' else args['unit_id']
            if kind == 'ablation':
                locator += ' / ' + args['reference_unit_id']
            return kind, locator, sha(updated)


def _owners(store, run_id, previous):
    ev = previous['evidence']['sha256']
    prefixes = ['experiment_diagnose:' + ev, 'experiment_decide:' + ev,
                'experiment_vote:' + previous['proposal_sha256']]
    owners = {}
    with store._read_db() as db:
        for prefix in prefixes:
            start = run_id + ':' + prefix + ':'
            for (owner,) in db.execute("SELECT key FROM records WHERE collection='handoffs' AND key>=? AND key<?",
                                       (start, start[:-1] + ';')):
                owners[owner] = None
    # Follow original receipts, never a predecessor's claim that it read something.
    for materials in previous.get(KEY, {}).get('roles', {}).values():
        for item in materials:
            for owner, receipt in item['receipts']:
                if owner not in owners or owners[owner] is not None:
                    owners.setdefault(owner, set()).add(receipt)
    return owners


def _collect(engine, run_id, ref, previous, *, owners=None, tools=None, include_overlapping_receipts=False):
    current = _summary(engine, run_id, ref)
    by_role = {role: {} for role in ROLES}
    notes = {role: {} for role in ROLES}
    for owner, allowed in (_owners(engine.store, run_id, previous) if owners is None else owners).items():
        h = engine.store.get('handoffs', owner) or {}
        role = h.get('to_role')
        if role not in by_role or h.get('status') != 'completed' or h.get('run_id') != run_id:
            continue
        inputs = engine.store.get('handoff_inputs', owner) or {}
        prior = (inputs.get('context') or {}).get('validation_evidence')
        if not prior or (allowed is None and prior != previous['evidence']):
            continue
        try:
            answer = engine.store.get('decisions', owner)['result']
            if authenticate(engine, run_id, role, h['purpose'],
                    {k: inputs[k] for k in ('instruction','context','schema')}, answer) != owner:
                continue
            old = _summary(engine, run_id, prior)
            if _scope(old) != _scope(current):
                continue
        except (KeyError, ValueError, TypeError, ValidationError):
            continue
        progress = engine.store.get('handoff_progress', owner) or {}
        if progress.get('run_id') != run_id or progress.get('id') != owner:
            continue
        verified = set()
        for row in progress.get('tool_results', []):
            call, result = row.get('call') or {}, row.get('result') or {}
            if not isinstance(result, dict) or (tools is not None and call.get('name') not in tools):
                continue
            receipt = result.get('receipt_sha256')
            if not result.get('text') or not receipt or (allowed is not None and receipt not in allowed):
                continue
            body = {k:v for k,v in result.items() if k != 'receipt_sha256'}
            if receipt != sha({'call': call, 'result': body}):
                continue
            try:
                identity = _page_identity(engine, run_id, prior, ref, old, current, call, body)
            except (KeyError, ValueError, TypeError, IndexError):
                continue  # Missing or changed material receives a normal fresh read.
            if not identity:
                continue
            start, end, total = (body[k] for k in ('offset','end','total_chars'))
            if not 0 <= start < end <= total:
                continue
            verified.add(receipt)
            item = by_role[role].setdefault(identity, {'kind': identity[0], 'key': identity[1],
                'sha256': identity[2], 'total_chars': total, 'ranges': [], 'receipts': []})
            extends = any(start < b and end > a for a,b in _gaps(total, item['ranges']))
            if extends:
                item['ranges'].append([start, end])
            if extends or include_overlapping_receipts:
                item['receipts'].append([owner, receipt])
        saved = engine.store.get('experiment_notes', run_id + ':' + owner) or {}
        if saved.get('run_id') == run_id and saved.get('role') == role and saved.get('handoff_id') == owner:
            for note in saved.get('notes', []):
                if note.get('receipt_sha256s') and verified.issuperset(note['receipt_sha256s']):
                    notes[role][note['key']] = note
    return ({role: list(materials.values()) for role,materials in by_role.items()},
            {role: list(rows.values())[-8:] for role,rows in notes.items()})


def build(engine, run_id, ref, previous, latest_unit):
    with reading(engine) as reader:
        roles, notes = _collect(reader, run_id, ref, previous)
    return {'version': 1, 'run_id': run_id, 'evidence': ref, 'latest_unit': latest_unit,
            'roles': roles, 'notes': notes, 'approval_inherited': False}


def packet(record):
    proof = record[KEY]
    roles = {}
    for role, items in proof['roles'].items():
        complete, partial = defaultdict(list), []
        for item in items:
            missing = _gaps(item['total_chars'], item['ranges'])
            if missing:
                partial.append({'kind': item['kind'], 'key': item['key'], 'missing_ranges': missing})
            else:
                complete[item['kind']].append(item['key'])
        roles[role] = {'complete': dict(complete), 'partial': partial,
            'prior_observations': [note['statement'] for note in proof['notes'][role]]}
    return {'decision_id': record['id'], 'sha256': sha(proof), 'latest_unit': proof['latest_unit'], 'roles': roles}


def items_for(engine, run_id, context, role):
    link = context.get(KEY)
    if not link:
        return []
    record = engine.store.get('experiment_decisions', link['decision_id']) or {}
    proof = record.get(KEY) or {}
    if (proof.get('run_id') != run_id or proof.get('evidence') != context['validation_evidence']
            or sha(proof) != link['sha256']):
        raise ValueError('Parameter reading index lost its frozen decision binding')
    return proof['roles'][role]


def for_role(context, role):
    index = context[KEY]
    materials = index['roles'][role]
    complete = {k: list(v) for k,v in materials['complete'].items()}
    current = index.get('this_point', {}).get(role)
    if current:
        for kind, keys in current.items():
            complete[kind] = sorted(set(complete.get(kind, [])) | set(keys))
    active = context['experiment_decision'].get('training_investigation', {})
    audit = active.get('saved_runtime_audit') or {}
    return {'role': role, 'latest_unit': index['latest_unit'], **materials, 'complete': complete,
        'partial': [p for p in materials['partial'] if p['key'] not in complete.get(p['kind'], [])],
        'pending_focus_trials': [t for t in active.get('focus_trial_ids', []) if t not in complete.get('training', [])],
        'pending_audit_paths': [p for p in audit.get('required_paths', []) if p not in complete.get('evidence', [])],
        'approval_inherited': False}


def this_point(reads, context):
    active = context['experiment_decision'].get('training_investigation', {})
    audit_paths = (active.get('saved_runtime_audit') or {}).get('required_paths', [])
    # analysis_receipts has already enforced complete coverage of each required audit path.
    return {r['role']: {'training': r.get('training_trials_read', []), 'source': r.get('source_paths', []),
                        'comparison': r.get('comparisons_read', []), 'evidence': audit_paths} for r in reads}
