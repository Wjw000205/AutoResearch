"""Versioned, independent Director/Scout review of the complete implementation.

Only the controller adopts this policy. Old packets and scoped rounds are audit
history, never converted into whole-document votes. Code review stays separate.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager

from . import implementation_acceptance as policy
from . import implementation_acceptance_flow as flow
from .async_ownership import gather as gather_owned, cancel as cancel_owned
from .implementation_review_routing import digest
from .storage import now

KEY = 'implementation_whole_document_review'
PROTOCOL = 'implementation_whole_document_review_protocol'
MIGRATIONS = 'implementation_whole_review_migrations'
ROLES = ('director', 'scout')
REPORT_CONTRACT_VERSION = 2
READ_LIMIT = 50000
REQUIREMENT = ('总监和研究员分别结合批准架构独立审核整份实现文档，给出修改意见和精确修改位置；'
               '收齐两份意见后由工程师统一修改并提交审核，两位审核者通过同一版本后进入下一阶段。'
               '请求只给索引，由审核者自行调用工具读完整文档；每次读取最多50000字符。')
POLICY = policy.sealed({'version': 1, 'reviewers': list(ROLES), 'author': 'engineer',
    'scope': 'complete_implementation_and_approved_architecture', 'reading': 'index_then_tools',
    'max_visible_source_chars': READ_LIMIT, 'max_read_batch_chars': READ_LIMIT,
    'requirement': REQUIREMENT, 'scientific_criteria_changed': False})
GUIDANCE = '''
你是本轮独立整篇文档审核者。先看索引，通过 documents.read 分页读完当前实现文档和批准架构。
请求没有预填正文；每批读取与活跃原文窗口最多50000字符。已读正文可以离开窗口，真实阅读账本、
精确版本索引和自己的工作笔记保留；不要仅因正文离开窗口重读全文，必要时定向重开决定性片段。
不能只审自己的模块或接口，不按段落逐个投票；从整体核对方法、接口、形状、配置、实验协议、
资源和测试设计与冻结架构/验收合同是否一致。每项 PASS 仅给必要、语义相关的实现和架构引用，
无需为每个标题、表格行或段落追加引用；完整阅读由宿主阅读回执核验。
问题给出具体影响、最小修改、change_locations 精确位置以及可核验 close_when。文档阶段只要求
可实施的设计，不要求尚未执行的代码或实验结果。P0/P1须违反冻结必要项，P2为非阻断建议。
完整独立审核期间看不到本轮另一位审核者的意见。已有未关闭问题必须逐条件核对；工程师自检
不是独立批准，不能用旧票、保存回执或另一个人的阅读代替你的判断。
UNKNOWN 必须说明实际缺口并主动调用工具补读；确实仍缺失时保留 UNKNOWN，宿主会继续来源澄清。
同时返回 C01-C06 和指定问题的条件核验。verdict=APPROVE 只表示本版本文档通过，不是代码、
实验或科学结论通过。存在未解决的必要缺陷返回 REQUEST_CHANGES；缺决定性原文返回 UNKNOWN。
legacy_findings 是尚未进入旧台账的原始反例，不是旧通过票：PASS 表示本版本中原反例已解决或
经原文核对不成立；FAIL 表示反例仍成立，必须同时提出当前版本具体 P0/P1 问题及修改位置；
UNKNOWN 表示证据仍不足，必须说明缺口。不得把 finding=PASS 理解为确认缺陷成立。
'''
AUTHOR_GUIDANCE = '''
当前采用总监和研究员各自独立整篇审核。收齐两人的全部意见后统一修订；acceptance_issues
给出原问题、精确修改位置和关闭条件。保留未受影响内容，逐项提交 patch_reports 和作者完成自检。
保存完成后由总监、研究员重新对照批准架构独立审核完整新版本，两人均通过后才进入代码阶段。
作者自检不能关闭问题；本次不再分派岗位、同行、接口端点或逐断言文档审核。
'''


def enabled(state):
    return state.get(policy.FIELD, {}).get(KEY, {}).get('version') == 1


def request_enabled(context):
    if PROTOCOL not in context:
        return False
    if type(context[PROTOCOL]) is not int or context[PROTOCOL] != 1:
        policy.fail('Unsupported whole-document review protocol')
    return True


def diagnostic_identity(diagnostic):
    """Wording changes are not progress; authenticated source coverage is."""
    fields = ('kind', 'criterion_id', 'issue_id', 'index', 'id')
    targets = {digest({k: g[k] for k in fields if k in g}): {k: g[k] for k in fields if k in g}
               for g in diagnostic.get('gaps', [])}
    return digest({k: diagnostic.get(k) for k in ('action', 'reason', 'field', 'document_id')} | {
        'targets': [targets[k] for k in sorted(targets)]})


def stagnant_count(attempts):
    latest = attempts[-1]
    identity = diagnostic_identity(effective_diagnostic(latest))
    return sum(a.get('diagnostic') is not None and diagnostic_identity(effective_diagnostic(a)) == identity
               and a.get('reading_signature') == latest.get('reading_signature') for a in attempts)


def adoption_id(state):
    return state['id'] + ':whole-document-policy-v1'


class _WorkflowView:
    """Read-only validation overlay; never patches the controller database."""
    def __init__(self, store, state):
        self._store, self._state = store, state
        self._identity = digest(state)

    def __getattr__(self, name):
        return getattr(self._store, name)

    def get(self, collection, key, default=None):
        if collection == 'codex_workflows' and key == self._state['id']:
            return copy.deepcopy(self._state)
        return self._store.get(collection, key, default)

    def _owned_record(self, collection, key, default=None):
        return self.get(collection, key, default)

    def memoized_read(self, key, build):
        return self._store.memoized_read(('whole_workflow_view', self._identity, key), build)

    @contextmanager
    def read_snapshot(self):
        with self._store.read_snapshot() as snapshot:
            yield _WorkflowView(snapshot, self._state)


def validation_engine(engine, state):
    reader = copy.copy(engine)
    reader.store = _WorkflowView(engine.store, state)
    if getattr(getattr(engine, 'sources', None), 'store', None) is engine.store:
        reader.sources = copy.copy(engine.sources)
        reader.sources.store = reader.store
    return reader


def historical_state(engine, run_id, state, revision):
    baseline(engine, run_id, state)
    archive = engine.store.get(MIGRATIONS, state[policy.FIELD][KEY]['migration_id'])
    legacy = copy.deepcopy(state)
    legacy[policy.FIELD] = copy.deepcopy(archive['baseline_ledger'])
    legacy['specifications'] = copy.deepcopy(archive['baseline_specifications'])
    if revision:
        ledger = legacy[policy.FIELD]
        ledger['rounds'] = [s for s in ledger['rounds'] if s['revision'] < revision]
        reader = validation_engine(engine, legacy)
        restored = flow.replay_ledger(reader, run_id, legacy, compare=False)
        ledger.update(restored)
        row = next(r for r in legacy['specifications'] if r['index'] == revision - 1)
        ledger['outcome'] = policy.outcome(ledger, row)
    return legacy


def validate_author(engine, run_id, state, row):
    from .implementation_authoring import validate_submission
    frozen = engine.store.get('handoff_inputs', row['authoring_handoff_id'], {}).get('context', {})
    if request_enabled(frozen):
        return validate_submission(engine, run_id, state, row)
    legacy = historical_state(engine, run_id, state, row['index'])
    return validate_submission(validation_engine(engine, legacy), run_id, legacy, row)


@flow.snapshot_check
def committed_review_context(engine, run_id, state, *, definition_authoring=False):
    """Existing submitted work owns review admission, independently of new code."""
    from .codex_workflow import _frozen_spec_assignment
    from . import experiment_materials as materials
    from .harness_delivery import authenticate
    from .implementation_definition_repair import FIELD as DEFINITION_REPAIR
    from .implementation_phase_binding import code_planning_document
    if not definition_authoring and state.get(DEFINITION_REPAIR, {}).get('status') not in (None, 'approved'):
        return None  # The separate definition/author transition owns admission.
    code_row = code_planning_document(state, definition_authoring=definition_authoring)
    if not state.get('specifications'):
        return None  # A genuinely prospective author still needs capability admission.
    baseline(engine, run_id, state)
    saved = state[policy.FIELD][KEY]
    slot = saved['rounds'][-1] if saved['rounds'] else None
    cursor = state.get('current_revision')
    if type(cursor) is not int:
        policy.fail('Submitted whole review lacks an exact controller revision')
    code_phase = code_row is not None
    if code_phase and (not slot or slot.get('revision') != code_row['index']
            or not slot.get('complete') or slot.get('outcome') not in policy.APPROVED):
        policy.fail('Code recovery requires the latest independently approved whole-document release')
    revision = code_row['index'] if code_phase else cursor
    rows = [r for r in state['specifications'] if r['index'] == revision]
    repairing = False
    if not rows and slot and slot.get('complete') and slot.get('outcome') == 'REPAIR' and cursor == slot['revision'] + 1:
        revision = slot['revision']
        rows = [r for r in state['specifications'] if r['index'] == revision]
        repairing = True
    if not rows:
        return None  # No submitted successor exists yet; keep ordinary author admission.
    if len(rows) != 1:
        policy.fail('Ambiguous submitted whole-document revision')
    row = rows[0]
    hid = row.get('authoring_handoff_id')
    if not hid:
        policy.fail('Submitted whole review lacks its actual author delivery')
    frozen = engine.store.get('handoff_inputs', hid, {})
    assignment = engine.store.get('handoffs', hid, {})
    exact = _frozen_spec_assignment(engine, run_id, assignment.get('purpose'), 'engineer')
    if not exact or exact['id'] != hid:
        policy.fail('Submitted whole review lost its exact frozen author assignment')
    validate_author(engine, run_id, state, row)
    context = copy.deepcopy(frozen.get('context', {}))
    result = engine.store.get('decisions', hid, {}).get('result')
    if authenticate(engine, run_id, 'engineer', hid, result) != context:
        policy.fail('Submitted whole author differs from its actual frozen delivery')
    ledger = state[policy.FIELD]
    binding = context.get('implementation_document_authoring', {})
    if (binding.get('workflow_id') != state['id'] or binding.get('revision') != row['index']
            or context.get('acceptance_contract') != ledger['contract']
            or context.get('acceptance_budget') != ledger['budget']
            or context.get('research_cycle') != engine.cycle(run_id)
            or any(context.get(key + '_hash') != digest(state['common'][key])
                for key in ('proposal', 'design', 'experiment_plan'))):
        policy.fail('Submitted whole author source or contract changed')
    if slot and slot['revision'] == row['index']:
        if (slot['document'] != row['document'] or slot['spec_hash'] != row['spec_hash']
                or ledger.get('binding') != {'revision': row['index'], 'spec_hash': row['spec_hash'],
                    'document_sha256': row['document']['sha256']}):
            policy.fail('Submitted whole review differs from its current acceptance binding')
        if slot.get('complete'):
            if slot['outcome'] in policy.APPROVED:
                validate_release(engine, run_id, state, row)
            elif slot['outcome'] == 'REPAIR':
                validate_previous(engine, run_id, state, row['index'] + 1)
            else:
                policy.fail('Terminal whole review cannot renew submitted source admission')
        elif row.get('approved') is not None or repairing or code_phase:
            policy.fail('Incomplete whole review cannot authorize a successor or code phase')
    available = {}
    for doc in [*context.get('indexed_documents', []), *context.get('implementation_on_demand_documents', [])]:
        if doc['document_id'] in available and available[doc['document_id']] != doc:
            policy.fail('Submitted whole author has conflicting source versions')
        available[doc['document_id']] = doc
    context['indexed_documents'] = list(available.values())
    if materials.FIELD in context:
        materials.verify_frozen(engine, run_id, state['common']['experiment_plan'], context)
    flow.validate_contract(engine, run_id, state, context)
    # Reuse source facts, not the former author's phase/tool permissions. New
    # author and code-review producers must bind their own current contracts.
    # The complete original input remains immutable and addressable by hid.
    phase_fields = {'acceptance_contract', 'acceptance_budget', 'acceptance_issues',
        'previous_specifications', 'document_read_convergence_protocol',
        'implementation_indexed_review_protocol', 'implementation_inventory_output_protocol',
        'implementation_phase_context_protocol', 'implementation_phase_context',
        PROTOCOL, 'implementation_whole_review_author_policy'}
    phase_prefixes = ('acceptance_author_', 'implementation_acceptance_', 'implementation_author_',
        'implementation_document_authoring', 'implementation_document_edit', 'implementation_document_locate',
        'implementation_document_readability', 'implementation_module_details', 'implementation_revision_',
        'implementation_issue_response', 'implementation_report_amendment')
    context = {key: value for key, value in context.items()
        if key not in phase_fields and not key.startswith(phase_prefixes)}
    context['implementation_submitted_source_view'] = {'version': 1, 'source_handoff_id': hid,
        'source_input_sha256': digest({key: frozen[key] for key in ('instruction', 'context', 'schema')}),
        'scope': 'authenticated_planning_sources', 'scientific_approval': False}
    return context


def adopt(engine, run_id, state, persist):
    """Durable controller transition; never rewrites an old assignment or vote."""
    if (enabled(state) or not policy.enabled(state)
            or state.get('kind', 'implementation') not in {'implementation','runtime_repair'}):
        return
    if any(r.get('approved') for r in state.get('specifications', [])) or state.get('attempts'):
        return  # Completed historical deliveries keep their actual protocol.
    version = engine.store.get('runs', run_id)['config'].get(PROTOCOL, 1)
    if type(version) is not int or version not in (0, 1):
        policy.fail('Unsupported whole-document policy selection')
    if not version:
        return
    cursor = state.get('current_revision', 0)
    rows = state.get('specifications', [])
    selected = [r for r in rows if r['index'] == cursor]
    if not selected:
        from .document_efficiency_recovery import current_author
        from .codex_workflow import _frozen_spec_author
        frozen = current_author(engine, run_id, state, cursor)
        frozen = frozen or _frozen_spec_author(engine, run_id, f'implementation_spec:{digest(state["id"])}:{cursor}')
        if frozen:
            return  # Finish the actual frozen author/draft before adopting after submit.
        pending = [r for r in rows if r.get('approved') is None]
        if len(pending) > 1:
            policy.fail('Whole-document adoption requires an unambiguous controller review cursor')
        selected = pending or rows[-1:]
    start_revision = selected[0]['index'] if selected else cursor
    key = adoption_id(state)
    archive = engine.store.get(MIGRATIONS, key)
    if archive is None:
        from .implementation_whole_history import pending_findings
        archive = {'id': key, 'run_id': run_id, 'workflow_id': state['id'], 'policy': POLICY,
            'pending_findings': pending_findings(engine, run_id, state, selected[0] if selected else None),
            'baseline_ledger': copy.deepcopy(state[policy.FIELD]),
            'baseline_specifications': copy.deepcopy(state.get('specifications', [])), 'created_at': now()}
        archive = policy.sealed(archive)
        engine.store.put(MIGRATIONS, key, archive)
    policy.check_seal(archive)
    if (archive['run_id'] != run_id or archive['workflow_id'] != state['id'] or archive['policy'] != POLICY
            or archive['baseline_ledger'] != state[policy.FIELD]):
        policy.fail('Whole-document policy migration differs from its frozen controller checkpoint')
    ledger = state[policy.FIELD]
    ledger[KEY] = {'version': 1, 'policy': copy.deepcopy(POLICY), 'migration_id': key,
        'migration_sha256': archive['sha256'], 'rounds': [],
        'pending_findings': copy.deepcopy(archive['pending_findings']),
        'stagnation_limit': engine.store.get('runs', run_id)['config'].get('stagnation_rounds', 3),
        'start_revision': start_revision}
    ledger.update(outcome=None, phase='WHOLE_REVIEW')
    persist()
    engine.store.event(run_id, 'implementation.whole_review_policy', REQUIREMENT,
        {'workflow_id': state['id'], 'migration_id': key, 'policy_sha256': POLICY['sha256'],
         'preserved_old_rounds': len(archive['baseline_ledger']['rounds']), 'scientific_approval': False})


def baseline(engine, run_id, state):
    saved = state[policy.FIELD][KEY]
    archive = engine.store.get(MIGRATIONS, saved['migration_id'], {})
    policy.check_seal(archive)
    if (saved['version'] != 1 or saved['policy'] != POLICY or archive['policy'] != POLICY
            or archive['sha256'] != saved['migration_sha256'] or archive['run_id'] != run_id
            or archive['workflow_id'] != state['id'] or archive['id'] != adoption_id(state)):
        policy.fail('Whole-document migration identity or policy changed')
    if saved['pending_findings'] != archive['pending_findings']:
        policy.fail('Unprocessed original findings were dropped during whole-document adoption')
    from .implementation_whole_history import authenticate as authenticate_history
    authenticate_history(engine, run_id, archive['pending_findings'])
    for frozen in archive['baseline_specifications']:
        row = next((r for r in state['specifications'] if r['index'] == frozen['index']), {})
        if any(row.get(k) != frozen.get(k) for k in ('index', 'spec_hash', 'spec', 'document', 'authoring_handoff_id')):
            policy.fail('Historical document or author receipt changed during migration')
    return archive['baseline_ledger']


def pending_index(state, default):
    if not enabled(state):
        return default
    saved = state[policy.FIELD][KEY]
    return saved['start_revision'] if not saved['rounds'] else default


def code_scope(state, row):
    if not state.get('implementation_architecture_scope'):
        return None
    from .code_whole_review import planned_scope
    return planned_scope(state, row)


def code_scope_identity(state, row):
    scope = code_scope(state, row)
    return ({'sha256': scope['sha256'], 'plan_available': scope['plan'] is not None,
             'diagnostic': scope['diagnostic']} if scope else None)


def context_for(state, row, common, role, issues, ordinal, *, prior=None, diagnostic=None):
    from .supplemental_reads import opt_in
    documents = list({d['document_id']: d for d in [*common['indexed_documents'], row['document']]}.values())
    architecture = common['architecture_document']
    context = flow.base_context(state, documents, row)
    context.update({PROTOCOL: 1, 'document_read_window_protocol': 2,
        'document_working_notes_protocol': 1,
        'implementation_acceptance_read_plan': 1, 'implementation_review_read_batch': 1})
    context[policy.CONTEXT] = {'phase': 'WHOLE_REVIEW', 'contract': copy.deepcopy(state[policy.FIELD]['contract']),
        'assigned_criteria': [c['id'] for c in policy.CRITERIA],
        'required_sections': {'whole': copy.deepcopy(row['document']['sections'])},
        'issues': copy.deepcopy(issues), 'document': row['document'], 'document_revision': row['index'],
        'spec_hash': row['spec_hash'], 'self_check': False}
    context[KEY] = {'version': 1, 'policy': copy.deepcopy(POLICY), 'workflow_id': state['id'],
        'report_contract_version': REPORT_CONTRACT_VERSION,
        'round': ordinal, 'attempt': 0, 'reviewer': role, 'document': row['document'], 'spec_hash': row['spec_hash'],
        'stagnation_limit': state[policy.FIELD][KEY]['stagnation_limit'],
        'architecture': architecture, 'required_sources': [architecture], 'code_scope': code_scope_identity(state, row)}
    context[KEY]['pending_findings'] = copy.deepcopy(state[policy.FIELD][KEY]['pending_findings'])
    if prior:
        context[KEY]['continuation'] = {'handoff_id': prior['handoff_id'], 'result_sha256': digest(prior['result']),
            'diagnostic': diagnostic, 'previous_own_report': copy.deepcopy(prior['result'])}
    return opt_in(context)


def report_contract_version(context):
    version = context[KEY].get('report_contract_version', 1)
    if type(version) is not int or version not in (1, REPORT_CONTRACT_VERSION):
        policy.fail('Unsupported whole-document report contract version')
    return version


def guidance(context):
    if report_contract_version(context) == 1:
        return GUIDANCE  # Preserve the exact instruction of frozen v1 requests.
    return GUIDANCE + ('\n本请求使用整篇报告合同v2。两位独立审核者均须返回完整 consistency 清单：'
        + '、'.join(policy.CONSISTENCY) + '；每项恰好一次，依据同版原文独立判断。')


def schema(context):
    from .implementation_issue_output_capacity import inventory_array
    value = copy.deepcopy(policy.REPORT_SCHEMA)
    value['properties'].update(verdict={'enum': ['APPROVE', 'REQUEST_CHANGES', 'UNKNOWN']}, review_summary=copy.deepcopy(policy.TEXT))
    value['required'] += ['verdict', 'review_summary']
    issue = value['properties']['issues']['items']
    issue['properties']['change_locations'] = copy.deepcopy(policy.REFS)
    issue['required'].append('change_locations')
    value['properties']['verifications'] = inventory_array(policy.VERIFY, context[policy.CONTEXT]['issues'], conditions=True)
    history_item = policy.obj({'issue_id': policy.TEXT, 'status': copy.deepcopy(policy.STATUS),
        'evidence': policy.array(policy.REF), 'explanation': policy.TEXT})
    history_item['properties']['status']['description'] = 'PASS=当前原反例已解决或不成立；FAIL=仍成立且须提出当前阻断问题；UNKNOWN=未决。'
    value['properties']['legacy_findings'] = inventory_array(history_item, context[KEY]['pending_findings'])
    value['required'].append('legacy_findings')
    checks = value['properties']['checks']
    checks['minItems'] = checks['maxItems'] = len(policy.CRITERIA)
    if report_contract_version(context) == REPORT_CONTRACT_VERSION:
        value['properties']['consistency'] = inventory_array(
            value['properties']['consistency']['items'], [{'id': name} for name in policy.CONSISTENCY], key='id')
    return value


@flow.snapshot_check
def validate_report(engine, run_id, state, row, packet):
    from . import implementation_whole_reading as reading
    from .tool_contracts import StrictToolValidator
    context = flow.check_packet(engine, run_id, packet)
    marker = context.get(KEY, {})
    scope = context.get(policy.CONTEXT, {})
    current = row['document']
    if (not request_enabled(context) or packet['role'] not in ROLES or marker.get('policy') != POLICY
            or marker.get('reviewer') != packet['role'] or marker.get('workflow_id') != state['id']
            or marker.get('document') != current or marker.get('spec_hash') != row['spec_hash']
            or marker.get('code_scope') != code_scope_identity(state, row)
            or scope.get('contract') != state[policy.FIELD]['contract']):
        policy.fail('Whole-document report changed role, source, scope or contract')
    report = packet['result']
    frozen = engine.store.get('handoff_inputs', packet['handoff_id'], {})
    hand = engine.store.get('handoffs', packet['handoff_id'], {})
    expected_purpose = f'implementation_acceptance_whole_review:v1:{digest(state["id"])}:{marker.get("round")}:{marker.get("attempt")}'
    if (hand.get('purpose') != expected_purpose or frozen.get('instruction') != guidance(context)
            or frozen.get('schema') != schema(context)):
        policy.fail('Whole-document request purpose, instruction or output contract changed')
    if marker.get('author_clarification'):
        from .implementation_whole_clarification import validate as validate_clarification
        validate_clarification(engine, run_id, state, row, marker['author_clarification'], packet['role'])
    if not StrictToolValidator(schema(context)).is_valid(report):
        policy.delivery_error('Whole-document report does not satisfy its frozen output contract')
    if (report['contract_sha256'] != scope['contract']['sha256'] or report['document_sha256'] != current['sha256']
            or len({c['criterion_id'] for c in report['checks']}) != len(policy.CRITERIA)
            or {c['criterion_id'] for c in report['checks']} != set(scope['assigned_criteria'])):
        policy.delivery_error('Whole-document report must cover all six original criteria at the exact version')
    for doc in [current, marker['architecture']]:
        if not reading.complete(engine, run_id, packet, doc):
            policy.delivery_error('Whole-document review requires complete independently delivered reading',
                document_id=doc['document_id'], document_sha256=doc['sha256'], action='read_missing_ranges')
    known = {i['issue_id'] for i in scope['issues']} | {i['local_id'] for i in report['issues']}
    findings = {i['issue_id']: i for i in scope['issues']}
    findings.update({i['local_id']: i for i in report['issues']})
    architecture = marker['architecture']
    if (architecture['sha256'] != scope['contract']['source_sha256']
            or not any(r['document_id'] == architecture['document_id'] for r in scope['contract']['source_refs'])
            or marker.get('required_sources') != [architecture]):
        policy.fail('Whole-document architecture differs from the approved frozen source')
    def refs(values, field, both=False):
        reading.validate_refs(engine, run_id, packet, values, current=current, field=field)
        if both and not any((r['document_id'], r['document_sha256']) == (architecture['document_id'], architecture['sha256']) for r in values):
            policy.delivery_error('Independent whole-document judgment lacks approved architecture evidence', field=field)
    for check in report['checks']:
        if not set(check['issue_ids']) <= known:
            policy.delivery_error('Whole-document check cites an unknown issue')
        if check['status'] == 'PASS':
            refs(check['evidence'], 'checks.' + check['criterion_id'], both=True)
        elif check['evidence']:
            refs(check['evidence'], 'checks.' + check['criterion_id'])
        if check['status'] == 'FAIL' and not check['issue_ids']:
            policy.delivery_error('FAIL needs a concrete finding and change location')
        if check['status'] == 'FAIL' and not any(findings[i]['severity'] in {'P0', 'P1'}
                and findings[i]['criterion_id'] == check['criterion_id'] for i in check['issue_ids']):
            policy.delivery_error('A required failed check needs its own blocking finding, not an optional note')
        if check['status'] == 'UNKNOWN' and not check['missing_information'].strip():
            policy.delivery_error('UNKNOWN must identify the unresolved evidence gap')
    for issue in report['issues']:
        refs(issue['evidence'], 'issues.' + issue['local_id'], both=True)
        if any((r['document_id'], r['document_sha256']) != (current['document_id'], current['sha256']) for r in issue['change_locations']):
            policy.delivery_error('Change locations must identify the current implementation document')
        refs(issue['change_locations'], 'change_locations.' + issue['local_id'])
    for finding in report['legacy_findings']:
        if finding['status'] == 'PASS' or finding['evidence']:
            refs(finding['evidence'], 'legacy_findings.' + finding['issue_id'], both=True)
        original = next(i['finding'] for i in marker['pending_findings'] if i['issue_id'] == finding['issue_id'])
        if finding['status'] == 'FAIL' and not any(i['criterion_id'] == original['criterion_id']
                and i['severity'] in {'P0', 'P1'} for i in report['issues']):
            policy.delivery_error('An unresolved original counterexample needs a concrete current blocking finding')
    expected = {i['issue_id']: i for i in scope['issues']}
    for verification in report['verifications']:
        issue = expected[verification['issue_id']]
        conditions = verification['conditions']
        if {c['index'] for c in conditions} != set(range(len(issue['close_when']))) or len(conditions) != len(issue['close_when']):
            policy.delivery_error('Every inherited finding must retain every original close condition')
        for condition in conditions:
            if condition['status'] in {'PASS', 'FAIL'} or condition['evidence']:
                refs(condition['evidence'], 'close_condition.' + verification['issue_id'])
    # V1 used a shared permissive wire schema. Retain historical empty Scout
    # inventories while admitting the complete independent inventory it also
    # explicitly allowed. New v2 packets require the same full set for both.
    legacy_empty = (report_contract_version(context) == 1 and packet['role'] == 'scout'
        and report['consistency'] == [])
    expected_consistency = set() if legacy_empty else set(policy.CONSISTENCY)
    if {c['id'] for c in report['consistency']} != expected_consistency or len(report['consistency']) != len(expected_consistency):
        policy.delivery_error('Whole-document consistency inventory differs from frozen responsibilities')
    for item in report['consistency']:
        if item['status'] == 'PASS' or item['evidence']:
            refs(item['evidence'], 'consistency.' + item['id'])
    if any(c['status'] == 'FAIL' for c in report['consistency']) and not (
            any(i['severity'] in {'P0', 'P1'} for i in report['issues'])
            or any(c['status'] == 'FAIL' for v in report['verifications'] for c in v['conditions'])):
        policy.delivery_error('Failed consistency must identify a concrete blocking finding and change location')
    verified = {v['issue_id']: all(c['status'] == 'PASS' for c in v['conditions']) for v in report['verifications']}
    if any(c['status'] == 'FAIL' and all(verified.get(i, False) for i in c['issue_ids']) for c in report['checks']):
        policy.delivery_error('A failed check cannot simultaneously verify all its blocking findings as satisfied')
    statuses = [c['status'] for c in report['checks']] + [c['status'] for v in report['verifications'] for c in v['conditions']] + [c['status'] for c in report['consistency']] + [c['status'] for c in report['legacy_findings']]
    blockers = any(i['severity'] in {'P0', 'P1'} for i in report['issues'])
    expected_verdict = 'REQUEST_CHANGES' if blockers or 'FAIL' in statuses else 'UNKNOWN' if 'UNKNOWN' in statuses else 'APPROVE'
    if report['verdict'] != expected_verdict:
        policy.delivery_error('Overall verdict conflicts with checks or unresolved findings')
    if report['verdict'] == 'APPROVE' and marker['code_scope'] and not marker['code_scope']['plan_available']:
        policy.delivery_error('Implementation mapping is not compilable; identify its concrete C03 repair', diagnostic=marker['code_scope']['diagnostic'])
    return report


def source_diagnostic(report):
    gaps = [{'kind': 'criterion', 'criterion_id': c['criterion_id'], 'explanation': c['missing_information']}
        for c in report['checks'] if c['status'] == 'UNKNOWN']
    gaps += [{'kind': 'close_condition', 'issue_id': v['issue_id'], 'index': c['index'],
        'explanation': c['explanation']} for v in report['verifications'] for c in v['conditions'] if c['status']=='UNKNOWN']
    gaps += [{'kind': 'consistency', 'id': c['id'], 'explanation': c['explanation']}
        for c in report['consistency'] if c['status']=='UNKNOWN']
    gaps += [{'kind': 'historical_finding', 'id': c['issue_id'], 'explanation': c['explanation']}
        for c in report['legacy_findings'] if c['status']=='UNKNOWN']
    return {'action': 'clarify_and_read_sources', 'gaps': gaps}


def effective_diagnostic(attempt):
    if 'revalidated_diagnostic' not in attempt:
        return attempt['diagnostic']
    receipt = attempt.get('contract_revalidation', {})
    expected = source_diagnostic(attempt['packet']['result'])
    if (attempt['packet']['result']['verdict'] != 'UNKNOWN'
            or receipt.get('outcome') != 'source_clarification_required'
            or receipt.get('diagnostic_sha256') != digest(attempt['diagnostic'])
            or receipt.get('semantic_diagnostic_sha256') != digest(expected)
            or attempt['revalidated_diagnostic'] != expected):
        policy.fail('Derived source diagnostic differs from the actual retained report')
    return expected


def _revalidation_receipt(engine, run_id, state, row, slot, role, attempt):
    packet = attempt['packet']
    frozen = engine.store.get('handoff_inputs', packet['handoff_id'], {})
    if attempt.get('diagnostic') is None or packet['role'] != role:
        policy.fail('Report revalidation requires its original rejection and role')
    unknown = packet['result']['verdict'] == 'UNKNOWN'
    return policy.sealed({'version': 1, 'kind': 'whole_document_report_revalidation',
        'validator_contract_version': REPORT_CONTRACT_VERSION,
        'report_contract_version': report_contract_version(packet['context']),
        'run_id': run_id, 'workflow_id': state['id'], 'round': slot['ordinal'],
        'revision': row['index'], 'spec_hash': row['spec_hash'],
        'document_sha256': row['document']['sha256'], 'role': role,
        'handoff_id': packet['handoff_id'], 'frozen_input_sha256': digest(
            {key: frozen.get(key) for key in ('instruction', 'context', 'schema')}),
        'result_sha256': digest(packet['result']), 'diagnostic_sha256': digest(attempt['diagnostic']),
        'outcome': 'source_clarification_required' if unknown else 'conclusive_report',
        'semantic_diagnostic_sha256': digest(source_diagnostic(packet['result'])) if unknown else None,
        'scientific_approval': False})


@flow.snapshot_check
def revalidate_report(engine, run_id, state, row, slot, role):
    """Recheck retained bytes before retry budgeting; never replace an answer."""
    attempts = slot['attempts'][role]
    if not attempts:
        policy.fail('Report revalidation lacks its actual latest attempt')
    attempt = attempts[-1]
    validate_report(engine, run_id, state, row, attempt['packet'])
    return _revalidation_receipt(engine, run_id, state, row, slot, role, attempt)


def validate_revalidation(engine, run_id, state, row, slot, role, attempt):
    expected = _revalidation_receipt(engine, run_id, state, row, slot, role, attempt)
    actual = attempt.get('contract_revalidation')
    if (not isinstance(actual, dict) or any(type(actual.get(k)) is not int for k in
            ('version', 'validator_contract_version', 'report_contract_version')) or actual != expected):
        policy.fail('Retained report revalidation lost its exact source/diagnostic binding')
    if actual['outcome'] == 'source_clarification_required':
        if attempt.get('revalidated_diagnostic') != source_diagnostic(attempt['packet']['result']):
            policy.fail('Source clarification revalidation lost its derived diagnostic')
        effective_diagnostic(attempt)
    elif 'revalidated_diagnostic' in attempt:
        policy.fail('A conclusive report cannot carry a source uncertainty diagnostic')


def validate_final_attempt(engine, run_id, state, row, slot, role):
    attempts = slot['attempts'][role]
    if not attempts or attempts[-1]['packet'] != slot['reports'][role]:
        policy.fail('Whole-document final report lost its actual attempt lineage')
    attempt = attempts[-1]
    if attempt['packet']['result']['verdict'] == 'UNKNOWN':
        policy.fail('An unresolved report cannot complete an independent review')
    if attempt['diagnostic'] is not None:
        validate_revalidation(engine, run_id, state, row, slot, role, attempt)
    elif 'contract_revalidation' in attempt:
        policy.fail('Report revalidation cannot erase its original rejection')


def apply_reports(ledger, state, row, slot):
    """Controller-owned aggregation; neither reviewer can veto the other's finding."""
    packets = [slot['reports'][role] for role in ROLES]
    # Both independently verify every inherited condition, including old scoped issues.
    for iid in slot['issue_ids']:
        issue = ledger['issues'][iid]
        answers = [next(v for v in p['result']['verifications'] if v['issue_id'] == iid) for p in packets]
        issue['whole_review_feedback'] = {'document_sha256': row['document']['sha256'],
            'reviews': [{'role': p['role'], 'handoff_id': p['handoff_id'], 'verification': copy.deepcopy(a)}
                        for p, a in zip(packets, answers)],
            'current_change_locations': [copy.deepcopy(r) for answer in answers for c in answer['conditions']
                if c['status'] == 'FAIL' for r in c['evidence'] if r['document_id'] == row['document']['document_id']]}
        passed = all(c['status'] == 'PASS' for answer in answers for c in answer['conditions'])
        if passed:
            refs = [r for answer in answers for c in answer['conditions'] for r in c['evidence']]
            issue['whole_verification'] = {'handoff_ids': [p['handoff_id'] for p in packets], 'conditions': answers,
                'document_sha256': row['document']['sha256']}
            policy.transition(ledger, issue, 'VERIFIED', actor='director+scout', evidence=refs)
            policy.transition(ledger, issue, 'CLOSED', actor='controller', evidence=refs)
        else:
            policy.transition(ledger, issue, 'OPEN', actor='director+scout', reason='whole_document_conditions_not_satisfied')
    for packet in packets:
        ids = policy.propose(ledger, state['id'] + ':whole-v1', row, packet['role'], packet['result'], packet['handoff_id'])
        for iid in ids:
            issue = ledger['issues'][iid]
            issue['verifier'] = packet['role']
            issue['confirmed'] = True
            policy.transition(ledger, issue, 'DEFERRED' if issue['severity'] == 'P2' else 'OPEN',
                actor=packet['role'], evidence=issue['evidence'], reason='whole_document_independent_finding')
    blockers = [i for i in ledger['issues'].values() if i['severity'] in {'P0', 'P1'} and i['status'] in policy.ACTIVE_ISSUES]
    if all(p['result']['verdict'] == 'APPROVE' for p in packets) and not blockers:
        return 'APPROVED_WITH_NOTES' if any(p['result']['issues'] for p in packets) else 'APPROVED'
    if any(i['needs'] == 'REPLAN' for i in blockers):
        return 'NEEDS_REPLAN'
    if any(i['needs'] == 'PROBE' for i in blockers):
        return 'NEEDS_PROBE'
    if not blockers:
        policy.fail('Whole-document UNKNOWN requires source clarification before author revision')
    maximum = ledger['budget']['max_repair_verify_cycles']
    return 'BLOCKED' if maximum is not None and row['index'] >= maximum else 'REPAIR'


@flow.snapshot_check
def replay(engine, run_id, state, *, through=None, compare=True):
    ledger = state[policy.FIELD]
    original = baseline(engine, run_id, state)
    rebuilt = copy.deepcopy(original)
    from .implementation_authoring import validate_submission
    for slot in ledger[KEY]['rounds']:
        if through is not None and slot['revision'] > through:
            break
        if not slot['complete']:
            policy.fail('Whole-document review round is not complete')
        row = next(r for r in state['specifications'] if r['index'] == slot['revision'])
        validate_author(engine, run_id, state, row)
        if (row['spec_hash'] != slot['spec_hash'] or row['document'] != slot['document']
                or set(slot['reports']) != set(ROLES)):
            policy.fail('Whole-document round/version or reviewer inventory changed')
        if slot['ordinal']:
            for patch in row.get('acceptance_patch_reports', []):
                issue = rebuilt['issues'][patch['issue_id']]
                if issue['status'] != 'OPEN':
                    policy.fail('Whole-document patch replay has no open predecessor')
                issue['patch'] = {'handoff_id': row['authoring_handoff_id'], **copy.deepcopy(patch)}
                policy.transition(rebuilt, issue, 'PATCHED', actor='engineer', evidence=patch['evidence'])
        expected = [copy.deepcopy(i) for i in rebuilt['issues'].values()
            if i['severity'] in {'P0', 'P1'} and i['status'] in policy.ACTIVE_ISSUES]
        if slot['issues'] != expected or slot['issue_ids'] != [i['issue_id'] for i in expected]:
            policy.fail('Whole-document findings were dropped or altered')
        for role in ROLES:
            packet = slot['reports'][role]
            attempts = slot['attempts'][role]
            validate_final_attempt(engine, run_id, state, row, slot, role)
            if slot['inputs'][role][KEY] != attempts[0]['packet']['context'][KEY]:
                policy.fail('Whole-document initial assignment differs from its frozen controller input')
            for index, attempt in enumerate(attempts):
                p = attempt['packet']
                actual = flow.check_packet(engine, run_id, p)
                if 'contract_revalidation' in attempt:
                    validate_report(engine, run_id, state, row, p)
                    validate_revalidation(engine, run_id, state, row, slot, role, attempt)
                elif 'revalidated_diagnostic' in attempt:
                    policy.fail('Derived source diagnostic lacks its actual revalidation receipt')
                if (p['role'] != role or actual[KEY]['round'] != slot['ordinal'] or actual[KEY]['attempt'] != index
                        or actual[policy.CONTEXT]['issues'] != expected):
                    policy.fail('Whole-document attempt changed exact role, round or finding inventory')
                if index:
                    previous = attempts[index-1]
                    continued = actual[KEY].get('continuation', {})
                    diagnostic = (effective_diagnostic(previous) if report_contract_version(actual) == 2
                                  else previous['diagnostic'])
                    if (continued.get('handoff_id') != previous['packet']['handoff_id']
                            or continued.get('diagnostic') != diagnostic):
                        policy.fail('Whole-document continuation changed its actual preceding rejection')
                    from .implementation_whole_reading import handoffs
                    handoffs(engine, run_id, p)
            if packet['role'] != role or packet['context'][policy.CONTEXT]['issues'] != expected:
                policy.fail('Independent review changed its inherited findings')
            validate_report(engine, run_id, state, row, packet)
        if apply_reports(rebuilt, state, row, slot) != slot['outcome']:
            policy.fail('Whole-document outcome cannot be reconstructed from actual reviews')
    if compare and any(rebuilt[k] != ledger[k] for k in ('issues', 'events')):
        policy.fail('Whole-document issue history differs from original reviews and patches')
    return rebuilt


@flow.snapshot_check
def validate_release(engine, run_id, state, row):
    ledger = state[policy.FIELD]
    flow.validate_contract(engine, run_id, state)
    saved = ledger[KEY]
    slot = saved['rounds'][-1] if saved['rounds'] else {}
    owning_rows = [r for r in state['specifications'] if r['index'] == row['index']]
    if (ledger.get('release_sha256') != digest({k: v for k, v in ledger.items() if k != 'release_sha256'})
            or not slot.get('complete') or slot.get('outcome') not in policy.APPROVED
            or ledger.get('outcome') != slot.get('outcome') or row.get('approved') is not True
            or row.get(PROTOCOL) != 1 or slot.get('revision') != row['index']
            or len(owning_rows) != 1 or owning_rows[0] != row
            or slot.get('spec_hash') != row['spec_hash'] or slot.get('document') != row['document']
            or ledger.get('binding') != {'spec_hash': row['spec_hash'], 'document_sha256': row['document']['sha256'], 'revision': row['index']}
            or row.get('acceptance_contract_sha256') != ledger['contract']['sha256']):
        policy.fail('Code dispatch requires both independent whole-document approvals at this exact version')
    replay(engine, run_id, state)
    return {'outcome': slot['outcome'], 'document_sha256': row['document']['sha256'],
        'contract_sha256': ledger['contract']['sha256'], 'scientific_approval': False}


def validate_previous(engine, run_id, state, revision):
    ledger = state[policy.FIELD]
    slots = [s for s in ledger[KEY]['rounds'] if s['revision'] == revision - 1]
    if len(slots) != 1:
        policy.fail('Missing exact preceding whole-document review')
    slot = slots[0]
    if slot.get('outcome') != 'REPAIR' or not slot.get('complete') or slot['revision'] != revision - 1:
        policy.fail('Engineer revision requires both completed whole-document reviews')
    flow.validate_contract(engine, run_id, state)
    replay(engine, run_id, state, through=revision - 1, compare=False)
    return next(r for r in state['specifications'] if r['index'] == revision - 1)


def release_scope(engine, run_id, state, row):
    validate_release(engine, run_id, state, row)
    scope = state[policy.FIELD][KEY]['rounds'][-1]['reports']['director']['context'][KEY]['code_scope']
    if scope != code_scope_identity(state, row) or not scope or not scope['plan_available']:
        policy.fail('Whole-document approval has no authenticated code ownership plan')
    return code_scope(state, row)


def historical_previous(engine, run_id, state, revision):
    """Authenticate an old author's predecessor under its original protocol.

    This is a derived read-only replay view, never a replacement approval or a
    write to the original ledger. New whole votes do not participate in it.
    """
    legacy = historical_state(engine, run_id, state, revision)
    row = next((r for r in legacy['specifications'] if r['index'] == revision - 1), None)
    if not row:
        policy.fail('Historical author predecessor is not in its migration archive')
    return flow.validate_repair_previous(validation_engine(engine, legacy), run_id, legacy, revision)
