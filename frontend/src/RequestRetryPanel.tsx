import { ReferenceText } from './ReferenceNames';
import { useEffect, useRef, useState } from 'react';
import { CircleAlert, LoaderCircle, RefreshCw, ShieldCheck } from 'lucide-react';
import { date, money, type Run } from './api';
import { humanizeMessage, PENDING_REQUEST_RECEIPTS_REASON, PENDING_REQUEST_RECEIPTS_DESCRIPTION } from './localization';
import { isContextPreflightGate, isLiteratureReadingGate, isNoveltyEvidenceGate, isDiscussionEvidenceGate, isNativeCodexGate, NO_FINAL_RETRY_REASON, NO_FINAL_RETRY_DESCRIPTION } from './RequestRecoveryDiagnostic';

type RetryRun = Pick<Run, 'id' | 'mode' | 'status' | 'updated_at' | 'error'>;
export interface RequestRetryTarget {
  handoff_id: string; role: string; provider: string; model: string; request_ids: string[];
  last_error: string; unknown_cost_usd: number; output_token_limit: number | null;
  previous_http_cost_usd?: number; previous_http_request_ids?: string[];
  recovery_kind?: 'streaming_gateway_timeout'; source_transport?: 'buffered'; destination_transport?: 'stream';
  estimated_cost_usd?: number; source_request_ids?: string[];
}
export interface ReadyRequestRetryAuthorization {
  run_id: string; authorization_id: string; snapshot_id: string; created_at: string; status: 'authorized';
  requires_resume: true; target_count: number; ready_target_count: number;
  ready_targets: { handoff_id: string; role: string; operation_id: string }[];
}
export interface NormalRecoveryTarget {
  handoff_id: string; role: string; provider: string; model: string; request_ids: string[];
  unknown_cost_usd: number; output_token_limit: number; tool_result_count: number;
  recovery_kind?: 'gateway_timeout'; http_statuses?: number[]; estimated_cost_usd?: number;
}
export interface NormalRecovery {
  available: boolean; next_action: 'explicit_resume' | null; targets: NormalRecoveryTarget[]; reason: string;
}
export interface RequestRetryPreview {
  run_id: string; eligible: boolean; snapshot_id: string | null; targets: RequestRetryTarget[]; reason: string;
  ready_authorizations?: ReadyRequestRetryAuthorization[];
  normal_recovery?: NormalRecovery;
}
export interface RequestRetryAuthorization {
  run_id: string; snapshot_id: string; authorization_id: string; status: 'authorized';
  requires_resume: true; idempotent: boolean; target_count: number;
}
export interface RequestRetryState {
  preview: RequestRetryPreview | null; acknowledged: boolean; busy: 'preview' | 'authorize' | null;
  authorization: RequestRetryAuthorization | null; readyAuthorizations: ReadyRequestRetryAuthorization[]; error: string; expired: boolean;
  errorOperation: 'preview' | 'authorize' | null;
  normalRecovery?: NormalRecovery | null;
}
type RetryRequest = (path: string, options: RequestInit) => Promise<unknown>;
export class RequestRetryHttpError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
const request: RetryRequest = async (path, options) => {
  const response = await fetch(`/api${path}`, { ...options, headers: { 'Content-Type': 'application/json' } });
  let result: any;
  try { result = await response.json(); } catch { throw new RequestRetryHttpError(`服务器响应异常（HTTP ${response.status}）`, response.status); }
  if (!response.ok) throw new RequestRetryHttpError(typeof result.detail === 'string' ? result.detail : `请求失败（HTTP ${response.status}）`, response.status);
  return result;
};
const emptyState = (): RequestRetryState => ({ preview: null, acknowledged: false, busy: null, authorization: null, readyAuthorizations: [], normalRecovery: null, error: '', expired: false, errorOperation: null });
export const requestRetryAvailable = (run: RetryRun, moderationPending = false) => run.mode === 'live' && ['blocked', 'paused'].includes(run.status) && !moderationPending;
export const requestRetryKey = (run: RetryRun, moderationPending = false) => JSON.stringify([run.id, run.mode, run.status, run.updated_at, moderationPending, run.error ?? '']);

// These known local gates are not evidence of an uncertain dispatched request.
// Keep the read-only lookup: a separate real retry target or saved authorization
// can still exist, and only the server's validated preview establishes its scope.
export function requestRetryPresentation(run: RetryRun, state: RequestRetryState): 'panel' | 'normal' | 'compact' | 'hidden' | 'diagnostic' | 'pending-receipts' {
  if (state.errorOperation === 'preview') return 'diagnostic';
  if (state.errorOperation === 'authorize') return 'panel';
  if (state.authorization || state.readyAuthorizations.length || state.preview?.eligible) return 'panel';
  if (state.normalRecovery?.available) return 'normal';
  if (!state.error && state.preview?.eligible === false && state.preview.reason === PENDING_REQUEST_RECEIPTS_REASON) return 'pending-receipts';
  if (isContextPreflightGate(run.error ?? '') || isLiteratureReadingGate(run.error ?? '') || isNoveltyEvidenceGate(run.error ?? '') || isDiscussionEvidenceGate(run.error ?? '') || isNativeCodexGate(run.error ?? '')) return state.error ? 'diagnostic' : 'hidden';
  if (!state.error && state.preview?.eligible === false && state.preview.reason === NO_FINAL_RETRY_REASON) return 'compact';
  return 'panel';
}

function invalidStreamingTarget(target: RequestRetryTarget): boolean {
  const hasStreamingFields = ['recovery_kind', 'source_transport', 'destination_transport', 'estimated_cost_usd', 'source_request_ids'].some(key => target[key as keyof RequestRetryTarget] !== undefined);
  if (!hasStreamingFields) return false;
  if (target.recovery_kind !== 'streaming_gateway_timeout' || target.source_transport !== 'buffered' || target.destination_transport !== 'stream'
    || !Number.isFinite(target.estimated_cost_usd) || (target.estimated_cost_usd as number) < 0 || target.unknown_cost_usd !== 0
    || !Number.isInteger(target.output_token_limit) || (target.output_token_limit as number) < 1
    || target.request_ids.length !== 1 || !target.request_ids[0].trim()
    || !Array.isArray(target.previous_http_request_ids) || target.previous_http_request_ids.length < 1 || target.previous_http_request_ids.length > 3
    || target.previous_http_request_ids.some(id => !id.trim())) return true;
  const sourceIds = [...target.previous_http_request_ids, ...target.request_ids];
  return new Set(sourceIds).size !== sourceIds.length || target.source_request_ids !== undefined && (
    !Array.isArray(target.source_request_ids) || target.source_request_ids.length !== sourceIds.length
    || new Set(target.source_request_ids).size !== sourceIds.length
    || target.source_request_ids.some(id => typeof id !== 'string' || !sourceIds.includes(id)));
}

function validatePreview(value: unknown, runId: string): RequestRetryPreview {
  const result = value as RequestRetryPreview | null;
  if (!result || result.run_id !== runId || typeof result.eligible !== 'boolean' || typeof result.reason !== 'string'
    || (result.snapshot_id !== null && typeof result.snapshot_id !== 'string') || !Array.isArray(result.targets)
    || result.eligible && (!result.snapshot_id || !result.targets.length)
    || result.targets.some(target => !target || !target.handoff_id || !['handoff_id', 'role', 'provider', 'model', 'last_error'].every(key => typeof target[key as keyof RequestRetryTarget] === 'string')
      || !Array.isArray(target.request_ids) || target.request_ids.some(id => typeof id !== 'string')
      || target.previous_http_cost_usd !== undefined && (!Number.isFinite(target.previous_http_cost_usd) || target.previous_http_cost_usd < 0)
      || target.previous_http_request_ids !== undefined && (!Array.isArray(target.previous_http_request_ids) || target.previous_http_request_ids.some(id => typeof id !== 'string' || !id))
      || !Number.isFinite(target.unknown_cost_usd) || target.unknown_cost_usd < 0 || invalidStreamingTarget(target))) {
    throw new Error('重试预览不完整或任务身份不匹配，请重新加载。');
  }
  if (result.ready_authorizations !== undefined && (!Array.isArray(result.ready_authorizations) || result.ready_authorizations.some(authorization =>
    !authorization || authorization.run_id !== runId || authorization.status !== 'authorized' || authorization.requires_resume !== true
    || !['authorization_id', 'snapshot_id', 'created_at'].every(key => typeof authorization[key as keyof ReadyRequestRetryAuthorization] === 'string' && authorization[key as keyof ReadyRequestRetryAuthorization])
    || !Number.isInteger(authorization.target_count) || !Number.isInteger(authorization.ready_target_count) || authorization.ready_target_count < 1 || authorization.ready_target_count > authorization.target_count
    || !Array.isArray(authorization.ready_targets) || authorization.ready_targets.length !== authorization.ready_target_count
    || authorization.ready_targets.some(target => !target || !['handoff_id', 'role', 'operation_id'].every(key => typeof target[key as keyof typeof target] === 'string' && target[key as keyof typeof target]))))) {
    throw new Error('已保存授权的任务身份或待恢复范围不匹配，请重新加载。');
  }
  if (result.normal_recovery !== undefined) {
    const normal = result.normal_recovery;
    if (!normal || typeof normal !== 'object' || Array.isArray(normal)
      || typeof normal.available !== 'boolean' || typeof normal.reason !== 'string' || !Array.isArray(normal.targets)
      || (normal.available ? normal.next_action !== 'explicit_resume' || !normal.targets.length : normal.next_action !== null || normal.targets.length !== 0)
      || normal.targets.some(target => !target || typeof target !== 'object' || Array.isArray(target)
        || !['handoff_id', 'role', 'provider', 'model'].every(key => typeof target[key as keyof NormalRecoveryTarget] === 'string' && String(target[key as keyof NormalRecoveryTarget]).trim())
        || !['director', 'engineer', 'scout'].includes(target.role)
        || !Array.isArray(target.request_ids) || !target.request_ids.length || target.request_ids.some(id => typeof id !== 'string' || !id.trim())
        || new Set(target.request_ids).size !== target.request_ids.length
        || !Number.isFinite(target.unknown_cost_usd) || target.unknown_cost_usd < 0
        || (target.recovery_kind !== undefined || target.http_statuses !== undefined || target.estimated_cost_usd !== undefined)
          && (target.recovery_kind !== 'gateway_timeout' || !Array.isArray(target.http_statuses) || !target.http_statuses.length
            || target.http_statuses.some(status => status !== 524) || !Number.isFinite(target.estimated_cost_usd) || (target.estimated_cost_usd as number) < 0)
        || !Number.isInteger(target.output_token_limit) || target.output_token_limit < 1
        || !Number.isInteger(target.tool_result_count) || target.tool_result_count < 0)
      || new Set(normal.targets.map(target => target.handoff_id)).size !== normal.targets.length) {
      throw new Error('普通回合恢复预览不完整或恢复动作不匹配，请重新加载。');
    }
  }
  return result;
}

// The current run and snapshot own the acknowledgment. No path sends a resume request.
export function createRequestRetryController(send: RetryRequest = request) {
  let scope = '', run: RetryRun | null = null, available = false, generation = 0;
  let readOnOpen = false;
  let previewRequest: AbortController | undefined;
  let state = emptyState();
  const controller = {
    setRun(next: RetryRun, moderationPending = false) {
      const nextScope = requestRetryKey(next, moderationPending);
      if (nextScope !== scope) { previewRequest?.abort(); scope = nextScope; generation++; state = emptyState(); readOnOpen = false; }
      run = next; available = requestRetryAvailable(next, moderationPending);
    },
    get: () => state,
    dispose() { previewRequest?.abort(); generation++; state = emptyState(); available = false; readOnOpen = false; },
    acknowledge(snapshotId: string, checked: boolean) {
      if (!state.busy && available && state.preview?.eligible && state.preview.snapshot_id === snapshotId && !state.authorization) state = { ...state, acknowledged: checked === true };
    },
    async load(onChange: () => void = () => {}) {
      if (!run || !available || state.busy) return;
      readOnOpen = true;
      const version = generation, runId = run.id;
      previewRequest = new AbortController();
      state = { ...emptyState(), busy: 'preview' }; onChange();
      try {
        const result = await send(`/runs/${encodeURIComponent(runId)}/request-retry`, { method: 'GET', signal: previewRequest.signal });
        if (version !== generation) return;
        const preview = validatePreview(result, runId);
        state = { ...emptyState(), preview, readyAuthorizations: preview.ready_authorizations ?? [], normalRecovery: preview.normal_recovery ?? null };
      } catch (error) {
        if (version !== generation) return;
        state = { ...emptyState(), error: error instanceof Error ? error.message : String(error), expired: error instanceof RequestRetryHttpError && error.status === 409, errorOperation: 'preview' };
      }
      onChange();
    },
    async loadOnOpen(onChange: () => void = () => {}): Promise<void> {
      if (!readOnOpen) await controller.load(onChange);
    },
    async authorize(onChange: () => void = () => {}): Promise<RequestRetryAuthorization | null> {
      if (!run || !available || state.busy || !state.preview?.eligible || !state.preview.snapshot_id || !state.acknowledged || state.authorization) return null;
      const version = generation, runId = run.id, snapshot = state.preview;
      state = { ...state, busy: 'authorize', acknowledged: false, error: '', expired: false }; onChange();
      try {
        const result = await send(`/runs/${encodeURIComponent(runId)}/request-retry`, { method: 'POST', body: JSON.stringify({ snapshot_id: snapshot.snapshot_id, acknowledge_duplicate_charge: true }) }) as RequestRetryAuthorization | null;
        if (version !== generation) return null;
        if (!result || result.run_id !== runId || result.snapshot_id !== snapshot.snapshot_id || !result.authorization_id || typeof result.authorization_id !== 'string'
          || result.status !== 'authorized' || result.requires_resume !== true || typeof result.idempotent !== 'boolean' || result.target_count !== snapshot.targets.length) {
          throw new Error('授权响应不完整或快照不匹配；授权状态待核对，请重新加载重试范围。');
        }
        state = { ...emptyState(), authorization: result, readyAuthorizations: state.readyAuthorizations, normalRecovery: state.normalRecovery }; onChange();
        return result;
      } catch (error) {
        if (version !== generation) return null;
        state = { ...emptyState(), error: error instanceof Error ? error.message : String(error), expired: error instanceof RequestRetryHttpError && error.status === 409, errorOperation: 'authorize' }; onChange();
        return null;
      }
    },
  };
  return controller;
}

const roleNames: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };
export function NormalRecoveryNotice({ recovery }: { recovery: NormalRecovery }) {
  if (!recovery.available) return null;
  const gatewayOnly = recovery.targets.every(target => target.recovery_kind === 'gateway_timeout');
  return <div className="request-retry-preview" aria-label="普通回合恢复"><header><RefreshCw size={18} /><h3><ReferenceText value={gatewayOnly ? 'HTTP 524 后的最终续答 · 从已保存材料继续' : '普通回合恢复 · 从已保存材料继续'} /></h3></header>
    <p><ReferenceText value={gatewayOnly ? '已收到网关 HTTP 524 回包，但尚无完整生成结果。' : '本次是普通推理回合中断，已保存的工具结果会复用。'} />使用顶部“恢复执行”后，系统会重新核验条件，并仅对符合条件的范围授权一次基于已存材料的最终续答；本次续答不调用工具，也不重复执行已完成工具。</p>
    <p>续答可能再次计费，<ReferenceText value={gatewayOnly ? '原失败请求及费用记录保留' : '旧请求及未知费用记录保留'} />。本卡仅展示恢复范围，不会自动授权或发送请求。</p>
    {recovery.targets.map(target => <article key={target.handoff_id}><header><strong>{roleNames[target.role] ?? target.role}</strong><span>{target.provider} · {target.model}</span></header>{target.recovery_kind === 'gateway_timeout' && <p>已收到 HTTP 524，但尚无完整生成结果。</p>}<p>已保存工具结果：{target.tool_result_count} 条</p>
      {target.recovery_kind === 'gateway_timeout'
        ? <p className="request-retry-cost">费用估算（扣费未核实）：<ReferenceText value={money(target.estimated_cost_usd)} /></p>
        : <p className="request-retry-cost">旧请求未知费用估算：<ReferenceText value={money(target.unknown_cost_usd)} /> <small>历史保守估计，非确认实扣</small></p>}
      <small>最终续答输出上限：{target.output_token_limit.toLocaleString()} 词元</small><details><summary>中断请求与交接记录</summary><dl><dt>交接 ID</dt><dd><code><ReferenceText value={target.handoff_id} /></code></dd><dt>旧请求 ID</dt><dd>{target.request_ids.map(id => <code key={id}><ReferenceText value={id} /></code>)}</dd>{target.recovery_kind === 'gateway_timeout' && <><dt>已记录 HTTP 状态</dt><dd>{target.http_statuses?.join(' / ')}</dd></>}</dl></details></article>)}
    {recovery.reason && <details><summary>恢复核验说明</summary><pre tabIndex={0}><ReferenceText value={recovery.reason} /></pre></details>}</div>;
}
export function NoFinalRetryNotice({ preview, busy, onReload }: { preview: RequestRetryPreview; busy: boolean; onReload: () => void }) {
  return <details className="request-retry-unavailable"><summary>未发现需要另行确认的最终回答重试</summary><p><ReferenceText value={NO_FINAL_RETRY_DESCRIPTION} /></p>{preview.normal_recovery?.available === false && preview.normal_recovery.reason && <p><ReferenceText value={humanizeMessage(preview.normal_recovery.reason)} /></p>}<details><summary>原始诊断</summary><pre tabIndex={0}><ReferenceText value={preview.reason} /><ReferenceText value={preview.normal_recovery?.reason ? `\n${preview.normal_recovery.reason}` : ''} /></pre></details><button type="button" className="btn small" disabled={busy} onClick={onReload}>重新读取恢复状态（不发送）</button></details>;
}
export function PendingRequestReceiptsNotice({ preview, busy, onReload }: { preview: RequestRetryPreview; busy: boolean; onReload: () => void }) {
  return <div className="request-retry-unavailable" role="status"><strong>请求回执待核对</strong><p>{PENDING_REQUEST_RECEIPTS_DESCRIPTION}</p><p>普通回合可通过顶部“恢复执行”发起恢复检查，是否继续仍由系统核验。此处仅查询状态，不会提交重试授权或发送模型请求。</p><details><summary>原始诊断</summary><pre tabIndex={0}><ReferenceText value={preview.reason} /><ReferenceText value={preview.normal_recovery?.reason ? `\n${preview.normal_recovery.reason}` : ''} /></pre></details><button type="button" className="btn small" disabled={busy} onClick={onReload}>重新读取恢复状态（不发送）</button></div>;
}
export function RequestRetryLookupNotice({ error, busy, onReload }: { error: string; busy: boolean; onReload: () => void }) {
  return <div className="request-retry-unavailable" role="alert"><strong>请求恢复状态暂未核对</strong><p>本次未能读取恢复状态与已保存授权。这次读取没有提交重试授权，也没有启动研究；已有授权是否存在仍需重新读取确认。</p><p><ReferenceText value={humanizeMessage(error)} /></p><details><summary>原始诊断</summary><pre tabIndex={0}><ReferenceText value={error} /></pre></details><button type="button" className="btn small" disabled={busy} onClick={onReload}>重新读取恢复状态（不发送）</button></div>;
}
export function RequestRetryErrorNotice({ error, expired }: { error: string; expired: boolean }) {
  return <div className="request-retry-error" role="alert"><CircleAlert size={15} /><div><strong>{expired ? '重试范围已变化或当前不可授权' : '重试操作未确认完成'}</strong><p>任务状态、配置或请求范围可能已发生变化；本次操作尚未确认完成。</p><p><ReferenceText value={humanizeMessage(error)} /></p><details><summary>原始诊断</summary><pre tabIndex={0}><ReferenceText value={error} /></pre></details><small>请重新加载当前范围并确认，不会自动再次提交授权。</small></div></div>;
}
export function RequestRetryAuthorizationNotice({ authorization }: { authorization: RequestRetryAuthorization }) {
  return <div className="request-retry-success" role="status"><ShieldCheck size={17} /><div><strong>已授权一次重试，请使用顶部“恢复执行”开始</strong><p>本次授权未启动研究；旧请求与计费记录继续保留。</p><code>授权记录：<ReferenceText value={authorization.authorization_id} /></code></div></div>;
}
export function ReadyRequestRetryNotice({ authorizations, reason }: { authorizations: ReadyRequestRetryAuthorization[]; reason?: string }) {
  if (!authorizations.length) return null;
  return <div className="request-retry-preview request-retry-persisted" role="status"><div className="request-retry-success"><ShieldCheck size={17} /><div><strong>已保存单次重试授权，等待使用顶部“恢复执行”</strong><p>以下范围已获授权，无需重复确认。尚未派发新的重试请求；恢复时仍会检查执行条件。</p></div></div>{authorizations.map(authorization => <article key={authorization.authorization_id}><header><strong>待恢复 {authorization.ready_target_count} 项 / 原授权 {authorization.target_count} 项</strong><span>{date(authorization.created_at, true)} 保存</span></header><p className="request-retry-cost">待恢复角色：<ReferenceText value={authorization.ready_targets.map(target => roleNames[target.role] ?? target.role).join('、')} /></p><details><summary>授权记录与角色范围</summary><code><ReferenceText value={authorization.authorization_id} /></code>{authorization.ready_targets.map(target => <dl key={target.handoff_id}><dt>{roleNames[target.role] ?? target.role}</dt><dd><code>交接：<ReferenceText value={target.handoff_id} /></code><code>请求操作：<ReferenceText value={target.operation_id} /></code></dd></dl>)}</details></article>)}{reason && <><p><ReferenceText value={humanizeMessage(reason)} /></p><details><summary>原始诊断</summary><pre tabIndex={0}><ReferenceText value={reason} /></pre></details></>}</div>;
}
export function RequestRetryPreviewDetails({ preview, acknowledged, busy, onAcknowledge, onAuthorize }: {
  preview: RequestRetryPreview; acknowledged: boolean; busy: boolean;
  onAcknowledge: (checked: boolean) => void; onAuthorize: () => void;
}) {
  if (!preview.eligible) return <div className="request-retry-unavailable"><strong>本次不可授权重试</strong><p>当前没有符合单次重试条件的失败请求，或任务状态、配置已发生变化。请核对诊断后再加载当前范围。</p>{preview.reason && <><p><ReferenceText value={humanizeMessage(preview.reason)} /></p><details><summary>原始诊断</summary><pre tabIndex={0}><ReferenceText value={preview.reason} /></pre></details></>}</div>;
  const hasStreamingRecovery = preview.targets.some(target => target.recovery_kind === 'streaming_gateway_timeout');
  return <div className="request-retry-preview"><p>本次授权仅对应下方 {preview.targets.length} 项失败交接，每项允许一次重试。基于原交接任务和当前上下文重新推理；保留原请求记录。已有请求与计费记录会保留，新请求可能再次计费。</p>{preview.targets.map(target => {
    const streaming = target.recovery_kind === 'streaming_gateway_timeout';
    return <article key={target.handoff_id}><header><strong>{roleNames[target.role] ?? target.role}</strong><span>{target.provider} · {target.model}</span></header>
      {streaming && <><p><strong>针对已记录 HTTP 524 的一次流式恢复</strong></p><p>原请求采用等待完整响应的接收方式；本次改为边生成边接收。已收到 HTTP 524，但没有完整生成结果；流式接收仍可能超时。本次只续答，不调用工具。</p></>}
      {streaming ? <p className="request-retry-cost">原请求链费用估算（扣费未核实）：<ReferenceText value={money(target.estimated_cost_usd)} /> <small>按账本记录估算，非确认实扣；不包含本次新请求</small></p>
        : <p className="request-retry-cost">旧请求未知费用估算：<ReferenceText value={money(target.unknown_cost_usd)} /> <small>历史保守估计，非确认实扣</small></p>}
      {!!target.previous_http_request_ids?.length && <p className="request-retry-cost">此前临时 HTTP 失败 {target.previous_http_request_ids.length} 次，账本费用 <ReferenceText value={typeof target.previous_http_cost_usd === 'number' ? money(target.previous_http_cost_usd) : '未记录'} /> <small>{streaming ? '（已含在原请求链估算中，非确认实扣）' : '（按记录、非确认实扣）'}</small></p>}
      {typeof target.output_token_limit === 'number' && <small>本次输出上限：{target.output_token_limit.toLocaleString()} 词元</small>}{target.last_error && <p><ReferenceText value={humanizeMessage(target.last_error)} /></p>}
      <details><summary>失败请求与原始错误</summary><dl><dt>交接 ID</dt><dd><code><ReferenceText value={target.handoff_id} /></code></dd><dt>旧请求 ID</dt><dd>{target.request_ids.length ? target.request_ids.map(id => <code key={id}><ReferenceText value={id} /></code>) : '未记录'}</dd>{!!target.previous_http_request_ids?.length && <><dt>此前 HTTP 请求 ID</dt><dd>{target.previous_http_request_ids.map(id => <code key={id}><ReferenceText value={id} /></code>)}</dd></>}{streaming && <><dt>响应接收方式</dt><dd>等待完整响应 → 流式接收</dd>{target.source_request_ids && <><dt>完整来源请求链 ID</dt><dd>{target.source_request_ids.map(id => <code key={id}><ReferenceText value={id} /></code>)}</dd></>}</>}</dl><pre tabIndex={0}><ReferenceText value={target.last_error || '未保存原始错误'} /></pre></details></article>;
  })}<label className="consent request-retry-consent"><input type="checkbox" checked={acknowledged} disabled={busy} onChange={event => onAcknowledge(event.target.checked)} /><span><strong>我确认授权以上请求单次重试，并知晓可能再次计费</strong><small>本次仅保存授权，不启动研究。授权后需使用顶部“恢复执行”；{hasStreamingRecovery ? '此流式恢复不自动重复，失败后保留结果等待核查。' : '再次失败需要重新确认。'}</small></span></label><button type="button" className="btn btn-secondary" disabled={busy || !acknowledged} onClick={onAuthorize}>{busy ? <LoaderCircle size={15} className="spin" /> : <ShieldCheck size={15} />}确认授权一次重试</button></div>;
}

export function RequestRetryStatus({ state, busy, onAcknowledge, onAuthorize }: {
  state: RequestRetryState; busy: boolean; onAcknowledge: (checked: boolean) => void; onAuthorize: () => void;
}) {
  return <>{state.normalRecovery?.available && <NormalRecoveryNotice recovery={state.normalRecovery} />}<ReadyRequestRetryNotice authorizations={state.readyAuthorizations} reason={state.preview?.eligible === false ? state.preview.reason : undefined} />{state.authorization && <RequestRetryAuthorizationNotice authorization={state.authorization} />}{state.error && <RequestRetryErrorNotice error={state.error} expired={state.expired} />}{!state.authorization && state.preview && (state.preview.eligible || !state.readyAuthorizations.length && !state.normalRecovery?.available) && <RequestRetryPreviewDetails preview={state.preview} acknowledged={state.acknowledged} busy={busy} onAcknowledge={onAcknowledge} onAuthorize={onAuthorize} />}</>;
}

export function RequestRetryContents({ run, state, busy, onReload, onAcknowledge, onAuthorize }: {
  run: RetryRun; state: RequestRetryState; busy: boolean; onReload: () => void;
  onAcknowledge: (checked: boolean) => void; onAuthorize: () => void;
}) {
  const presentation = requestRetryPresentation(run, state);
  if (presentation === 'hidden') return null;
  if (presentation === 'diagnostic') return <RequestRetryLookupNotice error={state.error} busy={busy} onReload={onReload} />;
  if (presentation === 'pending-receipts' && state.preview) return <PendingRequestReceiptsNotice preview={state.preview} busy={busy} onReload={onReload} />;
  if (presentation === 'compact' && state.preview) return <NoFinalRetryNotice preview={state.preview} busy={busy} onReload={onReload} />;
  const refresh = <button type="button" className="btn small" disabled={busy} onClick={onReload}>{state.busy === 'preview' ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}{state.busy === 'preview' ? '读取请求状态…' : state.preview || state.authorization || state.error ? '刷新授权状态与重试范围' : '查看单次重试范围'}</button>;
  if (presentation === 'normal') return <section className="request-retry-panel"><RequestRetryStatus state={state} busy={busy} onAcknowledge={onAcknowledge} onAuthorize={onAuthorize} />{refresh}</section>;
  return <section className="request-retry-panel" aria-label="最终回答重试"><header><RefreshCw size={18} /><div><h3>最终回答重试 · 另行确认一次</h3><p>此授权入口仅处理已发出的最终回答请求。顶部“恢复执行”不会代替这里的最终回答重试确认；普通回合另按其恢复条件核验。</p></div></header>{refresh}<RequestRetryStatus state={state} busy={busy} onAcknowledge={onAcknowledge} onAuthorize={onAuthorize} /></section>;
}

export function RequestRetryPanel({ run, moderationPending = false, externalBusy, onBusyChange, onAuthorized }: {
  run: RetryRun; moderationPending?: boolean; externalBusy: boolean;
  onBusyChange: (busy: boolean) => void; onAuthorized: (result: RequestRetryAuthorization) => void;
}) {
  const controller = useRef(createRequestRetryController());
  const mounted = useRef(true);
  const callbacks = useRef({ onBusyChange, onAuthorized }); callbacks.current = { onBusyChange, onAuthorized };
  const [, rerender] = useState(0);
  controller.current.setRun(run, moderationPending);
  const state = controller.current.get(), scope = requestRetryKey(run, moderationPending);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; controller.current.dispose(); callbacks.current.onBusyChange(false); }; }, []);
  useEffect(() => {
    controller.current.setRun(run, moderationPending);
    callbacks.current.onBusyChange(!!controller.current.get().busy);
    if (!externalBusy) void controller.current.loadOnOpen(update);
  }, [scope, externalBusy]);
  function update() { if (mounted.current) { rerender(value => value + 1); callbacks.current.onBusyChange(!!controller.current.get().busy); } }
  if (!requestRetryAvailable(run, moderationPending)) return null;
  const busy = !!state.busy || externalBusy;
  async function load() { if (!externalBusy) await controller.current.load(update); }
  async function authorize() {
    if (externalBusy) return;
    const result = await controller.current.authorize(update);
    if (result && mounted.current) callbacks.current.onAuthorized(result);
  }
  return <RequestRetryContents run={run} state={state} busy={busy} onReload={() => void load()} onAcknowledge={checked => { controller.current.acknowledge(state.preview?.snapshot_id ?? '', checked); update(); }} onAuthorize={() => void authorize()} />;
}
