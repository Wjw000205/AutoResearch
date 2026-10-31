import type { Json } from './api';

export function OptimizationProgress({ value }: { value?: Json | null }) {
  const progress = value?.progress;
  const attempts = progress?.attempts ?? value?.round;
  const status: Record<string, string> = {
    implementing: '收集候选交付', reviewing: '独立代码审核中', checking: 'CPU 检查中',
    review_blocked: '审核提出问题，等待定向处理', rejected: '候选审核未通过',
    check_failed: 'CPU 检查未通过', invalid: '交付未通过收集', activated: '候选已生效',
  };
  return <div aria-label="优化实际进展">
    <strong>{attempts == null ? '修改尝试次数未记录' : `修改尝试 · ${attempts} 次`}</strong>
    <p>次数记录修改单进入流程，不代表模型已修改或效果已提升。</p>
    {progress && <>
      <p>本阶段：代码交付 {progress.code_deliveries} 次 · 生效版本 {progress.activated_versions} 个 · 新增成功训练 {progress.successful_validation_runs} 次</p>
      <p>CPU 检查：通过 {progress.passed_code_checks} 次 · 失败 {progress.failed_code_checks} 次</p>
      {progress.current_program_revision != null && <p>当前生效版本 v{progress.current_program_revision}{progress.current_candidate_status ? ` · ${status[progress.current_candidate_status] ?? '候选处理中'}` : ''}</p>}
    </>}
  </div>;
}
