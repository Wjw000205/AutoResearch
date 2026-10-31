import { ReferenceText, ReferenceNames } from './ReferenceNames';
import { Database, BookOpen } from 'lucide-react';
import { type Json } from './api';

export function LiteratureRounds({ literature }: { literature?: Json | null }) {
  if (literature?.literature_protocol !== 2) return null;
  const rounds: Json[] = literature.rounds ?? [];
  const window = literature.date_policy;
  return <ReferenceNames records={literature}><section className="literature-rounds" aria-label="两轮论文分析"><header><h3><BookOpen size={17} />两轮论文分析</h3><p>论文与分析保存在持久记忆中。三个 agent 交接索引，按需检索、读取片段。</p>{literature.venue_policy?.enabled && <p><strong>文献范围：仅 CCF A / B</strong> · 2026 第七版 · 两轮分析与 RAG 均按已核验发表出处筛选</p>}</header><div className="literature-round-grid">{[
    { stage: 'literature', title: '第一轮 · 近期论文，寻找 idea', purpose: '关注最新研究问题、方法架构、训练流程与局限；把同一份文献索引交给三位 agent 独立提出创新点。' },
    { stage: 'related_work', title: '第二轮 · 围绕 idea，寻找相关工作', purpose: '选定 idea 后按机制与结构检索相近工作、经典源头和强基线。DLinear、PatchTST 等在相关时纳入，不设年代门槛。' },
  ].map(item => {
    const result = rounds.find(round => round.stage === item.stage);
    const index = result?.literature_index;
    return <article key={item.stage} className="literature-round-card"><h4><ReferenceText value={item.title} /></h4><p><ReferenceText value={item.purpose} /></p>{item.stage === 'literature' && <small>{window ? `截至 ${window.as_of_date}，优先 ${window.preferred_from} 之后，主体窗口 ${window.date_from} 至 ${window.date_to}。` : '运行时确定日期窗口：优先近 6 个月，主体限定近 12 个月。'}</small>}<strong className="literature-round-status">{index ? result?.provenance === 'demo' ? '演示索引 · 未执行真实论文研究' : '分析已保存，按索引读取' : '等待本轮分析完成'}</strong>{index && <details><summary><Database size={14} />持久记忆索引</summary><dl><dt>分析索引</dt><dd><span><ReferenceText value={index.title || item.title} /></span></dd><dt>记忆文档</dt><dd><span><ReferenceText value={index.title || item.title} /></span></dd><dt>索引分块</dt><dd>{index.chunk_count}</dd></dl><p>可在“持久化记忆”中搜索论文名称或方法关键词；agent 使用同一索引和来源定位。</p></details>}{item.stage === 'related_work' && result?.analysis?.related_papers?.length > 0 && <details><summary>最相关论文的比较记录 · {result?.analysis?.related_papers.length} 篇</summary>{result?.analysis?.related_papers.map((paper: Json) => <div className="related-paper-comparison" key={paper.paper_id}><code><ReferenceText value={paper.paper_id} /></code><p>用途：<ReferenceText value={paper.role} /></p><p>相关性：<ReferenceText value={paper.relevance_to_idea} /></p><p>重合点：<ReferenceText value={paper.overlap} /></p><p>差异：<ReferenceText value={paper.difference} /></p><p>待核验：<ReferenceText value={paper.required_check} /></p></div>)}</details>}</article>;
  })}</div></section></ReferenceNames>;
}
