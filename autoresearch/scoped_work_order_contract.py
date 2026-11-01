"""Separate migration equivalence from an approved structural intervention."""
from .yaml_migration import INSTRUCTION as MIGRATION

RUNTIME = '''保留动态 YAML 作为超参数入口、默认 YAML、JSON Schema 及宿主 runtime/receipt 契约。
模型和优化器实际消费 YAML，验证 SHA、profile、参数镜像、必填值与范围；缺失配置不得回退硬编码。
不改宿主控制的数据、划分、种子、指标、资源或科学验收。已支持的 YAML 值调整不在本源码工作项中搜索。
按修改单更新受影响参数的 schema 和行为断言，保留深度、宽度、weight_decay 的真实 CPU 检查。'''
STRUCTURAL = '''这是已独立批准的单项架构实验实现。读取 decision.architecture_review 中被选候选及其反证。
允许替换目标模块或增加必要运行文件；仅修改获批机制及其接口适配，不顺带改变训练目标或其他模块。
_control 是只读原架构对照。目标模块允许输出、张量结构、初始化和检查点格式发生已声明的必要变化；
不得对该模块套用“新旧默认行为完全等价”。未改部分仍需行为、配置与随机流公平性检查。
CPU 断言验证新机制实际进入 forward/梯度路径、形状/因果边界、配置生效、容量/成本及接口契约；
记录必要的不兼容项与处理方式，不能通过跳过检查或修改控制文件取得通过。
CPU 通过仅证明交付行为，不能证明预测改善。保持原科学目标与回退条件，随后仍需独立评审和成对验证。
不执行正式训练、不访问留出集、不重跑历史实验、不安装依赖。'''
ENGINEERING = '''这是获批的工程维修，保留模型科学机制和默认行为。只落实修改单，
用真实 CPU 行为断言复现故障并验证修复；不把日志、接口或测试维修当成架构收益。
不执行正式训练、不访问留出集、不重跑历史实验。'''


def contract(record):
    if record.get('yaml_migration'):
        return 'yaml_migration', MIGRATION
    if record['decision'].get('architecture_review') and record['decision']['action'] == 'revise_architecture':
        return 'architecture_experiment', RUNTIME + '\n' + STRUCTURAL
    return 'engineering_repair', RUNTIME + '\n' + ENGINEERING
