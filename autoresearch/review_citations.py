"""Discard unsupported citation IDs without rewriting a saved public review.

The caller supplies task-owned verified-paper/source registries, preserves the
raw provider response, and runs the ordinary schema and scientific gates on the
derived result. Removing an invalid citation neither verifies a claim nor turns
dissent into approval. No model call, retrieval, storage mutation, or ID guessing
is performed here.
"""
from __future__ import annotations

import copy
import hashlib
import json

from .constructive_review import ConstructiveReviewError, validate_references


__all__ = ['VERSION', 'CitationRepairError', 'discard_invalid_citations']
VERSION = 1


class CitationRepairError(ValueError):
    """An invalid structure or context path cannot be discarded as a citation."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode('utf-8')).hexdigest()


def _allowed(rows, field, *, papers=False):
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, (list, tuple)):
        raise CitationRepairError('引用合法域必须是任务内的结构化记录列表')
    result = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(field), str) or not row[field].strip():
            raise CitationRepairError('引用合法域缺少精确标识')
        if papers and (row.get('verified') is not True or
                isinstance(row.get('verification'), dict) and row['verification'].get('status') != 'verified'):
            continue
        result.add(row[field])
    return result


def _identifiers(value):
    if (not isinstance(value, list) or any(not isinstance(identifier, str) or not identifier.strip()
                                          for identifier in value)):
        raise CitationRepairError('引用字段必须是非空字符串组成的列表')
    # Preserve duplicate valid entries. The original schema/review gate decides
    # whether duplicates are allowed; citation filtering never repairs them.
    return value


def discard_invalid_citations(review, context, *, verified_papers, sources):
    """Return a copied review and audit; delete only IDs outside the exact domain.

    ``fixes[*].evidence_ids`` accepts task sources or verified task papers.
    ``problem_review.evidence_ids`` accepts only verified task papers. Explicit
    public-context paths are validated unchanged, never silently discarded.
    Remaining response structure and scientific sufficiency are caller gates.
    """
    if not isinstance(review, dict) or not isinstance(context, dict):
        raise CitationRepairError('丢弃无效引用需要原回复与冻结上下文')
    try:
        validate_references(review, context)
    except ConstructiveReviewError as exc:
        raise CitationRepairError(str(exc)) from exc
    papers = _allowed(verified_papers, 'id', papers=True)
    source_ids = _allowed(sources, 'source_id')
    original_count, valid_count, discarded = 0, 0, []
    derived = copy.deepcopy(review)

    def filter_ids(container, path, domain, allowed):
        nonlocal original_count, valid_count
        identifiers = _identifiers(container.get('evidence_ids'))
        original_count += len(identifiers)
        retained = []
        for index, identifier in enumerate(identifiers):
            if identifier in allowed:
                retained.append(identifier)
            else:
                discarded.append({'path': f'{path}/evidence_ids/{index}', 'original_id': identifier,
                    'domain': domain, 'reason': '不在当前任务的合法引用集合中；该引用已废弃，不能作为支持材料。'})
        container['evidence_ids'] = retained
        valid_count += len(retained)

    for index, fix in enumerate(derived['fixes']):
        filter_ids(fix, f'/fixes/{index}', 'task_paper_or_source', papers | source_ids)
    if 'problem_review' in derived:
        if not isinstance(derived['problem_review'], dict):
            raise CitationRepairError('问题评议必须是结构化对象')
        filter_ids(derived['problem_review'], '/problem_review', 'verified_task_paper', papers)
    return derived, {'version': VERSION, 'policy': 'discard_invalid_citations',
        'original_result_hash': _hash(review), 'derived_result_hash': _hash(derived),
        'discarded': discarded, 'original_reference_count': original_count,
        'valid_reference_count': valid_count, 'scientific_claims_verified': False,
        'scope': '仅废弃无法定位的引用；原观点与票意不变，剩余证据是否充分仍由原科研门禁判断。'}
