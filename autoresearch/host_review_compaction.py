"""Versioned host-review wire projection; evidence and original requests stay intact."""
import ast
import copy
import hashlib
import gzip
import json
from collections import Counter
from pathlib import Path

from .experiment_workflow import sha

VERSION = 1
LIMITS = {'read': 25000, 'triage': 35000, 'code': 50000, 'checks': 35000}
CORE = ('limits', 'original_resource_scope', 'diagnostic_protocol')


def wire_task(task, context, system_schema):
    """Keep one exact effective schema on the wire without changing the frozen assignment."""
    from .output_contract import FINAL_SCHEMA_PREFIX
    from .prose_schema import relax
    packet = context.get('bounded_evidence_review', {})
    if (system_schema is None or packet.get('compact_review_protocol') != VERSION
            or context.get('_harness_control', {}).get('mode') != 'final_only'
            or not str(context.get('_purpose', '')).startswith('rapid_review:host-repair:')):
        return task
    prefix, marker, suffix = task.rpartition(FINAL_SCHEMA_PREFIX)
    if not marker:
        return task
    try:
        embedded = json.loads(suffix)
    except ValueError:
        return task
    # Round-trip the original formatter to exclude duplicate keys, trailing instructions
    # and non-controller text. Compare serialized schema values to distinguish true and 1.
    if (not isinstance(embedded, dict) or suffix != json.dumps(embedded, ensure_ascii=False)
            or json.dumps(relax(embedded), sort_keys=True) != json.dumps(system_schema, sort_keys=True)):
        return task
    return (prefix + '\nFINAL OUTPUT CONTRACT (authoritative): follow the Required JSON schema in the '
            'system message at the top level, without a proposal/result/stage wrapper and without '
            'extra fields. Keep each prose field concise.')


def assert_preserved(context, active, *, require_exact=False):
    packet = context.get('bounded_evidence_review', {})
    if not require_exact and packet.get('compact_review_protocol') != VERSION:
        return
    def expand(value):
        from .context_records import text_slice
        sliced = text_slice(value, active)
        if sliced is not None:
            return sliced
        if isinstance(value, dict):
            for ref,table in {'$text':'shared_context_text','$record':'shared_context_records',
                              '$catalog_record':'shared_context_catalog_records'}.items():
                if set(value)=={ref} and table in active and value[ref] in active[table]:
                    return expand(active[table][value[ref]])
            return {k:expand(v) for k,v in value.items()}
        return [expand(v) for v in value] if isinstance(value,list) else value
    if expand(active.get('context', {}).get('bounded_evidence_review')) != packet:
        raise ValueError('Context compiler removed required compact review evidence')


def activate(engine, repair_id, instruction):
    """Opt in only future requests; keep run config/experimental bindings exact."""
    from .storage import now
    record = engine.store.get('bounded_host_repairs', repair_id)
    run = engine.store.get('runs', record['run_id'])
    if run['status'] != 'paused':
        raise ValueError('Pause the task before activating compact review')
    if record.get('compact_review_protocol') == VERSION:
        return record
    before = copy.deepcopy(record)
    archive = engine.run_dir(record['run_id']) / ('before-context-compaction-' + sha(record) + '.json.gz')
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
    if not archive.exists():
        archive.write_bytes(gzip.compress(raw, mtime=0))
    if gzip.decompress(archive.read_bytes()) != raw:
        raise ValueError('Original context archive changed')
    record['compact_review_protocol'] = VERSION
    record['compact_transition'] = {'original_record_sha256': sha(before), 'archive': str(archive),
        'instruction': instruction, 'created_at': now(), 'paid_requests_unchanged': True,
        'legacy_pending_policy': 'Read back only after explicit resume; unknown work stays protected. Never retransmit a large packet beyond the new admission ceiling.'}
    updated = copy.deepcopy(run)
    updated.setdefault('state', {})['host_review_token_policy'] = {'version': VERSION, 'estimated_input_limits': LIMITS}
    assert updated['config'] == run['config']
    engine.store.batch(puts=[('bounded_host_repairs', repair_id, record), ('runs', run['id'], updated)],
        expected=[('bounded_host_repairs', repair_id, before), ('runs', run['id'], run)])
    engine.store.event(run['id'], 'review.context_policy_activated',
        '已启用分阶段精简上下文和输入估算上限；已付费请求保留，任务继续暂停。',
        {'repair_id': repair_id, 'input_limits': LIMITS})
    return record


def core(scope):
    return {k: copy.deepcopy(scope[k]) for k in CORE if k in scope}


def requirements(scope):
    monitor = scope.get('original_disk_watchdog')
    reference = sha(monitor) if monitor is not None else scope.get('original_monitor_reference', {}).get('sha256')
    return {'core': core(scope), 'original_monitor_sha256': reference}


def selectors(catalog):
    paths = sorted({r['path'] for r in catalog})
    labels = {path: 'F' + str(i) for i,path in enumerate(paths)}
    counts = Counter((r['path'],r['symbol']) for r in catalog)
    mapping = {labels[r['path']] + '::' + r['symbol'] +
               ('@'+str(r['start_line']) if counts[r['path'],r['symbol']] > 1 else ''): r for r in catalog}
    if len(mapping) != len(catalog):
        raise ValueError('Location symbols are not unique within their file')
    files = [{'file_id': labels[path], 'path': path,
              'editable': all(r['editable'] for r in catalog if r['path'] == path)} for path in paths]
    return mapping, files


def compact_work_order(order, files):
    """For new native turns only: big indexes stay in exact read-only sidecars."""
    for key in ('source_manifest', 'location_catalog'):
        if key not in order:
            continue
        value = order.pop(key)
        path = '_control/' + key + '.json'
        files[path] = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
        order[key + '_reference'] = {'path': path, 'sha256': sha(value),
            'scope': 'Exact read-only host index; inspect specific entries only when needed.'}
    order['context_policy'] = 'Read EDIT_TASKS.md and current feedback first. Do not dump complete source manifests/location catalogs into the model context.'


def historical_findings(value):
    # Remove only obsolete identity/reference arrays, never prose or outcomes.
    if isinstance(value, dict):
        return {k: historical_findings(v) for k,v in value.items() if k not in ('packet_sha256', 'section_ids')}
    return [historical_findings(v) for v in value] if isinstance(value, list) else copy.deepcopy(value)


def tasks(record):
    path = Path(record['job']['workspace']) / 'work_order.json'
    order = json.loads(path.read_bytes())
    return [{'path': t['location']['path'], 'symbol': t['location']['symbol'],
             **{k: t[k] for k in ('problem','change','acceptance')}} for t in order.get('edit_tasks', [])]


def project(record, phase, payload):
    scope = payload.get('review_scope', payload)
    reduced = core(scope)
    current_tasks = tasks(record)
    if phase == 'read':
        paths = {s['path'] for s in payload['sections']}
        reduced['current_file_requirements'] = [
            {k:t[k] for k in ('path','symbol','problem','acceptance')} for t in current_tasks if t['path'] in paths]
        if scope.get('original_disk_watchdog') is not None:
            reduced['original_monitor_reference'] = {'sha256': sha(scope['original_disk_watchdog']),
                'scope': 'Complete baseline monitor is supplied in triage/final; this reference grants no reading credit.'}
        result = {'sections': copy.deepcopy(payload['sections']), 'review_scope': reduced,
                  'material_sha256': sha(scope),
                  'reading_scope': 'Current source plus exact execution constraints; observations only, no approval.'}
        return result, None
    reduced['original_disk_watchdog'] = copy.deepcopy(scope.get('original_disk_watchdog'))
    if current_tasks:
        reduced['historical_findings_reference'] = {
            'sha256': sha(scope.get('reviewer_findings', {})),
            'scope': 'Initial historical vetoes remain archived. Current full outstanding edit requirements follow below; no old approval is inherited.'}
    else:
        reduced['historical_findings'] = historical_findings(scope.get('reviewer_findings', {}))
        reduced['historical_findings_scope'] = 'Initial review requirements; historical opinions are not inherited approval.'
    reduced.update(rationale=scope.get('rationale'), test_plan=copy.deepcopy(scope.get('test_plan')),
        changes=[{k:r[k] for k in ('path','reason') if k in r} for r in scope.get('changes', [])],
        current_edit_tasks=current_tasks)
    if scope.get('repair_responses'):
        reduced['repair_responses'] = [{k:copy.deepcopy(v) for k,v in r.items() if k not in ('verified_locations',)}
                                       for r in scope['repair_responses']]
    catalog = scope.get('location_catalog')
    mapping = None
    if catalog:
        mapping, files = selectors(catalog)
        if phase == 'triage':
            targets = {(t['path'],t['symbol']) for t in current_tasks}
            targets |= {(p['path'],p['symbol']) for r in scope.get('repair_responses', []) for p in r.get('locations', [])}
            mapping = {k:r for k,r in mapping.items() if r['symbol'] in ('<module>','<new file>') or (r['path'],r['symbol']) in targets}
        reduced['location_files'] = files
        reduced['location_binding_sha256'] = sha(catalog)
        reduced['location_format'] = 'Select an exact F<number>::qualified_symbol from the response schema. Host binds SHA and line ranges; no reading credit or approval is implied.'
    result = {k:copy.deepcopy(v) for k,v in payload.items() if k != 'review_scope'} if 'review_scope' in payload else {}
    result['review_scope'] = reduced
    result['material_sha256'] = sha(scope)
    return result, mapping


def vote_packet_matches(purpose, packet, answer):
    expected = packet.get('packet_sha256')
    if packet.get('receipt_bound_vote') == 1:
        return (purpose == 'rapid_review:host-repair:' + str(expected)
                and expected == sha({k: v for k, v in packet.items() if k != 'packet_sha256'})
                and answer.get('packet_sha256', expected) == expected)
    return expected is not None and answer.get('packet_sha256') == expected


def normalize(record, req):
    packet = req['context']['bounded_evidence_review']
    answer = copy.deepcopy(req['answer'])
    if packet.get('compact_review_protocol') != VERSION or packet['phase'] == 'read':
        return answer
    if not vote_packet_matches(req['purpose'], packet, answer):
        raise ValueError('Host vote belongs to a different packet')
    answer.setdefault('packet_sha256', packet['packet_sha256'])
    material = record.get('compact_material', {})
    if (packet['candidate_sha256'] != record['delivery']['all_files_sha256']
            or material.get('sha256') != sha(material.get('body'))
            or material['sha256'] != packet['payload']['material_sha256']):
        raise ValueError('Compact review lost its frozen candidate material')
    catalog = material['body']['location_catalog']
    if sha(catalog) != packet['payload']['review_scope']['location_binding_sha256']:
        raise ValueError('Compact location binding changed')
    mapping, files = selectors(catalog)
    if files != packet['payload']['review_scope']['location_files']:
        raise ValueError('Compact file table changed')
    for change in answer['required_changes']:
        if change['location_id'] not in mapping:
            raise ValueError('Unknown compact modification location')
        change['location_id'] = mapping[change['location_id']]['location_id']
    return answer


def alias_catalog(context, schema):
    """Reconstruct visible selector names for bounded formatting correction."""
    packet = context.get('bounded_evidence_review', {})
    if packet.get('compact_review_protocol') != VERSION:
        return None
    scope = packet['payload']['review_scope']
    files = {r['file_id']:r for r in scope['location_files']}
    declared = schema['properties']['required_changes']['items']['properties']['location_id']['enum']
    result = []
    for alias in declared:
        label, symbol = alias.split('::', 1)
        if label not in files or not symbol:
            raise ValueError('Invalid compact location schema')
        result.append({'location_id': alias, 'path': files[label]['path'], 'symbol': symbol,
                       'editable': files[label]['editable']})
    return result


def interrupted_legacy_read(harness, record, role, req):
    """Local proof only, after normal user-resumed recovery has read the old turn.

    Never invokes a client, retires unknown work, or grants reading credit.
    A completed original response must be recovered, not replaced.
    """
    from .engine import Harness, digest
    from .codex_reasoning import key_for
    packet = req['context'].get('bounded_evidence_review', {})
    if (packet.get('compact_review_protocol') or packet.get('phase') != 'read'
            or packet.get('role') != role or packet.get('host_repair_id') != record.get('id')
            or packet.get('candidate_sha256') != record.get('delivery', {}).get('all_files_sha256')
            or req.get('answer') or not hasattr(harness, 'intelligence')):
        return None
    engine = harness.engine
    if engine.store.get('runs', record['run_id'])['status'] != 'running':
        return None
    key = Harness.decision_key(record['run_id'], role, req['purpose'], req['instruction'], req['context'], req['schema'])
    owner = engine.store.get('handoffs', key) or {}
    frozen = engine.store.get('handoff_inputs', key) or {}
    if (owner.get('run_id') != record['run_id'] or owner.get('to_role') != role
            or owner.get('status') not in ('running','blocked','interrupted')
            or owner.get('input_hash') != digest({k:req[k] for k in ('instruction','context','schema')})
            or any(frozen.get(k) != req[k] for k in ('instruction','context','schema'))
            or engine.store.get('decisions', key)):
        return None
    progress = engine.store.get('handoff_progress', key) or {}
    slots = [progress.get('finalization', {}), *progress.get('finalization_history', [])]
    proofs = []
    for slot in slots:
        prepared = slot.get('prepared')
        if not prepared or not slot.get('dispatched_at'):
            continue
        if slot.get('provider_id') != 'local_codex':
            return None
        native_key, operation = key_for(harness.intelligence, record['run_id'], **prepared)
        native = engine.store.get('codex_reasoning', native_key) or {}
        cp = native.get('checkpoint', {})
        reservation = native.get('reservation', {})
        budget = engine.store.get('budgets', record['run_id'], {})
        accounted = [r for r in [*budget.get('records', []), *budget.get('pending', {}).values()]
                     if r.get('id') == reservation.get('id') and r.get('operation_id') == operation
                     and r.get('run_id') == record['run_id'] and r.get('role') == role and r.get('provider') == 'local_codex']
        if (native.get('result') or native.get('run_id') != record['run_id'] or native.get('role') != role
                or len(accounted) != 1
                or native.get('operation_id') != operation or cp.get('status') != 'interrupted'
                or native.get('schema') != prepared['schema'] or cp.get('version') != 1
                or cp.get('recovered_read_only') is not True or cp.get('reasoning_only') is not True
                or cp.get('operation_id') != native_key or cp.get('run_id') != record['run_id']
                or not cp.get('thread_id') or not cp.get('turn_id')
                or not cp.get('turn_dispatched_at')
                or cp.get('instruction_sha256') != hashlib.sha256(str(native.get('instruction','')).encode()).hexdigest()
                or cp.get('requested_effort') != native.get('selection', {}).get('effort')
                or cp.get('native_tool_violation') or cp.get('cleanup_error_code')
                or cp.get('non_retryable_server_error')
                or cp.get('requested_model') != native.get('selection', {}).get('model')
                or any(m.get('phase') in ('final_answer', None) and m.get('text', '').strip()
                       for m in cp.get('public_messages', []))):
            return None
        proofs.append({'native_id': native_key, 'checkpoint_sha256': sha(cp), 'status': 'interrupted'})
    return {'handoff_id': key, 'receipts': proofs, 'reading_credit_granted': False} if proofs else None


class ReadReuse:
    """Only same-role authenticated exact pages, with unchanged dependency component."""
    def __init__(self, engine, record, scope):
        self.engine, self.record, self.scope = engine, record, scope
        self.valid = {}; self.affected = {}; self.graph = {}; self.dynamic = set()
        root = Path(record['job']['workspace'])
        self.current_targets = {t['path'] for t in tasks(record)} if (root/'work_order.json').is_file() else set()
        files = {p for p in record['delivery']['files'] if p.endswith('.py') and not p.startswith('_evidence/')}
        for path in files:
            self.graph.setdefault(path, set())
            tree = ast.parse((root/path).read_text(encoding='utf-8'))
            package = path.split('/')[:-1]
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ''
                    if name in ('__import__', 'import_module', 'spec_from_file_location', 'exec_module', 'eval', 'exec'):
                        self.dynamic.add(path)
                modules = []
                if isinstance(node, ast.Import):
                    modules = [n.name for n in node.names]
                elif isinstance(node, ast.ImportFrom):
                    prefix = '.'.join(package[:len(package)-node.level+1]) if node.level else ''
                    module = '.'.join(x for x in (prefix, node.module) if x)
                    modules = [module, *[module+'.'+n.name for n in node.names]]
                for module in modules:
                    name = module.replace('.', '/') + '.py'
                    for other in (name, 'tests/'+name):
                        if other in files:
                            self.graph[path].add(other); self.graph.setdefault(other,set()).add(path)
    def changed_component(self, old):
        identity = old['delivery']['all_files_sha256']
        if identity not in self.affected:
            current = self.record['delivery']['files']; prior = old['delivery']['files']
            delta = {p for p in set(current)|set(prior) if current.get(p)!=prior.get(p)}
            changed = {p for p in delta if p.endswith('.py')} | self.current_targets
            metadata = {'work_order.json','AGENTS.md','EDIT_TASKS.md','_delivery/changes.json',
                        '_delivery/rationale.md','_delivery/test_plan.json','_delivery/repair_responses.json',
                        '_delivery/repair_locations.json','_delivery/source_bindings.json',
                        '_control/source_manifest.json','_control/location_catalog.json'}
            # Data/config/fixture changes can affect unchanged code without a
            # Python import edge. Do not infer dependency equivalence there.
            if any(p not in metadata and not p.endswith('.py') for p in delta):
                changed |= set(self.graph)
            if changed:
                changed |= self.dynamic
            todo = list(changed)
            while todo:
                for other in self.graph.get(todo.pop(), ()):
                    if other not in changed:
                        changed.add(other); todo.append(other)
            self.affected[identity] = changed
        return self.affected[identity]
    def group(self, role, sections):
        from .parameter_resolution import owner
        from .host_read_citations import validated, annotations
        wanted = {s['id']:s for s in sections}
        for old in reversed(self.record.get('revision_history', [])):
            if not old.get('delivery') or any(s['path'] in self.changed_component(old) for s in sections):
                continue
            found, proofs = {}, []
            for req in old.get('acceptance', {}).get('source_reads', {}).get(role, {}).values():
                packet = req['context'].get('bounded_evidence_review', {})
                if (not req.get('answer') or packet.get('role') != role or packet.get('phase') != 'read'
                        or packet.get('candidate_sha256') != old['delivery']['all_files_sha256']
                        or packet.get('host_repair_id') != self.record['id']):
                    continue
                payload = packet['payload']
                if requirements(payload.get('review_scope', {})) != requirements(self.scope):
                    continue
                matched = [s for s in payload.get('sections', []) if s.get('id') in wanted and s == wanted[s['id']]]
                if not matched:
                    continue
                identity = sha(req)
                if (role, identity) not in self.valid:
                    _, hid, children = validated(self.engine, self.record['run_id'], role, req)
                    self.valid[role,identity] = (hid, children)
                observations = {o['section_id']:o for o in annotations(req)}
                for s in matched:
                    found[s['id']] = observations[s['id']]
                hid, children = self.valid[role,identity]
                proofs.append({'handoff_id': hid, 'request_sha256': identity,
                               'candidate_sha256': packet['candidate_sha256'],
                               **({'correction_handoff_ids':children} if children else {})})
            if set(found) == set(wanted):
                return {'observations': [found[s['id']] for s in sections], 'source_requests': proofs,
                        'current_candidate_sha256': self.record['delivery']['all_files_sha256'],
                        'same_role': role, 'approval_inherited': False,
                        'scope': 'Exact source and dependency component unchanged; prior observations only, fresh final judgment required.'}
        return None
