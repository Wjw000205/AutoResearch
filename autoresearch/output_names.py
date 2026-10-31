"""Readable, sequential output filenames with immutable legacy aliases."""
import hashlib
from threading import RLock
from pathlib import PurePosixPath

NAMING_LOCK = RLock()


DOCUMENT_NAMES = {
    'architecture_design': ('architecture', 'architecture'),
    'approved_proposal': ('architecture', 'approved-proposal'),
    'architecture_review': ('architecture', 'architecture-review'),
    'architecture_host_receipt': ('architecture', 'host-controls'),
    'architecture_literature_notes': ('architecture', 'literature-reading-notes'),
    'architecture_author_response': ('architecture', 'author-response'),
    'architecture_scientific_definitions': ('architecture', 'scientific-definitions'),
    'architecture_definition_issue': ('architecture/definitions', 'definition-item'),
    'architecture_definition_feedback': ('architecture', 'definition-feedback'),
    'architecture_protocol_review': ('architecture', 'experiment-protocol'),
    'architecture_experiment_plan': ('architecture', 'experiment-plan'),
    'revised_experiment_protocol': ('architecture', 'experiment-protocol'),
    'original_experiment_plan': ('architecture', 'original-experiment-plan'),
    'validated_literature_note': ('architecture/evidence', 'literature-note'),
    'architecture_reading_recovery': ('architecture', 'reading-recovery'),
    'architecture_continuity_recovery': ('architecture', 'continuity-recovery'),
    'document_working_notes': ('reading-notes', 'reading-notes'),
    'frozen_experiment_plan': ('implementation', 'frozen-experiment-plan'),
    'implementation_specification': ('implementation', 'implementation-specification'),
}


def allocate(engine, run_id, logical_name, content, kind):
    """Return a fresh readable name or a verified existing immutable artifact.

    Old paths stay valid. New files carry their original logical identity and
    content hash in metadata, so exact replay never renames or duplicates them.
    """
    if kind not in DOCUMENT_NAMES:
        return logical_name, None
    direct = engine.store.get('artifacts', f'{run_id}:{logical_name}')
    if direct and direct.get('output_naming_protocol') != 1:
        return logical_name, None
    rows = engine.store.list('artifacts', run_id)
    matched = ([direct] if direct else [row for row in rows if row.get('logical_name') == logical_name
               and row.get('kind') == kind and row.get('output_naming_protocol') == 1])
    if matched:
        if len(matched) != 1:
            raise ValueError('输出文件逻辑身份重复，不能分配新文件名。')
        from .document_index import artifact_text
        existing = matched[0]
        _, text = artifact_text(engine, run_id, existing['id'])
        if text != content or existing.get('sha256') != hashlib.sha256(content.encode()).hexdigest():
            raise ValueError('已保存输出内容发生变化，不能覆盖原文件。')
        return existing['name'], existing
    folder, stem = DOCUMENT_NAMES[kind]
    prefix = f'cycle-{engine.cycle(run_id)}/{folder}/{stem}-v'
    versions = [row.get('output_version', 0) for row in rows
                if row.get('output_naming_protocol') == 1 and row['name'].startswith(prefix)]
    version = max(versions, default=0) + 1
    extension = PurePosixPath(logical_name).suffix
    return prefix + str(version) + extension, {'output_naming_protocol': 1,
        'logical_name': logical_name, 'output_version': version}
