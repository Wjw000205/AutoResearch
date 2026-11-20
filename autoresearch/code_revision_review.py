"""Independent review of the changes since the last independently reviewed delivery."""
from __future__ import annotations

import copy
from collections import Counter
from difflib import SequenceMatcher
from itertools import accumulate
from pathlib import Path

from . import independent_full_review as full
from .architecture_validation import once

KEY = 'code_revision_review'
DOMAIN = 'revised_code_delivery'
GUIDANCE = ('两位评审分别只审查上一轮意见与本次实际代码差异。逐条说明原问题是否解决，引用对应变更编号。'
    '同时检查这些改动引入的回归。删除项使用旧版片段作为依据。问题未解决或证据不够时给出具体原因与后续需求。'
    '原问题延续仅引用本角色的 origin_id，新问题使用 null。只读 reviews.read_material 所列的本次材料。')


def parent(state, attempt):
    from .code_repair_contract import previous_attempt
    prior = previous_attempt(state, attempt['index'], reviewed=True)
    if prior and (prior.get(KEY) or prior.get('whole_code_review')) and full.reviewers(prior['review_votes']) == full.REVIEWERS:
        return prior
    return None


def _directory(engine, run_id, attempt):
    from .codex_workflow import _verify_snapshot
    snapshot = attempt['snapshot']
    _verify_snapshot(engine, run_id, snapshot)
    root = Path(snapshot['workspace']).relative_to(engine.run_dir(run_id))
    expected = [{'artifact_id': run_id + ':' + (root / row['path']).as_posix(), **row} for row in snapshot['files']]
    if expected != attempt['delivery_artifacts'] or len({r['path'] for r in expected}) != len(expected):
        raise ValueError('修改复审目录与原交付快照不一致')
    return {row['path']: row for row in expected}


def _changes(engine, run_id, before, after):
    from .implementation_alignment_evidence import _code_text
    changes = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old and new and old['sha256'] == new['sha256']:
            continue
        texts = [_code_text(engine, run_id, row['artifact_id'], row, allow_binary=True) if row else ''
                 for row in (old, new)]
        item = {'id': len(changes) + 1, 'path': path,
                'kind': 'added' if not old else 'removed' if not new else 'modified',
                'before': old, 'after': new, 'hunks': []}
        if None in texts:
            item['binary'] = True
        else:
            left, right = (text.splitlines(keepends=True) for text in texts)
            offsets = [list(accumulate(map(len, lines), initial=0)) for lines in (left, right)]
            for op, a, b, c, d in SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
                if op != 'equal':
                    item['hunks'].append({'before_offset': offsets[0][a], 'before': ''.join(left[a:b]),
                        'after_offset': offsets[1][c], 'after': ''.join(right[c:d])})
        changes.append(item)
    return changes


@once
def compile_context(engine, run_id, state, spec, attempt):
    from . import code_whole_policy as policy
    from .code_repair_contract import issues
    prior = parent(state, attempt)
    if prior is None:
        raise ValueError('修改复审缺少上一版完整独立评审')
    feedback = policy.previous_feedback(engine, run_id, state, spec, attempt)
    votes = feedback['votes']
    from .code_whole_review import validate_vote as check_parent
    prior_spec = policy._specification(state, prior, spec)
    for vote in votes:
        check_parent(engine, run_id, state, prior_spec, prior, vote)
    before, after = _directory(engine, run_id, prior), _directory(engine, run_id, attempt)
    changes = _changes(engine, run_id, before, after)
    findings = issues(prior)
    old_by_id = {row['artifact_id']:row for row in before.values()}
    for issue in findings:
        vote = next(v for v in votes if v['role']==issue['role'])
        issue['original_evidence'] = []
        for index in (issue['binding'] or {}).get('evidence_indices',[]):
            ref = copy.deepcopy(vote['code_evidence'][index])
            old = old_by_id.get(ref['artifact_id'])
            current = after.get(old['path']) if old else None
            issue['original_evidence'].append({'reference':ref,'unchanged_source':
                current if current and current['sha256']==ref['sha256'] else None})
    material = {'previous_reviews': [{'role': v['role'], 'approve': v['approve'], 'rationale': v['rationale'],
        'issues': [row for row in findings if row['role'] == v['role']]} for v in votes], 'changes': changes}
    marker = {'version': 1, 'workflow_id': state['id'], 'attempt': attempt['index'],
        'base_attempt': prior['index'], 'base_delivery_hash': prior['delivery_hash'],
        'base_votes_sha256': full.sha(votes), 'packet_sha256': full.sha(material),
        'directory_sha256': full.sha(attempt['delivery_artifacts'])}
    # The first save precedes dispatch. Keep existing handoff identities on replay.
    if KEY in attempt and 'answer_schema_version' in attempt[KEY] and (
            type(attempt[KEY]['answer_schema_version']) is not int or attempt[KEY]['answer_schema_version'] != 2):
        raise ValueError('未知的修改复审作答合同版本')
    if KEY not in attempt or 'answer_schema_version' in attempt[KEY]:
        marker['answer_schema_version'] = 2
    return {KEY: marker, 'revision_material': material, 'proposal_hash': state['proposal_hash'],
        'spec_hash': spec['spec_hash'], 'delivery_hash': attempt['delivery_hash'], 'code_sha256': attempt['code_sha256']}


def schema(context, spec):
    from .codex_workflow import DELIVERY_REVIEW, TEXT
    from .code_repair_contract import review_schema
    from .engine import obj
    result = review_schema(DELIVERY_REVIEW)
    properties = result['properties']
    for key in ('proposal_hash', 'spec_hash', 'delivery_hash', 'code_sha256'):
        properties[key] = {'const': context[key], 'type':'string'}
    properties['repair_findings']['items']['properties']['requirement_id'] = {
        'enum': [r['id'] for r in spec['spec']['acceptance_checks']]}
    properties['previous_issues'] = {'type':'array','items':obj({'issue_id':TEXT,
        'status':{'enum':['resolved','unresolved']}, 'explanation':TEXT,
        'change_ids':{'type':'array','items':{'type':'integer','minimum':1},'uniqueItems':True},
        'issue_indices':{'type':'array','items':{'type':'integer','minimum':0},'uniqueItems':True}})}
    properties['code_evidence'] = {'type':'array','items':obj({'artifact_id':TEXT,'sha256':TEXT,
        'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1},
        'requirement_id':TEXT, 'verification':TEXT,
        'implementation_refs':{'type':'array','maxItems':0,'items':{'type':'string'}}})}
    result['required'].extend(['previous_issues','code_evidence'])
    if 'answer_schema_version' in context[KEY]:
        if type(context[KEY]['answer_schema_version']) is not int or context[KEY]['answer_schema_version'] != 2:
            raise ValueError('未知的修改复审作答合同版本')
        constrain_answer_schema(result, context)
    return result


def constrain_answer_schema(output, context, *, role=None):
    """Compile finite identities from the same frozen material as validate_answer."""
    props = output['properties']
    material = context['revision_material']
    issues = [issue for review in material['previous_reviews'] for issue in review['issues']]
    ids = [issue['issue_id'] for issue in issues]
    if role is not None:
        if role not in full.REVIEWERS:
            raise ValueError('修改复审来源必须绑定原独立评审角色')
        own = [issue['issue_id'] for review in material['previous_reviews'] if review['role'] == role
               for issue in review['issues']]
        props['repair_findings']['items']['properties']['origin_id'] = {'enum':[None,*own]}
    rows = props['previous_issues']
    rows.update(minItems=len(ids), maxItems=len(ids))
    if ids:
        rows['items']['properties']['issue_id'] = {'type':'string', 'enum':ids}
        # Contains + exact cardinality enforces each ID once, in any order.
        rows['allOf'] = [{'contains':{'properties':{'issue_id':{'const':key}},'required':['issue_id']}} for key in ids]
    changes = material['changes']
    change_ids = rows['items']['properties']['change_ids']
    if changes:
        change_ids['items'] = {'type':'integer','enum':[row['id'] for row in changes]}
    else:
        change_ids['maxItems'] = 0
    sources = [row[side] for row in changes for side in ('before','after') if row[side]]
    sources.extend(e['unchanged_source'] for issue in issues for e in issue.get('original_evidence',[])
                   if e.get('unchanged_source'))
    refs = props['code_evidence']
    if sources:
        for field in ('artifact_id','sha256'):
            refs['items']['properties'][field] = {'type':'string','enum':sorted({row[field] for row in sources})}
    else:
        refs['maxItems'] = 0
    refs['items']['properties']['requirement_id'] = copy.deepcopy(
        props['repair_findings']['items']['properties']['requirement_id'])


def refill_answer_schema(schema, previous):
    """Keep the author's existing objections while allowing erroneous references to be replaced."""
    result = copy.deepcopy(schema)
    fixed = {'rationale':{'const':previous['rationale']}}
    if previous['approve'] is False:
        fixed['approve'] = {'const':False}
    issues = previous['issues']
    if issues:
        fixed['issues'] = {'prefixItems':[{'const':issue} for issue in issues], 'minItems':len(issues)}
    findings = previous['repair_findings']
    counts = Counter(row['issue_index'] for row in findings)
    # A duplicate association must remain editable. Preserve the other
    # judgments by issue identity, independent of array position.
    unique = [row for row in findings if 0 <= row['issue_index'] < len(issues)
              and counts[row['issue_index']] == 1]
    if unique:
        permitted = schema['properties']['repair_findings']['items']['properties']['origin_id'].get('enum')
        required = []
        for row in unique:
            fields = {k:{'const':row[k]} for k in ('issue_index','disposition','regression')}
            if permitted is None or row['origin_id'] in permitted:
                fields['origin_id'] = {'const':row['origin_id']}
            required.append({'contains':{'properties':fields}})
        fixed['repair_findings'] = {'allOf':required}
    changes = {row['issue_index'] for row in unique if row['disposition'] == 'repair_code'}
    fixes = [fix for fix in previous['fixes'] if fix['issue_index'] in changes]
    if fixes:
        fixed['fixes'] = {'allOf':[{'contains':{'const':fix}} for fix in fixes]}
    result.setdefault('allOf',[]).append({'properties':fixed})
    return result


def binding(context, output):
    return full.make_binding({KEY: context[KEY], **({'clarification_sha256':full.sha(context['author_source_clarification'])}
        if 'author_source_clarification' in context else {}), **{k:context[k] for k in (
        'proposal_hash','spec_hash','delivery_hash','code_sha256')}, 'schema_sha256':full.sha(output)}, DOMAIN)


def _supported_evidence(evidence, original, changes):
    supported = any(row['unchanged_source'] and evidence['artifact_id']==row['unchanged_source']['artifact_id']
        and all(evidence[k]==row['reference'][k] for k in ('sha256','offset','limit','requirement_id'))
        for issue in original for row in issue.get('original_evidence',[]))
    for change in changes:
        for side in ('before','after'):
            source = change[side]
            if not source or (source['artifact_id'],source['sha256']) != (evidence['artifact_id'],evidence['sha256']):
                continue
            start, end = evidence['offset'], evidence['offset'] + evidence['limit']
            supported |= any(h[side+'_offset'] <= start < end <= h[side+'_offset']+len(h[side]) for h in change['hunks'])
    return supported


def answer_sources(context, *, role=None):
    """Operand directory for a final answer after the original pages leave its window."""
    from .code_whole_review import RESPONSE_GUIDANCE
    material = context['revision_material']
    marker = context[KEY]
    policy = full.validate_binding(context[full.KEY])
    if (marker.get('version') != 1 or policy['domain'] != DOMAIN or policy['source_identity'].get(KEY) != marker
            or full.sha(material) != marker['packet_sha256']):
        raise ValueError('修改复审收答目录与冻结材料不一致')
    issues, sources = [], []
    for review in material['previous_reviews']:
        for issue in review['issues']:
            issues.append({'role':review['role'], 'issue_id':issue['issue_id'], 'issue':issue['issue']})
            for entry in issue.get('original_evidence', []):
                source = entry.get('unchanged_source')
                if source:
                    ref = entry['reference']
                    row = {'artifact_id':source['artifact_id'], 'sha256':source['sha256'], 'path':source['path'],
                        'unchanged_reference':{k:ref[k] for k in ('requirement_id','offset','limit')}}
                    if row not in sources:
                        sources.append(row)
    for change in material['changes']:
        for side in ('before','after'):
            source = change[side]
            if source:
                sources.append({'change_id':change['id'], 'side':side, 'path':change['path'],
                    'artifact_id':source['artifact_id'], 'sha256':source['sha256'],
                    'ranges':[[h[side+'_offset'],len(h[side])] for h in change['hunks'] if h[side]]})
    previous = context.get(full.READING, {}).get('previous_own_result')
    corrections = {}
    if previous is not None:
        original = [i for review in material['previous_reviews'] for i in review['issues']]
        received = {row['issue_id'] for row in previous['previous_issues']}
        corrections = {'missing_previous_issue_ids':[i['issue_id'] for i in original if i['issue_id'] not in received],
            'unsupported_code_evidence_indices':[i for i, ref in enumerate(previous['code_evidence'])
                if not _supported_evidence(ref, original, material['changes'])]}
        if role is not None:
            origins = {i['issue_id'] for i in issues if i['role'] == role}
            corrections['invalid_origin_finding_indices'] = [n for n, finding in enumerate(previous['repair_findings'])
                if finding['origin_id'] is not None and finding['origin_id'] not in origins]
    return {'version':1 if role is None else 2, **({'role':role} if role is not None else {}),
        'packet_sha256':marker['packet_sha256'], 'previous_issues':issues, 'sources':sources,
        **({'previous_answer_diagnostics':corrections} if corrections else {}),
        'instructions':'previous_issues 必须覆盖目录内两位评审的全部 issue_id，各一次。每个 ranges 条目为'
        '[起始字符 offset, 字符数 limit]，引用可取其中非空子区间。unchanged_reference 只可按原三项精确复用。'
        '依据自己读过的差异独立判断。repair_findings.evidence_indices 只指本次答复 code_evidence 数组的'
        '零起始下标，repair_code 的 requirement_id 须与引用一致；缺少可支持的证据要说明具体缺口。'
        + RESPONSE_GUIDANCE}


def validate_answer(context, raw, role, spec):
    from .code_whole_review import validate_response
    material = context['revision_material']
    changes = {row['id']:row for row in material['changes']}
    original = {i['issue_id']:i for v in material['previous_reviews'] for i in v['issues']}
    rows = raw['previous_issues']
    requirements = {r['id'] for r in spec['spec']['acceptance_checks']}
    problems = []
    if any(raw[k] != context[k] for k in ('proposal_hash','spec_hash','delivery_hash','code_sha256')):
        problems.append('本票必须绑定当前交付身份')
    if len(rows) != len(original) or {r['issue_id'] for r in rows} != set(original):
        problems.append('逐一回应两位评审的全部原始问题')
    clarified = {r['issue_id'] for r in context.get('author_source_clarification',{}).get('result',{}).get('clarifications',[])
        if r.get('explanation','').strip() and not r.get('missing_external_resource','').strip()}
    for row in rows:
        if (set(row['change_ids']) - changes.keys() or not row['explanation'].strip()
                or row['status']=='resolved' and not row['change_ids'] and row['issue_id'] not in clarified
                or row['status']=='resolved' and row['issue_indices']
                or row['status']=='unresolved' and (not row['issue_indices'] or
                    any(i>=len(raw['issues']) for i in row['issue_indices']))
                or raw['approve'] and row['status'] != 'resolved'):
            problems.append('原问题的处置必须有实际变更依据，未解决问题不能批准')
    for finding in raw['repair_findings']:
        origin = finding['origin_id']
        if origin is not None and original.get(origin,{}).get('role') != role:
            problems.append('延续的原问题身份与本人原始意见不符')
    for evidence in raw['code_evidence']:
        supported = _supported_evidence(evidence, original.values(), changes.values())
        if not supported or evidence['requirement_id'] not in requirements:
            problems.append('代码证据必须指向真实增删片段内的字符范围及当前验收项')
    if problems:
        raise full.ResponseCorrectionRequired('；'.join(dict.fromkeys(problems)), [{'kind':'revision_evidence'}])
    validate_response(raw, requirements)


def validate_vote(engine, run_id, state, spec, attempt, vote):
    frozen = full.authenticate(engine.store,run_id,vote)
    context = frozen['context']
    expected = compile_context(engine,run_id,state,spec,attempt)
    original = full.source_context(context)
    clarification = original.pop('author_source_clarification',None)
    if clarification:
        from .implementation_alignment_evidence import _delivery
        source = _delivery(engine,run_id,'engineer',clarification['handoff_id'],clarification['result'])
        record = source.get('whole_code_source_clarification',{})
        if (record.get('workflow_id'),record.get('source_attempt'),record.get('source_sha256')) != (
                state['id'],attempt['index'],clarification['source_sha256']):
            raise ValueError('修改复审澄清来源不一致')
    if (original != expected or attempt.get(KEY) != expected[KEY] or frozen['schema'] != schema(expected,spec)
            or vote[full.KEY]['policy'] != binding(context,frozen['schema'])):
        raise ValueError('修改复审与冻结的原意见、快照或输出合同不一致')
    full.validate_delivery(engine.store,run_id,vote,engine=engine)
    validate_answer(context,full.answer(vote),vote['role'],spec)
    return context


def _paid_answer(harness, run_id, frozen, slot, role, raw):
    from .codex_reasoning import key_for
    from .native_retained_response import captured_delivery, recover
    prepared = slot.get('prepared') or {}
    if (slot.get('provider_id') != 'local_codex' or prepared.get('role') != role
            or prepared.get('context',{}).get(full.KEY) != frozen['context'][full.KEY]):
        return None
    native_id, operation = key_for(harness.intelligence,run_id,role,prepared['task'],prepared['context'],prepared['schema'])
    store = harness.engine.store
    saved = store.get('codex_reasoning',native_id,{})
    if (saved.get('id') != native_id or saved.get('operation_id') != operation or saved.get('run_id') != run_id
            or saved.get('role') != role or saved.get('schema') != prepared['schema']
            or not captured_delivery(store,saved,frozen)):
        return None
    verified = recover(harness.intelligence,saved,strict_source=True)
    response = slot.get('response') or {}
    if (not verified or verified['json'] != raw or raw != response.get('json')
            or any(verified[k] != response.get(k) for k in ('thread_id','turn_id','request_id'))):
        return None
    return saved, verified


def recover_evicted_answer_sources(harness, run_id, vote, problems):
    """Replace one unusable legacy correction, only with its original paid input/output proof."""
    from .memory_capture import sha_text
    from .code_whole_review import RESPONSE_GUIDANCE

    policy = vote[full.KEY]['policy']
    kinds = {p['kind'] for p in problems}
    if policy.get('domain') != DOMAIN or kinds.isdisjoint({
            'revision_evidence','code_change_evidence','invalid_requirement_or_evidence',
            'issue_classification','executable_fix_scope'}):
        return None
    store = harness.engine.store
    for hid in full.reading_handoffs(store, run_id, vote):
        frozen = store.get('handoff_inputs',hid)
        ctx = frozen['context']
        final = store.get('handoff_progress',hid,{}).get('finalization') or {}
        prepared = final.get('prepared') or {}
        view = prepared.get('context',{})
        if view.get('revision_answer_sources') or ctx.get(full.READING,{}).get('answer_source_recovery'):
            return None
        if (final.get('status') != 'completed' or final.get('provider_id') != 'local_codex'
                or view.get('_harness_control',{}).get('mode') != 'final_only'
                or prepared.get('role') != vote['role'] or view.get(full.KEY) != policy
                or not ctx.get(full.READING,{}).get('parent_handoff_id')):
            continue
        raw = store.get('decisions',hid,{})['result']
        receipt = _paid_answer(harness,run_id,frozen,final,vote['role'],raw)
        if not receipt:
            continue
        saved, verified = receipt
        # Bind an omitted classification rule to the very answer rejected by
        # the semantic validator, even if its evidence spans were all valid.
        omitted_rule = (hid == vote[full.KEY]['handoff_id']
            and not kinds.isdisjoint({'issue_classification','executable_fix_scope','code_change_evidence'})
            and RESPONSE_GUIDANCE not in saved['instruction'])
        material = ctx['revision_material']
        issues = [i for review in material['previous_reviews'] for i in review['issues']]
        returned = {r['issue_id'] for r in raw['previous_issues']}
        missing = [i['issue_id'] for i in issues if i['issue_id'] not in returned
                   and i['issue_id'] not in saved['instruction']]
        # The legacy directory advertised whole-file byte sizes, but required
        # citations within character spans. Identify actual rejected citations
        # that copied that specific size from the matching file version.
        file_sizes = {(src['artifact_id'],src['sha256']):src['bytes'] for ch in material['changes']
                      for src in (ch['before'],ch['after']) if src}
        size_refs = [n for n, ref in enumerate(raw['code_evidence']) if ref['offset']==0 and
                     file_sizes.get((ref['artifact_id'],ref['sha256']))==ref['limit'] and
                     not _supported_evidence(ref,issues,material['changes'])]
        if missing or size_refs or omitted_rule:
            return {'version':1,'source_handoff_id':hid,'native_id':saved['id'],'turn_id':verified['turn_id'],
                'request_id':verified['request_id'],'input_sha256':sha_text(saved['instruction']),
                'result_sha256':full.sha(raw),'packet_sha256':ctx[KEY]['packet_sha256'],
                'missing_issue_ids':missing,'whole_file_size_evidence_indices':size_refs,
                **({'missing_response_guidance':True} if omitted_rule else {})}
    return None


def recover_answer_contract(harness, run_id, vote, problems):
    legacy = recover_evicted_answer_sources(harness,run_id,vote,problems)
    if legacy or not any(p['kind'] == 'revision_evidence' for p in problems):
        return legacy
    from .memory_capture import sha_text
    from .prose_schema import validator
    store = harness.engine.store
    frozen = full.authenticate(store,run_id,vote)
    context = frozen['context']
    reading = context.get(full.READING, {})
    if (vote[full.KEY]['policy']['domain'] != DOMAIN or reading.get('recovery_kind') != 'response_correction'
            or (reading.get('answer_source_recovery') or {}).get('version') == 2):
        return None
    hid, role = vote[full.KEY]['handoff_id'], vote['role']
    progress = store.get('handoff_progress',hid,{})
    slot = progress.get('finalization') or progress.get('normal_request') or {}
    if slot.get('status') not in ('response_saved','completed'):
        return None
    prepared = slot.get('prepared') or {}
    old = prepared.get('context',{}).get('revision_answer_sources') or {}
    if old.get('version') not in (None,1):
        return None
    raw = full.answer(vote)
    receipt = _paid_answer(harness,run_id,frozen,slot,role,raw)
    if not receipt:
        return None
    saved, verified = receipt
    effective = copy.deepcopy(frozen['schema'])
    constrain_answer_schema(effective,context,role=role)
    errors = list(validator(effective).iter_errors(raw))
    material = context['revision_material']
    issues = [i for review in material['previous_reviews'] for i in review['issues']]
    invalid_spans = [i for i, ref in enumerate(raw['code_evidence'])
        if not old and not _supported_evidence(ref,issues,material['changes'])]
    if not errors and not invalid_spans:
        return None
    return {'version':2,'source_handoff_id':hid,'native_id':saved['id'],'turn_id':verified['turn_id'],
        'request_id':verified['request_id'],'input_sha256':sha_text(saved['instruction']),
        'result_sha256':full.sha(raw),'packet_sha256':context[KEY]['packet_sha256'],
        'schema_sha256':full.sha(effective),'unsupported_code_evidence_indices':invalid_spans}
