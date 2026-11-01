/** Presentation of saved controller assignments; never an execution decision. */
const tasks: Record<string, { name: string; summary: string; expected: string; roles?: Record<string, string> }> = {
  experiment_diagnose: {
    name: '实验诊断',
    summary: '分析已有实验与训练日志，区分实际观测、原因假设和缺失证据，提出下一步建议。',
    roles: {
      director: '核对研究假设与已有实验结果，提出最小下一步及停止条件。',
      engineer: '核查训练日志、相关源码与优化过程，定位异常并提出可验证的原因假设。',
      scout: '核查反证、比较公平性与数据泄漏风险，指出证据缺口和验证办法。',
    },
    expected: '观测、原因假设、下一步建议及证据缺口',
  },
  experiment_decide: {
    name: '制定下一批实验方案',
    summary: '综合三方诊断，提出唯一下一步，明确修改或参数候选、验证办法及停止条件，提交独立评审。',
    expected: '绑定当前证据的单批次提案',
  },
  experiment_vote: {
    name: '评审下一批实验方案',
    summary: '独立核对本批次的必要性、证据、预算与停止条件，给出同意或具体阻塞问题。',
    expected: '针对同一提案的独立投票与理由',
  },
  rapid_implement: {
    name: '局部代码补修',
    summary: '按已批准修改项和已读取源码编写局部候选补丁与检查断言，保留已有实验。',
    expected: '候选代码差异、检查断言与修改说明',
  },
  rapid_review: {
    name: '局部补修评审',
    summary: '独立核对候选代码差异、原始源码和检查断言，确认是否覆盖批准项及遵守修改边界。',
    expected: '补丁审核意见、证据引用与未解决问题',
  },
};

export function experimentTaskDescription(purpose: string, role?: string) {
  if (/^experiment_diagnose:[a-f0-9]{64}:artifact-review-1:[a-f0-9]{64}$/.test(purpose)) return {
    name: '独立复核运行产物',
    summary: '复用工程师已读取的产物快照，由三方逐项核对公共初始化、容量及检查点关联，记录一致项、冲突与真实缺口。',
    expected: '绑定原产物的独立核验结论；完成后交工程师汇总下一步',
  };
  if (/^experiment_decide:[a-f0-9]{64}:artifact-synthesis-1:[a-f0-9]{64}$/.test(purpose)) return {
    name: '汇总产物复核结论',
    summary: '工程师依据三方独立核验提出具体下一步；真实缺口保留为明确阻塞，执行提案仍须独立投票。',
    expected: '具体下一步及证据绑定，或明确缺项、原因与补齐责任',
  };
  if (/^experiment_decide:[a-f0-9]{64}:artifact-evidence-1:[a-f0-9]{64}$/.test(purpose)) return {
    name: '核验既有运行产物',
    summary: '宿主已提供带哈希的原运行产物，由工程师核对模型、初始化、容量与检查点绑定，提出下一步后交三方独立复核。',
    expected: '原产物核验结论、证据引用与具体下一步；本次不执行训练',
  };
  if (/^experiment_train:\d+:(experiments|tuning):[a-zA-Z0-9_-]+$/.test(purpose)) return {
    name: purpose.includes('--ablation') ? '执行冻结消融' : '执行已批准验证实验',
    summary: '核验已批准计划、代码、参数及资源后执行本项验证，保存指标、训练记录和运行身份；已完成实验直接复用。执行完成后仍需评议结果。',
    expected: '实际执行回执、验证指标、配置身份及停止原因',
  };
  if (/^experiment_diagnose:causal-plan:[a-f0-9]{64}$/.test(purpose)) return {
    name: '制定单种子训练对照',
    summary: '复用已完成训练、上次受控检查和 CPU 检查回执，提出同检查点、同新批次、仅学习率不同的最小对照方案；当前尚未启动训练。',
    expected: '单种子方案、可区分原因的预测、停止条件与证据回执',
  };
  if (/^experiment_decide:causal-plan:[a-f0-9]{64}$/.test(purpose)) return {
    name: '汇总最小训练对照方案',
    summary: '综合三方独立方案，确定一个种子、有限更新步数和学习率对照，随后提交代码与协议评审。',
    expected: '保留冻结计划、不重跑已完成训练的单批对照方案',
  };
  if (/^rapid_review:causal:[a-f0-9]{64}(?::source-completion-1)?$/.test(purpose)) return {
    name: '训练对照执行前评审',
    summary: '独立核对对照源码、检查点、批次、判据与资源，确认能区分局部泛化权衡和更新不稳定；三方批准后执行一次。',
    expected: '完整证据回执、独立批准或具体阻塞项',
  };
  if (/^experiment_diagnose:controlled-result:[a-f0-9]{64}$/.test(purpose)) return {
    name: '受控检查结果诊断',
    summary: '读取检查点复现、相同批次的学习率对照和逐步更新统计，分别判断过拟合、更新不稳定与检查点问题，并保留证据不足项。',
    expected: '三类原因的证据、反证、判断与检查范围限制',
  };
  if (/^rapid_review:controlled:[a-f0-9]{64}(?::source-completion-1)?$/.test(purpose)) return {
    name: '受控检查方案评审',
    summary: '核对原始源码、检查候选和资源边界，确认三个对照能区分原因，并只对本次受控检查投票。',
    expected: '完整源码阅读回执、独立意见与具体阻塞项',
  };
  const base = purpose.split(':')[0];
  if (!Object.prototype.hasOwnProperty.call(tasks, base)) return undefined;
  const task = tasks[base];
  if (!task || (purpose !== base && !/^[a-z_]+:[a-f0-9]{64}(?::[a-z0-9:-]+)?$/.test(purpose))) return undefined;
  return { name: task.name, summary: task.roles?.[role ?? ''] ?? task.summary, expected: task.expected };
}
