import { ReferenceText } from './ReferenceNames';
import { type Json } from './api';
export function NoveltyGate({ gate, papers = [] }: { gate: Json; papers?: Json[] }) {
  const scopedPass = gate.approved && gate.approval_basis === 'no_high_similarity_in_reviewed_scope';
  const title = scopedPass ? '新颖性门禁通过 · 检索范围内未发现高度相似机制'
    : gate.approved ? '相似性审查通过'
    : gate.route === 'supplement_evidence' ? '相似性审查待补证据 · 保留当前候选'
    : '相似性审查未通过 · 退回重新思考';
  return <section className="novelty-gate" aria-label="创新点相似性审查">
    <h4><ReferenceText value={title} /></h4><p><ReferenceText value={gate.scope} /></p>
    {scopedPass && <p>按用户排重标准放行；原始模型意见保留。后续仍须设计审查、三方共识与真实实验。</p>}
    {(gate.evidence_issues ?? []).map((issue: string) => <p key={issue}><ReferenceText value={issue} /></p>)}
    {(gate.votes ?? []).map((vote: Json) => <article key={vote.role}>
      <strong>{{ director: '研究总监', engineer: '实验工程师', scout: '文献研究员' }[vote.role as string] ?? vote.role}</strong>
      <p><ReferenceText value={vote.rationale} /></p>
      {(vote.closest_work ?? []).map((work: Json, i: number) => <div key={i}>
        <strong><ReferenceText value={papers.find(p => p.id === work.paper_id)?.title ?? work.paper_id} /></strong>
        <p>重合：<ReferenceText value={work.overlap} /></p><p>差异：<ReferenceText value={work.difference} /></p><small>原文定位：{work.evidence_locator}</small>
      </div>)}
      {!!vote.required_changes?.length && <><p><ReferenceText value={scopedPass ? '保留的后续核查与设计问题' : '待解决问题'} /></p>
        <ul>{vote.required_changes.map((change: string, i: number) => <li key={i}>{change}</li>)}</ul></>}
      {vote.limitations && <p>局限：<ReferenceText value={vote.limitations} /></p>}
    </article>)}
  </section>;
}

