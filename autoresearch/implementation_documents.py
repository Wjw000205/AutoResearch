"""Persist immutable documents and dispatch indexed full-text triad review.

Historical record bytes are retained without a dispatch path.
Each reviewer reads approved architecture, the current SPEC and its own replies.
"""
from __future__ import annotations

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


async def _collect_admitted(collector, prepared):
    """Drain ordinary peer failures, but stop every peer promptly on pause."""
    tasks = []
    started = asyncio.Event()
    cancellation_sent = False

    def cancel_pending(exclude=None):
        nonlocal cancellation_sent
        cancellation_sent = True
        for task in tasks:
            if task is not exclude and not task.done() and not task.cancelling():
                task.cancel()

    async def collect(scope, value):
        try:
            await started.wait()
            return await collector(scope, value)
        except asyncio.CancelledError:
            if not cancellation_sent:
                cancel_pending(asyncio.current_task())
            raise

    try:
        for scope, value in prepared:
            tasks.append(asyncio.create_task(collect(scope, value)))
        started.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        cancel_pending()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    # A pause wins over an ordinary invalid opinion from another role.
    for outcome in outcomes:
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


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


def _save_planning_context(engine, h, run_id, common, code_contract, format_contract, repair, *,
                           protocol, read_window_protocol, checked_inputs):
    """Keep document writes on the caller after the pure ancestry validation."""
    from .engine import digest
    saved = architecture.save(engine, h, run_id, common['design'])
    plan = common.get('experiment_plan') or {}
    lines = ['# 冻结实验计划', '', '计划版本：`' + digest(plan) + '`',
             '本文件保留原计划全部字段；没有实验结果。', '']
    for field, value in plan.items():
        lines += ['## ' + field, '', '```json', _json(value), '```', '']
    directory = _save(engine, h, run_id,
        f'cycle-{engine.cycle(run_id)}/implementation/plans/{digest(plan)}.md',
        '\n'.join(lines), 'frozen_experiment_plan')
    docs = [saved['document'], saved['approved_proposal'], directory]
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
            or state.get('implementation_document_authoring_protocol') != 1
            or engine.store.get('runs', run_id, {}).get('config', {}).get('implementation_review_strategy') not in {'whole_alignment', 'acceptance_contract'}):
        _fail('旧实现审查派发已经删除；请使用新索引全文审核及真实作者保存流程，历史付费输入保持原样。')
    candidates = [row] if row is not None else state.get('specifications', [])
    if any(entry.get('implementation_whole_alignment_protocol') == 1
           or entry.get('implementation_incremental_review_protocol') == 1
           or entry.get('unit_reviews') or entry.get('superseded_reviews')
           or any(entry.get(name) is not None for name in
                ('review_batch', 'review_multiunit_batch', 'review_followup', 'review_wait',
                 'implementation_incremental_target')) for entry in candidates):
        _fail('新流程不能转换旧整篇全文、单问题或章节分组的冻结审查。')


async def reviews(engine, h, run_id, state, row, context, persist):
    """Dispatch exactly the new three independent indexed full-text reviews."""
    from .implementation_review_flow import run_indexed
    engine.check_active(run_id)
    require_indexed_review(engine, run_id, state, row)
    from . import implementation_acceptance as acceptance, implementation_acceptance_flow as flow
    if acceptance.enabled(state):
        return await flow.review(engine, h, run_id, state, row, context, persist)
    return await run_indexed(engine, h, run_id, state, row, context, persist)


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
        host_artifacts=(context.get('host_verification') or {}).get('artifacts',[])
        matches = [item for item in [*context.get('delivery_artifacts', []),*host_artifacts]
                   if item.get('artifact_id') == (call.get('arguments') or {}).get('id')]
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


def codex_instruction(state, row, feedback):
    bundle = state['input_bundle']
    implementation = Path(bundle['manifest']).parent / bundle['documents'][0]['file']
    instruction = '按照 "' + str(implementation) + '" 实现代码。'
    focused = any(item.get('repair_contract', {}).get('audit_revision') for item in feedback)
    if focused:
        from .code_repair_audit_view import view
        feedback = copy.deepcopy(feedback)
        for item in feedback:
            packet = item.get('repair_contract')
            if packet and packet.get('audit_revision'):
                item['repair_contract'] = {**view(packet), 'instruction': packet['instruction'],
                    'repair_receipt_schema': packet['repair_receipt_schema']}
        instruction = ('本轮仅按修改清单修复已有代码，保留已正确实现和已完成研究阶段。'
            '清单逐项给出审查意见、实现文档具体位置、代码位置、修改办法和验证要求。'
            '仅按需补读所引章节或代码附近，不把整份文档、源码或评审历史加入上下文。'
            '批准实现文档位置："' + str(implementation) + '"；文档索引："' + str(bundle['manifest']) + '"。')
    from .code_bundle import INSTRUCTION as layout_instruction
    instruction += '\n代码目录交付要求：' + layout_instruction
    if any(item.get('repair_contract') for item in feedback):
        from .code_repair_contract import DELIVERY_GUIDANCE
        instruction += '\n' + DELIVERY_GUIDANCE
    if feedback:
        path = implementation.parent / ('review-feedback-' + hashlib.sha256(_json(feedback).encode()).hexdigest()[:16] + '.md')
        text = ('# 当前交付修改指令\n\n仅处理本文件当前清单，不重开历史已解决意见。\n'
                'repair_contract 内的修改已绑定批准实现文档和本次交付，由独立角色核对；'
                '严格逐项遵守其中的原文、change 和 verification，并保存实际测试回执。'
                '不得因自然语言建议改写批准文档或扩张需求；发现冲突须报告，不能自行猜测。\n'
                '审查只核对要求完成情况；修改代码由 Codex 完成。\n'
                '先核对 local_acceptance.checks 中的真实参数、退出码及 stdout/stderr，'
                '以具体运行错误定位修复；artifact_path 保存完整运行文件。'
                'truncated 标记表示这里只提供了摘录。不要把后续缺少指标的汇总错误当作根因，'
                '也不要要求用户重新提供已经附带的运行信息。\n\n```json\n' + _json(feedback) + '\n```\n')
        _write_immutable(path, text.encode('utf-8'))
        pointer = {'path': str(path), 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        if pointer not in bundle.setdefault('feedback_documents', []):
            bundle['feedback_documents'].append(pointer)
        instruction += '\n处理审查意见："' + str(path) + '"。'
    return instruction

