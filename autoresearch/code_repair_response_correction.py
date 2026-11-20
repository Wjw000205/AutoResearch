"""Same-role completion of unsealed code-review deliveries.

No vote is rewritten by the host. Original answers and requests remain in the
handoff journal; the original reviewer may revise an unsealed draft. Normal
source, scope and approval validation applies to the complete successor.
"""
from __future__ import annotations

import copy

FIELD = 'code_repair_response_problems'
DIAGNOSTIC = 'implementation_code_repair_response_required'
SAME_REQUIREMENT_ERROR = '代码修改依据待核实：保留、纠正或撤回原问题都必须引用本次实际读取的同一验收项依据。'
SUPPLEMENT_SCOPE_ERROR = '只能在原有源码证据之后追加本次缺失验收项的实际读取依据。'


def request_mode(progress):
    """Mechanical corrections use saved evidence; only actual gaps need reads."""
    history = progress.get('alignment_candidate_history', [])
    if not history or not history[-1].get(FIELD):
        return None
    entry = history[-1]
    evidenced = {row['requirement_id'] for row in entry['candidate'].get('code_evidence', [])}
    for row in entry[FIELD]:
        if row['kind'] == 'missing_evidence_indices':
            return 'evidence_completion'
        if row['kind'] == 'review_completion':
            requirements = {item['requirement_id'] for item in row['available_original_issues']}
            if not requirements <= evidenced:
                return 'evidence_completion'
    return 'delivery_only'


def fixes_problem(value, context):
    """Initial reviews and rereviews share the same issue/remedy contract."""
    if context.get('whole_code_review'):
        # This versioned workflow classifies dependencies separately from
        # executable code changes. Its owning validator checks the complete
        # classification and fix mapping before accepting the vote. Applying
        # the legacy all-issues-have-fixes gate here contradicts that contract.
        return None
    if (context.get('implementation_alignment_protocol') != 1 or value.get('approve') is not False
            or not value.get('issues') or not isinstance(value.get('fixes'), list)):
        return None
    indices = list(range(len(value['issues'])))
    invalid = [{'fix_index': n, 'original_issue_index': row.get('issue_index')}
        for n, row in enumerate(value['fixes'])
        if type(row.get('issue_index')) is not int or row['issue_index'] not in indices]
    if invalid:
        return {'kind': 'fix_indices', 'invalid_indices': invalid, 'eligible_indices': indices}
    missing = sorted(set(indices) - {r['issue_index'] for r in value['fixes']})
    return {'kind': 'missing_fixes', 'missing_issue_indices': missing} if missing else None


def validate_delivery(value, context, role):
    """Run after source validation, before sealing any review decision."""
    if fixes_problem(value, context):
        fail('否决意见的修复建议存在缺项或编号错误。', value, context, role)


def _original_scope(row, known, legacy):
    origin = row.get('origin_id')
    if not legacy and origin is None and row.get('disposition') != 'repair_code' and row.get('regression') is None:
        return True
    return (origin in known and row.get('regression') is None
            and row.get('requirement_id') == known[origin]['requirement_id'])


def binding_indices_problem(value, context, role, *, legacy=False):
    from . import code_repair_contract as repair
    policy = context.get(repair.FIELD) or {}
    if policy.get('version') != repair.VERSION or policy.get('spec_hash') != context.get('spec_hash'):
        return None
    known = {i['issue_id']: i for i in policy['issues'] if i['role'] == role}
    rows = value.get('repair_findings', [])
    if any(not _original_scope(r, known, legacy) for r in rows):
        return None
    indices = [r.get('issue_index') for r in rows]
    eligible = list(range(len(value.get('issues', []))))
    invalid = [{'finding_index': n, 'original_issue_index': i} for n, i in enumerate(indices)
        if type(i) is not int or i not in eligible or indices.count(i) > 1]
    return {'kind': 'binding_indices', 'invalid_indices': invalid, 'eligible_indices': eligible} if invalid and eligible else None


def completion_problem(value, context, role, *, legacy=False):
    """Describe missing bindings without inventing their scientific content.

    Existing objections and evidence stay exact. The original reviewer must
    supply the absent entries and real evidence; normal validation still runs.
    """
    from . import code_repair_contract as repair
    from .code_repair_clarification import FIELD as CLARIFICATION
    policy = context.get(repair.FIELD) or {}
    if (policy.get('version') != repair.VERSION or policy.get('spec_hash') != context.get('spec_hash')
            or value.get('approve') is not False or not value.get('issues')):
        return None
    known = {i['issue_id']: i for i in policy['issues'] if i['role'] == role}
    if not known or {f.get('issue_index') for f in value.get('fixes', [])} != set(range(len(value['issues']))):
        return None
    findings = value.get('repair_findings', [])
    indices = [r.get('issue_index') for r in findings]
    origins = [r.get('origin_id') for r in findings if legacy or r.get('origin_id') is not None]
    # Foreign identities and changed requirements are not incomplete delivery.
    if (len(indices) != len(set(indices)) or any(type(i) is not int or not 0 <= i < len(value['issues']) for i in indices)
            or any(not _original_scope(r, known, legacy) for r in findings)):
        return None
    clarification = bool(context.get(CLARIFICATION))
    replies = value.get('clarification_responses', []) if clarification else []
    reply_ids = [r.get('issue_id') for r in replies]
    if (len(reply_ids) != len(set(reply_ids)) or any(identity not in known for identity in reply_ids)
            or any(r.get('resolution') not in {'retained', 'corrected', 'withdrawn'} or not r.get('reason', '').strip()
                   for r in replies)):
        return None
    missing_indices = sorted(set(range(len(value['issues']))) - set(indices))
    missing_responses = sorted(set(known) - set(reply_ids)) if clarification else []
    if not missing_indices and not missing_responses:
        return None
    return {'kind': 'review_completion', 'missing_issue_indices': missing_indices,
        'available_original_issues': [{k: copy.deepcopy(row[k]) for k in ('issue_id', 'requirement_id', 'issue', 'fixes')}
            for identity, row in known.items() if identity not in origins],
        'missing_response_issue_ids': missing_responses,
        'requirement_ids': sorted({row['requirement_id'] for row in known.values()}),
        'append_fields': ['repair_findings', 'code_evidence', 'alignment_findings']
            + (['clarification_responses'] if clarification else [])}


def problems(value, context, role, *, legacy=False):
    from . import code_repair_contract as repair
    from .code_repair_clarification import FIELD as CLARIFICATION
    fixes = fixes_problem(value, context)
    if fixes:
        return [fixes]
    bindings = binding_indices_problem(value, context, role, legacy=legacy)
    if bindings:
        return [bindings]
    completion = completion_problem(value, context, role, legacy=legacy)
    if completion:
        return [completion]
    if not context.get(CLARIFICATION):
        return []
    policy = context.get(repair.FIELD) or {}
    if policy.get('version') != repair.VERSION or policy.get('spec_hash') != context.get('spec_hash'):
        return []
    known = {i['issue_id']: i for i in policy['issues'] if i['role'] == role}
    replies = value.get('clarification_responses', [])
    responses = {r['issue_id']: r for r in replies}
    if len(replies) != len(known) or set(responses) != set(known):
        return []
    if any(r.get('resolution') not in {'retained', 'corrected', 'withdrawn'}
           or not r.get('reason', '').strip() for r in replies):
        return []
    findings = value.get('repair_findings', [])
    original_findings = [r for r in findings if legacy or r.get('origin_id') is not None]
    expected = {key for key, row in responses.items() if row['resolution'] != 'withdrawn'}
    if (len(findings) != len(value.get('issues', []))
            or {r.get('origin_id') for r in original_findings} != expected
            or {r.get('issue_index') for r in findings} != set(range(len(findings)))
            or any(not _original_scope(r, known, legacy) for r in findings)
            or {f.get('issue_index') for f in value.get('fixes', [])} != set(range(len(findings)))):
        return []  # New identities, requirements and missing issues are not formatting.
    refs = value.get('code_evidence', [])
    found = []
    for field, rows, identity_field in (
            ('clarification_responses', replies, 'issue_id'),
            ('repair_findings', findings, 'origin_id')):
        for index, row in enumerate(rows):
            identity = row[identity_field]
            if not legacy and identity is None:
                continue
            requirement = known[identity]['requirement_id']
            from .review_response_policy import evidence_required
            related = [row] if field == 'repair_findings' else [f for f in findings if f.get('origin_id') == identity]
            required = legacy or any(evidence_required(f) for f in related)
            eligible = [i for i, ref in enumerate(refs)
                        if not required or ref['requirement_id'] == requirement]
            indices = row.get('evidence_indices', [])
            if (indices or not required) and all(type(i) is int and i in eligible for i in indices):
                continue
            found.append({'kind': 'evidence_indices' if eligible else 'missing_evidence_indices',
                'field': field, 'index': index,
                'issue_id': identity, 'requirement_id': requirement,
                'original_indices': copy.deepcopy(indices), 'eligible_indices': eligible})
    for row in findings if legacy else []:
        identity, index = row['origin_id'], row['issue_index']
        if responses[identity]['resolution'] != 'retained':
            continue
        original = known[identity]['fixes']
        actual = [f for f in value['fixes'] if f['issue_index'] == index]
        if sorted((f['change'], f['verification']) for f in actual) == sorted(
                (f['change'], f['verification']) for f in original):
            continue
        expected_fixes = [{**copy.deepcopy(f), 'issue_index': index,
            'location': actual[n]['location'] if len(actual) == len(original) else f['location']}
            for n, f in enumerate(original)]
        found.append({'kind': 'retained_fixes', 'issue_id': identity,
            'issue_index': index, 'expected_fixes': expected_fixes})
    return found


def fail(message, value, context, role):
    from .implementation_failures import ImplementationCodeBlocked
    error = ImplementationCodeBlocked('代码修改依据待核实：' + message, kind='codex_repair_unverified')
    rows = problems(value, context, role)
    if rows:
        setattr(error, FIELD, rows)
    raise error


def verify_archive(engine, run_id, role, purpose, key, context, entry):
    from .engine import ResearchBlocked, digest
    from .implementation_alignment_recovery import _verify_format_source
    _verify_format_source(entry, role, purpose, problem_field=FIELD)
    if (not entry.get('source_requests')
            or digest(entry['source_requests']) != entry.get('code_repair_requests_sha256')
            or digest(context) != entry.get('code_repair_context_sha256')
            or problems(entry['candidate'], context, role,
                        legacy=entry.get('review_response_policy_version', 1) < 2) != entry[FIELD]):
        raise ResearchBlocked('代码意见补正的原付费答复、冻结范围或诊断发生变化。')


def validate_correction(value, entry):
    from .review_response_policy import validate_binding
    validate_binding(value, entry['candidate'])


def diagnostic(result, error, attempt, rows):
    from .engine import digest
    return {'host_diagnostic': DIAGNOSTIC, 'error': error,
        'alignment_read_correction': {'attempt': attempt, 'max_attempts': None},
        'code_repair_response_correction': {'candidate_sha256': digest(result),
            'original_candidate': copy.deepcopy(result), 'problems': copy.deepcopy(rows),
            'instruction': '原付费答复及读取已留档，当前意见尚未封存。请解决 problems 中的缺项，'
                '返回完整评审；可以重排、改写、补充或替换证据，并根据实际依据纠正或撤回原意见。'
                'evidence_indices 按新答复的 code_evidence 编号。待复核、缺执行回执和文档依赖'
                '可以直接说明缺口；具体代码修改仍须同验收项的实际依据。'
                '不要求逐字复制、固定顺序或只在末尾追加。版本、引用、问题范围及执行准入仍独立核验。'}}
