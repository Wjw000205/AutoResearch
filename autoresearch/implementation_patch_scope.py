"""Compare declared revision units with the actual immutable SPEC difference."""
from __future__ import annotations

from .implementation_review_routing import digest
from .implementation_authoring_preservation import _tokens, _index, SaveConflict

CONTEXT = 'acceptance_author_patch_scope'
GUIDANCE = ('修订范围以 acceptance_author_patch_scope.allowed_fields 和上一送审稿为准，'
    '每次保存后检查 acceptance_author_patch_manifest 的累计 actual_paths 与 forbidden_fields。'
    '中间草稿可以保存，但最终不得保留范围外的改动；恢复范围外字段时须恢复上一送审稿原值，'
    '并保留其余有效修改。不得仅从 patch_reports 删除声明来隐藏实际改动。'
    '若原义务确实无法在冻结范围内完成，明确报告具体来源冲突和所需授权，不得自行扩大范围。')


def view(context):
    from .implementation_acceptance import criterion_fields
    contract = context['acceptance_contract']
    issues = [{'issue_id': i['issue_id'], 'criterion_id': i['criterion_id'],
        'allowed_fields': sorted(criterion_fields(contract, i['criterion_id']))}
        for i in context['acceptance_issues']]
    return {'version': 1, 'contract_sha256': contract.get('sha256'), 'issues': issues,
        'allowed_fields': sorted({field for i in issues for field in i['allowed_fields']}),
        'scientific_approval': False}


def allowed_fields(context):
    """Same frozen dependency scope for producer, manifest and every validator."""
    value = view(context)
    marker = context.get(CONTEXT)
    if CONTEXT in context and (not isinstance(marker, dict)
            or type(marker.get('version')) is not int or marker != value):
        from .engine import ResearchBlocked
        raise ResearchBlocked('作者修订范围目录与冻结合同或原问题不一致。')
    return set(value['allowed_fields'])


def attach(context):
    value = dict(context)
    value[CONTEXT] = view(context)
    return value


def repair_metadata(context, previous, row, scope):
    return {'version': 1, 'scope': view(context), 'diagnostic': scope,
        'baseline_spec_hash': previous['spec_hash'],
        'baseline_document': {k: previous['document'][k] for k in ('document_id', 'sha256')},
        'draft_spec_hash': row['spec_hash'],
        'draft_document': {k: row['document'][k] for k in ('document_id', 'sha256')},
        'scientific_approval': False, 'reading_credit': False}


def validate_repair(context, state, source, pending):
    """Recompute the frozen diagnosis from original draft/answer and baseline."""
    repairs = [p['submission_scope_repair'] for p in pending if 'submission_scope_repair' in p]
    if not repairs:
        return
    from .implementation_authoring import COLLECTION
    from .engine import ResearchBlocked
    row = source[COLLECTION]['row']
    previous = next(r for r in state['specifications'] if r['index'] == row['index'] - 1)
    actual = diagnostic(previous['spec'], row['spec'], source['decisions']['result']['patch_reports'], allowed_fields(context))
    if (len(repairs) != 1 or type(repairs[0].get('version')) is not int or actual['valid']
            or repairs[0] != repair_metadata(context, previous, row, actual)):
        raise ResearchBlocked('作者范围补正诊断与原始提交、累计修改或冻结基线不一致。')


def pointer(tokens):
    return ''.join('/' + str(token).replace('~', '~0').replace('/', '~1') for token in tokens)


def changed_paths(before, after, prefix=()):
    if digest(before) == digest(after):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            path = (*prefix, key)
            result.extend(changed_paths(before[key], after[key], path)
                          if key in before and key in after else [path])
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = []
        for index in range(max(len(before), len(after))):
            path = (*prefix, str(index))
            result.extend(changed_paths(before[index], after[index], path)
                          if index < min(len(before), len(after)) else [path])
        return result
    return [prefix]


def _exists(spec, tokens):
    value = spec
    try:
        for token in tokens:
            if isinstance(value, list):
                value = value[_index(token, len(value))]
            elif isinstance(value, dict):
                value = value[token]
            else:
                return False
    except (KeyError, SaveConflict):
        return False
    return True


def diagnostic(before, after, reports, allowed):
    """Accept chapter names or exact paths, without widening their scope.

    Every declared unit must really change and every changed leaf must be covered.
    Parent declarations cover descendants; siblings cannot cover each other.
    Added/removed units are located in either immutable version.
    Numeric locator paths may omit the leading slash. Resolve literal chapter
    names first, and apply all ordinary pointer/existence/scope checks unchanged.
    """
    actual = changed_paths(before, after)
    claims, invalid = [], []
    for report in reports:
        for field in report['changed_fields']:
            try:
                if field.startswith('/'):
                    tokens = tuple(_tokens(field))
                elif field in before or field in after or '/' not in field:
                    tokens = (field,)
                else:
                    tokens = tuple(_tokens('/' + field))
                if not (_exists(before, tokens) or _exists(after, tokens)):
                    raise ValueError('missing revision unit')
            except (SaveConflict, ValueError):
                invalid.append(field)
                continue
            claims.append((field, tokens))
    def covers(parent, child):
        return child[:len(parent)] == parent
    unreported = [pointer(path) for path in actual if not any(covers(tokens, path) for _, tokens in claims)]
    unchanged = [field for field, tokens in claims if not any(covers(tokens, path) for path in actual)]
    forbidden = sorted({path[0] for path in actual if path[0] not in allowed}
                       | {tokens[0] for _, tokens in claims if tokens[0] not in allowed})
    return {'actual_fields': sorted({path[0] for path in actual}),
        'actual_paths': sorted(pointer(path) for path in actual),
        'invalid_paths': sorted(set(invalid)), 'unchanged_claims': sorted(set(unchanged)),
        'unreported_paths': sorted(unreported), 'forbidden_fields': forbidden,
        'valid': not (invalid or unchanged or unreported or forbidden)}
