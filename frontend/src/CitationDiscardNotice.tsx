import { ReferenceText } from './ReferenceNames';
import { humanizeMessage } from './localization';
import { textOf, type Json } from './api';

const domains: Record<string, string> = { task_paper_or_source: '本任务论文或来源', verified_task_paper: '本任务已核验论文' };

export function CitationDiscardNotice({ audit }: { audit?: Json | null }) {
  if (!audit || audit.version !== 1 || !Array.isArray(audit.discarded) || !audit.discarded.length) return null;
  return <section className="role-amendments citation-discard-notice" aria-label="废弃引用审计"><h4>已废弃 {audit.discarded.length} 处无法核对的引用</h4><p>原始回复、评审正文与反对意见保留。以下编号不再作为支持证据；清理引用不代表科学论断已验证，也不改变原投票。</p>{audit.discarded.map((item: Json, index: number) => <article key={index}><dl><div><dt>废弃编号</dt><dd><code><ReferenceText value={textOf(item?.original_id) || '未记录'} /></code></dd></div><div><dt>原位置</dt><dd><code><ReferenceText value={textOf(item?.path) || '未记录'} /></code></dd></div><div><dt>引用范围</dt><dd><ReferenceText value={domains[item?.domain] ?? humanizeMessage(textOf(item?.domain) || '未记录')} /></dd></div><div><dt>废弃原因</dt><dd><ReferenceText value={humanizeMessage(textOf(item?.reason) || '未记录')} /></dd></div></dl></article>)}<details><summary>查看完整引用审计</summary><pre tabIndex={0}><ReferenceText value={audit} /></pre></details></section>;
}
