"""Program-owned, append-only evidence continuations for a frozen capability vote.

This adapter authenticates a complete original group and each same-role child.
It does not combine different policies through the generic quorum validator.
"""
from __future__ import annotations

import copy
import asyncio

from . import independent_full_review as full, review_material_packet as packets
from .metric_contract import signature
from .architecture_validation import operation

KEY = 'metric_capability_evidence_recovery'
VERSION = 1
MAX_TARGET_CHARS = 40000
MAX_ROUNDS = 8
COLLECTION = 'stage_contexts'
IDENTITY = ('run_id', 'cycle', 'plan_sha256', 'metric_contract_sha256', 'source_work_order',
            'source_sha256', 'author_handoff_id', 'current_capability_sha256')


def supported(context):
    return packets.supported(context)


def active(engine, run_id):
    engine.check_active(run_id)
    owner = engine.store.get('runs', run_id).get('owner_instance')
    if owner is not None and owner != getattr(engine, 'instance_id', None):
        raise asyncio.CancelledError()


def schema(binding, *, diagnostic=False, material_ids=None):
    text = {'type': 'string', 'minLength': 1}
    request = {'type': 'object', 'properties': {
        'material_id': {'enum': material_ids} if material_ids is not None else text,
        'start': {'type': 'integer', 'minimum': 0},
        'end': {'type': 'integer', 'minimum': 1}, 'reason': text},
        'required': ['material_id', 'start', 'end', 'reason'], 'additionalProperties': False}
    return {'type': 'object', 'properties': {
        'binding_sha256': {'const': binding}, 'approve': {'const': False} if diagnostic else {'type': 'boolean'},
        'resolution': {'enum': ['need_evidence', 'repair_capability', 'external_blocker']
                       if diagnostic else ['approve', 'need_evidence', 'repair_capability', 'external_blocker']},
        'rationale': text, 'required_changes': {'type': 'array', 'items': text},
        'evidence_requests': {'type': 'array', 'maxItems': 8, 'items': request},
        'repair_paths': {'type': 'array', 'uniqueItems': True, 'items': text},
        'repair_tests': {'type': 'array', 'uniqueItems': True, 'items': text}},
        'required': ['binding_sha256', 'approve', 'resolution', 'rationale', 'required_changes',
                     'evidence_requests', 'repair_paths', 'repair_tests'], 'additionalProperties': False}


def original_bodies(context):
    base = packets.compile_packet(context)
    return {'root': base['root'], packets.sha(packets.encoded(['text', base['root']])): base['root'], **base['nodes']}


def validate_result(result, context, *, diagnostic=False):
    """No free-text keyword classification; the original reviewer chooses."""
    resolution = result['resolution']
    requests = result['evidence_requests']
    approved = resolution == 'approve'
    if (result['approve'] is not approved or diagnostic and approved
            or approved and (result['required_changes'] or requests)
            or not approved and not result['required_changes']
            or (resolution == 'need_evidence') != bool(requests)
            or (resolution == 'repair_capability') != bool(result['repair_paths'] and result['repair_tests'])
            or resolution != 'repair_capability' and (result['repair_paths'] or result['repair_tests'])):
        raise ValueError('Evidence resolution, objections, approval and repair scope disagree')
    bodies = original_bodies(context)
    seen, total = set(), 0
    for request in requests:
        material, start, end = request['material_id'], request['start'], request['end']
        if (material not in bodies or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(bodies[material]) or (material, start, end) in seen):
            raise ValueError('Evidence request has a foreign material or invalid/duplicate character range')
        seen.add((material, start, end)); total += end - start
    if total > MAX_TARGET_CHARS:
        raise ValueError('Request at most 40000 characters per batch; keep remaining obligations explicit')
    if resolution == 'repair_capability':
        from pathlib import PurePosixPath
        for name in result['repair_paths']:
            path = PurePosixPath(name)
            if (path.is_absolute() or '..' in path.parts or '\\' in name
                    or not name.startswith(('autoresearch/', 'tests/')) or not name.endswith('.py')):
                raise ValueError('Capability repair must name exact application/test Python source paths')
        for name in result['repair_tests']:
            path = PurePosixPath(name.split('::', 1)[0])
            if (path.is_absolute() or '..' in path.parts or '\\' in name
                    or not name.startswith('tests/test_') or path.suffix != '.py'):
                raise ValueError('Capability repair must name exact CPU test selectors')
    return True


def targets(context, requests):
    bodies = original_bodies(context)
    return [{**copy.deepcopy(r), 'sha256': packets.sha(bodies[r['material_id']])} for r in requests]


def target_fragment(target, body):
    start, end = target['start'], target['end']
    if (not isinstance(body, str) or target['sha256'] != packets.sha(body)
            or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body)):
        raise ValueError('Focused evidence changed the authenticated original material or range')
    return body[start:end]


def focus_packet(original, base):
    """A versioned derived view; all original source bytes remain addressable."""
    marker = original[KEY]
    raw = {k: v for k, v in original.items() if k != KEY}
    if (type(marker.get('version')) is not int or marker['version'] != VERSION
            or marker.get('base_source_sha256') != packets.sha(packets.encoded(raw))
            or marker.get('scientific_approval') is not False or marker.get('role') not in full.REVIEWERS
            or marker.get('mode') not in {'diagnostic', 'evidence'}):
        raise ValueError('Evidence view lacks its exact original frozen source binding')
    nodes, catalog, required = dict(base['nodes']), copy.deepcopy(base['catalog']), []
    # The original root retains its own immutable ID instead of losing the
    # shared plan/source directory when a focused root is introduced.
    original_root = packets.sha(packets.encoded(['text', base['root']]))
    nodes[original_root] = base['root']
    catalog.append({'id': original_root, 'sha256': packets.sha(base['root']), 'encoding': 'text',
                    'chars': len(base['root']), 'labels': ['Original complete review directory'], 'required': False})
    for item in catalog: item['required'] = False
    total = 0
    for target in marker['targets']:
        mid, start, end = target['material_id'], target['start'], target['end']
        body = base['root'] if mid in ('root', original_root) else base['nodes'].get(mid)
        fragment = target_fragment(target, body)
        identity = packets.sha(packets.encoded(['text', fragment]))
        nodes[identity] = fragment
        label = f'Target {mid} [{start},{end}): {target["reason"]}'
        existing = next((r for r in catalog if r['id'] == identity), None)
        if existing:
            existing['labels'].append(label); existing['required'] = True
        else:
            catalog.append({'id': identity, 'sha256': packets.sha(fragment), 'encoding': 'text',
                'chars': len(fragment), 'labels': [label], 'required': True})
        required.append(identity); total += len(fragment)
    if total > MAX_TARGET_CHARS or (marker['mode'] == 'evidence') != bool(required):
        raise ValueError('Focused evidence must preserve a complete bounded set of requested obligations')
    root = packets.encoded({'protocol': KEY, **{k: original[k] for k in IDENTITY},
        'recovery': {k: v for k, v in marker.items() if k != 'navigation_indexes'},
        'original_directory': ['material', original_root],
        'reading_policy': 'Read root and every required target. Original sources remain on demand. '
            'Use the exact material catalog to request another bounded batch if needed. '
            'Retain every unresolved objection. Reading, prior notes and checks never confer approval.'})
    if len(root) + total > full.READ_LIMIT:
        raise ValueError('Evidence request metadata and decisive source text exceed the review window')
    return {'root': root, 'nodes': nodes, 'formats': {**base['formats'], **{r['id']: r['encoding'] for r in catalog}},
        'required': sorted(set(required)), 'catalog': catalog,
        'source_sha256': packets.sha(packets.encoded(original)), 'lossless_source_sha256': base['lossless_source_sha256']}


@operation
def root_context(engine, run_id, root):
    """Authenticate the original whole group, including successful history."""
    if not getattr(engine.store, 'is_read_snapshot', False):
        with engine.store.read_snapshot() as snapshot:
            owner = copy.copy(engine); owner.store = snapshot
            return root_context(owner, run_id, root)
    from .metric_contract_recovery import CAPABILITY_REVIEWS
    if (root.get('id') != run_id + ':' + root.get('binding_sha256', '') or root.get('run_id') != run_id
            or root.get('status') not in {'accepted', 'revision_required'}
            or engine.store.get(CAPABILITY_REVIEWS, root['id']) != root
            or set(root.get('reviews', {})) != set(full.REVIEWERS)):
        raise ValueError('Evidence recovery requires its exact durable complete capability group')
    votes = [root['reviews'][r]['full_vote'] for r in full.REVIEWERS]
    full.authenticate_complete(engine.store, run_id, votes)
    contexts = []
    for role, vote in zip(full.REVIEWERS, votes):
        ballot = root['reviews'][role]
        frozen = full.authenticate(engine.store, run_id, vote)
        context = full.source_context(frozen['context'])
        if (vote['role'] != role or ballot['handoff_id'] != vote[full.KEY]['handoff_id']
                or ballot['result'] != full.answer(vote)
                or ballot['result'].get('binding_sha256') != root['binding_sha256']
                or not supported(context)):
            raise ValueError('Original capability review role, result or source drifted')
        full.validate_delivery(engine.store, run_id, vote, engine=engine)
        contexts.append(context)
    if contexts[0] != contexts[1]: raise ValueError('Capability group does not bind one exact source')
    return contexts[0]


def certificate_id(root):
    return root['id'] + ':evidence-resolution-v1'


def derive_marker(base, root, parent, role, generation):
    result = full.answer(parent)
    diagnostic = 'resolution' not in result
    if not diagnostic: validate_result(result, base)
    return {'version': VERSION, 'role': role, 'generation': generation,
        'root_review_id': root['id'], 'root_review_sha256': signature(root),
        'base_source_sha256': packets.sha(packets.encoded(packets.source(base))),
        'parent_handoff_id': parent[full.KEY]['handoff_id'], 'parent_vote_sha256': signature(parent),
        'previous_own_result': copy.deepcopy(result), 'mode': 'diagnostic' if diagnostic else 'evidence',
        'targets': [] if diagnostic else targets(base, result['evidence_requests']), 'scientific_approval': False}


def instruction(diagnostic):
    return (('Classify your original dissent without approving it. Select exact missing evidence ranges, '
             'or identify a concrete code defect with exact source files and CPU checks. ') if diagnostic else
            'Independently recheck ALL your original objections against the exact requested evidence. ') + (
        'Use reviews.read_material. All original sources remain available in the catalog. '
        'Evidence requests must use ORIGINAL material IDs (root means the original full directory), '
        'not focused fragment aliases. A batch is at most 40000 characters; explicitly retain all remaining obligations. '
        'Evidence already read with authenticated delivery is re-exhibited without another tool read. '
        'Choose need_evidence for another exact batch; repair_capability for a confirmed source defect; '
        'external_blocker only for unavailable external resources or missing operator information. '
        'Approve only after every objection is resolved; preserve all frozen scientific criteria and execution gates.')


def freeze(engine, key, value):
    active(engine, value['run_id'])
    sealed = {**value, 'id': key}
    sealed['sha256'] = signature(sealed)
    def keep(old):
        active(engine, value['run_id'])
        if old is not None and old != sealed:
            raise ValueError('Frozen evidence continuation changed; original request retained')
        return old or sealed
    return engine.store.mutate(COLLECTION, key, keep)


def validate_child(engine, run_id, root, base, role, parent, request, vote, *, check_result=True):
    frozen = full.authenticate(engine.store, run_id, vote)
    marker = frozen['context'].get(KEY, {})
    expected = request['context'][KEY]
    core = {k: v for k, v in expected.items() if k != 'navigation_indexes'}
    derived = derive_marker(base, root, parent, role, marker.get('generation'))
    original = {k: v for k, v in full.source_context(frozen['context']).items() if k != KEY}
    declared = [] if derived['mode'] == 'diagnostic' else full.answer(parent)['evidence_requests']
    actual_requests = [{k: t[k] for k in ('material_id', 'start', 'end', 'reason')} for t in marker.get('targets', [])]
    bodies = original_bodies(base)
    if (packets.source(original) != packets.source(base) or actual_requests != declared
            or any(t['sha256'] != packets.sha(bodies[t['material_id']]) for t in marker.get('targets', []))
            or full.answer(parent).get('approve') is True and not full.answer(parent).get('required_changes')
            or request.get('run_id') != run_id or type(request.get('version')) is not int or request['version'] != VERSION
            or request.get('id') != root['id'] + ':evidence:' + role + ':' + str(marker.get('generation')) + ':' + signature(parent)
            or vote['role'] != role or marker != expected
            or core != derived or request['purpose'] != 'metric_capability_evidence:' + signature(derived)
            or request['instruction'] != instruction(derived['mode'] == 'diagnostic')
            or request['schema'] != schema(root['binding_sha256'], diagnostic=derived['mode'] == 'diagnostic',
                material_ids=sorted(original_bodies(base)))
            or marker['root_review_id'] != root['id'] or marker['root_review_sha256'] != signature(root)
            or marker['parent_vote_sha256'] != signature(parent)
            or marker['parent_handoff_id'] != parent[full.KEY]['handoff_id']
            or full.source_context(frozen['context']) != request['context']
            or any(frozen['context'][k] != base[k] for k in IDENTITY)):
        raise ValueError('Evidence child changed its role, parent, scientific source or capability version')
    full.validate_delivery(engine.store, run_id, vote, engine=engine)
    if check_result: validate_result(full.answer(vote), base, diagnostic=marker['mode'] == 'diagnostic')
    return True


def authenticate_lineage(engine, run_id, root, base, leaf):
    """Authenticate a routed defect back to its actual original dissent."""
    role = leaf['role']; current = leaf; reverse = []; seen = set()
    original = root['reviews'][role]['full_vote']
    while current != original:
        hid = current[full.KEY]['handoff_id']
        if hid in seen or len(reverse) >= MAX_ROUNDS: raise ValueError('Evidence lineage is cyclic or exceeds its bound')
        seen.add(hid)
        frozen = full.authenticate(engine.store, run_id, current)
        marker = frozen['context'][KEY]
        pid = marker['parent_handoff_id']
        pinput = engine.store.get('handoff_inputs', pid) or {}
        decision = engine.store.get('decisions', pid) or {}
        parent = full.stamp_vote(role, decision.get('result', {}), pid, pinput.get('context', {}).get(full.KEY))
        request_id = root['id'] + ':evidence:' + role + ':' + str(marker['generation']) + ':' + signature(parent)
        request = engine.store.get(COLLECTION, request_id) or {}
        if request.get('sha256') != signature({k: v for k, v in request.items() if k != 'sha256'}):
            raise ValueError('Evidence lineage is missing its frozen request')
        validate_child(engine, run_id, root, base, role, parent, request, current)
        reverse.append(marker['generation']); current = parent
    if list(reversed(reverse)) != list(range(len(reverse))): raise ValueError('Evidence lineage skipped a generation')
    return True


def navigation(engine, run_id, role, root):
    """Task-scoped metadata directories, outside the raw-source body window."""
    result = {'version': 1, 'root_review_id': root['id'], 'scope': 'task/shared and own-role metadata only',
        'background': {'root_review_id': root['id'], 'read_with': 'reviews.read_material'}, 'directories': {}}
    with engine.store._read_db() as db:
        for collection in ('memory_documents', 'context_receipts', 'execution_archives', 'artifacts', 'independent_full_review_notes'):
            rows = db.execute("SELECT key,json_extract(data,'$.role'),json_extract(data,'$.title'),"
                "json_extract(data,'$.name'),json_extract(data,'$.sha256'),json_extract(data,'$.handoff_id'),"
                "json_extract(data,'$.note.key'),updated_at FROM records WHERE collection=? AND json_extract(data,'$.run_id')=? "
                "AND (json_extract(data,'$.role') IS NULL OR json_extract(data,'$.role') IN (?, 'shared')) "
                "ORDER BY key LIMIT 101", (collection, run_id, role)).fetchall()
            fields = ('id', 'role', 'title', 'name', 'sha256', 'handoff_id', 'note_key', 'updated_at')
            result['directories'][collection] = {'items': [dict(zip(fields, row)) for row in rows[:100]],
                'has_more': len(rows) > 100, 'after_key': rows[99][0] if len(rows) > 100 else None}
            for item in result['directories'][collection]['items']:
                if collection in {'context_receipts', 'execution_archives'} and item['sha256']:
                    item['read_with'] = {'name': 'context.expand', 'arguments': {
                        'receipt_id': item['id'], 'expected_sha256': item['sha256']}}
                elif collection == 'memory_documents':
                    item['read_with'] = {'name': 'memory.search', 'arguments': {'query': item['title'] or item['id']}}
                elif collection == 'artifacts':
                    item['read_with'] = {'name': 'artifacts.read', 'arguments': {'id': item['id']}}
                else:
                    item['read_with'] = {'name': 'reviews.record_notes', 'arguments': {'action': 'read', 'offset': 0, 'limit': 12}}
    return result


@operation
def checked(engine, run_id, root, *, expected=None):
    """Explicit same-source lineage adapter; never generic mixed-policy quorum."""
    if not getattr(engine.store, 'is_read_snapshot', False):
        with engine.store.read_snapshot() as snapshot:
            owner = copy.copy(engine); owner.store = snapshot
            return checked(owner, run_id, root, expected=expected)
    base = root_context(engine, run_id, root)
    if expected is not None and base != expected: raise ValueError('Recovery root differs from the current frozen plan/source')
    cert = engine.store.get(COLLECTION, certificate_id(root))
    if cert is None: return None
    if (type(cert.get('version')) is not int or cert['version'] != VERSION or cert.get('kind') != KEY
            or cert.get('root_review_sha256') != signature(root)
            or cert.get('sha256') != signature({k: v for k, v in cert.items() if k != 'sha256'})
            or cert.get('scientific_approval') is not False or cert.get('run_id') != run_id
            or cert.get('id') != certificate_id(root) or set(cert.get('chains', {})) != set(full.REVIEWERS)):
        raise ValueError('Capability evidence resolution certificate changed its exact original identity')
    for role in full.REVIEWERS:
        parent = root['reviews'][role]['full_vote']
        chain = cert['chains'][role]
        if len(chain) > MAX_ROUNDS: raise ValueError('Evidence resolution exceeds its declared convergence bound')
        for index, edge in enumerate(chain):
            request = engine.store.get(COLLECTION, edge['request_id'])
            if (not request or request['sha256'] != signature({k: v for k, v in request.items() if k != 'sha256'})
                    or request['context'][KEY]['generation'] != index):
                raise ValueError('Evidence recovery request is missing or changed its generation')
            validate_child(engine, run_id, root, base, role, parent, request, edge['vote'])
            parent = edge['vote']
        result = full.answer(parent)
        if result.get('approve') is not True or result.get('required_changes'):
            raise ValueError('Evidence recovery cannot adopt an unresolved negative vote')
    return cert
