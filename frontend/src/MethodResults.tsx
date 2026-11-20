import { ChevronDown, Star } from 'lucide-react';
import type { Json } from './api';
import { methodSections } from './MethodConfiguration';
import { parameterName, parameterValue } from './ParameterMatrix';

const value = (v: unknown) => typeof v === 'number' && Number.isFinite(v)
  ? v !== 0 && Math.abs(v) < 1e-6 ? v.toExponential(3) : v.toFixed(6).replace(/0+$/, '').replace(/\.$/, '') : '—';
const variants: Record<string, string> = {baseline: '主基线', proposed: '候选方法', ablation: '对照 / 消融'};
const phases: Record<string, string> = {validation: '验证集', test: '独立测试集'};
const sources: Record<string, string> = {real_experiment_execution: '真实训练', fixture: '测试样例',
  offline_fixture: '离线测试样例', simulation: '模拟记录', demo: '演示记录'};

export function MethodResults({ groups, experiments, datasets, metric, direction, metrics }: {
  groups: Json[]; experiments: Json[]; datasets: Json[]; metric: string; direction?: string; metrics: string[];
}) {
  const sections = methodSections(groups, experiments, metric, direction);
  return <section className="method-configurations" aria-label="各方法配置矩阵">
    <div className="result-table-heading"><h4>各方法结果 · 配置矩阵</h4>
      <span>{metric} {direction === 'maximize' ? '越高越好' : direction === 'minimize' ? '越低越好' : '方向未记录'}</span></div>
    <p className="result-note">变化参数按列展开。★ 最佳仅在同方法、同版本、同划分和同一种子集合内比较。</p>
    {sections.map(section => {
      const method = section.rows[0].group;
      const dataset = datasets.find(d => d.key === method.dataset_key);
      const winners = section.rows.filter(row => row.winner);
      const winnerScopes = new Set(winners.map(row => row.scope)).size;
      return <details className="method-configuration-block" key={section.id}>
        <summary><h5>{method.name}</h5><span className="result-tag">{variants[method.variant] ?? method.variant ?? '方法未标注'}</span>
          {winnerScopes === 1 && <span className="method-config-highlight"><Star size={12} fill="currentColor" aria-hidden="true" />
            种子 {winners[0].group.seeds.join('、')} · {winners.length > 1 ? '并列最佳' : '最佳'} {metric} {value(winners[0].group.metrics[metric].mean)}</span>}
          {winnerScopes > 1 && <span className="method-config-highlight">{winnerScopes} 个可比组各有最佳配置</span>}
          <span className="method-config-count">{section.rows.length} 组配置 <ChevronDown size={15} aria-hidden="true" /></span>
          <span className="method-config-meta">{phases[method.phase] ?? '划分未记录'} · {method.cycle ? `第 ${method.cycle} 轮 · ` : ''}v{method.code_revision ?? '未记录'} · {method.loss ?? '训练损失未记录'}
            {datasets.length > 1 && dataset && ` · ${dataset.name} · 预测 ${dataset.horizon ?? '未记录'}`}</span></summary>
        <div className="table-scroll result-method-table" tabIndex={0} role="region" aria-label={`${method.name} 配置矩阵`}>
          <table><caption>{method.name} 的配置、训练种子与原始指标</caption>
            <thead><tr><th scope="col">配置</th><th scope="col">训练种子</th>
              {section.columns.map(path => <th scope="col" key={path} title={path}>{parameterName(path)}</th>)}
              {metrics.map(m => <th scope="col" key={m} className={m === metric ? 'method-primary-heading' : undefined}>
                {m}{m === metric ? direction === 'maximize' ? ' ↑' : direction === 'minimize' ? ' ↓' : '' : ''}</th>)}
              <th scope="col">完成记录 / 详情</th></tr></thead>
            <tbody>{section.rows.map((row, index) => {
              const group = row.group;
              return <tr key={group.id} data-configuration-id={group.id} className={row.winner ? 'method-config-winner' : undefined}>
                <th scope="row"><span className="method-config-number">配置 {index + 1}</span>
                  {row.winner && <span className="method-best-badge"><Star size={10} fill="currentColor" aria-hidden="true" />同种子最佳</span>}
                  {!row.params && <small>配置待核对</small>}</th>
                <td><span className="method-seeds">{group.seeds?.length ? group.seeds.map((s: unknown) => s ?? '未记录').join('、') : '未记录'}</span>
                  {group.seeds?.length > 1 && <small>多种子均值</small>}</td>
                {section.columns.map(path => <td key={path} className="method-parameter" title={path}>
                  {row.values[path] === undefined ? <span className="method-unset" title="原记录未显式保存此参数">—</span> : parameterValue(row.values[path])}
                </td>)}
                {metrics.map(m => <td key={m} className={`result-number ${m === metric ? 'method-primary-value' : ''}`}>
                  {value(group.metrics?.[m]?.mean)}
                  {group.metrics?.[m]?.mean == null && group.metrics?.[m] && <small>{group.metrics[m].count}/{group.metrics[m].total} 已报告</small>}</td>)}
                <td>{group.completed} / {group.total}{(group.issues ?? []).map((issue: string, i: number) => <small className="warn-text" key={i}>{issue}</small>)}
                  <details className="result-source"><summary>对应实验与版本<ChevronDown size={12} /></summary>
                    <p>{sources[group.provenance] ?? group.provenance ?? '来源未记录'} · 配置 {group.id} · 代码版本 {group.code_revision ?? '未记录'}</p>
                    {(group.trial_ids ?? []).map((id: string) => <code key={id}>{id}</code>)}
                    <p>代码 SHA</p><code>{group.code_sha256 ?? '未记录'}</code>
                    <p>配置参数</p>{row.params ? <pre>{JSON.stringify(row.params, null, 2)}</pre> : <span>当前快照缺少匹配的完整配置。</span>}
                  </details></td>
              </tr>;
            })}</tbody>
          </table>
        </div>
      </details>;
    })}
    <p className="result-note">— 表示原记录未显式保存此参数。每格分数沿用该配置已保存的种子均值，最佳标记为描述性结果。</p>
  </section>;
}
