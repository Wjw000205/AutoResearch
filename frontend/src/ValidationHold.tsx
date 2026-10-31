import type { Json } from './api';
import { ReferenceText } from './ReferenceNames';

export const validationResumeReason = (run: Json): string | undefined =>
  run.validation_hold?.resume_allowed === false ? run.validation_hold.reason : undefined;

export function validationStageLabel(run: Json, stage: string): string | undefined {
  if (!validationResumeReason(run)) return undefined;
  return ({ validation_review: '评议已保存 · 待核对', revision: '自动修订已拦截',
    final_tests: '未获准执行' } as Record<string, string>)[stage];
}

const roles: Record<string, string> = {director: '研究总监', engineer: '实验工程师', scout: '文献研究员'};
const percent = (value: unknown) => typeof value === 'number' && Number.isFinite(value)
  ? `${(value * 100).toFixed(2)}%` : '未记录';

export function ValidationHoldFacts({ hold }: { hold?: Json }) {
  if (!hold) return null;
  return <div className="validation-hold-facts">
    {(hold.failed_checks ?? []).map((check: Json, index: number) => <p key={`${check.metric}:${index}`}>
      <strong><ReferenceText value={check.metric} /></strong>：已报告相对改善 {percent(check.relative_improvement)}；
      要求{check.comparison === 'greater_than' ? '大于' : '至少'} {percent(check.minimum_relative_improvement)}。
      {check.complete_seed_coverage === true && `已覆盖 ${check.n_pairs} 对种子。`}
    </p>)}
    <p>验收完整性：{hold.criteria_complete === true ? '已记录完整' : hold.criteria_complete === false ? '已记录未完成，需核对' : '未记录'}。
      同版本未批准角色：{(hold.rejected_roles ?? []).map((role: string) => roles[role] ?? role).join('、') || '未确认'}。</p>
    <p>下一步：核对已有验证制品与评审意见，记录停止结论。当前未获准新增训练或独立测试。</p>
  </div>;
}

