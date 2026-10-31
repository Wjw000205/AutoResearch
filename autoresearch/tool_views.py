"""Read-only views of registered research tools and observed invocations."""
from __future__ import annotations

import json

from .config import get_settings
from .engine import Harness, ROLES, TOOL_HELP, ROLE_TOOLS, SOURCE_ROLE_TOOLS, NOTES_ROLE_TOOLS, PDF_ROLE_TOOLS, LITERATURE_ROLE_TOOLS


def _availability(role, name):
    tiers = [(ROLE_TOOLS, None, '角色基础工具；仍受当前任务范围、资源及预算门禁约束。'),
             (SOURCE_ROLE_TOOLS, 'source_tool_protocol=1', '来源工具协议已启用。'),
             (NOTES_ROLE_TOOLS, 'literature_notes_protocol=1', '论文阅读笔记协议已启用。'),
             (PDF_ROLE_TOOLS, 'pdf_tools_protocol=1', '本地 PDF 工具协议已启用。'),
             (LITERATURE_ROLE_TOOLS, 'codex_literature_protocol=1', '论文目录、页面图像及引用扩展协议已启用。')]
    for index, (mapping, protocol, condition) in enumerate(tiers):
        if name in mapping[role]:
            inherited = [entry[1] for entry in tiers[index:] if entry[1] is not None]
            return {'role': role, 'protocol': ' or '.join(inherited) if protocol is not None else None,
                    'condition': condition + (' 更高层的笔记、PDF 或论文目录协议也继承此能力。' if index < 4 and protocol else '')}
    conditions = {
        'experiments.status': ('experiment_workflow_protocol=1', '分页核对当前周期全部冻结验证任务及剩余运行次数；台账完整不代表科学验收通过。'),
        'experiments.read': ('experiment_workflow_protocol=1', '按当前任务、周期及投影哈希读取单个验证实验；诊断尾部有长度上限，留出集结果不可读。'),
        'experiments.compare': ('experiment_workflow_protocol=1; validation_evidence', '按冻结的统计证据 ID、SHA256 和 JSON 路径分页复核验证结果，不执行实验或更换选参。'),
        'experiments.record_notes': ('experiment_workflow_protocol=1', '仅保存本角色本交接真实验证读取回执支持的简短观察；笔记不代表独立验证或批准。'),
        'experiments.analyze': ('experiment_decision; validation_evidence', '对冻结验证证据做分指标/种子对比、消融差异、训练趋势和完整性分析；输出可回读报告，区别观测与因果假设。'),
        'experiments.inspect_module': ('experiment_decision; validation_evidence', '定位并分页读取实际执行版本的模块、函数、调用关系和配置；静态位置不能证明根因。'),
        'literature.protocols': ('experiment_protocol_tools=1', '按原文、笔记及阅读回执重新校验结构化实验参数，目录不授予执行权限。'),
        'literature.record_protocol': ('experiment_protocol_tools=1; scout', '结构化保存参数值、缺失或冲突状态及已读笔记原文来源；不可变记录不等于科学审批。'),
        'python.run': ('training_context_protocol=1; engineer; experiment_train:*',
            '执行当前冻结的验证训练任务；宿主加载已批准代码与参数，强制检查共识、代码版本、硬件、预算及取消状态。'),
        'datasets.discover': ('dataset_tools_protocol=1', '从已读且仍可校验的论文笔记列出数据集与来源导航；不推断可下载性，不启动实验。'),
        'datasets.inspect': ('dataset_tools_protocol=1', '复核本任务数据集清单、文件哈希及实验相对路径；不代表科学协议已通过。'),
        'datasets.download': ('dataset_tools_protocol=1; scout or engineer', '按已读论文的数据集名从作者/基准维护者公开仓库联网下载，或沿实际来源链下载；限制网络/解压/磁盘用量，不启动训练。'),
        'code.locate': ('code_review or code_repair_tool_review=1', '只定位当前冻结交付的文件、函数及文字位置；不注入源码，不授予读取或修改权限。'),
        'code.read': ('code_review or code_repair_tool_review=1', '按文件哈希及字符范围读取相关代码；原文消费后退出上下文，精确回执保留并可回查。'),
        'code.record_notes': ('code_review or code_repair_tool_review=1', '保存绑定真实读取回执的简短观察和未决问题；笔记不代表实现文档、代码或实验已获批准。'),
        'documents.recall': ('code_review or code_repair_tool_review=1', '检索本角色在冻结范围内已读过的同版本文档及条款笔记；版本变化或记忆缺失时才补读原文。'),
        'artifacts.list': ('delivery_directory_protocol=1 and implementation_alignment_protocol=1; code_review', '仅导航当前冻结代码评审交接的交付目录；按页返回文件标识、路径和哈希，不代表已读文件或验收通过。'),
        'architecture.draw_diagrams': ('architecture_diagram_authoring_protocol=2', '仅 engineer 当前同周期、同 SHA 的已批准架构绘图交接可用；保存图示不代表科学审批或实现验收。'),
        'evidence.catalog': ('source_tool_protocol=1 and (context_reference_protocol=1 or literature_protocol=2)', '任务已启用来源工具及证据目录引用协议。'),
        'literature.rag_search': ('paper_rag_protocol=1', '任务已启用共享论文 RAG 协议。'),
        'literature.rag_read': ('paper_rag_protocol=1', '任务已启用共享论文 RAG 协议。'),
        'documents.list': ('document_index_protocol=1', '仅导航本任务文档；索引评审进一步限制文档版本与范围。'),
        'documents.search': ('document_index_protocol=1 or saved implementation continuation or architecture_diagram_authoring_protocol in (1,2)', '仅搜索本任务允许的文档版本；保存续接及图表编写另有范围限制。'),
        'documents.read': ('document_index_protocol=1 or saved implementation continuation or architecture_diagram_authoring_protocol in (1,2)', '仅读取本任务允许的精确版本与章节；索引评审保留原有阅读门禁。'),
        'context.expand': ('context_compiler_version in (2,3)', '工作上下文启用且非索引文档评审；仅展开本角色已归档的回执。'),
        'implementation.write_document': ('implementation_document_authoring_protocol=1; implementation_spec:*', '仅 engineer 的实现文档编写任务；保存续接可能只允许此工具。保存后仍须独立审计。'),
        'implementation.edit_document': ('implementation_document_authoring_protocol=1; implementation_spec:*; exact base required', '仅当前 engineer 按文档 ID、SHA256 和 spec_hash 修改基线；未改字段保留，生成下一不可变版本，仍须三方独立审计。'),
        'implementation.locate_document': ('implementation_document_locate_protocol=1; implementation_spec:*; exact base required', '按验收 ID、关键词或路径定位当前实现文档的原值；返回定向修改路径，不产生科学阅读或审批回执。'),
        'implementation.record_self_check': ('implementation_acceptance_author_protocol=1; acceptance_contract; implementation_spec:*; saved document required', '仅 engineer 针对当前已保存文档及冻结验收条件登记作者自检；须绑定文档 ID、SHA256、spec_hash 和实际阅读证据，不代表独立审批或问题关闭。'),
    }
    # Display metadata must not turn a newly registered capability into a 500.
    # Missing annotations grant no authority; execution still uses the harness.
    protocol, condition = conditions.get(name, (None,
        '调用条件说明尚未登记；实际可用性由当前任务的冻结工具合同与运行时检查确定，目录条目不授予调用权限。'))
    return {'role': role, 'protocol': protocol, 'condition': condition}


def tool_catalog(store, run_id=None):
    """Expose the execution registry, not a new authorization or model request."""
    from .tool_contracts import VERSION, argument_schema
    settings = get_settings(store)
    providers = {row['id']: row for row in settings.get('providers', [])}
    role_tools = {role: Harness.executable_tools(role) for role in ROLES}
    tools = []
    for name in sorted({name for names in role_tools.values() for name in names}):
        help_text = TOOL_HELP[name]
        contract, separator, _ = help_text.partition('} ')
        tools.append({'name': name, 'description': help_text,
                      'arguments_contract': contract + '}' if separator else help_text,
                      'arguments_schema': argument_schema(name), 'tool_contract_version': VERSION,
                      'roles': [role for role in ROLES if name in role_tools[role]],
                      'category': name.split('.', 1)[0],
                      'availability': [_availability(role, name) for role in ROLES if name in role_tools[role]],
                      'execution_scope': 'assignment_contract_and_runtime_gates'})
    agents = []
    for role in ROLES:
        provider = providers.get(settings.get('roles', {}).get(role), {})
        agents.append({'role': role, 'provider': provider.get('id'), 'model': provider.get('model'),
                       'tools': role_tools[role]})
    return {'schema_version': 1, 'source': 'runtime_harness', 'run_id': run_id,
            'agents': agents, 'tools': tools, 'total': len(tools),
            'scope': 'capability_inventory',
            'notice': '可用能力目录；具体任务还会按冻结上下文、只读评审、恢复及 final-only 模式缩小工具范围。目录读取不会调用模型或执行研究工具。'}


def _bounded(value, budget=7000):
    """Bound page JSON while retaining structure and explicit truncation markers."""
    remaining, truncated = budget, False

    def visit(item, depth=0):
        nonlocal remaining, truncated
        if remaining <= 0 or depth > 5:
            truncated = True
            return '[truncated]'
        if isinstance(item, str):
            cap = min(1200, remaining)
            remaining -= min(len(item), cap)
            if len(item) > cap:
                truncated = True
                return item[:cap] + '… [truncated]'
            return item
        if isinstance(item, dict):
            keys = list(item)[:20]
            result = {}
            for key in keys:
                display_key = str(key)[:120]
                truncated = truncated or len(str(key)) > 120
                remaining -= len(display_key) + 6
                result[display_key] = visit(item[key], depth + 1)
            if len(item) > len(keys):
                result['_truncated_fields'] = len(item) - len(keys)
                truncated = True
            return result
        if isinstance(item, list):
            result = [visit(child, depth + 1) for child in item[:8]]
            if len(item) > len(result):
                result.append({'_truncated_items': len(item) - len(result)})
                truncated = True
            return result
        remaining -= len(str(item)) + 2
        return item

    return visit(value), truncated


def _summary(value):
    if value is None:
        return ''
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return text[:320] + ('…' if len(text) > 320 else '')


def _tool_call_view(row, run, *, clean, preview):
    started, finished = row['started'] or {}, row['finished'] or {}
    start_data, finish_data = started.get('data', {}), finished.get('data', {})
    data = {**start_data, **finish_data}
    details = clean({'arguments': start_data.get('arguments'),
                     'result': finish_data.get('result'), 'error': finish_data.get('error')})
    details, truncated = _bounded(details) if preview else (details, False)
    result_provenance = (finish_data.get('result') or {}).get('provenance') if isinstance(finish_data.get('result'), dict) else None
    has_call_id = row['call_key'].startswith('call:')
    correlated = has_call_id and row['started_id'] is not None and row['finished_id'] is not None
    return {'id': row['call_key'], 'tool_name': row['message'], 'role': row['role'], 'status': row['status'],
            'started_at': started.get('created_at'), 'finished_at': finished.get('created_at'),
            'handoff_id': data.get('handoff_id'), **details,
            'arguments_summary': _summary(details['arguments']), 'result_summary': _summary(details['result']),
            'truncated': truncated,
            'provenance': {'mode': data.get('mode', run.get('mode', 'unknown')),
                           'source': 'correlated_events' if correlated else 'runtime_harness' if has_call_id else 'legacy_event',
                           'result': _summary(clean(result_provenance)) or None},
            'evidence': {'started_event_id': row['started_id'], 'finished_event_id': row['finished_id'],
                         'correlated': correlated},
            'interrupted': finish_data.get('interrupted') is True,
            'error_type': finish_data.get('error_type')}


def tool_call_detail_view(store, run, call_key, *, clean):
    """Return the complete saved payload after redaction, without preview limits."""
    row = store.tool_call_detail(run['id'], call_key)
    if row is None:
        return None
    return {'schema_version': 1, 'run_id': run['id'],
            **_tool_call_view(row, run, clean=clean, preview=False)}


def tool_calls_view(store, run, *, clean, limit=30, offset=0, role=None, tool=None, status=None):
    page = store.tool_call_page(run['id'], limit=limit, offset=offset, role=role, tool=tool, status=status)
    items = [_tool_call_view(row, run, clean=clean, preview=True) for row in page['rows']]
    has_more = offset + len(items) < page['total']
    return {'schema_version': 1, 'run_id': run['id'], 'items': items, 'total': page['total'],
            'limit': limit, 'offset': offset, 'has_more': has_more,
            'next_offset': offset + len(items) if has_more else None,
            'filters': {'role': role, 'tool': tool, 'status': status}}
