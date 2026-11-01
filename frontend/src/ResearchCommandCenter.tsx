import { ReferenceText } from './ReferenceNames';
import { AgentMemoryTree } from './AgentMemoryTree';
import { useEffect, useId, useRef, useState } from 'react';
import { Activity, ArrowDown, ArrowRight, ArrowUpRight, BookOpen, Check, ChevronDown, CircleAlert, Code2, Compass, FileText, GitBranch, LoaderCircle, RefreshCw, ShieldCheck, SkipForward, Terminal, Workflow, Wrench, X } from 'lucide-react';
import { date, number, textOf, type CurrentExecution, type AgentActivityProgress, type AgentCurrentActivity, type AgentHistoryEntry, type Json, type ResearchMap, type ResearchMapAgent, type ResearchMapLink } from './api';
import { isNativeCodexGate, requestRecoveryText } from './RequestRecoveryDiagnostic';
import { HumanMessage, humanizeMessage, sourceSupplementTaskSummary } from './localization';
import { CitationDiscardNotice } from './CitationDiscardNotice';
import { ToolEventSummary } from './ToolEventSummary';
import { historyDisplay } from './AgentHistoryFocus';
import { agentTaskCard } from './AgentTaskCard';
import { experimentTaskDescription } from './ExperimentTaskDescriptions';
import { conversationGeometry, useConversationLayout } from './ConversationLayout';
import { codexParticipant, codexExchanges, type CodexParticipant } from './CommandCodex';
import { ReviewFeedback } from './ReviewFeedback';
import { createAgentHistoryLoader, mergeAgentHistory, type AgentHistoryPages } from './AgentHistoryPagination';
import { useRemoteResource, useProjectionSnapshot } from './useRemoteResource';
import { optimizationDisplayStage } from './OptimizationDisplayStage';
import type { RunProjection } from './RunProjections';
export { createAgentHistoryLoader, fetchAgentHistory, mergeAgentHistory, mergeAgentHistoryPage } from './AgentHistoryPagination';

const roles: Record<string, { name: string; short: string; icon: typeof GitBranch }> = {
  director: { name: '研究总监', short: '方向 · 决策', icon: GitBranch },
  engineer: { name: '实验工程师', short: '实现 · 验证', icon: Code2 },
  scout: { name: '文献研究员', short: '证据 · 审议', icon: BookOpen },
  codex: { name: 'Codex', short: '写码 · 修复', icon: Terminal },
  harness: { name: '流程调度', short: '派发 · 收集', icon: Workflow },
};
const statusNames: Record<string, string> = { running: '正在工作', recovering: '恢复校验中', pending: '等待任务', queued: '等待接收', idle: '待命', completed: '已交付', blocked: '等待处理', paused: '已暂停', interrupted: '已中断', failed: '失败', cancelled: '已取消' };
const routeNames: Record<string, string> = { passed: '已通过', rejected: '已否决', pending: '待验证', active: '正在推进', skipped: '已跳过' };
const kindNames = { output: '公开输出', tool: '工具执行', assignment: '任务指派' };
const purposeNames: Record<string, string> = { discussion_action: '补充机制与实验设计', related_work: 'idea相关论文分析', related_work_queries: '围绕idea制定检索式', innovation: '创新点讨论', innovation_candidate: '独立提出创新点', innovation_compare: '比较并收束创新点', intake: '解析研究需求', literature: '检索与证据判断', evidence: '文献补充核验', amendment: '提交修改方案', implementation_spec: '制定明确实现规范', spec_review: '独立评审实现规范', code_review: '独立评审实际代码', code_repair_map: '整理代码修改依据', code_repair_audit: '独立核对修改依据', proposal: '提出研究方案', debate: '讨论研究方向', vote: '独立评议', design: '架构设计', implementation: '实现实验', review: '审查交付', experiments: '执行实验', tuning: '参数搜索', tune: '参数搜索', validation_review: '验证结果评审', revision: '修订研究方案', revise: '修订研究方案', protocol_review: '实验协议复审', cycle_review: '研究周期评审', repair: '修复实验实现', acceptance: '交付验收', final_tests: '独立测试', report: '整理研究报告' };
Object.assign(purposeNames, { dataset_select: '选择实验数据集', dataset_work_order: '编写配置与代码任务单', dataset_plan_review: '核验数据选型与实现任务单', innovation_review: '创新相似性审查', literature_reading: '原文阅读与笔记', literature_note_extract: '保存片段分析笔记' });
const purposeName = (value: string) => { const experimentTask = experimentTaskDescription(value); if (experimentTask) return experimentTask.name; const supplement = sourceSupplementTaskSummary(value); if (supplement) return supplement; const [base, ...suffix] = value.split(':'); if (base === 'evidence' || base === 'amendment') return purposeNames[base]; return `${purposeNames[base] ?? humanizeMessage(base)}${suffix.length ? ` · ${suffix.join(' / ')}` : ''}`; };

interface CommandMap extends Omit<ResearchMap, 'mainline'> {
  projection?: string;
  branches_loaded?: boolean;
  run_status?: string; run_error?: string | null; current_execution?: CurrentExecution | null;
  outcome?: {research_outcome?: string; phase?: string; checks?: unknown} | null;
  mainline: (ResearchMap['mainline'][number] & {is_current?: boolean; activity_status?: string | null; reason?: string | null})[];
}
const overallStatusNames: Record<string, string> = { running: '研究进行中', blocked: '研究已阻塞', paused: '研究已暂停', completed: '研究已完成', failed: '研究失败', cancelled: '研究已取消', draft: '研究待启动' };

export function ResearchOutcome({ outcome }: { outcome?: CommandMap['outcome'] }) {
  const labels: Record<string, string> = { positive: '正向证据', negative: '负结果', inconclusive: '证据不足' };
  const result = outcome?.research_outcome;
  if (!result || !labels[result]) return null;
  if (outcome?.phase === 'validation') return <div className={`command-outcome outcome-${result} outcome-validation`} aria-label="验证阶段科研结果"><CircleAlert size={15} /><span>验证阶段结果</span><strong>{`验证阶段${labels[result]} · 未执行独立测试`}</strong><small>依据已记录的验证集结果与预声明停止策略；不构成独立测试结论、方法劣势或泛化证明。</small></div>;
  return <div className={`command-outcome outcome-${result}`} aria-label="最终测试科研结果"><ShieldCheck size={15} /><span>最终测试结果</span><strong>{labels[result]}</strong><small>依据最终测试记录；流程完成与科研结论独立。</small></div>;
}

export function ExecutionActivity({ execution }: { execution?: CurrentExecution | null }) {
  if (!execution) return null;
  if (execution.kind === 'local_codex') {
    const labels = { running: execution.phase === 'acceptance' ? '本地验收中' : '本机实现中', preparing: '等待准备或检查', awaiting_confirmation: '需核对或处理' };
    const activity: Record<string, string> = { commandExecution: '执行本地命令', fileChange: '文件变更', contextCompaction: '上下文压缩' };
    return <section className={`command-execution execution-${execution.status}`} aria-label="本机 Codex 执行器当前状态"><div className="command-execution-heading"><Code2 size={17} /><strong>本机 Codex 执行器</strong><span>{labels[execution.status]}</span></div><HumanMessage text={execution.action} /><ul><li><span>实际执行目录</span><code><ReferenceText value={execution.workspace || '尚未记录'} /></code></li><li><span>会话：<ReferenceText value={execution.thread_id || '尚未记录'} /> · 请求：<ReferenceText value={execution.turn_id || '尚未记录'} /></span>{execution.last_event_at && <small>最近收到活动：{date(execution.last_event_at, true)}{activity[execution.latest_activity_type ?? ''] ? ` · ${activity[execution.latest_activity_type ?? '']}` : ''}</small>}</li></ul><small>依据已保存的 Codex 工作流与回执；三位研究角色的状态独立显示。{execution.source === 'persisted_dataset_codex' ? '公开回复、配置交付与检查详见当前实验阶段详情。' : '公开回复、产物与验收详见“代码实现与验收”。'}</small></section>;
  }
  const labels = { running: '试验运行中', preparing: '等待试验 / 结果核验', awaiting_confirmation: '结束状态待确认' };
  const phases: Record<string, string> = { validation: '验证集', test: '最终测试集' };
  const variants: Record<string, string> = { baseline: '基线', proposed: '候选', ablation: '消融' };
  return <section className={`command-execution execution-${execution.status}`} aria-label="实验执行器当前状态"><div className="command-execution-heading"><Terminal size={17} /><strong>实验执行器</strong><span>{labels[execution.status]}</span></div><HumanMessage text={execution.action} />{execution.trials.length > 0 && <ul>{execution.trials.map(trial => <li key={trial.source_id ?? trial.id}><code><ReferenceText value={trial.id} /></code><span>{phases[trial.phase ?? ''] ?? humanizeMessage(trial.phase ?? '未记录划分')} · {variants[trial.variant ?? ''] ?? humanizeMessage(trial.variant ?? '未记录方法')} · 随机种子 {trial.seed ?? '未记录'} · {trial.execution_mode === 'docker' ? 'Docker 容器' : trial.execution_mode === 'local' ? '本地进程' : '未记录运行环境'}</span>{trial.started_at && <small>{date(trial.started_at, true)} 启动</small>}</li>)}</ul>}<small>执行器状态依据实验记录；各智能体的工作状态独立显示。</small></section>;
}

export function AgentOrbit({ status, role }: { status: string; role: string }) {
  const Icon = roles[role]?.icon ?? Activity;
  const subject = role === 'harness' ? '流程调度' : '此智能体';
  return <span className={`command-orbit ${status === 'running' ? 'is-running' : ''}`} data-running={status === 'running'} aria-label={status === 'running' ? `${subject}正在运行` : `${subject}当前未运行`}><span className="command-orbit-track" aria-hidden="true" /><span className="command-orbit-center"><Icon size={23} strokeWidth={1.5} /></span>{status === 'running' && <span className="command-orbit-point" aria-hidden="true" />}</span>;
}

function activityPercentage(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
}

function ActivityProgress({ progress }: { progress: AgentActivityProgress }) {
  const pendingReading = progress.kind === 'reading' && Boolean(progress.state);
  const percent = progress.reported && !pendingReading ? activityPercentage(progress.percent) : null;
  const hasCounts = typeof progress.completed === 'number' && Number.isFinite(progress.completed) && progress.completed >= 0
    && typeof progress.total === 'number' && Number.isFinite(progress.total) && progress.total > 0 && progress.completed <= progress.total;
  const units: Record<string, string> = {characters: '字符', sections: '章节', documents: '份文档', steps: '项', revision_items: '条意见'};
  const label = progress.label || (progress.kind === 'writing' ? '编写进度' : '阅读进度');
  const status = pendingReading ? progress.state === 'calculating' ? '正在统计阅读进度' : '暂无法统计阅读进度'
    : progress.status_label ? progress.status_label : percent === null ? '尚未上报' : `${percent.toFixed(percent % 1 ? 1 : 0)}%`;
  return <div className={`command-activity-progress progress-${progress.kind ?? 'unknown'}`}>
    <div className="command-activity-progress-heading"><span>{label}</span><strong>{status}</strong></div>
    {percent !== null && <div className="command-activity-progress-track" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><span style={{width: `${percent}%`}} /></div>}
    {progress.reported && !pendingReading && hasCounts && <small>{progress.completed_label ?? '已完成'} {progress.completed!.toLocaleString('zh-CN')} / {progress.total!.toLocaleString('zh-CN')} {units[progress.unit ?? ''] ?? ''}</small>}
  </div>;
}

const referenceKindNames: Record<string, string> = {
  paper_rag_source: '论文原文', implementation_specification: '实现规范',
  architecture_design: '架构文档', architecture_scientific_definitions: '方法定义',
  architecture_definition_issue: '定义问题与处理记录', validated_literature_note: '文献笔记',
  approved_proposal: '已批准提案', frozen_experiment_plan: '冻结实验计划',
  original_experiment_plan: '原始实验计划', architecture_host_receipt: '架构核验记录',
  host_implementation_contract: '实现约束',
};
const validReadingCount = (value: number | null | undefined): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const shortCharacters = (value: number) => value >= 10000 ? `${(value / 10000).toFixed(1)} 万字符` : `${value.toLocaleString('zh-CN')} 字符`;

function ReferenceReading({reading}: {reading: NonNullable<AgentCurrentActivity['reading']>}) {
  const reported = reading.reported !== false && !reading.state;
  const total = reading.catalog_chars ?? reading.total;
  const documents = reading.catalog_documents ?? reading.documents_total;
  const hasCatalog = validReadingCount(total) && validReadingCount(documents) && documents > 0;
  return <div className="command-reference-reading" aria-label="按需查阅参考资料">
    <strong>按问题核对代码与相关条款</strong>
    <small>{reported && validReadingCount(reading.completed)
      ? `本次累计${reading.source === 'same_handoff_delivered_receipts' ? '提供' : '取回'} ${reading.completed.toLocaleString('zh-CN')} 字符参考正文`
      : reading.state === 'unavailable' ? '暂无法统计已提供的参考正文' : '正在统计已提供的参考正文'}</small>
    {hasCatalog && <details>
      <summary>可查资料 {documents} 份 · {shortCharacters(total)}</summary>
      <small>目录总量 {total.toLocaleString('zh-CN')} 字符，不等于当前上下文长度。</small>
      {!!reading.catalog_groups?.length && <ul>{reading.catalog_groups.filter(group => validReadingCount(group.documents) && validReadingCount(group.characters)).map(group =>
        <li key={group.kind}><span>{referenceKindNames[group.kind] ?? '其他参考资料'} · {group.documents} 份</span><span>{shortCharacters(group.characters)}</span></li>)}</ul>}
    </details>}
    <small>资料按需查阅，无需逐份全文重读；阅读量不代表审核完成度。</small>
  </div>;
}

export function AgentActivityDetail({ activity }: { activity: NonNullable<ReturnType<typeof agentTaskCard>['activity']> }) {
  const tool = activity.tool;
  const toolLabel = tool?.label === tool?.name && ['experiments.status', 'experiments.analyze', 'experiments.compare', 'experiments.inspect_module'].includes(tool?.name ?? '')
    ? humanizeMessage(tool!.name) : tool?.label;
  const reading = activity.reading;
  const sourceReading = reading && activity.progress?.kind !== 'reading';
  const revisionReading = typeof activity.version === 'number' && activity.version > 1
    && ['document_revision', 'document_saving', 'document_checking'].includes(activity.phase);
  const memory = activity.revision_memory;
  const revision = activity.revision_status;
  const fixedReadingScope = reading?.scope === 'current_task_sources';
  // Older servers exposed the entire code-review directory as a percentage.
  // It is an on-demand catalog, even before they adopt the explicit scope.
  const referenceCatalog = reading?.scope === 'reference_catalog' || activity.phase === 'code_review';
  const deliveredReading = reading?.source === 'same_handoff_delivered_receipts';
  const readingReported = reading?.reported !== false && !reading?.state;
  const location = tool ? [tool.document_title, tool.section_label || tool.section_id].filter(Boolean).join(' · ') : '';
  const range = tool && typeof tool.offset === 'number' && Number.isSafeInteger(tool.offset) && tool.offset >= 0
    && typeof tool.limit === 'number' && Number.isSafeInteger(tool.limit) && tool.limit > 0
    && Number.isSafeInteger(tool.offset + tool.limit)
    ? `${tool.range_kind === 'actual_read' ? '已读' : '请求'}第 ${tool.offset + 1}–${tool.offset + tool.limit} 字符` : '';
  return <div className={`command-task-activity activity-${activity.state}`} aria-label="当前任务执行细节">
    <div className="command-activity-heading"><Activity size={13} /><strong><ReferenceText value={activity.label} /></strong></div>
    {(typeof activity.version === 'number' && activity.version > 0 || typeof activity.round === 'number' && activity.round > 0) && <div className="command-activity-version">{typeof activity.version === 'number' && activity.version > 0 && <span>文档第 {activity.version} 版</span>}{typeof activity.round === 'number' && activity.round > 0 && <span>审核第 {activity.round} 轮</span>}</div>}
    {activity.detail && <p className="command-activity-detail"><ReferenceText value={humanizeMessage(requestRecoveryText(activity.detail))} /></p>}
    {activity.progress && !(referenceCatalog && activity.progress.kind === 'reading') && <ActivityProgress progress={activity.progress} />}
    {revision && <div className="command-activity-detail" aria-label="修订保存与自检状态">
      <p>保存记录覆盖 {revision.handled} / {revision.total} 条意见，不代表整条意见已完成。</p>
      {revision.failed_units > 0 && <p>{revision.failed_units} 个修改位置待重试；已保存修改保留。</p>}
      {revision.self_check.state !== 'not_reported' && <p>作者自检：{revision.self_check.incomplete ?? 0} 条待补齐，{revision.self_check.blocked ?? 0} 条阻塞。此处为作者报告，尚非独立审核结论。</p>}
      {!!revision.self_check.review_pending?.length && <details><summary>待独立复核（{revision.self_check.review_pending.length}），不计入作者阻塞</summary>
        {revision.self_check.review_pending.map(item => <p key={`${item.issue_id}:${item.index}`}><ReferenceText value={item.requirement} />：由{item.owner === 'director' ? '研究总监' : '文献研究员'}在送审后核验，尚未通过。</p>)}
      </details>}
      {revision.self_check.pending.length > 0 && <details><summary>自检待办（{revision.self_check.pending.length}）</summary>
        {revision.self_check.pending.map(item => <p key={`${item.issue_id}:${item.condition_index}`}><ReferenceText value={item.requirement} />：<ReferenceText value={item.reason || '尚未报告完成'} /></p>)}
      </details>}
      {revision.latest_notes.length > 0 && <details><summary>最近修订说明（作者记录）</summary>
        {revision.latest_notes.map((note, i) => <p key={i}><ReferenceText value={note} /></p>)}
      </details>}
    </div>}
    {memory && <p className="command-activity-detail">{typeof memory.current_fields === 'number'
      ? `当前请求已载入 ${memory.current_fields} 个当前稿字段、${memory.selected_notes} 条作者笔记、${(memory.restored_source_chars ?? 0).toLocaleString('zh-CN')} 字符来源正文。`
      : `当前请求仅载入第 ${memory.source_revision} 版的 ${memory.selected_fields} 个字段、${memory.selected_notes} 条作者笔记。`}
      历史阅读索引覆盖 {memory.historical_read_chars.toLocaleString('zh-CN')} 字符，索引不代表正文已全部载入。</p>}
    {sourceReading && revisionReading && <small className="command-reading-documents">本轮按修改意见定向修订；仅在上下文缺失、来源换版或细节需核对时补读，不要求重新通读材料。{readingReported && typeof reading.completed === 'number' && `本轮默认材料已交付 ${reading.completed.toLocaleString('zh-CN')} 字符。`}</small>}
    {reading && referenceCatalog && <ReferenceReading reading={reading} />}
    {sourceReading && !revisionReading && !referenceCatalog && <><ActivityProgress progress={{kind: 'reading', label: fixedReadingScope ? '本轮对照材料阅读进度' : reading.source === 'same_handoff_read_receipts' ? '本次来源阅读进度' : '来源阅读进度', reported: reading.reported !== false, state: reading.state, percent: reading.percent,
      completed: reading.completed, total: reading.total, unit: reading.unit}} />{typeof reading.documents_total === 'number' && reading.documents_total > 0
      && readingReported
      && typeof reading.documents_completed === 'number' && reading.documents_completed >= 0 && reading.documents_completed <= reading.documents_total
      && <small className="command-reading-documents">{deliveredReading ? '正文已交付' : '完整读完'} {reading.documents_completed} / {reading.documents_total} 份{fixedReadingScope ? '默认对照材料' : '来源'}</small>}
      {fixedReadingScope && <small className="command-reading-documents">总量按本轮固定范围统计；工作笔记和按需展开材料不增加总量。{deliveredReading && '进度依据实际进入模型请求的正文范围。'}</small>}
      {fixedReadingScope && typeof reading.optional_documents === 'number' && Number.isSafeInteger(reading.optional_documents) && reading.optional_documents > 0
        && <small className="command-reading-documents">另有 {reading.optional_documents} 份按需参考资料，不要求逐份重读。</small>}</>}
    {tool && <div className={`command-activity-tool tool-${tool.status}`}><span><Wrench size={12} />{activity.toolState}</span><code>{tool.name}</code>
      {toolLabel && <small><ReferenceText value={toolLabel} /></small>}
      {tool.status === 'failed' && (tool.validation_detail || tool.error) && <p className="command-tool-location"><ReferenceText value={tool.validation_detail || tool.error} /></p>}
      {(location || range) && <p className="command-tool-location">{location && <ReferenceText value={location} />}{location && range && <span> · </span>}{range}</p>}
    </div>}
    {activity.updated_at && <small className="command-activity-updated">更新于 {date(activity.updated_at, true)}</small>}
  </div>;
}

export function AgentConversationCard({ agent, selected, onSelect }: { agent: ResearchMapAgent; selected: boolean; onSelect: (role: string, entry?: AgentHistoryEntry) => void }) {
  const card = agentTaskCard(agent);
  const completedReading = !card.activity && card.output && agent.current_activity?.phase === 'code_review'
    && agent.current_activity.handoff_id === agent.current_assignment?.id ? agent.current_activity.reading : null;
  return <article className={`command-agent-card role-${agent.role} ${selected ? 'selected' : ''}`} data-role={agent.role}
    aria-label={`${roles[agent.role]?.name ?? agent.role}协作状态`}
    onClick={event => {
      if (!(event.target as HTMLElement).closest('details')) onSelect(agent.role, card.entry);
    }}>
    <div className="command-agent-top"><AgentOrbit status={agent.status} role={agent.role} /><span className={`command-state state-${agent.status}`}><i />{statusNames[agent.status] ?? humanizeMessage(agent.status)}</span></div>
    <div className="command-agent-name"><h3><ReferenceText value={roles[agent.role]?.name ?? agent.role} /></h3><span>{roles[agent.role]?.short ?? agent.role}</span></div>
    <div className="command-agent-model" title={`${agent.provider} · ${agent.model}`}>{agent.model || '未指定模型'}<small>{agent.provider || '未绑定供应商'}</small></div>
    <div className={`command-speech-bubble ${card.output ? 'has-task-output' : ''} ${card.feedback.fixes.length ? 'has-review-fixes' : ''}`}>
      <span className="command-task-label">{card.title}{(agent.current_assignments_count ?? 0) > 1 && <small> · {agent.current_assignments_count} 项同时进行</small>}</span>
      <strong className="command-task-title" title={card.task}><ReferenceText value={card.task} /></strong>
      {card.activity && <AgentActivityDetail activity={card.activity} />}
      {card.verdict && <span className="command-output-verdict">{card.verdict}</span>}
      {card.summary && <p className="command-output-summary" title={card.summary}><ReferenceText value={card.summary} /></p>}
      {card.feedback.fixes.length > 0 && <div className="command-fix-preview">
        <span>{card.feedback.title} · {card.feedback.total} 项</span>
        <p title={card.feedback.fixes[0].change}><ReferenceText value={card.feedback.fixes[0].change || '查看本次评议中的问题位置与处理要求'} /></p>
      </div>}
      {card.feedback.rejected && !card.feedback.fixes.length && <div className="command-fix-preview"><span>具体处理步骤</span><p>{card.feedback.truncated ? '查看全文中的修改与核查建议' : '此条评议尚未保存具体处理步骤'}</p></div>}
      {completedReading && <details className="command-completed-reading"><summary>参考资料与读取记录</summary><ReferenceReading reading={completedReading} /></details>}
      {card.expected && <p className="command-expected-output"><span>预期产出</span>{card.expected}</p>}
      {card.at && <small className="command-speech-meta">{date(card.at, true)} 交付</small>}
      {card.action && <div className="command-current-action"><Wrench size={11} /><span><ReferenceText value={card.action} /></span></div>}
    </div>
    <button type="button" className="command-agent-bottom" aria-pressed={selected}
      aria-label={`查看${roles[agent.role]?.name ?? agent.role}自己的工作记录`}><span>{card.link}</span><ArrowUpRight size={13} /></button>
  </article>;
}

function CodexConversationCard({ codex, model, onDetails }: { codex: CodexParticipant; model?: string; onDetails?: () => void }) {
  const Tag = onDetails ? 'button' : 'div';
  return <Tag className="command-agent-card role-codex" data-role="codex" onClick={onDetails} aria-label={onDetails ? '查看 Codex 实现与验收详情' : 'Codex 执行状态'}>
    <div className="command-agent-top"><AgentOrbit status={codex.status} role="codex" /><span className={`command-state state-${codex.status}`}><i />{codex.label}</span></div>
    <div className="command-agent-name"><h3>Codex</h3><span>写码 · 修复</span></div>
    <div className="command-agent-model">{model || '本机 Codex 执行器'}<small>独立代码执行</small></div>
    <div className="command-speech-bubble">
      <span className="command-task-label">实现工作流</span><strong className="command-task-title">{codex.phase}</strong>
      <p title={codex.summary}>{codex.summary}</p>
      {codex.files !== undefined && <small className="command-speech-meta">最近记录 {codex.files} 个文件</small>}
      {codex.at && <small className="command-speech-meta">最近活动 {date(codex.at, true)}</small>}
      <p className="command-expected-output"><span>交付去向</span>流程调度接收产物与回执，再组织验收和独立评审。</p>
    </div>
    {onDetails && <div className="command-agent-bottom"><span>查看实现与验收</span><ArrowUpRight size={13} /></div>}
  </Tag>;
}

function exchangePurpose(link: ResearchMapLink) {
  return link.purpose === 'codex_dispatch' ? '派发代码实现' : link.purpose === 'codex_receipt' ? '返回请求回执' : purposeName(link.purpose.split(':')[0]);
}

function exchangeStatus(link: ResearchMapLink) {
  if (link.purpose === 'codex_dispatch' && link.status === 'completed') return '已派发';
  if (link.purpose === 'codex_receipt' && link.status === 'completed') return '已返回';
  return statusNames[link.status] ?? (link.status === 'unknown' ? '待核对' : humanizeMessage(link.status));
}

function ExchangeRecord({ link }: { link: ResearchMapLink }) {
  return <div className={`exchange-${link.status}`}><span><ReferenceText value={roles[link.from_role]?.name ?? link.from_role} /><ArrowRight size={12} /><ReferenceText value={roles[link.to_role]?.name ?? link.to_role} /></span><small>{exchangePurpose(link)} · {exchangeStatus(link)}</small></div>;
}

export function AgentConversation({ agents, links = [], selectedRole, onSelect, codex = codexParticipant(), codexModel, onCodexDetails, runStatus, executionStatus }: {
  agents: ResearchMapAgent[]; links?: ResearchMapLink[]; selectedRole: string; onSelect: (role: string, entry?: AgentHistoryEntry) => void;
  codex?: CodexParticipant; codexModel?: string; onCodexDetails?: () => void;
  runStatus?: string; executionStatus?: CurrentExecution['status'];
}) {
  const dispatching = runStatus === 'running' && agents.length > 0
    && !agents.some(agent => agent.status === 'running') && codex.status !== 'running'
    && executionStatus !== 'running' && executionStatus !== 'awaiting_confirmation';
  const marker = `conversation-arrow-${useId().replace(/[^a-zA-Z0-9]/g, '')}`;
  const { canvas, layout } = useConversationLayout(['harness', 'codex', ...agents.map(agent => agent.role)].join(':'));
  const latestLinks = new Map<string, ResearchMapLink>();
  [...links, ...codexExchanges(codex)].sort((a, b) => a.at.localeCompare(b.at)).forEach(link => { const key = `${link.from_role}:${link.to_role}`; const existing = latestLinks.get(key); if (!existing || !existing.active || link.active) latestLinks.set(key, link); });
  const exchanges = [...latestLinks.values()];
  return <section className="command-discussion" aria-label="多智能体交流画布">
    <div className="command-discussion-caption"><span><span className="command-live-dot" />协作现场</span><p>点击角色查看记录 · 点击 Codex 查看实现</p></div>
    <div className="command-exchange-list command-mobile-exchanges" aria-label="交互方向">{exchanges.map(link => <ExchangeRecord key={link.id} link={link} />)}</div>
    <div className="command-conversation" ref={canvas}>
      <svg className="command-conversation-links" viewBox={`0 0 ${layout.width} ${layout.height}`} data-ready={layout.ready} preserveAspectRatio="none" aria-label="实际智能体交接关系" role="img">
        <defs><marker id={marker} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#a7c6b4" /></marker></defs>
        {exchanges.map(link => {
          const geometry = conversationGeometry(layout.nodes, link.from_role, link.to_role);
          if (!geometry) return null;
          const { path, label } = geometry;
          const active = link.status === 'running' && link.active === true;
          return <g className={`conversation-link link-${link.status} ${active ? 'is-active' : ''}`} key={link.id} data-from-role={link.from_role} data-to-role={link.to_role}>
            <path d={path} markerEnd={`url(#${marker})`} />
            <title>{`${roles[link.from_role]?.name ?? link.from_role} → ${roles[link.to_role]?.name ?? link.to_role}：${exchangePurpose(link)} · ${exchangeStatus(link)}`}</title>
            {(link.from_role === 'harness' || link.purpose === 'codex_receipt') && <text className="conversation-link-label" x={label.x} y={label.y - 12} textAnchor="middle">{exchangePurpose(link)}</text>}
            {active && <><path className="conversation-flow-trail" d={path} pathLength={100} /><circle className="conversation-flow-dot" r="3.5"><animateMotion dur="2.8s" repeatCount="indefinite" path={path} /></circle></>}
          </g>;
        })}
      </svg>
      <div className={`command-dispatcher ${dispatching ? 'is-running' : ''}`} data-role="harness"><strong>流程调度</strong><span>{dispatching ? '正在调度 · 派发与收集' : '派发任务 · 收集回执'}</span><AgentOrbit role="harness" status={dispatching ? 'running' : 'idle'} /></div>
      {agents.map(agent => <AgentConversationCard key={agent.role} agent={agent} selected={selectedRole === agent.role} onSelect={onSelect} />)}
      <CodexConversationCard codex={codex} model={codexModel} onDetails={onCodexDetails} />
    </div>
    <details className="command-exchanges"><summary>交互记录 · {exchanges.length} 条<ChevronDown size={13} /></summary><div className="command-exchange-list">
      {exchanges.length ? exchanges.map(link => <ExchangeRecord key={link.id} link={link} />) : <p>尚未记录任务派发。</p>}
    </div></details>
    <div className="command-discussion-note"><span className="command-connection-sample" />箭头来自已保存的交接与请求回执；流动光点表示正在执行，返回不代表验收通过。</div>
  </section>;
}

function PublicText({ text, truncated, technical = false, initiallyExpanded = false }: { text: string; truncated?: boolean; technical?: boolean; initiallyExpanded?: boolean }) {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const [structuredExpanded, setStructuredExpanded] = useState(false);
  const long = text.length > 640;
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === 'object') {
      const record = parsed as Record<string, unknown>;
      const fields = [['summary', '内容摘要'], ['rationale', '判断依据'], ['explanation', '说明'],
        ['research_question', '研究问题'], ['hypothesis', '研究假设'], ['architecture', '架构说明'],
        ['markdown', '公开报告'], ['title', '标题']];
      const summaries = fields.filter(([key]) => typeof record[key] === 'string' && record[key]);
      return <div className="command-public-text">
        {summaries.length ? summaries.map(([key, label]) => <div key={key}><strong>{label}</strong>{technical
          ? <HumanMessage text={String(record[key])} /> : <p><ReferenceText value={String(record[key])} /></p>}</div>)
          : <p><ReferenceText value={Array.isArray(record.fixes) ? `已保存 ${record.fixes.length} 条修改意见。` : '结构化记录已保存，可展开原文查看字段与完整内容。'} /></p>}
        <details open={structuredExpanded} onToggle={event => setStructuredExpanded(event.currentTarget.open)}><summary>完整结构化记录</summary><pre tabIndex={0}>{text}</pre></details>
        {truncated && <small>此条为截断摘要，刷新历史可读取完整公开内容。</small>}
      </div>;
    }
  } catch { /* Plain public text remains verbatim; technical messages use known labels only. */ }
  if (truncated && /^[\s]*[\[{]/.test(text)) return <div className="command-public-text"><p>此条结构化记录被截断。下方保留已提取的评议，刷新历史可读取完整内容。</p><details><summary>查看截断原文</summary><pre tabIndex={0}>{text}</pre></details></div>;
  if (technical) return <HumanMessage text={text} className="command-public-text" />;
  return <div className="command-public-text"><pre>{expanded || !long ? text : `${text.slice(0, 640)}…`}</pre>{long && <button type="button" className="command-text-toggle" onClick={() => setExpanded(!expanded)}>{expanded ? '收起全文' : `展开全文 · ${text.length.toLocaleString()} 字符`}<ChevronDown size={12} className={expanded ? 'rotated' : ''} /></button>}{truncated && <small>此条为截断摘要，刷新历史可读取完整公开内容。</small>}</div>;
}

export function AgentHistoryPanel({ agent, pages, loading = false, error = '', onRefresh, onLoadOlder, focus }: { agent: ResearchMapAgent; pages?: AgentHistoryPages; loading?: boolean; error?: string; onRefresh?: () => void; onLoadOlder?: () => void; focus?: { entry: AgentHistoryEntry; nonce: number } }) {
  const [filter, setFilter] = useState<string>(focus?.entry.kind ?? 'output');
  const [count, setCount] = useState(12);
  const history = [...(agent.history ?? [])].sort((a, b) => b.at.localeCompare(a.at));
  const { focused, visible, more } = historyDisplay(history, filter, count, focus?.entry);
  const focusedRecord = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!focus) return;
    setFilter(focus.entry.kind); setCount(12);
    const frame = requestAnimationFrame(() => {
      focusedRecord.current?.focus({ preventScroll: true });
      focusedRecord.current?.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    });
    return () => cancelAnimationFrame(frame);
  }, [focus?.nonce]);
  return <section className={`command-history role-${agent.role}`} aria-label={`${roles[agent.role]?.name ?? agent.role}专属公开历史`}><div className="command-history-heading"><span className="command-mini-dot" /><div><h3><ReferenceText value={roles[agent.role]?.name ?? agent.role} /><span>专属工作记录</span></h3><p>该角色的公开输出、决策理由与工具执行摘要</p></div>{onRefresh && <button className="command-icon-button" onClick={onRefresh} disabled={loading} aria-label="刷新此角色最新历史" title="获取最新记录全文，保留已加载的更早记录">{loading ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}</button>}</div><div className="command-selected-task"><div><span>当前任务</span><HumanMessage text={textOf(agent.current_task) || '等待任务分配'} /></div><div><span>当前动作</span><HumanMessage text={textOf(agent.current_action) || '暂无进行中的动作'} /></div></div><div className="command-history-filters" aria-label="筛选角色记录类型">{[['output', '公开输出'], ['tool', '工具执行'], ['assignment', '任务指派'], ['all', '全部']].map(([value, label]) => <button key={value} aria-pressed={filter === value} className={filter === value ? 'selected' : ''} onClick={() => { setFilter(value); setCount(12); }}>{label}<span>{history.filter(item => value === 'all' || item.kind === value).length}</span></button>)}</div>{error && <div className="command-history-error"><p><CircleAlert size={13} />本页历史加载失败，已加载的记录仍然保留。</p><HumanMessage text={error} /></div>}
    {visible.length ? <div className="command-history-records">{visible.map(entry => { const Icon = entry.kind === 'tool' ? Terminal : entry.kind === 'assignment' ? ArrowRight : FileText; return <article key={`${entry.id}:${entry.id === focused?.id ? focus?.nonce : 0}`} ref={entry.id === focused?.id ? focusedRecord : undefined} tabIndex={-1} data-record-id={entry.id} className={`command-history-entry entry-${entry.kind} ${entry.id === focused?.id ? 'is-located' : ''}`}>{entry.id === focused?.id && <div className="command-located-label"><ArrowRight size={14} />已定位到交流卡片对应的{entry.kind === 'output' ? '输出' : '任务'} · {roles[agent.role]?.name ?? agent.role}{entry.truncated && <span>{loading ? '正在读取全文…' : '当前为摘要，可刷新读取全文'}</span>}</div>}<div className="command-entry-meta"><span><Icon size={13} />{kindNames[entry.kind] ?? entry.kind}</span><time>{date(entry.at, true)}</time><span className={`command-state state-${entry.status}`}>{statusNames[entry.status] ?? humanizeMessage(entry.status)}</span></div><h4><ReferenceText value={purposeName(entry.purpose ?? '') || '工作记录'} /></h4>{entry.tool_name && <ToolEventSummary name={entry.tool_name} role={entry.caller_role ?? agent.role} status={entry.status} />}<ReviewFeedback entry={entry} /><PublicText text={textOf(entry.text) || '尚无公开内容'} truncated={entry.truncated} technical={entry.kind !== 'output'} initiallyExpanded={entry.id === focused?.id} /><CitationDiscardNotice audit={entry.citation_discard_audit} />{entry.consensus && <div className="command-consensus-reason"><strong><ShieldCheck size={13} />公开评议{typeof entry.consensus.approve === 'boolean' ? entry.consensus.approve ? ' · 同意' : ' · 有异议' : ''}</strong>{entry.consensus.rationale && <p><ReferenceText value={entry.consensus.rationale} /></p>}{Array.isArray(entry.consensus.required_changes) && entry.consensus.required_changes.length > 0 && <ul>{entry.consensus.required_changes.map((item: unknown, index: number) => <li key={index}><ReferenceText value={item} /></li>)}</ul>}</div>}<details className="command-source"><summary>记录来源<ChevronDown size={11} /></summary><code>{entry.id}</code></details></article>; })}{more > 0 && <button className="command-load-more" onClick={() => setCount(value => value + 20)}>显示更多记录<ArrowDown size={13} /><span>还有 {more} 条</span></button>}</div> : <div className="command-history-empty"><FileText size={23} strokeWidth={1.4} /><p>{loading ? '正在读取公开历史…' : agent.history_loaded === false && !pages?.loaded ? '公开历史尚未加载' : '该角色尚无此类公开记录'}</p><small>点击角色卡片或刷新，按需读取已保存记录。</small></div>}
    <div className="command-history-pagination" aria-live="polite"><span>{pages?.loaded ? `已加载 ${pages.history.length} 条完整记录 · 当前共 ${Math.max(pages.total, agent.history_total ?? 0)} 条` : agent.history_loaded === false ? '公开历史按需读取。' : '当前显示近期摘要，刷新可获取记录全文。'}</span>{pages?.loaded && !pages.hasOlder && <small>已加载至最早记录；可刷新获取最新记录全文。</small>}{pages?.hasOlder && onLoadOlder && <button type="button" className="command-load-more" disabled={loading} onClick={onLoadOlder}>{pages.loading === 'older' ? <LoaderCircle size={13} className="spin" /> : <ArrowDown size={13} />}加载更早记录</button>}{loading && <small>{pages?.loading === 'older' ? '正在加载更早的公开记录…' : '正在读取最新公开记录…'}</small>}</div>
  </section>;
}

export function ResearchRoute({ map, runId, revision = '', refreshRevision = 0 }: { map: CommandMap; runId?: string; revision?: string; refreshRevision?: number }) {
  const [open, setOpen] = useState(false);
  const snapshot = useProjectionSnapshot(open ? runId ?? null : null, revision, refreshRevision);
  const branchRequest = useRemoteResource<RunProjection>(open && runId ? `/runs/${encodeURIComponent(runId)}/views/branches` : null, snapshot.key, 0, true);
  const branchesLoaded = map.branches_loaded !== false || Boolean(branchRequest.data);
  const mainline = Array.isArray(map.mainline) ? map.mainline : [];
  const branches = branchRequest.data?.research_map?.branches ?? (Array.isArray(map.branches) ? map.branches : []);
  const unlinked = branches.filter(branch => !mainline.some(node => node.id === branch.parent_id));
  function branchCard(branch: ResearchMap['branches'][number]) { const Icon = branch.status === 'passed' ? Check : branch.status === 'rejected' ? X : Compass; return <details className={`command-route-branch route-${branch.status}`} key={branch.id}><summary><span className="route-branch-icon"><Icon size={12} /></span><span><small>第 {branch.cycle} 轮 · {humanizeMessage(branch.kind)}</small><strong>{branch.label}</strong></span><span className="route-branch-status">{routeNames[branch.status]}</span><ChevronDown size={12} /></summary><div><HumanMessage text={branch.reason || '此分支尚未记录理由。'} />{branch.source_id && <code>来源 <ReferenceText value={branch.source_id} /></code>}</div></details>; }
  return <section className="command-route" aria-label="研究主线与探索分支">
    <div className="command-section-heading"><div><span>研究路线图</span><h3><Workflow size={18} />研究路线</h3></div><div className="command-route-legend"><span className="route-passed"><i />已通过</span><span className="route-rejected"><i />已否决</span><span className="route-pending"><i />待验证</span>{mainline.some(node => node.status === 'skipped') && <span className="route-skipped"><i />已跳过</span>}</div></div>
    <p className="command-route-intro">沿主线推进，保留被否决与待验证的研究分支。</p>
    {!branchesLoaded && <p className="panel-note">历史研究分支尚未加载。{runId && <button className="btn small" disabled={branchRequest.loading} onClick={() => { setOpen(true); snapshot.refresh(); }}>{branchRequest.loading ? '正在读取历史分支…' : '加载历史研究分支'}</button>}</p>}
    {branchesLoaded && snapshot.stale && <button className="btn small" onClick={snapshot.refresh}>有新记录，刷新历史分支</button>}
    {branchRequest.error && <p role="alert"><ReferenceText value={branchRequest.error} /></p>}
    {mainline.length ? <ol className="command-route-tree">{mainline.map(node => {
      const related = branches.filter(branch => branch.parent_id === node.id);
      const skipped = node.status === 'skipped';
      const current = !skipped && (node.is_current ?? node.id === map.current_stage);
      const activity = node.activity_status ?? (current ? map.run_status : undefined);
      const stateLabel = current ? ({ blocked: '当前阻塞', failed: '当前失败', paused: '当前已暂停', cancelled: '当前已取消' } as Record<string, string>)[activity ?? ''] ?? (node.status === 'pending' ? '当前检查点' : routeNames[node.status]) : routeNames[node.status];
      const reason = node.reason || (skipped ? node.id === 'final_tests' ? '未执行独立测试；本轮依据验证阶段停止策略直接整理报告。' : '此阶段已跳过，未执行。' : current && ['blocked', 'failed'].includes(activity ?? '') ? map.run_error : current && activity === 'paused' ? '研究已暂停，恢复后从当前检查点继续。' : undefined);
      return <li className={`command-route-row route-${node.status} ${current ? 'is-current-node' : ''}`} key={node.id}>
        <div className="command-main-node"><span className="route-node-dot">{skipped ? <SkipForward size={11} /> : node.status === 'passed' ? <Check size={11} /> : node.status === 'rejected' ? <X size={11} /> : node.status === 'active' ? <Activity size={11} /> : <span />}</span><div><strong title={node.label}>{humanizeMessage(node.label)}</strong><small>{stateLabel}</small></div></div>
        <div className={`command-route-branches ${related.length ? 'has-branches' : ''} ${reason ? 'has-reason' : ''}`}>{reason && <div className="command-node-reason">{skipped ? <SkipForward size={13} /> : <CircleAlert size={13} />}<HumanMessage text={isNativeCodexGate(reason) ? requestRecoveryText(reason) : reason} /></div>}{related.length ? related.map(branchCard) : !reason && <span className="command-no-branch">{current ? node.status === 'active' ? '当前推进位置' : '当前检查点，查看上方下一步' : node.status === 'pending' ? '等待前序阶段完成' : !branchesLoaded ? '历史分支尚未加载' : '暂无关联分支记录'}</span>}</div>
      </li>;
    })}</ol> : <p className="command-empty-route">研究路线尚未生成。</p>}
    {unlinked.length > 0 && <div className="command-unlinked"><h4>尚未关联主线阶段的分支</h4>{unlinked.map(branchCard)}</div>}<p className="command-route-source"><GitBranch size={12} />节点与分支来自本任务的交接、评议和执行记录。</p>
  </section>;
}

export function ResearchCommandCenter({ map: sourceMap, datasetAdaptation = false, runId, definitionReview, implementationProgress, codexModel, onCodexDetails, revision = '', refreshRevision = 0 }: { map?: CommandMap; datasetAdaptation?: boolean; runId: string; definitionReview?: Json | null; implementationProgress?: Json | null; codexModel?: string; onCodexDetails?: () => void; revision?: string; refreshRevision?: number }) {
  const stageNames: Record<string, string> = { design: '架构设计', implementation: '代码实现' };
  const displayStage = optimizationDisplayStage(sourceMap?.current_stage ?? '', sourceMap?.optimization_loop);
  const recovered = sourceMap && displayStage !== sourceMap.current_stage;
  const displayMap = recovered ? { ...sourceMap, current_stage: displayStage,
    current_stage_label: '架构评估与微调', next_action: sourceMap.run_error || '继续当前架构候选的独立审核与 CPU 检查',
    mainline: sourceMap.mainline?.map(node => ['architecture_tuning', 'tuning'].includes(node.id) ? { ...node,
      is_current: node.id === displayStage, status: node.id === displayStage && sourceMap.run_status === 'running' ? 'active' as const : 'pending' as const,
      activity_status: node.id === displayStage ? sourceMap.run_status : null } : node),
  } : sourceMap;
  const map = datasetAdaptation && displayMap ? { ...displayMap,
    current_stage_label: stageNames[displayMap.current_stage] ?? displayMap.current_stage_label,
    mainline: displayMap.mainline?.map(node => ({ ...node, label: stageNames[node.id] ?? node.label })),
  } : displayMap;
  const [selectedRole, setSelectedRole] = useState('');
  const [memoryOpen, setMemoryOpen] = useState(true);
  const [focus, setFocus] = useState<{ runId: string; role: string; entry: AgentHistoryEntry; nonce: number }>();
  const focusSequence = useRef(0);
  const historyLoader = useRef(createAgentHistoryLoader());
  const [, historyChanged] = useState(0);
  historyLoader.current.setRun(runId);
  useEffect(() => () => historyLoader.current.cancelPending(), []);
  const historyPanel = useRef<HTMLDivElement>(null);
  const discussion = useRef<HTMLDivElement>(null);
  function scrollTo(target: HTMLDivElement | null) { target?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' }); }
  function selectAgent(role: string, entry?: AgentHistoryEntry) {
    historyLoader.current.cancelPending();
    setSelectedRole(role);
    setFocus(entry ? { runId, role, entry, nonce: ++focusSequence.current } : undefined);
    void loadHistory(role);
  }
  const agents = (Array.isArray(map?.agents) ? map.agents : []).map(agent => ({ ...agent, history: mergeAgentHistory(agent.history ?? [], historyLoader.current.get(agent.role)?.history ?? []) }));
  const activeRole = agents.some(agent => agent.role === selectedRole) ? selectedRole : map?.projection === 'summary' ? '' : agents.find(agent => agent.status === 'running')?.role ?? agents[0]?.role ?? '';
  async function loadHistory(role: string, mode: 'refresh' | 'older' = 'refresh') {
    await historyLoader.current.load(role, mode, () => historyChanged(value => value + 1));
  }
  useEffect(() => { if (activeRole && !historyLoader.current.get(activeRole)?.loaded) void loadHistory(activeRole); }, [activeRole, runId]);
  const focusedRequest = focus?.runId === runId && focus.role === activeRole ? focus : undefined;
  const activePages = historyLoader.current.get(activeRole);
  useEffect(() => {
    if (!focusedRequest || !activePages?.loaded || activePages.loading || activePages.error) return;
    if (!activePages.history.some(entry => entry.id === focusedRequest.entry.id && !entry.truncated) && activePages.hasOlder) void loadHistory(activeRole, 'older');
  }, [focusedRequest, activePages, activeRole]);
  if (!map) return <div className="research-command-center command-center-empty"><Compass size={33} strokeWidth={1.2} /><h3>研究指挥台，等待接入记录</h3><p>任务开始后，将展示各角色工作、公开交付、研究主线和探索分支。</p></div>;
  const chosenAgent = agents.find(agent => agent.role === activeRole);
  const pages = historyLoader.current.get(activeRole);
  const progress = Math.min(100, Math.max(0, number(map.progress)));
  const stage = map.current_stage_label ?? map.mainline?.find(node => node.id === map.current_stage)?.label ?? map.current_stage;
  let nextAction = map.next_action || '等待新的执行记录';
  if (map.current_stage === 'design' && definitionReview) {
    const d = definitionReview;
    if (d.status === 'prepared' || d.status === 'drafting') {
      if (d.authoring_protocol === 2) {
        const unresolved = Math.max(number(d.unresolved_issue_count),
          d.issue_documents?.filter((unit: Json) => unit.status === 'unresolved').length ?? 0);
        const scopeProblems = (d.scope_issue_count ?? d.scope_issues?.length ?? 0) > 0;
        const writing = d.status === 'drafting' && map.run_status === 'running';
        const gate = unresolved > 0
          ? `，已有 ${unresolved} 项未解决；完成其余定义并处理未决项后，再收集三方同版本独立意见。`
          : scopeProblems ? '；已记录的范围问题仍须处理，处理后再收集三方同版本独立意见。'
            : '；全部候选合成后收集三方同版本独立意见。';
        nextAction = `${writing ? '正在编写第' : '下一项为第'} ${d.active_issue_number ?? 1}/${d.issue_count} 项定义，已保存 ${d.saved_issue_count ?? 0} 项候选${gate}`;
      } else nextAction = '提交具体科学定义 Markdown，再收集三方对同一版本的独立意见。';
    }
    else if (d.status === 'reviewing') nextAction = `${d.review_protocol === 3 ? `分组意见已保存 ${d.saved_review_units ?? 0}/${d.expected_review_units ?? 0} 份；三方终审` : '具体定义已收到'} ${d.received_votes ?? 0}/${d.expected_votes ?? 3} 份${d.review_protocol === 3 ? '终审意见' : '独立意见'}；全部同意且无必改问题后继续新架构。`;
    else if (d.completion?.delivery_recovery?.pending) nextAction = '处理本版已保存终审的交付校验，继续收集剩余角色意见。';
    else if (d.status === 'blocked' || d.status === 'rejected') nextAction = `处理具体定义的未决项或必改意见：${d.reason ?? '查看上方定义文档与意见。'}`;
  }
  return <div className="research-command-center"><header className="command-header"><div className="command-eyebrow"><span /><span>研究指挥</span><span>{map.run_status ? overallStatusNames[map.run_status] ?? humanizeMessage(map.run_status) : '多智能体协作'}</span></div><div className="command-title-row"><div><h2>研究指挥台<span>从共同想法，到可验证的证据。</span></h2><p><ReferenceText value={humanizeMessage(stage || '等待开始')} /><span> / </span>第 {number(map.current_cycle, 1)} 轮研究</p></div><div className="command-progress"><strong>{progress.toFixed(progress % 1 ? 1 : 0)}<small>%</small></strong><span>当前流程进度</span></div></div><div className="command-progress-track" role="progressbar" aria-label="当前流程阶段进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><span style={{ width: `${progress}%` }} /></div><div className="command-next"><span><ArrowRight size={13} />下一步</span><HumanMessage text={isNativeCodexGate(nextAction) ? requestRecoveryText(nextAction) : nextAction} /></div><ResearchOutcome outcome={map.outcome} /><ExecutionActivity execution={map.current_execution} /></header>
    <div ref={discussion}><AgentConversation agents={agents} links={map.links} selectedRole={activeRole} onSelect={selectAgent} codex={codexParticipant(implementationProgress, map.current_execution?.kind === 'local_codex' ? map.current_execution : null, map.run_status)} codexModel={codexModel} onCodexDetails={onCodexDetails} runStatus={map.run_status} executionStatus={map.current_execution?.status} /></div>
    {chosenAgent && <div className={`command-memory-workspace ${memoryOpen ? '' : 'memory-is-collapsed'}`}>{memoryOpen && <AgentMemoryTree runId={runId} role={chosenAgent.role} onRoleChange={role => selectAgent(role)} onCollapse={() => setMemoryOpen(false)} />}<div ref={historyPanel} className="command-history-slot">{!memoryOpen && <button type="button" className="btn small command-memory-reopen" onClick={() => setMemoryOpen(true)}><GitBranch size={14} />展开四层记忆树</button>}<button className="command-return" onClick={() => scrollTo(discussion.current)}><ArrowUpRight size={13} />返回讨论画布</button><AgentHistoryPanel key={JSON.stringify([runId, chosenAgent.role])} agent={chosenAgent} focus={focusedRequest} pages={pages} loading={Boolean(pages?.loading)} error={pages?.error ?? ''} onRefresh={() => void loadHistory(chosenAgent.role)} onLoadOlder={() => void loadHistory(chosenAgent.role, 'older')} /></div></div>}
    <ResearchRoute map={map} runId={runId} revision={revision} refreshRevision={refreshRevision} /><div className="command-footer"><ShieldCheck size={14} /><span>公开输出与决策依据可追溯 · 流程进度不代表实验达标概率</span></div></div>;
}
