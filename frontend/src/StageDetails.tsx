import { ReferenceText, ReferenceNames } from './ReferenceNames';
import { OptimizationProgress } from './OptimizationProgress';
import { ArchitectureAdjustmentDetails } from './ArchitectureAdjustmentDetails';
import { validationStageLabel } from './ValidationHold';
import { useEffect, useRef, useState } from 'react';
import { X, ExternalLink, BookOpen, FileText, ChevronRight } from 'lucide-react';
import { textOf, safeUrl, type Detail, type Json, type Run } from './api';
import { NoveltyGate } from './NoveltyGate';
import { InnovationView } from './InnovationView';
import { ProposalProvenance } from './ProposalProvenance';
import { useDialogKeyboard } from './useDialogKeyboard';
import './stage-details.css';
import { ViewBoundary } from './ViewBoundary';
import { ArchitectureDocuments } from './ArchitectureDocuments';
import { ArchitectureRecovery } from './ArchitectureRecovery';
import { architectureDeliveryLabel, architectureDeliveryNext, architectureReviewDeliveryLabel } from './ArchitectureDelivery';
import { LocalCodexPanel } from './LocalCodexPanel';
import { ImplementationSidebar } from './ImplementationSidebar';
import { NativeCodeProgress } from './NativeCodeProgress';
import { ExperimentView } from './ExperimentView';
import { ParameterGrid } from './ParameterGrid';
import { ValidationReviewDetail } from './ValidationReviewDetail';
import { useRemoteResource, useProjectionSnapshot } from './useRemoteResource';
import { mergeRunProjection, projectionRevision, runStagePath, type RunProjection } from './RunProjections';
import { ARCHITECTURE_EVIDENCE_REPEAT_GATE, ARCHITECTURE_EVIDENCE_REPEAT_TITLE, ARCHITECTURE_EVIDENCE_REPEAT_DESCRIPTION } from './RequestRecoveryDiagnostic';

const purpose: Record<string, string> = {
  intake: '确认研究问题、成功标准和您允许使用的资源。', literature: '检索近期论文，阅读方法架构、实验和局限，形成共享进展材料。',
  innovation: '三位角色先阅读同一份近期进展，再独立提出候选、比较并收束一个方向。',
  related_work: '围绕创新点检索最相近论文，比较实质重合与差异，未通过审查则退回重想。',
  debate: '三位角色独立审查科学问题、证据、可行性和资源计划。', design: '将已通过共识的方案转为可执行实验设计。',
  implementation: '先编写实现文档，通过同版本独立审计后交给本机 Codex 实现，再验收实际代码。', experiments: '运行基线、候选和消融实验，保存真实测量。',
  tuning: '使用验证集选择参数并比较改进。', architecture_tuning: '依据验证证据微调既有架构，三方复审和检查通过后返回实验验证；与参数优化循环进行。', validation_review: '审查验证结果，决定继续改进或进入独立测试。',
  revision: '根据失败证据修订方案。', final_tests: '在冻结方案后进行独立测试。', report: '汇总研究结论、证据、局限和可复现记录。',
};

function definitionDissent(value?: Json): number {
  return (Array.isArray(value?.votes) ? value.votes : []).filter((vote: Json) =>
    vote.approve === false || vote.required_changes_count > 0 || (Array.isArray(vote.required_changes) && vote.required_changes.length > 0)).length;
}

export function stageSummary(stage: string, step?: Json, data?: Detail): string {
  const output = step?.output;
  if (stage === 'tuning' && (step?.activity ?? output?.activity) === 'diagnostic_repair') return '正在参数微调阶段处理诊断补修；补修和 CPU 检查不代表参数搜索完成，验证评审尚未开始。已有实验与原始回执保留。';
  if (stage === 'tuning' && (step?.tuning_pending || output?.tuning_pending)) return '当前批次已记录，仍需依据验证结果决定下一步；参数微调尚未完成。';
  const holdLabel = data?.run && validationStageLabel(data.run, stage);
  if (holdLabel) return `${holdLabel}。核对已保存的失败指标、验收完整性与三方意见后，按停止规则收尾。`;
  if (stage === 'design' && data?.run?.stage === 'design' && ['blocked', 'paused', 'failed'].includes(data.run.status) && data.run.error === ARCHITECTURE_EVIDENCE_REPEAT_GATE) {
    return `${ARCHITECTURE_EVIDENCE_REPEAT_TITLE}。${ARCHITECTURE_EVIDENCE_REPEAT_DESCRIPTION}`;
  }
  if (stage === 'design' && architectureDeliveryLabel(data?.architecture_review?.revision)) {
    return `${architectureDeliveryLabel(data?.architecture_review?.revision)}。${architectureDeliveryNext(data?.architecture_review?.revision)}`;
  }
  if (stage === 'design' && architectureReviewDeliveryLabel(data?.architecture_review?.current)) {
    return `${architectureReviewDeliveryLabel(data?.architecture_review?.current)}。原意见和读取记录保留，补正后继续同版本独立评议。`;
  }
  if (stage === 'design' && data?.architecture_review?.revision?.definition_review) {
    const definitions = data.architecture_review.revision.definition_review;
    if (definitions.status === 'rejected' || definitions.status === 'blocked') return `具体科学定义补充等待处理 · ${definitions.issue_count ?? 0} 项意见；${definitions.reason ?? '查看具体未决项与必改意见。'}`;
    if (definitions.status === 'drafting') return `正在编写具体科学定义 · ${definitions.issue_count ?? 0} 项意见；${definitions.authoring_protocol === 2 ? `逐项候选已保存 ${definitions.saved_issue_count ?? 0}/${definitions.issue_count ?? 0} 项，当前第 ${definitions.active_issue_number ?? 1} 项。` : ''}${definitions.activity ? `已读取 ${definitions.activity.read_pages ?? 0} 个分页。` : '按索引读取既有合同与原文。'}提交 Markdown 后进入三方独立评议。`;
    const dissent = definitionDissent(definitions);
    if (definitions.status === 'reviewing' && dissent) return `具体科学定义仍在独立评议 · 已收到 ${definitions.received_votes ?? 0}/${definitions.expected_votes ?? 3} 份独立意见，已有 ${dissent} 份未通过意见；当前版本尚未获准。`;
    return `具体科学定义补充 · ${definitions.issue_count ?? 0} 项意见，已收到 ${definitions.received_votes ?? 0}/${definitions.expected_votes ?? 3} 份独立意见；同版本取得一致意见后继续新架构评议。`;
  }
  if (stage === 'design' && data?.architecture_review?.revision?.author_response) {
    const response = data.architecture_review.revision.author_response;
    return `架构作者已交付，仍有 ${response.unresolved_issues?.length ?? 0} 项意见未闭合。${response.requires_new_consensus ? '需补齐定义并重新取得三方共识。' : '需闭合具体意见并完成独立评议。'}`;
  }
  if (stage === 'design' && data?.architecture_review?.current) return `${data.architecture_review.current.status === 'approved' ? '架构文档评议通过，准备推进代码实现。' : data.architecture_review.current.status === 'rejected' ? '架构文档需要修改，点击查看具体意见。' : '架构 Markdown 已保存，正在按索引分段评议。'}`;
  if (stage === 'implementation' && data?.local_codex?.current) {
    const workflow = data.local_codex.current;
    const status: Record<string, string> = { specification: '正在编写代码实现文档。', spec_review: '实现 Markdown 已保存，三方通过目录和工具阅读全文审核。', implementing: '实现文档已批准，本机 Codex 正在实现。', acceptance: 'Codex 已交付文件，正在进行本地验收。', code_review: '本地验收已记录，正在独立评议实际代码。', completed: '实现与交付验收已完成，准备进入实验。', blocked: '实现流程等待处理，点击查看具体原因。' };
    return status[workflow.status] ?? '查看实现文档、Codex 实际活动和验收记录。';
  }
  if (data?.projection === 'summary') {
    const summary = step?.summary ?? data.stage_summaries?.[stage];
    if (summary) return textOf(summary);
    const status = { completed: '已完成', running: '正在进行', rejected: '未通过', blocked: '等待处理', skipped: '已跳过' }[String(step?.status)];
    return status ? `${status}。点击按需查看已保存的阶段记录。` : purpose[stage] ?? '查看本阶段研究记录。';
  }
  if (stage === 'innovation') {
    const session = data?.innovation ?? output;
    if (session?.status === 'rethinking') return `创新审查未通过，正在进行第 ${session.attempt} 次思考。点击查看相似论文与退回原因。`;
    if (session?.proposal) return `${session.proposal.title} · ${session.candidates?.length ?? 0} 个候选，${session.comparisons?.length ?? 0} 份比较意见。${session.novelty_gate ? session.novelty_gate.approved ? '相似性审查通过。' : '相似性审查未通过。' : '相似性审查尚未完成。'}`;
  }
  if (stage === 'literature' || stage === 'related_work') {
    if (output) return `${output.papers?.length ?? 0} 篇检索论文 · ${output.reading_gate?.noted_core_ids?.length ?? 0} 篇核心笔记。${textOf(output.analysis?.summary).slice(0, 200)}`;
  }
  const summary = output?.summary ?? output?.research_question ?? output?.rationale ?? output?.architecture ?? output?.proposal?.title;
  return summary ? textOf(summary).slice(0, 260) : purpose[stage] ?? '查看本阶段研究记录。';
}

const labels: Record<string, string> = { summary: '摘要', research_question: '研究问题', method: '方法与架构', datasets: '数据集', baselines: '比较基线', limitations: '局限', relevance: '对当前研究的意义', missing_information: '尚未读到或待核实', domain: '研究领域', success_criteria: '成功标准', constraints: '约束', architecture: '架构', dataset: '数据', evaluation: '评估方法', rationale: '判断依据', hypothesis: '假设', novelty: '创新点', risks: '风险', explanation: '实现说明', decision: '阶段决定' };

function Fields({ value }: { value?: Json }) {
  return <dl className="stage-detail-fields">{Object.entries(labels).filter(([key]) => value?.[key] != null).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{Array.isArray(value?.[key]) ? <ul>{value[key].map((line: unknown, i: number) => <li key={i}><ReferenceText value={line} /></li>)}</ul> : textOf(value?.[key])}</dd></div>)}</dl>;
}

export function LiteratureStageDetail({ output, literature }: { output?: Json; literature?: Json | null }) {
  if (!output) return <p>本轮分析尚未完成，完成后这里将显示论文与阅读内容。</p>;
  const notes: Json[] = output.notes ?? literature?.notes ?? [];
  const core = new Set(output.reading_gate?.selected_core_ids ?? []);
  const papers: Json[] = [...(output.papers ?? [])].sort((a, b) => Number(core.has(b.id)) - Number(core.has(a.id)));
  return <div className="stage-literature"><p><ReferenceText value={output.analysis?.summary} /></p><p className="muted">检索截至 {output.date_policy?.as_of_date ?? output.searched_at?.slice(0, 10) ?? '未记录'} · {papers.length} 篇论文 · 已读片段和未读内容分别列出</p>{papers.map(paper => {
    const saved = notes.filter(note => note.paper_id === paper.id);
    const url = safeUrl(paper.url);
    const related = output.analysis?.related_papers?.find((p: Json) => p.paper_id === paper.id);
    return <details className="stage-paper" key={paper.id}><summary><BookOpen size={16} /><span><ReferenceText value={paper.title ?? paper.id} /><small>{paper.publication_date ?? paper.year ?? '日期不明'} · {core.has(paper.id) ? '核心论文 · ' : ''}{saved.length ? '已有阅读笔记' : '尚无合格阅读笔记'}</small></span></summary><div className="stage-paper-body">{url && <a href={url} target="_blank" rel="noreferrer">打开论文来源 <ExternalLink size={12} /></a>}<code><ReferenceText value={paper.id} /></code>{saved.length ? saved.map((note, i) => <article key={note.id ?? i}>{note.reused_from && <p>共享 RAG 复用 · 原分析 {note.reused_from.analysis_created_at?.slice(0, 10)} · 本次未重新阅读全文</p>}<Fields value={note} /><details><summary>原文摘录与定位</summary>{(note.excerpts ?? []).map((excerpt: Json, j: number) => <blockquote key={j}><small>{excerpt.locator}</small><p><ReferenceText value={excerpt.quote} /></p></blockquote>)}</details></article>) : <p>{paper.abstract ?? paper.abstract_excerpt ?? '尚未保存内容摘要。'}</p>}{related && <dl className="stage-detail-fields">{[['relevance_to_idea', '与创新点的关系'], ['overlap', '重合点'], ['difference', '实质差异'], ['required_check', '待核实']].map(([key, label]) => <div key={key}><dt>{label}</dt><dd><ReferenceText value={related[key]} /></dd></div>)}</dl>}</div></details>;
  })}</div>;
}

export function StageDetailBody({ stage, step, data, run }: { stage: string; step?: Json; data?: Detail; run: Run }) {
  if (stage === 'architecture_tuning') {
    const loop = data?.architecture_tuning;
    return <section className="architecture-tuning-detail">
      <ArchitectureAdjustmentDetails key={run.id} runId={run.id} value={data?.architecture_adjustments} />
      <details><summary>阶段进展与优化流程</summary><h3>验证与架构微调循环</h3>
      <p>实验验证 → 架构评估与调整 ↺ 验证 → 参数微调 → 稳定性复核</p>
      <p>三方先检查已有对照、消融、训练统计和模型源码，评估架构合理性；必要时修改并复验。架构通过后才调参数，原始实验和回执继续保留。</p>
      <p>{data?.optimization_loop?.completion_rule ?? '架构与参数经实际结果和三方评审确认稳定后才切换数据集；首数据集模型源码冻结复用。覆盖完成或停止不等于稳定。'}</p>
      {step?.output?.decision_id && <Fields value={step.output} />}
      <OptimizationProgress value={data?.optimization_loop ?? loop} />
      {loop ? <><Fields value={loop.result ?? loop.review} /><details><summary>阶段流程记录</summary><pre><ReferenceText value={loop} /></pre></details></> : <p>尚无已批准的架构修改；先评估已有验证证据，不因实验覆盖完成自动进入调参或下一数据集。</p>}
      </details>
    </section>;
  }
  const output = step?.output;
  if (stage === 'validation_review') return <ValidationReviewDetail output={output} run={run} />;
  if (['experiments', 'tuning', 'final_tests'].includes(stage)) return <>
    {stage === 'tuning' && <ParameterGrid value={data?.parameter_grid} experiments={data?.experiments} runId={run.id} />}
    {data?.dataset_configuration && <section className="panel dataset-configuration-panel" aria-label="实验数据配置"><h3>实验数据配置</h3>
      <p className="dataset-config-overview"><strong>{data.dataset_configuration.queue?.active_dataset ?? data.dataset_configuration.datasets.join('、')}</strong><span>{data.dataset_configuration.queue?.attempts_unlimited ? '实验不限次数' : '按任务执行策略运行'}</span><span>待运行：{data.dataset_configuration.queue?.pending_datasets.join('、') || '无'}</span></p>
      <details className="dataset-config-details"><summary>配置状态、执行策略与实现记录</summary>
      <p>复用原架构与模型实现，通过配置文件和命令行参数选择数据集。数据接口检查通过后开始成对验证。</p>
      <p>数据集：{data.dataset_configuration.datasets.join('、')}</p>
      {data.dataset_configuration.queue && <>
        <p>按数据集逐个配置和运行。当前：{data.dataset_configuration.queue.active_dataset ?? '本轮已结束'}；待配置：{data.dataset_configuration.queue.pending_datasets.join('、') || '无'}。</p>
        {data.dataset_configuration.queue.budget_scope !== 'legacy_shared' ? <>
          <p>{data.dataset_configuration.queue.attempts_unlimited ? '实验不限次数，按计划完成各数据集的验证与消融，已完成结果继续复用。' : '每个数据集依据自身任务配置制定预算，独立核验和计数，包含接口检查、失败和重试。'}</p>
          <ul aria-label="各数据集实验预算">{data.dataset_configuration.queue.datasets.map((item: Json) => <li key={item.dataset}>
            {item.dataset}：{item.attempt_limit == null
              ? `${item.budget_status === 'unlimited' ? '不限次数' : item.budget_status === 'proposed' ? '预算方案待评审' : '预算待定'}，已使用 ${item.attempts_used} 次`
              : `已使用 ${item.attempts_used} / ${item.attempt_limit} 次，剩余 ${item.remaining_attempts} 次`}{item.status === 'pending' ? '（待配置）' : ''}
            {item.budget_proposal && <details><summary>{item.budget_status === 'unlimited' ? '历史预算记录' : '预算依据与明细'}</summary><Fields value={item.budget_proposal} /></details>}
          </li>)}</ul>
        </> : <p>历史共享执行预算：已使用 {data.dataset_configuration.queue.attempts_used} / {data.dataset_configuration.queue.attempt_limit} 次（含接口检查及重试）。</p>}
        <details><summary>各数据集进度</summary><ul>{data.dataset_configuration.queue.batches.map((item: Json) => <li key={item.setup_id}>{item.dataset}：{item.status === 'finished' ? '本份流程已结束，结果已归档' : '正在配置或执行'}</li>)}</ul></details>
      </>}
      <p>状态：{({pending:'等待配置',planning_configuration:'研究 agent 正在核对数据选型并编写实现任务单',configuring:'正在编写配置与数据接口',implementing_configuration:'Codex 正在编写配置与运行接口',repairing_configuration:'Agent 正在补齐配置与修复接口',checking_configuration:'核对配置',reviewing:'独立核验配置与接口',checking_data:'运行兼容性检查',ready:'配置已就绪',blocked:'配置修复需要处理'} as Record<string,string>)[data.dataset_configuration.status] ?? data.dataset_configuration.status}</p>
      {data.dataset_configuration.planning?.length > 0 && <details><summary>数据选型与 Agent 实现任务单</summary>
        {data.dataset_configuration.planning.map((plan: Json) => <section key={plan.id}>
          <p>{plan.scope.includes(':selection') ? '数据选型' : '配置与代码任务单'}：{({authoring:'正在编写',reviewing:'独立评议中',approved:'已通过评议'} as Record<string,string>)[plan.status] ?? plan.status}</p>
          <p><ReferenceText value={plan['result.implementation_brief'] ?? plan['result.coverage_rationale'] ?? ''} /></p>
          {plan.votes?.length > 0 && <Fields value={plan.votes} />}
          {plan.issues?.length > 0 && <Fields value={plan.issues} />}
        </section>)}
      </details>}
      {data.dataset_configuration.codex && <details><summary>Codex 配置实现详情</summary>
        <p>工作目录：<code>{data.dataset_configuration.codex.workspace}</code></p>
        <p>交付状态：{data.dataset_configuration.codex.status}；代码与配置保存后进入独立核验。</p>
        {data.dataset_configuration.codex.activity?.length > 0 && <pre><ReferenceText value={data.dataset_configuration.codex.activity} /></pre>}
      </details>}
      {data.dataset_configuration.repair_sequence > 0 && <details><summary>第 {data.dataset_configuration.repair_sequence} 轮修复 · 查看当前缺项</summary><ul>{(data.dataset_configuration.repair ?? []).map((issue: string, index: number) => <li key={index}><ReferenceText value={issue} /></li>)}</ul></details>}
      <pre>{data.dataset_configuration.command}</pre>
      {data.dataset_configuration.configuration_path && <p>配置文件：<code>{data.dataset_configuration.configuration_path}</code></p>}
      {data.dataset_configuration.error && <p role="alert">{data.dataset_configuration.error}</p>}
      {data.dataset_configuration.reviews?.length > 0 && <details><summary>本次配置核验意见</summary><Fields value={data.dataset_configuration.reviews} /></details>}
      </details>
    </section>}
    {data?.dataset_configuration && !(data.experiments ?? []).some(item => (item.research_cycle ?? 1) === (run.state?.research_cycle ?? 1))
      ? <><p>本轮数据配置尚未产生实验测量。</p><details><summary>查看此前轮次的实验记录</summary>
        <ExperimentView runId={run.id} experiments={data?.experiments ?? []} evaluation={data?.evaluation} summary={data?.experiment_summary} datasetQueue={data?.dataset_configuration?.queue} currentCycle={run.state?.research_cycle} />
      </details></>
      : <ExperimentView runId={run.id} experiments={data?.experiments ?? []} evaluation={data?.evaluation} summary={data?.experiment_summary} datasetQueue={data?.dataset_configuration?.queue} currentCycle={run.state?.research_cycle} />}
    {output && <details><summary>阶段结论与参数选择</summary><Fields value={output} /></details>}
    {Boolean(step?.output_omitted) && <p>阶段原始记录较大，页面展示实验指标与训练日志，原记录保留。</p>}
  </>;
  if (stage === 'design' && data?.architecture_review) return <ArchitectureDocuments runId={run.id} value={data.architecture_review} datasetAdaptation={Boolean(run.state?.dataset_adaptation)} experimentConfiguration={run.state?.dataset_configuration_protocol === 1} />;
  if (stage === 'implementation' && data?.local_codex) return <LocalCodexPanel runId={run.id} workflow={data.local_codex} />;
  if (stage === 'innovation') return <InnovationView run={run} session={data?.innovation ?? output} papers={data?.literature?.papers ?? []} />;
  if (stage === 'literature' || stage === 'related_work') return <><LiteratureStageDetail output={output} literature={data?.literature} />{output?.novelty_gate && <NoveltyGate gate={output.novelty_gate} papers={output.papers} />}</>;
  return <><Fields value={output ?? (stage === 'debate' ? data?.consensus : undefined)} />{(output?.proposal ?? (stage === 'debate' ? data?.consensus?.proposal : undefined)) && <ProposalProvenance proposal={output?.proposal ?? data?.consensus?.proposal} papers={data?.literature?.papers ?? []} />}{!output && <p><ReferenceText value={stage === 'debate' && data?.current_debate ? '共识讨论正在进行，可在“共识讨论”中查看逐角色意见。' : '本阶段尚无完成结果，已有记录会在完成后显示。'} /></p>}{output && <details><summary>完整阶段记录</summary><pre><ReferenceText value={output} /></pre></details>}</>;
}

export function ArchitectureStageEntry({ data, run, openLimits, prepared, externalBusy, onBusyChange, progressView = false }: {
  data: Detail; run: Run; openLimits?: () => void; prepared?: () => void;
  externalBusy?: boolean; onBusyChange?: (busy: boolean) => void; progressView?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const value = data.architecture_review;
  if (!value?.required && !value?.current) return null;
  const definitions = value.revision?.definition_review;
  const definitionPhase = definitions && definitions.status !== 'approved';
  const convergence = definitionPhase ? definitions.completion?.convergence : value.revision?.convergence;
  const dissent = definitionDissent(definitions);
  const unresolved = Math.max(definitions?.unresolved_issue_count ?? 0,
    definitions?.issue_documents?.filter((unit: Json) => unit.status === 'unresolved').length ?? 0);
  const waiting = ['blocked', 'paused', 'failed'].includes(run.status);
  const repeatedEvidence = waiting && run.stage === 'design' && run.error === ARCHITECTURE_EVIDENCE_REPEAT_GATE;
  const deliveryLabel = architectureDeliveryLabel(value.revision) ?? (!definitionPhase ? architectureReviewDeliveryLabel(value.current) : undefined);
  const status = (repeatedEvidence ? ARCHITECTURE_EVIDENCE_REPEAT_TITLE : undefined) ?? deliveryLabel ?? (definitionPhase
    ? waiting || ['blocked', 'rejected'].includes(definitions.status) ? '定义等待处理'
      : definitions.status === 'drafting' ? run.status === 'running' ? '正在补充具体定义' : '等待补充具体定义'
        : definitions.status === 'reviewing' ? `正在独立评议具体定义${dissent ? ' · 已有异议' : ''}` : '已安排具体定义补充'
    : definitions?.status === 'approved' && value.revision?.status === 'needs_consensus'
      ? waiting ? '定义已通过，等待架构交接' : '定义已通过，正在交接架构修订'
      : value.revision?.status === 'drafting' ? waiting ? '架构修订等待处理' : '正在修订架构'
      : value.revision?.status === 'reviewing' ? '修订架构评议中'
        : value.current?.status === 'approved' || value.revision?.status === 'approved' ? '架构评议通过'
          : value.current?.status === 'rejected' ? '架构需要修改'
            : value.current ? '架构评议中' : '等待生成架构文档');
  const progress = definitionPhase
    ? `具体定义已保存 ${definitions.saved_issue_count ?? 0}/${definitions.issue_count ?? 0} 项${definitions.status === 'drafting' && definitions.active_issue_number ? ` · ${run.status === 'running' ? '当前处理' : '下一项'}：第 ${definitions.active_issue_number} 项` : ''}。${definitions.review_protocol === 3 ? `分组意见 ${definitions.saved_review_units ?? 0}/${definitions.expected_review_units ?? 0} 份；三方终审` : '独立意见'} ${definitions.received_votes ?? 0}/${definitions.expected_votes ?? 3} 份。${dissent ? `其中 ${dissent} 份暂不批准或仍有必改项。` : ''}`
    : definitions?.status === 'approved' && definitions.review_protocol === 3
      ? `具体定义已通过：分组意见 ${definitions.saved_review_units ?? 0}/${definitions.expected_review_units ?? 0} 份；三方终审 ${definitions.received_votes ?? 0}/${definitions.expected_votes ?? 3} 份。统一架构的独立评议另行记录。`
      : '文档版本、作者交付与独立评议可在详情中查看。';
  const next = (repeatedEvidence ? ARCHITECTURE_EVIDENCE_REPEAT_DESCRIPTION : undefined) ?? architectureDeliveryNext(value.revision) ?? (architectureReviewDeliveryLabel(value.current) && !definitionPhase
    ? value.current?.review_delivery_validation?.status === 'repair_failed' ? '评审补正仍有校验错误，已停止自动重发；查看详情中的具体原因。'
      : '下一步：恢复执行，由原评审补正已保存答复，继续同版本评议。'
    : undefined) ?? (definitionPhase
    ? definitions.completion?.delivery_recovery?.pending ? '下一步：处理已保存终审的交付校验，继续本版剩余评议。'
      : unresolved > 0 ? `推进条件：仍有 ${unresolved} 项未解决；补齐定义并处理未决项后，才能进行独立评议。`
      : definitions.scope_issue_count > 0 || definitions.scope_issues?.length ? '推进条件：处理方案范围问题后，才能继续独立评议。'
        : ['blocked', 'rejected'].includes(definitions.status) ? '推进条件：处理已保存的未闭合意见；当前版本尚未获准。'
          : definitions.status === 'reviewing' ? dissent
            ? '下一步：收齐剩余独立意见，并核对未通过意见；当前版本尚未获准。'
            : '下一步：取得三方对同一版本的独立意见，再继续架构修订。'
          : '下一步：完成具体定义，再进行三方独立评议。'
    : definitions?.status === 'approved' && value.revision?.status === 'needs_consensus' ? '下一步：将已批准的具体定义交给统一架构修订。'
      : definitions?.status === 'approved' && value.revision?.status === 'drafting' ? '下一步：工程师合并已批准定义，形成统一架构供独立评议。'
      : status === '架构评议通过' ? '架构评议已通过。实现与实验进度见对应阶段。'
      : '推进条件：当前版本通过独立评议后，才能进入代码实现。');
  return <><section className="panel architecture-stage-entry" aria-label="架构设计进度"><div><h3><FileText size={17} />{progressView ? '架构设计进度' : <>架构设计 · <ReferenceText value={status} /></>}</h3>{progressView && <p><strong>当前状态：<ReferenceText value={status} /></strong></p>}<p><ReferenceText value={progress} /></p><p className="muted"><ReferenceText value={next} /></p>{convergence?.mode === 'until_stalled' && !['blocked', 'rejected'].includes(definitions?.status) && <p className="muted">评议不设轮次上限 · 连续无实质推进 {convergence.consecutive_no_progress ?? 0} / {convergence.stagnation_window ?? 3} 轮；重复已否决方案时暂停。</p>}<ArchitectureRecovery run={run} value={value} openLimits={openLimits} prepared={prepared} externalBusy={externalBusy} onBusyChange={onBusyChange} /></div><button type="button" className="btn" aria-label="查看架构设计详情" aria-haspopup="dialog" onClick={() => setOpen(true)}>查看文档与评议<ChevronRight size={15} /></button></section>{open && <StageDetails stage="design" label="架构设计" step={data.steps?.find((step: Json) => step.stage === 'design')} data={data} run={run} close={() => setOpen(false)} />}</>;
}

export function stageDetailRequestPath(runId: string, stage: string, lazy: boolean, implementationRecordsOpen: boolean) {
  return lazy && (stage !== 'implementation' || implementationRecordsOpen) ? runStagePath(runId, stage) : null;
}

export function StageDetails({ stage, label, step, data, run, close }: { stage: string; label: string; step?: Json; data?: Detail; run: Run; close: () => void }) {
  useDialogKeyboard(close, false);
  const button = useRef<HTMLButtonElement>(null);
  const [recordSelection, setRecordSelection] = useState<string | null>(null);
  const recordScope = `${run.id}:${stage}`;
  const recordsOpen = recordSelection === recordScope;
  const lazy = data?.projection === 'summary';
  const detailPath = stageDetailRequestPath(run.id, stage, lazy, recordsOpen);
  const snapshot = useProjectionSnapshot(detailPath ? recordScope : null, projectionRevision(data), data?.client_refresh ?? 0);
  const trainingStage = ['experiments', 'tuning', 'final_tests'].includes(stage);
  const detail = useRemoteResource<RunProjection>(detailPath, snapshot.key, trainingStage ? 5000 : 0, !trainingStage);
  const selectedData = data && detail.data ? mergeRunProjection(data, detail.data) : data;
  const selectedStep = detail.data?.steps?.find(item => item.stage === stage) ?? step;
  const implementationProgress = data?.implementation_progress;
  const showCodeProgress = implementationProgress?.run_id === run.id
    && implementationProgress?.code_generation_started && implementationProgress.workflow?.id
    && ['code_generation', 'acceptance', 'code_review', 'completed'].includes(implementationProgress.phase);
  useEffect(() => { button.current?.focus(); }, []);
  return <ReferenceNames records={[selectedData, selectedStep, run]}><div className="stage-detail-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) close(); }}><section className={`stage-detail-dialog ${stage==='design'?'stage-detail-design':''} ${stage==='implementation'?'stage-detail-implementation':''} ${trainingStage?'stage-detail-experiments':''} ${stage==='validation_review'?'stage-detail-validation':''}`} role="dialog" aria-modal="true" aria-labelledby="stage-detail-title"><header><div><small>研究阶段详情</small><h2 id="stage-detail-title"><ReferenceText value={label} /></h2></div><button ref={button} type="button" className="btn" aria-label="关闭阶段详情" onClick={close}><X size={20} /></button></header><div className="stage-detail-content">{stage !== 'implementation' && <p className="stage-detail-purpose"><ReferenceText value={purpose[stage]} /></p>}{stage === 'implementation' && <><div className="implementation-detail-layout"><ImplementationSidebar progress={implementationProgress} runId={run.id} localCodex={selectedData?.local_codex} />
      {showCodeProgress && <NativeCodeProgress runId={run.id} workflowId={implementationProgress.workflow.id}
        cycle={implementationProgress.cycle} runStatus={run.status} />}
    </div><details className="implementation-source-records" open={recordsOpen} onToggle={event => setRecordSelection(event.currentTarget.open ? recordScope : null)}><summary>完整版本、交付与验收记录</summary>{recordsOpen && <><p><ReferenceText value={purpose[stage]} /></p>{lazy && detail.data && snapshot.stale && !trainingStage && <button className="btn small" onClick={snapshot.refresh}>有新记录，刷新阶段详情</button>}{lazy && !detail.data ? detail.error ? <div role="alert"><p>{detail.error}</p><button className="btn" onClick={snapshot.refresh}>重新读取阶段详情</button></div> : <p role="status">正在读取代码实现详情…</p> : <ViewBoundary key={`${run.id}:${stage}`} label={label}><StageDetailBody stage={stage} step={selectedStep} data={selectedData} run={run} /></ViewBoundary>}</>}</details></>}{run.mode === 'demo' && <p className="inline-info">演示模拟记录，不构成真实 AI 研究。</p>}{stage !== 'implementation' && <>{lazy && detail.data && snapshot.stale && !trainingStage && <button className="btn small" onClick={snapshot.refresh}>有新记录，刷新阶段详情</button>}{lazy && !detail.data ? detail.error ? <div role="alert"><p><ReferenceText value={detail.error} /></p><button className="btn" onClick={snapshot.refresh}>重新读取阶段详情</button></div> : <p role="status">正在读取{label}详情…</p> : <ViewBoundary key={`${run.id}:${stage}`} label={label}><StageDetailBody stage={stage} step={selectedStep} data={selectedData} run={run} /></ViewBoundary>}</>}</div></section></div></ReferenceNames>;
}
