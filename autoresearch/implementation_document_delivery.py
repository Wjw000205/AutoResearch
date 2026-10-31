"""Exact saved-file identity for document delivery, separate from review approval."""
from __future__ import annotations

import copy
from pathlib import Path
from urllib.parse import quote

KEY = 'implementation_document_delivery'
GUIDANCE = ('\nimplementation_document_delivery 明确本次交付的文档位置和版本。'
    'current_document 是本次编写/验收对象；file_version 是实际文件保存版本，review_round 是审核轮次，二者不能混用。'
    '所有验收 evidence、问题 evidence 和 consistency evidence 非空时都必须包含该当前文档的真实已读范围；'
    '批准架构、计划和历史意见可作对照，但不能替代当前文档证据。'
    '引用严格使用 citation_identity 中的 document_id/document_sha256，并通过 documents.read 读取所需章节；'
    '文件路径、目录和交付清单不代表已读取或已批准。保存修改后必须使用新交付回执中的版本。')


def location(engine, run_id, document, *, revision=None, spec_hash=None):
    from . import document_index
    manifest, _ = document_index.load(engine, run_id, document['document_id'], document['sha256'])
    artifact = engine.store.get('artifacts', manifest['artifact_id'])
    # load() verifies ownership, a task-owned path, bytes and the exact SHA.
    path = Path(artifact['path']).resolve()
    version = artifact.get('output_version')
    return {'document_id': document['document_id'], 'document_sha256': document['sha256'],
        'artifact_id': artifact['id'], 'file_name': path.name, 'absolute_path': str(path),
        'relative_path': artifact['name'],
        'file_version': version if type(version) is int and version > 0 else None,
        'review_round': revision + 1 if type(revision) is int and revision >= 0 else None,
        'spec_hash': spec_hash, 'saved_at': artifact.get('created_at'),
        'download_url': '/api/runs/' + quote(run_id, safe='') + '/documents/'
            + quote(document['document_id'], safe='') + '/file?expected_sha256=' + document['sha256'],
        'citation_identity': {'document_id': document['document_id'], 'document_sha256': document['sha256']},
        'read': copy.deepcopy(document.get('read') or {
            'tool': 'documents.read', 'arguments': {'document_id': document['document_id'],
                'expected_sha256': document['sha256'], 'section_id': manifest['sections'][0]['id'],
                'offset': 0, 'limit': 4000}}),
        'provenance': 'verified_saved_document', 'approval_granted': False}


def prepare(engine, run_id, context):
    """Only called for a new request; never upgrade an already frozen packet."""
    from . import implementation_authoring as author
    from .implementation_acceptance import CONTEXT
    doc, revision, spec_hash = None, None, None
    if author.enabled(context):
        draft = context.get('implementation_document_draft') or {}
        base = context.get('implementation_document_base') or {}
        doc = draft.get('document')
        if doc:
            revision = context['implementation_document_authoring']['revision']
            spec_hash = draft['submission']['spec_hash']
        elif base:
            doc = next((d for d in context.get('indexed_documents', [])
                if d['document_id'] == base['document_id'] and d['sha256'] == base['document_sha256']), None)
            revision, spec_hash = base.get('revision'), base.get('spec_hash')
    else:
        scope = context.get(CONTEXT) or {}
        doc = scope.get('document')
        # Other acceptance phases carry the target explicitly as well.
        target = context.get(KEY, {}).get('current_document', {})
        if not doc and target:
            doc = next((d for d in context.get('indexed_documents', [])
                if d['document_id'] == target['document_id'] and d['sha256'] == target['document_sha256']), None)
        revision = scope.get('document_revision')
        if revision is None and type(target.get('review_round')) is int:
            revision = target['review_round'] - 1
        spec_hash = scope.get('spec_hash', target.get('spec_hash'))
    if not doc:
        return context
    return {**context, KEY: {'version': 1, 'current_document': location(engine, run_id, doc,
        revision=revision, spec_hash=spec_hash), 'reference_rule': GUIDANCE.strip()}}
