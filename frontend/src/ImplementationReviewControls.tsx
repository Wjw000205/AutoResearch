import type { ExecutionConfig } from './api';

export function ImplementationReviewControls({ value, onChange }: {
  value: ExecutionConfig; onChange: (key: string, value: number | string | null) => void;
}) {
  return <section className="implementation-review-controls" aria-label="实现文档审计配置">
    <div className="field"><label>实现审查方式</label><strong>文档审核与代码审核</strong>
      <small>文档通过后开始写代码。总监和研究员独立审核，退回意见交给作者修订。</small>
    </div>
    <div className="form-grid">
      {([
        ['implementation_review_max_revisions', '文档修订次数上限', 2, 0, 20],
        ['implementation_review_max_calls', '项目审核调用上限', 240, 1, 10000],
        ['implementation_review_max_seconds', '项目审核总时限（秒）', 14400, 1, 604800],
      ] as const).map(([key, label, fallback, min, max]) => <label className="field" key={key}>{label}<input type="number" required={key === 'implementation_review_max_revisions'} placeholder={key === 'implementation_review_max_revisions' ? undefined : '无上限'} min={min} max={max} step="1" value={value[key] === null && key !== 'implementation_review_max_revisions' ? '' : value[key] ?? fallback} onChange={event => onChange(key, event.target.value === '' && key !== 'implementation_review_max_revisions' ? null : Number(event.target.value))} /></label>)}
    </div>
  </section>;
}
