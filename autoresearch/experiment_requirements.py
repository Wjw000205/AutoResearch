"""Requirements for newly authored experiments; never rewrite frozen protocols."""
import copy

SEED_GUIDANCE = '''
实验顺序：实验后先核验架构合理性，必要时调整并重新验证；架构评审通过后才进行参数微调。
当前数据集的架构和参数均经实际结果与三方评审确认稳定后才能切换数据集；
首数据集确认的模型架构冻结供后续数据集复用。覆盖当前实验、校准、报告完成或停止均不等于稳定。
实验要求：默认单种子。每个实验配置在验证、消融及最终测试阶段各使用一个固定模型随机种子；
同阶段 baseline、proposed 与消融使用相同种子以保持成对比较，数据划分种子另行固定。
只有用户在当前任务要求中明确要求多种子重复时，才制定多种子计划；在实验说明中引用该任务
要求的原文和位置，并列明要求的种子或重复次数。论文惯例、一般科研规范、agent 建议、
预算剩余或预算不限次数均不能代替用户的明确要求，也不能自动增加种子。
新计划显式填写 validation_seeds 和 final_test_seeds；默认各一个元素，不靠剩余实验预算推导重复次数。
参数搜索改变已批准的超参数，不通过换种子凑搜索次数。单种子结果如实注明范围，不声称跨种子稳定。
三方评审必须核对多种子是否有当前任务的明确依据；没有依据的新计划改回单种子后再批准。
此规则仅用于后续新实验计划。已有冻结计划、已派发任务、已完成实验及原始回执保持原样，
不能借此删除历史种子、缩减已批准覆盖范围或重跑已完成训练。
'''


def new_design_schema(schema):
    """New non-matrix designs must freeze both seed lists, so spare capacity
    cannot trigger the legacy compiler's implicit final-test replication.
    Matrix task schemas already require both lists. Multi-seed justification
    remains part of the independent task-requirement review.
    """
    result = copy.deepcopy(schema)
    result.setdefault('allOf', []).append({
        'if': {'not': {'required': ['benchmark_tasks']}},
        'then': {'required': ['validation_seeds', 'final_test_seeds']}})
    for field in ('validation_seeds', 'final_test_seeds'):
        if field in result.get('properties', {}):
            result['properties'][field]['description'] = (
                '默认一个固定模型种子；仅当前用户任务明确要求多种子时提供多个，'
                '并在实验说明中引用任务要求。不得按剩余预算扩展。')
    return result
