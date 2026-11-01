"""Explicit author responses and original-reviewer confirmations.

This extends the existing objection ledger for new frozen assignments only.
Saving a response is never closure, scientific approval, or execution authority.
"""
from __future__ import annotations

import copy
import json
import re

from .implementation_review_routing import digest
from .architecture_validation import operation
from .engine import ResearchBlocked

VERSION = 1
PROTOCOL = 'implementation_issue_response_protocol'
CONTRACT = 'implementation_issue_response_contract'
RECEIPT = 'issue_response_receipt'
META = {'role', 'handoff_id', 'unit_number', 'ranges', 'review_sequence', 'reading_proof'}
AUTHOR_GUIDANCE = '''
输入有 implementation_issue_response_contract 时，最终提交在真实 submission 指针上增加 issue_responses。
issue_responses 必须与 implementation_issue_response_contract.actions 严格一一对应：只回应 actions，不遗漏、不重复、不追加。
每项直接使用对应 action 的完整 (objection_id, origin_ref_sha256) 二元组；相同 objection_id 在不同轮次可有不同来源哈希，不能沿用历史回应或 obligations 中的旧 origin_ref_sha256。
implementation_issue_response_contract.obligations 是保留原始来源的审计背景；其中未列入 actions 的历史来源不属于本轮回复清单。
revision_or_rebuttal=revision 时列实际改变的 changed_fields；
changed_fields 可用顶层字段名或非根 JSON Pointer（如 /acceptance_checks/0/verification，~0/~1 转义）；每个路径的值必须相对本轮紧邻版本确实改变。
若认为原条款已经正确，用 rebuttal，changed_fields=[]，说明不修改的具体理由和决定性文档证据，不为了回应制造修改。
evidence 使用真实已读取文档的 document_id/SHA/section_id/全文绝对 start/end，至少一项指向本次实际保存的草稿。
同一SPEC的反驳可以原样保存归档；这不是已修复或科学进展。不得把 review_incomplete 或 external_dependency 改写成文档修复。
所有回应仅交原提出异议的角色独立复核；工程师不能关闭问题、声明评审接受，或代替三方终审。
'''
REVIEW_GUIDANCE = '''
implementation_issue_response_contract.requirements 是本次须逐项复核的原异议来源；保留每个 origin_ref_sha256。
逐项返回 issue_confirmations，核对工程师的 revision 或 rebuttal 是否足以说服你；不得因工程师声明已修复而接受。
issue_confirmations 只列 requirements 中本轮要求的来源；requirements=[] 时必须返回 []。背景或已延后的异议不能额外放进本轮确认清单。
outcome=resolved 仅表示你作为原提出者已实际核对决定性证据，原问题全部满足验证条件；给出具体理由和真实已读 evidence。
部分改善、外部依赖未恢复、仍缺决定性原文或待验证时用 unresolved，延续原 objection_id 及准确路由/恢复条件。
同文案不同来源分别核对；别的角色的同意、保存、读取覆盖或最终approve都不能自动关闭未逐项确认的问题。
progress.resolved_objection_ids 只可列本次全部来源均确认 resolved 的原ID；背景来源留待最终统合。
有任何本次原异议未解决时不能 approve。三方终审和科学、资源、预算门禁继续独立有效。
'''


class ChangedFieldsBlocked(ResearchBlocked):
    """Only changed-field declarations failed after all identity/evidence checks."""

    def __init__(self, problems):
        super().__init__('实现异议逐项闭环：文档修复必须指向实际变化字段；反驳不能伪装成已修复。')
        self.problems = copy.deepcopy(problems)


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现异议逐项闭环：' + message)


_MISSING = object()
_INVALID_PATH = object()


def _field_parts(path):
    """Accept legacy field names and strict non-root JSON Pointers."""
    if not isinstance(path, str) or not path:
        return None
    if not path.startswith('/'):
        return [path]
    tokens = path[1:].split('/')
    if not tokens[0] or any(re.search(r'~(?![01])', token) for token in tokens):
        return None
    return [token.replace('~1', '/').replace('~0', '~') for token in tokens]


def _path_value(spec, parts):
    value = spec
    for index, part in enumerate(parts):
        absent = _MISSING if index == len(parts) - 1 else _INVALID_PATH
        if isinstance(value, dict):
            if part not in value:
                return absent
            value = value[part]
        elif isinstance(value, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', part):
                return _INVALID_PATH
            # A very large valid index is simply absent, without an unbounded
            # integer conversion; additions/deletions still compare by value.
            if len(part) > len(str(len(value))) or int(part) >= len(value):
                return absent
            value = value[int(part)]
        else:
            return _INVALID_PATH
    return value


def _changed_path(before, after, path):
    """A changed ancestor cannot substantiate an unchanged leaf claim."""
    parts = _field_parts(path)
    if parts is None:
        return False
    old, new = _path_value(before, parts), _path_value(after, parts)
    if old is _INVALID_PATH or new is _INVALID_PATH:
        return False
    if old is _MISSING or new is _MISSING:
        return old is not new
    return digest(old) != digest(new)


def _changed_fields_match(bases, spec, fields, changed):
    return bool(fields and bases) and all(
        (parts := _field_parts(path)) is not None and parts[0] in changed
        and all(_changed_path(base, spec, path) for base in bases)
        for path in fields)


def _changed_fields_problem(index, reply, target, bases, spec, changed):
    fields = reply['changed_fields']
    kind = reply['revision_or_rebuttal']
    if kind == 'revision':
        invalid = [path for path in fields
            if not _changed_fields_match(bases, spec, [path], changed)]
        if fields and not invalid:
            return None
        reason = 'revision_fields_empty' if not fields else 'revision_fields_not_changed'
    else:
        if not fields:
            return None
        invalid, reason = list(fields), 'rebuttal_claims_changed_fields'
    return {'reply_index': index, 'field_path': ['issue_responses', index, 'changed_fields'],
        'objection_id': reply['objection_id'], 'origin_ref_sha256': reply['origin_ref_sha256'],
        'origin_ref': copy.deepcopy(target['origin_ref']), 'role': target['role'],
        'revision_or_rebuttal': kind, 'claimed_fields': copy.deepcopy(fields),
        'actual_changed_fields': sorted(changed), 'reason': reason,
        'invalid_paths': [{'path': path, 'field_path': ['issue_responses', index, 'changed_fields', position]}
            for position, path in enumerate(fields) if path in invalid]}


def origin_ref(row, vote, index):
    return {'revision': row.get('index'), 'spec_hash': row.get('spec_hash'),
        'document_id': (row.get('document') or {}).get('document_id'),
        'document_sha256': (row.get('document') or {}).get('sha256'),
        'role': vote.get('role'), 'handoff_id': vote.get('handoff_id'),
        'unit_id': vote.get('unit_id'), 'issue_index': index}


def _valid_origin(ref):
    return (isinstance(ref, dict) and set(ref) == {'revision', 'spec_hash', 'document_id',
        'document_sha256', 'role', 'handoff_id', 'unit_id', 'issue_index'}
        and type(ref['revision']) is int and ref['revision'] >= 0
        and type(ref['issue_index']) is int and ref['issue_index'] >= 0
        and ref['role'] in {'director', 'engineer', 'scout'}
        and all(isinstance(ref[key], str) and ref[key] for key in
                ('spec_hash', 'document_id', 'document_sha256', 'handoff_id'))
        and (ref['unit_id'] is None or isinstance(ref['unit_id'], str) and bool(ref['unit_id'])))


def _obligations(previous):
    result = []
    for issue in previous.get('objections', []):
        if not isinstance(issue.get('origin_refs'), list) or not issue['origin_refs']:
            _fail('原异议没有可核验的真实来源，不能隐式遗漏或关闭。')
        for ref in issue.get('origin_refs', []):
            if not _valid_origin(ref) or ref['role'] != issue.get('role'):
                _fail('原异议来源身份不完整。')
            result.append({'objection_id': issue['id'], 'role': issue['role'],
                'origin_ref': copy.deepcopy(ref), 'origin_ref_sha256': digest(ref),
                'disposition': issue.get('disposition', 'legacy_review'),
                'issue': issue['issue'], 'required_reads': copy.deepcopy(issue.get('required_reads', []))})
    keys = [item['origin_ref_sha256'] for item in result]
    if len(keys) != len(set(keys)):
        _fail('原异议来源重复或被分配给多个身份。')
    return result


def author_context(state, revision, context):
    """Opt in only an unfrozen author with an actual current revision delivery."""
    actions = context.get('document_revision_objections')
    if not context.get('implementation_revision_actions') or not isinstance(actions, list) or not actions:
        return context
    previous = context.get('previous_implementation_review') or {}
    obligations = _obligations(previous)
    known = {identity: item['id'] for item in previous.get('objections', [])
             for identity in [item['id'], *item.get('aliases', [])]}
    selected, origins = [], {item['origin_ref_sha256']: item for item in obligations}
    for action in actions:
        identity = known.get(action['id'], action['id'])
        related = [item for item in obligations if item['objection_id'] == identity]
        if not related or any(item['role'] != action['role'] for item in related):
            _fail('本轮文档修改要求不能借用其他角色或未知的原异议。')
        ref = {**copy.deepcopy(action['source']), 'role': action['role']}
        if not _valid_origin(ref):
            _fail('本轮文档修改要求缺少可核验的真实来源。')
        key = digest(ref)
        item = origins.get(key)
        if item is None:
            item = {'objection_id': identity, 'role': action['role'], 'origin_ref': ref,
                'origin_ref_sha256': key, 'issue': action.get('issue', related[0]['issue']),
                'required_reads': copy.deepcopy(action.get('required_reads', []))}
            obligations.append(item)
            origins[key] = item
        if item['objection_id'] != identity or any(old['origin_ref_sha256'] == key for old in selected):
            _fail('本轮修改要求重复或更换了原异议身份。')
        disposition = action.get('disposition', 'document_revision')
        if disposition not in {'document_revision', 'external_dependency', 'review_incomplete'}:
            _fail('整轮回复不能改变原问题的准确分类。')
        item.update(disposition=disposition, latest_action_sources=[copy.deepcopy(action['source'])])
        selected.append(copy.deepcopy(item))
    contract = {'version': VERSION, 'workflow_id': state['id'], 'revision': revision,
        'obligations': obligations, 'actions': selected, 'scientific_approval': False}
    contract['sha256'] = digest(contract)
    return {**context, PROTOCOL: VERSION, CONTRACT: contract}


def _contract(context):
    value = context.get(CONTRACT)
    if (context.get(PROTOCOL) != VERSION or not isinstance(value, dict)
            or value.get('version') != VERSION or value.get('scientific_approval') is not False
            or value.get('sha256') != digest({k: v for k, v in value.items() if k != 'sha256'})):
        _fail('冻结的逐项回应合同或指纹不一致。')
    return value


def _anchor_schema():
    text = {'type': 'string', 'minLength': 1}
    fields = {'document_id': text, 'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'section_id': text, 'start': {'type': 'integer', 'minimum': 0},
        'end': {'type': 'integer', 'minimum': 1}}
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}


def author_schema(base, context):
    if context.get(PROTOCOL) != VERSION:
        return copy.deepcopy(base)
    contract = _contract(context)
    result = copy.deepcopy(base)
    text = {'type': 'string', 'minLength': 1}
    fields = {'objection_id': text, 'origin_ref_sha256': text,
        'revision_or_rebuttal': {'enum': ['revision', 'rebuttal']},
        'rationale': {'type': 'string', 'minLength': 1, 'maxLength': 2400, 'pattern': r'\S'},
        'changed_fields': {'type': 'array', 'uniqueItems': True, 'items': text},
        'evidence': {'type': 'array', 'minItems': 1, 'maxItems': 12, 'items': _anchor_schema()}}
    count = len(contract['actions'])
    result['properties']['issue_responses'] = {'type': 'array', 'minItems': count, 'maxItems': count,
        'items': {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}}
    result['required'].append('issue_responses')
    from .implementation_author_revision import author_schema as revision_schema
    return revision_schema(result, context)


def _evidence(engine, run_id, refs, documents, handoffs, *, author_context=None):
    from . import document_index, implementation_reading as reading
    required = {}
    allowed = {doc['document_id']: doc['sha256'] for doc in documents}
    for ref in refs:
        did, sha = ref['document_id'], ref['sha256']
        if allowed.get(did) != sha:
            _fail('回应或确认引用了冻结范围外的文档版本。')
        manifest, _ = document_index.load(engine, run_id, did, sha)
        section = next((s for s in manifest['sections'] if s['id'] == ref['section_id']), None)
        if (not section or type(ref['start']) is not int or type(ref['end']) is not int
                or not section['start'] <= ref['start'] < ref['end'] <= section['end']):
            _fail('回应或确认的证据不是固定文档中的精确原文范围。')
        required.setdefault(did, []).append({'result': {'section_id': ref['section_id'],
            'char_start': ref['start'], 'char_end': ref['end']}})
    for did, ranges in required.items():
        document = {'document_id': did, 'sha256': allowed[did]}
        if author_context is None:
            report = reading.coverage(engine, run_id, document, ranges, handoffs)
        else:
            from .implementation_author_inherited_reading import coverage
            if len(handoffs) != 1:
                _fail('作者回应只能绑定当前真实保存交接。')
            report = coverage(engine, run_id, document, ranges, handoffs[0], author_context)
        if not report['complete']:
            from .engine import ResearchBlocked
            from .implementation_alignment_evidence import READ_PENDING
            raise ResearchBlocked(READ_PENDING + '；逐项回应或确认缺少本角色实际原文读取，不能关闭异议。')


def _external_identity(context):
    from .implementation_review_routing import evidence_key
    # New response/history archives and old SPEC references are audit material,
    # not an acquired dependency or a repaired host capability.
    documents = [doc for doc in context.get('indexed_documents', [])
        if doc.get('kind') not in {'implementation_specification', 'implementation_review_history', 'implementation_review_response'}
        and doc.get('scope') != 'historical_implementation_reference']
    return evidence_key({**context, 'indexed_documents': documents}, external=True)


def _response_bundles(obligations, responses):
    """Bind an older origin to every current response for its own canonical ID.

    This host-derived binding is not another author response or confirmation.
    Each current source still has its own mandatory response and review.
    """
    exact = {reply['origin_ref_sha256'] for reply in responses}
    bundles = []
    for item in obligations:
        related = sorted([reply for reply in responses if reply['objection_id'] == item['objection_id']
                          and reply['role'] == item['role']], key=lambda reply: reply['response_id'])
        if item['origin_ref_sha256'] in exact or not related:
            continue
        evidence = []
        for reply in related:
            for ref in reply['evidence']:
                if ref not in evidence:
                    evidence.append(copy.deepcopy(ref))
        value = {key: copy.deepcopy(item[key]) for key in
            ('objection_id', 'role', 'origin_ref', 'origin_ref_sha256')}
        value.update(response_id=item['origin_ref_sha256'], response_type='historical_response_bundle',
            response_ids=[reply['response_id'] for reply in related],
            response_hashes=[reply['response_sha256'] for reply in related], evidence=evidence,
            rationale='宿主将本角色的全部当前逐项回应绑定到此历史来源；仍须原提出者独立确认每个当前来源与历史来源。',
            provenance='host_response_binding', scientific_approval=False)
        value['response_sha256'] = digest(value)
        bundles.append(value)
    return bundles


def _origin_delivery(engine, run_id, state, ref, cache):
    """Authenticate an original objection; incomplete reading stays incomplete."""
    from . import document_index
    from . import implementation_alignment_evidence as alignment
    from .intelligence import parse_json
    matches = [source for source in state.get('specifications', [])
        if source.get('index') == ref['revision'] and source.get('spec_hash') == ref['spec_hash']
        and source.get('document', {}).get('document_id') == ref['document_id']
        and source.get('document', {}).get('sha256') == ref['document_sha256']]
    if len(matches) != 1:
        _fail('原异议文档身份与真实历史版本不一致。')
    source = matches[0]
    document_index.load(engine, run_id, ref['document_id'], ref['document_sha256'])
    votes = [vote for vote in source.get('review_votes', [])
        if vote.get('role') == ref['role'] and vote.get('handoff_id') == ref['handoff_id']
        and vote.get('unit_id') == ref['unit_id']]
    if len(votes) != 1 or not 0 <= ref['issue_index'] < len(votes[0].get('issues', [])):
        _fail('原异议缺少唯一真实问题位置。')
    key = (ref['revision'], ref['spec_hash'], ref['document_sha256'], ref['role'], ref['handoff_id'], ref['unit_id'])
    if key in cache:
        return cache[key]
    assignment = engine.store.get('handoffs', ref['handoff_id'], {})
    from . import implementation_indexed_review as indexed
    if (not indexed.enabled(engine, run_id, state, source)
            or assignment.get('purpose') != indexed.purpose(state, source)
            or ref['unit_id'] is not None):
        _fail('作者交付不能成为原评审异议或关闭凭证。')
    result = {key: value for key, value in votes[0].items() if key not in META}
    context = alignment._delivery(engine, run_id, ref['role'], ref['handoff_id'], result)
    frozen = engine.store.get('handoff_inputs', ref['handoff_id'], {})
    delivered = engine.store.get('decisions', ref['handoff_id'], {}).get('result')
    parse_json(json.dumps(delivered, ensure_ascii=False), frozen['schema'])
    if (context.get('spec_hash') != ref['spec_hash'] or context.get('document_sha256') != ref['document_sha256']
            or context.get('implementation_document', {}).get('document_id') != ref['document_id']
            or (context.get('review_unit') or {}).get('id') != ref['unit_id']):
        _fail('原异议的冻结文档或范围绑定已改变。')
    indexed.validate_vote(engine, run_id, state, source, ref['role'], ref['handoff_id'], result, context)
    cache[key] = context
    return context


@operation
def validate_author(engine, run_id, state, row, hid, result, context):
    """Validate a real submitting author's complete response; never close it."""
    if context.get(PROTOCOL) != VERSION:
        return None
    from .prose_schema import validate
    from . import implementation_authoring as author, implementation_spec_progress as progress
    from .engine import Harness
    contract = _contract(context)
    validate(result, author_schema(author.SUBMISSION, context))
    if contract['workflow_id'] != state['id'] or contract['revision'] != row['index']:
        _fail('作者回应绑定了另一个工作流或修订版本。')
    if (result['document_id'] != row['document']['document_id']
            or result['document_sha256'] != row['document']['sha256']
            or result['spec_hash'] != row['spec_hash']):
        _fail('作者回应提交指针不属于本交接实际保存的当前文档版本。')
    assignment = engine.store.get('handoffs', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    decision = engine.store.get('decisions', hid, {})
    payload = {key: frozen.get(key) for key in ('instruction', 'context', 'schema')}
    from . import implementation_alignment_evidence as alignment
    candidate = alignment._candidate.get()
    checking_candidate = bool(candidate and candidate[:6] == (
        id(engine), run_id, 'engineer', hid, digest(result), digest(context)))
    if (assignment.get('id') != hid or assignment.get('run_id') != run_id
            or assignment.get('to_role') != 'engineer'
            or not assignment.get('purpose', '').startswith('implementation_spec:')
            or frozen.get('context') != context or frozen.get('run_id') != run_id
            or assignment.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, 'engineer', assignment['purpose'], **payload) != hid):
        _fail('作者回应缺少该工程师完整、真实且冻结的独立交付。')
    if not checking_candidate and (assignment.get('status') != 'completed'
            or assignment.get('output_hash') != digest(result)
            or decision.get('id') != hid or decision.get('run_id') != run_id
            or decision.get('role') != 'engineer' or decision.get('purpose') != assignment['purpose']
            or decision.get('result') != result):
        _fail('作者回应缺少该工程师完整、真实且冻结的独立交付。')
    obligations, source_contexts = copy.deepcopy(contract['obligations']), {}
    for item in obligations:
        source_context = _origin_delivery(engine, run_id, state, item['origin_ref'], source_contexts)
        if item['disposition'] == 'external_dependency':
            item['external_evidence_key'] = _external_identity(source_context)
    expected = {item['origin_ref_sha256']: item for item in contract['actions']}
    replies = result['issue_responses']
    if (len({reply['origin_ref_sha256'] for reply in replies}) != len(replies)
            or {reply['origin_ref_sha256'] for reply in replies} != set(expected)):
        _fail('作者必须逐一回应全部本轮文档异议来源，不能遗漏或重复。')
    documents = [row['document'], *context.get('indexed_documents', [])]
    values, evidence_refs, changed_problems = [], [], []
    for index, reply in enumerate(replies):
        target = expected[reply['origin_ref_sha256']]
        if context.get('indexed_response_actions') is True and target['disposition'] != 'document_revision' and reply['revision_or_rebuttal'] != 'rebuttal':
            _fail('外部依赖或未完成审读只能说明真实状态，不能用作者改写冒充已修复。')
        if reply['objection_id'] != target['objection_id']:
            _fail('作者回应更换了原异议身份。')
        changed_sets, comparison_bases = [], []
        for source_ref in target['latest_action_sources']:
            source = next((old for old in state.get('specifications', [])
                           if old.get('index') == source_ref['revision']), None)
            if (not source or source.get('spec_hash') != source_ref['spec_hash']
                    or source.get('document', {}).get('document_id') != source_ref['document_id']
                    or source.get('document', {}).get('sha256') != source_ref['document_sha256']):
                _fail('作者回应缺少本轮修改要求的原始审查文档。')
            matches = [vote for vote in source.get('review_votes', [])
                if vote.get('handoff_id') == source_ref['handoff_id'] and vote.get('role') == target['role']
                and vote.get('unit_id') == source_ref['unit_id']]
            if len(matches) != 1 or not 0 <= source_ref['issue_index'] < len(matches[0].get('issues', [])):
                _fail('作者回应的本轮修改要求没有真实原问题位置。')
            changed_sets.append(set(progress._changed(source, row)))
            comparison_bases.append(source.get('spec', {}))
        changed = set.intersection(*changed_sets)
        problem = _changed_fields_problem(index, reply, target, comparison_bases, row.get('spec', {}), changed)
        if problem:
            changed_problems.append(problem)
        if not any(ref['document_id'] == row['document']['document_id']
                   and ref['sha256'] == row['document']['sha256'] for ref in reply['evidence']):
            _fail('作者回应缺少实际保存的本版文档证据。')
        evidence_refs.extend(reply['evidence'])
        value = {**copy.deepcopy(reply), 'response_id': target['origin_ref_sha256'],
            'role': target['role'], 'origin_ref': copy.deepcopy(target['origin_ref']),
            'latest_action_sources': copy.deepcopy(target['latest_action_sources'])}
        value['response_sha256'] = digest(value)
        values.append(value)
    # Finish the known author work before granting a read-only final repair.
    # No incomplete candidate is sealed as a decision or sent to reviewers.
    from .implementation_author_revision import validate as validate_revision
    validate_revision(engine, run_id, state, row, hid, result, context)
    _evidence(engine, run_id, evidence_refs, documents, [hid], author_context=context)
    # A declaration may be corrected only after every response's origin,
    # version and actual read evidence passed. Missing evidence remains a
    # normal read gate and must never be reclassified as recoverable format.
    if changed_problems:
        raise ChangedFieldsBlocked(changed_problems)
    receipt = {'version': VERSION, 'workflow_id': state['id'], 'revision': row['index'],
        'spec_hash': row['spec_hash'], 'document_id': row['document']['document_id'],
        'document_sha256': row['document']['sha256'], 'authoring_handoff_id': hid,
        'input_sha256': assignment['input_hash'], 'output_sha256': digest(result),
        'read_ledger_sha256': digest(engine.store.get('handoff_progress', hid, {}).get('tool_results', [])),
        'contract_sha256': contract['sha256'], 'obligations': obligations,
        'responses': values, 'response_bundles': _response_bundles(obligations, values), 'scientific_approval': False}
    receipt['sha256'] = digest(receipt)
    return receipt


def _all_responses(receipt):
    return receipt['responses']


def _receipt(row):
    receipt = row.get(RECEIPT)
    if (row.get(PROTOCOL) != VERSION or not isinstance(receipt, dict)
            or receipt.get('version') != VERSION or receipt.get('scientific_approval') is not False
            or receipt.get('revision') != row.get('index') or receipt.get('spec_hash') != row.get('spec_hash')
            or receipt.get('document_id') != row.get('document', {}).get('document_id')
            or receipt.get('document_sha256') != row.get('document', {}).get('sha256')
            or receipt.get('sha256') != digest({k: v for k, v in receipt.items() if k != 'sha256'})):
        _fail('保存的逐项回应与文档版本或指纹不一致。')
    if not isinstance(receipt.get('obligations'), list) or not isinstance(receipt.get('responses'), list):
        _fail('保存的逐项回应缺少原异议清单。')
    origins = {}
    for item in receipt['obligations']:
        ref = item.get('origin_ref')
        if (not _valid_origin(ref) or item.get('role') != ref['role']
                or item.get('origin_ref_sha256') != digest(ref) or digest(ref) in origins):
            _fail('保存的原异议来源或指纹不一致。')
        origins[digest(ref)] = item
    if 'incremental_target_sha256' in receipt or receipt.get('prior_responses'):
        _fail('旧单问题继承回应已删除，不能作为新整轮作者回复。')
    seen = set()
    for reply in _all_responses(receipt):
        key = reply.get('origin_ref_sha256')
        item = origins.get(key)
        if (item is None or key in seen or reply.get('origin_ref') != item['origin_ref']
                or reply.get('response_id') != key or reply.get('role') != item['role']
                or reply.get('objection_id') != item['objection_id']
                or reply.get('response_sha256') != digest({k: v for k, v in reply.items() if k != 'response_sha256'})):
            _fail('保存的工程师逐项回应来源或完整结果指纹不一致。')
        seen.add(key)
    if receipt.get('response_bundles') != _response_bundles(receipt['obligations'], _all_responses(receipt)):
        _fail('历史来源与本角色全部当前回应的宿主绑定已改变。')
    return receipt


def review_context(row, role, value):
    if row.get(PROTOCOL) != VERSION:
        return value
    receipt = _receipt(row)
    replies = {reply['origin_ref_sha256']: reply for reply in _all_responses(receipt)}
    bundles = {reply['origin_ref_sha256']: reply for reply in receipt['response_bundles']}
    requirements = []
    for item in receipt['obligations']:
        if item['role'] != role:
            continue
        reply = replies.get(item['origin_ref_sha256'])
        reply = reply or bundles.get(item['origin_ref_sha256'])
        requirements.append({**copy.deepcopy(item), 'response_sha256': (reply or {}).get('response_sha256'),
            'author_response': copy.deepcopy(reply),
            **({'current_responses': [copy.deepcopy(candidate) for candidate in _all_responses(receipt)
                if candidate['response_id'] in reply['response_ids']]} if reply and 'response_ids' in reply else {})})
    contract = {'version': VERSION, 'workflow_id': receipt['workflow_id'], 'revision': row['index'],
        'receipt_sha256': receipt['sha256'], 'requirements': requirements,
        'obligations': [copy.deepcopy(item) for item in receipt['obligations'] if item['role'] == role],
        'scientific_approval': False}
    contract['sha256'] = digest(contract)
    return {**value, PROTOCOL: VERSION, CONTRACT: contract}


def review_schema(base, context=None):
    result = copy.deepcopy(base)
    text = {'type': 'string', 'minLength': 1}
    fields = {'objection_id': text, 'origin_ref_sha256': text,
        'response_sha256': {'type': ['string', 'null'], 'minLength': 1},
        'outcome': {'enum': ['resolved', 'unresolved']},
        'rationale': {'type': 'string', 'minLength': 1, 'maxLength': 2400, 'pattern': r'\S'},
        'evidence': {'type': 'array', 'maxItems': 12, 'items': _anchor_schema()}}
    result['properties']['issue_confirmations'] = {'type': 'array', 'items': {
        'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}}
    if context is not None and context.get(PROTOCOL) == VERSION:
        required = _contract(context)['requirements']
        result['properties']['issue_confirmations'].update(minItems=len(required), maxItems=len(required))
        if required:
            fields['origin_ref_sha256'] = {**text, 'enum': [item['origin_ref_sha256'] for item in required]}
    result['required'].append('issue_confirmations')
    return result


def confirmed_ids(vote, row):
    """Pure closure projection; actual delivery/read checks remain at gates."""
    receipt = _receipt(row)
    own = [item for item in receipt['obligations'] if item['role'] == vote.get('role')]
    replies = {item['origin_ref_sha256']: item['response_sha256']
               for item in [*_all_responses(receipt), *receipt['response_bundles']]}
    confirmations = vote.get('issue_confirmations', [])
    lookup = {item['origin_ref_sha256']: item for item in own}
    resolved = {item.get('origin_ref_sha256') for item in confirmations
        if item.get('outcome') == 'resolved' and item.get('origin_ref_sha256') in lookup
        and item.get('objection_id') == lookup[item['origin_ref_sha256']]['objection_id']
        and item.get('response_sha256') == replies.get(item.get('origin_ref_sha256'))}
    return {identity for identity in {item['objection_id'] for item in own}
            if {item['origin_ref_sha256'] for item in own if item['objection_id'] == identity} <= resolved}


def validate_review(engine, run_id, role, hid, result, context):
    if context.get(PROTOCOL) != VERSION:
        return None
    contract = _contract(context)
    assignment = engine.store.get('handoffs', hid, {})
    token = digest(contract['workflow_id'])
    from .implementation_indexed_review import CONTRACT as INDEXED_CONTRACT
    if (not assignment.get('purpose', '').startswith('implementation_document_review:' + token + ':')
            or INDEXED_CONTRACT not in context or context.get('review_unit')):
        _fail('工程师的作者回应不能代替原提出者的独立审查确认。')
    required = {item['origin_ref_sha256']: item for item in contract['requirements']}
    confirmations = result.get('issue_confirmations')
    if (not isinstance(confirmations, list)
            or len({item.get('origin_ref_sha256') for item in confirmations}) != len(confirmations)
            or {item.get('origin_ref_sha256') for item in confirmations} != set(required)):
        _fail('原提出者必须逐项确认冻结工作单中的全部异议来源。')
    from . import implementation_alignment_evidence as alignment
    handoffs = alignment._handoffs(engine, run_id, role, hid, result, context)
    unresolved, evidence_refs = False, []
    for confirmation in confirmations:
        item = required[confirmation['origin_ref_sha256']]
        if (item['role'] != role or confirmation['objection_id'] != item['objection_id']
                or confirmation['response_sha256'] != item['response_sha256']):
            _fail('确认不是原提出者对原异议和当前回应的复核。')
        if confirmation['outcome'] == 'resolved':
            if item['disposition'] == 'document_revision' and not item['author_response']:
                _fail('原文档异议没有工程师逐项回应，不能确认已解决。')
            if not confirmation['evidence']:
                _fail('确认关闭必须给出实际核对的决定性文档证据。')
            current = context['implementation_document']
            if not any(ref['document_id'] == current['document_id'] and ref['sha256'] == current['sha256']
                       for ref in confirmation['evidence']):
                _fail('确认关闭必须实际核对当前实现文档，不能借用旧版确认。')
            if (item['disposition'] == 'external_dependency'
                    and (not item.get('external_evidence_key')
                         or item['external_evidence_key'] == _external_identity(context))):
                _fail('外部依赖尚无真实恢复证据；改写或读取实现文档不能关闭。')
            evidence_refs.extend(confirmation['evidence'])
        else:
            unresolved = True
    _evidence(engine, run_id, evidence_refs, context['indexed_documents'], handoffs)
    own = contract['obligations']
    resolved = {item['origin_ref_sha256'] for item in confirmations if item['outcome'] == 'resolved'}
    allowed = {identity for identity in {item['objection_id'] for item in own}
        if {item['origin_ref_sha256'] for item in own if item['objection_id'] == identity} <= resolved}
    claimed = set(result.get('progress', {}).get('resolved_objection_ids', []))
    if claimed != allowed or unresolved and result.get('approve') is True:
        _fail('尚未由原提出者逐来源确认的异议不能关闭或批准。')
    return {'confirmed_origin_refs': sorted(resolved), 'scientific_approval': False}


def validate_gate(engine, run_id, state, row):
    if row.get(PROTOCOL) != VERSION:
        return
    from . import implementation_authoring as author
    author.validate_submission(engine, run_id, state, row)
    receipt = _receipt(row)
    from .implementation_spec_progress import _bound_vote
    votes = row.get('review_votes', [])
    if (len(votes) != 3 or {vote.get('role') for vote in votes} != {'director', 'engineer', 'scout'}
            or any(not _bound_vote(row, vote, unit=False) or vote.get('approve') is not True
                or vote.get('issues') or vote.get('fixes')
                or vote['handoff_id'] == receipt['authoring_handoff_id'] for vote in votes)):
        _fail('仍须同版本三方独立终审；工程师作者交付不能替代确认。')
    closed = set()
    for vote in votes:
        closed.update(confirmed_ids(vote, row))
    required = {item['objection_id'] for item in receipt['obligations']}
    if not required <= closed:
        _fail('仍有异议缺少原提出者的逐项独立确认，不能派发代码。')
