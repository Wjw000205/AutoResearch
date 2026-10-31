"""Restore historical definition navigation from a validated adoption chain.

Only structured input/draft directories are followed. Textual references are not
authority to discover arbitrary files, and catalog access gives no reading credit
or approval to superseded candidates.
"""
from __future__ import annotations

import copy
from pathlib import Path

KINDS = {'architecture_definition_issue', 'architecture_scientific_definitions'}


def inherited_documents(engine, run_id, record, existingdocs, *, adopted=None):
    """Return missing catalogs; ``adopted`` must be validate_adoption's result.

    The caller may pass its just-validated adoption to avoid re-running the full
    scientific gate. Otherwise this function performs that validation itself.
    It never changes the caller's catalogs, frozen inputs, votes, or documents.
    """
    from .architecture_definitions import validate_adoption
    from .document_index import directory
    from .engine import ResearchBlocked, digest

    adoption_id = record.get('definition_adoption_id')
    if not adoption_id:
        return []
    adopted = validate_adoption(engine, run_id, adoption_id) if adopted is None else adopted
    cycle = engine.cycle(run_id)
    consensus = engine.store.get('consensus', run_id) or {}
    if (record.get('run_id') != run_id or adopted.get('run_id') != run_id
            or record.get('cycle') != cycle or adopted.get('cycle') != cycle
            or adopted.get('id') != adoption_id or adopted.get('status') != 'approved'
            or record.get('proposal_hash') != adopted.get('proposal_hash')
            or adopted.get('proposal_hash') != digest(consensus.get('proposal'))
            or adopted.get('parent_design_hash') != record.get('original_design_hash')):
        raise ResearchBlocked('继承定义目录与当前任务、周期或批准来源不一致。')

    known = {}
    for doc in existingdocs:
        identifier = doc['document_id']
        if identifier in known and known[identifier]['sha256'] != doc['sha256']:
            raise ResearchBlocked('继承定义目录存在同一文档的版本冲突。')
        known[identifier] = doc

    sources = [('approved_definition_input', doc) for doc in adopted['input_documents']]
    sources += [('approved_definition_issue', draft['document']) for draft in adopted.get('issue_drafts', [])]
    additions = []
    cycle_root = (engine.data_dir / 'runs' / run_id / f'cycle-{cycle}' / 'architecture').resolve()
    for source, doc in sources:
        if doc.get('kind') not in KINDS:
            continue
        identifier, sha = doc['document_id'], doc['sha256']
        if identifier in known and known[identifier]['sha256'] != sha:
            raise ResearchBlocked('继承定义目录存在同一文档的版本冲突。')
        fresh = directory(engine, run_id, identifier, sha)
        if any(doc.get(field) != fresh.get(field) for field in (
                'document_id', 'sha256', 'artifact_id', 'kind', 'sections', 'total_chars')):
            raise ResearchBlocked('继承定义的冻结目录与原始文档结构不一致。')
        artifact = engine.store.get('artifacts', fresh['artifact_id']) or {}
        if (artifact.get('run_id') != run_id
                or not fresh['artifact_id'].startswith(f'{run_id}:cycle-{cycle}/architecture/')
                or not Path(artifact.get('path', '')).resolve().is_relative_to(cycle_root)):
            raise ResearchBlocked('继承定义必须属于当前任务和研究周期。')
        if identifier in known:
            continue
        detached = copy.deepcopy(fresh)
        detached.update(historical_dependency=True, scientific_approval=False, reading_credit=False,
            dependency_provenance={'definition_adoption_id': adoption_id, 'source': source,
                'scope': '历史候选仅供核对已批准定义的继承来源；不能覆盖当前裁决或替代本角色实际阅读。'})
        known[identifier] = detached
        additions.append(detached)
    return additions
