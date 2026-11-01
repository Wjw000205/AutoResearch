import { ReferenceText, ReferenceNames } from './ReferenceNames';
import { ValidationHoldFacts, validationResumeReason } from './ValidationHold';
import { useState } from 'react';
import { HumanMessage, humanizeMessage, PROCESS_RESTARTED_REASON, PROCESS_RESTARTED_DESCRIPTION } from './localization';
import { ArrowRight, ArrowUpRight, BookOpen, Check, ChevronDown, CircleAlert, Code2, ExternalLink, FileText, GitBranch, MessageSquareText, Network, ShieldCheck } from 'lucide-react';
import { date, number, safeUrl, textOf, type Json, type Run, type DiscussionProgressRecord } from './api';
import { isContextPreflightGate, CONTEXT_PREFLIGHT_DESCRIPTION, CONTEXT_PREFLIGHT_TITLE, isNormalDispatchGate, isDiscussionStalled, isConstructiveCitationFailure, CONSTRUCTIVE_CITATION_DESCRIPTION, CONSTRUCTIVE_CITATION_TITLE, DISCUSSION_STALLED_DESCRIPTION, DISCUSSION_STALLED_TITLE, REQUEST_RECOVERY_DESCRIPTION, REQUEST_RECOVERY_TITLE } from './RequestRecoveryDiagnostic';
import { isLiteratureReadingGate, LITERATURE_READING_TITLE, READ_TIMEOUT_LIMIT_NOTE, isNoveltyEvidenceGate, NOVELTY_EVIDENCE_TITLE, NOVELTY_EVIDENCE_DESCRIPTION } from './RequestRecoveryDiagnostic';
import { isGatewayTimeoutGate, GATEWAY_TIMEOUT_TITLE, GATEWAY_TIMEOUT_NEXT_ACTION } from './RequestRecoveryDiagnostic';
import { isConstructiveContextFailure, CONSTRUCTIVE_CONTEXT_TITLE, CONSTRUCTIVE_CONTEXT_DESCRIPTION, isContextCapacityGate } from './RequestRecoveryDiagnostic';
import { isDiscussionEvidenceGate, DISCUSSION_EVIDENCE_TITLE, DISCUSSION_EVIDENCE_DESCRIPTION } from './RequestRecoveryDiagnostic';
import { isNativeCodexGate, nativeReasoningDiagnostic, jsonContractDiagnostic } from './RequestRecoveryDiagnostic';
import { ARCHITECTURE_EVIDENCE_REPEAT_GATE, ARCHITECTURE_EVIDENCE_REPEAT_TITLE, ARCHITECTURE_EVIDENCE_REPEAT_DESCRIPTION } from './RequestRecoveryDiagnostic';
import { discussionMode, auditedDiscussionProgress } from './ExecutionControls';
import { LiteratureRounds } from './LiteratureRounds';
import { LiteratureCoverage } from './LiteratureCoverage';
import { LiteratureNotes, LiteratureReadingProgress } from './LiteratureNotes';
import { RoleAmendments } from './RoleAmendments';
import { CitationDiscardNotice } from './CitationDiscardNotice';
import { ProblemReview, ProposalProvenance, type ProposalSourceProps } from './ProposalProvenance';
import type { LiteratureDetail } from './api';

const roles: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员', harness: '统一执行控制器', system: '系统', user: '用户' };
const stages: Record<string, string> = { related_work: 'idea相关论文分析', related_work_queries: '围绕idea制定检索式', innovation: '创新点讨论', innovation_candidate: '独立提出创新点', innovation_compare: '比较并收束创新点', intake: '需求与资源', literature: '文献发现', debate: '想法共识', design: '架构设计', implementation: '代码实现', experiments: '实验验证', tuning: '参数微调', architecture_tuning: '架构微调', validation_review: '验证评审', revision: '方案修订', final_tests: '独立测试', report: '结果报告' };
const roleName = (role: unknown) => roles[String(role)] ?? humanizeMessage(textOf(role));
const stringList = (value: unknown) => Array.isArray(value) ? value.map(textOf).filter(Boolean) : typeof value === 'string' && value ? [value] : [];

export function RecoveryNotice({ run, handoffs, literature, contextReferenceCheck, innovationPending = false, openLimits, openConsensus, openEvidence, openArchitecture, openImplementation, openValidation }: { run: Run; contextReferenceCheck?: Json | null; innovationPending?: boolean; handoffs?: Json[]; literature?: LiteratureDetail | null; openLimits: () => void; openConsensus: () => void; openEvidence?: () => void; openArchitecture?: () => void; openImplementation?: () => void; openValidation?: () => void }) {
  if (!['blocked', 'paused', 'failed'].includes(run.status)) return null;
  const message = (run.error ?? '').toLowerCase();
  const contractDiagnostic = jsonContractDiagnostic(run.error ?? '');
  let title = run.status === 'paused' ? '任务已暂停，检查点已保留' : run.status === 'failed' ? '执行失败' : '研究被执行门禁阻塞';
  let description = run.status === 'failed' ? '查看下方具体错误和执行记录，修复失败原因后再恢复。已保存记录会保留。' : '检查下方执行记录，处理阻塞原因后恢复。已完成阶段和已有实验会保留。';
  let action: 'limits' | 'consensus' | 'providers' | 'evidence' | 'architecture' | 'implementation' | 'validation' | null = null;
  if (run.error === PROCESS_RESTARTED_REASON) {
    title = '后端已重启，任务等待恢复'; description = PROCESS_RESTARTED_DESCRIPTION;
  }
  else if (validationResumeReason(run) || /^验证结果与停止规则待核对：/.test(message)
    || message.includes('the frozen stop policy forbids automatically revising a reported negative validation')) {
    title = '验证结果待核对，自动修订已停止';
    description = '已有验收记录报告负结果，后续修订受已批准的停止规则约束。请查看“验证评审”中的结果与三方意见，核对验收证据后收尾。已完成阶段和实验均已保留。';
    action = openValidation ? 'validation' : null;
  }
  else if (/^ProcessLookupError:\s*$/.test(run.error ?? '')) {
    title = '子进程退出后的清理失败';
    description = '程序清理子进程时，该进程已退出；这个异常可能遮住先前的连接或执行错误。请核对停止前的请求记录，确认原请求状态后再恢复。下方保留的是当时的原始停止记录。';
  }
  else if (/实现文档缺少必要定义/.test(message)) {
    title = '实现文档有待澄清的条款';
    description = '已保存对应的文档位置和原审查问题。需要由文档作者定向补齐并独立审核，随后才能交给 Codex 修改。已有代码、测试回执和已完成阶段保留。';
    action = openImplementation ? 'implementation' : null;
  }
  else if (/宿主有界测试已有真实回执/.test(message)) {
    title = '代码检查仍有未完成项';
    description = '查看宿主测试的实际结果和未执行用例。相同代码的已有检查不会因恢复而重跑；后续按剩余检查额度和原实现文档处理。';
    action = openImplementation ? 'implementation' : null;
  }
  else if (/\btiming_coverage_insufficient\b/.test(message)) {
    title = '资源估算依据不完整';
    description = '代码缺少部分计算步骤的耗时依据，需要按实现文档补齐估算并复审。当前记录不能证明硬件不足或运行超时，增加执行额度也不会补齐这些依据。已完成阶段和原始失败记录保留。';
    action = openImplementation ? 'implementation' : null;
  }
  else if (run.stage === 'design' && run.error === ARCHITECTURE_EVIDENCE_REPEAT_GATE) {
    title = ARCHITECTURE_EVIDENCE_REPEAT_TITLE; description = ARCHITECTURE_EVIDENCE_REPEAT_DESCRIPTION;
    action = openArchitecture ? 'architecture' : null;
  }
  else if (isContextPreflightGate(run.error ?? '')) {
    title = CONTEXT_PREFLIGHT_TITLE; description = CONTEXT_PREFLIGHT_DESCRIPTION; action = 'limits';
    if (run.stage === 'literature' && handoffs?.some(item => item.run_id === run.id && item.to_role === 'scout'
      && item.status === 'blocked' && isContextPreflightGate(item.error ?? '')
      && typeof item.purpose === 'string' && ['literature', 'literature_reading'].includes(item.purpose.split(':')[0]))) {
      description = '任务已分派给文献研究员，但本次模型请求在发送前的本地上下文预检失败，尚未调用模型。' + description;
    }
  }
  else if (isLiteratureReadingGate(run.error ?? '')) {
    title = literature?.reading_gate?.approved === true ? '当前阅读门禁检查已通过' : LITERATURE_READING_TITLE;
    description = literature?.reading_gate?.approved === true
      ? '上次停止原因仍保留供追溯；请核对任务状态及其他执行条件，研究不会自动恢复。'
      : run.config?.pdf_tools_protocol === 1
        ? '本地 PDF 阅读工具已启用。点击恢复执行后将按具体页补读，并通过片段 ID 保存原文引用；已有回复保留，补读仍受预算与调用上限约束。'
        : '请先核对下方具体缺项，并在研究证据中查看待补笔记与跳过原因。';
    action = openEvidence ? 'evidence' : null;
  }
  else if (isNoveltyEvidenceGate(run.error ?? '')) {
    title = NOVELTY_EVIDENCE_TITLE; description = NOVELTY_EVIDENCE_DESCRIPTION;
    action = openEvidence ? 'evidence' : null;
  }
  else if (isDiscussionEvidenceGate(run.error ?? '')) { title = DISCUSSION_EVIDENCE_TITLE; description = DISCUSSION_EVIDENCE_DESCRIPTION; action = 'consensus'; }
  else if (isNativeCodexGate(run.error ?? '')) { ({ title, description } = nativeReasoningDiagnostic(run)); }
  else if (isNormalDispatchGate(run.error ?? '')) { title = REQUEST_RECOVERY_TITLE; description = REQUEST_RECOVERY_DESCRIPTION; }
  else if (isConstructiveCitationFailure(run.error ?? '')) { title = CONSTRUCTIVE_CITATION_TITLE; description = CONSTRUCTIVE_CITATION_DESCRIPTION; action = 'consensus'; }
  else if (isConstructiveContextFailure(run.error ?? '')) { title = contextReferenceCheck?.status === 'ready' ? '上下文引用已通过复核，可以恢复执行' : CONSTRUCTIVE_CONTEXT_TITLE; description = contextReferenceCheck?.status === 'ready' ? (innovationPending ? '已保存回复中的上下文引用已通过复核，历史记录保留。恢复后会先进行新增的创新点讨论，再评审选定方案；新阶段会调用模型，并沿用现有预算与调用上限。' : '已保存评审回复中的上下文引用已通过当前校验。点击“恢复执行”继续；系统复用已有回复，其他评审与执行条件仍会单独检查。') : CONSTRUCTIVE_CONTEXT_DESCRIPTION; action = 'consensus'; }
  else if (isDiscussionStalled(run.error ?? '')) { title = DISCUSSION_STALLED_TITLE; description = DISCUSSION_STALLED_DESCRIPTION; action = 'consensus'; }
  else if (isGatewayTimeoutGate(run.error ?? '')) { title = GATEWAY_TIMEOUT_TITLE; description = GATEWAY_TIMEOUT_NEXT_ACTION; }
  else if (/decision cites missing evidence|decision cites workflow context/.test(message)) { title = '提案证据引用待修复'; description = '提案引用与绑定的证据快照不一致。请核对原始诊断和交接材料；此错误不代表讨论已达到停滞轮数。'; action = null; }
  else if (contractDiagnostic) { ({ title, description } = contractDiagnostic); }
  else if (/^\S+ network request failed \(readtimeout\)$/.test(message)) { title = '模型接口等待响应超时'; description = '本次模型请求未在读取超时内收到响应数据。请检查供应商状态与请求记录；可在“智能体与设置”调整该供应商的“模型响应读取超时”。' + READ_TIMEOUT_LIMIT_NOTE; action = 'providers'; }
  else if (/^\S+ network request failed \((connect|write|pool)timeout\)$/.test(message)) { title = '模型接口连接或传输超时'; description = '模型网络请求未能按时完成连接或数据传输。请核对供应商 API 地址、网络连接与请求记录，再决定是否恢复。'; action = 'providers'; }
  else if (/^provider \S+ circuit is cooling down; (retry after \d+s|batch not started)$/.test(message)) { title = '模型请求因接口冷却暂停'; description = '该接口连续失败后进入短暂冷却。本次请求未启动；冷却结束后，核对供应商状态与请求记录再恢复。'; action = 'providers'; }
  else if (isContextCapacityGate(run.error ?? '')) { title = '必要证据超过当前上下文限制'; description = '查看各角色上下文使用量与压缩快照。可调整上下文预算；完整原始记录仍然保留，模型自身的上下文窗口上限继续生效。'; action = 'limits'; }
  else if (/实验数据配置\/接口检查待修复|实验配置证据待补齐/.test(run.error ?? '')) { title = '实验配置待修复'; description = '查看配置缺项和接口证据，在当前实验阶段继续补齐。已有模型、配置草稿和读取记录保留。'; action = null; }
  else if (/当前数据集的实验次数预算已用完/.test(run.error ?? '')) { title = '当前数据集实验次数已用完'; description = '查看本数据集的消耗和停止结论。其他数据集的独立额度保留，任务费用与模型调用额度另行计算。'; action = null; }
  else if (/research.?cycle|max_research_cycles|research iteration/.test(message)) { title = '已到达本轮科研迭代边界'; description = '查看流程中的验证结果与各角色评审。仍需改进时，可取消科研迭代次数上限后恢复；已有方案、实验和修订记录会保留。'; action = 'limits'; }
  else if (/budget|reservation|cost|calls|call limit/.test(message)) { title = '当前预算或调用额度不足'; description = '本轮请求未能通过费用准入。可以查看计费明细，再调整本任务预算或调用次数上限；保存配置后由你选择是否恢复。'; action = 'limits'; }
  else if (/consensus|unanimous|debate|stagnat|\brounds?\b/.test(message)) { title = '尚未形成可执行的共同结论'; description = discussionMode(run.config ?? {}) === 'until_stalled' ? `查看各角色的异议、必要修改和当前记录。当前讨论不设固定轮数上限；连续 ${run.config.stagnation_rounds ?? 3} 轮无进展时暂停，保留记录供复核。` : '查看各角色的反对理由和必要修改，了解哪些条件仍未满足。达到轮数或重复提案限制后，系统会停止继续循环。'; action = 'consensus'; }
  else if (/permission|allow_local|authoriz|local execution|consent/.test(message)) { title = '实验执行需要本地授权'; description = '在执行配置中选择运行环境并明确允许执行生成代码，然后再恢复任务。'; action = 'limits'; }
  else if (/memory|vram|hardware|resource|disk|experiment.*timeout|trial.*timeout|experiment_timeout/.test(message) || (message === 'timeout' && ['experiments', 'tuning', 'final_tests'].includes(run.stage))) { title = '实验触及资源或时间限制'; description = '查看资源检查结果与实验日志。超时限制可在执行配置中调整；硬件不满足的方案仍会被门禁阻止。'; action = 'limits'; }
  if (run.status === 'failed' && title !== '执行失败') title = `执行失败 · ${title}`;
  return <div className={`recovery-notice${run.validation_hold ? " has-validation-hold" : ""}`}><CircleAlert size={20} /><div><h3><ReferenceText value={title} /></h3>{isLiteratureReadingGate(run.error ?? '') && <LiteratureReadingProgress readingPlan={literature?.reading_plan} readingGate={literature?.reading_gate} />}<p id={validationResumeReason(run) ? "validation-resume-reason" : undefined}><ReferenceText value={description} /></p><ValidationHoldFacts hold={run.validation_hold} />{run.error && <details className="recovery-original"><summary>查看原始停止记录<ChevronDown size={12} /></summary><pre tabIndex={0}><ReferenceText value={run.error} /></pre></details>}</div>{action === 'providers' ? <a className="btn small" href="#/settings">模型连接与超时<ArrowRight size={14} /></a> : action && <button className="btn small" onClick={action === 'limits' ? openLimits : action === 'evidence' ? openEvidence : action === 'architecture' ? openArchitecture : action === 'implementation' ? openImplementation : action === 'validation' ? openValidation : openConsensus}>{action === 'limits' ? '调整执行边界' : action === 'evidence' ? '查看阅读记录与缺项' : action === 'architecture' ? '查看架构文档与评议' : action === 'implementation' ? '查看代码与修改意见' : action === 'validation' ? '查看验证评审' : '查看异议'}<ArrowRight size={14} /></button>}</div>;
}

function Proposal({ proposal, proposalHash, ...sources }: { proposal: Json | string | null; proposalHash?: string } & ProposalSourceProps) {
  if (!proposal || typeof proposal !== 'object') return <><div className="proposal-text"><ReferenceText value={textOf(proposal) || '提案尚未生成'} /></div>{proposal && <ProposalProvenance proposal={{}} {...sources} />}</>;
  const fields: [string, string][] = [['hypothesis', '可证伪假设'], ['method', '方法与实验'], ['baseline', '比较基线'], ['data_plan', '数据划分'], ['novelty', '创新定位']];
  return <div className="proposal-structured">{proposal.title && <h3><ReferenceText value={proposal.title} /></h3>}{fields.filter(([key]) => proposal[key]).map(([key, label]) => <div className="proposal-field" key={key}><span>{label}</span><p><ReferenceText value={proposal[key]} /></p></div>)}{stringList(proposal.risks).length > 0 && <div className="proposal-field"><span>风险与反例</span><ul>{stringList(proposal.risks).map((item, index) => <li key={index}><ReferenceText value={item} /></li>)}</ul></div>}{stringList(proposal.references).length > 0 && <div className="proposal-field"><span>引用证据 ID</span><div className="evidence-id-list">{stringList(proposal.references).map((item, index) => <code key={index}><ReferenceText value={item} /></code>)}</div></div>}<ProposalProvenance proposal={proposal} proposalHash={proposalHash} {...sources} /><details><summary>完整提案与资源计划<ChevronDown size={13} /></summary><pre><ReferenceText value={proposal} /></pre></details></div>;
}

export function CurrentDebatePanel({ debate, ...sources }: { debate: Json } & ProposalSourceProps) {
  const statusLabels: Record<string, string> = { collecting: '正在收集投票', awaiting_commit: '等待完整共识提交', blocked: '已阻塞', interrupted: '已中断', paused: '已暂停', cancelled: '已取消', failed: '失败', pending: '等待投票', queued: '等待接收', running: '正在处理', completed: '已返回' };
  const preparing = debate.phase === 'preparing_proposal';
  const votes: Json[] = Array.isArray(debate.votes) ? debate.votes : [];
  const states: Json[] = Array.isArray(debate.role_states) ? debate.role_states : [];
  return <section className="current-debate-panel" aria-label="当前未完成评议"><header><MessageSquareText size={18} /><div><h3><ReferenceText value={preparing ? '下一版本提案准备中' : `当前评议 · 第 ${number(debate.round)} 轮`} /></h3><p>第 {number(debate.cycle, 1)} 轮研究 · <ReferenceText value={statusLabels[debate.status] ?? humanizeMessage(textOf(debate.status))} /></p></div></header><HumanMessage text={textOf(debate.reason)} className="current-debate-note" />{preparing ? <p>尚无新版本投票；下方最近已完成评议仅供追溯。</p> : <><div className="current-debate-summary"><strong>已收到 {votes.length} / 3 票</strong><span>尚无完整共识结论</span></div>{debate.proposal_hash && <span>当前提案：<ReferenceText value={debate.proposal?.title || '当前研究提案'} /></span>}<details className="current-debate-proposal"><summary>当前冻结提案<ChevronDown size={13} /></summary><Proposal proposal={debate.proposal} proposalHash={debate.proposal_hash} {...sources} /></details><div className="current-debate-roles">{['director', 'engineer', 'scout'].map(role => { const vote = votes.find(item => item.role === role); const state = states.find(item => item.role === role); return <article key={role} className={vote ? 'has-vote' : 'awaiting-vote'}><div><strong>{roleName(role)}</strong><span><ReferenceText value={vote ? vote.approve === true ? '已返回 · 同意' : '已返回 · 有异议' : statusLabels[state?.status ?? 'pending'] ?? humanizeMessage(textOf(state?.status))} /></span></div>{vote ? <><p><ReferenceText value={vote.rationale} /></p>{stringList(vote.required_changes).length > 0 && <ol>{stringList(vote.required_changes).map((change, index) => <li key={index}>{change}</li>)}</ol>}<ProblemReview review={vote.problem_review} {...sources} /><RoleAmendments vote={vote} amendments={Array.isArray(debate.amendments) ? debate.amendments : []} /></> : <p>尚无匹配当前提案版本的有效投票；不计为反对或同意。</p>}{state?.error && <HumanMessage text={textOf(state.error)} className="current-vote-error" />}</article>; })}</div></>}</section>;
}

export function ConsensusView({ consensus, currentDebate, ...sources }: { consensus?: Json; currentDebate?: Json | null } & ProposalSourceProps) {
  const hasCompleted = consensus && (consensus.proposal || consensus.votes?.length || consensus.history?.length);
  return <ReferenceNames records={[consensus, currentDebate, sources]}><>{currentDebate && <CurrentDebatePanel debate={currentDebate} {...sources} />}{(!currentDebate || hasCompleted) && <CommittedConsensus consensus={consensus} {...sources} />}</></ReferenceNames>;
}

function discussionReason(reason: string | undefined) {
  const known: Record<string, string> = {
    'Initial proposal establishes the comparison point.': '首轮提案作为后续讨论的比较基准。',
    'Returned to an already rejected proposal with the same evidence.': '提案与证据回到此前被否决的状态，检测到重复循环。',
    'Independent reviewers identified grounded progress or resolved an approval blocker.': '独立评议确认了有依据的进展，或原有的同意障碍得到解决。',
    'Legacy review has no progress assessment; new rounds will use the progress protocol.': '历史评议未保存进展判断，后续新轮次开始记录。',
  };
  const match = reason?.match(/^(\d+) consecutive rounds without supported material progress\.$/);
  return match ? `连续 ${match[1]} 轮未获得有依据的实质进展判断。` : known[reason ?? ''] ?? '查看下方原始记录与各角色判断，核对本轮进展依据。';
}

export function DiscussionProgressDetails({ progress, votes = [] }: { progress?: DiscussionProgressRecord; votes?: Json[] }) {
  if (!progress) return null;
  const fields: Record<string, string> = { hypothesis: '研究假设', novelty: '创新定位', method: '研究方法', baseline: '比较基线', metrics: '评估指标', risks: '风险', resource_plan: '资源计划', data_plan: '数据计划', references: '引用资料', publication_gap: '投稿目标差距', idea_rationale: '想法依据', paper_story: '论文故事线', novelty_review: '相近工作与查重' };
  const streak = progress.consecutive_no_progress;
  const measured = typeof streak === 'number' && Number.isInteger(streak) && streak >= 0;
  const reviewers = votes.filter(vote => vote.progress && typeof vote.progress === 'object');
  return <details className="discussion-progress-details"><summary><MessageSquareText size={14} />讨论进展依据{measured && ` · 连续无进展 ${streak}/${progress.stagnation_window ?? '未记录'}`}<ChevronDown size={13} /></summary><p><ReferenceText value={discussionReason(progress.reason)} /></p><dl><div><dt>计入进展的评议角色</dt><dd>{stringList(progress.progress_roles).map(roleName).join('、') || '本轮未记录'}</dd></div><div><dt>记录到的提案字段变化</dt><dd>{stringList(progress.changed_fields).map(field => fields[field] ?? field).join('、') || '本轮未记录'}</dd></div><div><dt>新增来源记录</dt><dd>{stringList(progress.new_evidence_ids).length ? stringList(progress.new_evidence_ids).map(id => <code key={id}><ReferenceText value={id} /></code>) : '本轮未记录'}</dd></div></dl>{reviewers.map((vote, index) => <article key={`${vote.role}-${index}`}><strong>{roleName(vote.role)}<span>{vote.progress.made_progress === true ? '认为有进展' : '未确认进展'}</span></strong><p><ReferenceText value={textOf(vote.progress.rationale) || '未记录判断理由'} /></p></article>)}<small>以上为已记录的角色评议与变化检查。无关新来源与单纯改写表述不作为实质进展依据；这些判断不等同于科研创新已获证明。</small><details><summary>原始进展记录<ChevronDown size={12} /></summary><pre><ReferenceText value={progress} /></pre></details></details>;
}

function CommittedConsensus({ consensus, ...sources }: { consensus?: Json } & ProposalSourceProps) {
  const [selected, setSelected] = useState('current');
  if (!consensus || (!consensus.proposal && !consensus.votes?.length && !consensus.history?.length)) return <div className="empty-state"><div className="empty-illustration"><MessageSquareText size={30} strokeWidth={1.3} /></div><h3>好的方向，来自充分讨论</h3><p>三个角色分别审议创新性、可行性和证据。异议与必要修改会逐条保留，达成一致后推进。</p></div>;
  const history: Json[] = Array.isArray(consensus.history) ? consensus.history : [];
  const round = selected === 'current' ? consensus : history[number(selected)] ?? consensus;
  const votes: Json[] = Array.isArray(round.votes) ? round.votes : [];
  const approved = votes.filter(vote => vote.approve).length;
  const required = votes.flatMap(vote => stringList(vote.required_changes));
  return <div className="consensus-view"><div className="consensus-heading"><div className="round-selector"><MessageSquareText size={16} /><select aria-label="查看已完成共识轮次" value={selected} onChange={event => setSelected(event.target.value)}><option value="current">最近已完成评议 · 第 {number(consensus.round)} 轮</option>{history.map((item, index) => <option value={index} key={index}>历史第 {number(item.round)} 轮 · {item.unanimous ? '一致通过' : '存在异议'}</option>)}</select></div><span className={`status ${round.unanimous ? 'status-approved' : 'status-blocked'}`}><i />{round.unanimous ? '一致通过' : `${approved} / ${votes.length || 3} 同意`}</span></div>
    <div className={`consensus-outcome ${round.unanimous ? 'is-approved' : ''}`}><ShieldCheck size={18} /><div><strong>{round.unanimous ? '各角色同意本轮提案' : `本轮有 ${required.length} 条必要修改`}</strong><p><ReferenceText value={round.unanimous ? '共识对应下方提案版本；后续仍需通过资源与预算门禁。' : '未达成一致时不会进入后续实验。以下修改是智能体明确提出的约束。'} /></p>{round.proposal_hash && <span>提案：<ReferenceText value={round.proposal?.title || '本轮研究提案'} /></span>}</div></div>
    {round.progress_reassessment?.derived === true && <p className="panel-note">进展计数已按保存的独立评议重验；原计数与否决意见保留，仍需修订并重新评审。</p>}
    <DiscussionProgressDetails progress={auditedDiscussionProgress(round.discussion_progress, round.progress_reassessment)} votes={votes} />
    {round.progress_reassessment?.derived === true && <details className="consensus-history"><summary>原进展计数与重验审计<ChevronDown size={14} /></summary><pre><ReferenceText value={{ original: round.discussion_progress, reassessment: round.progress_reassessment }} /></pre></details>}
    <h3>研究提案</h3><Proposal proposal={round.proposal} proposalHash={round.proposal_hash} {...sources} /><h4>各角色独立评议</h4>{votes.map((vote, index) => <div className={`vote ${vote.approve ? 'vote-approved' : 'vote-dissent'}`} key={`${vote.role}-${index}`}><span className={`agent-avatar ${vote.role}`}>{vote.role === 'engineer' ? <Code2 size={17} /> : vote.role === 'scout' ? <BookOpen size={17} /> : <GitBranch size={17} />}</span><div><div className="vote-header"><strong>{roleName(vote.role)}</strong><span className={vote.approve ? 'good-text' : 'warn-text'}>{vote.approve ? <Check size={13} /> : <CircleAlert size={13} />}{vote.approve ? '同意推进' : '需要修订'}</span></div><p><ReferenceText value={vote.rationale} /></p>{stringList(vote.required_changes).length > 0 && <div className="required-changes"><strong>必要修改</strong><ol>{stringList(vote.required_changes).map((change, changeIndex) => <li key={changeIndex}>{change}</li>)}</ol></div>}<ProblemReview review={vote.problem_review} {...sources} /><RoleAmendments vote={vote} amendments={Array.isArray(round.amendments) ? round.amendments : []} /></div></div>)}
    {round.resource_assessment && <details className="consensus-history"><summary><ShieldCheck size={14} />本轮资源评估 · {round.resource_assessment.approved ? '通过' : '未通过'}<ChevronDown size={14} /></summary><pre><ReferenceText value={round.resource_assessment} /></pre></details>}
  </div>;
}

export function EvidencePanel({ steps, run, references = [], literature }: { steps: Json[]; run: Run; references?: Json[]; literature?: LiteratureDetail | null }) {
  const output = { ...(steps.find(step => step.stage === 'literature')?.output ?? {}), ...(literature ?? {}) };
  const papers: Json[] = Array.isArray(output.papers) ? output.papers : [];
  const analysis = output.analysis ?? {};
  const referenceIds: string[] = Array.isArray(run.config?.reference_ids) ? run.config.reference_ids : [];
  const readingNotes = <LiteratureNotes papers={papers} readings={output.readings} notes={output.notes} readingPlan={output.reading_plan} readingGate={output.reading_gate} />;
  if (!papers.length && !referenceIds.length) return <ReferenceNames records={[papers, references, output]}><div className="run-evidence"><LiteratureRounds literature={literature} /><LiteratureCoverage coverage={output.coverage} />{readingNotes}<div className="empty-state"><div className="empty-illustration"><BookOpen size={30} strokeWidth={1.3} /></div><h3><ReferenceText value={run.mode === 'demo' ? '演示任务未执行真实文献检索' : output.coverage ? '本次检索尚未汇集可展示的文献' : '文献阶段完成后，证据将在这里汇集'} /></h3><p>这里展示该任务实际使用的资料及核验状态；上传参考资料与在线检索结果分别保留来源。</p></div></div></ReferenceNames>;
  return <ReferenceNames records={[papers, references, output]}><div className="run-evidence"><LiteratureRounds literature={literature} /><LiteratureCoverage coverage={output.coverage} />{readingNotes}<div className="evidence-summary"><div><strong>{papers.length}</strong><span>检索文献</span></div><div><strong>{papers.filter(paper => paper.verified).length}</strong><span>标识与元数据已核验</span></div><div><strong>{referenceIds.length}</strong><span>用户参考资料</span></div></div>{analysis.summary && <div className="evidence-analysis"><h3>文献研究员的综合判断</h3><p><ReferenceText value={analysis.summary} /></p>{stringList(analysis.coverage_limitations).length > 0 && <details><summary>检索与证据的局限性<ChevronDown size={13} /></summary><ul>{stringList(analysis.coverage_limitations).map((item, index) => <li key={index}><ReferenceText value={item} /></li>)}</ul></details>}</div>}
    {referenceIds.length > 0 && <div className="uploaded-reference-list"><h4>上传 / 导入的参考资料</h4><p>原始文档是研究输入，不能仅凭上传视为已核验文献。内容与来源随任务归档导出。</p>{referenceIds.map(id => { const reference = references.find(item => item.id === id); return <div key={id}><FileText size={15} /><div><strong><ReferenceText value={reference?.title ?? id} /></strong>{reference && <small>{number(reference.text_length).toLocaleString()} 字符 · <ReferenceText value={reference.source} /></small>}<code><ReferenceText value={id} /></code></div></div>; })}</div>}
    <div className="evidence-papers">{papers.map((paper, index) => <article key={paper.id ?? index}><div className="evidence-paper-top"><span className="pill">{paper.source ?? paper.provenance ?? '任务证据'}</span><span>{paper.year ?? '年份未知'}</span><span className={paper.verified ? 'good-text' : 'warn-text'}><ShieldCheck size={13} />{paper.verified ? '标识已核验' : '未通过核验'}</span></div><h3>{safeUrl(paper.url) ? <a href={safeUrl(paper.url)} target="_blank" rel="noreferrer"><ReferenceText value={paper.title} /><ArrowUpRight size={15} /></a> : paper.title}</h3><p><ReferenceText value={Array.isArray(paper.authors) ? paper.authors.map(textOf).join(' · ') : textOf(paper.authors)} /></p><div className="evidence-paper-id"><code>{paper.id ?? paper.doi ?? '无稳定标识'}</code>{paper.checked_at && <span>{date(paper.checked_at, true)} 核验</span>}</div>{paper.verification && <details><summary>核验来源与记录<ChevronDown size={12} /></summary>{safeUrl(paper.verification.evidence_url) && <a className="text-btn" href={safeUrl(paper.verification.evidence_url)} target="_blank" rel="noreferrer">查看权威记录<ExternalLink size={12} /></a>}<pre><ReferenceText value={paper.verification} /></pre></details>}</article>)}</div><div className="inline-info"><ShieldCheck size={16} />元数据核验确认文献身份，不验证论文结论、实验可复现性或当前方案创新性。</div></div></ReferenceNames>;
}

export function HandoffBoard({ handoffs }: { handoffs?: Json[] }) {
  const [filter, setFilter] = useState('all');
  const [recipient, setRecipient] = useState('all');
  const records = (Array.isArray(handoffs) ? [...handoffs] : []).sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  if (!records.length) return <ReferenceNames records={handoffs}><div className="empty-state"><div className="empty-illustration"><Network size={30} strokeWidth={1.3} /></div><h3>智能体交接将在这里留痕</h3><p>展示实际持久化的发送方、接收方、交付物与验收状态。旧任务若没有交接记录，不会补造历史。</p></div></ReferenceNames>;
  const statusNames: Record<string, string> = { queued: '待接收', running: '处理中', completed: '已交付', blocked: '被阻塞', interrupted: '已中断' };
  const purposeNames: Record<string, string> = { intake: '解析研究问题', literature: '检索与证据判断', evidence: '文献补充核验', amendment: '提交修改方案', proposal: '形成研究提案', revision: '根据异议修订', revise: '根据异议修订', protocol_review: '实验方案复审', cycle_review: '研究周期评审', repair: '修复实验实现', acceptance: '交付验收', vote: '独立评议', design: '设计研究架构', design_document_section: '分段评议架构文档', design_document_review: '汇总架构文档评议', implementation_spec: '编写代码实现文档', implementation_document_review: '三方全文审核', implementation: '生成实验代码', review: '审查交付物', tuning: '设计参数搜索', report: '整理研究报告' };
  const purposeName = (purpose: unknown) => {
    const [base, iteration] = String(purpose ?? '').split(':');
    if (base === 'evidence' || base === 'amendment') return purposeNames[base];
    const label = purposeNames[base === 'tune' ? 'tuning' : base] ?? humanizeMessage(base || '研究交接');
    return iteration !== undefined ? `${label} · ${base === 'vote' || base === 'revision' || base === 'revise' ? `第 ${iteration} 轮` : `版本 ${iteration}`}` : label;
  };
  const dependencyName = (id: string) => {
    const dependency = records.find(record => record.id === id);
    return dependency ? `${roleName(dependency.to_role)} · ${purposeName(dependency.purpose)}` : id;
  };
  const goToDependency = (id: string) => {
    setFilter('all');
    setRecipient('all');
    // Reset filters before locating a dependency that may currently be hidden.
    requestAnimationFrame(() => document.getElementById(`handoff-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
  };
  const shown = records.filter(handoff => (filter === 'all' || ['blocked', 'interrupted'].includes(handoff.status)) && (recipient === 'all' || handoff.to_role === recipient));
  return <ReferenceNames records={handoffs}><div className="handoff-board"><div className="handoff-overview"><div><strong>{records.length}</strong><span>实际交接</span></div><div><strong>{records.filter(item => item.status === 'completed').length}</strong><span>已交付</span></div><div><strong>{records.reduce((total, item) => total + number(item.reused_count), 0)}</strong><span>复用既有交付</span></div></div><div className="handoff-toolbar"><div className="tabs"><button className={filter === 'all' ? 'selected' : ''} onClick={() => setFilter('all')}>全部交接</button><button className={filter === 'attention' ? 'selected' : ''} onClick={() => setFilter('attention')}>阻塞 / 中断</button></div><select aria-label="按接收智能体筛选交接" value={recipient} onChange={event => setRecipient(event.target.value)}><option value="all">全部接收方</option>{['director', 'engineer', 'scout'].map(role => <option key={role} value={role}>{roleName(role)}</option>)}</select></div>
    {!shown.length && <p className="handoff-no-match">当前筛选下没有交接记录。</p>}{shown.map((handoff, index) => <article className={`handoff-record handoff-${handoff.status}`} id={`handoff-${handoff.id}`} key={handoff.id ?? index}><div className="handoff-route"><span className={`handoff-role ${handoff.from_role}`}>{roleName(handoff.from_role)}</span><ArrowRight size={15} /><span className={`handoff-role ${handoff.to_role}`}>{roleName(handoff.to_role)}</span><span className={`status status-${handoff.status === 'queued' ? 'pending' : handoff.status}`}><i /><ReferenceText value={statusNames[handoff.status] ?? humanizeMessage(textOf(handoff.status))} /></span></div><div className="handoff-title"><h3><ReferenceText value={purposeName(handoff.purpose)} /></h3><span><ReferenceText value={stages[handoff.stage] ?? humanizeMessage(textOf(handoff.stage))} /></span><time>{date(handoff.updated_at ?? handoff.created_at, true)}</time></div>{handoff.summary && (handoff.status === 'completed' ? <p><ReferenceText value={handoff.summary} /></p> : <HumanMessage text={textOf(handoff.summary)} />)}{handoff.error && <div className="handoff-error"><CircleAlert size={14} /><HumanMessage text={textOf(handoff.error)} /></div>}<CitationDiscardNotice audit={handoff.citation_discard_audit} /><div className="handoff-fingerprints"><span>尝试 <b>{number(handoff.attempts)}</b></span><span>复用 <b>{number(handoff.reused_count)}</b></span><span>输入：<ReferenceText value={purposeName(handoff.purpose)} /></span>{handoff.output_hash && <span>交付：<ReferenceText value={purposeName(handoff.purpose)} /></span>}</div><details><summary>来源证据与交接依赖<ChevronDown size={13} /></summary><div className="handoff-evidence-group"><strong>关联文献</strong>{stringList(handoff.source_paper_ids).length ? <div className="evidence-id-list">{stringList(handoff.source_paper_ids).map(id => <code key={id}><ReferenceText value={id} /></code>)}</div> : <span>无关联文献 ID</span>}</div><div className="handoff-evidence-group"><strong>输入产物</strong>{stringList(handoff.source_artifact_ids).length ? <div className="evidence-id-list">{stringList(handoff.source_artifact_ids).map(id => <code key={id}><ReferenceText value={id} /></code>)}</div> : <span>无产物 ID</span>}</div><div className="handoff-evidence-group"><strong>依赖的前序交接</strong>{stringList(handoff.depends_on).length ? <div className="handoff-dependencies">{stringList(handoff.depends_on).map(id => <a href={`#handoff-${id}`} onClick={event => { event.preventDefault(); goToDependency(id); }} title={dependencyName(id)} key={id}>{dependencyName(id)}<ArrowUpRight size={11} /></a>)}</div> : <span>无前序依赖</span>}</div><code className="handoff-record-id">交接 ID：<ReferenceText value={handoff.id} /></code></details></article>)}
  </div></ReferenceNames>;
}
