"""Finite, evidence-bound implementation-document acceptance protocol.

This protocol authorizes a document stage only. It never grants scientific or
execution approval. Historical indexed-review assignments retain their rules.
"""
from __future__ import annotations

import copy

from .implementation_review_routing import digest
from .storage import now

PROTOCOL = 'implementation_acceptance_protocol'
FIELD = 'acceptance_review'
CONTEXT = 'implementation_acceptance'
VERSION = 1
TERMINAL = {'APPROVED', 'APPROVED_WITH_NOTES', 'BLOCKED', 'NEEDS_PROBE', 'NEEDS_REPLAN'}
APPROVED = {'APPROVED', 'APPROVED_WITH_NOTES'}
ACTIVE_ISSUES = {'PROPOSED', 'OPEN', 'PATCHED', 'VERIFIED'}
DEFAULT_BUDGET = {'initial_discovery_passes': 1, 'max_repair_verify_cycles': 2,
    'max_dispute_exchanges_per_issue': 1, 'max_probe_attempts_per_issue': 1,
    'max_calls': 240, 'max_seconds': 14400, 'tool_timeout_seconds': 90,
    'allow_new_blockers_with_evidence': True, 'allow_auto_approval_on_budget_exhaustion': False}
# Stable modules are structured SPEC fields; dependencies propagate invalidation.
CRITERIA = [
    {'id': 'C01', 'title': '研究目标与范围', 'reviewer': 'director',
     'fields': ['summary', 'limitations'], 'depends_on': []},
    {'id': 'C02', 'title': '方法、公式、假设与来源', 'reviewer': 'scout',
     'fields': ['algorithm', 'architecture_traceability'], 'depends_on': ['C01']},
    {'id': 'C03', 'title': '模块、接口、形状与实现路径', 'reviewer': 'director',
     'fields': ['algorithm', 'parameter_contract', 'implementation_map', 'comparison_transport'], 'depends_on': ['C02']},
    {'id': 'C04', 'title': '数据划分、模型选择、指标与对照', 'reviewer': 'director',
     'fields': ['data_contract', 'branches', 'metrics', 'comparison_transport'], 'depends_on': ['C02']},
    {'id': 'C05', 'title': '配置、随机性、环境、日志与复现', 'reviewer': 'director',
     'fields': ['parameter_contract', 'dependencies'], 'depends_on': ['C03', 'C04']},
    {'id': 'C06', 'title': '测试映射、失败判据与后续证据', 'reviewer': 'director',
     'fields': ['acceptance_checks', 'implementation_map', 'limitations'], 'depends_on': ['C03', 'C04', 'C05']},
]
HARD_CONSTRAINTS = ['保留批准方案与冻结参数、数据划分及资源边界',
    '禁止测试集选模或调参、数据泄漏及伪造结果', '文档计划、代码验收与实验结论分阶段',
    '作者不能独立关闭问题；已确认 P0/P1 不得风险豁免', '未知证据不能计为通过']
CONSISTENCY = ['parameters', 'symbols', 'interfaces', 'metrics', 'configuration']

TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 6000, 'pattern': r'\S'}
SHA = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}


def obj(fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}


def array(items, minimum=0, maximum=64):
    return {'type': 'array', 'items': items, 'minItems': minimum, 'maxItems': maximum}


REF = obj({'document_id': TEXT, 'document_sha256': SHA, 'section_id': TEXT,
           'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 6000}})
REFS = array(REF, 1)
STATUS = {'enum': ['PASS', 'FAIL', 'UNKNOWN']}
CONTRACT_SCHEMA = obj({'goal': TEXT, 'scope': TEXT, 'non_goals': array(TEXT, 1),
    'criteria': array(obj({'id': {'enum': [c['id'] for c in CRITERIA]}, 'requirement': TEXT,
                          'evidence_required': TEXT}), 6, 6), 'source_refs': REFS, 'change_reason': TEXT})
ISSUE = obj({'local_id': TEXT, 'criterion_id': {'enum': [c['id'] for c in CRITERIA]},
    'severity': {'enum': ['P0', 'P1', 'P2']}, 'claim': TEXT, 'evidence': REFS, 'impact': TEXT,
    'minimal_fix': TEXT, 'close_when': array(TEXT, 1, 16),
    'reopen_issue_id': {'type': ['string', 'null']},
    'new_evidence_reason': {'type': 'string', 'maxLength': 6000},
    'needs': {'enum': ['REPAIR', 'PROBE', 'REPLAN', 'NOTE']}})
CHECK = obj({'criterion_id': TEXT, 'status': STATUS, 'evidence': array(REF),
             'issue_ids': array(TEXT), 'missing_information': {'type': 'string', 'maxLength': 6000}})
VERIFY = obj({'issue_id': TEXT, 'conditions': array(obj({'index': {'type': 'integer', 'minimum': 0},
    'status': STATUS, 'evidence': array(REF), 'explanation': TEXT}), 1, 16)})
REPORT_SCHEMA = obj({'contract_sha256': SHA, 'document_sha256': SHA, 'checks': array(CHECK),
    'issues': array(ISSUE), 'verifications': array(VERIFY),
    'consistency': array(obj({'id': {'enum': CONSISTENCY}, 'status': STATUS,
                             'evidence': array(REF), 'explanation': TEXT}))})
PROBE_PLAN = obj({'input_refs': REFS, 'question': TEXT, 'procedure': TEXT,
    'pass_condition': TEXT, 'failure_condition': TEXT, 'required_artifact': TEXT,
    'max_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 120},
    'failure_exit': {'const': 'BLOCKED'}})
DECISION_RECORD = obj({'decision_id': TEXT, 'criterion_id': {'enum': [c['id'] for c in CRITERIA]},
    'alternatives': array(TEXT, 2, 12), 'chosen': TEXT, 'reason': TEXT,
    'valid_when': TEXT, 'reopen_when': TEXT, 'evidence': REFS})
ADJUDICATION_SCHEMA = obj({'decisions': array(obj({'issue_id': TEXT,
    'action': {'enum': ['OPEN', 'REJECTED', 'DUPLICATE', 'DEFERRED', 'NEEDS_PROBE', 'NEEDS_REPLAN']},
    'duplicate_of': {'type': ['string', 'null']}, 'reason': TEXT, 'evidence': REFS,
    'probe_plan': {'anyOf': [{'type': 'null'}, PROBE_PLAN]}})), 'decision_records': array(DECISION_RECORD)})
PATCH = obj({'issue_id': TEXT, 'changed_fields': array(TEXT), 'impact_analysis': TEXT,
             'evidence': REFS, 'response': TEXT})

GUIDANCE = '''
你的任务是检查本阶段冻结 acceptance_contract，不能自行增加标准、扩大范围或重置预算。
实现文档只须描述如何检验假设，不要求提前证明科研成功。代码与实验结论另行验收。
仅检查 assigned_criteria，每项返回 PASS/FAIL/UNKNOWN。每个 PASS 的 evidence 必须同时包含已真实读取的当前版本原文和 indexed_documents 中 kind=architecture_design 的批准来源精确范围。
其他验收项或 verifications 内的来源引用不能代替本项引用；只选语义相关的批准来源，证据不足返回 UNKNOWN，不能为了格式随意引用。
close_when 只列作者能够修订满足的文档条件；独立读取、复核和关闭回执属于后续审查流程，不得要求作者先取得回执才能送审。
FAIL 关联问题 local_id 或已有 issue_id；UNKNOWN 说明缺失信息，不能伪装 PASS。
没有问题时 issues=[]。P0 是科学有效性或不可豁免约束的严重缺陷；P1 必须违反冻结必要项；
P2 为非阻断建议。每项含精确证据、实质影响、最小修改和可核验 close_when。
任何角色都能报告跨职责的严重缺陷，但须由总监统一准入，不能直接命令作者修订。
VERIFY 仅核验指定问题的关闭条件及受影响依赖；新发现的旧缺陷也可提交，须证明违反既定要求。
新论文、额外实验、表达偏好和可选优化进入 P2。重开须引用原 issue_id 和新反例/失效证据。
作者“已修改”只代表 PATCHED。逐项核验 conditions，缺证据返回 UNKNOWN。
所有引用必须通过 documents.read 读取；目录、摘要、他人结论和保存回执不能冒充阅读。
按 required_sections 分页完成本职责范围，可分多次调用；同时核对相关批准来源，不必重审未失效模块。
总监的一致性检查只覆盖 parameters/symbols/interfaces/metrics/configuration 的既定一致性。
'''
AUTHOR_GUIDANCE = '''
实现文档采用冻结验收合同。你是唯一文档作者，自检不属于独立审核。
只按 acceptance_issues 中已裁决的 OPEN 问题作最小相关修改，保留未受影响字段。
修订提交 patch_reports：准确 issue_id、changed_fields、影响分析、当前原文证据及回应。
changed_fields 可填章节字段名或该版本实际修改单元的 JSON Pointer（如 /acceptance_checks/2/verification）。
同一精确位置也接受省略首个斜杠的数字索引路径；不接受模糊 ID、通配符或省略层级。
索引修订使用目录解析后的真实路径；完整覆盖相对上一审查版本的修改，不能漏报其他条目或声明未修改的条目。
不得自行关闭问题。全文结构化 SPEC 仍须保存为不可变版本；保存后由职责审查者定向验证。
本协议取代旧提示中的三方全文重审/三票通过要求；代码交付后的独立代码审查仍按原合同执行。
'''
AUTHOR_GUIDANCE += '''
修订先看完全部 acceptance_issues 和 close_when，集中列出全部修改位置，再一次批量保存全部相关索引单元。
允许先落地已完成部分，保存失败仅重试对应位置，遗漏项继续补写；全部意见完成后才提交下一轮审核。
acceptance_completion 逐条件标 complete/incomplete/blocked 并提供真实当前稿原文自检。
全部自检完成后才提交独立审核；中间稿继续同一作者修订，不增加审核轮次。
缺少决定性来源或宿主依赖须明确说明，先完成其余可修项，不得编造完成或科学批准。
'''


def enabled(value):
    return type(value.get(PROTOCOL)) is int and value[PROTOCOL] == VERSION


def fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现文档验收：' + message)


def sealed(value):
    return {**value, 'sha256': digest(value)}


def check_seal(value):
    if not isinstance(value, dict) or value.get('sha256') != digest({k: v for k, v in value.items() if k != 'sha256'}):
        fail('冻结合同或证据指纹已改变。')


def contract(result, state, source, version=1, *, reason='initial', previous_sha256=None):
    from .prose_schema import validate
    validate(result, CONTRACT_SCHEMA)
    if sorted(c['id'] for c in result['criteria']) != [c['id'] for c in CRITERIA]:
        fail('合同必须唯一覆盖 C01 至 C06。')
    by_id = {c['id']: c for c in result['criteria']}
    return sealed({'version': version, 'phase': 'implementation_document',
        'goal': result['goal'], 'scope': result['scope'], 'non_goals': result['non_goals'],
        'criteria': [{**copy.deepcopy(c), **by_id[c['id']], 'required': True} for c in CRITERIA],
        'non_waivable': HARD_CONSTRAINTS, 'consistency_checks': CONSISTENCY,
        'proposal_hash': state['proposal_hash'], 'design_hash': digest(state['common']['design']),
        'experiment_plan_hash': digest(state['common'].get('experiment_plan') or {}),
        'source_sha256': source['sha256'], 'source_refs': result['source_refs'],
        'budget': copy.deepcopy(state[FIELD]['budget']), 'change_reason': reason,
        'previous_sha256': previous_sha256, 'affected_criteria': [c['id'] for c in CRITERIA]})


def criterion_fields(contract, criterion_id):
    by_id = {c['id']: c for c in contract['criteria']}
    seen, fields = set(), set()
    def visit(cid):
        if cid in seen:
            return
        seen.add(cid)
        fields.update(by_id[cid]['fields'])
        for dependency in by_id[cid]['depends_on']:
            visit(dependency)
    visit(criterion_id)
    return fields


def fingerprint(contract, spec, criterion_id):
    return digest({'contract': contract['sha256'],
                   'fields': {k: spec.get(k) for k in sorted(criterion_fields(contract, criterion_id))}})


def required_sections(row, contract, criteria):
    from .implementation_document_readability import HEADINGS
    # These structured fields are rendered outside the core prose headings.
    # Keep this read mapping separate: extending HEADINGS would render twice.
    headings = {**HEADINGS, 'architecture_traceability': '架构要求与实现验收对应',
                'comparison_transport': '比较数据传递合同'}
    fields = set().union(*(criterion_fields(contract, c) for c in criteria)) if criteria else set()
    titles = fields | {headings.get(field, field) for field in fields}
    sections = row['document']['sections']
    selected = [s for s in sections if s['title'].strip('# ') in titles]
    matched = {field for field in fields if any(s['title'].strip('# ') in {field, headings.get(field, field)} for s in selected)}
    if (fields & set(row['spec'])) - matched:
        return sections  # Unknown layout: conservatively widen, never infer coverage.
    return selected


def transition(ledger, issue, status, *, actor, evidence=None, reason=''):
    prior = issue['status']
    issue['status'] = status
    ledger['events'].append(sealed({'sequence': len(ledger['events']), 'issue_id': issue['issue_id'],
        'from': prior, 'to': status, 'actor': actor, 'evidence': copy.deepcopy(evidence or []),
        'reason': reason, 'previous_sha256': ledger['events'][-1]['sha256'] if ledger['events'] else None}))


def bind_document(ledger, row):
    """Keep approvals only while their module, dependency and source hashes match."""
    contract = ledger['contract']
    invalid = [c['id'] for c in contract['criteria'] if ledger['coverage'].get(c['id'], {}).get('fingerprint')
               != fingerprint(contract, row['spec'], c['id'])]
    for cid in invalid:
        ledger['coverage'].pop(cid, None)
    for issue in ledger['issues'].values():
        if issue['status'] == 'CLOSED' and issue['criterion_id'] in invalid:
            transition(ledger, issue, 'PATCHED', actor='controller', reason='module_or_dependency_changed')
    ledger['binding'] = {'spec_hash': row['spec_hash'], 'document_sha256': row['document']['sha256'],
                         'revision': row['index']}
    ledger['phase'] = 'DISCOVER' if row['index'] == 0 else 'VERIFY'
    ledger['consistency'] = []
    return invalid


def propose(ledger, workflow_id, row, role, report, hid):
    ids = []
    if len({item['local_id'] for item in report['issues']}) != len(report['issues']):
        fail('问题 local_id 重复。')
    for item in report['issues']:
        prior_id = item['reopen_issue_id']
        equivalent = [old for old in ledger['issues'].values() if old['criterion_id'] == item['criterion_id']
            and old['claim'].strip().casefold() == item['claim'].strip().casefold()
            and old['close_when'] == item['close_when']]
        if not prior_id and any(old['status'] == 'CLOSED' for old in equivalent):
            fail('已关闭原问题须引用原 issue_id 及新的反例，不能改用新编号重开。')
        if prior_id:
            prior = ledger['issues'].get(prior_id)
            if (not prior or prior['status'] != 'CLOSED' or not item['new_evidence_reason'].strip()
                    or item['evidence'] == prior['evidence']):
                fail('重开必须引用已关闭原问题及新的有效证据。')
            iid = prior_id
            prior['reopen_proposal'] = copy.deepcopy(item)
            transition(ledger, prior, 'PROPOSED', actor=role, evidence=item['evidence'], reason=item['new_evidence_reason'])
        else:
            iid = 'I-' + digest([workflow_id, row['index'], role, item['local_id']])[:16]
            if iid in ledger['issues']:
                fail('同一来源不能重复登记问题。')
            ledger['issues'][iid] = {**copy.deepcopy(item), 'issue_id': iid,
                'document_version': row['spec_hash'], 'document_sha256': row['document']['sha256'],
                'owner': 'engineer', 'verifier': next(c['reviewer'] for c in ledger['contract']['criteria'] if c['id'] == item['criterion_id']),
                'reporter': role, 'source_handoff_id': hid, 'status': 'PROPOSED',
                'confirmed': False, 'dispute_exchanges': 0, 'probe_attempts': 0}
            if equivalent:
                ledger['issues'][iid]['duplicate_candidates'] = [old['issue_id'] for old in equivalent]
            transition(ledger, ledger['issues'][iid], 'PROPOSED', actor=role, evidence=item['evidence'])
        ids.append(iid)
    return ids


def adjudicate(ledger, decisions, proposed_ids, hid):
    if len(decisions) != len(proposed_ids) or {d['issue_id'] for d in decisions} != set(proposed_ids):
        fail('总监裁决必须唯一覆盖全部提议问题。')
    for decision in decisions:
        issue = ledger['issues'][decision['issue_id']]
        action = decision['action']
        if issue['confirmed'] and issue['severity'] in {'P0', 'P1'} and action in {'REJECTED', 'DEFERRED'}:
            fail('已确认阻断问题不能通过裁决豁免。')
        if action == 'DEFERRED' and issue['severity'] != 'P2':
            fail('只有非阻断 P2 可以进入待办。')
        if action == 'DUPLICATE':
            target = ledger['issues'].get(decision['duplicate_of'])
            if (not target or target is issue or target['status'] in {'REJECTED', 'DEFERRED', 'DUPLICATE'}
                    or target['criterion_id'] != issue['criterion_id']
                    or target['status'] == 'CLOSED' or target['severity'] > issue['severity']):
                fail('重复问题必须指向同验收项且仍待解决的同等或更高等级原问题。')
            issue['duplicate_of'] = target['issue_id']
        elif decision['duplicate_of'] is not None:
            fail('非重复裁决不能指定 duplicate_of。')
        if action in {'OPEN', 'NEEDS_PROBE', 'NEEDS_REPLAN'}:
            issue['confirmed'] = True
        if action == 'NEEDS_PROBE':
            if decision.get('probe_plan') is None:
                fail('验证出口必须包含输入、预算、通过条件、失败条件和产物要求。')
            issue['needs'] = 'PROBE'
            issue['probe_task'] = {'status': 'AWAITING_EXECUTION', 'plan': copy.deepcopy(decision['probe_plan']),
                'attempts': 0, 'max_attempts': ledger['budget']['max_probe_attempts_per_issue'],
                'project_budget_continues': True, 'execution_authorized': False}
        elif decision.get('probe_plan') is not None:
            fail('只有 NEEDS_PROBE 可创建验证任务。')
        if action == 'NEEDS_REPLAN':
            issue['needs'] = 'REPLAN'
        issue['adjudication'] = {**copy.deepcopy(decision), 'handoff_id': hid}
        transition(ledger, issue, 'OPEN' if action in {'NEEDS_PROBE', 'NEEDS_REPLAN'} else action,
                   actor='director', evidence=decision['evidence'], reason=decision['reason'])


def apply_report(ledger, row, role, report, hid, *, self_check=False):
    if report['contract_sha256'] != ledger['contract']['sha256'] or report['document_sha256'] != row['document']['sha256']:
        fail('审查结果没有绑定当前合同与文档。')
    if self_check:
        if report['verifications'] or report['consistency']:
            fail('作者自检不能替代问题验证或独立发布检查。')
        return
    for check in report['checks']:
        cid = check['criterion_id']
        if next((c['reviewer'] for c in ledger['contract']['criteria'] if c['id'] == cid), None) != role:
            fail('验收覆盖超出角色职责。')
        if check['status'] == 'PASS' and not check['evidence']:
            fail('PASS 必须有证据。')
        if check['status'] == 'FAIL' and not check['issue_ids']:
            fail('FAIL 必须关联具体问题。')
        if check['status'] == 'UNKNOWN' and not check['missing_information'].strip():
            fail('UNKNOWN 必须说明缺失信息。')
        ledger['coverage'][cid] = {**copy.deepcopy(check), 'role': role, 'handoff_id': hid,
            'fingerprint': fingerprint(ledger['contract'], row['spec'], cid), 'spec_hash': row['spec_hash']}
    for verification in report['verifications']:
        issue = ledger['issues'].get(verification['issue_id'])
        if not issue or issue['verifier'] != role or issue['status'] != 'PATCHED':
            fail('只有独立指定核验者可以验证 PATCHED 问题。')
        conditions = verification['conditions']
        if len(conditions) != len(issue['close_when']) or {v['index'] for v in conditions} != set(range(len(issue['close_when']))):
            fail('须逐项覆盖原关闭条件，不能缩减。')
        if any(v['status'] == 'PASS' and not v['evidence'] for v in conditions):
            fail('关闭条件通过缺少原文证据。')
        issue['verification'] = {'handoff_id': hid, 'conditions': copy.deepcopy(conditions),
            'fingerprint': fingerprint(ledger['contract'], row['spec'], issue['criterion_id']),
            'document_sha256': row['document']['sha256']}
        if all(v['status'] == 'PASS' for v in conditions):
            refs = [r for v in conditions for r in v['evidence']]
            transition(ledger, issue, 'VERIFIED', actor=role, evidence=refs)
            transition(ledger, issue, 'CLOSED', actor='controller', evidence=refs)
        else:
            transition(ledger, issue, 'OPEN', actor=role, reason='close_conditions_not_satisfied')
    if role == 'director':
        ledger['consistency'] = copy.deepcopy(report['consistency'])


def outcome(ledger, row):
    contract = ledger['contract']
    blockers = [i for i in ledger['issues'].values() if i['severity'] in {'P0', 'P1'} and i['status'] in ACTIVE_ISSUES]
    coverage = all((entry := ledger['coverage'].get(c['id'], {})).get('status') == 'PASS'
        and entry.get('fingerprint') == fingerprint(contract, row['spec'], c['id']) for c in contract['criteria'])
    consistency = (len(ledger['consistency']) == len(CONSISTENCY)
        and {c['id'] for c in ledger['consistency']} == set(CONSISTENCY)
        and all(c['status'] == 'PASS' and c['evidence'] for c in ledger['consistency']))
    if not blockers and coverage and consistency:
        return 'APPROVED_WITH_NOTES' if any(i['status'] == 'DEFERRED' or i['severity'] == 'P2'
            and i['status'] in ACTIVE_ISSUES for i in ledger['issues'].values()) else 'APPROVED'
    if any(i['needs'] == 'REPLAN' for i in blockers):
        return 'NEEDS_REPLAN'
    if any(i['needs'] == 'PROBE' for i in blockers):
        return 'NEEDS_PROBE'
    if row['index'] >= ledger['budget']['max_repair_verify_cycles']:
        return 'BLOCKED'
    # Evidence gaps cannot be repaired by forcing prose edits or invented votes.
    if not blockers or any(i['status'] == 'PROPOSED' for i in blockers):
        return 'BLOCKED'
    return 'REPAIR'


def initial_ledger(config):
    budget = {**DEFAULT_BUDGET,
        'max_repair_verify_cycles': config.get('implementation_review_max_revisions', 2),
        'max_calls': config.get('implementation_review_max_calls', 240),
        'max_seconds': config.get('implementation_review_max_seconds', 14400)}
    for field in ('max_repair_verify_cycles', 'max_calls', 'max_seconds'):
        if field != 'max_repair_verify_cycles' and budget[field] is None:
            continue
        if type(budget[field]) is not int or budget[field] < (0 if field == 'max_repair_verify_cycles' else 1):
            fail('新协议必须具有有限非空预算。')
    return {'version': VERSION, 'phase': 'CONTRACT', 'outcome': None, 'budget': budget,
            'contracts': [], 'issues': {}, 'coverage': {}, 'events': [], 'rounds': [], 'consistency': [], 'decision_records': []}


def budget_guard(store, run_id, context, *, consume=False):
    """Project-level monotone counters survive workflow and contract restarts."""
    if not enabled(context):
        return
    from .implementation_review_clock import charge
    exhausted = charge(store, run_id, context['acceptance_budget'], consume=consume, at=now())
    if exhausted:
        workflow_id = context.get('acceptance_workflow_id') or context.get('implementation_document_authoring', {}).get('workflow_id')
        if workflow_id:
            def block(record):
                if record and enabled(record):
                    record[FIELD].update(outcome='BLOCKED', phase='BLOCKED', budget_exhausted=exhausted)
                    record.update(status='blocked', blocked_kind='codex_spec_review_budget_exhausted', reason='；'.join(exhausted))
                return record
            store.mutate('codex_workflows', workflow_id, block)
        from .implementation_failures import ImplementationSpecificationBlocked
        raise ImplementationSpecificationBlocked('；'.join(exhausted), kind='codex_spec_review_budget_exhausted',
            details={'review_outcome': 'BLOCKED', 'scientific_approval': False})


def assert_compiled(context, active):
    """Lossless references are fine; silently dropped contracts/issues are not."""
    if not enabled(context):
        return
    tables = {'$text': active.get('shared_context_text', {}),
              '$catalog_record': active.get('shared_context_catalog_records', {}),
              '$record': active.get('shared_context_records', {})}
    def expand(value, seen=frozenset()):
        if isinstance(value, dict) and len(value) == 1 and next(iter(value)) in tables:
            kind, reference = next(iter(value.items()))
            if not isinstance(reference, str) or reference not in tables[kind] or (kind, reference) in seen:
                fail('编译输入的合同引用缺失或循环。')
            return expand(tables[kind][reference], seen | {(kind, reference)})
        if isinstance(value, dict):
            return {k: expand(v, seen) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v, seen) for v in value]
        return value
    source = expand(active.get('context', {}))
    for key in (PROTOCOL, CONTEXT, 'acceptance_workflow_id', 'acceptance_contract', 'acceptance_budget', 'acceptance_issues',
                'implementation_document_delivery', 'implementation_delivery_correction', 'implementation_adjudication_intake',
                'implementation_read_window_recovery', 'document_read_window_protocol',
                'implementation_acceptance_citation_correction',
                'implementation_acceptance_read_recovery', 'implementation_acceptance_read_plan',
                'acceptance_criteria', 'non_waivable',
                'proposed_issues', 'existing_issues', 'author_response', 'prior_contract', 'decision_records', 'indexed_documents'):
        if key in context and source.get(key) != context[key]:
            fail('编译输入改变了验收合同、问题关闭条件或当前文档范围。')
    if active.get('recent_turns') or active.get('retrieved_memories'):
        fail('定向验收上下文混入未授权历史或自动检索内容。')
