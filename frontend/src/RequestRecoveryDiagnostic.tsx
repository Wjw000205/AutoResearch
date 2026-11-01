import { ReferenceText } from './ReferenceNames';
import { CircleAlert } from 'lucide-react';
import type { Run } from './api';

export const NORMAL_DISPATCH_GATE = 'Normal request was already dispatched without a verified complete or output-limit receipt; automatic retransmission is disabled';
export const REQUEST_RECOVERY_TITLE = '上次请求暂无可复用的完整结果';
export const REQUEST_RECOVERY_DESCRIPTION = '请求已发出，但尚无可复用的完整结果。系统已暂停自动重发，避免重复请求；请先核对请求记录与恢复条件。';
export const NO_FINAL_RETRY_REASON = 'No terminal final request has complete canonical unknown-charge receipts without a saved output';
export const NO_FINAL_RETRY_DESCRIPTION = '未发现需要另行确认的最终回答重试；普通回合的恢复入口在顶部，仍需系统核验。此状态不代表普通回合已经可恢复。';
export const READ_TIMEOUT_LIMIT_NOTE = '调整读取超时只影响后续请求，不会补回本次回复或自动获得重试资格。';
export const NATIVE_CODEX_TITLE = '本机模型尚未完成回复';
export const NATIVE_CODEX_DESCRIPTION = '恢复时先核对提交状态：存在可定位会话时才回读原会话。确认因服务过载等临时错误失败且无回复后，废弃旧请求并重新发起；原回执、用量和已有研究结果保留。尚在运行或状态不明的请求先核验，明确未提交的请求继续初始化。可在执行记录中查看恢复结果。';
export const NATIVE_INITIALIZATION_TITLE = '本机模型请求初始化未完成';
export const NATIVE_INITIALIZATION_DESCRIPTION = '当前检查点确认首次推理尚未提交，会话初始化未完成。恢复执行后继续同一请求的初始化，保留原任务、参数和已有预算记录；不会回读不存在的原会话，也不会回退到 API。';
export const ARCHITECTURE_EVIDENCE_REPEAT_GATE = '架构定向补读后仍重复相同证据缺口；已保留实际意见，须处理具体来源问题。';
export const ARCHITECTURE_EVIDENCE_REPEAT_TITLE = '架构补读仍有相同证据缺口';
export const ARCHITECTURE_EVIDENCE_REPEAT_DESCRIPTION = '已保存的同版本补读评议仍提出相同证据请求，系统暂停了重复补读。请打开架构文档与评议，核对该角色的具体问题、修改意见和请求的来源范围，处理后再继续；当前尚未通过独立架构评议。';
export const GATEWAY_TIMEOUT_GATE = 'openai returned HTTP 524: openai_error';
export const FINAL_GATEWAY_TIMEOUT_GATE = `Final-only completion did not finish: ${GATEWAY_TIMEOUT_GATE}`;
export const GATEWAY_TIMEOUT_TITLE = '网关等待后端响应超时（HTTP 524）';
export const GATEWAY_TIMEOUT_NEXT_ACTION = '请先核对网关请求记录与费用回执，再确认当前恢复条件；此错误本身不能证明请求可直接重试。';
export const GATEWAY_TIMEOUT_DESCRIPTION = 'HTTP 524 表示网关等待后端响应超时，现有信息不能确定具体服务故障或实际扣费。' + GATEWAY_TIMEOUT_NEXT_ACTION + '增加本地读取超时不保证解决本次错误。';
export const DISCUSSION_STALLED_PREFIX = 'Discussion stalled without material progress:';
export const DISCUSSION_STALLED_TITLE = '讨论缺少实质进展，已暂停';
export const DISCUSSION_STALLED_DESCRIPTION = '系统记录了讨论停滞或重复循环，已暂停继续讨论。请查看各角色评议与未解决问题；仅改写表述或补入无关资料不算实质进展。';
export const CONSTRUCTIVE_CITATION_PREFIX = 'Constructive review is incomplete: Modification cites evidence outside the verified task evidence set';
export const CONSTRUCTIVE_CITATION_TITLE = '引用待自动清理';
export const CONSTRUCTIVE_CITATION_DESCRIPTION = '恢复执行时，系统会废弃当前任务引用集合中核对不到的编号，复用已保存回复继续讨论；该清理不额外调用模型。原始回复和反对意见保留，废弃引用不计为已核验证据；其他评审与执行条件仍会单独检查。';
export const CONSTRUCTIVE_CONTEXT_GATE = 'Constructive review is incomplete: Context reference is not an existing nonempty allowed public path';
export const CONSTRUCTIVE_CONTEXT_TITLE = '修改建议中的上下文引用未通过校验';
export const CONSTRUCTIVE_CONTEXT_DESCRIPTION = '修改方案引用的内部字段路径，未满足“允许公开、当前存在且内容非空”的要求。请查看该角色的修改方案与原始记录，依据当前提案和评议内容核对引用路径；该项引用通过校验后才能继续。其他并行评审的请求恢复需单独核验。';
export const DISCUSSION_EVIDENCE_GATE = 'Discussion delivery cited evidence outside the saved task index';
export const DISCUSSION_EVIDENCE_TITLE = '讨论交付的引用待核对';
export const DISCUSSION_EVIDENCE_DESCRIPTION = '讨论回复已保存。系统会核对引用的任务归属、读取回执与内容哈希；历史检查点和交接索引保留为记录引用，不能代替论文原文或已读笔记。可复核的记录会复用，无法定位或已变更的引用仍需处理；其他评审和执行条件继续检查。';
export const CONTEXT_PREFLIGHT_GATE = 'Critical task/evidence exceeds context limit; raise context_tokens or narrow the supplied material. Batch preflight did not modify persisted context.';
export const CONTEXT_PREFLIGHT_TITLE = '必要证据超过上下文预算，发送前预检未通过';
export const CONTEXT_PREFLIGHT_DESCRIPTION = '必须保留的任务与证据超过本次可用上下文预算，系统在本地发送前预检时停止；本批模型请求尚未发送，已有持久化上下文未被改写。请在执行配置中核对上下文词元预算，或缩小本次必需材料；模型自身的上下文窗口上限仍然生效。';
export const LITERATURE_READING_GATE_PREFIX = '文献阅读尚未完成，补读后无足够证据进展：';
export const LITERATURE_READING_TITLE = '文献阅读笔记尚未通过门禁';
export const LITERATURE_READING_DESCRIPTION = '当前核心论文的合格阅读笔记尚不足。正文获取与索引、实际读取片段、保存通过校验的笔记是不同状态；已保存的阅读记录可在研究证据中核对。找不到正文时，可检索 arXiv 等公开版本，并查看已记录的跳过清单与原因；跳过不计为已读或论点证据。请逐篇查看缺少的笔记及其原文读取依据，处理缺项或校验问题后再恢复；重复模型请求不能替代笔记核验。';
export const NOVELTY_EVIDENCE_TITLE = '相似性审查仍有待补证据';
export const NOVELTY_EVIDENCE_DESCRIPTION = '当前候选与已有阅读笔记已保留。系统先复用RAG中的分析，按各角色评审列出的缺项定位附录或关键机制片段；新增笔记通过引用校验并入RAG后，才继续独立评审。同一片段和未产生新摘录的补读不会反复触发付费评审。请在研究证据与评议中查看具体缺项；当前状态尚未通过创新和实验门禁。';
const noveltyEvidenceGates = [
  '相似性审查两轮定向补读后仍有证据缺口；保留当前候选，未将缺证据当作创新已否决。',
  '相似性核查补读尚未形成合格原文笔记；保留当前候选和补读记录，不自动判为科学否决。',
  '定向获取修复已完成，独立审查仍有具体未解决问题；补读与评审已保存，不重复请求或自动放行实验。',
];

// Translate only this known harness state and its two server projection prefixes.
// Public transcripts and unrecognized provider errors must retain their original text.
const projectionPrefixes = ['', '处理阻塞后从当前检查点继续：', '处理失败原因后恢复当前检查点：'];
export function jsonContractDiagnostic(message: string): { kind: 'working_note_hash' | 'schema' | 'syntax'; title: string; description: string } | null {
  let diagnostic = message;
  for (const prefix of projectionPrefixes.filter(Boolean)) {
    if (diagnostic.startsWith(prefix)) { diagnostic = diagnostic.slice(prefix.length); break; }
  }
  const finalPrefix = 'Final-only completion did not finish: ';
  if (diagnostic.startsWith(finalPrefix)) diagnostic = diagnostic.slice(finalPrefix.length);
  if (/^Provider JSON failed schema validation at \$\.working_notes\[\d+\]\.anchors\[\d+\]\.expected_sha256: must satisfy pattern$/.test(diagnostic)) {
    return { kind: 'working_note_hash', title: '阅读笔记引用格式待核对', description: 'JSON 已解析，但阅读笔记引用的文档哈希不符合 64 位格式。恢复时，系统核对已保存的真实读取回执，只有唯一可验证的引用才自动修复；原答复和笔记内容保留。无法匹配时仍暂停。' };
  }
  if (diagnostic.startsWith('Provider JSON failed schema validation at ')) {
    return { kind: 'schema', title: '答复字段校验未通过', description: 'JSON 已解析，但部分字段未满足本次请求的格式或内容约束。原答复与检查点已保留；恢复时先核验保存结果，仍须通过原有的字段、版本与证据检查。' };
  }
  if (/^Provider returned no valid JSON object(?:$|;|:)/.test(diagnostic)) {
    return { kind: 'syntax', title: '答复 JSON 格式无法解析', description: '模型答复未能解析为完整 JSON。系统先尝试本地格式修复；确认原请求完整结束后，若仍无法解析，会交回原智能体修正一次。原答复与工具记录保留，重试受预算和原有校验限制；再次失败或请求状态不明时仍暂停。' };
  }
  return null;
}
export function nativeCodexErrorCode(message: string): string | null {
  const formats = [
    /^本机 Codex 请求尚无完整结果：([a-z][a-z0-9_]{0,80})；原回执已保存，恢复时读取原会话，不回退到 API。$/,
    /^本机 Codex 初始化尚未完成：([a-z][a-z0-9_]{0,80})；尚未提交模型回合，恢复时继续原请求的首次初始化。$/,
  ];
  for (const prefix of projectionPrefixes) {
    for (const format of formats) {
      const match = message.startsWith(prefix) && message.slice(prefix.length).match(format);
      if (match) return match[1];
    }
  }
  return null;
}
export const isNativeCodexGate = (message: string) => nativeCodexErrorCode(message) !== null;

function nativeExecutableDiagnostic(message: string) {
  const code = nativeCodexErrorCode(message);
  if (['codex_not_installed', 'codex_executable_invalid', 'codex_process_start_failed'].includes(code ?? '')) {
    return { title: '本机 Codex 程序无法启动',
      description: '后端未找到可用的 Codex 程序，或启动失败。系统会检查 PATH 和桌面版安装目录；可在 .env.local 中设置 AUTORESEARCH_CODEX_EXECUTABLE 的绝对路径。程序恢复可用后，再按原检查点核对会话和提交状态。此次错误不能判断旧请求是否完成，原回执和用量保留。' };
  }
  return null;
}

export function nativeReasoningDiagnostic(run: Run) {
  const executable = nativeExecutableDiagnostic(run.error ?? '');
  if (executable) return executable;
  const hint = run.native_reasoning_recovery;
  const current = hint?.derived === true && hint.run_id === run.id && hint.run_updated_at === run.updated_at
    && typeof hint.operation_id === 'string' && hint.operation_id.length > 0
    && typeof hint.error_code === 'string'
    && hint.error_code === nativeCodexErrorCode(run.error ?? '');
  if (current && hint.kind === 'transient_service_failure' && hint.error_code === 'codex_turn_failed'
    && hint.readback_required === true && ['serverOverloaded', 'internalServerError', 'httpConnectionFailed',
      'responseStreamConnectionFailed', 'responseStreamDisconnected', 'responseTooManyFailedAttempts'].includes(hint.category)) {
    return { title: hint.category === 'serverOverloaded' ? '本机模型服务过载，本次请求已失败' : '本机模型服务或连接异常，本次请求已失败',
      description: '已保存的回执显示本次请求失败，未收到回复或工具活动。点击“恢复执行”后，系统先回读原会话确认终止状态，再核对预算、模型与冻结输入；符合条件时创建一次新请求。原请求及用量保留，连续失败仍会暂停。' };
  }
  const pendingInitialization = current && hint.kind === 'unsubmitted_initialization' && hint.resume_same_request === true;
  return pendingInitialization ? { title: NATIVE_INITIALIZATION_TITLE, description: NATIVE_INITIALIZATION_DESCRIPTION }
    : { title: NATIVE_CODEX_TITLE, description: NATIVE_CODEX_DESCRIPTION };
}
export function isNormalDispatchGate(message: string): boolean {
  return projectionPrefixes.some(prefix => message === prefix + NORMAL_DISPATCH_GATE);
}
export function isGatewayTimeoutGate(message: string): boolean {
  return projectionPrefixes.some(prefix => [GATEWAY_TIMEOUT_GATE, FINAL_GATEWAY_TIMEOUT_GATE].some(known => message === prefix + known));
}
export function isDiscussionStalled(message: string): boolean {
  return projectionPrefixes.some(prefix => message.startsWith(prefix + DISCUSSION_STALLED_PREFIX));
}
export function isConstructiveCitationFailure(message: string): boolean {
  return projectionPrefixes.some(prefix => message === prefix + CONSTRUCTIVE_CITATION_PREFIX);
}
export function isConstructiveContextFailure(message: string): boolean {
  return projectionPrefixes.some(prefix => message === prefix + CONSTRUCTIVE_CONTEXT_GATE);
}
export function isDiscussionEvidenceGate(message: string): boolean {
  return projectionPrefixes.some(prefix => message === prefix + DISCUSSION_EVIDENCE_GATE);
}
export function isContextPreflightGate(message: string): boolean {
  return projectionPrefixes.some(prefix => message === prefix + CONTEXT_PREFLIGHT_GATE);
}
export function isContextCapacityGate(message: string): boolean {
  const known = [
    CONTEXT_PREFLIGHT_GATE,
    'Critical task/evidence exceeds context limit; raise context_tokens or narrow the supplied material. Full snapshot retained.',
    'Context window leaves insufficient room for role instructions and output',
    'Task and JSON schema exceed the configured context budget',
  ];
  return projectionPrefixes.some(prefix => known.some(error => message === prefix + error));
}
export function isLiteratureReadingGate(message: string): boolean {
  return projectionPrefixes.some(prefix => message.startsWith(prefix + LITERATURE_READING_GATE_PREFIX));
}
export function isNoveltyEvidenceGate(message: string): boolean {
  return projectionPrefixes.some(prefix => noveltyEvidenceGates.some(known => message === prefix + known)
    || message.startsWith(prefix + '定向补读未新增有效原文摘录，未消耗完成轮次或重复付费评审；'));
}
export function literatureReadingGateIssue(message: string): string | null {
  const prefix = projectionPrefixes.find(prefix => message.startsWith(prefix + LITERATURE_READING_GATE_PREFIX));
  return prefix === undefined ? null : message.slice(prefix.length + LITERATURE_READING_GATE_PREFIX.length).trim();
}
export function isHistoricalReadingGate(message: string, currentApproved: unknown): boolean {
  return currentApproved === true && isLiteratureReadingGate(message);
}
export const hasRecoveryDiagnostic = (message: string) => jsonContractDiagnostic(message) !== null || isNormalDispatchGate(message) || isDiscussionStalled(message) || isConstructiveCitationFailure(message) || isConstructiveContextFailure(message) || isDiscussionEvidenceGate(message) || isContextPreflightGate(message) || isLiteratureReadingGate(message) || isNoveltyEvidenceGate(message) || isGatewayTimeoutGate(message);

export function requestRecoveryText(message: string): string {
  const contract = jsonContractDiagnostic(message);
  if (contract) return contract.description;
  if (isNativeCodexGate(message)) return nativeExecutableDiagnostic(message)?.description ?? NATIVE_CODEX_DESCRIPTION;
  if (isDiscussionEvidenceGate(message)) return DISCUSSION_EVIDENCE_DESCRIPTION;
  if (isNoveltyEvidenceGate(message)) return NOVELTY_EVIDENCE_DESCRIPTION;
  return isNormalDispatchGate(message) ? REQUEST_RECOVERY_DESCRIPTION : isDiscussionStalled(message) ? DISCUSSION_STALLED_DESCRIPTION : isConstructiveCitationFailure(message) ? CONSTRUCTIVE_CITATION_DESCRIPTION : isConstructiveContextFailure(message) ? CONSTRUCTIVE_CONTEXT_DESCRIPTION : isContextPreflightGate(message) ? CONTEXT_PREFLIGHT_DESCRIPTION : isLiteratureReadingGate(message) ? `阅读门禁缺项：${literatureReadingGateIssue(message) || '原始记录未列出具体缺项，请查看研究证据。'}` : isGatewayTimeoutGate(message) ? (message.endsWith(FINAL_GATEWAY_TIMEOUT_GATE) ? '最终续答未完成。' : '') + GATEWAY_TIMEOUT_DESCRIPTION : message;
}

export function RequestRecoveryDiagnostic({ message, historical = false }: { message: string; historical?: boolean }) {
  if (!hasRecoveryDiagnostic(message)) return null;
  if (historical && isLiteratureReadingGate(message)) return <details className="request-retry-unavailable"><summary>上次停止记录（当前阅读门禁检查已通过）</summary><p>这条记录保留此前的停止原因；当前检查通过不代表研究已恢复或其他执行条件已通过。</p><pre tabIndex={0} aria-label="上次停止的原始诊断，可选中复制"><ReferenceText value={message} /></pre></details>;
  return <div className="error-box request-recovery-diagnostic" role="alert"><CircleAlert size={18} /><div><strong>{jsonContractDiagnostic(message)?.title ?? (isDiscussionEvidenceGate(message) ? DISCUSSION_EVIDENCE_TITLE : isNoveltyEvidenceGate(message) ? NOVELTY_EVIDENCE_TITLE : isDiscussionStalled(message) ? DISCUSSION_STALLED_TITLE : isConstructiveCitationFailure(message) ? CONSTRUCTIVE_CITATION_TITLE : isConstructiveContextFailure(message) ? CONSTRUCTIVE_CONTEXT_TITLE : isContextPreflightGate(message) ? CONTEXT_PREFLIGHT_TITLE : isLiteratureReadingGate(message) ? LITERATURE_READING_TITLE : isGatewayTimeoutGate(message) ? GATEWAY_TIMEOUT_TITLE : REQUEST_RECOVERY_TITLE)}</strong><p><ReferenceText value={requestRecoveryText(message)} /></p><details><summary>原始诊断</summary><pre tabIndex={0} aria-label="原始技术诊断，可选中复制"><ReferenceText value={message} /></pre></details></div></div>;
}
