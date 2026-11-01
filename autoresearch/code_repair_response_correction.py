"""Same-role corrections of an unsealed clarification's indices and retained fixes.

No vote is rewritten by the host. The original answer and request remain in the
handoff journal; the actual successor must preserve every scientific judgment,
source reference, resolution and version outside the diagnosed fields.
"""
from __future__ import annotations

import copy

FIELD = 'code_repair_response_problems'
DIAGNOSTIC = 'implementation_code_repair_response_required'


def problems(value, context, role):
    from . import code_repair_contract as repair
    from .code_repair_clarification import FIELD as CLARIFICATION
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
    expected = {key for key, row in responses.items() if row['resolution'] != 'withdrawn'}
    if (len(findings) != len(value.get('issues', [])) or len(findings) != len(expected)
            or {r.get('origin_id') for r in findings} != expected
            or {r.get('issue_index') for r in findings} != set(range(len(findings)))
            or any(r.get('regression') is not None or r.get('requirement_id') !=
                   known[r['origin_id']]['requirement_id'] for r in findings)
            or {f.get('issue_index') for f in value.get('fixes', [])} != set(range(len(findings)))):
        return []  # New identities, requirements and missing issues are not formatting.
    refs = value.get('code_evidence', [])
    found = []
    for field, rows, identity_field in (
            ('clarification_responses', replies, 'issue_id'),
            ('repair_findings', findings, 'origin_id')):
        for index, row in enumerate(rows):
            identity = row[identity_field]
            requirement = known[identity]['requirement_id']
            eligible = [i for i, ref in enumerate(refs) if ref['requirement_id'] == requirement]
            indices = row.get('evidence_indices', [])
            if indices and all(type(i) is int and i in eligible for i in indices):
                continue
            if not eligible:
                return []  # A missing evidence item needs source work, not index repair.
            found.append({'kind': 'evidence_indices', 'field': field, 'index': index,
                'issue_id': identity, 'requirement_id': requirement,
                'original_indices': copy.deepcopy(indices), 'eligible_indices': eligible})
    for row in findings:
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
            or problems(entry['candidate'], context, role) != entry[FIELD]):
        raise ResearchBlocked('代码意见补正的原付费答复、冻结范围或诊断发生变化。')


def validate_correction(value, entry):
    from .engine import ResearchBlocked, digest
    original = entry['candidate']
    if digest(value) == digest(original):
        return  # Idempotent resume before the successor has been dispatched.
    expected = copy.deepcopy(original)
    for row in entry[FIELD]:
        if row['kind'] == 'evidence_indices':
            try:
                indices = value[row['field']][row['index']]['evidence_indices']
            except (KeyError, IndexError, TypeError) as exc:
                raise ResearchBlocked('代码意见补正缺少原问题的证据下标。') from exc
            if (not isinstance(indices, list) or not indices
                    or any(type(i) is not int or i not in row['eligible_indices'] for i in indices)
                    or len(set(indices)) != len(indices)):
                raise ResearchBlocked('代码意见补正只能选择本次已核验的同验收项证据下标。')
            expected[row['field']][row['index']]['evidence_indices'] = copy.deepcopy(indices)
        else:
            fixes = []
            inserted = False
            for fix in expected['fixes']:
                if fix['issue_index'] != row['issue_index']:
                    fixes.append(fix)
                elif not inserted:
                    fixes.extend(copy.deepcopy(row['expected_fixes']))
                    inserted = True
            expected['fixes'] = fixes
    if digest(value) != digest(expected):
        raise ResearchBlocked('代码意见补正只能修正已诊断下标并恢复保留项的原标准；不得改变结论、问题、澄清分类或来源。')


def diagnostic(result, error, attempt, rows):
    from .engine import digest
    return {'host_diagnostic': DIAGNOSTIC, 'error': error,
        'alignment_read_correction': {'attempt': attempt, 'max_attempts': None},
        'code_repair_response_correction': {'candidate_sha256': digest(result),
            'original_candidate': copy.deepcopy(result), 'problems': copy.deepcopy(rows),
            'instruction': '原付费答复及实际读取已保存，尚未封存决定。仅补正 problems 指定字段，'
                '返回原 Schema 的完整答复，不重新评审、不重读已核验正文。evidence_indices 从本次 '
                'code_evidence 的 eligible_indices 选择确实支持原问题的下标，不能混入其他验收项；'
                '不要改 code_evidence 的 requirement_id 来规避校验。retained_fixes 表示你选择保留'
                '原问题，必须逐字复制 expected_fixes，不能只保留大意。其他所有字段、顺序、approve、'
                'issues、corrected/withdrawn 分类与理由、已纠正要求、来源范围和哈希均原样保留。'
                '不得新增问题、撤回异议或提高标准。补正不授权改代码，仍须原证据校验和独立修改审计。'}}
