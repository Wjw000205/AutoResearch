"""Publish the two document revisions from an independently approved mechanism change."""
from . import document_index
from .experiment_workflow import sha


def render(decision, parents, decision_id):
    review = decision['architecture_review']
    chosen = next(item for item in review['candidates'] if item['id'] == review['selected_id'])
    def header(title, parent):
        return [f'# {title}', '', f'基于 {parent}。变更范围内以下列新版定义为准，其余条款沿用该版本。',
                f'依据已审核修改单 `{decision_id}`。本版本的实验收益待验证。', '']
    architecture = header('架构设计修订', parents['architecture'])
    for title, body in [('受影响模块', '、'.join(chosen['module_paths'])),
                        ('先前机制', chosen['current_mechanism']), ('现行机制', chosen['proposed_mechanism']),
                        ('选择依据', review['selection_rationale']), ('替代解释', chosen['competing_explanation']),
                        ('验证预期', chosen['validation_prediction']), ('反证条件', chosen['falsification']),
                        ('回退条件', review['rollback_condition'])]:
        architecture += ['## ' + title, '', body, '']
    implementation = header('代码实现文档修订', parents['implementation'])
    for change in decision['changes']:
        implementation += ['## ' + change['target'], '', '原实现', '', change['before'], '',
                           '本版实现', '', change['after'], '', change['reason'], '']
    implementation += ['## 接口与初始化', '', chosen['interface_and_initialization'], '',
                       '## 容量与成本', '', chosen['capacity_and_cost'], '',
                       '## 复验', '', decision['verification'], '',
                       '对照运行 ' + '、'.join(review['control_trial_ids']), '']
    return {'architecture': '\n'.join(architecture), 'implementation': '\n'.join(implementation)}


def publish(engine, harness, run_id, record):
    from .engine import digest
    base = record['base_program']
    previous = base.get('revision_documents')
    if previous:
        parents = dict(previous)
    else:
        design = engine.outputs(run_id)['design']
        design_hash = digest(design)
        architecture = engine.store.get('architecture_reviews', f"{run_id}:architecture:{record['cycle']}:{design_hash}") or {}
        parents = {}
        if architecture.get('document'):
            parents['architecture'] = architecture['document']
        if base.get('implementation_document_id'):
            parents['implementation'] = {'document_id': base['implementation_document_id'],
                                         'sha256': base['implementation_document_sha256']}
    # Older reviewed programs can have a typed design and a code bundle without indexed Markdown.
    references = {kind: f"{doc['document_id']}（{doc['sha256']}）" for kind, doc in parents.items()}
    for doc in parents.values():
        document_index.load(engine, run_id, doc['document_id'], doc['sha256'])
    if 'architecture' not in references:
        references['architecture'] = f'已审设计 {design_hash}'
    if 'implementation' not in references:
        references['implementation'] = f"已审代码包 {record['binding']['bundle_sha256']}"
    versions = {}
    root = f"cycle-{record['cycle']}/architecture-amendments/{sha(record['id'])}"
    for kind, text in render(record['decision'], references, record['decision_id']).items():
        name = f'{root}/{kind}.md'
        artifact = engine.store.get('artifacts', run_id + ':' + name)
        if artifact:
            if document_index.artifact_text(engine, run_id, artifact['id'])[1] != text:
                raise ValueError('Saved architecture amendment differs from the approved decision')
        else:
            artifact = engine.artifact(run_id, name, text, kind + '_amendment')
        doc = document_index.index(engine, harness.intelligence, run_id, artifact['id'], kind=kind + '_amendment')
        versions[kind] = {field: doc[field] for field in ('document_id', 'artifact_id', 'sha256')}
    return versions


def material(engine, run_id, versions):
    return {kind: {**ref, 'text': document_index.load(engine, run_id, ref['document_id'], ref['sha256'])[1]}
            for kind, ref in versions.items()}
