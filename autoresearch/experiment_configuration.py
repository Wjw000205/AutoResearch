"""One YAML input for supported experiment settings; admission stays separate."""
from __future__ import annotations

import copy

SCOPE = 'experiment'
HOST_FIELDS = frozenset({'variant', 'phase', 'seed', 'validation_seeds', 'final_test_seeds',
    'comparison_task', 'comparison_inputs', 'comparison_transport', 'benchmark_task_id',
    'benchmark_source', 'evaluation', 'acceptance', 'runtime', 'algorithm', 'loss_contract',
    'evaluation_contract_version', 'implementation_contract', 'ablation_configuration',
    'configurations', 'common_ref', 'baseline_ref', 'proposed_ref', 'fredn_ref',
    'resources', 'resource_plan', 'budget', 'budgets', 'max_trials', 'allow_local_execution',
    'execution_enabled', 'local_training', 'execution_authorized', 'scientific_approval'})
REQUIRED_SETTINGS = ('dataset', 'batch_size', 'depth', 'width', 'learning_rate', 'weight_decay')

DATA_READING_GUIDANCE = (
    '工程师依据当前任务、实际资产清单和格式说明，自行判断样本如何组织，制定读取规则。'
    '用足以确认格式的最少元数据说明样本单位、模型输入、目标或标签的来源、需忽略的字段，以及如何沿用批准的划分。'
    '有表头的表格查看表头并按配置选列；只有确有日期字段且任务需要其值时才解析。'
    '图像数据按实际目录或标注清单配对图像与标签；其他格式按各自的结构读取。'
    '资产不足以确定字段含义时，定向查格式说明或获准的小样本。不要预设首列、日期、标签位置或时间窗口，也无需遍历全量数据来决定格式。'
    '同时按任务协议或预训练模型的输入约定说明预处理：哪些特征或目标需要变换、沿哪个维度、参数从哪里取得、指标在哪个尺度报告。'
    '从数据估计的统计量只在训练划分拟合，验证和测试复用；采用给定常数或原始输入时说明依据。'
    '核对已发表方法时，分别交代数据加载器的变换、模型内部的归一化及其还原位置，按论文对应的划分和指标解释结果。')

DESIGN_GUIDANCE = '''
实验设计阶段必须定义统一 YAML 配置入口，并把它写进 architecture/evaluation、实现接口和验收要求。
同一 YAML 配置数据来源、当前任务的输入输出规格与读取设置，以及 batch_size、网络层数、网络宽度、学习率、weight_decay 和其余已支持超参数。
时序预测按协议配置输入长度、预测长度、窗口步长；其他任务配置实际使用的维度。
区分论文对比必须统一的关键条件与模型内部训练参数；它们可在同一 YAML 中分区，不能混成论文报告值。
交付版本化默认 YAML、JSON Schema、真实加载器和 --config <yaml路径> 入口；数据加载器、窗口构造、
模型及优化器必须实际消费 YAML 值。禁止写死数据集、输入输出长度，或只改 YAML 而代码仍用常量。
在已支持的数据格式和模型能力内，更换数据集、长度/步长或超参数只修改 YAML，不派发源码修改。
新增数据格式/适配器、模型组件或源码能力才走 Codex 实现与独立代码审查。
后续调参使用 hyperparameters.read/revise 保存 YAML 版本与 SHA；数据集和任务尺寸变更也用 YAML 表达，
但按论文协议及数据集/任务变更流程独立评议，不把它混入同一任务的超参数搜索。
YAML 是统一配置输入；修改文件仍需通过原有实验、预算、资源及科学门禁，不自动授权执行。
设计必须要求 CPU 行为检查：换两个同格式的微型输入后实际读取对象改变；变更该任务支持的输入规格后验证相应行为；
改 batch_size 后实际批大小改变；改层数/宽度后真实网络结构改变；学习率/权重衰减进入优化器并影响 toy 更新。
类型、范围、未知/重复键、非法路径、缺失配置和配置哈希错配应明确拒绝，不静默回退硬编码。
''' + DATA_READING_GUIDANCE

TUNING_GUIDANCE = '''
已支持的模型/训练参数只修订统一 YAML，使用 hyperparameters.read/revise 并绑定版本/SHA，不重新派发 Codex。
数据集及任务实际支持的输入输出规格也由同一 YAML 输入；变更时保留旧版本，先走相应协议/数据集任务评议，
再由宿主把获批配置落实为运行 YAML。只有新增源码能力或科学架构改变才修改 Python。
'''

IMPLEMENTATION_GUIDANCE = '''
本交付使用完整实验 YAML：configs/hyperparameters.contract.json 必须声明 configuration_scope:experiment。
contract 的 configuration_paths 为 dataset、batch_size、depth、width、learning_rate、weight_decay 和任务专用设置声明实际 JSON pointer。
时序窗口映射实际使用的 input_length、output_length 和 stride；图像等任务映射其自身读取配置。每个映射须存在于可执行 profile。
保持 version:1, profiles:{baseline,proposed,ablation} 及既有运行文件名；每个可执行 profile 同时包含本方法的
数据/窗口设置及模型/训练超参数。配置可以按 data/model/training 分区，但必须映射到实际运行参数。
YAML profile 与 params.json 运行配置使用同名、同层级路径；分区应在设计参数中一并定义，不能另造不受宿主支持的映射。
提供 --config <yaml路径> 命令行入口；实际读取宿主为当前任务传入的 YAML，params.json 是获批镜像。
实现工程师为这份数据制定的读取规则，并用该格式的微型输入核对实际加载结果。
variant/phase/seed、comparison_task、论文来源绑定及审批/预算仍由宿主控制，不能借 YAML 切换算法或执行权限。
YAML 改数据集或尺寸在源码上不应要求修改，但正式执行须匹配获批的任务与数据清单。
在完整配置模式下 effective_profile_sha256 覆盖所选 profile 的数据、窗口、模型及训练设置。
'''


def scope(program):
    return program.get('hyperparameter_contract', {}).get('contract', {}).get(
        'configuration_scope', 'hyperparameters')


def inputs(params):
    """Read-only projection of executable configuration, excluding host authority."""
    return {key: copy.deepcopy(value) for key, value in params.items()
            if key not in HOST_FIELDS and not key.startswith('_')}


def instruction(base, configuration_scope):
    if configuration_scope != SCOPE:
        return base
    return base.replace(
        '科学任务、数据来源/划分、种子、评价指标及预算留在宿主控制，不由 YAML 修改。',
        '数据集及窗口设置也从 YAML 读取，并与宿主批准任务严格核对；种子、评价规则和预算仍由宿主控制。'
    ) + '\n' + DESIGN_GUIDANCE + IMPLEMENTATION_GUIDANCE
