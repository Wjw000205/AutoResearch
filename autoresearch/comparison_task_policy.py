"""Shared task conditions for new design/review assignments; frozen work stays intact."""
from __future__ import annotations

import json
from .experiment_requirements import SEED_GUIDANCE


LITERATURE_GUIDANCE = '''
为后续实验设计提取可追溯的基准设置：实际读取相关论文实验部分、附录/表格和可用官方配置，
把数据集版本/子集、任务类型、输入输出或样本规格、划分、预处理、指标与归约尺度、训练/选优
设置分别保存在现有 datasets、method、results[].setting 和 excerpts 中；没有读到数值结果时
不要为保存设置编造 results 条目。每项重要设置保留论文版本、页/节/表/原文定位，官方代码则
保留仓库、可核验提交和配置路径；用当前来源工具实际读取，再 literature.record_notes 保存。
参数与结果须绑定同一个实验设置；摘要、搜索结果、模型名称、其他论文的设置不能替代来源。
未报告的值和论文/代码冲突写入 missing_information，不凭惯例补值。
复用已有同版本原文锚定笔记，只补读决定当前设计的缺项。
新交接启用 experiment_protocol_tools 时，继续用 literature.record_protocol 将参数值、
reported/not_reported/conflicting 状态和笔记引用索引结构化保存；未报告项不填默认值。
'''


DESIGN_GUIDANCE = '''
公平比较先从相关主流论文实验部分/附录和官方配置确定基准协议，再冻结共同任务条件。
在 architecture/evaluation 中列出“论文及版本/表格或配置定位 → 数据集与任务 → 实验设置
→ 本设计实际参数路径”的来源对照；优先复用已保存原文锚定笔记，缺项定向读取，不靠模型记忆。
每个所选基准列出数据集版本/子集、输入输出规格、任务模式、变量/目标、划分、归一化、指标
及训练/选优设置；共同条件与模型内部参数分别标明来源。未报告项明确未知或标明待批准的
设计选择，不能冒充论文参数。论文和官方代码不一致时列明差异、选择理由和验证办法。
任务规格必须从实际读取的论文对应实验设置逐项提取。
完整基准矩阵须逐项列明计划、来源和待完成范围，不能只跑其中一项却宣称完成全部。
论文原表结果、同条件复现和机制试验分别标识；资源不足的缩小规模方案属于独立 pilot，
不能无说明替代标准基准。已批准方案与主流基准有实质偏差时提出带来源的协议修订，保留
旧版并重新评议，不能在实现阶段悄悄换参数或把机制试验当作公开数据集上的基准成绩。
baseline、proposed 和每个消融必须遵守批准协议声明的共同任务条件：外部输入与目标规格、
评价成员、数据划分、预处理、允许使用的信息及指标归约/还原尺度。
模型内部结构、优化器及已批准损失可以不同，但不能改变评价任务或获得额外信息。
在各 *_params 中用 comparison_task 的 version=1 与 dimensions 数组声明实际共同条件，
每项填写 name、value、paths；维度名称和值取自批准的任务协议，
paths 指向该方法实际使用的参数。
每项 value 是具体冻结 JSON 值，paths 是该方法实际参数的精确 JSON pointer；不同方法可用不同
字段名，但维度名与值必须相同。也声明目标、数据划分、信息权限与评价口径的实际参数路径。
不要指向声明自身或只写说明文字，不能把算法特有学习率/隐藏维度当作共同任务条件。
命名消融必须检查合并后的每个配置；共享搜索不能悄悄覆盖这些条件。不同任务规格在
benchmark_tasks 中分别提供完整三分支参数、搜索网格及验证/测试种子列表，绑定
literature.record_protocol 已保存的 protocol_id/protocol_sha256，并声明 dataset 和 task_dimensions。
每个任务分别选优和报告；根级参数不代替各任务参数。
模型不支持批准规格时，明确不兼容及适配办法并交审查，不得自行改变任务条件。
'''

REVIEW_GUIDANCE = '''
核对基准来源：实际读取所引用论文的实验设置/附录/表格或官方配置及已有原文锚定笔记，
确认数据集版本/子集、任务模式、输入输出规格、划分、预处理、训练/选优与指标对应所引用
的同一组结果。架构需给出来源到实际参数的对照；不能拿论文名称或索引证明参数正确，
也不能把未报告值/自选默认值说成论文规定。正文与官方配置冲突须列出来源与解决办法。
逐项核对任务协议声明的各数据集与任务规格、来源记录及完成范围，
允许有来源的不同标准，但不能把资源缩水 pilot 当作完整标准复现。
独立核对公平比较的共同任务条件：逐一展开 baseline、proposed、全部命名消融和搜索点，
对照批准架构/冻结计划的外部输入、目标规格、预处理、评价成员、
数据划分、信息权限及指标归约/尺度。共同 comparison_task 的每个参数路径须指向真实配置。
分段审查仅核对本组相关条件及决定性来源，复用同版本有效读取与意见；终审汇总全部组以
覆盖完整矩阵，不要求每一小组重读全部论文、架构和配置。旧冻结协议未声明新字段不是缺陷。
同一配置内的别名矛盾、局部覆盖或缺失字段不能猜值；不同任务规格的最佳模型须分别比较。
实现/代码审查须追踪实际的数据处理、输入输出、目标对齐和评分路径，参数回显不能代替核验。
允许批准的内部模型、损失与优化器差异。
若宿主通用提示、实现文档与批准研究阶段规则冲突，指出全部冲突来源、具体修正和验证方法，
不能由实现者自行宣称覆盖已批准科学协议；缺读继续定向补读，不因未知就判定模型不公平。
仅将影响比较、复现或结论的实质问题列为必改，逐项给出位置、具体修改与验收方式；
不因学习率等模型内部参数不同或没有可获得的官方代码直接否决。
'''


DESIGN_GUIDANCE += SEED_GUIDANCE
REVIEW_GUIDANCE += SEED_GUIDANCE


def assignment(engine, run_id, role, purpose, instruction, context, schema, guidance):
    """Add guidance only before freezing; recover exact paid input on resume."""
    from .engine import Harness, ResearchBlocked, digest
    with engine.store._read_db() as db:
        rows = db.execute(
            "SELECT h.key,json_extract(h.data,'$.input_hash'),i.data FROM records h "
            "JOIN records i ON i.collection='handoff_inputs' AND i.key=h.key "
            "WHERE h.collection='handoffs' AND json_extract(h.data,'$.run_id')=? "
            "AND json_extract(h.data,'$.to_role')=? AND json_extract(h.data,'$.purpose')=?",
            (run_id, role, purpose)).fetchall()
    matches = []
    for key, input_hash, raw in rows:
        frozen = json.loads(raw)
        old = frozen.get('context', {})
        if old.get('research_cycle') != context.get('research_cycle'):
            continue
        payload = {name: frozen.get(name) for name in ('instruction', 'context', 'schema')}
        if (frozen.get('id') != key or frozen.get('run_id') != run_id
                or digest(payload) != input_hash
                or Harness.decision_key(run_id, role, purpose, **payload) != key):
            raise ResearchBlocked('公平比较审查的原冻结交接身份无效，未重新派发。')
        for field in ('proposal_hash', 'design_hash', 'revision_hash', 'spec_hash',
                      'document_sha256', 'parent_design_hash', 'review_unit'):
            if old.get(field) != context.get(field):
                raise ResearchBlocked('公平比较审查的原冻结范围已变化，须沿同版本交接恢复。')
        matches.append(tuple(payload[name] for name in ('instruction', 'context', 'schema')))
    if len(matches) > 1:
        raise ResearchBlocked('同一公平比较范围存在多个冻结交接，不能任意选择或重发。')
    if matches:
        return matches[0]
    if purpose == 'design':
        from .experiment_requirements import new_design_schema
        schema = new_design_schema(schema)
    return instruction + '\n' + guidance, {**context, 'comparison_task_review_protocol': 1}, schema
