"""Carry authenticated source across datasets, without carrying execution approval."""
from __future__ import annotations

import copy

from .experiment_workflow import sha


def freeze(engine, run_id, saved, program):
    from .rapid_experiment_revision import validate_program, COLLECTION
    from .experiment_decisions import _archive
    from .engine import ResearchBlocked
    bundle = validate_program(engine, run_id, program)
    refs = []

    def retain(collection, key):
        row = engine.store.get(collection, key)
        if not row or row.get('run_id') != run_id:
            raise ResearchBlocked('数据集源码交接缺少原交付或修订证据。')
        refs.append({'collection': collection, 'key': key, 'sha256': sha(row)})
        return row

    from .dataset_experiments import COLLECTION as SETUPS
    retain(SETUPS, saved['id'])
    current = program
    seen = set()
    while current.get('rapid_revision_id'):
        key = current['rapid_revision_id']
        if key in seen:
            raise ResearchBlocked('数据集源码交接的修订链重复。')
        seen.add(key)
        revision = retain(COLLECTION, key)
        retain('experiment_decisions', revision['decision_id'])
        retain('experiments', run_id + ':' + revision['check']['id'])
        current = revision['base_program']
    if current != saved['program']:
        raise ResearchBlocked('数据集源码交接的修订不属于当前数据配置。')
    summary = {'kind': 'dataset_source_release', 'source_cycle': engine.cycle(run_id),
        'setup_id': saved['id'], 'program': copy.deepcopy(program), 'bundle': bundle,
        'plan': saved['plan'], 'design': saved['design'],
        'architecture_design': saved['architecture_design'], 'architecture_cycle': saved['architecture_cycle'],
        'records': refs, 'execution_authorized': False,
        'scope': 'Verified source reuse only; the next dataset requires its own configuration, reviews, budget and CPU check.'}
    return _archive(engine, run_id, summary)


def read(engine, run_id, prior):
    from .code_bundle import validate
    ref = prior['source_release']
    row = engine.store.get('experiment_evidence', ref['id']) or {}
    value = row.get('summary', {})
    if (row.get('run_id') != run_id or sha(value) != ref['sha256']
            or value.get('kind') != 'dataset_source_release' or value.get('execution_authorized') is not False
            or value.get('setup_id') != prior['setup_id'] or value.get('source_cycle') != prior['cycle']):
        raise ValueError('数据集源码交接记录发生变化。')
    for item in value['records']:
        original = engine.store.get(item['collection'], item['key'])
        if not original or original.get('run_id') != run_id or sha(original) != item['sha256']:
            raise ValueError('数据集源码交接的原始审阅或检查记录发生变化。')
    validate(value['bundle'], value['program']['code'])
    return copy.deepcopy(value)


def bundle(engine, run_id, original):
    from .dataset_configuration_queue import current
    queue = current(engine.store, run_id)
    if not queue or len(queue['batches']) < 2:
        raise ValueError('数据集源码交接不属于当前队列。')
    prior = queue['batches'][-2]
    if prior.get('source_release') != original.get('source_release'):
        raise ValueError('数据集源码交接引用与当前队列不同。')
    value = read(engine, run_id, prior)
    if value['program'] != original['program']:
        raise ValueError('数据集源码交接的程序版本发生变化。')
    return value['bundle']
