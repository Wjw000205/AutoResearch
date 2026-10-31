"""Derived issue continuity; never rewrite original opinions or infer approval."""
from __future__ import annotations

import copy
import re

VERSION = 1

INSTRUCTION = '''
previous_architecture_review.issue_ledger 是跨版本稳定的问题账本，ID 不随措辞或文档版本改变。
progress.issue_updates 必须逐项覆盖本角色账本（包括已闭合项）。未解决项用 carried_forward，
并用 current_issue_indices 绑定本次必改数组；同义改述仍绑定原 ID，不得通过另起问题隐藏旧异议。
resolved 仅在本版已实际核验原 fixes.verification_plan 后使用，给出 affected_fields 和
acceptance_evidence 的同版本文档定位及解释；仅字段哈希变化、换措辞、遗漏旧问题不算闭合。
affected_fields 相对该问题的 origin_material 核对，重开相对 closed_material 核对；
前版已完成但尚未获独立核验的修改可以本次首次闭合，不要求为了让哈希变化再改正文。
已闭合且不受影响的问题继续 resolved，可复用原闭合证据；重开必须用 reopened，
说明本版新冲突、实际受影响字段及已读取的同版本决定性证据。不得任意推翻已核验条款。
这些记录只约束意见连续性，不代替独立科学判断或执行授权。
同一文档仅因缺少读取产生的误判可用 evidence_only=true，affected_fields 为空；
必须本次新读取同版本决定性范围，不能复用上轮已有读取冒充新证据，也不要求为了补读而改正文。
'''


def extend_progress(shape):
    from .engine import obj, STRS
    result = copy.deepcopy(shape)
    text = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
    anchor = obj({'document_id': copy.deepcopy(text), 'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'char_start': {'type': 'integer', 'minimum': 0}, 'char_end': {'type': 'integer', 'minimum': 1},
        'explanation': copy.deepcopy(text)})
    update = obj({'issue_id': copy.deepcopy(text),
        'disposition': {'enum': ['resolved', 'carried_forward', 'reopened']},
        'current_issue_indices': {'type': 'array', 'uniqueItems': True,
                                  'items': {'type': 'integer', 'minimum': 0}},
        'affected_fields': copy.deepcopy(STRS),
        'acceptance_evidence': {'type': 'array', 'items': anchor}, 'rationale': copy.deepcopy(text)})
    update['properties']['evidence_only'] = {'type': 'boolean'}
    result['properties']['issue_updates'] = {'type': 'array', 'items': update}
    result['required'].append('issue_updates')
    return result


def _normal(text):
    return re.sub(r'\s+', ' ', str(text)).strip()


def initial(rows, source_id=None):
    """The first recorded ID is an origin, never a fresh ID on each revision."""
    return [{**copy.deepcopy(row), 'status': 'open', 'origin_source_id': source_id,
             'last_source_id': source_id, 'closure_evidence': [], 'reopen_evidence': []}
            for row in rows]


def rows(receipt):
    if 'issue_ledger' in receipt:
        return copy.deepcopy(receipt['issue_ledger'])
    return initial(receipt.get('objections', []), receipt.get('source_id'))


def history_snapshot(history):
    """Recover old open objections without inventing legacy closure receipts."""
    snapshot = None
    for receipt in history:
        if 'issue_ledger' in receipt:
            snapshot = receipt
        else:
            snapshot = {**receipt, **reconcile(snapshot, receipt)}
    return snapshot


def validate_vote(vote, context, document):
    """Check identity/version/coverage, leaving evidence reading to the review gate."""
    if context.get('issue_ledger_protocol') != VERSION:
        return
    prior = context.get('previous_architecture_review', {}).get('issue_ledger', [])
    updates = vote.get('progress', {}).get('issue_updates')
    if not isinstance(updates, list):
        raise ValueError('架构终审缺少跨版本问题账本。')
    by_id = {row['id']: row for row in prior}
    if (any(not isinstance(update, dict) for update in updates)
            or len({u.get('issue_id') for u in updates}) != len(updates)
            or {u.get('issue_id') for u in updates} != set(by_id)):
        raise ValueError('架构终审必须逐项核验原问题 ID，不能遗漏、重造或重复登记。')
    changed = set(context.get('previous_architecture_review', {}).get('changed_fields', []))
    issues = vote.get('issues', vote.get('required_changes', []))
    assigned = set()
    for update in updates:
        old = by_id[update['issue_id']]
        disposition = update.get('disposition')
        indices = update.get('current_issue_indices', [])
        if (not isinstance(indices, list) or any(type(i) is not int or i < 0 or i >= len(issues) for i in indices)
                or len(set(indices)) != len(indices) or assigned.intersection(indices)):
            raise ValueError('稳定问题与本轮必改项的对应关系无效或重复。')
        assigned.update(indices)
        if disposition not in {'resolved', 'carried_forward', 'reopened'}:
            raise ValueError('问题账本必须明确闭合、延续或重开。')
        if disposition == 'resolved' and indices or disposition != 'resolved' and not indices:
            raise ValueError('未闭合问题须保留本轮必改项；闭合问题不能仍列作阻塞。')
        if old.get('status') == 'closed' and disposition == 'carried_forward':
            raise ValueError('已闭合问题只能保持闭合或以新证据明确重开。')
        if disposition == 'reopened' and old.get('status') != 'closed':
            raise ValueError('只能重开已闭合问题。')
        if not isinstance(update.get('rationale'), str) or not update['rationale'].strip():
            raise ValueError('问题状态变更必须说明核验依据。')
        transition = disposition == 'reopened' or disposition == 'resolved' and old.get('status') != 'closed'
        if not transition:
            continue
        affected = set(update.get('affected_fields', []))
        targets = set(old.get('affected_fields', []))
        baseline = old.get('closed_material') if disposition == 'reopened' else old.get('origin_material')
        current_material = context.get('previous_architecture_review', {}).get('current_material')
        issue_changed = ({field for field in set(baseline) | set(current_material)
                          if baseline.get(field) != current_material.get(field)}
                         if baseline is not None and current_material is not None else changed)
        evidence_only = update.get('evidence_only') is True
        same_document = (context.get('previous_architecture_review', {}).get('document') or {}).get('sha256') == document.get('sha256')
        if evidence_only and (affected or not same_document):
            raise ValueError('仅补证据闭合须保持同一文档版本，不能伪装本轮设计变更。')
        if not evidence_only and (not affected or not affected <= issue_changed or targets and not affected.intersection(targets)):
            raise ValueError('问题闭合或重开必须绑定相对原问题基点实际变化且关联原问题的字段。')
        evidence = update.get('acceptance_evidence', [])
        if not evidence:
            raise ValueError('问题闭合或重开必须给出同版本决定性证据。')
        for anchor in evidence:
            if (not isinstance(anchor, dict) or anchor.get('document_id') != document.get('document_id')
                    or anchor.get('sha256') != document.get('sha256')
                    or type(anchor.get('char_start')) is not int or type(anchor.get('char_end')) is not int
                    or not 0 <= anchor['char_start'] < anchor['char_end'] <= document.get('total_chars', 0)
                    or not isinstance(anchor.get('explanation'), str) or not anchor['explanation'].strip()):
                raise ValueError('问题验收依据必须定位当前完整文档的有效原文范围。')
    resolved = {u['issue_id'] for u in updates if u['disposition'] == 'resolved'
                and by_id[u['issue_id']].get('status') != 'closed'}
    claim = vote.get('progress', {})
    if set(claim.get('resolved_objection_ids', [])) != resolved:
        raise ValueError('推进申报的闭合 ID 必须与逐项账本一致。')
    if vote.get('approve') is True and any(u['disposition'] != 'resolved' for u in updates):
        raise ValueError('批准不能跳过账本中尚未闭合的问题。')
    for index, text in enumerate(issues):
        if index in assigned:
            continue
        remedies = {( _normal(f.get('target')), _normal(f.get('verification_plan')))
                    for f in vote.get('fixes', []) if f.get('issue_index') == index
                    and f.get('target') and f.get('verification_plan')}
        for old in prior:
            previous_remedies = {(_normal(f.get('target')), _normal(f.get('verification_plan')))
                                 for f in old.get('fixes', []) if f.get('target') and f.get('verification_plan')}
            if _normal(text) == _normal(old['issue']) or remedies.intersection(previous_remedies):
                raise ValueError('同一问题或同一验收条件不能改述成新问题；须延续原 ID 或以新证据明确重开。')


def affected_fields(issue, available):
    """Only explicit field paths are inferred; prose targets remain unclassified."""
    targets = [fix.get('target', '') for fix in issue.get('fixes', [])]
    return sorted({field for field in available for target in targets
                   if target == field or target.startswith(field + '.') or target.startswith(field + '/')})


def evidence_covered(engine, run_id, document, vote, handoff_ids, context):
    """Each newly claimed closure/reopening must point into actual own reads."""
    from .implementation_documents import read_coverage
    prior = {row['id']: row for row in context.get('previous_architecture_review', {}).get('issue_ledger', [])}
    for update in vote.get('progress', {}).get('issue_updates', []):
        old = prior.get(update['issue_id'], {})
        transition = (update['disposition'] == 'reopened'
                      or update['disposition'] == 'resolved' and old.get('status') != 'closed')
        if not transition:
            continue
        eligible = handoff_ids
        if update.get('evidence_only') is True:
            previous_hids = set(context.get('previous_architecture_review', {}).get('review_handoff_ids', []))
            eligible = [hid for hid in handoff_ids if hid not in previous_hids]
            if not eligible:
                return False
        for anchor in update['acceptance_evidence']:
            covered = 0
            for section in document.get('sections', []):
                start, end = max(section['start'], anchor['char_start']), min(section['end'], anchor['char_end'])
                if start >= end:
                    continue
                required = [{'result': {'section_id': section['id'], 'char_start': start, 'char_end': end}}]
                if not any(read_coverage(engine, run_id, document, required, hid) for hid in eligible):
                    return False
                covered += end - start
            if covered != anchor['char_end'] - anchor['char_start']:
                return False
    return True


def reconcile(previous, current):
    """Carry unproven closures forward; matching text is only a conservative alias."""
    ledger = rows(previous) if previous else []
    by_id = {row['id']: row for row in ledger}
    current_rows = copy.deepcopy(current.get('objections', []))
    accepted, closures, reopenings = {}, [], []
    errors = []
    for vote in current.get('votes', []):
        if not isinstance(vote.get('progress', {}).get('issue_updates'), list):
            continue  # Legacy claims never manufacture closures from missing prose.
        role = vote['role']
        own = [copy.deepcopy(row) for row in ledger if row['role'] == role]
        context = {'issue_ledger_protocol': VERSION, 'previous_architecture_review': {
            'issue_ledger': own, 'changed_fields': sorted(current.get('_changed_fields', [])),
            'current_material': current.get('material'),
            'document': (previous or {}).get('document') or {}}}
        try:
            validate_vote(vote, context, current.get('document') or {})
        except ValueError as exc:
            errors.append({'role': role, 'reason': str(exc)})
            continue
        for update in vote['progress']['issue_updates']:
            row = by_id[update['issue_id']]
            previous_status = row['status']
            row.update(last_source_id=current.get('source_id'), last_update=copy.deepcopy(update))
            if update['disposition'] == 'resolved':
                row['status'] = 'closed'
                if previous_status != 'closed':
                    row['closure_evidence'] = copy.deepcopy(update['acceptance_evidence'])
                    row['closed_source_id'] = current.get('source_id')
                    row['closed_material'] = copy.deepcopy(current.get('material', {}))
                    closures.append({'id': row['id'], 'role': role, 'affected_fields': update['affected_fields'],
                                     'evidence_only': update.get('evidence_only') is True})
            else:
                row['status'] = 'open'
                if update['disposition'] == 'reopened':
                    row['reopen_evidence'] = copy.deepcopy(update['acceptance_evidence'])
                    reopenings.append(row['id'])
                for index in update['current_issue_indices']:
                    accepted[(role, index)] = row['id']
    for issue in current_rows:
        # Final review indices are supplied by the caller; segment issues may
        # only have a text match, and never remove an earlier unresolved entry.
        identity = accepted.get((issue['role'], issue.get('final_issue_index')))
        match = by_id.get(identity or issue['id'])
        if match is None:
            match = next((row for row in ledger if row['role'] == issue['role']
                          and _normal(row['issue']) == _normal(issue['issue'])), None)
        if match is not None:
            if match['status'] == 'closed':
                # No implicit reopening: retain the verified closed entry and
                # store this unbound dissent separately, with no progress credit.
                if identity:
                    continue
            else:
                match.setdefault('aliases', [])
                if issue['id'] != match['id'] and issue['id'] not in match['aliases']:
                    match['aliases'].append(issue['id'])
                match['last_source_id'] = current.get('source_id')
                continue
        fresh = initial([issue], current.get('source_id'))[0]
        fresh['origin_material'] = copy.deepcopy(current.get('material', {}))
        fresh['affected_fields'] = affected_fields(issue, current.get('material', {}))
        if fresh['id'] in by_id:
            from .engine import digest
            fresh['id'] = digest([fresh['id'], current.get('source_id'), 'unbound-dissent'])
        ledger.append(fresh)
        by_id[fresh['id']] = fresh
    return {'issue_ledger': ledger, 'verified_closures': closures, 'reopened_issue_ids': reopenings,
            'issue_ledger_errors': errors, 'issue_ledger_protocol': VERSION}
