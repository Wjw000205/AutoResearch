"""Repair a copied code locator, never a delivery version or a review verdict."""
from __future__ import annotations

import copy
from pathlib import PurePosixPath

ERROR = '代码对齐依据引用了当前冻结交付之外的文件或版本。'
FIELD = 'code_reference_problems'
DIAGNOSTIC = 'implementation_code_reference_required'


def problems(engine, run_id, handoff_id, result, context):
    """Only unique, same-hash, already-read files can explain a bad locator."""
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


def validate_correction(result, entry):
    from .engine import ResearchBlocked, digest
    original = entry['candidate']
    expected = copy.deepcopy(original)
    for row in entry[FIELD]:
        expected['code_evidence'][row['index']]['artifact_id'] = row['artifact_id']
    if digest(result) not in {digest(expected), digest(original)}:
        raise ResearchBlocked('代码引用补正只能修正已诊断的 artifact_id；原结论、异议、SHA 和证据范围必须保留。')


def diagnostic(result, attempt, rows):
    from .engine import digest
    return {'host_diagnostic': DIAGNOSTIC, 'error': ERROR,
        'alignment_read_correction': {'attempt': attempt, 'max_attempts': None},
        'code_reference_correction': {
            'candidate_sha256': digest(result), 'original_candidate': copy.deepcopy(result),
            'invalid_references': copy.deepcopy(rows),
            'instruction': '原付费答复已归档，尚未封存决定。这是代码引用标识抄写错误；'
                '所列文件的 SHA、验收位置和本角色实际读取范围已由宿主核对。'
                '由原评审角色返回原 Schema 的完整答复，只将所列 code_evidence[index].artifact_id '
                '改为所列精确 artifact_id。其余字段必须原样保留，包括 approve、issues、fixes、'
                'alignment_findings、SHA、offset、limit、implementation_refs 和 verification。'
                '无需重复补读，不得改变结论、删除异议、换版本或修改代码。补正后仍执行完整版本和证据校验。'}}
