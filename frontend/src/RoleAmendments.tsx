import { ReferenceText } from './ReferenceNames';
import { Wrench } from 'lucide-react';
import { textOf, type Json } from './api';

const roleNames: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };
const fieldNames: Record<string, string> = { hypothesis: '研究假设', novelty: '创新定位', method: '研究方法', baseline: '比较基线', metrics: '评估指标', risks: '风险', resource_plan: '资源计划', data_plan: '数据计划', references: '引用资料', publication_gap: '投稿目标差距' };
const fixesOf = (value: unknown): Json[] => Array.isArray(value) ? value.filter(item => item && typeof item === 'object' && !Array.isArray(item)) : [];
const sameJson = (left: unknown, right: unknown): boolean => {
  if (left === right) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  if (Array.isArray(left) || Array.isArray(right)) return Array.isArray(left) && Array.isArray(right) && left.length === right.length && left.every((value, index) => sameJson(value, right[index]));
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every(key => Object.hasOwn(right, key) && sameJson((left as Json)[key], (right as Json)[key]));
};
const amendmentLabel = (origin: unknown) => origin === 'independent_vote' ? '来自本轮独立评议' : origin === 'legacy_vote_supplement' ? '由本角色后补提交' : origin ? `角色修改方案 · 来源：${textOf(origin)}` : '角色修改方案（来源未记录）';

export function RoleAmendments({ vote, amendments = [] }: { vote: Json; amendments?: Json[] }) {
  const own = typeof vote.role === 'string' ? amendments.filter(record => record.role === vote.role) : [];
  const voteFixes = fixesOf(vote.fixes);
  const groups = [{ label: '随本轮评议提交', fixes: voteFixes, required: vote.required_changes }, ...own.map(record => {
    // Only suppress identical projections of this role's own vote, including its objection context.
    const sameVote = record.origin === 'independent_vote' && Array.isArray(vote.required_changes) && sameJson(record.required_changes, vote.required_changes);
    const fixes = fixesOf(record.fixes).filter(fix => !sameVote || !voteFixes.some(original => sameJson(original, fix)));
    return { label: amendmentLabel(record.origin), fixes, required: record.required_changes };
  })].filter(group => group.fixes.length > 0);
  if (!groups.length && vote.approve === true && !(Array.isArray(vote.required_changes) && vote.required_changes.length)) return null;
  const author = (roleNames[vote.role] ?? textOf(vote.role)) || '未记录角色';
  return <section className="role-amendments" data-author-role={vote.role} aria-label={`${author}的修改方案`}><h4><Wrench size={14} /><ReferenceText value={author} />的修改方案</h4>{groups.length ? groups.map((group, groupIndex) => {
    const objections = Array.isArray(group.required) ? group.required.map(textOf) : [];
    return <div className="role-amendment-group" key={groupIndex}><small>{group.label}</small>{group.fixes.map((fix, index) => {
      const objectionIndex = fix.objection_index;
      const matched = Number.isInteger(objectionIndex) && objectionIndex >= 0 && objectionIndex < objections.length;
      return <article key={index}><div className="amendment-objection"><strong>{matched ? `对应异议 ${objectionIndex + 1}` : '对应异议未匹配'}</strong><p><ReferenceText value={matched ? objections[objectionIndex] || '该条异议未记录文本' : `原始异议索引：${textOf(objectionIndex) || '未记录'}`} /></p></div><dl><div><dt>修改位置</dt><dd><ReferenceText value={(fieldNames[fix.target_field] ?? textOf(fix.target_field)) || '未记录'} /></dd></div><div><dt>具体修改方案</dt><dd><ReferenceText value={textOf(fix.proposed_change) || '未记录'} /></dd></div><div><dt>验证办法</dt><dd><ReferenceText value={textOf(fix.verification_plan) || '未记录'} /></dd></div><div data-reference-kind="literature"><dt>文献来源 ID</dt><dd>{Array.isArray(fix.evidence_ids) && fix.evidence_ids.length ? fix.evidence_ids.map((id: unknown, idIndex: number) => <code key={idIndex}><ReferenceText value={id} /></code>) : '未引用证据 ID'}</dd></div>{Array.isArray(fix.context_references) && fix.context_references.length > 0 && <div data-reference-kind="context"><dt>提案/评议上下文</dt><dd>{fix.context_references.map((reference: unknown, referenceIndex: number) => <code key={referenceIndex} title="内部字段路径，不是论文来源标识"><ReferenceText value={reference} /></code>)}</dd></div>}</dl></article>;
    })}</div>;
  }) : <p className="amendment-missing">该角色尚未记录具体修改方案。</p>}</section>;
}
