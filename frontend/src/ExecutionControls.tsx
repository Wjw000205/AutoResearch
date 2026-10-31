import { ImplementationReviewControls } from './ImplementationReviewControls';
import { useId } from 'react';
import { LocalCodexModelControls } from './LocalCodexPanel';
import { money, type Json, type DiscussionConfig, type DiscussionMode, type DiscussionProgressRecord, type ExecutionConfig, type ImplementationBackend } from './api';

export const budgetLabel = (value: number | null | undefined) => value == null ? '无金额上限' : money(value);
export const CONTEXT_TOKEN_MAX = 256000;

export function CallBudgetControl({ value, onChange }: { value: number | null | undefined; onChange: (value: number | null) => void }) {
  const id = useId();
  const unlimited = value === null;
  return <div className="field"><label htmlFor={id}>模型调用次数上限</label><input id={id} type="number" required={!unlimited} disabled={unlimited} min="3" max="1000" step="1" placeholder={unlimited ? '无次数上限' : ''} value={unlimited ? '' : value ?? 150} onChange={event => onChange(Number(event.target.value))} /><label className="consent"><input type="checkbox" checked={unlimited} onChange={event => onChange(event.target.checked ? null : 150)} /><span>无次数上限</span></label><small>每次新模型请求仍记录累计次数；复用已有回复不增加次数。</small></div>;
}

export function ContextBudgetControl({ value, onChange }: { value: number | undefined; onChange: (value: number) => void }) {
  const id = useId();
  return <div className="field"><label htmlFor={id}>上下文词元预算</label><input id={id} type="number" required min="4000" max={CONTEXT_TOKEN_MAX} step="1" value={value ?? CONTEXT_TOKEN_MAX} onChange={event => onChange(Number(event.target.value))} /><small>预算可设至 256,000 词元（十进制 256k）。实际输入还受模型总窗口限制，并扣除本次输出额度和 256 个词元安全余量；提高预算不会扩大模型窗口。例如总窗口 128,000、输出额度 5,000 时，输入最多 122,744 词元。</small><small>每次请求都尽量压缩，普通 High 目标为 4 万词元，目标与分槽额度均为参考，不设独立的 6 万硬上限。必要原文、精确数值、反证和约束继续保留；仅超出配置或模型可用容量时，在发送前阻断，并要求缩小读取范围或拆分任务。</small></div>;
}

export function discussionMode(config: DiscussionConfig, fallback: DiscussionMode = 'bounded'): DiscussionMode {
  return config.discussion_mode ?? fallback;
}

export function DiscussionControls({ value, onChange }: { value: DiscussionConfig; onChange: (key: string, value: number | DiscussionMode) => void }) {
  const id = useId();
  const mode = discussionMode(value, 'until_stalled');
  return <section className="discussion-controls" aria-label="共识讨论停止条件"><div className="form-grid"><div className="field"><label htmlFor={`${id}-discussion-mode`}>共识讨论方式</label><select id={`${id}-discussion-mode`} value={mode} onChange={event => onChange('discussion_mode', event.target.value as DiscussionMode)}><option value="until_stalled">持续讨论，停滞时暂停</option><option value="bounded">设置讨论轮次上限</option></select><small>{mode === 'until_stalled' ? '科研方案共识持续推进，不以固定讨论轮数截断。实现文档使用独立的验收合同与有限审核预算。连续多轮没有实质性进展时暂停并保留记录。' : '科研方案共识按讨论上限推进，实现文档按冻结验收合同审查；到达上限或连续无实质性进展时暂停。'}</small></div><div className="field"><label htmlFor={`${id}-stagnation`}>连续无进展轮数</label><input id={`${id}-stagnation`} type="number" required min="2" max="10" step="1" value={value.stagnation_rounds ?? 3} onChange={event => onChange('stagnation_rounds', Number(event.target.value))} /><small>依据已记录的讨论变化检测实质性进展，暂停原因可复核；实现规范与科研方案分别计数。</small></div>{mode === 'bounded' && <div className="field"><label htmlFor={`${id}-max-rounds`}>共识轮次上限</label><input id={`${id}-max-rounds`} type="number" required min="1" max="8" step="1" value={value.max_rounds ?? 3} onChange={event => onChange('max_rounds', Number(event.target.value))} /></div>}</div></section>;
}

export function auditedDiscussionProgress(progress?: DiscussionProgressRecord, audit?: Json): DiscussionProgressRecord | undefined {
  return typeof progress?.state_hash === 'string' && /^[a-f0-9]{64}$/.test(progress.state_hash)
    && audit?.derived === true && typeof audit.id === 'string' && typeof audit.review_hash === 'string'
    && /^[a-f0-9]{64}$/.test(audit.review_hash) && audit.old_progress?.state_hash === progress?.state_hash
    && audit.old_progress?.consecutive_no_progress === progress?.consecutive_no_progress
    && audit.reassessed_progress?.version === 2 ? audit.reassessed_progress : progress;
}

export function DiscussionProgress({ round, config, progress, reassessment }: { round: number; config: DiscussionConfig; progress?: DiscussionProgressRecord; reassessment?: Json }) {
  const count = Number.isFinite(round) ? Math.max(0, Math.floor(round)) : 0;
  const displayed = auditedDiscussionProgress(progress, reassessment);
  const corrected = displayed !== progress;
  const streak = displayed?.consecutive_no_progress;
  const measured = typeof streak === 'number' && Number.isInteger(streak) && streak >= 0;
  const windowSize = displayed?.stagnation_window ?? config.stagnation_rounds ?? 3;
  return <span className="discussion-progress">{discussionMode(config) === 'until_stalled' ? measured ? `已讨论 ${count} 轮 · 连续无进展 ${streak}/${windowSize}` : `已讨论 ${count} 轮 · 连续 ${config.stagnation_rounds ?? 3} 轮无进展时暂停` : `已讨论 ${count} / ${config.max_rounds ?? 3} 轮`}{corrected && '（计数已重验，原记录保留）'}</span>;
}

export function ProviderTimeoutControl({ providerName, value, onChange }: { providerName: string; value?: number | null; onChange: (value: number) => void }) {
  const id = useId();
  return <div className="field"><label htmlFor={id}>模型响应读取超时（秒）</label><input id={id} aria-label={`${providerName}模型响应读取超时（秒）`} type="number" required min="5" max="180" step="1" value={value ?? 120} onChange={event => onChange(Number(event.target.value))} /><small>等待模型响应数据的最长间隔，默认 120 秒，可设 5–180 秒。单次实验运行时间在执行配置中设置。</small></div>;
}

export function ProviderStreamingControl({ providerName, protocol, value, onChange }: { providerName: string; protocol: string; value?: boolean; onChange: (value: boolean) => void }) {
  const id = useId();
  if (protocol !== 'responses') return null;
  return <div className="field"><div className="provider-options"><label htmlFor={id}><input id={id} aria-label={`${providerName}开启流式响应`} type="checkbox" checked={value === true} onChange={event => onChange(event.target.checked)} /><span>开启流式响应</span></label></div><small>默认关闭。开启后边生成边接收；读取超时指连续收不到数据的时长，并非整次请求总时长。仍可能遇到网关超时。保存后用于后续请求。</small></div>;
}

export function implementationBackend(_config: ExecutionConfig): ImplementationBackend {
  return 'local_codex';
}

export function ImplementationControls({ value, onChange, mode = 'live', projectLocked = false }: {
  value: ExecutionConfig; onChange: (key: string, value: number | string | null) => void; mode?: 'live' | 'demo'; projectLocked?: boolean;
}) {
  const id = useId();
  const unlimitedDeliveries = value.codex_max_revisions === null;
  return <section className="implementation-controls" aria-label="代码实现方式"><div className="form-grid">
    <div className="field"><label>代码实现方式</label><strong>本机 Codex</strong><small>{mode === 'demo' ? '演示响应只用于研究流程验证。实现文档和代码仍需真实读取、审核与交付记录。' : '三角色通过目录和工具评审实现文档，同版本三方通过后交本机 Codex 在独立目录写入与测试代码；产物经本地验收和三方代码评审后才能推进。'}</small></div>
    <div className="field"><label htmlFor={`${id}-project`}>Codex 项目工作目录</label><input id={`${id}-project`} required readOnly={projectLocked} value={value.codex_project_dir ?? 'F:\\Python program\\AutoResearch'} onChange={event => onChange('codex_project_dir', event.target.value)} placeholder="输入本机已存在的绝对目录" /><small>{projectLocked ? '实现规范已启动，工作目录已锁定，避免中断恢复时执行到不同位置。' : '填写本机已存在的绝对目录。每项研究会在此目录下的 .autoresearch/研究标识/实现标识/workspace 中写入独立代码；实际执行路径以回执为准。'}</small></div>
    <LocalCodexModelControls model={value.codex_model} effort={value.codex_reasoning_effort} locked={projectLocked} onChange={onChange} />
    <div className="field"><label htmlFor={`${id}-timeout`}>本机 Codex 单次实现超时（秒）</label><input id={`${id}-timeout`} type="number" min="60" max="7200" step="1" required value={value.codex_timeout_seconds ?? 1800} onChange={event => onChange('codex_timeout_seconds', Number(event.target.value))} /><small>默认 1800 秒，与模型响应读取超时和单次实验超时分别设置。</small></div>
    <div className="field"><label htmlFor={`${id}-revisions`}>代码交付次数上限</label><input id={`${id}-revisions`} type="number" min="1" max="6" step="1" required={!unlimitedDeliveries} disabled={unlimitedDeliveries} value={unlimitedDeliveries ? '' : value.codex_max_revisions ?? 3} placeholder={unlimitedDeliveries ? '不限次数' : undefined} onChange={event => onChange('codex_max_revisions', Number(event.target.value))} /><label className="budget-unlimited"><input type="checkbox" checked={unlimitedDeliveries} onChange={event => onChange('codex_max_revisions', event.target.checked ? null : 3)} /><span>不设置代码交付次数上限</span></label><small>代码交付默认最多 3 次，包含首次。不限次数时可继续修复与交付，累计用量保留；单次超时、任务总预算、本地验收与独立审核仍生效。</small></div>
  </div><ImplementationReviewControls value={value} onChange={onChange} /><p className="panel-note">本机 Codex 使用本机登录；未指定型号时沿用默认模型，三位研究角色继续使用各自已配置的模型接口。写入与测试沿用研究任务的本地执行授权。</p></section>;
}

export function BudgetControl({ value, onChange }: { value: number | null | undefined; onChange: (value: number | null) => void }) {
  const id = useId();
  const unlimited = value == null;
  return <div className="field budget-control"><label htmlFor={id}>金额上限（美元）</label><input id={id} type="number" min="0.01" step="0.01" required={!unlimited} disabled={unlimited} value={unlimited || value === 0 ? '' : value} placeholder={unlimited ? '无金额上限' : '输入金额上限'} onChange={event => onChange(Number(event.target.value))} /><label className="budget-unlimited"><input type="checkbox" checked={unlimited} onChange={event => onChange(event.target.checked ? null : 10)} /><span>不设置金额上限</span></label><small>所有模型请求继续记录用量与估算费用。</small></div>;
}

export function ResearchCycleControls({ value, onChange }: { value: { max_research_cycles?: number | null; min_relative_improvement?: number }; onChange: (key: string, value: number | null) => void }) {
  const id = useId();
  const unlimited = value.max_research_cycles == null;
  return <div className="research-cycle-controls"><div className="form-grid"><div className="field"><label htmlFor={`${id}-cycles`}>科研迭代轮数上限</label><input id={`${id}-cycles`} type="number" required={!unlimited} disabled={unlimited} min="1" value={value.max_research_cycles ?? ''} placeholder={unlimited ? '不限次数' : undefined} onChange={event => onChange('max_research_cycles', Number(event.target.value))} /><label className="budget-unlimited"><input type="checkbox" checked={unlimited} onChange={event => onChange('max_research_cycles', event.target.checked ? null : 3)} /><span>不设置科研迭代次数上限</span></label><small>逐轮完成调整、复验与独立评审，已完成实验继续复用。不限次数时持续推进，单次资源约束与验收要求仍生效。</small></div><div className="field"><label htmlFor={`${id}-improvement`}>目标相对改善幅度</label><input id={`${id}-improvement`} type="number" required min="0" max="1" step="0.001" value={value.min_relative_improvement ?? 0.01} onChange={event => onChange('min_relative_improvement', Number(event.target.value))} /><small>0.01 表示相对改善 1%。最终推进还需全部角色同意。</small></div></div></div>;
}
