"""Versioned host implementation facts for specification authors and reviewers.

These are implementation contracts, not fabricated experiment results or an
approval. The immutable document and source fingerprint let a reviewer decide
whether the requested transport is supported by the host source implementation.
"""
from __future__ import annotations

import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path


_SOURCES = ('experiment_protocols.py', 'benchmark_matrix.py', 'dataset_catalog.py', 'datasets.py', 'comparison_transport.py', 'lab.py', 'engine.py', 'experiment_plan.py',
            'evaluation.py', 'codex_delivery.py', 'implementation_capabilities.py', 'split_identity.py',
            'research_cycle.py', 'historical_closure.py')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode('utf-8')).hexdigest()


@lru_cache(maxsize=32)
def _source_hash(path, size, modified_ns):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sources():
    root = Path(__file__).parent
    result = {}
    for name in _SOURCES:
        path = root / name
        stat = path.stat()
        result['autoresearch/' + name] = _source_hash(str(path), stat.st_size, stat.st_mtime_ns)
    return result


def planning_evidence(engine, run_id, common):
    from .comparison_transport import capability_description

    plan = common.get('experiment_plan') or {}
    description = capability_description(plan=plan)
    criteria = copy.deepcopy((plan.get('acceptance') or {}).get('criteria', []))
    facts = {'version': 1, 'implementation_sources': _sources(),
             'experiment_plan_hash': _digest(plan), 'comparison_transport': description,
             'frozen_acceptance_inventory': {
                 'source': 'experiment_plan.acceptance.criteria',
                 'criteria': criteria,
                 'metric_names': [criterion['metric'] for criterion in criteria],
                 'scope': '冻结计划原有验收项与精确指标名；用于声明阶段选择，不推断阶段、不代表已读取来源或验收通过。'}}
    from .split_identity import capability
    facts['split_identity'] = capability()
    facts['benchmark_matrix'] = {'version': 1, 'plan_version': 3,
        'design_field': 'benchmark_tasks', 'plan_field': 'task_plans',
        'source_tools': ['literature.record_protocol', 'literature.protocols'],
        'runtime_identity': ['benchmark_task_id', 'benchmark_source'],
        'source_policy': 'Every dataset/input/output and declared common dimension binds an actually read paper protocol; no default lengths.',
        'selection': 'Independent complete validation grids and seed sets; one frozen baseline/proposed winner per task.',
        'testing': 'Only each task winner and its explicit final_test_seeds after the unchanged all-task consensus/resource gates.',
        'aggregation': 'Per-task criteria and descriptive results; all tasks required, no cross-task pooled winner.',
        'comparison_transport_v1_supported': False}
    facts['public_datasets'] = {
        'version': 1, 'binding_parameter': 'dataset_assets',
        'binding_fields': ['asset_id', 'manifest_sha256'],
        'discovery_tool': 'datasets.discover', 'download_tool': 'datasets.download',
        'inspection_tool': 'datasets.inspect',
        'input_path': 'Use each manifest files[].trial_path relative to the trial working directory.',
        'integrity': 'Manifest and retained files are SHA256-checked before copying to each isolated trial.',
        'comparison': 'All compared configurations must freeze the same ordered dataset_assets bindings.',
        'limits': 'Staging counts against trial disk and wall time; download does not authorize execution.',
        'scope': 'Source-linked public files and selected ZIP members; no authentication, scripts or automatic benchmark selection.'}
    facts['split_identity']['host_enabled'] = (engine.store.get('runs', run_id)['config'].get('evaluation_contract_version', 0) >= 2
                                               if engine is not None else None)
    return {**facts, 'capability_fingerprint': _digest(facts), 'run_id': run_id,
        'provenance': 'host_source_implementation_contract',
        'source_binding': '当前源代码文件指纹；修改后须重启后端以加载对应实现。',
        'scope': '宿主实现合同与代码文件指纹；须由独立评审对照请求合同。不是实验结果或审批回执。',
        'scientific_approval': False, 'execution_authorized': False,
        'experiment_executed': False}


def review_evidence(context, row):
    """Bind fresh host facts to a review without mutating a frozen assignment."""
    evidence = copy.deepcopy(context.get('implementation_capability_evidence') or {})
    if not evidence:
        return evidence
    evidence['review_binding'] = {
        'spec_hash': row['spec_hash'], 'document_sha256': row['document']['sha256'],
        'experiment_plan_hash': context.get('experiment_plan_hash')}
    evidence['requested_contract'] = copy.deepcopy(row['spec'].get('comparison_transport'))
    evidence['contract_declared'] = evidence['requested_contract'] is not None
    evidence['acceptance_phases_declared'] = (
        isinstance(evidence['requested_contract'], dict)
        and 'acceptance_phases' in evidence['requested_contract'])
    evidence['review_policy'] = (
        '仅comparison_transport结构字段经同版本三方审批后才可启用运输；'
        '自然语言提及、能力文档和能力指纹均不构成启用或批准。'
        '未声明该结构字段的旧规范保持原执行合同；如确需运输，应做明确合同修订。'
        '宿主支持由作者在comparison_transport.acceptance_phases显式声明有界机器阶段选择；'
        '逐项使用冻结指标名和phases数组，完整覆盖每个冻结指标且全部保留validation；'
        '至少一项及主指标仍须适用于test。'
        '必须对照frozen_acceptance_inventory完整保留验证验收，以实际读取的批准来源核对测试适用范围，'
        '并完成该规范版本的独立审批。未声明acceptance_phases时保持原有全阶段验收，不能自动推断豁免。'
        '不能删除验证门禁、用验证结果代替测试结果、放宽阈值或增加冻结计划以外的测试调用。'
        '宿主能力变化可触发同版本复核，不可追改旧票或自动批准。')
    return evidence


def document_text(evidence):
    return '\n'.join([
        '# 宿主比较数据传递能力', '',
        '本文件由当前宿主实现生成，保留实际代码指纹和冻结计划绑定。',
        '无模型调用、拟合或科研验证；下列描述不能替代同版本独立审计和执行授权。', '',
        '## 阶段适用与冻结验收清单', '',
        '宿主支持在 comparison_transport.acceptance_phases 中显式声明验证与测试的验收范围。'
        '字段结构以以下 comparison_transport.schema 为准；frozen_acceptance_inventory 保留冻结计划原指标名和完整验收项。'
        'acceptance_phases 的每项填写 metric 与 phases，所有冻结指标恰好各列一次；'
        '全部保留 validation，至少一项及主指标保留 test。'
        '这份清单只提供定位及机器字段，不代表原文已经读取、阶段范围已经批准或指标已经测量。', '',
        '作者须依据实际读取的批准来源声明范围，并由同一规范版本的三方独立审查。'
        '验证阶段必须保留全部冻结验收；测试范围不得以指标改名、缺失结果或自然语言推断。'
        '旧规范没有 acceptance_phases 时仍按原有全阶段验收，不能自动获得阶段豁免。'
        '禁止用验证结果代替测试结果、降低原阈值或增加批准计划外的测试调用。', '',
        '## 完整机器合同与来源指纹', '',
        '```json', json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2,
                            allow_nan=False), '```', '',
        '仅在实现规范中明确声明并审批 comparison_transport 后启用。'
        '作者须按冻结计划填写所需消费者和完整数组合同；不能改变计划调用数量或科学参数。',
    ])
