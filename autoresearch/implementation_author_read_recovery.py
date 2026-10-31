"""One bounded exact-range read continuation for a saved author's own draft.

The paid final answer is retained as an immutable archive. This does not save,
edit, approve or close anything: ordinary author validation remains mandatory.
"""
from __future__ import annotations

import copy

from . import implementation_authoring as author
from .implementation_review_routing import digest
from .implementation_author_recovery_scope import SCOPE_VERSION, workflow_fingerprint
from .storage import now

KEY = 'implementation_author_read_recovery'
VERSION = 1
EXTRA_TURNS = 2
HOST_DIAGNOSTIC = 'implementation_author_current_draft_read_required'


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现作者新稿补读：' + message)


def _checked(progress):
    audit = progress.get(KEY)
    if audit is None:
        return None
    if (not isinstance(audit, dict) or type(audit.get('version')) is not int or audit['version'] != VERSION
            or audit.get('role') != 'engineer' or audit.get('run_id') != progress.get('run_id')
            or audit.get('handoff_id') != progress.get('id')
            or type(audit.get('start_turn')) is not int or audit['start_turn'] < 1
            or type(audit.get('turn_limit')) is not int
            or audit['turn_limit'] != audit['start_turn'] + EXTRA_TURNS
            or audit.get('scientific_approval') is not False
            or audit.get('allowed_tool') != 'documents.read'
            or type(audit.get('scope_version')) is not int or audit['scope_version'] != SCOPE_VERSION
            or not isinstance(audit.get('scope'), dict)
            or type(audit['scope'].get('scope_version')) is not int or audit['scope']['scope_version'] != SCOPE_VERSION
            or not isinstance(audit.get('missing_ranges'), list) or not audit['missing_ranges']
            or audit.get('sha256') != digest({k: v for k, v in audit.items() if k != 'sha256'})):
        _fail('唯一补读记录或两轮额度已改变。')
    return audit


def tool_turn_limit(context, progress):
    """Pure sealed cap; never grants an extension from model context fields."""
    audit = _checked(progress)
    if audit is None:
        return None
    if (digest(context) != audit.get('scope', {}).get('context_sha256')
            or type(progress.get('turn')) is not int or progress['turn'] < audit['start_turn']):
        _fail('冻结作者上下文或补读轮次不一致。')
    return audit['turn_limit']


def _call(audit, call):
    if (not isinstance(call, dict) or set(call) != {'name', 'arguments'}
            or call.get('name') != 'documents.read' or not isinstance(call.get('arguments'), dict)):
        _fail('补读只允许 documents.read，不能写稿、搜索、列目录或调用其他工具。')
    args = call['arguments']
    if (set(args) != {'document_id', 'expected_sha256', 'section_id', 'offset', 'limit'}
            or args.get('document_id') != audit['document']['document_id']
            or args.get('expected_sha256') != audit['document']['sha256']
            or type(args.get('offset')) is not int or args['offset'] < 0
            or type(args.get('limit')) is not int or not 1 <= args['limit'] <= 6000):
        _fail('补读必须精确绑定当前稿版本和不超过 6000 字符的相对范围。')
    start, end = args['offset'], args['offset'] + args['limit']
    ranges = sorted((row['offset'], row['offset'] + row['limit'])
                    for row in audit['missing_ranges'] if row['section_id'] == args['section_id'])
    cursor = start
    for a, b in ranges:
        if a > cursor:
            break
        if b > cursor:
            cursor = b
        if cursor >= end:
            return
    _fail('请求范围超出原候选实际缺失的当前稿证据并集。')


def _scope(engine, run_id, role, purpose, hid, progress, *, granting=False):
    from .engine import Harness
    assignment, context, state, binding = author._assignment(engine, run_id, role, hid)
    inputs = engine.store.get('handoff_inputs', hid, {})
    record = engine.store.get(author.COLLECTION, hid, {})
    run = engine.store.get('runs', run_id, {})
    payload = {name: inputs.get(name) for name in ('instruction', 'context', 'schema')}
    if (role != 'engineer' or not purpose.startswith('implementation_spec:')
            or assignment.get('id') != hid or assignment.get('purpose') != purpose
            or inputs.get('id') != hid or inputs.get('run_id') != run_id
            or assignment.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, role, purpose, **payload) != hid
            or progress.get('id') != hid or progress.get('run_id') != run_id
            or type(progress.get('turn')) is not int or progress['turn'] < 0
            or run.get('stage') != 'implementation' or state.get('status') != 'specification'
            or state.get('current_revision') != binding.get('revision')
            or not record or record.get('status') != 'draft' or record.get('id') != hid
            or record.get('run_id') != run_id or record.get('role') != role
            or record.get('workflow_id') != state.get('id') or record.get('revision') != binding.get('revision')
            or record.get('row', {}).get('index') != binding.get('revision')
            or record.get('context') != context or engine.store.get('decisions', hid)):
        _fail('原冻结输入、作者、已保存稿或当前工作流已改变。')
    from .implementation_author_response_correction import KEY as FORMAT_KEY
    format_active = progress.get(FORMAT_KEY) is not None
    if format_active and (granting or progress.get(KEY) is None
            or progress.get('mode') != 'final_only' or progress.get('pending_tool_calls')):
        _fail('不能把无工具的回复格式补正转换为补读额度。')
    audits = progress.get('alignment_candidate_history', [])
    if not isinstance(audits, list) or any(any(item.get(name) for name in (
            'findings_format_problems', 'routing_coordinate_problems', 'confirmation_scope_problems'))
            for item in audits if isinstance(item, dict)):
        _fail('不能把其他格式恢复转换为新稿补读。')
    audit = _checked(progress)
    keys = engine.store.record_keys('tool_invocations', run_id,
        predicate="json_extract(data,'$.handoff_id')=? AND json_extract(data,'$.status') IN ('pending','running','unknown')",
        parameters=[hid])
    for key in keys:
        invocation = engine.store.get('tool_invocations', key, {})
        from .tool_invocations import identity
        pending = progress.get('pending_tool_calls', [])
        matches = [index for index, call in enumerate(pending) if call == invocation.get('call')
                   and identity(run_id, role, hid, progress['turn'], index, call) == key]
        if (granting or audit is None or invocation.get('run_id') != run_id
                or invocation.get('role') != role or invocation.get('handoff_id') != hid
                or invocation.get('id') != key or not matches
                or invocation.get('request_id') != progress.get('normal_request', {}).get('response', {}).get('request_id')):
            _fail('存在未核对或范围外的工具执行意图。')
        _call(audit, invocation['call'])
    return {'scope_version': SCOPE_VERSION, 'input_sha256': digest(inputs), 'context_sha256': digest(context),
        'draft_sha256': digest(record), 'configuration_sha256': digest(run.get('config')),
        'workflow_sha256': workflow_fingerprint(state)}, record, context


def _missing(engine, run_id, hid, result, record, context):
    """Check every cited source; missing non-draft evidence is never eligible."""
    from . import document_index, implementation_author_inherited_reading as reading
    documents = [record['row']['document'], *context.get('indexed_documents', [])]
    allowed = {doc['document_id']: doc['sha256'] for doc in documents}
    required, manifests = {}, {}
    for reply in result['issue_responses']:
        for ref in reply['evidence']:
            did, sha = ref['document_id'], ref['sha256']
            if allowed.get(did) != sha:
                _fail('证据不属于原冻结文档范围或当前版本。')
            if did not in manifests:
                manifests[did] = document_index.load(engine, run_id, did, sha)[0]
            section = next((row for row in manifests[did]['sections'] if row['id'] == ref['section_id']), None)
            if (not section or type(ref['start']) is not int or type(ref['end']) is not int
                    or not section['start'] <= ref['start'] < ref['end'] <= section['end']):
                _fail('证据不是原文的精确有效字符范围。')
            required.setdefault(did, []).append({'result': {'section_id': ref['section_id'],
                'char_start': ref['start'], 'char_end': ref['end']}})
    current = record['row']['document']
    missing = []
    for did, ranges in required.items():
        report = reading.coverage(engine, run_id, {'document_id': did, 'sha256': allowed[did]}, ranges, hid, context)
        if report['missing_ranges'] and did != current['document_id']:
            return None
        missing.extend(report['missing_ranges'])
    sections = {row['id']: row for row in manifests.get(current['document_id'], {}).get('sections', [])}
    merged = []
    for row in sorted(missing, key=lambda item: (item['section_id'], item['start'], item['end'])):
        if merged and merged[-1]['section_id'] == row['section_id'] and row['start'] <= merged[-1]['end']:
            merged[-1]['end'] = max(merged[-1]['end'], row['end'])
        else:
            merged.append(copy.deepcopy(row))
    return [{'section_id': row['section_id'], 'start': row['start'], 'end': row['end'],
             'offset': row['start'] - sections[row['section_id']]['start'], 'limit': row['end'] - row['start']}
            for row in merged]


def _source(engine, run_id, hid, audit):
    from .execution_archive import load
    from .implementation_save_resume import _settled_response
    try:
        payload = load(engine.store, run_id, 'engineer', audit['source_archive_id'], audit['source_archive_sha256'])
    except (KeyError, ValueError, OSError) as exc:
        _fail('原已结算答复归档不可验证：' + str(exc))
    final = payload.get('finalization', {})
    normal = payload.get('normal_request')
    if (engine.store.get('runs', run_id, {}).get('mode') != 'demo'
            and (final.get('provider_id') is None or normal is not None and normal.get('provider_id') is None)):
        _fail('在线研究不能以测试标签代替原付费结算回执。')
    if (digest(final) != audit.get('source_finalization_sha256') or final.get('status') != 'response_saved'
            or not final.get('operation_id') or final.get('operation_id') != audit.get('source_operation_id')
            or not _settled_response(engine.store, run_id, final)
            or digest(final.get('response', {}).get('json')) != audit.get('candidate_sha256')
            or final.get('prepared', {}).get('role') != 'engineer'
            or final.get('prepared', {}).get('context', {}).get('_purpose') != audit.get('purpose')
            or digest(normal) != audit.get('source_normal_request_sha256')
            or normal is not None and (normal.get('status') != 'response_saved'
                or not _settled_response(engine.store, run_id, normal))):
        _fail('原答复身份、结算或候选已改变；不重发原付费请求。')
    return final


def validate_scope(h, run_id, role, purpose, hid, progress):
    audit = _checked(progress)
    if audit is None:
        return
    scope, record, context = _scope(h.engine, run_id, role, purpose, hid, progress)
    if (audit.get('purpose') != purpose or scope != audit.get('scope')
            or audit['document'] != record['row']['document']
            or progress['turn'] < audit['start_turn']):
        _fail('补读范围的输入、文档、配置或工作流已经改变。')
    final = _source(h.engine, run_id, hid, audit)
    missing = _missing(h.engine, run_id, hid, final['response']['json'], record, context)
    if missing is None:
        _fail('其他冻结来源也缺少真实读取，不能扩大本次补读。')
    from .implementation_author_response_correction import KEY as FORMAT_KEY
    if progress.get(FORMAT_KEY) is not None and missing:
        _fail('回复格式补正之前仍缺实际交付的新稿证据；不能改用补正绕过读取。')
    for row in missing:
        for offset in range(row['offset'], row['offset'] + row['limit'], 6000):
            _call(audit, {'name': 'documents.read', 'arguments': {'document_id': audit['document']['document_id'],
                'expected_sha256': audit['document']['sha256'], 'section_id': row['section_id'],
                'offset': offset, 'limit': min(6000, row['offset'] + row['limit'] - offset)}})
    for call in progress.get('pending_tool_calls', []):
        _call(audit, call)


def validate_call(engine, run_id, role, hid, context, call):
    progress = engine.store.get('handoff_progress', hid, {})
    audit = _checked(progress)
    if audit is None:
        return
    assignment = engine.store.get('handoffs', hid, {})
    from types import SimpleNamespace
    validate_scope(SimpleNamespace(engine=engine), run_id, role, assignment.get('purpose', ''), hid, progress)
    from .implementation_author_response_correction import KEY as FORMAT_KEY
    if progress.get(FORMAT_KEY) is not None or progress.get('mode') == 'final_only' or progress['turn'] >= audit['turn_limit']:
        _fail('两轮定向补读已用完，不能继续执行工具。')
    _call(audit, call)


def schedule(h, run_id, role, purpose, hid, result, error):
    from .engine import ResearchBlocked
    from .implementation_alignment_evidence import READ_PENDING, validate_candidate
    from .implementation_save_resume import _settled_response
    from .execution_archive import save
    if not str(error).startswith(READ_PENDING):
        return False
    engine = h.engine
    progress = copy.deepcopy(engine.store.get('handoff_progress', hid, {}))
    if progress.get(KEY) is not None:
        _fail('一次两轮补读后仍未满足原证据要求；已保留原稿与答复，未封存或批准。')
    if role != 'engineer' or not purpose.startswith('implementation_spec:'):
        return False
    engine.check_active(run_id)
    scope, record, context = _scope(engine, run_id, role, purpose, hid, progress, granting=True)
    final = progress.get('finalization', {})
    normal = progress.get('normal_request')
    if (engine.store.get('runs', run_id, {}).get('mode') != 'demo'
            and (final.get('provider_id') is None or normal is not None and normal.get('provider_id') is None)):
        _fail('在线研究不能以测试标签领取补读额度。')
    if (progress.get('mode') != 'final_only' or progress.get('pending_tool_calls')
            or final.get('status') != 'response_saved' or not final.get('operation_id')
            or final.get('withheld_tool_calls') or final.get('response', {}).get('json') != result
            or final.get('prepared', {}).get('role') != role
            or final.get('prepared', {}).get('context', {}).get('_purpose') != purpose
            or not _settled_response(engine.store, run_id, final)
            or normal is not None and (normal.get('status') != 'response_saved'
                or not _settled_response(engine.store, run_id, normal))):
        _fail('只允许真实保存稿的已结算 final 答复，不能越过在途请求或未执行工具。')
    try:
        validate_candidate(engine, run_id, role, hid, result, context)
    except ResearchBlocked as exc:
        if not str(exc).startswith(READ_PENDING):
            raise
    else:
        return False
    missing = _missing(engine, run_id, hid, result, record, context)
    if not missing:
        return False
    archive = save(engine.store, run_id, role, hid + ':author-current-draft-read:v1',
        {'finalization': final, 'normal_request': copy.deepcopy(normal)},
        handoff_id=hid, request_id=final['response'].get('request_id'))
    if archive.get('redacted'):
        _fail('原答复含已清理敏感信息，无法完整绑定本次恢复。')
    next_turn = progress['turn'] + 1
    audit = {'version': VERSION, 'scope_version': SCOPE_VERSION,
        'run_id': run_id, 'role': role, 'purpose': purpose, 'handoff_id': hid,
        'scope': scope, 'document': copy.deepcopy(record['row']['document']),
        'revision': record['revision'], 'spec_hash': record['row']['spec_hash'],
        'start_turn': next_turn, 'turn_limit': next_turn + EXTRA_TURNS, 'missing_ranges': missing,
        'source_archive_id': archive['id'], 'source_archive_sha256': archive['sha256'],
        'source_finalization_sha256': digest(final), 'source_operation_id': final['operation_id'],
        'source_normal_request_sha256': digest(normal),
        'candidate_sha256': digest(result), 'created_at': now(), 'allowed_tool': 'documents.read',
        'scientific_approval': False, 'old_request_replayed': False}
    audit['sha256'] = digest(audit)
    reads = []
    for row in missing:
        for offset in range(row['offset'], row['offset'] + row['limit'], 6000):
            reads.append({'name': 'documents.read', 'arguments': {'document_id': audit['document']['document_id'],
                'expected_sha256': audit['document']['sha256'], 'section_id': row['section_id'],
                'offset': offset, 'limit': min(6000, row['offset'] + row['limit'] - offset)}})
    diagnostic = {'host_diagnostic': HOST_DIAGNOSTIC, 'error': str(error), KEY: {
        'version': VERSION, 'original_candidate': copy.deepcopy(result), 'candidate_sha256': digest(result),
        'document': copy.deepcopy(audit['document']), 'revision': audit['revision'], 'spec_hash': audit['spec_hash'],
        'missing_ranges': copy.deepcopy(missing), 'read_calls': reads, 'extra_normal_turns': EXTRA_TURNS,
        'scientific_approval': False, 'instruction': '当前稿已保存，但这些证据尚未进入本角色模型上下文。'
            '只能按列出的相对 offset/limit 调用 documents.read；禁止写入、搜索、列目录和其他工具。'
            '最多两轮补读，然后按原 Schema 返回真实同版完整 final；读取不等于科学批准或关闭异议。'}}
    def claim(current):
        if current != progress or _scope(engine, run_id, role, purpose, hid, current, granting=True)[0] != scope:
            _fail('补读已领取或原检查点改变，未追加新额度。')
        value = {k: v for k, v in current.items() if k not in {'mode', 'normal_request', 'finalization'}}
        value.update({KEY: audit, 'turn': next_turn, 'pending_tool_calls': [], 'tool_cursor': 0, 'tool_counted': False})
        value.setdefault('tool_results', []).append(diagnostic)
        return value
    engine.check_active(run_id)
    engine.store.mutate('handoff_progress', hid, claim)
    engine.store.event(run_id, 'implementation.author_current_draft_read_recovery',
        '仅补读当前保存稿中原候选缺失的引用范围，最多两轮；原付费答复已归档，未重复保存或批准。',
        {'handoff_id': hid, 'source_archive_id': archive['id'], 'source_operation_id': final['operation_id'],
         'document_id': audit['document']['document_id'], 'sha256': audit['document']['sha256'],
         'missing_chars': sum(row['limit'] for row in missing), 'turn_limit': audit['turn_limit'],
         'scientific_approval': False})
    return True
