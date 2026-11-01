"""Once deliver clipped, authenticated predecessor reads to the save author.

The saved draft, paid answer, and read ledger stay unchanged. This grants no
tools or reading credit: the successor must actually receive the cited text,
return the original submission, and pass ordinary author validation.
"""
from __future__ import annotations

import copy
import json

from . import implementation_authoring as author
from .implementation_author_recovery_scope import SCOPE_VERSION, workflow_fingerprint
from .implementation_review_routing import digest
from .architecture_validation import operation
from .storage import now, uid

KEY = 'implementation_author_inherited_delivery_recovery'
VERSION = 1


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现作者继承原文交付恢复：' + message)


def _scope(h, run_id, role, purpose, hid, progress):
    from .engine import Harness
    from .implementation_authoring_completion import enabled
    engine = h.engine
    assignment, context, state, binding = author._assignment(engine, run_id, role, hid)
    inputs = engine.store.get('handoff_inputs', hid, {})
    record = engine.store.get(author.COLLECTION, hid, {})
    run = engine.store.get('runs', run_id, {})
    payload = {key: inputs.get(key) for key in ('instruction', 'context', 'schema')}
    if (role != 'engineer' or not purpose.startswith('implementation_spec:') or not enabled(context)
            or assignment.get('id') != hid or assignment.get('purpose') != purpose
            or inputs.get('id') != hid or inputs.get('run_id') != run_id
            or inputs.get('schema') != author.submission_schema(context)
            or assignment.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, role, purpose, **payload) != hid
            or progress.get('id') != hid or progress.get('run_id') != run_id
            or progress.get('mode') != 'final_only' or progress.get('pending_tool_calls')
            or run.get('status') != 'running' or run.get('stage') != 'implementation'
            or state.get('status') != 'specification' or state.get('current_revision') != binding.get('revision')
            or not record or record.get('status') != 'draft' or record.get('id') != hid
            or record.get('run_id') != run_id or record.get('role') != role
            or record.get('workflow_id') != state.get('id') or record.get('revision') != binding.get('revision')
            or record.get('row', {}).get('index') != binding.get('revision')
            or record.get('context') != context or engine.store.get('decisions', hid)):
        _fail('冻结输入、保存交接、草稿或当前工作流已改变。')
    if engine.store.record_keys('tool_invocations', run_id,
            predicate="json_extract(data,'$.handoff_id')=? AND json_extract(data,'$.status') IN ('pending','running','unknown')",
            parameters=[hid]):
        _fail('存在未核对工具意图，不能重发或交付原文。')
    completion = context['implementation_document_completion']
    predecessor = completion['source_handoff_id']
    return {'scope_version': SCOPE_VERSION, 'input_sha256': digest(inputs), 'context_sha256': digest(context),
        'draft_sha256': digest(record), 'configuration_sha256': digest(run.get('config')), 'mode': run.get('mode'),
        'workflow_sha256': workflow_fingerprint(state),
        'run_state_sha256': digest({name: run.get('state', {}).get(name) for name in (
            'research_cycle', 'implementation_generation', 'implementation_restart', 'implementation_graph_thread_id')}),
        'normal_request_sha256': digest(progress.get('normal_request')),
        'tool_results_sha256': digest(progress.get('tool_results')), 'turn': progress.get('turn'),
        'predecessor_input_sha256': digest(engine.store.get('handoff_inputs', predecessor)),
        'predecessor_progress_sha256': digest(engine.store.get('handoff_progress', predecessor)),
        'predecessor_audit_sha256': digest(engine.store.get('stage_contexts', completion['predecessor_audit_id'])),
        'save_resume_sha256': digest(progress.get('implementation_document_save_resume'))}, record, context


def _required(engine, run_id, hid, result, record, context):
    """Every missing passage must be a sealed predecessor read, not new reading."""
    from . import document_index, implementation_author_inherited_reading as inherited
    from .implementation_author_delivery_recovery import _clipped
    from .document_read_window import _merge_ranges
    receipts, allowed = inherited._lineage(engine, run_id, hid, context)
    current = record['row']['document']
    documents = {doc['document_id']: doc for doc in [current, *context.get('indexed_documents', [])]}
    refs = {}
    for reply in result['issue_responses']:
        for ref in reply['evidence']:
            doc = documents.get(ref['document_id'])
            if not doc or doc['sha256'] != ref['sha256']:
                _fail('候选引用了原冻结范围外的版本。')
            refs.setdefault(ref['document_id'], []).append({'result': {'section_id': ref['section_id'],
                'char_start': ref['start'], 'char_end': ref['end']}})
    required = []
    progress = engine.store.get('handoff_progress', hid, {})
    for did, ranges in refs.items():
        doc = documents[did]
        report = inherited.coverage(engine, run_id, doc, ranges, hid, context)
        if not report['missing_ranges']:
            continue
        if did == current['document_id'] or allowed.get(did, {}).get('sha256') != doc['sha256']:
            return None
        manifest, text = document_index.load(engine, run_id, did, doc['sha256'])
        spans = {}
        for receipt in receipts:
            call, page = receipt['call'], receipt['result']
            if call.get('name') != 'documents.read' or page.get('document_id') != did:
                continue
            args = call['arguments']
            section = next((row for row in manifest['sections'] if row['id'] == args.get('section_id')), None)
            start, end = page.get('char_start'), page.get('char_end')
            offset, limit = args.get('offset', 0), args.get('limit', 4000)
            if (args.get('document_id') != did or args.get('expected_sha256') != doc['sha256']
                    or page.get('sha256') != doc['sha256'] or section is None
                    or page.get('section_id') != section['id'] or type(start) is not int or type(end) is not int
                    or type(offset) is not int or type(limit) is not int or not 1 <= limit <= 6000
                    or not section['start'] <= start < end <= section['end']
                    or start != section['start'] + offset or end > start + limit or page.get('text') != text[start:end]):
                _fail('前驱回执的版本、精确范围或原文已改变。')
            spans.setdefault(section['id'], []).append((start, end))
        missing = {}
        for row in report['missing_ranges']:
            cursor = row['start']
            for start, end in _merge_ranges(spans.get(row['section_id'], [])):
                if start > cursor:
                    break
                cursor = max(cursor, end)
            if cursor < row['end']:
                return None
            missing.setdefault(row['section_id'], []).append((row['start'], row['end']))
        merged = [{'section_id': sid, 'start': start, 'end': end}
            for sid, rows in sorted(missing.items()) for start, end in _merge_ranges(rows)]
        if not _clipped(progress['finalization'], progress, doc, merged):
            return None
        required.append({'document': copy.deepcopy(doc), 'required_ranges': merged})
    return sorted(required, key=lambda row: row['document']['document_id'])


def _source(h, run_id, audit):
    from .execution_archive import load
    from .implementation_save_resume import _settled_response
    try:
        source = load(h.engine.store, run_id, 'engineer', audit['source_archive_id'],
            audit['source_archive_sha256'])['finalization']
    except (KeyError, ValueError, OSError) as exc:
        _fail('原答复归档无法验证：' + str(exc))
    if (digest(source) != audit['source_finalization_sha256'] or source.get('status') != 'response_saved'
            or source.get('operation_id') != audit['source_operation_id']
            or digest(source.get('response', {}).get('json')) != audit['candidate_sha256']
            or not _settled_response(h.engine.store, run_id, source)
            or source.get('provider_id') is None and h.engine.store.get('runs', run_id, {}).get('mode') != 'demo'):
        _fail('原付费答复身份或结算回执已改变；未重发原请求。')
    return source


def _checked(h, run_id, role, purpose, hid, progress, *, rederive=False):
    audit = progress.get(KEY)
    if audit is None:
        return None
    if (not isinstance(audit, dict) or type(audit.get('version')) is not int or audit['version'] != VERSION
            or audit.get('scope_version') != SCOPE_VERSION or audit.get('run_id') != run_id
            or audit.get('role') != role or audit.get('purpose') != purpose or audit.get('handoff_id') != hid
            or type(audit.get('attempt')) is not int or audit['attempt'] != 1
            or audit.get('tools_allowed') is not False or audit.get('scientific_approval') is not False
            or audit.get('sha256') != digest({key: value for key, value in audit.items() if key != 'sha256'})
            or _scope(h, run_id, role, purpose, hid, progress)[0] != audit.get('scope')):
        _fail('唯一补交的版本、配置、前驱账本或冻结范围已改变。')
    source = _source(h, run_id, audit)
    from .implementation_author_response_correction import KEY as FORMAT_KEY
    correction = progress.get(FORMAT_KEY)
    from .implementation_author_response_recovery import ORIGIN_KEY
    if progress.get(ORIGIN_KEY) is not None:
        _fail('不能将来源身份补正转换为继承原文交付。')
    if correction is not None:
        if correction.get('source_operation_id') != audit['operation_id']:
            _fail('后续字段补正未绑定本次实际交付。')
        from .implementation_author_response_recovery import validate_preview
        validate_preview(h, run_id, role, purpose, hid, progress)
    else:
        final = progress.get('finalization', {})
        if (final.get('operation_id') != audit['operation_id'] or digest(final.get('prepared')) != audit['prepared_sha256']
                or final.get('author_inherited_delivery_of') != source['operation_id']
                or final.get('withheld_tool_calls') or final.get('completed_result_hashes') != [
                    digest(row) for row in progress.get('tool_results', [])]):
            _fail('补交请求被替换或新增了工具。')
    # Reauthenticate the immutable predecessor on every reuse, without treating
    # its archived text as delivery to this save handoff.
    if rederive:
        from .implementation_author_inherited_reading import _lineage
        _lineage(h.engine, run_id, hid, h.engine.store.get('handoff_inputs', hid)['context'])
    return audit, source


@operation
def validate_preview(h, run_id, role, purpose, hid, progress):
    _checked(h, run_id, role, purpose, hid, progress)


@operation
def validate_dispatch(h, run_id, hid, progress):
    assignment = h.engine.store.get('handoffs', hid, {})
    checked = _checked(h, run_id, assignment.get('to_role'), assignment.get('purpose', ''), hid, progress, rederive=True)
    if checked is not None and hasattr(h.intelligence, 'provider_for'):
        _, source = checked
        provider = h.intelligence.provider_for('engineer')
        if (source.get('provider_id'), source.get('model')) != (provider.get('id'), provider.get('model')):
            _fail('必须沿用原工程师供应方和模型。')


def validate_claim(h, run_id, hid, progress):
    """Cheap scope guard inside the dispatch CAS, after all awaited reads."""
    assignment = h.engine.store.get('handoffs', hid, {})
    _checked(h, run_id, assignment.get('to_role'), assignment.get('purpose', ''), hid, progress)


@operation
def validate_candidate(h, run_id, role, purpose, hid, result):
    progress = h.engine.store.get('handoff_progress', hid, {})
    checked = _checked(h, run_id, role, purpose, hid, progress, rederive=True)
    if checked is None:
        return
    _, source = checked
    from .implementation_author_response_correction import KEY as FORMAT_KEY, validate_correction
    correction = progress.get(FORMAT_KEY)
    if correction is not None:
        validate_correction(result, source['response']['json'], correction['problems'])
    elif result != source['response']['json']:
        _fail('补交只核对原文并返回原提交，不能更改文档、回应或证据范围。')


@operation
def prepare_schedule(h, run_id, role, purpose, hid, result, error):
    """Read-only derivation; all writes and the CAS claim stay on the caller."""
    from .engine import ResearchBlocked
    from .implementation_alignment_evidence import READ_PENDING, validate_candidate as ordinary
    from .implementation_author_response_recovery import _kind
    from .implementation_author_delivery_recovery import KEY as DELIVERY_KEY
    from .implementation_save_resume import _settled_response
    from .document_read_window import TOTAL_LIMIT, sha
    if not str(error).startswith(READ_PENDING) or role != 'engineer' or not purpose.startswith('implementation_spec:'):
        return None
    engine = h.engine
    progress = copy.deepcopy(engine.store.get('handoff_progress', hid, {}))
    if progress.get(KEY) is not None:
        _fail('一次原文补交后仍未通过普通校验；原文、草稿和答复均已保留。')
    if _kind(progress) is not None or progress.get(DELIVERY_KEY) is not None:
        return None
    context = engine.store.get('handoff_inputs', hid, {}).get('context', {})
    from .implementation_authoring_completion import enabled
    if not enabled(context):
        return None
    engine.check_active(run_id)
    scope, record, context = _scope(h, run_id, role, purpose, hid, progress)
    source = progress.get('finalization', {})
    if (source.get('status') != 'response_saved' or not source.get('operation_id')
            or source.get('withheld_tool_calls') or source.get('response', {}).get('json') != result
            or source.get('prepared', {}).get('role') != role
            or source.get('prepared', {}).get('context', {}).get('_purpose') != purpose
            or not _settled_response(engine.store, run_id, source)
            or source.get('provider_id') is None and engine.store.get('runs', run_id, {}).get('mode') != 'demo'):
        _fail('只允许已完整接收并结算、没有待执行工具的原答复。')
    if hasattr(h.intelligence, 'provider_for'):
        provider = h.intelligence.provider_for(role)
        if (source.get('provider_id'), source.get('model')) != (provider.get('id'), provider.get('model')):
            _fail('原工程师供应方或模型已改变。')
    try:
        ordinary(engine, run_id, role, hid, result, context)
    except ResearchBlocked as exc:
        if not str(exc).startswith(READ_PENDING):
            raise
    else:
        return None
    required = _required(engine, run_id, hid, result, record, context)
    if not required:
        return None
    count = sum(row['end'] - row['start'] for doc in required for row in doc['required_ranges'])
    if count > TOTAL_LIMIT:
        _fail(f'必要继承原文共 {count} 字符，超过 {TOTAL_LIMIT} 字符交付窗口；未扩大额度或派发。')
    operation_id = uid()
    inputs = engine.store.get('handoff_inputs', hid)
    marker = {'version': VERSION, 'role': role, 'required_documents': required, 'scientific_approval': False}
    control = {'mode': 'final_only', 'operation_id': operation_id,
        'reason': 'Once deliver clipped authenticated predecessor passages; no tools or edits'}
    prepared = h._document_prepare(run_id, role, purpose, inputs['instruction'], context,
        inputs['schema'], progress['tool_results'], final_control=control, inherited_delivery=marker)
    prepared = h._working_request(run_id, role, hid, progress, prepared)
    projection = prepared['context'].get('document_read_window', {}).get('inherited_delivery_projection', {})
    if (projection.get('required_documents_sha256') != sha(required)
            or projection.get('required_read_chars') != count or projection.get('protected_read_chars') != count
            or projection.get('unread_required_chars') != 0):
        _fail('新窗口未完整保留候选引用的继承原文，未领取补交。')
    # Only one no-tool acknowledgment is needed. Pin the complete original
    # output so a receipt repair cannot become another document revision.
    prepared['schema'] = {'enum': [copy.deepcopy(result)]}
    prepared['task'] += ('\n宿主已将你在原编写交接实际读取、但在保存窗口被裁剪的同版本原文完整补交。'
        '逐项核对实际收到的原文，随后原样返回下面的完整 JSON；禁止工具调用、编辑文档或改变回应和证据。'
        '本次交付不代表科学批准，仍须普通校验与三方审核。\n原提交 JSON：' +
        json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return {'scope': scope, 'progress': progress, 'source': source, 'required_documents': required,
        'candidate_sha256': digest(result), 'operation_id': operation_id, 'prepared': prepared}


def commit_schedule(h, run_id, role, purpose, hid, result, plan):
    if plan is None:
        return False
    from .execution_archive import save
    from .tool_request_recovery import prepared_successor
    engine = h.engine
    engine.check_active(run_id)
    progress = plan['progress']
    if (digest(result) != plan['candidate_sha256'] or engine.store.get('handoff_progress', hid) != progress
            or _scope(h, run_id, role, purpose, hid, progress)[0] != plan['scope']):
        _fail('原检查点或冻结范围在核验期间改变，未领取补交。')
    archive = save(engine.store, run_id, role, hid + ':author-inherited-delivery:v1',
        {'finalization': plan['source']}, handoff_id=hid, request_id=plan['source']['response'].get('request_id'))
    if archive.get('redacted'):
        _fail('原答复含已清理敏感信息，不能完整绑定恢复。')
    final = {'status': 'ready', 'operation_id': plan['operation_id'], 'prepared': plan['prepared'],
        'reason': 'Once deliver clipped authenticated predecessor passages; no tools or edits',
        'created_at': now(), 'withheld_tool_calls': [],
        'author_inherited_delivery_of': plan['source']['operation_id'],
        'completed_result_hashes': [digest(row) for row in progress['tool_results']]}
    final = prepared_successor(engine.store, hid, final)
    audit = {'version': VERSION, 'scope_version': SCOPE_VERSION, 'run_id': run_id, 'role': role,
        'purpose': purpose, 'handoff_id': hid, 'attempt': 1, 'scope': plan['scope'],
        'source_archive_id': archive['id'], 'source_archive_sha256': archive['sha256'],
        'source_finalization_sha256': digest(plan['source']), 'source_operation_id': plan['source']['operation_id'],
        'candidate_sha256': plan['candidate_sha256'], 'operation_id': plan['operation_id'],
        'prepared_sha256': digest(plan['prepared']), 'required_documents': plan['required_documents'],
        'tools_allowed': False, 'scientific_approval': False, 'old_request_replayed': False, 'created_at': now()}
    audit['sha256'] = digest(audit)
    def claim(current):
        engine.check_active(run_id)
        if current != progress or _scope(h, run_id, role, purpose, hid, current)[0] != plan['scope']:
            _fail('补交已被领取或原检查点改变，未重复派发。')
        return {**current, KEY: audit, 'mode': 'final_only', 'finalization': final}
    engine.store.mutate('handoff_progress', hid, claim)
    engine.store.event(run_id, 'implementation.author_inherited_delivery_recovery',
        '继承原文已封存但未完整交付；仅一次无工具补交，当前文档与原提交不变。',
        {'handoff_id': hid, 'operation_id': plan['operation_id'], 'source_operation_id': plan['source']['operation_id'],
         'required_chars': sum(row['end'] - row['start'] for doc in plan['required_documents'] for row in doc['required_ranges']),
         'tools_allowed': False, 'scientific_approval': False})
    return True


def schedule(h, run_id, role, purpose, hid, result, error):
    return commit_schedule(h, run_id, role, purpose, hid, result,
        prepare_schedule(h, run_id, role, purpose, hid, result, error))
