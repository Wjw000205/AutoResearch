"""Accept reviewer revisions by meaning, then run the normal source validators.

An unsealed answer is a draft, not a frozen scientific decision. Its paid
request and original text stay in the journal; the reviewer may revise the
draft without reproducing its wording, array positions or evidence count.
"""
from __future__ import annotations

import copy

VERSION = 2
FIELD = 'review_response_policy_version'
BINDINGS = ('proposal_hash', 'spec_hash', 'code_sha256', 'delivery_hash',
            'document_sha256', 'alignment_binding')
TEXT_SUFFIXES = {'.py', '.pyi', '.json', '.yaml', '.yml', '.toml', '.ini',
                 '.cfg', '.md', '.txt', '.sh', '.ps1', '.bat', '.cmd'}
GUIDANCE = ('本次采用可修订评审交付：尚未封存的答复可以调整措辞、顺序、证据集合、理由和原问题处置，'
            '无需逐字复制旧答复。允许结合实际证据修订原判断；原始请求和答复由宿主留档。'
            '旧交接中的逐字保留、只追加和固定顺序限制由本接收规则替代，科学方案及验收标准仍按冻结版本核对。'
            '请回应本角色原问题，具体代码修改须有对应代码依据；待复核、文档冲突、缺定义或缺执行回执'
            '可如实说明依赖，不必编造代码证据。需要时定向读取，不要求重复阅读已有有效依据。'
            '宿主仍核对当前版本、实际引用和批准条件；接收意见不等于批准改码或启动实验。')


def validate_binding(value, original):
    """Lock the assigned source version, not the author's provisional opinion."""
    from .engine import ResearchBlocked
    if not isinstance(value, dict) or any(
            key in original and value.get(key) != original[key] for key in BINDINGS):
        raise ResearchBlocked('评审修订必须对应同一冻结方案、规范和代码版本。')


def evidence_required(finding):
    return finding.get('disposition') == 'repair_code' or bool(finding.get('regression'))


def acceptance_schema(schema):
    """A dependency report need not pretend it has code evidence.

    This is a host acceptance revision; callers keep the original dispatched
    schema intact. Actionable findings still require evidence in validate_review.
    """
    result = copy.deepcopy(schema)
    for branch in result.get('anyOf', [result]):
        fields = branch.get('properties', {})
        for field in ('repair_findings', 'clarification_responses'):
            indices = fields.get(field, {}).get('items', {}).get('properties', {}).get('evidence_indices')
            if isinstance(indices, dict):
                indices.pop('minItems', None)
    return result
