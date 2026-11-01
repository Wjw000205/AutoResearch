import { ReferenceText, ReferenceNames } from './ReferenceNames';
import { ArrowDownToLine, BookOpen, ExternalLink, FileText, GitBranch, ShieldCheck } from 'lucide-react';
import { safeUrl, type Json } from './api';

export interface ProposalSourceProps {
  papers?: Json[];
  references?: Json[];
  onExportProposal?: (proposal: Json, proposalHash?: string) => void;
}

const record = (value: unknown): Json | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Json : null;
const records = (value: unknown): Json[] => Array.isArray(value) ? value.map(record).filter((item): item is Json => item !== null) : [];
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && !!item.trim()) : [];
const written = (value: unknown, fallback = '未记录') => typeof value === 'string' && value.trim() ? value : fallback;
const relations: Record<string, string> = { supports: '支持', contradicts: '反证', context: '背景' };
const levels: Record<string, string> = { metadata: '元数据', abstract: '摘要', full_text: '全文' };
const claimStatuses: Record<string, string> = { hypothesis: '待验证', supported: '已有支持', refuted: '被反驳' };
const risks: Record<string, string> = { duplicate: '相同想法风险', high: '高相似风险', medium: '中等相似风险', low: '低相似风险' };
const conclusions: Record<string, string> = { duplicate_found: '发现相同想法', high_overlap: '与已有工作高度重合', differentiated: '已记录差异化依据', insufficient_evidence: '查重证据不足' };

function PaperSource({ id, papers = [], references = [] }: { id: unknown } & Pick<ProposalSourceProps, 'papers' | 'references'>) {
  const sourceId = written(id, '未记录来源标识');
  // Exact task-scoped identifiers only; neither titles nor model-supplied URLs establish source identity.
  const matches = typeof id === 'string' && id ? [
    ...papers.filter(item => record(item)?.id === id).map(item => ({ item, uploaded: false })),
    ...references.filter(item => record(item)?.id === id).map(item => ({ item, uploaded: true })),
  ] : [];
  const match = matches.length === 1 ? matches[0] : undefined;
  const url = match ? safeUrl(match.item.url) : undefined;
  return <ReferenceNames records={[papers, references]}><div className="proposal-paper-source" data-paper-id={sourceId}>
    <code><ReferenceText value={sourceId} /></code>
    {match ? <>
      {url ? <a href={url} target="_blank" rel="noreferrer"><ReferenceText value={written(match.item.title, '查看来源记录')} /><ExternalLink size={12} /></a> : <span><ReferenceText value={written(match.item.title, '来源未记录标题')} /></span>}
      <small>{match.uploaded ? '用户参考资料，上传不等于核验' : match.item.verified === true ? '来源标识与元数据已核验' : '来源标识与元数据尚未核验'}</small>
    </> : <small className="warn-text">{matches.length > 1 ? '当前任务有多个同标识来源，需核对对应记录' : '未在当前任务来源记录中匹配到此标识'}</small>}
  </div></ReferenceNames>;
}

function Field({ label, value }: { label: string; value: unknown }) {
  return <div className="proposal-field"><span>{label}</span><p><ReferenceText value={written(value)} /></p></div>;
}

export function ProblemReview({ review, papers = [], references = [] }: { review?: unknown } & Pick<ProposalSourceProps, 'papers' | 'references'>) {
  const assessment = record(review);
  const evidenceIds = strings(assessment?.evidence_ids);
  const grounded = assessment?.grounded === true ? '该角色认为有证据支持' : assessment?.grounded === false ? '该角色认为证据不足' : '未记录';
  const necessary = assessment?.necessary === true ? '该角色认为需要新方法' : assessment?.necessary === false ? '该角色认为尚无必要' : '未记录';
  return <section className="problem-review" aria-label="痛点与必要性独立评议"><h5>痛点与必要性独立评议</h5><small>以下为该角色的评议声明，不等于痛点、必要性或科学主张已获事实核验。</small>{!assessment && <p>该角色尚未记录痛点与必要性评议。</p>}<div className="provenance-grid"><Field label="是否基于证据" value={grounded} /><Field label="新方法的必要性" value={necessary} /><Field label="更简单的替代方案" value={assessment?.baseline_alternative} /><Field label="反驳条件" value={assessment?.falsification} /><Field label="评议理由" value={assessment?.rationale} /></div><div className="proposal-field"><span>评议引用的证据</span>{evidenceIds.length ? evidenceIds.map((id, index) => <PaperSource key={index} id={id} papers={papers} references={references} />) : <p>未记录</p>}</div></section>;
}

export function ProposalProvenance({ proposal, proposalHash, papers = [], references = [], onExportProposal }: { proposal?: Json | null; proposalHash?: string } & ProposalSourceProps) {
  const value = record(proposal);
  if (!value) return <section className="proposal-provenance provenance-missing" aria-label="想法依据与论文故事"><h4><GitBranch size={16} />想法依据与论文故事</h4><p>尚未形成可展示的提案。</p></section>;
  proposal = value;
  const rationale = record(proposal.idea_rationale);
  const problem = record(rationale?.problem_validation);
  const story = record(proposal.paper_story);
  const novelty = record(proposal.novelty_review);
  const sourceProps = { papers, references };
  const evidence = records(rationale?.evidence);
  const claims = records(story?.claims);
  const closest = records(novelty?.closest_work);
  const storySteps: [string, unknown][] = [['研究问题', story?.problem], ['尚未解决的缺口', story?.gap], ['可检验的假设', story?.hypothesis], ['方法如何回应问题', story?.method]];
  const proposalRisks = strings(proposal.risks);
  if (!rationale && !story && !novelty) return <section className="proposal-provenance provenance-missing" aria-label="想法依据与论文故事"><h4><GitBranch size={16} />想法依据与论文故事</h4><p>历史提案未记录这些信息。</p><small>提出理由、文献佐证、相似工作对比与故事线均以已保存的提案为准。</small></section>;
  return <section className="proposal-provenance" aria-label="想法依据与论文故事">
    <header className="proposal-provenance-heading"><div><h4><GitBranch size={16} />想法依据与论文故事</h4><p>记录为什么提出这个想法，以及每项主张仍需怎样验证。</p></div>{onExportProposal && proposalHash && <button type="button" className="btn small" onClick={() => onExportProposal(proposal, proposalHash)}><ArrowDownToLine size={14} />下载想法论证材料</button>}</header>

    <section className="provenance-card" aria-label="为什么提出这个想法"><h4>为什么提出这个想法</h4>{rationale ? <div className="provenance-grid"><Field label="想法来源" value={rationale.origin} /><Field label="提出理由" value={rationale.motivation} /><Field label="前作留下的差距" value={rationale.gap} /><Field label="预期起作用的机制" value={rationale.mechanism} /><Field label="与已有方法的区别" value={rationale.differentiation} /></div> : <p>历史提案未记录提出理由。</p>}</section>

    <section className="provenance-card" aria-label="痛点与必要性"><h4>痛点与必要性</h4><p className="muted">以下为提案记录；痛点是否真实存在、是否需要新方法，仍需证据与对照验证。</p>{!problem && <p>提案尚未记录痛点与必要性分析。</p>}<div className="provenance-grid"><Field label="真实使用或研究场景" value={problem?.target_scenario} /><Field label="受影响对象" value={problem?.affected_users} /><Field label="可观察的失败" value={problem?.observable_failure} /><Field label="问题为何重要" value={problem?.why_it_matters} /><Field label="现有解法" value={problem?.existing_solutions} /><Field label="现有解法的不足" value={problem?.why_insufficient} /><Field label="是否真的需要新方法" value={problem?.necessity_check} /><Field label="什么结果出现时应放弃" value={problem?.abandonment_rule} /></div><div className="proposal-field"><span>痛点引用的证据</span>{strings(problem?.evidence_ids).length ? strings(problem?.evidence_ids).map((id, index) => <PaperSource key={index} id={id} {...sourceProps} />) : <p>未记录</p>}</div></section>

    <section className="provenance-card" aria-label="论文依据与反证"><h4><BookOpen size={16} />论文依据与反证</h4><p className="muted">支持关系与阅读层级按提案记录展示；来源身份核验不等于论文结论、创新性或实验效果已获验证。</p>{evidence.length ? <div className="proposal-evidence-list">{evidence.map((item, index) => <article key={index} className={`proposal-evidence relation-${Object.hasOwn(relations, item.relation) ? item.relation : 'unknown'}`}><header><strong>证据 {index + 1}</strong><span className="pill">{relations[item.relation] ?? '关系未记录'}</span><span>提案记录阅读层级：{levels[item.evidence_level] ?? '未记录'}</span></header><PaperSource id={item.paper_id} {...sourceProps} /><Field label="与本想法相关的主张" value={item.claim} /><Field label="页码、章节或内容定位" value={item.locator} /><Field label="证据局限" value={item.limitation} /></article>)}</div> : <p>提案尚未记录逐篇论文佐证。</p>}</section>

    <section className="provenance-card proposal-novelty" aria-label="相近工作与查重范围"><h4><ShieldCheck size={16} />相近工作与查重范围</h4>{novelty ? <>
      <div className={`proposal-novelty-conclusion novelty-${Object.hasOwn(conclusions, novelty.conclusion) ? novelty.conclusion : 'unknown'}`} role="status"><strong>{conclusions[novelty.conclusion] ?? '查重结论未记录'}</strong><p>这是已记录检索范围内的判断，不能保证全网没有相同或高相似研究。</p></div>
      <Field label="记录的检索截止日期" value={novelty.search_cutoff} /><div className="proposal-field"><span>检索问题与范围</span>{strings(novelty.search_queries).length ? <ul>{strings(novelty.search_queries).map((query, index) => <li key={index}>{query}</li>)}</ul> : <p>未记录检索范围</p>}</div>
      {closest.length ? <div className="proposal-evidence-list">{closest.map((work, index) => <article key={index}><header><strong>相近工作 {index + 1}</strong><span className={`pill novelty-risk-${Object.hasOwn(risks, work.risk) ? work.risk : 'unknown'}`}>{risks[work.risk] ?? '相似风险未记录'}</span></header><PaperSource id={work.paper_id} {...sourceProps} /><Field label="重合之处" value={work.overlap} /><Field label="具体差异" value={work.differences} /><Field label="差异为何具有意义" value={work.why_distinct} /><Field label="还需核验什么" value={work.required_check} /></article>)}</div> : <p>尚未记录可供对比的相近工作；不能据此认为没有重复。</p>}
      <div className="proposal-field"><span>查重局限</span>{strings(novelty.limitations).length ? <ul>{strings(novelty.limitations).map((limitation, index) => <li key={index}>{limitation}</li>)}</ul> : <p>未记录</p>}</div>
    </> : <p>历史提案未记录相近工作对比与查重范围。</p>}</section>

    <section className="provenance-card proposal-paper-story" aria-label="论文故事线"><h4><FileText size={16} />论文故事线</h4>{story ? <>
      <p className="muted">问题 → 缺口 → 假设 → 方法 → 验证 → 贡献与限制</p>
      <ol className="proposal-story-steps">{storySteps.map(([label, value], index) => <li className="story-step" key={label}><span className="story-step-number">{index + 1}</span><Field label={label} value={value} /></li>)}<li className="story-step"><span className="story-step-number">5</span><div className="proposal-field"><span>怎样验证</span>{claims.length ? <ol>{claims.map((claim, index) => <li key={index}><strong>主张 {index + 1}</strong><p><ReferenceText value={written(claim.validation_plan, '未记录验证计划')} /></p></li>)}</ol> : <p>尚未记录主张对应的验证计划</p>}</div></li><li className="story-step"><span className="story-step-number">6</span><div className="proposal-field"><span>拟贡献与当前主张</span><p className="muted">以下状态来自提案声明；“已有支持”不等于已证明，也不能替代实验记录。</p>{claims.length ? <div className="proposal-evidence-list">{claims.map((claim, index) => <article key={index}><header><strong>主张 {index + 1}</strong><span className={`pill claim-${Object.hasOwn(claimStatuses, claim.status) ? claim.status : 'unknown'}`}>{claimStatuses[claim.status] ?? '状态未记录'}（提案声明）</span></header><p><ReferenceText value={written(claim.claim)} /></p>{strings(claim.evidence_ids).length ? strings(claim.evidence_ids).map((id, idIndex) => <PaperSource key={idIndex} id={id} {...sourceProps} />) : <small>尚未关联文献来源</small>}</article>)}</div> : <p>尚未记录拟贡献主张</p>}</div></li></ol>
      <div className="proposal-field"><span>适用边界与限制</span>{proposalRisks.length ? <ul>{proposalRisks.map((risk, index) => <li key={index}>{risk}</li>)}</ul> : <p>提案未单独记录风险限制；请结合上方逐条证据局限核对。</p>}{typeof proposal.publication_gap === 'string' && proposal.publication_gap.trim() && <p><ReferenceText value={proposal.publication_gap} /></p>}</div>
      <div className="provenance-grid"><div className="proposal-field"><span>论文结构草案</span>{strings(story.outline).length ? <ol>{strings(story.outline).map((section, index) => <li key={index}>{section}</li>)}</ol> : <p>未记录</p>}</div><div className="proposal-field"><span>候选标题</span>{strings(story.title_candidates).length ? <ul>{strings(story.title_candidates).map((title, index) => <li key={index}><ReferenceText value={title} /></li>)}</ul> : <p>未记录</p>}</div></div>
    </> : <p>历史提案未记录论文故事线。</p>}</section>
    <details className="proposal-provenance-raw"><summary>原始想法论证记录</summary><pre tabIndex={0}><ReferenceText value={{ idea_rationale: proposal.idea_rationale, novelty_review: proposal.novelty_review, paper_story: proposal.paper_story }} /></pre></details>
  </section>;
}
