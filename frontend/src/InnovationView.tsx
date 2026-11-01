import { ReferenceText } from './ReferenceNames';
import { BrainCircuit, Check, ChevronDown, MessageSquareText } from 'lucide-react';
import { textOf, type Json, type Run } from './api';
import './innovation.css';
import { ProposalProvenance } from './ProposalProvenance';
import { NoveltyGate } from './NoveltyGate';

const roles: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };
const fields = [['hypothesis', '可证伪假设'], ['novelty', '创新定位'], ['method', '核心方法'], ['baseline', '比较基线'], ['risks', '风险与未知']] as const;
const agendaLabels: Record<string, string> = { research_question: '研究问题', paradigm_limitation: '近期方法的共同限制', why_simple_fixes_fail: '简单改动为何不够', mechanism_claim: '机制贡献', competing_explanation: '最强竞争解释', decisive_experiment: '关键区分实验', falsification: '否证与停止条件', scope_and_significance: '价值与适用边界' };
const criteriaLabels: Record<string, string> = { important_question: '问题价值', substantive_mechanism: '机制深度', discriminating_test: '实验辨识力', resource_feasible: '资源可行', not_repackaged: '非旧方向包装' };
const record = (value: unknown): Json | undefined => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Json : undefined;
const records = (value: unknown): Json[] => Array.isArray(value) ? value.map(record).filter((item): item is Json => !!item) : [];

function QualityGate({ gate }: { gate?: Json }) {
  if (!gate) return null;
  if (gate.version === 2) {
    const labels: Record<string, string> = { rethink: '退回重想', revise_design: '作者修订设计', supplement_evidence: '保留候选 · 定向补读', focused_reading: '进入相关论文核查' };
    return <div className="innovation-material"><h4>研究价值审查与下一步</h4><p><ReferenceText value={gate.scope} /></p>{Object.entries(gate.routes ?? {}).map(([candidate, route]) => <div key={candidate}><strong>{roles[candidate]}候选 · {labels[String(route)] ?? '等待分类'}</strong>{records(gate.rejections?.[candidate]).map((row: Json) => <div key={row.reviewer}><p><ReferenceText value={roles[row.reviewer]} />：<ReferenceText value={row.reason} /></p><p>具体待办：<ReferenceText value={row.next_question_or_test} /></p></div>)}</div>)}</div>;
  }
  return <div className="innovation-material"><h4>研究价值门禁：<ReferenceText value={gate.approved_for_focused_reading ? '存在可进入聚焦核查的候选' : '全部退回'} /></h4><p>只有三个角色均认可问题价值、机制贡献、区分实验和可行性的候选才参与选择。此判断不证明已达到目标会议水平。</p>{Object.entries(gate.rejections ?? {}).map(([candidate, rows]) => <div key={candidate}><strong>{roles[candidate]}候选</strong>{records(rows).length ? <ul>{records(rows).map((row: Json) => <li key={row.reviewer}>{roles[row.reviewer]}：{(Array.isArray(row.criteria) ? row.criteria : []).map((key: string) => criteriaLabels[key] ?? key).join('、')}未通过。<ReferenceText value={row.reason} /><p><ReferenceText value={row.next_question_or_test} /></p></li>)}</ul> : <p><ReferenceText value={Array.isArray(rows) ? '三方均通过前瞻性审查，仍需相关文献与共识门禁。' : '尚无完整审查记录。'} /></p>}</div>)}</div>;
}

function ResearchAgenda({ agenda }: { agenda?: Json }) {
  if (!agenda) return null;
  return <details open><summary>研究问题与贡献设计</summary><dl>{Object.entries(agendaLabels).map(([key, label]) => <div key={key}><dt>{label}</dt><dd><ReferenceText value={textOf(agenda[key]) || '尚未说明'} /></dd></div>)}</dl></details>;
}

function CandidateCards({ candidates, papers, selected }: { candidates: Json[]; papers: Json[]; selected?: string }) {
  return <div className="innovation-candidates">{Object.entries(roles).map(([role, name]) => {
    const proposal = record(candidates.find(item => item.role === role)?.proposal);
    const progress = record(proposal?.research_progress);
    return <article key={role} className={selected === role ? 'selected' : ''}>
      <div className="innovation-card-heading"><strong><ReferenceText value={name} /></strong>{selected === role && <span className="good-text"><Check size={14} />选定方向</span>}</div>
      <ResearchAgenda agenda={record(proposal?.research_agenda)} />
      {proposal ? <><h4><ReferenceText value={proposal.title} /></h4>{progress && <details><summary>该角色读到的最新进展</summary><p><ReferenceText value={progress.summary} /></p><p>剩余缺口：<ReferenceText value={progress.remaining_gap} /></p><ul>{(Array.isArray(progress.paper_ids) ? progress.paper_ids : []).map((id: string) => <li key={id}><ReferenceText value={papers.find(p => p.id === id)?.title ?? id} /></li>)}</ul></details>}<dl>{fields.map(([field, label]) => <div key={field}><dt>{label}</dt><dd><ReferenceText value={textOf(proposal[field]) || '尚未记录'} /></dd></div>)}</dl><details><summary>创新依据、相近论文与故事线<ChevronDown size={12} /></summary><ProposalProvenance proposal={proposal} papers={papers} /></details><details><summary>完整候选<ChevronDown size={12} /></summary><pre><ReferenceText value={proposal} /></pre></details></> : <p className="muted">尚未收到独立候选</p>}
    </article>;
  })}</div>;
}

function Comparisons({ rows }: { rows: Json[] }) {
  if (!rows.length) return null;
  return <div className="innovation-comparisons"><h4><MessageSquareText size={17} />三方比较意见</h4>{rows.map(row => <article key={row.role}><strong>{roles[row.role] ?? row.role}</strong><p className="innovation-ranking"><ReferenceText value={(Array.isArray(row.ranking) ? row.ranking : []).map((role: string) => roles[role] ?? role).join(' → ')} /></p><p><ReferenceText value={row.rationale} /></p>{records(row.comparisons).map(item => <details key={item.candidate_id}><summary>对<ReferenceText value={roles[item.candidate_id] ?? item.candidate_id} />候选的评议<ChevronDown size={12} /></summary><dl><div><dt>优势</dt><dd><ReferenceText value={item.strength} /></dd></div><div><dt>担忧</dt><dd><ReferenceText value={item.concern} /></dd></div><div><dt>验证建议</dt><dd><ReferenceText value={item.suggested_test} /></dd></div></dl></details>)}</article>)}</div>;
}

export function InnovationView({ session, run, papers = [] }: { session?: Json | null; run: Run; papers?: Json[] }) {
  const candidates = records(session?.candidates);
  const comparisons = records(session?.comparisons);
  const history = records(session?.history);
  const receipts = records(session?.reading_receipts);
  const selected = session?.selection?.candidate_id;
  const complete = session?.status === 'completed';
  return <section className="innovation-view" aria-label="创新点讨论">
    <header><BrainCircuit size={24} /><div><h3>先独立思考，再收束方向</h3><p>三个角色提出候选并独立审查。科学缺陷退回重想，设计缺口交作者修订，证据缺口交定向阅读；选定方向后继续核查。</p></div></header>
    <div className="innovation-progress"><span>{complete ? '已选定讨论方向' : session?.status === 'revising_design' ? '候选作者正在修订设计' : session?.status === 'needs_design_revision' ? '候选已保留，仍有设计缺口待处理' : session?.status === 'repairing_evidence' ? '文献与工程角色正在补齐审查缺口' : session?.status === 'rejected_quality' ? '候选均未通过研究价值门禁' : session?.status === 'comparing' ? '三方正在比较候选' : session ? '正在收集独立候选' : '等待创新点讨论'}</span><span>候选 {candidates.length}/3 · 比较意见 {comparisons.length}/3</span></div>
    {session?.status === 'repairing_evidence' && <p className="inline-info">先定向补读近邻论文并完善机制和区分实验，再由三个角色重新提出候选、独立审查。原始否决记录与预算保留。</p>}
    {['revising_design', 'needs_design_revision'].includes(session?.status) && <p className="inline-info"><ReferenceText value={session?.status === 'revising_design' ? `${roles[session?.design_revision_owner] ?? '候选作者'}正在处理第 ${session?.design_revision_number ?? 1} 轮设计修订。` : '两轮定向修订后仍有设计缺口，当前已暂停。'} />原候选、具体异议与三方复审记录保留；修订不代表批准实验。</p>}
    {run.mode === 'demo' && <p className="inline-info">演示模拟：候选和比较意见为预设响应，不构成真实创新结论。</p>}
    {session && <div className="innovation-material"><h4>讨论前的近期论文进展</h4><p>材料截至 {session.literature_index?.as_of_date ?? '未记录'} · {session.literature_index?.paper_count ?? 0} 篇核心论文</p>{receipts.length ? <ul>{receipts.map(receipt => <li key={receipt.id}>{roles[receipt.role]}：{receipt.completed ? '同一份进展材料已完整读取并交付' : '读取中'}</li>)}</ul> : <p className="muted">历史记录未提供三方完整读取回执，不能据此认定每个角色已读完材料。</p>}</div>}
    {session?.research_brief && <p className="inline-info">本次研究要求：<ReferenceText value={session.research_brief} /></p>}
    {session?.status === 'rethinking' && <p className="inline-info">旧方向已退回，正在进行第 <ReferenceText value={session.attempt} /> 次思考。</p>}
    <QualityGate gate={session?.quality_gate} />
    {!!records(session?.design_revisions).length && <details><summary>设计修订记录（{session?.design_revisions.length} 轮）</summary>{records(session?.design_revisions).map(row => <div key={row.revision}><h4>第 <ReferenceText value={row.revision} /> 轮 · <ReferenceText value={roles[row.candidate_id]} /></h4><QualityGate gate={row.previous_quality_gate} /></div>)}</details>}
    {!session && <p className="muted">文献阶段完成后开始。已有记录会保留；恢复后的任务将按当前检查点继续。</p>}
    <CandidateCards candidates={candidates} papers={papers} selected={complete ? selected : undefined} />
    <Comparisons rows={comparisons} />
    {complete && <div className="innovation-selection"><h4>收束结果：<ReferenceText value={session?.proposal?.title} /></h4><p><ReferenceText value={session?.selection?.rule} /></p><p><ReferenceText value={Object.entries(roles).map(([role, name]) => `${name}候选 ${session?.selection?.scores?.[role] ?? 0} 分`).join(' · ')} /></p><p>所选候选保持原始内容，比较中的担忧交给下一阶段继续处理。选定方向不等于共识通过，也不代表创新性已获验证。</p></div>}
    {session?.proposal && <ProposalProvenance proposal={session.proposal} papers={papers} />}
    {session?.novelty_gate && <NoveltyGate gate={session.novelty_gate} papers={papers} />}
    {!!history.length && <section><h4>退回与重新思考记录</h4>{history.map((row, index) => {
      const proposal = record(row.proposal);
      const priorCandidates = records(row.candidates);
      return <details key={row.id ?? index}><summary>{row.status === 'legacy' ? '升级前的历史讨论' : `第 ${row.attempt ?? index + 1} 次 · 未通过`}<ReferenceText value={proposal?.title ? ` · ${textOf(proposal.title)}` : ' · 未选定方向'} /></summary>{row.upgrade_reason && <p><ReferenceText value={row.upgrade_reason} /></p>}<QualityGate gate={row.quality_gate} />{row.novelty_gate && <NoveltyGate gate={row.novelty_gate} papers={row.related_work?.papers ?? papers} />}
        {proposal ? <ProposalProvenance proposal={proposal} papers={papers} /> : <p className="muted">本轮未选定提案，以下保留各角色的候选与审查意见。</p>}
        {!!priorCandidates.length && <CandidateCards candidates={priorCandidates} papers={papers} />}
        <Comparisons rows={records(row.comparisons)} />
      </details>;
    })}</section>}
  </section>;
}
