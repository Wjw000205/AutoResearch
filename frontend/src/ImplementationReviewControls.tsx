import type { ExecutionConfig } from './api';

export function ImplementationReviewControls({ value, onChange }: {
  value: ExecutionConfig; onChange: (key: string, value: number | string | null) => void;
}) {
  return <section className="implementation-review-controls" aria-label="实现文档审计配置">
    <div className="field"><label>实现文档审查方式</label><strong>{value.implementation_review_strategy === 'whole_alignment' ? '历史三方全文协议' : '冻结合同与问题台账验收'}</strong>
      <small>工程师撰写与自检，总监和文献研究员按职责审查。完成必要验收项且阻断问题验证关闭后，才进入代码实现。</small>
      <small>默认集中发现一次、最多两次修复验证。预算耗尽可阻断或转入验证、重新规划；不会自动通过。新合同与新版本继续消耗项目总预算。</small>
    </div>
    {value.implementation_review_strategy !== 'whole_alignment' && <div className="form-grid">
      {([
        ['implementation_review_max_revisions', '修复验证次数上限', 2, 0, 20],
        ['implementation_review_max_calls', '项目审核调用上限', 240, 1, 10000],
        ['implementation_review_max_seconds', '项目审核总时限（秒）', 14400, 1, 604800],
      ] as const).map(([key, label, fallback, min, max]) => <label className="field" key={key}>{label}<input type="number" required={key === 'implementation_review_max_revisions'} placeholder={key === 'implementation_review_max_revisions' ? undefined : '无上限'} min={min} max={max} step="1" value={value[key] === null && key !== 'implementation_review_max_revisions' ? '' : value[key] ?? fallback} onChange={event => onChange(key, event.target.value === '' && key !== 'implementation_review_max_revisions' ? null : Number(event.target.value))} /></label>)}
    </div>}
  </section>;
}
