"""Return invalid evidence locators to their owner without changing a verdict."""
from __future__ import annotations

import copy
from pathlib import PurePosixPath

ERROR = '代码对齐依据引用了当前冻结交付之外的文件或版本。'
NON_CODE_ERROR = '代码对齐依据必须定位实际 Python 实现或测试，清单与完成声明不能代替代码。'
FIELD = 'code_reference_problems'
DIAGNOSTIC = 'implementation_code_reference_required'
PAYLOAD_ERRORS = (
    '代码引用补正不能删除或增加原证据项。',
    '代码引用补正只能选择同一验收 ID 的当前 Python 实现或测试及真实范围。',
    '代码引用补正只能修正已诊断的 artifact_id；原结论、异议、SHA 和证据范围必须保留。')
RECOVERABLE_ERRORS = (NON_CODE_ERROR, *PAYLOAD_ERRORS)


def problems(engine, run_id, handoff_id, result, context):
    """Diagnose copying errors or a non-code file cited instead of Python."""
    from .implementation_alignment_evidence import _code_text, _code_spans
    deliveries = context.get('delivery_artifacts', [])
    by_id = {row['artifact_id']: row for row in deliveries}
    locations = {row['id']: {path.split(':', 1)[0] for path in row['locations']}
                 for row in context.get('implementation_alignment_manifest_requirements', [])}
    found = []
    for index, item in enumerate(result.get('code_evidence', [])):
        if not isinstance(item, dict) or not isinstance(item.get('artifact_id'), str):
            return []
        identity, sha = item['artifact_id'], item.get('sha256')
        existing = by_id.get(identity)
        if existing and existing.get('sha256') == sha:
            name = PurePosixPath(existing['path'].replace('\\', '/'))
            from .code_repair_clarification import enabled as clarification_enabled
            clarification = clarification_enabled(context)
            if name.suffix.lower() not in {'.py', '.pyi'} and not (
                    clarification and existing['path'] == 'implementation.json'):
                # Selection remains the reviewer's responsibility. The host
                # supplies only exact current candidates, never substitute proof.
                targets = [row for row in deliveries if row['path'] in locations.get(item.get('requirement_id'), set())
                           and PurePosixPath(row['path'].replace('\\', '/')).suffix.lower() in {'.py', '.pyi'}]
                if existing['path'] not in locations.get(item.get('requirement_id'), set()) or not targets:
                    return []
                _code_text(engine, run_id, identity, existing)
                for target in targets:
                    _code_text(engine, run_id, target['artifact_id'], target)
                found.append({'index': index, 'requirement_id': item.get('requirement_id'),
                    'kind': 'non_code_target', 'original_artifact_id': identity,
                    'original_sha256': sha, 'path': existing['path'], 'allowed_targets': copy.deepcopy(targets)})
            continue
        # A wrong SHA is a version conflict, not a locator copying error.
        if existing or not identity.startswith(run_id + ':'):
            return []
        candidates = [row for row in deliveries if row.get('sha256') == sha
                      and identity.endswith('/' + row['path'])
                      and row['path'] in locations.get(item.get('requirement_id'), set())
                      and PurePosixPath(row['path']).suffix.lower() in {'.py', '.pyi'}]
        if len(candidates) != 1:
            return []
        target = candidates[0]
        offset, limit = item.get('offset'), item.get('limit')
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
            return []
        text = _code_text(engine, run_id, target['artifact_id'], target)
        if offset + limit > len(text):
            return []
        cursor = offset
        for start, end in _code_spans(engine, handoff_id, target['artifact_id'], sha, text):
            if start <= cursor:
                cursor = max(cursor, end)
        if cursor < offset + limit:
            return []
        found.append({'index': index, 'requirement_id': item.get('requirement_id'),
                      'original_artifact_id': identity, 'artifact_id': target['artifact_id'],
                      'path': target['path'], 'sha256': sha, 'offset': offset, 'limit': limit})
    return found


def verify_archive(engine, run_id, role, purpose, key, context, entry):
    from .engine import ResearchBlocked, digest
    from .implementation_alignment_recovery import _verify_format_source
    _verify_format_source(entry, role, purpose, problem_field=FIELD)
    sources = entry.get('source_requests')
    if (not sources or digest(sources) != entry.get('code_reference_requests_sha256')
            or digest(context) != entry.get('code_reference_context_sha256')
            or problems(engine, run_id, key, entry['candidate'], context) != entry[FIELD]):
        raise ResearchBlocked('代码引用补正的原请求、冻结交付或真实读取依据已改变。')


def recoverable_blocked_run(engine, run):
    """Authenticate an old host's received but unsealed target-selection error.

    This only admits the original graph under its existing execution intent.
    The ordinary harness then archives the answer and requests owner correction;
    no vote, provider response, version or scientific gate is synthesized here.
    """
    if (run.get('status') != 'blocked' or run.get('stage') != 'implementation'
            or run.get('error') not in RECOVERABLE_ERRORS):
        return False
    if run.get('error') in PAYLOAD_ERRORS:
        from .code_reference_delivery_recovery import recoverable_blocked_run as received_correction
        return received_correction(engine, run)
    from .engine import ResearchBlocked, digest
    from .implementation_alignment_evidence import validate_candidate
    run_id = run['id']
    keys = engine.store.record_keys('handoffs', run_id,
        predicate="json_extract(data,'$.status')='blocked' AND json_extract(data,'$.error')=?",
        parameters=(NON_CODE_ERROR,))
    for key in keys:
        try:
            assignment = engine.store.get('handoffs', key, {})
            if not isinstance(assignment, dict):
                continue
            purpose, role = assignment.get('purpose', ''), assignment.get('to_role')
            if (not isinstance(purpose, str) or purpose.split(':', 1)[0] != 'code_review'
                    or role not in {'director', 'engineer', 'scout'}
                    or engine.store.get('decisions', key)):
                continue
            context = engine.store.get('handoff_inputs', key, {})['context']
            progress = engine.store.get('handoff_progress', key, {})
            if not isinstance(context, dict) or not isinstance(progress, dict):
                continue
            slot = 'finalization' if progress.get('mode') == 'final_only' else 'normal_request'
            source = progress.get(slot) or {}
            if not isinstance(source, dict) or not isinstance(source.get('response'), dict):
                continue
            candidate = source['response'].get('json')
            if (source.get('status') != 'response_saved' or not isinstance(candidate, dict)
                    or progress.get('pending_tool_calls')):
                continue
            try:
                validate_candidate(engine, run_id, role, key, candidate, context, delivery=True)
            except ResearchBlocked as exc:
                rows = getattr(exc, FIELD, [])
                if str(exc) != NON_CODE_ERROR or not any(r.get('kind') == 'non_code_target' for r in rows):
                    continue
                sources = {name: progress[name] for name in ('normal_request', 'finalization') if name in progress}
                entry = {'candidate': candidate, 'candidate_sha256': digest(candidate), FIELD: rows,
                    'source_requests': sources, 'source_mode': progress.get('mode'), 'source_turn': progress.get('turn'),
                    'code_reference_requests_sha256': digest(sources), 'code_reference_context_sha256': digest(context)}
                verify_archive(engine, run_id, role, purpose, key, context, entry)
                return True
        except (ResearchBlocked, KeyError, TypeError, ValueError, OSError):
            continue  # A malformed, foreign or uncertain receipt grants no retry.
    return False


def _legacy_validate_correction(result, entry):
    from .engine import ResearchBlocked, digest
    from .code_reference_delivery_recovery import DeliveryRejected
    original = entry['candidate']
    expected = copy.deepcopy(original)
    for row in entry[FIELD]:
        if row.get('kind') == 'non_code_target':
            items = result.get('code_evidence')
            if not isinstance(items, list) or len(items) != len(original['code_evidence']):
                raise DeliveryRejected(PAYLOAD_ERRORS[0], result, entry)
            item = items[row['index']]
            if item == original['code_evidence'][row['index']]:
                continue  # An unchanged answer is still invalid, never approval.
            if (not isinstance(item, dict) or not any(
                    item.get('artifact_id') == target['artifact_id'] and item.get('sha256') == target['sha256']
                    for target in row['allowed_targets'])
                    or type(item.get('offset')) is not int or item['offset'] < 0
                    or type(item.get('limit')) is not int or not 1 <= item['limit'] <= 16000):
                raise DeliveryRejected(PAYLOAD_ERRORS[1], result, entry)
            for field in ('artifact_id', 'sha256', 'offset', 'limit', 'verification'):
                expected['code_evidence'][row['index']][field] = item.get(field)
        else:
            expected['code_evidence'][row['index']]['artifact_id'] = row['artifact_id']
    if digest(result) not in {digest(expected), digest(original)}:
        raise DeliveryRejected(PAYLOAD_ERRORS[2], result, entry)


def validate_correction(result, entry):
    from .review_response_policy import validate_binding
    validate_binding(result, entry['candidate'])


def diagnostic(result, attempt, rows, *, error=None):
    from .engine import digest
    selection = any(row.get('kind') == 'non_code_target' for row in rows)
    return {'host_diagnostic': DIAGNOSTIC, 'error': error or (NON_CODE_ERROR if selection else ERROR),
        'alignment_read_correction': {'attempt': attempt, 'max_attempts': None},
        'code_reference_correction': {
            'candidate_sha256': digest(result), 'original_candidate': copy.deepcopy(result),
            'invalid_references': copy.deepcopy(rows),
            'instruction': '原付费答复已留档，当前意见尚未封存。请修正所列引用，返回完整评审。'
                '可以重排、增删或替换证据，并根据实际依据修订理由和原问题处置；无需复制原文或保持证据条数。'
                '引用须来自本次实际交付和实际读取范围。配置及说明可提供相应依据，批准实现仍须实际代码覆盖。'
                '修订后继续核对当前版本、引用、问题范围及批准条件。'}}
