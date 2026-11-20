"""Forward-only document supplement scheduler; historical packets are evidence only.

Version 1 adapts existing rendered sections without changing the author's SPEC.
Frozen module ownership constrains prose markers. Unresolved units require
bounded author clarification, subject to independent review.
"""
from __future__ import annotations

import copy
import re
from functools import lru_cache
from . import implementation_acceptance as policy
from . import implementation_acceptance_flow as flow
from .implementation_review_routing import digest

KEY = 'implementation_document_review_scope'
SLOT = 'scoped_supplement'
ROLES = ['director', 'engineer', 'scout']


def completed(state, row):
    return any(s.get(SLOT) and s.get('complete') and s['revision'] == row['index']
               for s in state.get(policy.FIELD, {}).get('rounds', []))


def units(engine, run_id, row, contract):
    """Use authenticated render positions, never guessed headings or offsets."""
    from .document_index import load
    from .implementation_document_readability import HEADINGS
    manifest, text = load(engine, run_id, row['document']['document_id'], row['document']['sha256'])
    if manifest['sections'] != row['document']['sections']:
        policy.fail('Scoped document index changed')
    headings = {**HEADINGS, 'architecture_traceability': '架构要求与实现验收对应',
                'comparison_transport': '比较数据传递合同'}
    result = []
    for field in sorted(set().union(*(set(c['fields']) for c in contract['criteria']))):
        if field not in row['spec']:
            continue
        sections = [s for s in manifest['sections'] if s['title'].strip('# ') in {field, headings.get(field, field)}]
        if len(sections) != 1:
            policy.fail('Cannot bind exact rendered field: ' + field)
        section = sections[0]
        # Blank-line paragraphs and individual table rows retain their real
        # section identity and absolute Unicode positions (including whitespace).
        body = text[section['start']:section['end']]
        for match in re.finditer(r'[^\n]+(?:\n(?!\n|\|)[^\n]+)*\n*', body):
            start, end = section['start'] + match.start(), section['start'] + match.end()
            result.append({'id': digest([row['document']['sha256'], field, start, end]),
                'field': field, 'text': text[start:end], 'range': {**section, 'start': start, 'end': end,
                    'chars': end-start, 'section_start': section['start']},
                'criteria': [c['id'] for c in contract['criteria'] if field in c['fields']]})
    return result


def assignments(atoms, mapping, source, endpoint_bindings=()):
    by_id = {a['id']: a for a in atoms}
    if len(mapping) != len(atoms) or {a['unit_id'] for a in mapping} != set(by_id):
        policy.delivery_error('Scope clarification must cover each exact unit once')
    if any(a['role'] not in ROLES or (required_roles(by_id[a['unit_id']]) and
           a['role'] not in required_roles(by_id[a['unit_id']])) for a in mapping):
        policy.delivery_error('Scope clarification changed a fixed science responsibility')
    endpoint_roles = clarified_endpoint_roles(source, endpoint_bindings)
    if any(not required_roles(by_id[a['unit_id']]) and endpoint_roles.get(a['unit_id'])
           and a['role'] not in endpoint_roles[a['unit_id']] for a in mapping):
        policy.delivery_error('Scope clarification conflicts with frozen endpoint ownership')
    tasks = []
    for role in ROLES:
        selected = [by_id[a['unit_id']] for a in mapping
                    if role in (required_roles(by_id[a['unit_id']]) or
                                endpoint_roles.get(a['unit_id']) or [a['role']])]
        if selected:
            tasks.append({'stage': 'role', 'role': role, 'units': selected, 'self_check': role == 'engineer'})
            if role == 'engineer':
                tasks.append({'stage': 'author_peer', 'role': 'director', 'units': selected, 'self_check': False})
    owners = {p: g['role'] for g in source['body']['groups'] for p in g['paths']}
    for edge in source['body']['declared_interfaces']:
        if owners[edge['consumer']] == owners[edge['provider']]:
            continue
        for side in ('consumer', 'provider'):
            path, marker = edge[side], edge[side + '_marker']
            selected = [a for a in atoms if exact_endpoint(path, marker, a['text'])]
            binding = next((b for b in endpoint_bindings if b['endpoint_id'] == digest([edge, side])), None)
            if not selected and binding:
                candidates = endpoint_candidates(atoms, owners[path])
                if (not binding['unit_ids'] or len(set(binding['unit_ids'])) != len(binding['unit_ids'])
                        or not set(binding['unit_ids']) <= {a['id'] for a in candidates}
                        or not binding['explanation'].strip()):
                    policy.delivery_error('Endpoint clarification exceeds frozen ownership or canonical units')
                selected = [a for a in candidates if a['id'] in binding['unit_ids']]
            if not selected:
                policy.delivery_error('Declared endpoint lacks an exact rendered location: ' + path + ':' + marker)
            role = owners[path]
            task = {'stage': 'endpoint', 'role': role, 'units': selected,
                    'endpoint': {**edge, 'side': side}, 'self_check': role == 'engineer'}
            tasks.append(task)
            if role == 'engineer':
                tasks.append({**task, 'stage': 'endpoint_peer', 'role': 'director', 'self_check': False})
    return [policy.sealed(t) for t in tasks]


def endpoint_candidates(atoms, role):
    # Unbound legacy prose is a location candidate, never an ownership grant.
    # The frozen endpoint owner and its independent peer still review it.
    return [a for a in atoms if not required_roles(a) or role in required_roles(a)]


def endpoint_locations(atoms, role):
    """Locate legacy headings before selecting assertions; never infer an owner."""
    groups = []
    current = None
    for atom in atoms:
        if re.match(r'^\s*#{1,6}\s', atom['text']) or current is None or current['field'] != atom['field']:
            current = {'anchor_id': atom['id'], 'field': atom['field'], 'units': []}
            groups.append(current)
        current['units'].append(atom)
    return [{**g, 'units': endpoint_candidates(g['units'], role)} for g in groups
            if endpoint_candidates(g['units'], role)]


def located_endpoints(saved, packet):
    missing = missing_endpoints(saved['units'], saved['source']['plan'])
    entries = packet['result']['locations']
    if len(entries) != len(missing) or {e['endpoint_id'] for e in entries} != {m['endpoint_id'] for m in missing}:
        policy.delivery_error('Endpoint location must cover each frozen endpoint once')
    for item in missing:
        groups = endpoint_locations(saved['units'], item['role'])
        entry = next(e for e in entries if e['endpoint_id'] == item['endpoint_id'])
        ids = entry['anchor_ids']
        if not ids or len(set(ids)) != len(ids) or not set(ids) <= {g['anchor_id'] for g in groups}:
            policy.delivery_error('Endpoint location exceeds canonical headings')
        item['units'] = [a for g in groups if g['anchor_id'] in ids for a in g['units']]
    return missing


def clarified_endpoint_roles(source, bindings):
    if not isinstance(source.get('body'), dict):
        policy.delivery_error('Scope clarification lacks a frozen source plan')
    owners = {p: g['role'] for g in source['body']['groups'] for p in g['paths']}
    endpoints = {digest([edge, side]): owners[edge[side]]
                 for edge in source['body']['declared_interfaces']
                 for side in ('consumer', 'provider')}
    result = {}
    for binding in bindings:
        if binding['endpoint_id'] not in endpoints:
            policy.delivery_error('Unknown frozen endpoint')
        for uid in binding['unit_ids']:
            result.setdefault(uid, set()).add(endpoints[binding['endpoint_id']])
    return result


def missing_endpoints(atoms, source):
    owners = {p: g['role'] for g in source['body']['groups'] for p in g['paths']}
    result = []
    for edge in source['body']['declared_interfaces']:
        if owners[edge['consumer']] == owners[edge['provider']]:
            continue
        for side in ('consumer', 'provider'):
            if not any(exact_endpoint(edge[side], edge[side + '_marker'], a['text']) for a in atoms):
                result.append({'endpoint_id': digest([edge, side]), 'endpoint': {**edge, 'side': side},
                    'role': owners[edge[side]], 'units': endpoint_candidates(atoms, owners[edge[side]])})
    return result


def required_roles(atom):
    """A prose label cannot remove or transfer a host-owned obligation."""
    text = atom.get('text', '')
    roles = set(atom.get('module_roles', []))
    fixed = {'data_contract': 'scout', 'metrics': 'scout', 'branches': 'director',
             'parameter_contract': 'engineer', 'dependencies': 'engineer'}.get(atom.get('field'))
    if fixed:
        roles.add(fixed)
    if roles:
        return sorted(roles)
    return sorted(set(re.findall(r'\[review-role:(director|engineer|scout)\]', text)))


def fixed_role(atom):
    roles = required_roles(atom)
    return roles[0] if roles else None


def exact_endpoint(path, marker, text):
    # Match the pair, including both sides of an interface declaration. Separate
    # hits in one paragraph do not authenticate a path:symbol relationship.
    return bool(re.search(r'(?<![\w./-])' + re.escape(path) + ':' + re.escape(marker)
                          + r'(?![\w./:-])', text))


def bind_modules(atoms, source):
    owners = {p: g['role'] for g in source['body']['groups'] for p in g['paths']}
    titles = {}
    # Only exact table cells join a body title to a planned path. No fuzzy title
    # matching, symbol-name guessing, or whole-algorithm fallback.
    for atom in atoms:
        if atom['field'] != 'algorithm':
            continue
        for line in atom['text'].splitlines():
            cells = [c.strip().strip('`') for c in line.strip().strip('|').split('|')]
            if not line.lstrip().startswith('|') or len(cells) < 2:
                continue
            paths = {p for p in owners if any(exact_token(p, c) for c in cells[1:])}
            if paths and cells[0]:
                titles.setdefault(cells[0], set()).update(paths)
    references = {}
    for entry in source['body'].get('effective_implementation_map', []):
        paths = {p for p in owners if exact_token(p, entry['location'])}
        for title in re.findall(r'\[module-title:([^\]]+)\]', entry['behavior']):
            titles.setdefault(title, set()).update(paths)
        references.setdefault(entry['requirement_id'], set()).update(paths)
    inherited, field = [], None
    for atom in atoms:
        if atom['field'] != field:
            inherited, field = [], atom['field']
        direct = {role for path, role in owners.items() if exact_token(path, atom['text'])}
        for reference in re.findall(r'\[requirement:([^\]]+)\]', atom['text']):
            direct.update(owners[p] for p in references.get(reference, []))
        # Only an exact module heading establishes a paragraph boundary. An
        # unbound heading resets it; a reference in ordinary prose does not.
        heading = re.match(r'^\s*#{4,6}\s+模块\s*[：:]\s*([^\n]+)', atom['text'])
        if heading:
            title = heading[1].strip().strip('`')
            direct.update(owners[p] for p in titles.get(title, []))
            inherited = sorted(direct)
        elif re.match(r'^\s*#{1,4}\s', atom['text']):
            inherited = sorted(direct)
        atom['module_roles'] = sorted(direct | set(inherited))
    return atoms


def exact_token(token, text):
    return bool(re.search(r'(?<![\w./-])' + re.escape(token) + r'(?![\w./-])', text))


def apply_history(engine, run_id, state, row, slot, ledger):
    """Reauthenticate original reports before reconstructing their ledger prefix."""
    proposed = []
    for packet in slot['reports']:
        flow.validate_report(engine, run_id, row, packet)
        policy.apply_report(ledger, row, packet['role'], packet['result'], packet['handoff_id'],
                            self_check=packet['role'] == 'engineer')
        proposed.extend(policy.propose(ledger, state['id'], row, packet['role'],
                                       packet['result'], packet['handoff_id']))
    return proposed


def verification_tasks(saved):
    tasks = []
    for issue in saved.get('verification_issues', []):
        # A single issue may concern several assertions. Keep its close_when
        # conjunction together; never close it on the first same-role packet.
        binding = saved.get('verification_scope', {}).get('result', {}).get('bindings')
        if binding is not None:
            entry = next(b for b in binding if b['issue_id'] == issue['issue_id'])
            ids = {uid for c in entry['conditions'] for uid in c['unit_ids']} | set(entry['dependency_unit_ids'])
            selected = [a for a in saved['units'] if a['id'] in ids]
        else:
            # Immutable v1 rounds retain their original enumeration on replay.
            selected = [a for a in saved['units'] if issue['criterion_id'] in a['criteria']]
        if not selected or issue['verifier'] == 'engineer':
            policy.fail('Patched issue lacks independent evidence-bearing units')
        target = ({'criterion_id': issue['criterion_id']} if
                  saved.get('verification_scope', {}).get('context', {}).get(KEY, {}).get('version') == 2 else {})
        tasks.append(policy.sealed({'stage': 'verification', 'role': issue['verifier'], **target,
            'units': selected, 'self_check': False, 'issue_id': issue['issue_id'],
            'close_when': issue['close_when']}))
    return tasks


def verification_candidates(saved, issue, *, dependencies=False):
    from .implementation_verification_contract import candidates
    return candidates(saved, issue, dependencies=dependencies)


def validate_verification_scope(saved, packet):
    from .implementation_verification_contract import validate
    return validate(saved, packet)


def source_spans(context, source, task):
    """Freeze only approved citations intersecting the pre-author role scope.

    Directories remain available for targeted source discovery. They are never
    promoted to mandatory full-document reads by this preload projection.
    """
    architecture = source['architecture_scope']
    roles = {role for atom in task['units'] for role in required_roles(atom)}
    if not roles and task['stage'] != 'global':
        roles = {'engineer' if task['stage'] in {'author_peer', 'endpoint_peer'} else task['role']}
    ids = {s['section_id'] for role in roles
           for s in architecture['responsibilities'][role]['architecture_sections']}
    shared = set(architecture.get('shared_architecture_sections', []))
    refs = context[policy.CONTEXT]['contract'].get('source_refs', [])
    result = []
    seen = set()
    for doc in context['indexed_documents']:
        if doc['kind'] != 'architecture_design' or doc['sha256'] != architecture['architecture_document_sha256']:
            continue
        identity = (doc['document_id'], doc['sha256'])
        if identity in seen:
            continue
        seen.add(identity)
        spans = {}
        for section in doc['sections']:
            if section['id'] not in ids and section['title'] not in shared:
                continue
            for ref in refs:
                if (ref['document_id'], ref['document_sha256'], ref['section_id']) != (
                        doc['document_id'], doc['sha256'], section['id']):
                    continue
                start = section['start'] + ref['offset']
                end = start + ref['limit']
                if not section['start'] <= start < end <= section['end']:
                    policy.fail('Approved source citation exceeds its exact section')
                spans[(section['id'], start, end)] = {**section, 'start': start, 'end': end, 'chars': end-start}
        if spans:
            result.append({'document_id': doc['document_id'], 'sha256': doc['sha256'],
                           'sections': list(spans.values())})
    return result


def task_context(state, row, common, saved, task):
    criteria = task_criteria(task)
    context = flow.report_context(state, row, common, task['role'], criteria)
    scope = context[policy.CONTEXT]
    scope['required_sections'] = task_sections(task)
    scope['self_check'] = task['self_check']
    # Global is a separate five-ID conclusion, not another criterion review.
    scope['issues'] = [i for i in scope['issues'] if i['status'] != 'PATCHED' or
        task.get('issue_id') == i['issue_id']]
    if task['stage'] == 'global':
        scope.update(assigned_criteria=[], required_sections={})
    context[KEY] = {'version': 1, 'stage': task['stage'], 'identity': saved['identity'],
        'assignment': task, 'source_plan': saved['source'], 'legacy_handoffs': saved['legacy_handoffs']}
    if 'endpoint_closure_protocol' in saved:
        context[KEY]['endpoint_closure_protocol'] = saved['endpoint_closure_protocol']
    context[KEY]['source_spans'] = source_spans(context, saved['source'], task)
    if saved.get('unit_protocol') == 2:
        context[KEY]['unit_protocol'] = 2
        context[KEY]['unit_catalog'] = saved['units']
        context[KEY]['proof_field_contract'] = {
            'version': 1, 'fields': sorted(row['spec']), 'spec_hash': row['spec_hash']}
    if task.get('endpoint') and saved.get('endpoint_clarification'):
        context[KEY]['endpoint_clarification'] = saved['endpoint_clarification']['result']
        if saved.get('endpoint_locations'):
            context[KEY]['endpoint_locations'] = saved['endpoint_locations']['result']
    if task['stage'] == 'verification' and saved.get('verification_scope'):
        context[KEY]['verification_scope'] = saved['verification_scope']['result']
    if task['stage'] == 'global':
        context[KEY]['authenticated_reports'] = output_refs(saved['packets'])
    from .implementation_endpoint_gap import WORK
    work = next((s.get(SLOT, {}).get(WORK) for s in state[policy.FIELD]['rounds']
                 if s['revision'] == row['index'] - 1), None)
    if (work and task.get('endpoint') == work['assignment']['endpoint']
            and task['role'] == work['verifier'] and not task['self_check']):
        context[KEY][WORK] = copy.deepcopy(work)
        from .implementation_endpoint_gap import author_ranges
        context[KEY]['endpoint_author_ranges'] = author_ranges(row, work)
    if task['stage'] != 'global':
        from .supplemental_reads import PROTOCOL, VERSION
        context[PROTOCOL] = VERSION
    return context


def task_criteria(task):
    return ([task['criterion_id']] if task['stage'] == 'verification' and 'criterion_id' in task
            else sorted({cid for a in task['units'] for cid in a['criteria']}))


def task_sections(task):
    return {cid: [a['range'] for a in task['units']
                  if task.get('criterion_id') == cid or cid in a['criteria']]
            for cid in task_criteria(task)}


def obligation_key(task):
    # Absolute positions/document SHA change on unrelated edits. Exact assertion
    # bytes, criterion bindings and endpoint contracts are the dependency key.
    return digest([task['stage'], task['role'], task['self_check'], task.get('endpoint'),
                   [(a['field'], a['text'], a['criteria'], a.get('module_roles', [])) for a in task['units']]])


def proof_sources(source):
    # The effective map is nested in the production source plan as well as in
    # the envelope. Bind both; never compare absent top-level values as proof.
    return [source.get('architecture_scope'), source.get('effective_implementation_map'),
            source.get('plan', {}).get('body', {}).get('effective_implementation_map')]


def proof_spans_match(original_context, context, task):
    # Equal role plans alone do not bind citations. Require explicit frozen
    # spans on both proofs and rederive the current exact source/version ranges.
    expected = source_spans(context, context[KEY]['source_plan'], task)
    return (original_context[KEY].get('source_spans') == expected
            and context[KEY].get('source_spans') == expected)


@flow.snapshot_check
def validate_task_packet(engine, run_id, state, row, packet, task):
    if 'endpoint_author_ranges' in packet['context'][KEY]:
        from .implementation_endpoint_gap import WORK, author_ranges
        from .implementation_authoring import validate_submission
        validate_submission(engine, run_id, state, row)
        work = next((s.get(SLOT, {}).get(WORK) for s in state[policy.FIELD]['rounds']
                     if s['revision'] == row['index'] - 1), None)
        if (not work or task.get('endpoint') != work['assignment']['endpoint']
                or task['role'] != work['verifier'] or task['self_check']
                or packet['context'][KEY].get(WORK) != work
                or packet['context'][KEY]['endpoint_author_ranges'] != author_ranges(row, work)):
            policy.fail('Endpoint author evidence changed its submitted version or verifier')
    if packet['context'][KEY].get('version') != 1:
        policy.fail('Unsupported scoped proof version')
    if 'unit_catalog' in packet['context'][KEY]:
        authenticate_catalog(engine, run_id, state, row, packet['context'])
    if packet.get('legacy_proof') and packet.get('carried_proof'):
        policy.fail('Scoped proof must have exactly one derivation type')
    if packet.get('legacy_proof'):
        original = packet['legacy_proof']
        if not legacy_covers(engine, run_id, state, row, original, task):
            policy.fail('Legacy independent proof no longer covers this exact assignment')
        expected = {**copy.deepcopy(original['result']), 'consistency': []}
        if (packet['result'] != expected or packet['handoff_id'] != original['handoff_id']
                or packet['role'] != original['role']):
            policy.fail('Legacy derived report changed')
        return
    carried = packet.get('carried_proof')
    if carried is None:
        flow.validate_report(engine, run_id, row, packet)
        return
    if carried.get('unit_protocol') == 2:
        validate_unit_carry(engine, run_id, state, row, packet, task)
        return
    original = carried['packet']
    old_rows = [r for r in state['specifications'] if r['index'] == carried.get('revision')]
    origins = [s for s in state[policy.FIELD]['rounds']
               if s['revision'] == carried.get('revision') and s.get('complete')]
    if (len(old_rows) != 1 or len(origins) != 1
            or original not in origins[0].get(SLOT, {}).get('packets', [])):
        policy.fail('Carried scoped proof lacks its completed historical lineage')
    old_row = old_rows[0]
    old_task = original['context'][KEY]['assignment']
    if (old_row['index'] >= row['index'] or task['self_check'] or task['stage'] == 'global'
            or original.get('carried_proof') or original.get('legacy_proof')
            or original['context'][KEY].get('version') != 1 or obligation_key(old_task) != obligation_key(task)
            or original['role'] != task['role'] or original['role'] == 'engineer'
            or original['result'] != packet['result'] or original['handoff_id'] != packet['handoff_id']
            or original['result']['issues'] or original['result']['verifications']
            or any(c['status'] != 'PASS' for c in original['result']['checks'])
            or original['context'][policy.CONTEXT]['contract'] != state[policy.FIELD]['contract']):
        policy.fail('Carried scoped proof changed or lacks independent approval')
    old_source = original['context'][KEY]['source_plan']
    new_source = packet['context'][KEY]['source_plan']
    if proof_sources(old_source) != proof_sources(new_source):
        policy.fail('Carried proof source provenance changed')
    if not proof_spans_match(original['context'], packet['context'], task):
        policy.fail('Carried proof approved source spans changed')
    # Conservative compatibility for shared definitions and undeclared needs:
    # until unit-level dependency proof is available, require every criterion
    # dependency to retain its actual fingerprint.
    for cid in {cid for atom in task['units'] for cid in atom['criteria']}:
        if policy.fingerprint(state[policy.FIELD]['contract'], old_row['spec'], cid) != policy.fingerprint(
                state[policy.FIELD]['contract'], row['spec'], cid):
            policy.fail('Carried proof criterion dependencies changed')
    flow.validate_report(engine, run_id, old_row, original)


def legacy_covers(engine, run_id, state, row, original, task):
    if task['stage'] != 'role' or task['self_check'] or original['role'] != task['role']:
        return False
    scope = original['context'][policy.CONTEXT]
    result = original['result']
    ids = {cid for a in task['units'] for cid in a['criteria']}
    if (scope['contract'] != state[policy.FIELD]['contract'] or scope['document'] != row['document']
            or result['issues'] or result['verifications'] or set(scope['assigned_criteria']) != ids
            or any(c['status'] != 'PASS' for c in result['checks'])):
        return False
    approved_sha = state['implementation_architecture_scope']['architecture_document_sha256']
    approved = {d['document_id'] for d in original['context']['indexed_documents']
                if d['kind'] == 'architecture_design' and d['sha256'] == approved_sha}
    if any(not any(r['document_id'] in approved and r['document_sha256'] == approved_sha
                   for r in c['evidence']) for c in result['checks']):
        return False
    # A criterion-wide PASS with a token citation cannot silently approve every
    # newly assigned assertion. Only full explicit assertion evidence qualifies.
    sections = {s['id']: s for s in row['document']['sections']}
    from .document_read_window import _uncovered
    for atom in task['units']:
        for cid in atom['criteria']:
            check = next((c for c in result['checks'] if c['criterion_id'] == cid), None)
            if check is None:
                return False
            ranges = [(sections[r['section_id']]['start'] + r['offset'],
                       sections[r['section_id']]['start'] + r['offset'] + r['limit'], '')
                      for r in check['evidence'] if r['document_id'] == row['document']['document_id']
                      and r['document_sha256'] == row['document']['sha256']
                      and r['section_id'] == atom['range']['id']]
            if _uncovered(atom['range']['start'], atom['range']['end'], sorted(ranges)):
                return False
    flow.validate_report(engine, run_id, row, original)
    return True


@flow.snapshot_check
def reusable_packet(engine, run_id, state, row, task, context):
    if task['self_check'] or task['stage'] == 'global' or context[policy.CONTEXT]['issues']:
        return None
    if len(task['units']) == 1 and 'unit_catalog' in context[KEY]:
        carried = reusable_unit(engine, run_id, state, row, task, context)
        if carried:
            return carried
    for slot in reversed(state[policy.FIELD]['rounds']):
        if slot['revision'] >= row['index'] or not slot.get('complete'):
            continue
        for old in slot.get(SLOT, {}).get('packets', []):
            # v1 only carries direct scoped reports. Derived legacy and carried
            # reports retain their original proof; unsupported chains schedule
            # this task normally instead of creating a contradictory envelope.
            if (old.get('carried_proof') or old.get('legacy_proof')
                    or old['context'][KEY].get('version') != 1
                    or obligation_key(old['context'][KEY]['assignment']) != obligation_key(task)):
                continue
            if old['result']['issues'] or old['result']['verifications'] or any(c['status'] != 'PASS' for c in old['result']['checks']):
                continue
            if proof_sources(old['context'][KEY]['source_plan']) != proof_sources(context[KEY]['source_plan']):
                continue
            if not proof_spans_match(old['context'], context, task):
                continue
            previous = next(r for r in state['specifications'] if r['index'] == slot['revision'])
            if any(policy.fingerprint(state[policy.FIELD]['contract'], previous['spec'], cid) !=
                   policy.fingerprint(state[policy.FIELD]['contract'], row['spec'], cid)
                   for cid in {cid for atom in task['units'] for cid in atom['criteria']}):
                continue
            if any(p['dependencies_complete'] and not unit_proof_ranges_complete(old['context'], p)
                   for p in old['result'].get('unit_proofs', [])):
                continue
            candidate = {**copy.deepcopy(old), 'context': context,
                         'carried_proof': {'revision': slot['revision'], 'packet': copy.deepcopy(old)}}
            validate_task_packet(engine, run_id, state, row, candidate, task)
            return candidate
    return None


def canonical_unit(atom):
    return {k: atom.get(k) for k in ('field', 'text', 'criteria', 'module_roles')}


def authenticate_catalog(engine, run_id, state, row, context):
    """Bind derived contexts to current bytes, once per immutable snapshot.

    Cache only the digest: callers cannot mutate a borrowed catalog and the
    key includes all compiler operands, never a model-supplied identity alone.
    """
    from .implementation_scope_review import contract
    expected_source = contract(state, row, ROLES)
    scope = context[KEY]
    acceptance = context[policy.CONTEXT]
    approved = state[policy.FIELD]['contract']
    if (scope['source_plan'] != expected_source or acceptance['contract'] != approved
            or acceptance['document'] != row['document']):
        policy.fail('Scoped current catalog document/contract/source binding changed')
    expected = catalog_digest(engine, run_id, row, approved, expected_source)
    if digest(scope.get('unit_catalog')) != expected:
        policy.fail('Scoped current unit catalog changed')
    proof_fields(context, row=row)


def catalog_digest(engine, run_id, row, approved, source):
    identity = ('scoped_current_catalog_v1', run_id,
                digest([row['document'], row['spec'], approved, source]))
    return engine.store.memoized_read(identity, lambda: digest(bind_modules(
        units(engine, run_id, row, approved), source['plan'])))


def proof_fields(context, *, row=None):
    """One field vocabulary for new schemas and validators; old inputs stay frozen."""
    scope = context[KEY]
    contract = scope.get('proof_field_contract')
    if contract is not None:
        if (not isinstance(contract, dict) or set(contract) != {'version', 'fields', 'spec_hash'}
                or type(contract['version']) is not int or contract['version'] != 1
                or not isinstance(contract['fields'], list)
                or any(not isinstance(f, str) or not f for f in contract['fields'])
                or contract['fields'] != sorted(set(contract['fields']))
                or contract['spec_hash'] != context[policy.CONTEXT]['spec_hash']
                or row is not None and (contract['fields'] != sorted(row['spec'])
                                       or contract['spec_hash'] != row['spec_hash'])):
            policy.fail('Unit proof field contract differs from the exact implementation')
        return contract['fields']
    # Legacy catalogs need not include every field (some old schemas carried
    # only unit IDs). Keep their syntax; runtime checks the actual row keys.
    return sorted(row['spec']) if row is not None else []


def unit_dependency_fields(context, proof):
    catalog = {a['id']: a for a in context[KEY]['unit_catalog']}
    contract = context[policy.CONTEXT]['contract']
    fields = set(proof['dependency_fields'])
    for criterion in contract['criteria']:
        if criterion['id'] in catalog[proof['unit_id']]['criteria']:
            for cid in criterion['depends_on']:
                fields.update(policy.criterion_fields(contract, cid))
    return fields


def unit_binding(context, proof, spec):
    """Explicit reviewer dependency assertion plus mandatory contract dependencies.

    Positions remain in the original packet; this key identifies exact bytes
    across document revisions. Unknown dependency scope is never reusable.
    """
    catalog = {a['id']: a for a in context[KEY]['unit_catalog']}
    atom = catalog[proof['unit_id']]
    contract = context[policy.CONTEXT]['contract']
    fields = unit_dependency_fields(context, proof)
    return {'unit': canonical_unit(atom), 'role': context[KEY]['assignment']['role'],
        'dependencies': sorted((canonical_unit(catalog[uid]) for uid in proof['dependency_unit_ids']), key=digest),
        'fields': {f: spec.get(f) for f in sorted(fields)},
        'contract': contract['sha256'], 'sources': proof_sources(context[KEY]['source_plan']),
        'source_spans': context[KEY]['source_spans']}


def unit_proof_ranges_complete(context, proof, *, supplemental_refs=()):
    """Eligibility only; real delivery authentication remains mandatory."""
    from .document_read_window import _uncovered
    catalog = {a['id']: a for a in context[KEY]['unit_catalog']}
    doc = context[policy.CONTEXT]['document']
    sections = {s['id']: s for s in doc['sections']}
    fields = unit_dependency_fields(context, proof)
    ids = {proof['unit_id'], *proof['dependency_unit_ids']}
    ids.update(a['id'] for a in catalog.values() if a['field'] in fields)
    # Never trim mandatory dependencies to the old grant. A complete-looking
    # citation outside that grant cannot earn carry credit; ordinary scheduling
    # must obtain a fresh exact-range proof instead. Legacy unscoped inputs keep
    # their original delivery authentication path.
    authority = read_ranges(context) if wire_enabled(context) else None
    ranges_by_section = {}
    for ref in proof['evidence']:
        if authority is not None and not _permitted_range(authority, {
                'document_id': ref['document_id'], 'expected_sha256': ref['document_sha256'],
                'section_id': ref['section_id'], 'offset': ref['offset'], 'limit': ref['limit']}) and ref not in supplemental_refs:
            return False
        if (ref['document_id'], ref['document_sha256']) != (doc['document_id'], doc['sha256']):
            continue
        section = sections.get(ref['section_id'])
        if section is None:
            return False
        start = section['start'] + ref['offset']
        ranges_by_section.setdefault(ref['section_id'], []).append((start, start + ref['limit'], ''))
    for ranges in ranges_by_section.values():
        ranges.sort()
    for uid in ids:
        atom = catalog[uid]
        if _uncovered(atom['range']['start'], atom['range']['end'],
                      ranges_by_section.get(atom['range']['id'], [])):
            return False
    return True


def validate_unit_proofs(engine, run_id, row, packet):
    context, result = packet['context'], packet['result']
    proofs = result.get('unit_proofs', [])
    if not proofs:
        return
    scope = context[KEY]
    catalog = {a['id']: a for a in scope.get('unit_catalog', [])}
    assigned = {a['id'] for a in scope['assignment']['units']}
    fields = set(proof_fields(context, row=row))
    if len({p['unit_id'] for p in proofs}) != len(proofs):
        policy.delivery_error('Duplicate unit proof')
    for proof in proofs:
        ids = [proof['unit_id'], *proof['dependency_unit_ids']]
        if (proof['unit_id'] not in assigned or not set(ids) <= catalog.keys()
                or len(set(proof['dependency_unit_ids'])) != len(proof['dependency_unit_ids'])
                or scope.get('proof_field_contract') is not None and
                    len(set(proof['dependency_fields'])) != len(proof['dependency_fields'])
                or not set(proof['dependency_fields']) <= fields):
            policy.delivery_error('Unit proof exceeds frozen catalog', unit_id=proof['unit_id'],
                unassigned_unit=proof['unit_id'] not in assigned,
                unknown_unit_ids=sorted(set(ids) - catalog.keys()),
                duplicate_dependency_ids=len(set(proof['dependency_unit_ids'])) != len(proof['dependency_unit_ids']),
                duplicate_dependency_fields=len(set(proof['dependency_fields'])) != len(proof['dependency_fields']),
                invalid_dependency_fields=sorted(set(proof['dependency_fields']) - fields),
                allowed_dependency_fields=sorted(fields),
                instruction='dependency_fields 只能填写当前实现文档的顶层共享字段名；文件路径或端点标记不属于字段名。'
                    '按冻结 catalog 核对单元与依赖；可省略无法证明完整依赖的可选证明，不得伪称完整。')
        flow.validate_refs(engine, run_id, packet['handoff_id'], context, proof['evidence'],
                           current=row['document'], field='unit_proofs.evidence')
        if not proof['dependencies_complete']:
            continue
        from .supplemental_reads import permits_evidence
        extra = [ref for ref in proof['evidence'] if permits_evidence(
            engine, run_id, packet['handoff_id'], context, ref)]
        complete = (unit_proof_ranges_complete(context, proof, supplemental_refs=extra) if extra
                    else unit_proof_ranges_complete(context, proof))
        if not complete:
            policy.delivery_error('Unit proof lacks complete exact assertion/dependency evidence')


def unit_projection(original, task):
    # Select before copying: each original may contain evidence for many units.
    return copy.deepcopy({**{k: v for k, v in original['result'].items()
                            if k not in ('checks', 'unit_proofs')},
        'checks': [c for c in original['result']['checks'] if c['criterion_id'] in task_criteria(task)]})


def match_unit_proof(original, context, task, old_spec, spec):
    if (original.get('carried_proof') or original.get('legacy_proof') or task['self_check']
            or task['stage'] not in ('role', 'author_peer', 'endpoint', 'endpoint_peer')
            or original['role'] != task['role'] or original['result']['issues']
            or original['result']['verifications'] or len(task['units']) != 1):
        return None
    old_context = original['context']
    if (old_context[KEY].get('version') != 1 or old_context[KEY].get('unit_protocol') != 2
            or old_context[policy.CONTEXT]['contract'] != context[policy.CONTEXT]['contract']):
        return None
    old_task = old_context[KEY]['assignment']
    if any(old_task.get(k) != task.get(k) for k in ('role', 'stage', 'self_check', 'endpoint')):
        return None
    for proof in original['result'].get('unit_proofs', []):
        if not proof['dependencies_complete']:
            continue
        catalog = old_context[KEY]['unit_catalog']
        atom = next(a for a in catalog if a['id'] == proof['unit_id'])
        if canonical_unit(atom) != canonical_unit(task['units'][0]):
            continue
        if not unit_proof_ranges_complete(old_context, proof):
            continue
        if any(sum(canonical_unit(a) == canonical_unit(atom) for a in items) != 1
               for items in (catalog, context[KEY]['unit_catalog'])):
            continue
        checks = [c for c in original['result']['checks'] if c['criterion_id'] in task_criteria(task)]
        if {c['criterion_id'] for c in checks} != set(task_criteria(task)) or any(c['status'] != 'PASS' for c in checks):
            continue
        mapped = []
        for uid in proof['dependency_unit_ids']:
            dependency = next(a for a in catalog if a['id'] == uid)
            matches = [a for a in context[KEY]['unit_catalog'] if canonical_unit(a) == canonical_unit(dependency)]
            if len(matches) != 1:
                break
            mapped.append(matches[0]['id'])
        else:
            current = {**proof, 'unit_id': task['units'][0]['id'], 'dependency_unit_ids': mapped}
            if (proof_spans_match(old_context, context, task) and
                    unit_binding(old_context, proof, old_spec) == unit_binding(context, current, spec)):
                return proof
    return None


def reusable_unit(engine, run_id, state, row, task, context, *, detached=True):
    for slot in reversed(state[policy.FIELD]['rounds']):
        if not slot.get('complete') or slot['revision'] >= row['index']:
            continue
        previous = next(r for r in state['specifications'] if r['index'] == slot['revision'])
        for original in slot.get(SLOT, {}).get('packets', []):
            proof = match_unit_proof(original, context, task, previous['spec'], row['spec'])
            if proof is None:
                continue
            flow.validate_report(engine, run_id, previous, original)
            # Validation compares a borrowed envelope synchronously; persisted
            # candidates still own a detached original, including raw proofs.
            clone = copy.deepcopy if detached else (lambda value: value)
            return {**clone({k: v for k, v in original.items() if k not in ('context', 'result')}),
                'context': context, 'result': unit_projection(original, task),
                'carried_proof': {'unit_protocol': 2, 'revision': slot['revision'],
                    'unit_proof': clone(proof), 'packet': clone(original),
                    'fingerprints': {cid: policy.fingerprint(state[policy.FIELD]['contract'], previous['spec'], cid)
                                     for cid in task_criteria(task)}}}
    return None


def validate_unit_carry(engine, run_id, state, row, packet, task):
    expected = reusable_unit(engine, run_id, state, row, task, packet['context'], detached=False)
    if expected is None or expected != packet:
        policy.fail('Carried unit proof changed or lacks authenticated historical lineage')


@flow.snapshot_check
def delta_tasks(engine, run_id, state, row, common, saved, tasks):
    """Split only proven carry units; keep remaining work batched by role."""
    result = []
    plan = []
    for task in tasks:
        start = len(result)
        proofs = {}
        pending = []
        if task['stage'] == 'verification':
            # close_when is a conjunction owned by one independent verifier.
            # Splitting it would let several partial packets close one issue.
            result.append(task)
            plan.append({'original': task, 'tasks': [task], 'proofs': {}})
            continue
        for atom in task['units']:
            single = policy.sealed({**{k: v for k, v in task.items() if k != 'sha256'}, 'units': [atom]})
            context = task_context(state, row, common, saved, single)
            carried = reusable_packet(engine, run_id, state, row, single, context)
            if carried:
                result.append(single)
                proofs[single['sha256']] = copy.deepcopy(carried['carried_proof'])
            else:
                pending.append(atom)
        if pending:
            result.append(policy.sealed({**{k: v for k, v in task.items() if k != 'sha256'}, 'units': pending}))
        plan.append({'original': task, 'tasks': result[start:], 'proofs': proofs})
    saved['delta_plan'] = plan
    return result


def apply_packets(ledger, state, row, saved):
    """Typed adapter: retain partial proofs; only their conjunction covers Cxx."""
    partials = {}
    proposed = []
    for packet in effective_packets(saved):
        scope = packet['context'][KEY]
        task = scope['assignment']
        result = packet['result']
        # Keep original contract immutable. The adapter changes only the local
        # responsibility check; requirement/needs/close_when logic is shared.
        # apply_report mutates only these five operands. Copying rounds and
        # contracts here recopied every frozen packet (including carried proof
        # ancestry) once per unit. Those histories are not inputs to this adapter.
        local = copy.deepcopy({key: ledger[key] for key in
                               ('contract', 'coverage', 'issues', 'events', 'consistency')})
        for criterion in local['contract']['criteria']:
            criterion['reviewer'] = packet['role']
        flow_policy_result = dict(result)
        if packet.get('carried_proof'):
            # Derived ledger view only; original response stays inside carried_proof.
            flow_policy_result['document_sha256'] = row['document']['sha256']
        policy.apply_report(local, row, packet['role'], flow_policy_result, packet['handoff_id'],
                            self_check=task['self_check'])
        for key in ('issues', 'events'):
            ledger[key] = local[key]
        if task['stage'] == 'global':
            ledger['consistency'] = local['consistency']
        elif not task['self_check']:
            for check in result['checks']:
                partials.setdefault(check['criterion_id'], []).append({**check, 'handoff_id': packet['handoff_id']})
        proposed.extend(policy.propose(ledger, state['id'] + ':' + task['sha256'], row,
                                       packet['role'], result, packet['handoff_id']))
    for cid, proofs in partials.items():
        status = 'FAIL' if any(p['status'] == 'FAIL' for p in proofs) else (
            'PASS' if all(p['status'] == 'PASS' for p in proofs) else 'UNKNOWN')
        ledger['coverage'][cid] = {'criterion_id': cid, 'status': status, 'partial_proofs': proofs,
            'fingerprint': policy.fingerprint(ledger['contract'], row['spec'], cid), 'spec_hash': row['spec_hash']}
    return proposed


def authenticate_verification_issues(saved, ledger):
    for issue in saved.get('verification_issues', []):
        current = ledger['issues'].get(issue['issue_id'], {})
        if any(issue.get(k) != current.get(k) for k in ('criterion_id', 'verifier', 'close_when', 'patch',
                                                      'claim', 'evidence', 'document_version')):
            policy.fail('Scoped verification changed its original issue or patch')


@flow.snapshot_check
def authenticate_obligations(engine, run_id, state, row, saved, *, ledger=None):
    """Rebuild frozen obligations independently of the saved task/packet count.

    Historical unsplit assignments retain their identity; an existing delta
    plan must preserve every original unit and independent responsibility.
    This read-only prefix is shared by dispatch and release/replay.
    """
    from .implementation_scope_review import contract
    revision = saved['source'].get('mapping_revision')
    if revision and revision.get('scoped_clarification_packet'):
        flow.check_packet(engine, run_id, revision['scoped_clarification_packet'])
        if revision['implementation_map'] != revision['scoped_clarification_packet']['result']['implementation_map']:
            policy.fail('Source clarification changed')
    if saved['source'] != contract(state, row, ROLES):
        policy.fail('Scoped source plan drift')
    atoms = saved['units']
    # The inventory and a derived packet may share these objects in memory.
    # Report the same catalog violation before either path checks identity;
    # JSON persistence must not change the meaning of this diagnostic.
    if digest(atoms) != catalog_digest(engine, run_id, row, state[policy.FIELD]['contract'], saved['source']):
        policy.fail('Scoped current unit catalog changed')
    if saved['identity'] != digest([saved['source'], atoms, state[policy.FIELD]['contract']['sha256']]):
        policy.fail('Scoped render binding changed')
    authenticate_verification_issues(saved, ledger if ledger is not None else state[policy.FIELD])
    if saved.get('clarification'):
        flow.check_packet(engine, run_id, saved['clarification'])
    mapping = [*saved.get('fixed_assignments', []), *saved.get('clarification', {}).get('result', {}).get('assignments', [])]
    if saved.get('endpoint_clarification'):
        flow.check_packet(engine, run_id, saved['endpoint_clarification'])
    bindings = saved.get('endpoint_clarification', {}).get('result', {}).get('bindings', [])
    if saved.get('endpoint_location_protocol') == 1 and missing_endpoints(saved['units'], saved['source']['plan']):
        location = saved['endpoint_locations']
        flow.check_packet(engine, run_id, location)
        if (location['context'][KEY]['identity'] != saved['identity'] or location['role'] != 'engineer'
                or location['context'][KEY]['source_plan'] != saved['source']):
            policy.fail('Endpoint location source changed')
        for item in located_endpoints(saved, location):
            binding = next(b for b in bindings if b['endpoint_id'] == item['endpoint_id'])
            if not set(binding['unit_ids']) <= {a['id'] for a in item['units']}:
                policy.fail('Endpoint binding exceeds located canonical units')
    if saved.get('verification_scope'):
        packet = saved['verification_scope']
        flow.check_packet(engine, run_id, packet)
        if packet['context'][KEY].get('prior_scope_assignments'):
            from .implementation_verification_redispatch import authenticate
            authenticate(engine, run_id, packet['context'][KEY]['prior_scope_assignments'])
        if (saved.get('verification_document') != row['document']
                or packet['role'] != 'engineer' or packet['context'][KEY]['issues'] != saved['verification_issues']
                or packet['context'][KEY]['units'] != saved['units']):
            policy.fail('Verification clarification source changed')
        validate_verification_scope(saved, packet)
    expected = assignments(saved['units'], mapping, saved['source']['plan'], bindings) + verification_tasks(saved)
    if 'delta_plan' in saved:
        if [p['original'] for p in saved['delta_plan']] != expected:
            policy.fail('Delta plan changed original obligations')
        for entry in saved['delta_plan']:
            original = entry['original']
            atoms = [a for t in entry['tasks'] for a in t['units']]
            if sorted(atoms, key=digest) != sorted(original['units'], key=digest):
                policy.fail('Delta plan omitted or duplicated units')
            for task in entry['tasks']:
                if task != policy.sealed({**{k: v for k, v in original.items() if k != 'sha256'}, 'units': task['units']}):
                    policy.fail('Delta plan changed independent responsibilities')
        expected = [t for p in saved['delta_plan'] for t in p['tasks']]
    if expected != saved['tasks']:
        policy.fail('Scoped task enumeration changed')
    if saved.get('endpoint_closure_protocol') not in (None, 1):
        policy.fail('Unsupported endpoint closure protocol')
    return expected


def authenticate_task_packets(engine, run_id, state, row, saved, tasks):
    """Authenticate receipts against an already reconstructed task inventory."""
    from .implementation_endpoint_gap import WORK
    work = next((s.get(SLOT, {}).get(WORK) for s in state[policy.FIELD]['rounds']
                 if s['revision'] == row['index'] - 1), None)
    if work:
        responsible = [t for t in tasks if t.get('endpoint') == work['assignment']['endpoint']
                       and t['role'] == work['verifier'] and not t['self_check']]
        if not {c['criterion_id'] for c in work['checks']} <= {
                cid for t in responsible for cid in task_criteria(t)}:
            policy.fail('Endpoint evidence revision lost its original independent responsibility')
    for packet, task in zip(saved['packets'], tasks):
        if work and task in responsible and packet['context'][KEY].get(WORK) != work:
            policy.fail('Endpoint evidence recheck lost its original work binding')
        assigned = task_criteria(task)
        required = task_sections(task)
        scope = packet['context'][policy.CONTEXT]
        if packet['context'][KEY].get('endpoint_closure_protocol') != saved.get('endpoint_closure_protocol'):
            policy.fail('Endpoint closure protocol changed')
        if scope['assigned_criteria'] != assigned or scope['required_sections'] != required:
            policy.fail('Scoped report changed its frozen reading obligations')
        if (packet['context'][KEY]['assignment'] != task or packet['role'] != task['role']
                or packet['context'][KEY]['source_plan'] != saved['source']):
            policy.fail('Scoped report belongs to another assignment')
        if ('source_spans' in packet['context'][KEY] and packet['context'][KEY]['source_spans'] !=
                source_spans(packet['context'], saved['source'], task)):
            policy.fail('Scoped approved source spans changed')
        validate_task_packet(engine, run_id, state, row, packet, task)
        planned = next((entry['proofs'][task['sha256']] for entry in saved.get('delta_plan', [])
                        if task['sha256'] in entry['proofs']), None)
        if planned is not None and packet.get('carried_proof') != planned:
            policy.fail('Frozen delta carry proof changed on replay')
        if packet.get('legacy_proof') and packet['legacy_proof'] not in saved['historical_reports']:
            policy.fail('Legacy proof is outside the authenticated historical slot')


@flow.snapshot_check
def authenticate_packets(engine, run_id, state, row, saved, *, ledger=None):
    expected = authenticate_obligations(engine, run_id, state, row, saved, ledger=ledger)
    deferred = saved.get('global_deferred', False)
    if len(saved['packets']) != len(expected) + (0 if deferred else 1):
        policy.fail('Incomplete scoped role/endpoint/global receipts')
    authenticate_task_packets(engine, run_id, state, row, saved, [*expected, global_task(saved)])
    predecessors = {**saved, 'packets': saved['packets'][:len(expected)]}
    closed = validate_endpoint_followups(engine, run_id, state, row, predecessors)
    if deferred:
        from .implementation_endpoint_gap import WORK, repeated_work
        work = saved.get(WORK)
        if work is not None and work != repeated_work(predecessors, engine, run_id):
            policy.fail('Endpoint evidence work changed its authenticated terminal chain')
        if closed or not (endpoint_blockers(predecessors) or work):
            policy.fail('Deferred global review lacks authenticated endpoint repair findings')
        return
    # Historical completed packets retain their original ordering protocol.
    # New dispatches always enforce closure in validate_predecessors; new
    # frozen packets additionally bind that requirement for release replay.
    if not closed and saved.get('endpoint_closure_protocol') == 1:
        policy.fail('Global review preceded independent endpoint closure')
    if saved['packets'][-1]['context'][KEY]['authenticated_reports'] != output_refs(effective_packets(predecessors)):
        policy.fail('Global review did not see the frozen independent outputs')


def output_refs(packets):
    return [{'handoff_id': p['handoff_id'], 'role': p['role'], 'result': copy.deepcopy(p['result']),
             'assignment': p['context'][KEY]['assignment']} for p in packets]


def global_task(saved):
    return policy.sealed({'stage': 'global', 'role': 'director', 'units': [], 'self_check': False})


def effective_packets(saved):
    from .implementation_source_read_recovery import effective
    return effective(saved)


def endpoint_blockers(saved):
    return [i for p in effective_packets(saved)
            if p['context'][KEY]['stage'] in ('endpoint', 'endpoint_peer')
            for i in p['result']['issues'] if i['severity'] in ('P0', 'P1')]


def endpoint_closed(packet):
    result = packet['result']
    return (all(c['status'] == 'PASS' for c in result['checks'])
            and not any(i['severity'] in ('P0', 'P1') for i in result['issues']))


def report_schema(context):
    """Specialize the existing report protocol, including final-only recovery.

    Legacy packets retain their schema. New catalogs permit explicit optional
    unit dependency proofs; absence or unknown scope grants no selective carry.
    """
    schema = copy.deepcopy(policy.REPORT_SCHEMA)
    scope = context[policy.CONTEXT]
    props = schema['properties']
    catalog = context[KEY].get('unit_catalog')
    if catalog:
        proof_evidence = copy.deepcopy(props['checks']['items']['properties']['evidence'])
        proof_evidence['minItems'] = 1
        fields = proof_fields(context)
        assigned = [a['id'] for a in context[KEY].get('assignment', {}).get('units', catalog)]
        props['unit_proofs'] = policy.array(policy.obj({
            'unit_id': {'enum': assigned or [a['id'] for a in catalog]},
            'dependencies_complete': {'type': 'boolean'},
            'dependency_unit_ids': {**policy.array({'enum': [a['id'] for a in catalog]}), 'uniqueItems': True},
            'dependency_fields': (
                {**policy.array({'enum': fields}), 'uniqueItems': True} if fields and context[KEY].get('proof_field_contract')
                else {'type': 'array', 'items': False, 'maxItems': 0} if context[KEY].get('proof_field_contract')
                else policy.array({'type': 'string'})),
            'evidence': proof_evidence}))
    props['contract_sha256'] = {'const': scope['contract']['sha256']}
    props['document_sha256'] = {'const': scope['document']['sha256']}
    ids = scope['assigned_criteria']
    props['checks']['minItems'] = props['checks']['maxItems'] = len(ids)
    if ids:
        props['checks']['items']['properties']['criterion_id'] = {'enum': ids}
    global_stage = context[KEY]['stage'] == 'global'
    props['consistency']['minItems'] = props['consistency']['maxItems'] = len(policy.CONSISTENCY) if global_stage else 0
    return schema


@flow.snapshot_check
def validate_predecessors(engine, run_id, state, row, saved):
    """One immutable view for the entire global-dispatch prerequisite batch."""
    tasks = authenticate_obligations(engine, run_id, state, row, saved)
    if len(saved['packets']) != len(tasks):
        policy.fail('Global review requires all role and endpoint reports')
    authenticate_task_packets(engine, run_id, state, row, saved, tasks)
    return validate_endpoint_followups(engine, run_id, state, row, saved)


def validate_endpoint_followups(engine, run_id, state, row, saved):
    """Called only after authenticating the complete original task inventory."""
    from .implementation_source_read_recovery import effective as source_effective, validate_followups
    validate_followups(engine, run_id, state, row, saved)
    effective = source_effective(saved)
    parents = {p['handoff_id']: p for p in [*saved['packets'], *saved.get('source_followups', []),
                                            *saved.get('endpoint_followups', [])]}
    for packet in saved.get('endpoint_followups', []):
        task = packet['context'][KEY]['assignment']
        if task not in saved['tasks'] or task['stage'] not in ('endpoint', 'endpoint_peer'):
            policy.fail('Endpoint followup changed its responsibility')
        original_gap = packet['context'][KEY].get('endpoint_gap', [])
        prior = parents.get(original_gap[0]['handoff_id']) if len(original_gap) == 1 else None
        if prior is None:
            policy.fail('Endpoint followup lost its authenticated predecessor')
        if (packet['role'] != task['role']
                or any(packet['context'][KEY].get(k) != prior['context'][KEY].get(k)
                       for k in ('version', 'stage', 'source_plan', 'source_spans', 'endpoint_closure_protocol'))
                or any(packet['context'].get(policy.CONTEXT, {}).get(k) !=
                       prior['context'].get(policy.CONTEXT, {}).get(k)
                       for k in ('contract', 'document', 'assigned_criteria', 'required_sections', 'self_check'))):
            policy.fail('Endpoint followup changed frozen scope or independent role')
        if (endpoint_closed(prior) or any(i['severity'] in ('P0', 'P1') for i in prior['result']['issues'])
                or any(i not in packet['result']['issues'] for i in prior['result']['issues'])
                or packet['context'][KEY].get('endpoint_gap') != output_refs([prior])):
            policy.fail('Endpoint followup changed its original gap')
        validate_task_packet(engine, run_id, state, row, packet, task)
        from .implementation_endpoint_gap import validate_followup
        validate_followup(engine, run_id, packet)
    return all(endpoint_closed(p) for p in effective
               if p['context'][KEY]['stage'] in ('endpoint', 'endpoint_peer'))


def replay_round(engine, run_id, state, row, slot, ledger):
    saved = slot[SLOT]
    history = saved['historical_slot']
    if history['reports'] != saved['historical_reports'] or history['reports'] != slot['reports']:
        policy.fail('Historical report prefix changed')
    if apply_history(engine, run_id, state, row, history, ledger) != history['proposed_ids']:
        policy.fail('Historical issue provenance changed')
    from .implementation_adjudication import replay
    if history['proposed_ids']:
        replay(engine, run_id, state, row, history, ledger)
    for packet in history.get('rechecks', []):
        flow.validate_report(engine, run_id, row, packet)
        policy.apply_report(ledger, row, packet['role'], packet['result'], packet['handoff_id'])
        policy.propose(ledger, state['id'] + ':recheck', row, packet['role'], packet['result'], packet['handoff_id'])
    # Verify against this authenticated historical prefix, after its original
    # reports/adjudication, before applying the supplement's verifications.
    authenticate_packets(engine, run_id, state, row, saved, ledger=ledger)
    increment = saved['adjudication_slot']
    if apply_packets(ledger, state, row, saved) != increment['proposed_ids']:
        policy.fail('Scoped issue provenance changed')
    if increment['proposed_ids']:
        replay(engine, run_id, state, row, increment, ledger)


def authenticate_release(engine, run_id, state, row):
    flow.validate_release(engine, run_id, state, row)
    saved = next(s[SLOT] for s in state[policy.FIELD]['rounds'] if s['revision'] == row['index'])
    return saved['source']['plan']


WIRE_VERSION = 3


def wire_enabled(context):
    version = context.get(KEY, {}).get('wire_version')
    if version is not None and (type(version) is not int or version not in (1, 2, 3)):
        policy.fail('Unsupported scoped outbound version; preserve original request')
    return version in (2, 3)


def _derive_read_ranges(context):
    """Host-frozen authority; catalog membership alone never authorizes a read."""
    scope = context[policy.CONTEXT]
    current = scope['document']
    result = []
    for doc in context['indexed_documents']:
        if (doc['document_id'], doc['sha256']) == (current['document_id'], current['sha256']):
            spans = [s for group in scope['required_sections'].values() for s in group]
            spans += reading_dependencies(context)
        elif context[KEY].get('wire_version') == 3:
            spans = [s for source in context[KEY].get('source_spans', [])
                     if (source['document_id'], source['sha256']) == (doc['document_id'], doc['sha256'])
                     for s in source['sections']]
        elif doc['kind'] == 'architecture_design':
            # Approved architecture remains precisely navigable, never inlined.
            approved = context[KEY]['source_plan']['architecture_scope']['architecture_document_sha256']
            spans = doc['sections'] if doc['sha256'] == approved else []
        else:
            spans = []
        sections = {s['id']: s for s in doc['sections']}
        for span in spans:
            result.append((doc['document_id'], doc['sha256'], span['id'],
                           span['start'] - sections[span['id']]['start'],
                           span['end'] - sections[span['id']]['start']))
    return sorted(set(result))


AUTHORITY = 'read_authority'


def reading_dependencies(context):
    """Legacy grants are replay-only; v3 never preloads field dependencies."""
    scope = context.get(KEY, {})
    from .implementation_endpoint_gap import ranges
    return (ranges(context) + scope.get('endpoint_author_ranges', []) +
            ([] if scope.get('wire_version') == 3 else scope.get('dependency_sections_v1', [])))


def dependency_sections(context):
    """Minimal canonical ranges implied by the approved criterion dependencies.

    A field dependency means every canonical unit in that field, not arbitrary
    neighboring section bytes. Explicit extra dependencies still require a
    fresh host assignment; a reviewer cannot enlarge this grant.
    """
    scope = context[KEY]
    # Conservative field fingerprints are not model reading permissions.
    # Additional reads require a separately frozen exact assignment.
    if scope.get('wire_version') == 3:
        return []
    criteria = {cid for atom in scope.get('assignment', {}).get('units', [])
                for cid in atom['criteria']}
    contract = context[policy.CONTEXT]['contract']
    dependencies = {cid for criterion in contract['criteria'] if criterion['id'] in criteria
                    for cid in criterion['depends_on']}
    fields = {field for cid in dependencies for field in policy.criterion_fields(contract, cid)}
    spans = { (a['range']['id'], a['range']['start'], a['range']['end']): a['range']
             for a in scope.get('unit_catalog', []) if a['field'] in fields }
    return [spans[key] for key in sorted(spans)]


def new_read_contract(context):
    """Explicit adapter for a new handoff only; never mutate its predecessor."""
    context = copy.deepcopy(context)
    if context.get(KEY, {}).get('assignment'):
        scope = context[KEY]
        if scope.get('wire_version') != WIRE_VERSION:
            scope.pop(AUTHORITY, None)
            scope.pop('dependency_sections_v1', None)
        scope['wire_version'] = WIRE_VERSION
        freeze_read_authority(context)
    return context


def replay_read_contract(expected, actual, engine=None, run_id=None, *, packet=None, purpose=None):
    """Reconstruct the actual historical wire contract without changing it.

    A suffix is authorized only by the same-role terminal predecessor chain,
    frozen input/progress hashes and the delivered successor outcome.
    """
    wire_enabled(actual)
    version = actual.get(KEY, {}).get('wire_version')
    if version == 2 and expected.get(KEY, {}).get('wire_version') == 3:
        policy.fail('Scoped recovery cannot downgrade its frozen parent')
    role = actual.get(KEY, {}).get('assignment', {}).get('role')
    if packet is not None:
        authenticated = flow.authenticate(engine, run_id, packet['role'],
                                         packet['handoff_id'], packet['result'])
        handoff = engine.store.get('handoffs', packet['handoff_id'], {})
        if authenticated != actual or (role and role != packet['role']):
            policy.fail('Scoped recovery role or frozen outcome changed')
        actual_purpose = handoff.get('purpose')
        if actual_purpose != purpose and actual_purpose != purpose + ':wire-v' + str(version):
            policy.fail('Scoped recovery deterministic purpose changed')

    def adapt(value):
        value = copy.deepcopy(value)
        if version in (2, 3) and (version == 2 or value.get(KEY, {}).get('wire_version') != version):
            # v2 had both implicit and explicitly frozen representations.
            # Authenticate any existing grant before reconstructing a copy;
            # a version match does not imply a representation match.
            authenticate_read_authority(value)
            value[KEY].pop(AUTHORITY, None)
            value[KEY].pop('dependency_sections_v1', None)
            value[KEY]['wire_version'] = version
            freeze_read_authority(value)
            if version == 2:
                # Early v2 packets used implicit derived ranges. Later v2
                # froze the same authority explicitly; neither variant may
                # acquire new fields during historical replay.
                if AUTHORITY not in actual[KEY]:
                    value[KEY].pop(AUTHORITY, None)
                if 'dependency_sections_v1' not in actual[KEY]:
                    value[KEY].pop('dependency_sections_v1', None)
        return value

    expected = adapt(expected)
    if KEY not in actual:
        return expected
    origins = actual[KEY].get('legacy_handoffs', [])
    prior = expected[KEY].get('legacy_handoffs', [])
    added = []
    if origins != prior:
        if engine is None or origins[:len(prior)] != prior:
            policy.fail('Scoped recovery predecessor lineage changed')
        from .implementation_review_reads import authenticate_origin
        for origin in origins[len(prior):]:
            authenticate_origin(engine, run_id, role, origin)
            # Delivery tracking can suppress reuse, but cannot suppress identity
            # checks of the retained frozen recovery input.
            frozen = engine.store.get('handoff_inputs', origin['handoff_id'])
            predecessor = engine.store.get('handoffs', origin['handoff_id'], {})
            old_version = frozen['context'].get(KEY, {}).get('wire_version')
            if (predecessor.get('status') not in ('interrupted', 'blocked', 'failed')
                    or engine.store.get('decisions', origin['handoff_id'])
                    or old_version not in (None, 1, 2) or version not in (2, 3)
                    or (old_version or 1) >= version):
                policy.fail('Scoped recovery predecessor is not an unfinished older request')
            if purpose is not None:
                expected_purpose = purpose if not added else purpose + ':wire-v2'
                if predecessor.get('purpose') != expected_purpose or (added and old_version != 2):
                    policy.fail('Scoped recovery predecessor purpose chain changed')
            added.append(origin['handoff_id'])
            if len(set(added)) != len(added):
                policy.fail('Duplicate scoped recovery predecessor')
            candidate = adapt(frozen['context'])
            candidate[KEY]['legacy_handoffs'] = prior
            expected[KEY]['legacy_handoffs'] = prior
            if candidate != expected:
                policy.fail('Scoped recovery successor changed original contract')
        expected[KEY]['legacy_handoffs'] = copy.deepcopy(origins)
    if packet is not None and (actual_purpose != purpose) != bool(added):
        policy.fail('Scoped recovery successor purpose and predecessor must agree')
    return expected


def freeze_read_authority(context):
    """Called only before a new host input is frozen; never upgrade paid inputs."""
    if AUTHORITY in context[KEY]:
        authenticate_read_authority(context)
        return
    if context[KEY].get('unit_catalog'):
        context[KEY]['dependency_sections_v1'] = dependency_sections(context)
    ranges = [list(row) for row in _derive_read_ranges(context)]
    value = {'version': 1, 'ranges': ranges}
    value['sha256'] = digest(value)
    context[KEY][AUTHORITY] = value


def read_ranges(context):
    value = context[KEY].get(AUTHORITY)
    if value is None:  # Explicit compatibility with immutable wire-v2 inputs.
        return _derive_read_ranges(context)
    if (not isinstance(value, dict) or set(value) != {'version', 'ranges', 'sha256'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['sha256'], str)):
        policy.fail('Invalid frozen scoped read authority')
    rows = value['ranges']
    if (not isinstance(rows, list) or any(not isinstance(r, list) or len(r) != 5
            or any(not isinstance(v, str) or not v for v in r[:3])
            or type(r[3]) is not int or type(r[4]) is not int or not 0 <= r[3] < r[4]
            for r in rows)):
        policy.fail('Invalid ordered scoped read ranges')
    frozen = tuple(tuple(r) for r in rows)
    _check_read_authority_digest(frozen, value['sha256'])
    return list(frozen)


@lru_cache(maxsize=128)
def _check_read_authority_digest(rows, sha256):
    # Cache only immutable value snapshots, never context/object identity or
    # an authorization decision. Mutations (including a recomputed digest)
    # get a different key; host authentication still rederives the grant.
    if any(left >= right for left, right in zip(rows, rows[1:])):
        policy.fail('Invalid ordered scoped read ranges')
    if sha256 != digest({'version': 1, 'ranges': [list(r) for r in rows]}):
        policy.fail('Invalid frozen scoped read authority')


def authenticate_read_authority(context):
    # Only host inputs reach this boundary. A digest is integrity, not authority.
    if ('dependency_sections_v1' in context.get(KEY, {}) and
            context[KEY]['dependency_sections_v1'] != dependency_sections(context)):
        policy.fail('Frozen dependency ranges differ from approved contract')
    if AUTHORITY in context.get(KEY, {}) and read_ranges(context) != _derive_read_ranges(context):
        policy.fail('Frozen scoped read authority differs from original assignment')


def permitted_read(context, args):
    return _permitted_range(read_ranges(context), args)


def permitted_report_evidence(context, args):
    """Wire-v3 citations may describe a continuous union of frozen windows.

    This is citation eligibility, never a tool grant or a dependency proof.
    Each character must belong to a window of the same document/SHA/section;
    validate_refs still authenticates source bytes and same-handoff delivery.
    No evidence is synthesized and the outer citation schema stays unchanged.

    For v3 report references only, splitting the submitted interval at every
    frozen boundary would authenticate exactly the same characters. Advancing
    a cursor through sorted grants is that equivalent check without rewriting
    the report: overlaps/nesting add no authority and any gap stops admission.
    Tools and dependency proofs retain their single-window contract, as do
    historical wire versions. This predicate alone never establishes delivery.
    """
    enabled = wire_enabled(context)  # Reject unknown versions, including bools.
    if not enabled or context[KEY]['wire_version'] != 3:
        return permitted_read(context, args)
    authenticate_read_authority(context)
    ranges = read_ranges(context)
    # Retain the exact identity, integer and size constraints of a single read.
    if not isinstance(args, dict):
        return False
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    if type(offset) is not int or type(limit) is not int:
        return False
    if not _permitted_range([(args.get('document_id'), args.get('expected_sha256'),
            args.get('section_id'), 0, offset + limit)], args):
        return False
    cursor = args.get('offset', 0)
    end = cursor + args.get('limit', 4000)
    identity = (args['document_id'], args['expected_sha256'], args['section_id'])
    for did, sha, sid, start, stop in sorted(ranges):
        if (did, sha, sid) != identity or stop <= cursor:
            continue
        if start > cursor:
            return False
        cursor = max(cursor, stop)  # Overlapping/nested grants add no new bytes.
        if cursor >= end:
            return True
    return False


def _permitted_range(ranges, args):
    required = {'document_id', 'expected_sha256', 'section_id'}
    if (not isinstance(args, dict) or not required <= args.keys()
            or not args.keys() <= required | {'offset', 'limit'}):
        return False  # No alternate identity, search or paging aliases.
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    return (type(offset) is int and type(limit) is int and 0 <= offset and 0 < limit <= 6000 and
        any((args.get('document_id'), args.get('expected_sha256'), args.get('section_id')) ==
            (did, sha, sid) and start <= offset < offset + limit <= end
            for did, sha, sid, start, end in ranges))


def read_schema(context):
    # Constant-size syntax only. Host dispatch enforces the same frozen ranges;
    # repeating paragraph enumerations here is neither necessary nor sufficient
    # to validate offset + limit. This also survives repeated wire projection.
    return {'type': 'object', 'additionalProperties': False,
        'required': ['document_id', 'expected_sha256', 'section_id'],
        'properties': {'document_id': {'type': 'string', 'minLength': 1},
            'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
            'section_id': {'type': 'string', 'minLength': 1},
            'offset': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 6000}}}


def wire_context(context):
    """Disposable outbound view. Never persist this over host inputs or receipts.

    Body bytes travel only through authorized read receipts. Canonical units,
    including assigned ones, are metadata here, so preloaded text occurs once.
    """
    if not wire_enabled(context):
        return context
    authority = read_ranges(context)
    text_digests = {}
    def metadata(value):
        if isinstance(value, list):
            return [metadata(v) for v in value]
        if not isinstance(value, dict):
            return value
        if 'id' in value and 'range' in value and 'criteria' in value and 'text' in value:
            body = value['text']
            if body not in text_digests:
                text_digests[body] = digest(body)
            return {**{k: metadata(v) for k, v in value.items() if k != 'text'},
                    'text_sha256': text_digests[body]}
        return {k: metadata(v) for k, v in value.items()}
    # One detached traversal; do not copy and recursively scan the full source
    # plan just to discard it afterwards. The host retains its original bytes.
    source = context[KEY]['source_plan']
    source_view = source if set(source) == {'sha256', 'architecture_scope'} else {
        'sha256': digest(source), 'architecture_scope': {
            'architecture_document_sha256': source['architecture_scope']['architecture_document_sha256']}}
    result = metadata({**context, KEY: {**context[KEY], 'source_plan': source_view}})
    from .implementation_endpoint_gap import CLARIFICATION
    note = result[KEY].get(CLARIFICATION)
    if note:
        # Host authenticates the packet; reviewers receive only its explanation.
        result[KEY][CLARIFICATION] = {k: note[k] for k in ('handoff_id', 'role', 'result')}
    # Failure lineage is audit metadata, never an extra source-text delivery.
    proof = result[KEY].get('endpoint_failed_predecessor')
    if proof:
        result[KEY]['endpoint_failed_predecessor'] = {k: proof[k] for k in
            ('version', 'handoff_id', 'role', 'purpose', 'sha256')}
    if AUTHORITY not in result[KEY]:
        value = {'version': 1, 'ranges': [list(row) for row in authority]}
        result[KEY][AUTHORITY] = {**value, 'sha256': digest(value)}
    # Revision diagnostics otherwise carry whole before/after field bodies,
    # even when this assignment owns only one paragraph in that field.
    for change in result.get(policy.CONTEXT, {}).get('changes', []):
        change.pop('before', None)
        change.pop('after', None)
    # Build navigation once for this projection, not once per stored receipt.
    sections = {(d['document_id'], d['sha256'], s['id']): s
                for d in context['indexed_documents'] for s in d['sections']}
    seen = set()
    from .document_read_window import _reference
    for index, receipt in enumerate(result.get('tool_results', [])):
        call, page = receipt.get('call', {}), receipt.get('result', {})
        # Range-window projection can move text into fragments while removing
        # the parent's text. Those fragments must obey the very same authority;
        # a historical broad page is not permission to expose its subsets.
        fragments = page.get('document_read_fragments', [])
        if fragments:
            section = sections.get((page.get('document_id'), page.get('sha256'), page.get('section_id')))
            kept = []
            for fragment in fragments:
                start, end = fragment.get('char_start'), fragment.get('char_end')
                if (type(start) is not int or type(end) is not int or start >= end
                        or not isinstance(fragment.get('text'), str)
                        or len(fragment['text']) != end - start):
                    continue
                args = {**call.get('arguments', {}),
                        'offset': start - section['start'] if section else -1, 'limit': end-start}
                signature = (page.get('document_id'), page.get('sha256'), start, end)
                if (section and call.get('name') == 'documents.read'
                        and (fragment.get('document_id'), fragment.get('sha256'), fragment.get('section_id')) ==
                            (page.get('document_id'), page.get('sha256'), page.get('section_id'))
                        and (page.get('document_id'), page.get('sha256'), page.get('section_id')) ==
                            (args.get('document_id'), args.get('expected_sha256'), args.get('section_id'))
                        and _permitted_range(authority, args) and signature not in seen):
                    kept.append(fragment)
                    seen.add(signature)
            page['document_read_fragments'] = kept
        if call.get('name') == 'documents.read' and isinstance(page.get('text'), str):
            signature = (page.get('document_id'), page.get('sha256'), page.get('char_start'), page.get('char_end'))
            args = call.get('arguments', {})
            section = sections.get((page.get('document_id'), page.get('sha256'), page.get('section_id')))
            exact = (section is not None and _permitted_range(authority, args) and
                (page.get('document_id'), page.get('sha256'), page.get('section_id')) ==
                (args.get('document_id'), args.get('expected_sha256'), args.get('section_id')) and
                page.get('char_start') == section['start'] + args.get('offset', 0) and
                page.get('char_end') == page.get('char_start') + len(page['text']) and
                0 < len(page['text']) <= args.get('limit', 4000))
            if not exact or signature in seen:
                result['tool_results'][index] = _reference(receipt, index)
            else:
                seen.add(signature)
    return result
