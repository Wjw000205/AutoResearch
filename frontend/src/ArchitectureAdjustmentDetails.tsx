import { useState } from 'react';
import type { Json } from './api';
import { ReferenceText } from './ReferenceNames';
import { useRemoteResource } from './useRemoteResource';

const status: Record<string, string> = { implementing: '交付收集中', reviewing: '代码审核中', checking: 'CPU 检查中',
  review_blocked: '审核未通过，等待定向处理', rejected: '代码审核未通过', invalid: '交付无效',
  check_failed: 'CPU 检查未通过', activated: '已生效', diagnostic_checked: '仅完成候选诊断检查',
  superseded_before_dispatch: '旧证据提案已归档，未派发' };
const roles: Record<string, string> = {director:'研究总监', engineer:'实验工程师', scout:'文献研究员'};
const operations: Record<string, string> = {added:'新增', modified:'修改', retained:'保留原文件', unknown:'对照缺失，待核验'};
const kinds: Record<string, string> = {runtime:'运行源码 / 配置',check:'检查脚本',metadata:'交付材料'};
const date = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '历史记录';

export function AdjustmentContent({ item }: {item: Json}) {
  const counts = item.file_counts ?? {};
  const changed = (kind: string) => (counts[kind]?.added ?? 0) + (counts[kind]?.modified ?? 0);
  const files: Json[] = [...(item.files ?? [])].sort((a,b) => Number(a.operation==='retained')-Number(b.operation==='retained') || a.path.localeCompare(b.path));
  const checkAccepted = ['activated','diagnostic_checked'].includes(item.status) && item.post_exit?.post_exit_verified && item.post_exit?.candidate_behavior_passed;
  return <div className="adjustment-content">
    <div className="adjustment-state" role="status"><strong>{status[item.status] ?? '等待处理'}</strong><span>{item.is_current_active ? '当前生效版本' : item.historically_activated ? '历史已生效版本，当前已被后续版本替代' : '候选尚未生效'}</span></div>
    {item.error && <p className="inline-error"><ReferenceText value={item.error}/></p>}
    <h4>修改了什么</h4>
    <p className="muted">以下是这份修改单的要求；实际交付和验收状态列在下方。</p>
    {(item.changes ?? []).map((change: Json, index: number) => <article className="adjustment-change" key={index}>
      <h5>{index + 1}. <ReferenceText value={change.target}/></h5>
      <dl className="adjustment-comparison"><div><dt>修改前</dt><dd><ReferenceText value={change.before}/></dd></div><div><dt>修改后要求</dt><dd><ReferenceText value={change.after}/></dd></div></dl>
      {change.reason && <p className="adjustment-reason"><strong>修改原因：</strong><ReferenceText value={change.reason}/></p>}
    </article>)}
    {!item.changes?.length && <p>这份记录没有保存具体修改项。</p>}
    <h4>实际交付</h4>
    {item.candidate_delivered ? <>
      {item.comparison_basis==='unavailable' ? <p>已保存 {files.length} 个候选文件；这份历史记录缺少完整对照清单，无法确认本次改动数量。</p> : <p>{item.comparison_basis==='previous_candidate' ? '与上一份未生效候选对比' : '与原生效控制版本对比'}：运行源码 / 配置变化 <strong>{changed('runtime')}</strong> 个，保留 {counts.runtime?.retained ?? 0} 个；检查脚本变化 {changed('check')} 个，交付材料变化 {changed('metadata')} 个。</p>}
      <details className="adjustment-files"><summary>涉及文件与摘要（{files.length} 个，改动优先）</summary><ul>{files.map(file => <li key={file.path}><div><span>{operations[file.operation]}</span><small>{kinds[file.kind]}</small></div><code>{file.path}</code><details><summary>查看文件身份</summary><code>当前 SHA256：{file.sha256}</code>{file.previous_sha256 && <code>对照 SHA256：{file.previous_sha256}</code>}</details></li>)}</ul></details>
    </> : <p>尚未形成可审核的候选交付，不能把修改单当作已实现。</p>}
    <h4>审核与检查</h4>
    <div className="adjustment-reviews">{(item.reviews ?? []).map((review: Json) => <article key={review.role}><header><strong>{roles[review.role] ?? review.role}</strong><span>{review.approve===true ? '同意本候选' : review.approve===false ? '未通过' : review.status==='running' ? '审核中' : review.status==='interrupted' ? '审核已中断' : '尚无最终意见'}</span></header>{review.rationale && <p><ReferenceText value={review.rationale}/></p>}{review.issues?.length>0 && <ul>{review.issues.map((issue: unknown, index: number) => <li key={index}><ReferenceText value={issue}/></li>)}</ul>}</article>)}</div>
    <p>CPU 检查：{item.check ? item.check.status==='completed' ? checkAccepted ? '检查通过，断言及退出后证据已核验' : '运行完成，等待宿主最终核验' : `未通过（${item.check.status}）` : item.status==='checking' ? '检查准备或执行中，最终回执待生成' : '尚未执行'}</p>
    {item.check?.timeout_seconds != null ? <p>本次检查时间上限：{item.check.timeout_seconds} 秒；实际用时：{item.check.duration_seconds ?? '未记录'} 秒。</p>
      : item.check_budget && <p>{item.status==='checking' ? '本次完整验收时限：' : '完整验收申请时限：'}{item.check_budget.timeout_seconds} 秒{item.status==='checking' ? '。' : '，独立审核后生效。'}</p>}
    {item.post_exit && <p>退出后证据复核：{item.post_exit.post_exit_verified ? '产物身份已核验' : '尚未通过'}{item.post_exit.error && <> · <ReferenceText value={item.post_exit.error}/></>}</p>}
    <details><summary>验收要求与停止条件</summary><p><ReferenceText value={item.verification}/></p><p><ReferenceText value={item.stop_condition}/></p></details>
    <details><summary>修改单来源与原始记录</summary><p>创建于 {date(item.created_at)}</p><code>{item.id}</code><code>{item.decision_id}</code>{item.runtime_sha256 && <code>候选运行包 SHA256：{item.runtime_sha256}</code>}<pre><ReferenceText value={item}/></pre></details>
  </div>;
}

export function ArchitectureAdjustmentDetails({ runId, value }: {runId: string; value?: Json | null}) {
  const [selected, setSelected] = useState('current');
  const remote = useRemoteResource<Json>(selected==='current' ? null : `/runs/${encodeURIComponent(runId)}/architecture-adjustments/${encodeURIComponent(selected)}`, selected);
  const item = selected==='current' ? value?.current : remote.data;
  return <section className="architecture-adjustments" aria-label="架构微调修改内容">
    <h3>本阶段修改内容</h3>
    <label className="adjustment-selector">查看修改单<select value={selected} onChange={event=>setSelected(event.target.value)}><option value="current">当前处理的修改单</option>{(value?.history ?? []).filter((entry: Json)=>entry.id!==value?.current?.id).map((entry: Json)=><option key={entry.id} value={entry.id}>{entry.attempt ? `第 ${entry.attempt} 次 · ` : ''}{date(entry.created_at)} · {status[entry.status] ?? '处理中'}</option>)}</select></label>
    {selected==='current' && value?.current_attempt && value.current_attempt!==value.attempts_total && <p className="muted">当前恢复处理第 {value.current_attempt} 次修改的交付；累计修改尝试 {value.attempts_total} 次。</p>}
    {item ? <AdjustmentContent item={item}/> : remote.error ? <p role="alert">读取修改内容失败：{remote.error}</p> : remote.loading ? <p role="status">正在读取所选修改单…</p> : <p>当前还没有可显示的具体修改单。</p>}
  </section>;
}
