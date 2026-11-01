import { ReferenceText } from './ReferenceNames';
import { useState } from 'react';
import { LoaderCircle, RefreshCw, Settings2 } from 'lucide-react';
import { post, type Json, type Run } from './api';
import { ArchitectureDelivery, architectureDeliveryLabel, architectureLocallyRevalidated } from './ArchitectureDelivery';

export function definitionResumeReason(run: Run, value?: Json | null): string | undefined {
  if (run.stage !== 'design') return;
  if (value?.current?.review_delivery_validation?.status === 'repair_failed') return '评审交付补正仍未通过，已停止自动重试，请处理保存的校验错误。';
  const delivery = value?.revision?.delivery_validation;
  if (delivery?.status === 'delivery_validation_failed') return '作者答复已保存，请先准备架构交付补正，再恢复执行。';
  if (delivery?.status === 'repair_failed') return '架构交付补正仍未通过，已停止自动重试，请处理保存的校验错误。';
  const definitions = value?.revision?.definition_review;
  if (run.stage !== 'design' || run.config?.architecture_revision_protocol !== 1 || !definitions) return;
  if (definitions.completion?.delivery_recovery?.pending) {
    return '本版终审答复已保存，交付校验需要处理。恢复已保存终审后继续收集剩余意见。';
  }
  const votes: Json[] = Array.isArray(definitions.votes) ? definitions.votes : [];
  if (['rejected', 'blocked'].includes(definitions.status)
      || votes.length === 3 && votes.some(v => v.approve !== true || v.required_changes_count > 0 || v.required_changes?.length)) {
    return '当前定义版本尚未获准，请先准备下一版定义评议，再恢复执行。';
  }
}

function ArchitectureDeliveryRecovery({ run, value, prepared, externalBusy, onBusyChange }: {
  run: Run; value: Json; prepared?: () => void; externalBusy: boolean; onBusyChange?: (busy: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [savedLocallyRevalidated, setSavedLocallyRevalidated] = useState(false);
  const delivery = value.revision?.delivery_validation;
  async function prepare() {
    if (busy || externalBusy || saved || !delivery?.can_prepare) return;
    setBusy(true); onBusyChange?.(true); setError('');
    try {
      const result = await post<Json>(`/runs/${encodeURIComponent(run.id)}/architecture-delivery-repair-handoff`);
      if (result.status !== 'repair_pending') {
        prepared?.();
        throw new Error(result.reason || '原交付无法自动补正，请查看保存的具体校验原因。');
      }
      setSavedLocallyRevalidated(architectureLocallyRevalidated({delivery_validation: result}));
      setSaved(true); prepared?.();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); onBusyChange?.(false); }
  }
  return <div className="architecture-recovery" aria-label="架构交付补正">
    <ArchitectureDelivery revision={value.revision} />
    {definitionResumeReason(run, value) && <p id="definition-resume-reason"><ReferenceText value={definitionResumeReason(run, value)} /></p>}
    {saved ? <p role="status">{savedLocallyRevalidated ? '已保存交付通过本地复验，刷新后可恢复执行并复用原答复。尚未通过独立架构评议。' : '补正已准备，刷新后可恢复执行。尚未调用模型或启动实验。'}</p>
      : delivery?.status === 'delivery_validation_failed' && <button className="btn small" disabled={busy || externalBusy || !delivery.can_prepare} onClick={() => void prepare()}>
        {busy ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}准备架构交付补正</button>}
    {error && <p role="alert"><ReferenceText value={error} /></p>}
  </div>;
}

export function ArchitectureRecovery({ run, value, openLimits, prepared, externalBusy = false, onBusyChange }: {
  run: Run; value?: Json | null; openLimits?: () => void; prepared?: () => void;
  externalBusy?: boolean; onBusyChange?: (busy: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  if (value && architectureDeliveryLabel(value.revision) && ['paused', 'blocked', 'failed'].includes(run.status)) {
    return <ArchitectureDeliveryRecovery run={run} value={value} prepared={prepared} externalBusy={externalBusy} onBusyChange={onBusyChange} />;
  }
  if (value?.current?.review_delivery_validation?.status === 'repair_failed') {
    return <div className="inline-info" aria-label="评审交付补正失败"><p id="definition-resume-reason"><ReferenceText value={definitionResumeReason(run, value)} /></p><p>具体校验原因和原始意见见文档与评议详情。</p></div>;
  }
  const reason = definitionResumeReason(run, value);
  if (!reason || !['paused', 'blocked', 'failed'].includes(run.status)) return null;
  const completion = value?.revision?.definition_review?.completion;
  const progress = completion?.convergence;
  const delivery = completion?.delivery_recovery;
  const recoverDelivery = Boolean(delivery?.pending);
  const canProceed = recoverDelivery ? delivery?.can_recover : completion?.can_prepare;
  async function prepare() {
    if (busy || externalBusy || saved || !canProceed) return;
    setBusy(true); onBusyChange?.(true); setError('');
    try {
      await post(`/runs/${encodeURIComponent(run.id)}/${recoverDelivery ? 'architecture-definition-review-recovery' : 'architecture-definition-completion-handoff'}`);
      setSaved(true); prepared?.();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); onBusyChange?.(false); }
  }
  return <div className="inline-info architecture-recovery" aria-label="定义评议恢复">
    <p id="definition-resume-reason"><ReferenceText value={reason} /></p>
    {completion && <p>已安排 <ReferenceText value={completion.versions_used} /> 版定义评议，不设轮次上限。</p>}
    {progress && <p>连续无实质推进 <ReferenceText value={progress.consecutive_no_progress} /> / <ReferenceText value={progress.stagnation_window} /> 轮；独立评审确认问题闭合后清零。</p>}
    {delivery?.reason && <p><ReferenceText value={delivery.reason} /></p>}
    {saved ? <p role="status"><ReferenceText value={recoverDelivery ? '本版原终审已恢复，刷新后可继续执行。未调用模型或启动实验。' : '下一版已准备，刷新后可恢复执行。尚未启动模型或实验。'} /></p>
      : !recoverDelivery && progress?.stalled ? <><p>评议因无实质推进暂停：<ReferenceText value={progress.reason} />请根据保存的具体意见改变修改方法，避免重复同一候选。</p>
        {!progress.repeated_state && openLimits && <button className="btn small" disabled={busy || externalBusy} onClick={openLimits}><Settings2 size={14} />调整停滞条件</button>}</>
      : <button className="btn small" disabled={busy || externalBusy || !canProceed} onClick={() => void prepare()}>
        {busy ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}{recoverDelivery ? '恢复已保存终审' : '准备下一版定义评议'}</button>}
    <p className="muted"><ReferenceText value={recoverDelivery ? '核验并保留本版候选、分组意见和原始终审答复，继续本版剩余评议。' : '下一版按完整修改意见定向修订，保留已明确且不受影响的内容。恢复执行后，有实质推进就继续交接；此操作只安排修订，原候选、否决与阅读记录保留。'} /></p>
    {error && <p role="alert"><ReferenceText value={error} /></p>}
  </div>;
}
