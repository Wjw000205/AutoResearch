"""Save valid indexed changes, retain failures and omissions for the author."""
from __future__ import annotations

import copy


def resolve(base, args, validate):
    from .implementation_document_locator import revision_patch
    from .implementation_authoring_preservation import _patch, _tokens, SaveConflict
    accepted, failed = [], []
    entries = args['revisions']
    counts = {entry['index_id']: sum(r['index_id'] == entry['index_id'] for r in entries) for entry in entries}
    for entry in entries:
        try:
            if counts[entry['index_id']] != 1:
                raise SaveConflict('duplicate_index', '同一索引多次出现，请合并后重试该单元。')
            operation = revision_patch(base['spec'], [entry])[0]
            accepted.append((entry, operation))
        except SaveConflict as exc:
            failed.append({'index_id': entry['index_id'], 'code': exc.code, 'error': str(exc)})
    # Ambiguous parent/child replacements are retried together; independent units can save.
    overlaps = {i for i, (_, a) in enumerate(accepted) for j, (_, b) in enumerate(accepted)
                if i != j and (a['path'].startswith(b['path'] + '/') or b['path'].startswith(a['path'] + '/'))}
    for i in sorted(overlaps):
        failed.append({'index_id': accepted[i][0]['index_id'], 'code': 'overlapping_index',
                       'error': '父子单元同时修改；合并成一个索引单元后重试。'})
    accepted = [pair for i, pair in enumerate(accepted) if i not in overlaps]
    while accepted:
        candidate = _patch(base['spec'], [operation for _, operation in accepted])
        try:
            validate(candidate)
            return candidate, [entry['index_id'] for entry, _ in accepted], failed
        except SaveConflict as exc:
            problems = exc.details.get('errors') or [exc.details]
            paths = [tuple(str(token) for token in p['field_path']) for p in problems if p.get('field_path')]
            rejected = {i for i, (_, operation) in enumerate(accepted)
                        if any(tuple(_tokens(operation['path']))[:len(path)] == path
                               or path[:len(_tokens(operation['path']))] == tuple(_tokens(operation['path']))
                               for path in paths)}
            if not rejected:
                # A gate with no exact location may involve several fields. Keep
                # independently valid root groups, while never publishing a
                # candidate which violates the complete SPEC/source gates.
                groups = {}
                for i, (_, operation) in enumerate(accepted):
                    groups.setdefault(_tokens(operation['path'])[0], []).append(i)
                kept = []
                for group in groups.values():
                    attempt = _patch(base['spec'], [accepted[i][1] for i in kept + group])
                    try:
                        validate(attempt)
                        kept.extend(group)
                    except SaveConflict:
                        pass
                rejected = set(range(len(accepted))) - set(kept)
            if not rejected:
                raise  # No demonstrable safe partial write.
            for i in sorted(rejected):
                failed.append({'index_id': accepted[i][0]['index_id'], 'code': exc.code,
                    'error': str(exc), 'validation': copy.deepcopy(exc.details)})
            accepted = [pair for i, pair in enumerate(accepted) if i not in rejected]
    return copy.deepcopy(base['spec']), [], failed


def progress(context, base, args, prior, saved, failed):
    from .implementation_document_locator import catalog
    known = {entry['index_id'] for entry in catalog(base['spec'])}
    handled = {tuple(pair) for pair in prior.get('handled_conditions', [])}
    pending = {entry['index_id']: copy.deepcopy(entry) for entry in prior.get('failed_units', [])}
    for unit in saved:
        for key in list(pending):
            if key == unit or key.startswith(unit + '/'):
                del pending[key]
    pending.update({entry['index_id']: copy.deepcopy(entry) for entry in failed})
    saved_set = set(saved)
    for plan in args['revision_plan']:
        for condition in plan['conditions']:
            key = (plan['issue_id'], condition['index'])
            targets = set(condition['index_ids'])
            if ((condition['action'] == 'revise' and targets <= saved_set)
                    or (condition['action'] == 'already_satisfied' and targets <= known)):
                handled.add(key)
            else:
                handled.discard(key)
    from .implementation_condition_roles import review_keys
    reviewer_conditions = review_keys(context)
    missing = [{'issue_id': issue['issue_id'], 'condition_index': index, 'requirement': requirement}
               for issue in context['acceptance_issues'] for index, requirement in enumerate(issue['close_when'])
               if (issue['issue_id'], index) not in handled and (issue['issue_id'], index) not in reviewer_conditions]
    return {'saved_index_ids': saved, 'failed_units': list(pending.values()), 'missing_conditions': missing,
        'handled_conditions': [list(pair) for pair in sorted(handled)],
        'ready_for_self_check': not pending and not missing,
        'instruction': '成功项已保留；只重试失败项，再补写未处理条件。全部完成并自检后才提交下一轮审核。',
        'scientific_approval': False}


def status(engine, hid, context, record=None):
    """Project only actual save results, including an entirely rejected later call."""
    value = (record or {}).get('revision_progress', context.get('acceptance_author_saved_progress', {}))
    for receipt in engine.store.get('handoff_progress', hid, {}).get('tool_results', []):
        if receipt.get('call', {}).get('name') != 'implementation.edit_document':
            continue
        result = receipt.get('result', {})
        newer = result.get('revision_progress') or result.get('validation', {}).get('revision_progress')
        if newer:
            value = newer
    value = copy.deepcopy(value)
    from .implementation_condition_roles import review_pending
    review = review_pending(context)
    if review:
        keys = {(r['issue_id'], r['index']) for r in review}
        value['missing_conditions'] = [r for r in value.get('missing_conditions', [])
            if (r['issue_id'], r['condition_index']) not in keys]
        value['review_pending'] = review
        value['ready_for_self_check'] = not value.get('failed_units') and not value['missing_conditions']
    return value
