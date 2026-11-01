import { ReferenceText } from './ReferenceNames';
import { experimentTaskDescription } from './ExperimentTaskDescriptions';
import { hasRecoveryDiagnostic, requestRecoveryText, NO_FINAL_RETRY_REASON, NO_FINAL_RETRY_DESCRIPTION, READ_TIMEOUT_LIMIT_NOTE } from './RequestRecoveryDiagnostic';

export const PROCESS_RESTARTED_REASON = 'Process restarted. Resume from the last committed checkpoint.';
export const PROCESS_RESTARTED_DESCRIPTION = '后端重启后，任务已暂停在最后保存的检查点。使用顶部“恢复执行”发起恢复，系统会重新核验恢复条件；已完成阶段和已有结果保留。';
export const PENDING_REQUEST_RECEIPTS_REASON = 'Provider requests are still pending; their receipts must settle first';
export const PENDING_REQUEST_RECEIPTS_DESCRIPTION = '仍有未结算的模型请求，请先核对请求回执；当前不能授权最终回答重试。';

/** Chinese presentation of controller messages; original diagnostics stay available. */
const labels: Record<string, string> = {
  director: '研究总监', engineer: '实验工程师', scout: '文献研究员',
  pending: '待处理', running: '运行中', completed: '已完成', failed: '失败', blocked: '等待处理',
  paused: '已暂停', interrupted: '已中断', recovering: '恢复校验中', cancelled: '已取消', queued: '等待接收', idle: '待命',
  ready: '准备就绪', dispatching: '请求已派发', retry_wait: '等待重试', response_saved: '回复已保存',
  passed: '已通过', rejected: '未通过', active: '进行中', skipped: '已跳过',
  collecting: '正在收集投票', awaiting_commit: '等待完整共识提交', not_dispatched: '未发出请求',
  system: '系统', user: '用户', harness: '执行框架',
  live_provider: '真实模型调用', local_cache: '本地缓存', demo: '模拟演示', live: '真实运行',
  cached_live_provider: '复用真实模型缓存', lexical_hash_v1: '词项与特征哈希检索（第 1 版）',
  baseline: '基线方法', proposed: '候选方法', ablation: '消融实验', validation: '验证集', test: '测试集',
  'literature.search': '检索文献', 'literature.survey': '检索近期进展', 'literature.verify': '核验文献来源',
  'literature.read_papers': '获取并索引论文', 'sources.read': '读取文献内容', 'evidence.request': '委派证据核验',
  'web.search': '学术网络检索', 'papers.download': '下载论文至本地', 'pdf.info': '查看本地 PDF 目录',
  'pdf.search': '检索 PDF 页内内容', 'pdf.read': '按页读取本地 PDF',
  'memory.search': '检索持久记忆', 'memory.write': '保存持久记忆', 'artifacts.read': '读取研究产物', 'documents.list': '查看文档索引', 'documents.search': '检索文档片段', 'documents.read': '按索引读取文档章节',
  'experiments.history': '查阅实验记录', 'experiments.status': '核对实验状态与证据版本',
  'experiments.analyze': '分析实验比较、训练诊断与证据完整性',
  'experiments.compare': '读取冻结实验对比证据', 'experiments.inspect_module': '核查相关模块源码与调用关系',
  'hardware.profile': '读取主机配置', 'code.validate': '检查实验代码', 'implementation.write_document': '编写代码实现文档', 'implementation.edit_document': '修改代码实现文档',
  'architecture.draw_diagrams': '生成架构图与流程图', architecture_diagram_author: '生成架构图与流程图',
  'hardware.inspect': '检查主机配置', 'resources.assess': '评估资源需求',
  'sources.fetch': '获取文献正文', 'sources.open_reference': '读取已选参考资料',
  'review.citations_discarded': '引用自动清理记录',
  'handoff.reference_self_repair': '评审引用自动修正', review_reference_repair: '修正评审引用',
  'handoff.fix_self_repair': '自动补齐修改建议', review_fix_repair: '补齐遗漏的修改建议',
  'handoff.review_split': '自动分段完成评审', review_split_judgment: '分段评审：判断与异议', review_split_remedy: '分段评审：逐项修改建议',
  innovation: '创新点讨论', innovation_candidate: '独立提出创新点', innovation_compare: '比较并收束创新点', intake: '解析研究需求', literature: '文献发现', proposal: '提出研究方案', debate: '方案讨论',
  vote: '独立评议', amendment: '提交修改意见', evidence: '补充证据', design: '架构设计', design_document_section: '分段评议架构文档', design_document_review: '汇总架构文档评议', design_revision: '根据具体意见修订架构文档', architecture_definitions_author: '补充未闭合的具体科学定义', architecture_definitions_review: '独立审议科学定义补充', implementation_spec: '编写代码实现文档', implementation_document_review: '三方全文审核',
  discussion_action: '交付讨论阻塞项的具体修正', literature_reading: '补读并保存文献证据',
  architecture_definition_issue_author: '逐项编写科学定义候选',
  architecture_definition_work_plan: '整理定义依赖与评议范围',
  architecture_component_review: '审议相关定义与具体修正',
  'discussion.work_started': '开始解决讨论阻塞项', 'discussion.work_delivered': '已交付具体修正材料',
  'discussion.records_resolved': '已复核讨论中的历史记录引用',
  'discussion.constraints_updated': '讨论已同步当前执行上限',
  'provider.retained_response_recovered': '已复用原完成回执恢复回复',
  'context.evidence_raw_retained': '摘要未通过核验，已保留原文',
  'innovation.quality_rejected': '候选研究价值不足，重新思考', 'innovation.direction_reopened': '重新选择研究问题',
  implementation: '代码实现', review: '交付审查', experiments: '实验验证', tune: '参数调整',
  tuning: '参数搜索', validation_review: '验证结果评审', revision: '修订方案',
  revise: '修订方案', experiment: '实验执行',
  final_tests: '独立测试', report: '结果报告', acceptance: '定义验收标准',
  protocol_review: '实验方案复审', cycle_review: '研究周期评审', repair: '修复实验代码',
  '三位 Agent 一致同意': '三位智能体一致同意',
  '三位 Agent 同意本版本立项；效果仍待实验验证': '三位智能体同意本版本立项；效果仍待实验验证',
  '共享证据、检索与工具结果；模型响应按角色/供应商隔离。远程 KV 由供应商管理，无法跨模型共享；仅将服务端 usage 中的缓存命中计为 KV 命中。默认价格是保守的预算估算，不是瑞科实际单价。': '共享证据、检索与工具结果；模型响应按角色和供应商隔离。远程推理缓存由供应商管理，无法跨模型共享；仅按服务端用量报告中的缓存命中计量。默认价格是保守的预算估算，不是瑞科实际单价。',
};

// Operator-facing summaries of known controller templates, not translations of
// scientific prose. Match complete opening sentences; the full instruction is
// retained by HumanMessage. Instruction text, hashes and stored records are untouched.
const controllerAssignmentPrefixes: readonly (readonly [string, string])[] = [
  ['Translate the user-defined research goal into a precise research question.', '明确用户的研究问题、成果要求和资源约束，并准备学术检索词。'],
  ['Review retrieved literature and the user paper.', '审阅已检索文献与用户论文，区分来源核验和结论验证，提出相关基线与证据缺口。'],
  ['Propose one falsifiable, resource-feasible improvement matching the user domain and supplied paper.', '提出符合研究方向与本机资源、能够被实验检验的改进方案，说明基线、数据、风险和成果缺口。'],
  ['Independently review this exact proposal.', '独立评审当前提案，给出同意或异议、具体理由及修改方案；同意立项不代表实验结果已通过验证。'],
  ['Supplement your own saved dissenting review with concrete modifications.', '为自己已保存的每项异议补充具体修改方案、验证办法和相关证据；保留原投票，不代替其他角色同意。'],
  ['Revise the proposal to resolve the explicit objections and resource failures.', '依据明确异议和资源限制实质修订提案，保留用户研究方向和未解决的分歧。'],
  ['Design an executable experiment for the agreed domain-specific idea.', '为已同意的方案设计可复现的基线、候选、消融与共享调参实验，明确验收条件、停止策略和资源需求。'],
  ['Implement the agreed domain-specific research design as a COMPLETE standalone Python program, not a template or synthetic replacement unless the design explicitly justified synthetic data.', '实现或修复已批准的完整实验程序，按记录参数计算真实指标，遵守数据划分、复现和资源要求。'],
  ['Audit this complete Python experiment.', '审查完整实验代码的数据真实性、计算过程、数据泄漏、基线和消融分支、复现与资源约束。'],
  ['Review repaired code for correctness, unchanged methodology, genuine numeric computation, no leakage and resource compliance.', '复审修复后的代码，核对方法保持一致、数值计算真实、数据无泄漏且资源需求可行。'],
  ['Translate the already agreed quantitative success criteria into a machine-readable contract before any experiments.', '在实验前将已同意的定量成功条件整理为可检查的验收规则，保留全部指标要求。'],
  ['Review this exact premeasurement execution protocol against the unanimously approved proposal.', '独立核对实验执行方案与已批准提案是否一致，检查完整参数网格、验收阈值和停止规则。'],
  ['Independently decide whether this exact validated research revision is ready for a frozen final test.', '独立评审真实验证结果，判断能否进入固定配置的最终测试；若未准备好，提出下一轮具体修改意见。'],
  ['Use the actual validation failures and independent objections to revise the hypothesis, method, architecture or parameter strategy for the next experiment cycle.', '依据真实验证失败和各角色异议，为下一轮实验实质调整假设、方法、架构或参数策略。'],
  ['Write a transparent research report in Chinese for the user objective.', '依据真实研究记录撰写中文报告，整理实验结果、负结果、证据、限制与复现步骤，不将流程完成等同于科研成功。'],
  ["Answer the director's bounded evidence_request in context.constraints.", '根据研究总监限定的证据问题检索和阅读资料，报告支持与反对证据、来源核验状态及覆盖限制。'],
];

export function controllerAssignmentSummary(raw: string): string | null {
  const text = raw.trim();
  const matched = controllerAssignmentPrefixes.find(([prefix]) => text.startsWith(prefix));
  return matched ? `任务概要：${matched[1]}` : null;
}

export function sourceSupplementTaskSummary(text: string): string | null {
  const match = /^implementation_source_supplement:[a-f0-9]{64}:(scout|engineer|director)(?::unit:(H0[1-4]|D0[12]|G0[1-5]):[a-f0-9]{64})?$/.exec(text);
  if (!match) return null;
  const role = { scout: '文献研究员', engineer: '实验工程师', director: '研究总监' }[match[1]];
  return `实现文档补充审核 · ${role}${match[2] ? ` · ${match[2]}` : ''}`;
}

export function humanizeMessage(raw: string): string {
  const text = String(raw ?? '').trim();
  if (!text) return '';
  if (Object.prototype.hasOwnProperty.call(labels, text)) return labels[text];
  const experimentTask = experimentTaskDescription(text);
  if (experimentTask) return experimentTask.name;
  const supplementTask = sourceSupplementTaskSummary(text);
  if (supplementTask) return supplementTask;
  const definitionTask = text.match(/^(architecture_definition_issue_author|architecture_definitions_author|architecture_definitions_review|architecture_definition_work_plan|architecture_component_review):[a-f0-9]{64}$/);
  if (definitionTask) return labels[definitionTask[1]];
  const diagramTask = text.match(/^architecture_diagram_author:[a-f0-9]{64}:(\d+)$/);
  if (diagramTask) return `${labels.architecture_diagram_author} · 第 ${diagramTask[1]} 次`;
  const reviewPart = text.match(/^(review_split_judgment|review_split_remedy):[a-f0-9]{64}$/);
  if (reviewPart) return labels[reviewPart[1]];
  // Normal assignment constraints mention budgets/timeouts; they are not errors.
  const assignment = controllerAssignmentSummary(text);
  if (assignment) return assignment;
  if (hasRecoveryDiagnostic(text)) return requestRecoveryText(text);
  if (text === NO_FINAL_RETRY_REASON) return NO_FINAL_RETRY_DESCRIPTION;
  if (text === PROCESS_RESTARTED_REASON) return PROCESS_RESTARTED_DESCRIPTION;
  if (text === PENDING_REQUEST_RECEIPTS_REASON) return PENDING_REQUEST_RECEIPTS_DESCRIPTION;
  if (/content_policy_violation|content moderation|policy review|内容审计命中/i.test(text))
    return '请求触发了内容审核，需要核对审核记录后处理；目前不能据此判断是网关拦截还是上游拒绝。';
  if (/original provider and model must remain selected|provider (mapping|identity).*changed|retry provider.*changed|selected provider channel/i.test(text))
    return '当前模型或连接配置与原请求不一致，暂不能直接重试。旧请求及费用记录已保留。';
  if (/ReadTimeout|read timed out|network request failed.*timeout|Final-only completion did not finish.*timeout/i.test(text))
    return '等待模型回复超时，尚未取得完整结果。请求可能已经计费，请先核对请求记录，再决定是否恢复。' + READ_TIMEOUT_LIMIT_NOTE;
  if (/ConnectTimeout|ConnectError|Failed to fetch|NetworkError|ECONNREFUSED|network request failed/i.test(text))
    return '连接未完成，请检查服务是否运行以及网络和接口地址；当前请求未取得可用结果。';
  if (/Normal request was already dispatched|Final-only request was already dispatched|automatic retransmission is disabled/i.test(text))
    return '上次请求已发出，但没有可复用的完整结果。系统已停止自动重发，请核对记录和恢复条件。';
  if (/Maximum discussion rounds reached without unanimous agreement/i.test(text))
    return '讨论已达到当前轮次上限，但各角色尚未一致同意。请查看异议与修改意见，调整方案或讨论设置后继续。';
  if (/Discussion stalled without material progress|repeated.*without progress/i.test(text))
    return '讨论出现重复或缺少实质进展，已暂停。请查看尚未解决的问题和具体修改意见。';
  if (/Modification cites evidence outside|outside the verified task evidence set/i.test(text))
    return '修改意见引用了当前任务尚未核验的来源，需要核对引用标识和证据后继续。';
  if (/Constructive review is incomplete/i.test(text))
    return '修改意见尚不完整或未通过校验，请核对具体建议、验证步骤与引用来源。';
  if (/output.*truncat|output limit|output-limit|effective output limit/i.test(text))
    return '回复或输出额度未满足当前任务的要求，请核对输出限制和完整性记录后再恢复。';
  if (/Final-only response rejected|structured final result|did not match.*schema|invalid JSON|JSON.*invalid|schema validation/i.test(text))
    return '模型回复未符合要求的结构或字段，暂不能作为有效交付。请查看原始诊断和回复。';
  if (/no access to model|not allowed.*model|model.*not found/i.test(text))
    return '当前密钥或连接通道无法使用该模型，请核对模型名称和访问权限。';
  if (/HTTP 401|Unauthorized|authentication/i.test(text)) return '身份验证未通过，请检查连接配置和密钥。';
  if (/HTTP 403|Forbidden/i.test(text)) return '请求被拒绝，请核对访问权限和服务方诊断；仅凭此状态不能确定拒绝原因。';
  if (/HTTP 429|rate limit/i.test(text)) return '请求受到限流，请等待冷却后再处理。';
  if (/HTTP (500|502|503|504)|bad_response_status_code/i.test(text)) return '模型服务返回临时错误，系统会依据已保存回执和重试上限处理。';
  if (/budget|maximum.*calls|call limit|reservation.*limit/i.test(text)) return '当前请求未通过费用或调用次数检查，请核对用量与执行配置。';
  if (/circuit.*cooling|cool.*down/i.test(text)) return '该连接正在等待故障冷却，冷却结束后才能继续请求。';
  if (/No terminal final request.*canonical unknown-charge/i.test(text)) return '没有符合单次恢复条件的未知结果请求。请核对是否已有回复、回执是否完整及连接配置是否一致。';
  if (/saved one-attempt authorization.*awaiting Resume/i.test(text)) return '已保存单次重试授权，等待恢复执行；发送前仍会检查执行条件。';
  if (/Worker interrupted|Stopped during stage|Interrupted;.*resume/i.test(text)) return '工作进程已中断；恢复时会复用已经保存的结果和工具记录。';
  if (/^Return the final (answer|result)|^Complete .*assignment/i.test(text)) return '汇总已获得的证据，提交本项任务的最终结果。';
  const task = text.match(/^([a-z_]+):(\d+)(?::(.*))?$/);
  if (task && labels[task[1]]) return `${labels[task[1]]} · 第 ${task[2]} 轮`;
  if (/^[A-Za-z][A-Za-z_.]+$/.test(text)) return '其他记录';
  // Unknown English prose is not guessed into a scientific conclusion.
  if (/[\u3400-\u9fff]/.test(text)) return text;
  const withoutIdentifiers = text.replace(/https?:\/\/\S+|[A-Za-z]:[\\/][^\s]+|\b(?:gpt|claude|grok|deepseek|qwen)[\w.-]*/gi, '');
  if ((withoutIdentifiers.match(/[A-Za-z]{3,}/g) ?? []).length >= 4)
    return /[\u3400-\u9fff]/.test(text) ? '这条记录包含英文说明，请展开原文核对完整内容。' : '这条记录尚无中文说明，可展开查看原文。';
  return text;
}

export function HumanMessage({ text, className = '' }: { text: string; className?: string }) {
  const message = humanizeMessage(text);
  if (!message) return null;
  return <div className={`human-message ${className}`.trim()}><span><ReferenceText value={message} /></span>{message !== text.trim() && <details><summary>查看原始记录</summary><pre tabIndex={0}>{text}</pre></details>}</div>;
}
