"""Endpoint-only supplemental authority. Original requests are immutable.

This adapter supports canonical path rows, exact symbols, and requirement-ID
acceptance continuations. Ambiguous headings confer no authority.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime

from . import implementation_document_review_scope as scoped
from . import implementation_acceptance as policy
from .implementation_review_routing import digest

KEY = 'endpoint_read_plan'
ERROR = 'Scoped read exceeds frozen assignment authority'
CLARIFICATION = 'endpoint_source_clarification'
WORK = 'endpoint_evidence_work'


def _evidence_identity(refs):
    """Union exact authenticated ranges; citation order/anchors are not progress."""
    spans = {}
    for ref in refs:
        key = (ref['document_id'], ref['document_sha256'], ref['section_id'])
        spans.setdefault(key, []).append((ref['offset'], ref['offset'] + ref['limit']))
    merged = []
    for key, ranges_ in sorted(spans.items()):
        intervals = []
        for start, end in sorted(ranges_):
            if intervals and start <= intervals[-1][1]:
                intervals[-1][1] = max(intervals[-1][1], end)
            else:
                intervals.append([start, end])
        merged.append([key, intervals])
    return merged


def _proof_identity(proofs):
    # Unit IDs belong to the already authenticated same-version catalog.
    # Keep dependency completeness and every new range as progress signals.
    return sorted([{'unit_id': p['unit_id'],
        'dependencies_complete': p['dependencies_complete'],
        'dependency_unit_ids': sorted(set(p['dependency_unit_ids'])),
        'dependency_fields': sorted(set(p['dependency_fields'])),
        'evidence': _evidence_identity(p['evidence'])} for p in proofs], key=digest)


def gap_identity(packet, *, version=3, engine=None, run_id=None):
    """Semantic stall key: prose and receipt pagination are not progress.

    Only used after scoped receipt/read authentication. A new plan, resolved
    criterion or newly delivered authenticated range breaks the chain in v3.
    Frozen v1/v2 work retains its original citation-based algorithm; v4 uses
    v3 delivery identity and requires an actual directed author edit.
    """
    if type(version) is not int or version not in (1, 2, 3, 4):
        policy.fail('Unsupported endpoint gap identity version')
    scope = packet['context'][scoped.KEY]
    plan = scope.get(KEY)
    if not plan or scope['assignment']['self_check']:
        return None
    checks = packet['result']['checks']
    if any(c['status'] == 'FAIL' for c in checks) or any(
            i['severity'] in ('P0', 'P1') for i in packet['result']['issues']):
        return None
    unknown = sorted(c['criterion_id'] for c in checks if c['status'] == 'UNKNOWN')
    if not unknown:
        return None
    if version in (3, 4):
        if engine is None or run_id is None:
            policy.fail('Endpoint delivery identity requires authenticated host reads')
        from .implementation_reading import coverage
        from .implementation_acceptance_reading import required
        refs = []
        for doc in packet['context']['indexed_documents']:
            # coverage authenticates original bytes and acknowledged delivery;
            # neither report citations nor a planned grant establish progress.
            covered = coverage(engine, run_id, doc, required(doc['sections']),
                               [packet['handoff_id']], include_memory=False)
            sections = {s['id']: s for s in doc['sections']}
            refs.extend({'document_id': doc['document_id'], 'document_sha256': doc['sha256'],
                'section_id': r['section_id'], 'offset': r['start'] - sections[r['section_id']]['start'],
                'limit': r['end'] - r['start']} for r in covered['covered_ranges'])
        return digest([scope['assignment'], scope['source_plan'], scope['source_spans'],
            packet['context'][policy.CONTEXT]['contract'],
            packet['context'][policy.CONTEXT]['document'], plan, unknown, _evidence_identity(refs)])
    spans = {}
    for check in checks:
        for ref in check['evidence']:
            key = (check['criterion_id'], ref['document_id'], ref['document_sha256'], ref['section_id'])
            spans.setdefault(key, []).append((ref['offset'], ref['offset'] + ref['limit']))
    merged = []
    for key, ranges_ in sorted(spans.items()):
        intervals = []
        for start, end in sorted(ranges_):
            if intervals and start <= intervals[-1][1]:
                intervals[-1][1] = max(intervals[-1][1], end)
            else:
                intervals.append([start, end])
        merged.append([key, intervals])
    return digest([scope['assignment'], scope['source_plan'], scope['source_spans'],
        packet['context'][policy.CONTEXT]['contract'], plan, unknown, merged,
        _proof_identity(packet['result'].get('unit_proofs', [])) if version == 2
        else packet['result'].get('unit_proofs', [])])


def repeated_work(saved, engine=None, run_id=None):
    """Derive work from the authenticated ordered terminal followup chain.

    This is not an issue, verdict or approval. Callers must authenticate the
    entire predecessor inventory before using this pure derivation.
    """
    if scoped.endpoint_blockers(saved):
        return None
    # Re-derive frozen work with its original identity and author semantics.
    # New work uses acknowledged delivery; no adapter changes the packets.
    version = saved.get(WORK, {}).get('version', 4)
    for prior in scoped.effective_packets(saved):
        task = prior['context'][scoped.KEY]['assignment']
        if task['stage'] not in ('endpoint', 'endpoint_peer'):
            continue
        chain = [p for p in saved.get('endpoint_followups', [])
                 if p['context'][scoped.KEY]['assignment'] == task]
        if len(chain) < 3:
            continue
        identity = gap_identity(chain[-1], version=version, engine=engine, run_id=run_id)
        if identity is None or any(gap_identity(p, version=version, engine=engine, run_id=run_id) != identity for p in chain[-3:]):
            continue
        return policy.sealed({'version': version, 'kind': 'pending_evidence',
            **({'status': '需要作者补证', 'author': 'engineer'} if version == 3 else {}),
            **({'status': '需要修改', 'author': 'engineer'} if version == 4 else {}),
            'semantic_identity': identity, 'assignment': task,
            'document': prior['context'][policy.CONTEXT]['document'],
            'contract': prior['context'][policy.CONTEXT]['contract'],
            'source_plan': prior['context'][scoped.KEY]['source_plan'],
            'source_spans': prior['context'][scoped.KEY]['source_spans'],
            'history': scoped.output_refs(chain), 'verifier': prior['role'],
            'checks': [copy.deepcopy(c) for c in prior['result']['checks'] if c['status'] == 'UNKNOWN']})
    return None


def author_items(work):
    """Explicit v1 adapter to the existing author checklist wire shape.

    These entries exist only in the author request, never in scientific issues.
    issue_id is the legacy checklist key; confirmed/status/severity are absent.
    """
    return [{'kind': 'pending_evidence', 'issue_id': 'endpoint-evidence:' + digest([work['sha256'], c['criterion_id']]),
        'criterion_id': c['criterion_id'], 'verifier': work['verifier'],
        'claim': c['missing_information'], 'evidence': copy.deepcopy(c['evidence']),
        'close_when': [c['missing_information'] + (DIRECTED_EDIT if work['version'] == 4 else '')],
        'work_sha256': work['sha256']}
        for c in work['checks']]


def author_ranges(row, work):
    """Exact submitted author citations, authenticated by validate_submission.

    These are navigation obligations for the original verifier, never reading
    credit or an author verdict. No neighboring units or other issues expand.
    """
    ids = {i['issue_id'] for i in author_items(work)}
    doc = row['document']
    sections = {s['id']: s for s in doc['sections']}
    spans = {}
    for report in row.get('acceptance_patch_reports', []):
        if report['issue_id'] not in ids:
            continue
        for ref in report['evidence']:
            if (ref['document_id'], ref['document_sha256']) != (doc['document_id'], doc['sha256']):
                continue
            section = sections[ref['section_id']]
            start = section['start'] + ref['offset']
            end = start + ref['limit']
            if not section['start'] <= start < end <= section['end']:
                policy.fail('Author endpoint evidence exceeds submitted version')
            spans[(section['id'], start, end)] = {**section, 'start': start, 'end': end, 'chars': end-start}
    return [spans[key] for key in sorted(spans)]


DIRECTED_EDIT = ('。需要修改：在原独立审核范围内实际定向编辑相关正文；缺处补写，'
    '已有内容则补准确定位同版证据的交叉引用、移动关键定义或明确绑定验收项。'
    '保留原要求、来源和科学内容，不得改无关内容制造差异。'
    'revision_plan 使用 revise；patch_reports 申报实际 changed_fields、影响及当前稿引用，'
    'acceptance_completion 仅在相关实际编辑完成后填 complete；仅保存原证据不能完成本待办。')


def validate_directed_edit(work, before, after, reports):
    """V4 author completion gate; historical work keeps its frozen semantics.

    Mechanical field/path checks establish an edit, not semantic sufficiency.
    The original independent verifier still decides the latter.
    """
    if work.get('version') != 4:
        return
    from .implementation_patch_scope import diagnostic
    from .implementation_acceptance_author import IncompleteRevision
    by_id = {r['issue_id']: r for r in reports}
    complete = diagnostic(before, after, reports, set(before) | set(after))
    invalid = set(complete['invalid_paths']) | set(complete['unchanged_claims'])
    pending = []
    for item in author_items(work):
        report = by_id.get(item['issue_id'], {'changed_fields': []})
        allowed = policy.criterion_fields(work['contract'], item['criterion_id'])
        # Keep the complete saved versions: a shared real batch change must
        # not become a nonexistent path merely because this criterion is narrow.
        # Only claims independently valid in this criterion earn edit credit.
        local = diagnostic({k: v for k, v in before.items() if k in allowed},
                           {k: v for k, v in after.items() if k in allowed}, [report], allowed)
        excluded = invalid | set(local['invalid_paths']) | set(local['unchanged_claims'])
        credited = set(report['changed_fields']) - excluded
        # Unreported/foreign edits are checked by the cumulative submission gate.
        # Each pending criterion must itself claim a real, permitted changed unit.
        if not credited:
            pending.append({'issue_id': item['issue_id'], 'condition_index': 0,
                'requirement': item['close_when'][0], 'status': 'incomplete',
                'reason': DIRECTED_EDIT})
    if pending:
        raise IncompleteRevision(pending)


AUTHOR_GUIDANCE = '''
endpoint_evidence_work 是认证的重复待补证据，不是已裁决问题、FAIL 或批准。
acceptance_issues 中 kind=pending_evidence 是该工作项适配到既有作者清单的记录；
issue_id 仅供 revision_plan、patch_reports、acceptance_completion 关联，不进入科学 issue 台账。
使用 documents.locate/read 及 implementation.edit_document 定向处理原缺口。
version=4 的“需要修改”描述下一步 action，不代表此前审核已确认 P0/P1；历史 UNKNOWN 保留。
本待办必须实际定向修改相关正文，不接受 already_satisfied 或只保存原有证据。
缺处补写；已有内容则在原独立范围内补准确定位同版证据的交叉引用、移动关键定义或明确绑定验收项。
revision_plan 使用 revise，patch_reports 如实申报实际 changed_fields、影响及当前稿引用；
acceptance_completion 仅在相关实际编辑完成后填 complete。不得改无关内容制造差异。
保留科学标准及其他正文。作者完成仅是送审条件，原独立角色随后在提交版本复核。
'''


def author_guidance(work):
    if work['version'] == 4:
        return AUTHOR_GUIDANCE
    # Already frozen legacy work retains its optional evidence-only route.
    return '''
endpoint_evidence_work 是认证的重复待补证据，不是已裁决问题、FAIL 或批准。
acceptance_issues 中 kind=pending_evidence 是该工作项适配到既有作者清单的记录；
issue_id 仅供 revision_plan、patch_reports、acceptance_completion 关联，不进入科学 issue 台账。
使用 documents.locate/read 及 implementation.edit_document 定向处理原缺口。
现文已有明确依据时给出确切已读原文引用，already_satisfied 并保存提交；
缺少内容时实际修订相关单元并提交新版本，不能只解释同一狭窄片段。
保留科学标准及其他正文。作者完成仅是送审条件，原独立角色随后在提交版本复核。
'''


def clarification_input(context):
    """An interpretation of exact frozen material, never replacement evidence."""
    scope = context[scoped.KEY]
    ids = {u['unit_id'] for u in scope[KEY]['units']}
    return {'version': 1, 'assignment': scope['assignment'],
        'document': context[policy.CONTEXT]['document'],
        'contract_sha256': context[policy.CONTEXT]['contract']['sha256'],
        'source_plan_sha256': digest(scope['source_plan']), 'source_spans': scope['source_spans'],
        'gap': scope['endpoint_gap'], 'plan': scope[KEY],
        'units': [u for u in scope.get('unit_catalog', []) if u['id'] in ids]}


def compile_plan(context, *, version=2):
    if type(version) is not int or version not in (1, 2):
        policy.fail('Unsupported endpoint supplemental plan version')
    scope = context[scoped.KEY]
    task = scope['assignment']
    edge = task['endpoint']
    atoms = scope.get('unit_catalog', [])
    body = scope['source_plan']['plan']['body']
    unknown = sorted({c['criterion_id'] for p in scope['endpoint_gap']
                      for c in p['result']['checks'] if c['status'] == 'UNKNOWN'})
    requirements = set()
    titles = {}
    for entry in body.get('effective_implementation_map', []):
        # Location may contain only the exact approved path. Behavior must
        # prove both declared endpoints; historical call direction is immaterial.
        if any(scoped.exact_token(edge[s], entry['location']) for s in ('consumer', 'provider')) and all(
                scoped.exact_token(edge[s + '_marker'], entry['behavior']) for s in ('consumer', 'provider')):
            requirements.add(entry['requirement_id'])
            for title in re.findall(r'\[module-title:([^\]]+)\]', entry['behavior']):
                titles.setdefault(title, set()).update(s for s in ('consumer', 'provider')
                    if scoped.exact_token(edge[s], entry['location']))
    # Only the canonical algorithm responsibility table's path column binds
    # a short module title. A dependency column cannot create ownership.
    table_paths, path_rows = {}, set()
    responsibility_table = False
    for a in atoms:
        if a['field'] != 'algorithm':
            responsibility_table = False
            continue
        cells = [c.strip().strip('`') for c in a['text'].strip().strip('|').split('|')]
        if not a['text'].lstrip().startswith('|'):
            responsibility_table = False
        if len(cells) >= 2 and cells[0] in ('模块', 'Module', 'module'):
            responsibility_table = cells[1] in ('计划位置', '批准路径', '路径', '文件路径', 'Path', 'path')
        if (version == 1 or responsibility_table) and a['text'].lstrip().startswith('|') and len(cells) >= 2:
            sides = {s for s in ('consumer', 'provider') if cells[1] == edge[s]}
            if sides:
                path_rows.add(a['id'])
                titles.setdefault(cells[0], set()).update(sides)
            # A repeated title belonging to another path is ambiguous, even
            # when just one of its rows mentions the current endpoint.
            table_paths.setdefault(cells[0], set()).add(cells[1])
    for title, paths in table_paths.items():
        if version == 2 and len(paths) != 1:
            titles.pop(title, None)
    heading_counts = {}
    for a in atoms:
        for title in re.findall(r'^\s*#{1,6}\s+(?:模块\s*[：:]\s*)?([^\n]+)', a['text']):
            heading_counts[title.strip()] = heading_counts.get(title.strip(), 0) + 1
    selected, stack, field, acceptance_level = [], [], None, None
    for a in atoms:
        if a['field'] != field:
            stack, field, acceptance_level = [], a['field'], None
        text = a['text']
        heading = re.match(r'^\s*(#{1,6})\s+(?:模块\s*[：:]\s*)?([^\n]+)', text)
        if heading:
            level, title = len(heading[1]), heading[2].strip()
            if acceptance_level is not None and level <= acceptance_level:
                acceptance_level = None
            while stack and stack[-1][0] >= level:
                stack.pop()
            sides = titles.get(title, set()) if heading_counts.get(title) == 1 else set()
            sides = sides | {s for s in ('consumer', 'provider') if scoped.exact_token(edge[s], title)}
            stack.append((level, sides))
        sides = {s for s in ('consumer', 'provider') if scoped.exact_token(edge[s], text)}
        if not sides and stack:
            sides = next((v for _, v in reversed(stack) if v), set())
        symbols = {s for s in ('consumer', 'provider') if scoped.exact_token(edge[s + '_marker'], text)}
        # Standalone interface semantic rows must identify a unique symbol in
        # the complete frozen edge inventory, not a generic shared function.
        unique = {s for s in symbols if all(e == {k: v for k, v in edge.items() if k != 'side'} or
            edge[s + '_marker'] not in (e['consumer_marker'], e['provider_marker'])
            for e in body['declared_interfaces'])}
        cells = [c.strip().strip('`') for c in text.strip().strip('|').split('|')]
        criterion_row = text.lstrip().startswith('|') and cells[0] in requirements
        criterion_heading = heading and any(re.match(re.escape(r) + r'(?:\s|[：:、.\-]|$)', heading[2])
                                              for r in requirements)
        if criterion_heading:
            acceptance_level = len(heading[1])
        bound_requirement = any(scoped.exact_token('[requirement:' + r + ']', text) for r in requirements)
        reason = None
        path_row = a['id'] in path_rows
        interface_row = unique and text.lstrip().startswith('|') and (
            version == 1 or cells[0] in (edge['consumer_marker'], edge['provider_marker']))
        if a['field'] == 'algorithm' and (symbols & sides or interface_row or version == 2 and path_row):
            reason = 'declared_interface'
        if a['field'] in ('acceptance', 'acceptance_checks', 'acceptance_criteria') and (
                criterion_row or criterion_heading or bound_requirement or acceptance_level is not None):
            reason = 'bound_acceptance'
        if reason and set(a['criteria']) & set(unknown):
            selected.append({'unit_id': a['id'], 'range': a['range'], 'reason': reason})
    return policy.sealed({'version': version, 'document': context[policy.CONTEXT]['document']['sha256'],
        'assignment': task['sha256'], 'source': digest(scope['source_plan']),
        'criteria': unknown, 'units': selected})


def ranges(context):
    # Shared reading planners also serve unscoped and source-only views.
    # Absence of an endpoint plan must never create an obligation or a grant.
    plan = context.get(scoped.KEY, {}).get(KEY)
    if plan is None:
        return []
    scope = context[scoped.KEY]
    if 'plan' not in scope['source_plan']:
        # Disposable outbound views contain only source hashes. They may
        # navigate the already frozen authority, never reconstruct new grants.
        doc = context[policy.CONTEXT]['document']
        sections = {s['id']: s for s in doc['sections']}
        for unit in plan['units']:
            span = unit['range']
            if not scoped.permitted_read(context, {'document_id': doc['document_id'],
                    'expected_sha256': doc['sha256'], 'section_id': span['id'],
                    'offset': span['start'] - sections[span['id']]['start'],
                    'limit': span['end'] - span['start']}):
                policy.fail('Projected endpoint plan exceeds frozen authority')
    elif plan != compile_plan(context, version=plan.get('version')):
        policy.fail('Endpoint supplemental authority differs from canonical source')
    return [u['range'] for u in plan['units']]


def _time(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        policy.fail('Endpoint denial lacks a valid timestamp')


def failure(engine, run_id, hid, role, purpose):
    """Read-only legacy adapter: require an unambiguous terminal event window.

    No invocation is fabricated for a pre-execution guard rejection. Equal
    arguments in older requests are not evidence of current execution.
    """
    store = engine.store
    frozen = store.get('handoff_inputs', hid, {})
    handoff = store.get('handoffs', hid, {})
    progress = store.get('handoff_progress', hid, {})
    normal = progress.get('normal_request', {})
    response = normal.get('response', {})
    pending = progress.get('pending_tool_calls')
    if (handoff.get('status') != 'blocked' or handoff.get('error') != ERROR
            or handoff.get('to_role') != role or handoff.get('purpose') != purpose
            or handoff.get('run_id') != run_id or store.get('decisions', hid)
            or handoff.get('input_hash') != digest({k: frozen.get(k) for k in ('instruction', 'context', 'schema')})
            or progress.get('tool_cursor') != 0 or progress.get('tool_counted') is not True
            or not pending or pending != response.get('json', {}).get('tool_calls')
            or normal.get('status') != 'response_saved'
            or normal.get('prepared_sha256') != digest(normal.get('prepared'))
            or normal.get('turn') != progress.get('turn')
            or pending[0].get('name') != 'documents.read'
            or scoped.permitted_read(frozen['context'], pending[0]['arguments'])):
        policy.fail('Endpoint denial lacks request-bound rejection evidence')
    if any(frozen.get(k) not in (None, expected) for k, expected in
           (('run_id', run_id), ('role', role), ('purpose', purpose))):
        policy.fail('Endpoint denial frozen request identity conflicts')
    from .tool_invocations import identity
    request_id = response.get('request_id')
    if not request_id:
        policy.fail('Endpoint denial lacks paid request identity')
    if any(store.get('tool_invocations', identity(run_id, role, hid, progress['turn'], i, call))
           for i, call in enumerate(pending)) or any(v.get('request_id') == request_id
                for v in store.list('tool_invocations', run_id)):
        policy.fail('Endpoint denied batch has execution intent; reconcile before migration')
    calls = [c for c in handoff.get('model_calls', []) if c.get('request_id') == request_id]
    transcripts = [t for t in store.list('transcripts', run_id) if t.get('request_id') == request_id]
    if (len(calls) != 1 or not transcripts or any(c.get('role') != role for c in transcripts)
            or any(calls[0].get(k) != response.get(k) for k in ('provider', 'model', 'transcript_id'))):
        policy.fail('Endpoint denial lacks retained provider evidence')
    outputs = [t for t in transcripts if t.get('id') == response.get('transcript_id') and t.get('kind') == 'output']
    try:
        raw_matches = len(outputs) == 1 and json.loads(outputs[0]['text']) == response['json']
    except (ValueError, KeyError, TypeError):
        raw_matches = False
    if not raw_matches:
        policy.fail('Endpoint denial raw response differs from pending batch')
    start, received, terminal = map(_time, (normal.get('dispatched_at'), normal.get('received_at'), handoff.get('updated_at')))
    if not start <= received <= terminal:
        policy.fail('Endpoint denial request chronology conflicts')
    if calls[0].get('received_at') and not received <= _time(calls[0]['received_at']) <= terminal:
        policy.fail('Endpoint denial provider receipt chronology conflicts')
    events, after = [], 0
    while True:
        page = store.events(run_id, after=after, limit=5000)
        if not page:
            break
        events.extend(page)
        after = page[-1]['id']
    matches = [e for e in events if e['kind'] == 'run.blocked' and e['message'] == ERROR
               and 0 <= _time(e['created_at']) - terminal <= 5]
    # Newer events may carry identities. Never downgrade a conflicting
    # explicit binding to the legacy timestamp-only adapter.
    if any(any(e.get('data', {}).get(k) not in (None, expected) for k, expected in
               (('handoff_id', hid), ('role', role), ('purpose', purpose),
                ('request_id', request_id))) for e in matches):
        policy.fail('Endpoint denial event request identity conflicts')
    competitors = [h for h in store.list('handoffs', run_id) if h.get('id') != hid
                   and h.get('error') == ERROR and abs(_time(h.get('updated_at')) - terminal) <= 5]
    if len(matches) != 1 or competitors:
        policy.fail('Endpoint denial event window is not unique')
    return policy.sealed({'version': 'legacy-terminal-v1', 'handoff_id': hid, 'role': role, 'purpose': purpose,
        'input_sha256': digest(frozen), 'progress_sha256': digest(progress), 'terminal_sha256': digest(handoff),
        'event': matches[0], 'model_calls_sha256': digest(calls), 'transcripts_sha256': digest(transcripts)})


def prepare(engine, h, run_id, saved, prior, context, purpose):
    from .codex_workflow import _frozen_spec_assignment
    role = context[scoped.KEY]['assignment']['role']
    frozen = _frozen_spec_assignment(engine, run_id, purpose, role)
    proof = None
    if frozen:
        hid = h.decision_key(run_id, role, purpose, **{k: frozen[k] for k in ('instruction', 'context', 'schema')})
        terminal = engine.store.get('handoffs', hid, {})
        # Normal pause, partial reads, final-only and corrections keep their
        # original identity and authority. Only an authenticated denial migrates.
        if engine.store.get('decisions', hid) or terminal.get('error') != ERROR:
            return purpose, frozen['context']
        if frozen['context'][scoped.KEY].get('endpoint_gap') != scoped.output_refs([prior]):
            policy.fail('Endpoint denial does not belong to current predecessor')
        if any(frozen['context'][scoped.KEY].get(k) != context[scoped.KEY].get(k)
               for k in ('assignment', 'identity', 'source_plan', 'source_spans')):
            policy.fail('Endpoint denial frozen scope changed')
        proof = failure(engine, run_id, hid, role, purpose)
    context = copy.deepcopy(context)
    scope = context[scoped.KEY]
    scope[KEY] = compile_plan(context)
    if proof:
        scope['endpoint_failed_predecessor'] = proof
        purpose += ':narrow-v1:' + proof['sha256']
    scope.pop(scoped.AUTHORITY, None)
    return purpose, context


def validate_followup(engine, run_id, packet):
    context = packet['context']
    ranges(context)
    clarification = context[scoped.KEY].get(CLARIFICATION)
    if clarification is not None:
        from . import implementation_acceptance_flow as flow
        flow.check_packet(engine, run_id, clarification)
        source = clarification_input(context)
        if (clarification['role'] != 'engineer'
                or clarification['context'].get(CLARIFICATION) != source
                or clarification['result'].get('source_sha256') != digest(source)):
            policy.fail('Endpoint source clarification changed its frozen gap')
    proof = context[scoped.KEY].get('endpoint_failed_predecessor')
    if proof is not None:
        if proof != failure(engine, run_id, proof['handoff_id'], packet['role'], proof['purpose']):
            policy.fail('Endpoint denial evidence changed on replay')
        handoff = engine.store.get('handoffs', packet['handoff_id'], {})
        if handoff.get('purpose') != proof['purpose'] + ':narrow-v1:' + proof['sha256']:
            policy.fail('Endpoint successor purpose changed')
