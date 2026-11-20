"""Persist immutable documents and dispatch indexed full-text triad review.

Historical record bytes are retained without a dispatch path.
Each reviewer reads approved architecture, the current SPEC and its own replies.
"""
from __future__ import annotations

from .async_ownership import gather as gather_owned, cancel as cancel_owned

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path

from . import architecture_documents as architecture, document_index
from .architecture_validation import existing_only

VERSION = 1
READ_LIMIT = 12000


class SpecificationTooLarge(ValueError):
    def __init__(self, actual_bytes, limit_bytes):
        super().__init__('Implementation document exceeds local index size limit')
        self.actual_bytes, self.limit_bytes = actual_bytes, limit_bytes


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def _save(engine, h, run_id, name, text, kind):
    existing = engine.store.get('artifacts', run_id + ':' + name)
    if existing:
        _, old = document_index.artifact_text(engine, run_id, existing['id'])
        if old != text:
            raise ValueError('Immutable implementation document changed')
        artifact = existing
    else:
        artifact = engine.artifact(run_id, name, text, kind)
    return document_index.index(engine, h.intelligence, run_id, artifact['id'], kind=kind)


def original_plan_text(plan):
    from .engine import digest
    lines = ['# 冻结实验计划', '', '计划版本：`' + digest(plan) + '`',
             '本文件保留原计划全部字段；没有实验结果。', '']
    for field, value in plan.items():
        lines += ['## ' + field, '', '```json', _json(value), '```', '']
    return '\n'.join(lines)


def _save_planning_context(engine, h, run_id, common, code_contract, format_contract, repair, *,
                           protocol, read_window_protocol, checked_inputs):
    """Keep document writes on the caller after the pure ancestry validation."""
    from .engine import digest
    saved = architecture.save(engine, h, run_id, common['design'])
    plan = common.get('experiment_plan') or {}
    definitions = []
    if common.get('task_document_projection_protocol') == 1:
        from .task_document_projection import brief, render
        view, text = brief(plan, '冻结实验计划任务字段')
        base = f'cycle-{engine.cycle(run_id)}/task-documents/v1/{view["source_sha256"]}'
        directory = _save(engine, h, run_id, base + '-experiment_plan-brief.md', text, 'frozen_experiment_plan')
        definitions = [_save(engine, h, run_id, base + '-definitions.md', render(view, '任务资料精确定义'),
                             'task_document_definitions')]
    else:
        directory = _save(engine, h, run_id,
            f'cycle-{engine.cycle(run_id)}/implementation/plans/{digest(plan)}.md',
            original_plan_text(plan), 'frozen_experiment_plan')
    docs = [saved['document'], saved['approved_proposal'], directory, *definitions]
    context = {'document_index_protocol': 1, 'indexed_document_context': True,
        'source_tool_protocol': 1, 'research_cycle': engine.cycle(run_id),
        'indexed_documents': docs, 'architecture_document': saved['document'],
        'proposal_hash': saved['proposal_hash'], 'design_hash': saved['design_hash'],
        'experiment_plan_hash': digest(plan), 'execution_constraints': common.get('execution_constraints', {}),
        'hardware': common.get('hardware'), 'code_contract': code_contract,
        'format_contract_version': 2, 'implementation_format_contract': format_contract,
        'document_read_budget': READ_LIMIT,
        'document_policy': '目录仅用于定位。按章节补读，每次至多6000字符；单请求正文总量至多12000字符。'
                           '不得把任一整份文档或历史论文全文重新注入上下文。'}
    from .dataset_transition import review_scope
    context.update(review_scope(engine.store, run_id))
    if context.get('dataset_adaptation'):
        packet = common.get('dataset_transition', {})
        context['existing_implementation'] = packet.get('existing_implementation', {})
        original = packet.get('approved_method_document')
        if original:
            docs.append(original)
            context['existing_method_document'] = original
    if protocol == 2:
        from .execution_contract import constraints
        current = checked_inputs
        for doc in current['indexed_documents']:
            if all(d['document_id'] != doc['document_id'] for d in docs):
                docs.append(doc)
        context.update({key: value for key, value in current.items() if key != 'indexed_documents'})
        context['document_input_protocol'] = 2
        context['execution_constraints'] = constraints(engine.store.get('runs', run_id)['config'])
        if read_window_protocol == 1:
            context['document_read_window_protocol'] = 1
    if repair:
        # The accepted implementation is actual code, not an architecture dump.
        context['runtime_repair'] = {k: v for k, v in repair.items() if k != 'program'}
        context['prior_program'] = {k: repair.get('program', {}).get(k)
            for k in ('code', 'code_sha256', 'artifact_id', 'spec_hash', 'delivery_hash')}
    from . import implementation_capabilities as capabilities
    evidence = capabilities.planning_evidence(engine, run_id, common)
    capability_document = _save(engine, h, run_id,
        f'cycle-{engine.cycle(run_id)}/implementation/host-capabilities/{evidence["capability_fingerprint"]}.md',
        capabilities.document_text(evidence), 'host_implementation_contract')
    context['implementation_capability_evidence'] = evidence
    context['implementation_capability_document'] = capability_document
    context['indexed_documents'].append(capability_document)
    if common.get('dataset_catalog'):
        context['dataset_catalog'] = copy.deepcopy(common['dataset_catalog'])
        context['dataset_request_protocol'] = 2
    return context


def planning_context(engine, h, run_id, common, code_contract, format_contract, repair=None, *,
                     protocol=1, read_window_protocol=0):
    """Directories replace full design, proposal, plan, history and paper texts."""
    from .architecture_revisions import indexed_inputs
    from .architecture_validation import operation
    checked = operation(indexed_inputs)(engine, run_id) if protocol == 2 else None
    return _save_planning_context(engine, h, run_id, common, code_contract, format_contract, repair,
        protocol=protocol, read_window_protocol=read_window_protocol, checked_inputs=checked)


async def planning_context_async(engine, h, run_id, common, code_contract, format_contract, repair=None, *,
                                 protocol=1, read_window_protocol=0):
    from .architecture_revisions import indexed_inputs
    from .architecture_validation import operation
    checked = (await engine._read_consensus_check(run_id, operation(indexed_inputs), engine, run_id)
               if protocol == 2 else None)
    engine.check_active(run_id)
    return _save_planning_context(engine, h, run_id, common, code_contract, format_contract, repair,
        protocol=protocol, read_window_protocol=read_window_protocol, checked_inputs=checked)


def committed_review_context(engine, run_id, state):
    """Recover a submitted v1 materials review; never renew prospective author code.

    None means the existing author/legacy path owns admission. Once a current
    compact review is identified, failed authentication must propagate.
    """
    from . import implementation_whole_review as whole
    if whole.enabled(state):
        return whole.committed_review_context(engine, run_id, state)
    from . import implementation_acceptance as policy, implementation_acceptance_flow as flow
    from . import implementation_authoring as author, experiment_materials as materials
    from .codex_workflow import _frozen_spec_assignment
    from .engine import digest
    from .implementation_definition_repair import FIELD as DEFINITION_REPAIR
    from .implementation_phase_binding import code_planning_document
    if not policy.enabled(state):
        return None
    if state.get(DEFINITION_REPAIR, {}).get('status') not in (None, 'approved'):
        return None  # A separate author/definition transition retains admission.
    code_row = code_planning_document(state)
    pending = [code_row] if code_row is not None else [r for r in state['specifications'] if r.get('approved') is None]
    repairing = False
    if not pending:
        ledger = state[policy.FIELD]
        last = state['specifications'][-1] if state['specifications'] else None
        if (last and last.get('approved') is False and ledger.get('outcome') == 'REPAIR'
                and ledger['rounds'][-1].get('scoped_supplement')):
            # Both pending endpoint evidence and confirmed endpoint defects
            # use the submitted author's authenticated compact materials.
            # validate_repair_previous below replays the actual disposition;
            # selecting this source view grants no author or reviewer approval.
            pending = [last]
            repairing = True
    if not pending:
        # A released document can be resumed before code dispatch too. It does
        # not reopen authorship; authenticate its ordinary release below.
        pending = [r for r in reversed(state['specifications']) if r.get('approved') is True][:1]
        if not pending:
            return None
    if len(pending) != 1:
        policy.fail('Ambiguous submitted review cursor')
    row = pending[0]
    hid = row.get('authoring_handoff_id')
    context = engine.store.get('handoff_inputs', hid or '', {}).get('context', {})
    assignment = engine.store.get('handoffs', hid or '', {})
    frozen = _frozen_spec_assignment(engine, run_id, assignment.get('purpose'), 'engineer')
    if not frozen or frozen['id'] != hid:
        policy.fail('Submitted review lacks its exact frozen author')
    author.validate_submission(engine, run_id, state, row)
    if materials.FIELD not in context:
        return None  # Historical protocols retain their original admission.
    ledger = state[policy.FIELD]
    slots = [s for s in ledger['rounds'] if s['revision'] == row['index']]
    released = row.get('approved') is True
    if (len(slots) != 1 or slots[0].get('complete') is not (released or repairing)
            or slots[0] != ledger['rounds'][-1]
            or (code_row is None and state.get('current_revision') not in
                ([row['index'], row['index'] + 1] if repairing else [row['index']]))
            or ledger.get('binding') != {'revision': row['index'], 'spec_hash': row['spec_hash'],
                                       'document_sha256': row['document']['sha256']}):
        policy.fail('Submitted review does not match the current acceptance binding')
    decision = engine.store.get('decisions', hid, {})
    from .harness_delivery import authenticate
    from .intelligence import parse_json
    parse_json(json.dumps(decision.get('result'), ensure_ascii=False), frozen['schema'])
    if authenticate(engine, run_id, 'engineer', hid, decision.get('result')) != context:
        policy.fail('Submitted author delivery differs from its frozen context')
    context = copy.deepcopy(context)
    binding = context.get('implementation_document_authoring', {})
    if (binding.get('workflow_id') != state['id'] or binding.get('revision') != row['index']
            or context.get('acceptance_contract') != ledger['contract']
            or context.get('acceptance_budget') != ledger['budget']
            or context.get('research_cycle') != engine.cycle(run_id)
            or any(context.get(key + '_hash') != digest(state['common'][key])
                   for key in ('proposal', 'design', 'experiment_plan'))):
        policy.fail('Submitted author scientific source or acceptance contract changed')
    available = {}
    for doc in [*context['indexed_documents'], *context.get('implementation_on_demand_documents', [])]:
        if doc['document_id'] in available and available[doc['document_id']] != doc:
            policy.fail('Conflicting frozen common document versions')
        available[doc['document_id']] = doc
    context['indexed_documents'] = list(available.values())
    materials.verify_frozen(engine, run_id, state['common']['experiment_plan'], context)
    flow.validate_contract(engine, run_id, state, context)
    if released:
        flow.validate_release(engine, run_id, state, row)
    if repairing:
        flow.validate_repair_previous(engine, run_id, state, row['index'] + 1)
    # Authenticate existing base review packets as well as their source view.
    # The scoped scheduler authenticates its reports, obligations and successor
    # lineage before dispatch, with its original deadline and budget intact.
    for role in ('director', 'engineer', 'scout'):
        packet = _frozen_spec_assignment(engine, run_id,
            f'implementation_acceptance_review:{digest(state["id"])}:{row["index"]}', role)
        if packet:
            review = packet['context']
            scope = review.get(policy.CONTEXT, {})
            if (review.get('acceptance_workflow_id') != state['id']
                    or scope.get('contract') != ledger['contract']
                    or scope.get('document') != row['document']
                    or scope.get('document_revision') != row['index']
                    or scope.get('spec_hash') != row['spec_hash']
                    or scope.get('self_check') is not (role == 'engineer')
                    or review.get(materials.FIELD) != context[materials.FIELD]):
                policy.fail('Frozen review differs from submitted author acceptance binding')
    return context


def save_specification(engine, h, run_id, state, row, context):
    from .engine import digest
    from . import implementation_document_readability as readability
    if digest(row['spec']) != row['spec_hash']:
        raise ValueError('Implementation specification fingerprint changed')
    readable = readability.enabled(context)
    layout_version = context.get(readability.PROTOCOL) if readable else None
    lines = ['# 代码实现文档', '', '规范版本：`' + row['spec_hash'] + '`',
        '共识方案：`' + state['proposal_hash'] + '`',
        '架构设计：`' + context['design_hash'] + '`',
        '冻结计划：`' + context['experiment_plan_hash'] + '`',
        '来源：' + engine.store.get('runs', run_id)['mode'] + '；计划与验收要求，不代表代码已实现或科学成功。', '']
    if readable:
        lines += [readability.render_specification(row['spec'], version=layout_version), '']
    else:
        for field, value in row['spec'].items():
            lines += ['## ' + field, '']
            lines += [value, ''] if isinstance(value, str) else ['```json', _json(value), '```', '']
    lines += ['## 代码目录与入口运行契约', '', context['code_contract'], '',
              '## 计划映射与真实交付格式', '']
    if readable:
        lines += [readability.render_format_contract(context['implementation_format_contract'], version=layout_version), '']
    else:
        lines += ['```json', _json(context['implementation_format_contract']), '```', '']
    lines += ['## 输入文档索引', '', '按下列版本读取约束，章节目录由 documents.list 提供，目录不等于已读取正文。', '']
    lines += [f'- {v["title"]}；document_id=`{v["document_id"]}`；SHA256=`{v["sha256"]}`'
              for v in context['indexed_documents']]
    if state.get('codex_prompt_protocol') == 1:
        target = Path(state['workspace']).parent / 'inputs' / row['spec_hash'][:16]
        lines += ['', '## Codex 实现与交付', '',
            '实际实现与修复由完整 Codex 代理完成；研究 agent 只审查实际产物及要求的完成情况。',
            '工作目录：`' + state['workspace'] + '`。交付仅含入口的 experiment.py、按职责拆分的功能模块、implementation.json 和必要测试；清单声明 code_layout=modular 和完整 runtime_files。',
            '输入索引清单：`' + str(target / 'document-index.json') + '`。',
            '按索引定位的标准库阅读器：`' + str(target / 'document_reader.py') + '`。',
            '所有原文副本的路径和版本见输入索引。implementation.json 中 implementation_document_sha256 '
            '须填写该索引中 implementation.md 的原文 SHA256，并按上述格式映射每项要求的实际位置和验证。',
            'Codex 自行选择实现步骤、读文件并运行有界开发检查；正式实验与 held-out 测试由宿主门禁调度。',
            '审查未完成项形成独立意见文档后交回 Codex；审查 agent 不生成或应用代码补丁。']
    lines += ['']
    layout = f'readable-v{layout_version}/' if readable else ''
    name = f'cycle-{engine.cycle(run_id)}/implementation/{digest(state["id"])[:16]}/{row["spec_hash"]}/{layout}implementation.md'
    # Markdown is read using universal newlines. Canonicalize only the rendered
    # document (never the SPEC) so CRLF input still round-trips its SHA on Windows.
    text = '\n'.join(lines).replace('\r\n', '\n').replace('\r', '\n')
    size = len(text.replace('\n', os.linesep).encode('utf-8'))
    if size > document_index.MAX_TEXT_BYTES:
        raise SpecificationTooLarge(size, document_index.MAX_TEXT_BYTES)
    row['document'] = _save(engine, h, run_id, name, text, 'implementation_specification')
    row['document_protocol'] = state['document_protocol']
    if readable:
        row[readability.PROTOCOL] = layout_version
    row['input_documents'] = context['indexed_documents']
    return row['document']


def _validate_result(result, context):
    from .codex_workflow import _fail
    for key in ('proposal_hash', 'spec_hash', 'document_sha256'):
        if result.get(key) != context[key]:
            _fail('实现文档评议没有绑定同一' + key)
    issues, fixes = result['issues'], result['fixes']
    if result['approve'] and (issues or fixes):
        _fail('实现文档批准票仍有未解决修改')
    if not result['approve'] and (not issues or {v['issue_index'] for v in fixes} != set(range(len(issues)))):
        _fail('实现文档否决须为每项问题提供修改与验证方法')


def require_indexed_review(engine, run_id, state, row=None):
    """Accept only the frozen new protocol; never retrofit paid old inputs."""
    from .codex_workflow import _fail
    from . import implementation_indexed_review as indexed
    if (type(state.get(indexed.PROTOCOL)) is not int or state[indexed.PROTOCOL] != indexed.VERSION
            or row is not None and (type(row.get(indexed.PROTOCOL)) is not int or row[indexed.PROTOCOL] != indexed.VERSION)
            or state.get('implementation_whole_alignment_protocol') == 1
            or state.get('implementation_incremental_review_protocol') == 1
            or state.get('document_protocol') != 2
            or state.get('implementation_document_authoring_protocol') != 1):
        _fail('历史文档与其冻结的作者或审核协议不一致。')
    candidates = [row] if row is not None else state.get('specifications', [])
    if any(entry.get('implementation_whole_alignment_protocol') == 1
           or entry.get('implementation_incremental_review_protocol') == 1
           or entry.get('unit_reviews') or entry.get('superseded_reviews')
           or any(entry.get(name) is not None for name in
                ('review_batch', 'review_multiunit_batch', 'review_followup', 'review_wait',
                 'implementation_incremental_target')) for entry in candidates):
        _fail('新流程不能转换旧整篇全文、单问题或章节分组的冻结审查。')


def _read_coverage_spans(engine, run_id, doc, handoff_id):
    """Authenticate source bytes and delivered receipts into exact spans."""
    manifest, text = document_index.load(engine, run_id, doc['document_id'], doc['sha256'])
    progress = engine.store.get('handoff_progress', handoff_id, {})
    from .evidence_delivery import tracking_required, delivery_spans
    tracked = tracking_required(engine.store, run_id, handoff_id)
    delivered = []
    if tracked:
        role = engine.store.get('handoffs', handoff_id, {}).get('to_role')
        delivered = delivery_spans(engine.store, run_id, role, doc['document_id'], doc['sha256'],
            identity_key='document_id', handoff_id=handoff_id, text=text)
    spans = {}
    for receipt in progress.get('tool_results', []):
        if receipt.get('call', {}).get('name') == 'materials.request_read':
            if receipt.get('result', {}).get('error'):
                continue
            from .supplemental_reads import normal_read
            receipt = normal_read(engine, run_id, handoff_id, receipt)
        call, page = receipt.get('call', {}), receipt.get('result', {})
        if call.get('name') != 'documents.read' or not isinstance(page, dict) or page.get('error'):
            continue
        args = call.get('arguments', {})
        if args.get('document_id') != doc['document_id']:
            continue
        section = next((s for s in manifest['sections'] if s['id'] == args.get('section_id')), None)
        start, end = page.get('char_start'), page.get('char_end')
        offset, limit = args.get('offset', 0), args.get('limit', 4000)
        if (section is None or type(start) is not int or type(end) is not int
                or type(offset) is not int or type(limit) is not int or not 1 <= limit <= 6000
                or not section['start'] <= start <= end <= section['end']
                or start != section['start'] + offset or end > start + limit
                or args.get('expected_sha256') != doc['sha256']
                or page.get('document_id') != doc['document_id'] or page.get('sha256') != doc['sha256']
                or page.get('section_id') != section['id'] or page.get('text') != text[start:end]):
            return None
        if tracked:
            for row in delivered:
                if row.get('section_id') == section['id']:
                    a, b = max(start, row['char_start']), min(end, row['char_end'])
                    if a < b:
                        spans.setdefault(section['id'], []).append((a, b))
        else:
            spans.setdefault(section['id'], []).append((start, end))
    return spans


@existing_only
def _snapshot_read_coverage_spans(engine, run_id, doc, handoff_id):
    """Reuse compact spans only within one validation of an immutable DB view."""
    return _read_coverage_spans(engine, run_id, doc, handoff_id)


def read_coverage(engine, run_id, doc, required, handoff_id):
    """Verify actual on-demand Unicode ranges, allowing arbitrary pagination."""
    reader = (_snapshot_read_coverage_spans if getattr(engine.store, 'is_read_snapshot', False)
              else _read_coverage_spans)
    spans = reader(engine, run_id, doc, handoff_id)
    if spans is None:
        return False
    for receipt in required:
        page = receipt['result']
        cursor = page['char_start']
        for start, end in sorted(spans.get(page['section_id'], [])):
            if start <= cursor:
                cursor = max(cursor, end)
        if cursor < page['char_end']:
            return False
    return True


@architecture.operation
def validate_review(engine, run_id, state, row):
    """Authorize only a current authentic new-protocol three-role approval."""
    from . import implementation_cycle as cycle
    if cycle.active(row):
        return cycle.validate_document(engine,run_id,state,row)
    from .implementation_authoring import validate_submission
    from .implementation_indexed_replies import validate_documents
    from . import implementation_indexed_review as indexed
    require_indexed_review(engine, run_id, state, row)
    if row.get('implementation_definition_repair_protocol') == 1:
        from .implementation_definition_repair import validate_release
        return validate_release(engine, run_id, state, row)
    from . import implementation_acceptance as acceptance, implementation_acceptance_flow as flow
    if acceptance.enabled(state):
        return flow.validate_release(engine, run_id, state, row)
    validate_submission(engine, run_id, state, row)
    validate_documents(engine, run_id, state, row)
    return indexed.validate_review(engine, run_id, state, row)


def validate_review_snapshot(engine, run_id, state, row):
    """Recheck the same approval against one scoped read-only database view."""
    with engine.store.read_snapshot() as store:
        reader = copy.copy(engine)
        reader.store = store
        if getattr(getattr(engine, 'sources', None), 'store', None) is engine.store:
            reader.sources = copy.copy(engine.sources)
            reader.sources.store = store
        return validate_review(reader, run_id, state, row)


def bounded_tool_result(context, receipts, call, output):
    from . import independent_full_review as full
    if full.tool_available(context) and call.get('name') in {full.TOOL, 'reviews.record_notes'}:
        # Execution authenticates the exact immutable virtual material. Its
        # actual pages share the final combined 50k presentation window.
        return output
    if call.get('name') == 'materials.request_read':
        from .supplemental_reads import bounded_output
        return bounded_output(context, call, output)
    if context.get('indexed_document_context') is not True or context.get('document_index_protocol') != 1:
        return output
    if call.get('name') in {'code.locate', 'code.read', 'code.record_notes', 'documents.recall'}:
        return output  # Exact handoff scope and per-page limits are checked by code_navigation.
    if context.get('code_repair_tool_review') == 1 and call.get('name', '').startswith('documents.'):
        return output  # Per-page reads, no obsolete lifetime cumulative quota.
    if (context.get('implementation_document_authoring_protocol') == 1
            and call.get('name') in {'implementation.write_document', 'implementation.edit_document'}):
        return output
    from .implementation_self_check import TOOL as SELF_CHECK, enabled as self_check_enabled
    if self_check_enabled(context) and call.get('name') == SELF_CHECK:
        # Execution has already checked the frozen tool authority, current draft
        # and actual delivered evidence. This is a bounded receipt, not source text.
        return output
    from .implementation_document_locator import TOOL as LOCATE
    if (context.get('implementation_document_authoring_protocol') == 1
            and call.get('name') == LOCATE):
        # Already authorized against the frozen request and exact current draft.
        # The locator bounds its own values and never grants source-reading credit.
        # Its request opt-in can be newer than this handoff's projected context.
        return output
    from .document_working_notes import read_directories
    docs = read_directories(context)
    scoped_code_delivery = full.code_delivery_scope(context)
    if scoped_code_delivery:
        from .code_whole_review import scope_directory
        docs = scope_directory(context, docs)
    if (call.get('name') == 'artifacts.list' and context.get('delivery_directory_protocol') == 1
            and context.get('implementation_alignment_protocol') == 1):
        return output  # Frozen assignment scope is checked by the directory tool.
    if (call.get('name') == 'artifacts.read'
            and context.get('implementation_alignment_protocol') == 1):
        if isinstance(output, dict) and output.get('error'):
            return output
        # Host receipts are a separate, immutable evidence class. They may be
        # read on demand, but are never added to delivery_artifacts or accepted
        # as code_evidence by the implementation alignment validator.
        args = call.get('arguments') or {}
        host_artifacts, call_identity = [], args.get('id')
        if not scoped_code_delivery:
            from .host_execution_artifacts import catalog as host_catalog, resolve as host_resolve
            host_artifacts = [*(context.get('host_verification') or {}).get('artifacts', []), *host_catalog(context).values()]
            host_bound = host_resolve(context, args)
            if host_bound:
                call_identity = host_bound['artifact_id']
        matches = [item for item in [*context.get('delivery_artifacts', []),*host_artifacts]
                   if item.get('artifact_id') == call_identity]
        if (len(matches) != 1 or not isinstance(output, dict)
                or output.get('artifact_id') != matches[0]['artifact_id']
                or output.get('sha256') != matches[0].get('sha256')):
            return {'error': '代码评审读取不属于当前冻结交付版本', 'text_returned': False}
        return output
    if call.get('name') == 'documents.list':
        args = call.get('arguments') or {}
        if args:
            match = [doc for doc in docs if doc.get('document_id') == args.get('document_id')
                     and doc.get('sha256') == args.get('expected_sha256')]
            if (len(match) != 1 or not isinstance(output, list) or len(output) != 1
                    or output[0].get('document_id') != match[0]['document_id']
                    or output[0].get('sha256') != match[0]['sha256']):
                return {'error': '目录不属于本次固定输入版本', 'text_returned': False}
            return output
        if scoped_code_delivery and isinstance(output, list):
            return scope_directory(context, output)
        return docs
    if call.get('name') not in {'documents.search', 'documents.read', 'hardware.inspect', 'resources.assess'}:
        raise ValueError('Indexed implementation requires scoped document tools')
    if call.get('name') not in {'documents.search', 'documents.read'} or not isinstance(output, dict) or output.get('error'):
        return output
    doc = next((v for v in docs if v['document_id'] == output.get('document_id')), None)
    if not doc:
        return {'error': '文档不属于本次固定输入索引', 'text_returned': False}
    if call['name'] == 'documents.search':
        chars = min(2000, max(0, doc['total_chars'] - 1))
        hits = []
        for row in output.get('hits', []):
            length = min(240, chars)
            hits.append({**row, 'excerpt': row.get('excerpt', '')[:length], 'excerpt_truncated': True})
            chars -= min(length, len(row.get('excerpt', '')))
        return {**output, 'hits': hits}
    from .document_read_window import enabled, bound_read
    if full.tool_available(context) or context.get('scientific_author_protocol') == 1:
        if len(output.get('text', '')) > full.PAGE_LIMIT:
            raise ValueError('Full-review document page exceeds its frozen per-page limit')
        return output
    if enabled(context):
        return bound_read(doc, output)
    pages = [v['result'] for v in receipts if v.get('call', {}).get('name') == 'documents.read'
             and isinstance(v.get('result'), dict) and isinstance(v['result'].get('text'), str)]
    used = sum(len(v['text']) for v in pages)
    same = sum(len(v['text']) for v in pages if v.get('document_id') == doc['document_id'])
    limit = min(READ_LIMIT - used, 6000 - same, doc['total_chars'] - same - 1)
    if limit <= 0:
        return {'error': '本次输入正文达到范围；按目录定位，保留尚待核实的问题，不累积全文。', 'text_returned': False}
    if len(output['text']) <= limit:
        return output
    end = output['offset'] + limit
    return {**output, 'text': output['text'][:limit], 'char_end': output['char_start'] + limit,
            'next_offset': end, 'truncated': True, 'document_budget_limited': True}


def export_bundle(engine, run_id, state, row):
    """Read-only Codex inputs live outside its writable delivery workspace."""
    from .codex_workflow import _checked_workspace
    _checked_workspace(state['project_root'], run_id, state['workspace'])
    from .implementation_cycle import active
    if not active(state):
        require_indexed_review(engine, run_id, state, row)
    name = row['spec_hash'][:16]
    target = Path(state['workspace']).parent / 'inputs' / name
    from .deletion import _plain_path
    _plain_path(target, Path(state['project_root']))
    target.mkdir(parents=True, exist_ok=True)
    documents = []
    for number, directory in enumerate([row['document'], *row['input_documents']]):
        _, text = document_index.load(engine, run_id, directory['document_id'], directory['sha256'])
        filename = 'implementation.md' if number == 0 else f'document-{number}.md'
        documents.append({**directory, 'file': filename})
        _write_immutable(target / documents[-1]['file'], text.encode('utf-8'))
    manifest = {'version': VERSION, 'run_id': run_id, 'spec_hash': row['spec_hash'],
        'proposal_hash': state['proposal_hash'], 'documents': documents, 'max_read_chars': 6000}
    reader = Path(__file__).with_name('document_reader.py').read_bytes()
    _write_immutable(target / 'document-index.json', _json(manifest).encode('utf-8'))
    _write_immutable(target / 'document_reader.py', reader)
    return {'manifest': str(target / 'document-index.json'), 'reader': str(target / 'document_reader.py'),
        'manifest_sha256': hashlib.sha256(_json(manifest).encode('utf-8')).hexdigest(),
        'reader_sha256': hashlib.sha256(reader).hexdigest(), 'documents': documents,
        'project_root': state['project_root'], 'workspace': state['workspace']}


def _write_immutable(path, content):
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
        raise ValueError('Implementation input path is a link')
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError('Frozen Codex document input changed')
    else:
        with path.open('xb') as stream:
            stream.write(content)


def verify_bundle(bundle):
    """Readback validates frozen inputs; it never recopies a changed reader."""
    from .document_reader import navigate
    from .deletion import _plain_path
    root = Path(bundle['workspace']).parent / 'inputs'
    for field in ('manifest', 'reader'):
        path = Path(bundle[field])
        _plain_path(path, Path(bundle['project_root']))
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Frozen bundle is outside its task inputs')
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Frozen input bundle contains links')
        if hashlib.sha256(path.read_bytes()).hexdigest() != bundle[field + '_sha256']:
            raise ValueError('Frozen Codex document bundle changed')
    for doc in bundle['documents']:
        navigate(bundle['manifest'], 'read', document_id=doc['document_id'], section_id=doc['sections'][0]['id'], limit=1)
    for feedback in bundle.get('feedback_documents', []):
        path = Path(feedback['path'])
        _plain_path(path, Path(bundle['project_root']))
        if (not path.resolve().is_relative_to(root.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != feedback['sha256']):
            raise ValueError('Frozen Codex review feedback changed')


