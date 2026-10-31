"""Auditable progress for unlimited, checkpointed architecture handoffs."""
from __future__ import annotations

import copy
import re

from .storage import now
from . import architecture_issue_ledger as issue_ledger

VERSION = 1
COLLECTION = 'architecture_progress'

REVIEW = '''
对 previous_architecture_review 中你自己的上版必改项逐一复核。本轮一次提交完整修改意见，
明确区分已闭合、部分推进和仍阻塞的事项；不因仍有其他问题就否认已经完成的实质修正。
progress.resolved_objection_ids 只引用该输入中的原意见 ID，changed_fields 只引用实际变化字段。
只有明确的修改真正解决原阻塞才 made_progress=true；新增标题、换措辞、扩大局限清单、
删除比较或把异议从本轮列表中省略都不算推进。批准与推进分别判断，不为推动流程虚构批准。
已核验并且未受本次修改影响的条款继续保留；重开问题须说明新发现的具体冲突或原文依据，
不得反复推翻同一合理实现选择。缺正文时定向补读同版本决定性范围，不要求重写已经明确的合同。
'''
STRUCTURED_REVIEW = REVIEW + issue_ledger.INSTRUCTION

AUTHOR = '''
先依据 revision_agenda 制定本轮最小修改范围，合并所有角色的具体必改方案，明确交叉依赖与冲突取舍。
优先闭合阻塞项，保留不受修改影响且已明确的公式、配置和验收条款，不从头生成整份方案。
逐项交付实际改动和可核验的通过条件；不得仅换措辞、加免责声明或重申旧局限。
对需要补证据的意见定向恢复原文，对需要明确实现的意见选择一个完整且兼容的定义。
如果上一条修改路径已经失败，明确改变处理方法并解决原失败原因，不重复同一答复。
评议没有总轮数上限；连续没有经独立评审确认的推进才暂停，科学和执行门禁仍逐项核验。
'''


def enabled(engine, run_id):
    return engine.store.get('runs', run_id)['config'].get('architecture_progress_protocol') == VERSION


def _normal(value):
    if isinstance(value, str):
        return re.sub(r'\s+', ' ', value).strip()
    if isinstance(value, dict):
        return {k: _normal(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_normal(v) for v in value]
    return value


def fields(candidate, scope):
    if scope == 'definitions':
        return {**{'definitions.' + d['issue_id']: {k: d[k] for k in ('status', 'definition_md')}
                   for d in candidate['definitions']},
                'compatible_with_approved_scope': candidate['compatible_with_approved_scope'],
                'scope_issues': candidate['scope_issues']}
    return {k: v for k, v in candidate.items() if k not in {'title', 'limitations', 'change_summary'}}


def material(candidate, scope):
    from .engine import digest
    return {k: digest(_normal(v)) for k, v in fields(candidate, scope).items()}


def objections(source, scope):
    if scope == 'architecture':
        from .architecture_revisions import _objections
        return _objections(source)
    from .engine import digest
    rows = {}
    for final, vote in [(True, v) for v in source.get('votes', [])] + [(False, v) for v in source.get('unit_reviews', [])]:
        updates = vote.get('progress', {}).get('issue_updates') if final else None
        stable_ids = {index: u['issue_id'] for u in updates or []
                      if u.get('disposition') in {'carried_forward', 'reopened'}
                      for index in u.get('current_issue_indices', [])}
        for index, text in enumerate(vote.get('required_changes', [])):
            identity = (vote['role'], _normal(text))
            row = rows.setdefault(identity, {'id': stable_ids.get(index, digest([source['id'], *identity])),
                                            'role': vote['role'], 'issue': text, 'fixes': []})
            if final and updates is not None:
                row['final_issue_index'] = index
            for fix in vote.get('fixes', []):
                if fix['issue_index'] == index and fix not in row['fixes']:
                    row['fixes'].append(copy.deepcopy(fix))
    return list(rows.values())


def history(engine, run_id, scope):
    rows = [r for r in engine.store.list(COLLECTION, run_id)
            if r['scope'] == scope and r['cycle'] == engine.cycle(run_id)]
    return sorted(rows, key=lambda r: r['number'])


def context(engine, run_id, scope, candidate, *, role=None, structured=False):
    prior = history(engine, run_id, scope)
    last = prior[-1] if prior else None
    current = material(candidate, scope)
    changed = [k for k in sorted(set(current) | set(last['material'] if last else {}))
               if last and current.get(k) != last['material'].get(k)]
    ledger_source = issue_ledger.history_snapshot(prior) if structured else last
    ledger = [i for i in issue_ledger.rows(ledger_source) if role is None or i['role'] == role] if ledger_source else []
    # Legacy frozen assignments must reconstruct byte-for-byte old context.
    # Only the explicit new protocol receives derived ledger metadata.
    issues = ([copy.deepcopy(i) for i in ledger if i.get('status') != 'closed'] if structured else
              [copy.deepcopy(i) for i in last['objections'] if role is None or i['role'] == role] if last else [])
    result = {'architecture_progress_protocol': VERSION,
            'previous_architecture_review': {'source_id': last['source_id'] if last else None,
                'document': copy.deepcopy(last['document']) if last else None,
                'objections': issues, 'changed_fields': changed,
                'consecutive_no_progress': last['consecutive_no_progress'] if last else 0},
            'revision_agenda': issues}
    if structured:
        result['issue_ledger_protocol'] = issue_ledger.VERSION
        result['previous_architecture_review']['issue_ledger'] = ledger
        result['previous_architecture_review']['current_material'] = current
        result['previous_architecture_review']['review_handoff_ids'] = [
            row['handoff_id'] for row in (last or {}).get('review_handoffs', [])
            if role is None or row['role'] == role]
    return result


def schema(base, *, structured=False):
    from .engine import STRS, obj
    result = copy.deepcopy(base)
    result['properties']['progress'] = obj({'made_progress': {'type': 'boolean'},
        'resolved_objection_ids': copy.deepcopy(STRS), 'changed_fields': copy.deepcopy(STRS),
        'rationale': {'type': 'string', 'minLength': 1, 'maxLength': 1600}})
    result['required'].append('progress')
    if structured:
        result['properties']['progress'] = issue_ledger.extend_progress(result['properties']['progress'])
    return result


def validate_vote(vote, context, document):
    return issue_ledger.validate_vote(vote, context, document)


def assess(prior, current, *, window, author_role):
    """A reviewer must ground a closure in actual changed fields, never prose counts."""
    from .engine import digest
    approved = set(current['approved_roles'])
    state_hash = digest([current['material'], sorted(approved)])
    result = {'state_hash': state_hash, 'consecutive_no_progress': 0, 'stalled': False,
              'supported_progress': False,
              'progress_roles': [], 'resolved_objection_ids': [], 'changed_fields': [],
              'repeated_state': False, 'stagnation_window': window}
    if not prior:
        return {**result, 'reason': '首次评议建立比较基点，不追认历史推进。'}
    last = prior[-1]
    changed = {k for k in set(current['material']) | set(last['material'])
               if current['material'].get(k) != last['material'].get(k)}
    roles, closed = set(), set()
    # Missing old prose plus an arbitrary content hash change is not closure.
    # Reconcile again when used as a pure function, so callers cannot inject a
    # claimed verified_closures list without actual structured review evidence.
    reconciled = issue_ledger.reconcile(issue_ledger.history_snapshot(prior), {**current, '_changed_fields': changed})
    claims = {v['role']: v.get('progress') or {} for v in current['votes']}
    for closure in reconciled['verified_closures']:
        claim = claims.get(closure['role'], {})
        if claim.get('made_progress') is True and (closure['affected_fields'] or closure['evidence_only']):
            roles.add(closure['role'])
            closed.add(closure['id'])
    # Approval oscillation A/B/A must not repeatedly reset the same clock.
    previously_approved = {role for row in prior for role in row['approved_roles']}
    new_approvals = approved - previously_approved
    roles.update(new_approvals)
    supported = bool(roles - {author_role})
    repeated = any(r['state_hash'] == state_hash for r in prior)
    if (repeated and current['status'] != 'approved'
            and not any(c['role'] != author_role for c in reconciled['verified_closures'])):
        supported = False
    count = 0 if supported else last['consecutive_no_progress'] + 1
    # A/B/A and unchanged rejected content stop without repeating paid work.
    stalled = not supported and (repeated or count >= window) and current['status'] != 'approved'
    reason = ('独立评审确认了具体修改的推进。' if supported else
              '回到已否决的相同实质内容与批准状态，暂停重复评议。' if repeated else
              f'连续 {count} 轮未取得独立评审确认的实质推进。')
    return {**result, **reconciled, 'consecutive_no_progress': count, 'stalled': stalled,
            'supported_progress': supported,
            'repeated_state': repeated, 'progress_roles': sorted(roles),
            'resolved_objection_ids': sorted(closed), 'changed_fields': sorted(changed), 'reason': reason}


def record(engine, run_id, scope, source, candidate):
    """Persist a separate derived receipt; historical candidates and votes stay immutable."""
    from .engine import ResearchBlocked, digest
    key = f'{run_id}:architecture-progress:{scope}:{engine.cycle(run_id)}:{source["id"]}'
    existing = engine.store.get(COLLECTION, key)
    if existing:
        if existing['source_sha256'] != digest(source):
            raise ResearchBlocked('架构推进记录的原评议发生变化，不能复用停滞判断。')
        return existing
    raw_source = source
    if scope == 'architecture' and source.get('review_recovery_protocol') == 1:
        from .architecture_review_recovery import effective_source
        source = effective_source(source)
    votes = source.get('reviews', source.get('votes', []))
    issue_field = 'issues' if scope == 'architecture' else 'required_changes'
    approved = {v['role'] for v in votes if v.get('approve') is True and not v.get(issue_field)}
    approved -= {v['role'] for v in source.get('unit_reviews', [])
                 if v.get('approve') is not True or v.get(issue_field)}
    previous = history(engine, run_id, scope)
    current = {'id': key, 'run_id': run_id, 'cycle': engine.cycle(run_id), 'scope': scope,
        'number': len(previous) + 1, 'source_id': source['id'], 'source_sha256': digest(raw_source),
        'document': copy.deepcopy(source.get('document')), 'material': material(candidate, scope),
        'review_handoffs': [{'role': v['role'], 'handoff_id': v['handoff_id']}
                            for v in [*votes, *source.get('unit_reviews', [])] if v.get('handoff_id')],
        'status': source['status'], 'votes': copy.deepcopy(votes), 'approved_roles': sorted(approved),
        'objections': objections(source, scope), 'derived': True, 'scientific_approval': False,
        'created_at': now()}
    if not previous:
        current.update(issue_ledger.reconcile(None, current))
    window = engine.store.get('runs', run_id)['config'].get('architecture_stagnation_rounds', 3)
    current.update(assess(previous, current, window=window,
                          author_role='engineer' if scope == 'architecture' else 'director'))
    engine.store.put(COLLECTION, key, current)
    engine.store.event(run_id, 'architecture.progress_recorded', current['reason'],
        {k: current[k] for k in ('scope', 'source_id', 'consecutive_no_progress', 'stalled', 'progress_roles')})
    return current


def view(engine, run_id, scope):
    rows = history(engine, run_id, scope)
    last = rows[-1] if rows else {}
    window = engine.store.get('runs', run_id)['config'].get('architecture_stagnation_rounds', 3)
    ledger = issue_ledger.rows(issue_ledger.history_snapshot(rows)) if rows else []
    return {'mode': 'until_stalled', 'round_limit': None, 'completed_rounds': len(rows),
            'issue_ledger_protocol': issue_ledger.VERSION, 'issue_ledger': ledger,
            'open_issue_count': sum(i.get('status') != 'closed' for i in ledger),
            'closed_issue_count': sum(i.get('status') == 'closed' for i in ledger),
            'consecutive_no_progress': last.get('consecutive_no_progress', 0),
            'stagnation_window': window,
            'stalled': bool(last and last.get('status') != 'approved'
                           and (last.get('repeated_state') and not last.get('supported_progress')
                                or last['consecutive_no_progress'] >= window)),
            'reason': last.get('reason'), 'progress_roles': last.get('progress_roles', []),
            'repeated_state': last.get('repeated_state', False),
            'changed_fields': last.get('changed_fields', []), 'scientific_approval': False}


def check(engine, run_id, scope):
    from .engine import ResearchBlocked
    progress = view(engine, run_id, scope)
    if progress['stalled']:
        raise ResearchBlocked(progress['reason'])
