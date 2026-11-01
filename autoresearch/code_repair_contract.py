"""Evidence-bound code repair, with independent semantic review before dispatch.

Historical votes are retained verbatim. A vote is never itself a code-edit
instruction. The host authenticates references; independent reviewers assess
whether the proposed change follows from those references.
"""
from __future__ import annotations

import copy
import hashlib
import json

from .architecture_validation import operation

FIELD = 'code_repair_scope'
VERSION = 1
ROLES = ('director', 'engineer', 'scout')
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 6000, 'pattern': r'\S'}
DELIVERY_GUIDANCE = (
    '交付规则：源码只改原清单 allowed_paths 及相关 tests/，不得扩张实现要求。'
    'implementation.json、repair-results.json、README.md 可同步记录本轮真实交付和未验证状态；'
    '测试结果摘要放在 test-results/，同时列入 delivery_files 和 test_result_files，不能列入 runtime_files。'
    '历史清单若写“验证输出保存于 tests/”，按当前目录合同使用 test-results/；原问题和验证标准不变。'
    '只运行原要求已授权的检查；须由宿主执行或尚未获准的验证如实填 outcome=not_run，'
    '已执行但失败填 failed，不得为交付伪填 passed。not_run 只表示待验收，不能关闭问题或视为通过。'
)


def obj(fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def fail(message):
    from .implementation_failures import ImplementationCodeBlocked
    raise ImplementationCodeBlocked('代码修改依据待核实：' + message,
        kind='codex_repair_unverified')


def isolated_request(context):
    """Repair mapping/audits use their full frozen packet, not all role history.

    The packet already contains original objections, current source evidence,
    clarified votes and deferred issues. Unrelated historical replies or RAG
    must not become additional instructions or overflow this scoped audit.
    This changes only context assembly; it grants no review/edit authority.
    """
    purpose = str(context.get('_purpose', ''))
    kind = purpose.split(':', 1)[0]
    if (type(context.get('code_repair_protocol')) is not int
            or context['code_repair_protocol'] != VERSION
            or context.get('indexed_document_context') is not True
            or kind not in {'code_repair_map', 'code_repair_audit', 'code_repair_amend'}):
        return False
    audit = kind == 'code_repair_audit'
    amendment = kind == 'code_repair_amend'
    identity = context.get('amendment_source_sha256' if amendment else 'packet_sha256' if audit else 'source_sha256')
    payload = context.get('amendment_source' if amendment else 'repair_packet' if audit else 'repair_source')
    compact = audit and context.get('audit_view_version') in (2, 3)
    if compact:
        payload = context.get('repair_packet_view')
    if (not isinstance(identity, str) or len(identity) != 64
            or any(c not in '0123456789abcdef' for c in identity)
            or purpose != kind + ':' + identity or not isinstance(payload, dict)
            or (audit and not compact and sha(payload) != identity)
            or (amendment and sha(payload) != identity)
            or (compact and (payload.get('source_packet_sha256') != identity
                or sha(payload) != context.get('audit_view_sha256')))):
        fail('独立修改核对的上下文未绑定完整原输入。')
    return True


def previous_attempt(state, before=None, *, reviewed=False):
    rows = [a for a in state['attempts'] if a.get('status') != 'superseded'
            and (before is None or a['index'] < before)
            and (not reviewed or a.get('review_votes'))]
    return max(rows, key=lambda a: a['index']) if rows else None


def check_progress(state, attempt):
    """Unlimited delivery allowance must not mean an unchanged-objection loop."""
    rows = sorted((a for a in state['attempts'] if a['index'] <= attempt['index']
        and a.get('status') != 'superseded' and a.get('review_votes')), key=lambda a: a['index'])[-3:]
    if len(rows) != 3 or not attempt.get('repair_contract_sha256'):
        return
    outstanding = [{issue_id(a, v, i) for v in a['review_votes'] for i in range(len(v['issues']))} for a in rows]
    if outstanding[0] and outstanding[0] == outstanding[1] == outstanding[2]:
        from .implementation_failures import ImplementationCodeBlocked
        raise ImplementationCodeBlocked('连续三轮代码复审的原问题集未减少，停止自动修改并核对原因；原记录保留。',
            kind='codex_code_no_progress', details={'workflow_id': state['id'],
                'attempt': attempt['index'], 'unresolved_issue_ids': sorted(outstanding[-1])})


def issue_id(attempt, vote, index):
    bindings = vote.get('repair_findings', [])
    existing = [r.get('origin_id') for r in bindings if r.get('issue_index') == index and r.get('origin_id')]
    return existing[0] if len(existing) == 1 else sha({
        'delivery_hash': attempt['delivery_hash'], 'role': vote['role'],
        'vote_sha256': sha(vote), 'issue_index': index})


def issues(attempt):
    result = []
    for vote in attempt.get('review_votes', []):
        for index, text in enumerate(vote.get('issues', [])):
            result.append({'issue_id': issue_id(attempt, vote, index), 'role': vote['role'],
                'issue_index': index, 'vote_sha256': sha(vote), 'issue': text,
                'binding': next((copy.deepcopy(r) for r in vote.get('repair_findings', [])
                                 if r['issue_index'] == index), None),
                'fixes': copy.deepcopy([f for f in vote['fixes'] if f['issue_index'] == index])})
    if len({r['issue_id'] for r in result}) != len(result):
        fail('原问题身份重复。')
    return result


def scope(state, attempt, specification):
    if attempt.get('definition_repair_id'):
        return None  # New approved definition; old instructions cannot cross SPEC versions.
    old = previous_attempt(state, attempt['index'], reviewed=True)
    if old is None:
        return None
    packet = old.get('repair_contract')
    if not packet or packet.get('version') != VERSION:
        return None  # First upgraded repair establishes the historical baseline.
    if packet['spec_hash'] != specification['spec_hash']:
        fail('复审不得跨实现规范版本沿用问题。')
    before = {r['path']: r['sha256'] for r in old['snapshot']['files']}
    after = {r['path']: r['sha256'] for r in attempt['snapshot']['files']}
    return {'version': VERSION, 'spec_hash': specification['spec_hash'],
        'previous_delivery_hash': old['delivery_hash'],
        'changed_paths': sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p)),
        'previous_artifacts': copy.deepcopy(old['delivery_artifacts']),
        'document_memory_handoffs': [{k: v[k] for k in ('role', 'handoff_id')}
            for v in [*old.get('review_votes', []), *packet.get('clarification', {}).get('votes', [])]],
        'issues': [{k: copy.deepcopy(item[k]) for k in
                   ('issue_id', 'role', 'issue', 'requirement_id', 'fixes')}
                   for item in [*packet['items'], *packet.get('deferred_items', [])]]}


def review_schema(schema):
    result = copy.deepcopy(schema)
    result['properties']['repair_findings'] = {'type': 'array', 'maxItems': 90, 'items': obj({
        'issue_index': {'type': 'integer', 'minimum': 0}, 'requirement_id': TEXT,
        'origin_id': {'anyOf': [TEXT, {'type': 'null'}]},
        'disposition': {'enum': ['repair_code', 'needs_review', 'spec_conflict', 'needs_execution', 'needs_definition']},
        'evidence_indices': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
            'items': {'type': 'integer', 'minimum': 0}},
        'regression': {'anyOf': [{'type': 'null'}, obj({'path': TEXT,
            'severity': {'enum': ['P0', 'P1']}, 'reason': TEXT})]}})}
    if 'repair_findings' not in result['required']:
        result['required'].append('repair_findings')
    return result


REVIEW_GUIDANCE = '''
本轮实际代码复审按 code_repair_scope 收敛。只验收本角色的原 issue_id 与原 fixes.change/verification，
不得追加可选优化、普通漏提问题，或借原问题提高标准。已满足或经证据反驳的问题不再列入 issues。
每条未解决问题返回一个 repair_findings，origin_id 使用本角色原 issue_id；change 和 verification
逐字沿用原标准，location 可指向当前代码。evidence_indices 引用本次 code_evidence 的下标，
requirement_id 必须一致。证据不足用 needs_review，文档冲突用 spec_conflict，均不能驱动改代码。
已有实现仅缺实际执行回执用 needs_execution；批准文档未定义必要做法用 needs_definition。
这两类不得改写为代码缺陷、要求重复实现，或让 Codex 自行新增算法、测试标准和资源额度。
只有本次实际改动新引入的 P0/P1 严重回归可用 origin_id=null，并给出 changed_paths 中的文件、
实际变更导致的错误和批准条款依据。旧问题/旧缺项不能冒充新增回归。所有问题解决且无严重回归才批准。
repair_verification 仅为真实作者回执及待验收清单，不是宿主验证结果。outcome=not_run 表示尚未执行；
须按原实现文档区分当前必要检查和授权后才能执行的检查，结合本轮真实宿主回执核对。
不得把作者自报 passed、保存完成或通用 smoke 当作逐项验证通过，也不得要求作者提前执行未授权实验。
'''


def validate_review(value, context, role):
    policy = context.get(FIELD)
    if not policy:
        return
    if policy.get('version') != VERSION or policy.get('spec_hash') != context.get('spec_hash'):
        fail('复审范围与当前规范不一致。')
    findings = value.get('repair_findings', [])
    if (len(findings) != len(value['issues']) or
            {r['issue_index'] for r in findings} != set(range(len(value['issues'])))):
        fail('每条否决必须对应唯一原问题或真实严重回归。')
    allowed = {i['issue_id']: i for i in policy['issues'] if i['role'] == role}
    from .code_repair_clarification import validate_responses, FIELD as CLARIFICATION
    responses = validate_responses(value, context, role) if context.get(CLARIFICATION) else None
    from .code_repair_response_correction import fail as response_error
    seen = set()
    files = {i['artifact_id']: i['path'] for i in context['delivery_artifacts']}
    for row in findings:
        refs = value.get('code_evidence', [])
        if any(type(i) is not int or not 0 <= i < len(refs) for i in row['evidence_indices']):
            response_error('问题引用不存在的本轮代码依据。', value, context, role)
        selected = [refs[i] for i in row['evidence_indices']]
        if not selected or any(e['requirement_id'] != row['requirement_id'] for e in selected):
            response_error('问题与代码依据不属于同一验收项。', value, context, role)
        origin = row['origin_id']
        if origin is not None:
            if origin not in allowed or origin in seen or row['regression'] is not None:
                fail('问题不属于本角色待复核范围，或重复重开。')
            seen.add(origin)
            old = allowed[origin]
            if old['requirement_id'] != row['requirement_id']:
                fail('原问题不能更换验收项。')
            expected = sorted((f['change'], f['verification']) for f in old['fixes'])
            actual = sorted((f['change'], f['verification']) for f in value['fixes']
                            if f['issue_index'] == row['issue_index'])
            if expected != actual and not (responses and responses[origin]['resolution'] == 'corrected'):
                response_error('原问题的修改要求或验证标准发生漂移。', value, context, role)
        else:
            if responses is not None:
                fail('同一代码交付的意见澄清不得新增问题。')
            regression = row.get('regression') or {}
            if (regression.get('severity') not in {'P0', 'P1'}
                    or regression.get('path') not in policy['changed_paths']
                    or not regression.get('reason')
                    or regression['path'] not in {files.get(e['artifact_id']) for e in selected}
                    or not any(f['requirement_id'] == row['requirement_id'] and f['status'] == 'mismatch'
                               for f in value.get('alignment_findings', []))):
                fail('新问题没有绑定本次实际修改引入的严重回归。')


PLAN_SCHEMA = obj({'source_sha256': TEXT, 'items': {'type': 'array', 'minItems': 1, 'maxItems': 90,
    'items': obj({'issue_id': TEXT, 'requirement_id': TEXT,
        'disposition': {'enum': ['repair_code', 'needs_review', 'spec_conflict', 'needs_execution', 'needs_definition']},
        'evidence_indices': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
            'items': {'type': 'integer', 'minimum': 0}}, 'reason': TEXT})}})
AUDIT_SCHEMA = obj({'packet_sha256': TEXT, 'approve': {'type': 'boolean'},
    'checks': {'type': 'array', 'minItems': 1, 'maxItems': 90, 'items': obj({
        'issue_id': TEXT, 'supported': {'type': 'boolean'}, 'reason': TEXT})}})
RECEIPT_SCHEMA = obj({'repair_contract_sha256': TEXT, 'items': {
    'type': 'array', 'minItems': 1, 'maxItems': 90, 'items': obj({
        'issue_id': TEXT, 'changed_files': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': TEXT},
        'verification': TEXT, 'commands': {'type': 'array', 'minItems': 1, 'items': TEXT},
        'outcome': {'enum': ['passed', 'failed', 'not_run']}, 'details': TEXT})}})
PLAN_TASK = '''逐条核对待修改意见，不编写代码、不改实现文档、不重写原 fixes。
为每个 issue_id 选择本角色已有 code_evidence 下标和当前 acceptance_checks 的完整 requirement_id。
只有批准实现条款明确要求且实际代码违背时才选择 repair_code；引用存在不代表语义支持。
缺乏决定性原文/代码依据用 needs_review；要求与批准规范冲突或要求改变科学方案用 spec_conflict。
原作者的建议只是待核实意见，不能凭其措辞补造要求。逐条解释为何原 change 和 verification 忠实实现原条款。
返回全部问题，不遗漏、不合并、不新增，不把已完成的历史问题重新下发。'''
AUDIT_TASK = '''独立核对这份代码修改指令是否确实由当前批准实现文档支持。
逐条对照 host_evidence 中宿主核验的原文、实际代码、acceptance_check、原 fixes.change/verification，
检查意见是否误读、与规范冲突、提高门槛、属于可选优化、缺证据或与另一条修改相互矛盾。
引文和哈希有效不等于修改合理；必须核对语义，不能照抄计划作者结论。严重回归还需核对是否由本次修改引入。
每个 issue_id 返回 supported 与具体理由；只有全部条款支持原修改且彼此兼容才 approve=true。
不得改计划、代码、文档或代替代码验收。任何不确定项返回 supported=false，保留原问题等待核实。'''


@operation
def build_source(engine, run_id, state, specification, attempt, *, votes=None, purpose=None):
    from .codex_workflow import _verify_snapshot, _frozen_spec_assignment, _digest
    from .implementation_alignment_evidence import validate_vote
    _verify_snapshot(engine, run_id, attempt['snapshot'])
    votes = attempt['review_votes'] if votes is None else votes
    if {v['role'] for v in votes} != set(ROLES) or len(votes) != 3:
        fail('缺少同一交付的三个角色原始结论。')
    contexts = {}
    for vote in votes:
        frozen = _frozen_spec_assignment(engine, run_id,
            purpose or attempt.get('review_purpose') or f'code_review:{_digest(state["id"])}:{attempt["index"]}', vote['role'])
        if not frozen or frozen['context'].get('implementation_alignment_protocol') != 1:
            fail('原审核缺少不可变交接。')
        context = frozen['context']
        if (context['spec_hash'] != specification['spec_hash']
                or context['delivery_hash'] != attempt['delivery_hash']
                or context['implementation_document']['sha256'] != specification['document']['sha256']):
            fail('原意见没有绑定当前批准实现文档与代码交付。')
        # The sealed result excludes host-added role/handoff_id metadata.
        result = {k: v for k, v in vote.items() if k not in {'role', 'handoff_id'}}
        validate_vote(engine, run_id, vote['role'], vote['handoff_id'], result, context, delivery=True)
        contexts[vote['role']] = context
    return {'version': VERSION, 'workflow_id': state['id'], 'attempt_index': attempt['index'],
        'spec_hash': specification['spec_hash'], 'implementation_document': specification['document'],
        'delivery_hash': attempt['delivery_hash'], 'issues': issues({'review_votes': votes,
            'delivery_hash': attempt['delivery_hash']}),
        'acceptance_checks': specification['spec']['acceptance_checks'],
        'role_evidence': {v['role']: v['code_evidence'] for v in votes},
        'review_scope': contexts['director'].get(FIELD),
        'source_contexts': contexts}


@operation
def materialize(engine, run_id, source, proposal, *, allow_deferred=False, allow_empty=False):
    from .document_index import load
    from .implementation_alignment_evidence import _code_text
    from .intelligence import parse_json
    parse_json(json.dumps(proposal, ensure_ascii=False), PLAN_SCHEMA)
    if proposal['source_sha256'] != sha(source):
        fail('修改映射不是当前意见集。')
    known = {r['issue_id']: r for r in source['issues']}
    if (len(proposal['items']) != len(known)
            or {r['issue_id'] for r in proposal['items']} != set(known)):
        fail('修改映射遗漏、重复或新增问题。')
    requirements = {r['id']: r for r in source['acceptance_checks']}
    items = []
    for row in proposal['items']:
        issue = known[row['issue_id']]
        binding = issue.get('binding')
        if binding and (binding['disposition'] != row['disposition']
                or binding['requirement_id'] != row['requirement_id']
                or binding['evidence_indices'] != row['evidence_indices']):
            fail('修改映射改变了原审核的问题分类或证据绑定。')
        if row['disposition'] != 'repair_code' and not allow_deferred:
            fail('存在需要补证据或与实现文档冲突的意见；不下发代码修改。')
        if row['requirement_id'] not in requirements:
            fail('修改意见引用不存在的验收项。')
        context = source['source_contexts'][issue['role']]
        refs = source['role_evidence'][issue['role']]
        if any(type(i) is not int or not 0 <= i < len(refs) for i in row['evidence_indices']):
            fail('修改映射引用不存在的原代码证据。')
        evidence = []
        for index in row['evidence_indices']:
            ref = refs[index]
            if ref['requirement_id'] != row['requirement_id']:
                fail('原修改、验收项、代码依据未对齐。')
            code = next((d for d in context['delivery_artifacts'] if d['artifact_id'] == ref['artifact_id']
                         and d['sha256'] == ref['sha256']), None)
            if code is None:
                fail('代码证据不是当前冻结交付。')
            text = _code_text(engine, run_id, ref['artifact_id'], code)
            end = ref['offset'] + ref['limit']
            if not 0 <= ref['offset'] < end <= len(text):
                fail('代码证据范围无效。')
            documents = []
            for pointer in ref['implementation_refs']:
                doc = source['implementation_document']
                if pointer['document_id'] != doc['document_id'] or pointer['document_sha256'] != doc['sha256']:
                    fail('修改依据不是当前批准实现文档。')
                manifest, body = load(engine, run_id, doc['document_id'], doc['sha256'])
                section = next((s for s in manifest['sections'] if s['id'] == pointer['section_id']), None)
                if section is None or not 0 <= pointer['offset'] < pointer['offset'] + pointer['limit'] <= section['end'] - section['start']:
                    fail('修改依据超出真实文档章节。')
                start = section['start'] + pointer['offset']
                documents.append({'reference': pointer, 'text': body[start:start + pointer['limit']]})
            if not documents:
                fail('代码修改缺少当前文档原文。')
            entry = {'code_reference': ref, 'path': code['path'],
                'code_text': text[ref['offset']:end], 'implementation_text': documents}
            if binding and (binding.get('regression') or {}).get('path') == code['path']:
                previous = next((p for p in (context.get(FIELD) or {}).get('previous_artifacts', [])
                                 if p['path'] == code['path']), None)
                if previous:
                    # Full before/after diff for this file is evidence, never an
                    # agent's claim that its selected line was actually changed.
                    import difflib
                    old = _code_text(engine, run_id, previous['artifact_id'], previous)
                    entry['change_diff'] = ''.join(difflib.unified_diff(old.splitlines(True),
                        text.splitlines(True), fromfile='previous/' + code['path'], tofile='current/' + code['path']))
                else:
                    entry['change_diff'] = 'New file:\n' + text
            evidence.append(entry)
        mapped = next((r for r in context['implementation_alignment_manifest_requirements']
                       if r['id'] == row['requirement_id']), None)
        if not mapped:
            fail('修改项缺少实际清单中的验收映射。')
        items.append({**issue, **row, 'acceptance_check': requirements[row['requirement_id']],
                      'allowed_paths': sorted({p.split(':', 1)[0] for p in mapped['locations']}
                          | {e['path'] for e in evidence}),
                      'host_evidence': evidence})
    deferred = [item for item in items if item['disposition'] != 'repair_code']
    items = [item for item in items if item['disposition'] == 'repair_code']
    if not items and not allow_empty:
        fail('原评审已补查，但尚无有证据支持的代码修改项；' + '；'.join(
            f"{i['role']}/{i['requirement_id']} [{i['disposition']}] {i['reason'][:300]}"
            for i in deferred[:4]))
    packet = {'version': VERSION, 'source_sha256': sha(source), 'spec_hash': source['spec_hash'],
        'implementation_document': source['implementation_document'],
        'delivery_hash': source['delivery_hash'], 'items': items,
        **({'deferred_items': deferred} if deferred else {}),
        'review_scope': source['review_scope'],
        'repair_receipt_schema': RECEIPT_SCHEMA,
        'instruction': '仅修复本清单原问题；逐条遵守所引当前实现文档及原验证标准，保留已正确实现。'
            '不能修改批准文档、降低验收门槛或依据历史建议重写。发现矛盾立即报告，不擅自选边。'
            + DELIVERY_GUIDANCE +
            '按 repair_receipt_schema 写 repair-results.json，为每个 issue_id 提供实际变更文件、验证命令、结果和未验证项；'
            'verification 逐字沿用原标准（多项标准用换行拼接），details 记录退出码及真实输出文件位置。'
            'implementation.json 的 delivery_files 必须包含 repair-results.json；它不是 runtime_files。'
            '不能伪造执行记录。'
            'repair_contract_sha256 使用修改指令指定的完整 SHA256。'
            '宿主仍独立核验交付、执行固定本地验收并由原角色复审，作者自报不是通过。'}
    if deferred:
        packet['instruction'] += ('deferred_items 是保留给原评审的未决意见，不是代码修改授权；'
            '不得据其改动科学方案或验收标准，也不得宣称它们已经解决。')
    from .code_repair_audit_evidence import enrich
    return enrich(engine, run_id, source, packet)


def validate_audit(value, packet, *, require_approval=True):
    from .intelligence import parse_json
    parse_json(json.dumps(value, ensure_ascii=False), AUDIT_SCHEMA)
    expected = {i['issue_id'] for i in packet['items']}
    if (value['packet_sha256'] != sha(packet) or len(value['checks']) != len(expected)
            or {r['issue_id'] for r in value['checks']} != expected
            or (value['approve'] is True and any(r['supported'] is not True for r in value['checks']))
            or (require_approval and (value['approve'] is not True
                or any(r['supported'] is not True for r in value['checks'])))):
        fail('独立核对未确认全部修改均有文档依据且相互一致。')


def audit_context(packet):
    context = {'code_repair_protocol': VERSION, 'indexed_document_context': True,
               'packet_sha256': sha(packet)}
    if packet.get('audit_revision'):
        from .code_repair_audit_view import view
        compact = view(packet)
        context.update(audit_view_version=2, repair_packet_view=compact, audit_view_sha256=sha(compact))
    else:
        context['repair_packet'] = packet
    return context


def audit_task(packet):
    if packet.get('audit_revision'):
        return ('独立核对原作者定向纠正后的修改清单，不能以作者自报代替审查。'
            'repair_packet_view 按原问题、实现文档具体位置、实际代码位置、修改办法组织，'
            'documents/code_files.excerpts 仅提供标明范围的真实片段，目录不能冒充已读全文。'
            '对照原 original_standards、当前 fixes 及 amendment 理由和引用，'
            '确认每条修改符合批准规范，没有提高门槛、遗漏必要子项或引入矛盾。'
            'previous_audit 保留上轮逐项结论，只复核被质疑的原要求和本次纠正引起的实际冲突；'
            '仅本角色此前已独立支持且未变更的要求沿用原结论，不借此新增普通问题；'
            '他人的支持不能替代你自己的独立核对。'
            'checks 必须覆盖当前 items 的全部 issue_id；deferred_items 保留未决，'
            '不为其返回 supported，不授权据其修改，也不宣称已经解决。'
            '只有全部修改有依据且相互兼容才 approve=true，不确定项 supported=false 并指出需要补读的具体文档或代码范围。'
            'packet_sha256 逐字使用请求中的完整清单哈希，不使用去重视图哈希。'
            '本轮不改代码、文档或代替代码验收。')
    task = AUDIT_TASK
    if packet.get('audit_sources'):
        task += ('\n补齐的 audit_sources 是宿主从同一冻结交付、批准实现文档及采用的科学定义读取的完整正文。'
            'segments 的 provided_text 复用本包精确引文，其余 text 补齐全文，不得把指针当作缺少正文。'
            '先结合全文核对原指令及继承关系。named_delivery_paths 修正了原问题已指明但清单漏登的候选文件，'
            '它们已列入本项 allowed_paths；仍须核对这些文件与该原问题的实际关系，不能扩展到其他文件。')
    if packet.get('clarification'):
        task += ('\n本轮包含原角色的意见澄清。review_scope.issues 保留原问题和原标准，'
            'clarification.votes 保留逐项回应；核对纠正后的要求是否忠实于批准条款，'
            '不得新增验收标准或把作者撤回意见直接等同于科研批准。')
    if packet.get('deferred_items'):
        task += ('\nchecks 仅覆盖 repair_packet.items 中的代码修改项。deferred_items 是待复核意见，'
            '不为其返回 supported、不授权据其修改、也不宣称已经解决。'
            '检查本次修改是否与这些未决意见存在实际冲突；未决本身不否定其他有独立依据的修改。')
    return task


def validate_dispatch(engine, run_id, state, attempt):
    """Crash/restart cannot bypass the plan or its two sealed audit decisions."""
    from .engine import Harness
    if hashlib.sha256(attempt['instruction'].encode()).hexdigest() != attempt.get('instruction_sha256'):
        fail('已保存的 Codex 指令发生变化。')
    if attempt.get('definition_repair_id'):
        from .implementation_definition_repair import validate_dispatch as validate_definition_dispatch
        return validate_definition_dispatch(engine, run_id, state, attempt)
    expected = attempt.get('repair_contract_sha256')
    if not expected:
        return
    base = previous_attempt(state, attempt['index'], reviewed=True)
    packet = (base or {}).get('repair_contract')
    audits = (base or {}).get('repair_audits', [])
    if (not packet or sha(packet) != expected or len(audits) != 2
            or {v.get('role') for v in audits} != {'director', 'scout'}):
        fail('修改指令缺少同版本的独立核对。')
    if packet.get('clarification'):
        from .code_repair_clarification import verify_saved
        source = verify_saved(engine, run_id, state, base, packet['clarification'])
        if sha(source) != packet['source_sha256']:
            fail('修改指令未绑定已核验的意见澄清。')
    if packet.get('audit_revision'):
        from .code_repair_amendment import verify
        verify(engine, run_id, packet)
    context = audit_context(packet)
    instruction = audit_task(packet)
    for audit in audits:
        if audit.get('tool_review'):
            from .code_repair_tool_review import authenticate
            authenticate(engine, run_id, packet, audit)
            validate_audit(audit['result'], packet)
            continue
        purpose = 'code_repair_audit:' + expected
        hid = Harness.decision_key(run_id, audit['role'], purpose, instruction, context, AUDIT_SCHEMA)
        decision = engine.store.get('decisions', hid, {})
        frozen = engine.store.get('handoff_inputs', hid, {})
        handoff = engine.store.get('handoffs', hid, {})
        payload = {'instruction': instruction, 'context': context, 'schema': AUDIT_SCHEMA}
        if (audit.get('handoff_id') != hid or decision.get('result') != audit['result']
                or decision.get('run_id') != run_id or decision.get('role') != audit['role']
                or {k: frozen.get(k) for k in payload} != payload or frozen.get('run_id') != run_id
                or handoff.get('status') != 'completed' or handoff.get('input_hash') != sha(payload)
                or handoff.get('output_hash') != sha(audit['result'])):
            fail('独立核对结论或冻结输入已变化，不能恢复派发。')
        validate_audit(audit['result'], packet)


async def prepare(engine, h, run_id, state, specification, attempt, save):
    """Once per immutable rejection; completed decisions are replayed by Harness."""
    source = await engine._read_consensus_check(run_id, build_source,
        engine, run_id, state, specification, attempt)
    engine.check_active(run_id)
    packet = attempt.get('repair_contract')
    if packet:
        # Delivery/receipt corrections do not reopen an already sealed plan.
        # Reauthenticate the actual sources and all independent votes, while
        # preserving historical packet text even after host formatting changes.
        if not packet.get('clarification') and packet.get('source_sha256') != sha(source):
            fail('已保存修改清单不属于当前冻结来源。')
        await engine._read_consensus_check(run_id, validate_dispatch, engine, run_id, state,
            {'index': attempt['index'] + 1, 'instruction': '',
             'instruction_sha256': hashlib.sha256(b'').hexdigest(), 'repair_contract_sha256': sha(packet)})
        return copy.deepcopy(packet)
    original = source
    from .code_followup import enabled as followup_enabled, prepare as prepare_followup
    if followup_enabled(engine, run_id, state):
        source, proposal, clarification = await prepare_followup(
            engine, h, run_id, state, specification, attempt, source, save)
    else:
        source, proposal, clarification = await _legacy_proposal(
            engine, h, run_id, state, specification, attempt, source, save)
    if not source['issues']:
        return None
    packet = await engine._read_consensus_check(run_id, materialize, engine, run_id, source, proposal,
        allow_deferred=clarification is not None or followup_enabled(engine, run_id, state))
    if clarification:
        packet['clarification'] = clarification
    return await _audit_and_save(engine, h, run_id, state, specification, attempt, original, packet, clarification, save)


async def _legacy_proposal(engine, h, run_id, state, specification, attempt, source, save):
    identity = sha(source)
    # Full frozen contexts stay in SQLite; the mapper only needs source locators.
    view = {k: v for k, v in source.items() if k != 'source_contexts'}
    proposal = await h.ask(run_id, 'engineer', 'code_repair_map:' + identity,
        PLAN_TASK, {'code_repair_protocol': VERSION, 'indexed_document_context': True,
                    'source_sha256': identity, 'repair_source': view}, PLAN_SCHEMA)
    clarification = None
    from .code_repair_clarification import clarify, mapping_problems
    if any(row['disposition'] != 'repair_code' for row in proposal['items']) or mapping_problems(source, proposal):
        source, proposal, clarification = await clarify(
            engine, h, run_id, state, specification, attempt, source, proposal, save)
        if not source['issues']:
            # All three original roles re-read and approved the unchanged
            # delivery. The caller completes that delivery without a fake edit.
            return source, proposal, clarification
    return source, proposal, clarification


async def _audit_and_save(engine, h, run_id, state, specification, attempt, original, packet, clarification, save):
    from .code_repair_amendment import amend, fingerprint
    visited = set()
    while True:
        mark = fingerprint(packet)
        if mark in visited:
            fail('定向纠正后回到了同一修改要求，停止重复审查并保留原分歧。')
        visited.add(mark)
        audits = []
        instruction, context = audit_task(packet), audit_context(packet)
        if packet.get('audit_sources'):
            identity = sha(packet)
            saved = engine.store.get('code_repair_packets', identity)
            record = {'run_id': run_id, 'packet': packet}
            if saved is not None and saved != record:
                fail('已保存的修改清单发生变化。')
            if saved is None:
                engine.store.put('code_repair_packets', identity, record)
        for role in ('director', 'scout'):
            from .code_repair_tool_review import review
            audit = await review(engine, h, run_id, role, packet)
            vote = audit['result']
            validate_audit(vote, packet, require_approval=False)
            if any(not row['supported'] for row in vote['checks']):
                packet = await amend(engine, h, run_id, state, attempt, packet, audit, save)
                break
            validate_audit(vote, packet)
            audits.append(audit)
        else:
            break
    engine.check_active(run_id)
    # Reauthenticate sources after the async reviews and before dispatch authority.
    current = await engine._read_consensus_check(run_id, build_source,
        engine, run_id, state, specification, attempt)
    if sha(current) != sha(original):
        fail('独立核对期间原输入变化。')
    if clarification:
        from .code_repair_clarification import verify
        await engine._read_consensus_check(run_id, verify,
            engine, run_id, state, specification, attempt, original, clarification)
    attempt['repair_contract'] = packet
    attempt['repair_audits'] = audits
    save()
    return packet


def validate_delivery(state, attempt):
    """Bind author checks and actual changed files; never treat claims as tests."""
    expected = attempt.get('repair_contract_sha256')
    if not expected:
        return None  # Historical dispatched work keeps its original contract.
    from pathlib import Path, PurePosixPath
    from .intelligence import parse_json, ProviderError
    from .implementation_failures import ImplementationDeliveryCorrection
    previous = [a for a in state['attempts'] if a['index'] < attempt['index'] and a.get('repair_contract')]
    if not previous:
        fail('修改交付缺少原指令。')
    base = max(previous, key=lambda a: a['index'])
    packet = base['repair_contract']
    if sha(packet) != expected:
        fail('原修改清单发生变化。')
    path = Path(attempt['snapshot']['workspace']) / 'repair-results.json'
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ImplementationDeliveryCorrection('缺少 repair-results.json，或回执超过 256 KiB。',
                                               diagnostic='repair_receipt_missing')
    try:
        result = parse_json(path.read_text(encoding='utf-8'), RECEIPT_SCHEMA)
    except (ValueError, ProviderError) as exc:
        raise ImplementationDeliveryCorrection('repair-results.json 不符合已派发的回执格式。',
                                               diagnostic='repair_receipt_format') from exc
    known = {r['issue_id']: r for r in packet['items']}
    if (result['repair_contract_sha256'] != expected or len(result['items']) != len(known)
            or {r['issue_id'] for r in result['items']} != set(known)):
        fail('修改回执未完整对应原清单及其版本。')
    before = {r['path']: r['sha256'] for r in base['snapshot']['files']}
    after = {r['path']: r['sha256'] for r in attempt['snapshot']['files']}
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)} - {'repair-results.json'}
    allowed = {'implementation.json', *(p for item in known.values() for p in item['allowed_paths'])}
    def test_path(p):
        name = PurePosixPath(p)
        return (len(name.parts) > 1 and name.as_posix() == p and '\\' not in p and ':' not in p
                and not name.is_absolute() and '..' not in name.parts and name.parts[0] == 'tests')
    # Delivery metadata is required by the directory contract but is not
    # permission to change unrelated runtime code. Only declared, non-runtime
    # reports are included; a folder name alone is never an exemption.
    auxiliary = set()
    manifest_path = path.parent / 'implementation.json'
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        declared = set(manifest.get('delivery_files', []))
        runtime = set(manifest.get('runtime_files', []))
        if 'README.md' in declared and 'README.md' not in runtime:
            auxiliary.add('README.md')
        for name in manifest.get('test_result_files', []):
            relative = PurePosixPath(name)
            if (name in declared and name not in runtime and len(relative.parts) > 1
                    and relative.parts[0] == 'test-results' and relative.as_posix() == name
                    and '..' not in relative.parts and '\\' not in name and ':' not in name
                    and relative.suffix.lower() in {'.json', '.txt', '.md', '.xml', '.csv'}):
                auxiliary.add(name)
    unexpected = sorted(p for p in changed if p not in allowed | auxiliary and not test_path(p))
    if unexpected:
        raise ImplementationDeliveryCorrection(
            '以下文件超出原清单范围，请撤回这些额外改动并保留已授权修复：' + '、'.join(unexpected),
            diagnostic='repair_scope_escape', unexpected_paths=unexpected)
    if not changed - auxiliary:
        raise ImplementationDeliveryCorrection('交付没有原清单范围内的实际修改。', diagnostic='repair_no_change')
    problems = []
    for row in result['items']:
        item = known[row['issue_id']]
        if row['verification'] != '\n'.join(f['verification'] for f in item['fixes']):
            fail('修改回执改变了原问题的验证标准。')
        invalid = [p for p in row['changed_files'] if p not in changed or p not in after
                   or (p not in item['allowed_paths'] and p not in auxiliary
                       and p != 'implementation.json' and not test_path(p))]
        if invalid:
            problems.append({'kind': 'repair_file_claim', 'issue_id': row['issue_id'],
                             'invalid_paths': invalid})
    if problems:
        raise ImplementationDeliveryCorrection(
            '逐项交付回执需要修正：' + '；'.join(p['issue_id'] + '：' +
                ('文件对应关系越出本项范围 ' + '、'.join(p['invalid_paths']) if 'invalid_paths' in p else '已执行检查失败')
                for p in problems) + '。保留其他条目已授权的修复；不得伪造对应关系或验证通过。',
            diagnostic='repair_receipt_checks', problems=problems)
    return {'repair_contract_sha256': expected, 'receipt_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'changed_paths': sorted(changed), 'issue_ids': sorted(known),
        'author_verification': copy.deepcopy(result['items']),
        'pending_issue_ids': [r['issue_id'] for r in result['items'] if r['outcome'] == 'not_run'],
        'failed_issue_ids': [r['issue_id'] for r in result['items'] if r['outcome'] == 'failed'],
        'verification_passed': False,
        'scope': '仅核对作者回执身份和真实文件变化；不证明测试实际执行或代码正确，仍须宿主验收与三方独立复审。'}


def reject_delivery(state, attempt, error):
    """Keep the immutable attempt and route only correctable delivery failures."""
    from .implementation_failures import ImplementationCodeBlocked
    diagnostic = {**copy.deepcopy(error.failure['details']),
        'repair_contract_sha256': attempt.get('repair_contract_sha256'),
        'snapshot_sha256': attempt['snapshot']['files_hash']}
    fingerprint = sha(diagnostic)
    if any(a['index'] < attempt['index'] and a.get('status') != 'superseded'
           and a.get('delivery_correction', {}).get('fingerprint') == fingerprint for a in state['attempts']):
        raise ImplementationCodeBlocked('同一交付内容重复触发相同修正错误，停止无进展重试；请核对具体文件诊断。',
            kind='codex_code_no_progress', details={'workflow_id': state['id'], 'attempt': attempt['index'], **diagnostic})
    attempt.update(status='rejected', issues=[str(error)],
                   delivery_correction={**diagnostic, 'fingerprint': fingerprint})


def unchanged_failed_execution(engine, run_id, state, attempt, specification):
    """Do not pay for another receipt edit around an unchanged, actual failure.

    This is a stop-only guard, never permission to reuse a success or skip a
    review. Reauthenticate immutable code, author receipt and the original host
    check. Changed runtime code and missing/unbound evidence use normal checks.
    """
    if (not attempt.get('snapshot') or not attempt.get('repair_contract_sha256')
            or attempt.get('checkpoint', {}).get('status') != 'completed'):
        return None
    correction = attempt.get('delivery_correction')
    if correction and (correction.get('diagnostic') != 'repair_receipt_checks'
            or any(p.get('kind') != 'repair_check_failed' for p in correction.get('problems', []))):
        return None
    from .codex_workflow import _verify_snapshot
    from .codex_delivery import inspect_delivery
    from .execution_diagnostics import failure_diagnostic, failure_message
    from .implementation_failures import ImplementationDeliveryCorrection
    from pathlib import Path
    _verify_snapshot(engine, run_id, attempt['snapshot'])
    try:
        receipt = validate_delivery(state, attempt)
    except ImplementationDeliveryCorrection:
        return None  # Actual receipt defects still take their ordinary repair path.
    if not receipt or not receipt['failed_issue_ids']:
        return None
    current = inspect_delivery(Path(attempt['snapshot']['workspace']), specification['spec'])
    if not current.get('approved'):
        return None
    runtime_sha = (current.get('runtime_bundle') or {}).get('sha256') or current.get('code_sha256')
    working = inspect_delivery(Path(state['workspace']), specification['spec'])
    if not working.get('approved') or ((working.get('runtime_bundle') or {}).get('sha256')
            or working.get('code_sha256')) != runtime_sha:
        return None  # A new source edit must be snapshotted and checked normally.
    for prior in sorted(state['attempts'], key=lambda a: a['index'], reverse=True):
        acceptance = prior.get('acceptance', {})
        if (prior['index'] >= attempt['index'] or prior.get('status') == 'superseded'
                or acceptance.get('runtime_sha256') != runtime_sha or acceptance.get('approved') is not False
                or not prior.get('snapshot')):
            continue
        _verify_snapshot(engine, run_id, prior['snapshot'])
        original = inspect_delivery(Path(prior['snapshot']['workspace']), specification['spec'])
        original_sha = (original.get('runtime_bundle') or {}).get('sha256') or original.get('code_sha256')
        if original_sha != runtime_sha:
            continue
        for smoke in acceptance.get('smoke', []):
            check_key = run_id + ':' + str(smoke.get('id'))
            check = engine.store.get('codex_checks', check_key, {})
            if (check.get('run_id') != run_id or check.get('id') != smoke.get('id')
                    or check.get('provenance') != 'real_experiment_execution'
                    or check.get('status') != 'failed' or type(check.get('exit_code')) is not int
                    or check['exit_code'] == 0
                    or any(check.get(k) != smoke.get(k) for k in ('status','exit_code','stdout','stderr','params'))):
                continue
            diagnostic = failure_diagnostic(check)
            if (diagnostic and diagnostic.get('detail_reason') == 'timing_coverage_insufficient'
                    and diagnostic.get('missing')):
                return {'workflow_id': state['id'], 'attempt': attempt['index'],
                    'source_attempt': prior['index'], 'source_check': check_key,
                    'snapshot_sha256': attempt['snapshot']['files_hash'], 'runtime_sha256': runtime_sha,
                    'repair_receipt_sha256': receipt['receipt_sha256'],
                    'failed_issue_ids': receipt['failed_issue_ids'], 'failure_diagnostic': diagnostic,
                    'message': '原检查失败且运行代码未改变；仅修改回执不能解决该失败，已停止重复派发。'
                        + failure_message(diagnostic)}
    return None
