import type { LiteratureCoverageRecord } from './LiteratureCoverage';

export type Json = Record<string, any>;
export type DiscussionMode = 'until_stalled' | 'bounded';
export interface DiscussionConfig { discussion_mode?: DiscussionMode; stagnation_rounds?: number; max_rounds?: number }
export interface DiscussionProgressRecord { version?: number; state_hash?: string; consecutive_no_progress?: number; stagnation_window?: number; stalled?: boolean; progress_roles?: string[]; changed_fields?: string[]; new_evidence_ids?: string[]; reason?: string }
export type ImplementationBackend = 'local_codex';
export type ImplementationReviewTimeBasis = 'per_revision_review' | 'policy_wall_time' | 'policy_active_time';
export interface ExecutionConfig extends DiscussionConfig { python_executable?: string; gpu_index?: number | null; vram_limit_gb?: number | null; cpu_limit_cores?: number | null; ram_limit_gb?: number | null; disk_limit_gb?: number | null; execution_mode?: string; max_research_cycles?: number | null; architecture_max_revisions?: number | null; architecture_stagnation_rounds?: number; min_relative_improvement?: number; implementation_backend?: ImplementationBackend; codex_timeout_seconds?: number; codex_max_revisions?: number | null; codex_project_dir?: string; codex_model?: string; codex_reasoning_effort?: string; implementation_review_strategy?: 'whole_alignment' | 'acceptance_contract'; implementation_indexed_review_protocol?: 1; implementation_review_max_revisions?: number | null; implementation_review_max_seconds?: number | null; implementation_review_time_basis?: ImplementationReviewTimeBasis; implementation_review_max_calls?: number | null }
export interface Provider extends Json {
  id: string; name: string; model: string; protocol: string; base_url: string;
  configured: boolean; input_price: number | null; output_price: number | null;
  cached_input_price: number | null; price_verified: boolean; timeout_seconds?: number; responses_streaming?: boolean;
}
export interface Run extends Json {
  research_iteration?: { limit: number | null; unlimited: boolean; source: string; scope: string; policy_sha256?: string | null };
  id: string; title: string; objective: string; mode: 'live' | 'demo'; status: string;
  stage: string; created_at: string; updated_at: string; config: Json & ExecutionConfig; state: Json;
  error?: string; progress: number;
}
export type RunListItem = Pick<Run, 'id' | 'title' | 'mode' | 'status' | 'stage' | 'created_at' | 'updated_at' | 'progress'>
  & { objective_preview?: string; objective?: string };
export interface Overview { runs: RunListItem[]; run_counts?: { total: number; running: number; completed: number }; hardware: Json; providers: Provider[]; roles: Record<string, string>; totals: Json; memory_count: number }
export interface EvaluationProvenance { status?: string; source?: string; verification_scope?: string; [key: string]: unknown }
export interface EvaluationIssue { code: string; severity: string; message: string; trial_ids?: string[] }
export interface PairedComparison {
  comparison_id?: string;
  configuration: string; metric: string; direction: string; n_pairs: number; seeds: (string | number)[];
  baseline_configuration?: string; proposed_configuration?: string;
  baseline_parameters?: Json; proposed_parameters?: Json;
  baseline_mean: number | null; proposed_mean: number | null; mean_delta: number | null; improvement: number | null;
  pairs: { seed: string | number; baseline: number; proposed: number; delta: number; improvement: number; baseline_trial_id: string; proposed_trial_id: string; provenance_status: string }[];
  ci95: { low: number; high: number; method: string; estimand: string; confidence: number; n_min: number; assumptions?: string } | null;
  limitations: string[]; provenance: EvaluationProvenance;
}
export interface TrialEvaluation { primary_metric?: string; test_metric?: string; direction?: string; paired_comparisons: PairedComparison[]; issues: EvaluationIssue[]; provenance: EvaluationProvenance }
export interface Orchestration { framework: string; current_node?: string; nodes: {id: string; label: string; status?: string; role?: string}[]; edges: {source: string; target: string; label?: string; condition?: string}[]; agents: Json[] }
export interface ResearchCycle extends Json { id: string; run_id: string; cycle: number; status: string; decision?: 'iterate' | 'finalize' | 'blocked' | 'stop'; rationale?: string; validation_summary?: Json; review_votes?: Json[]; started_at?: string; completed_at?: string }
export interface AgentHistoryEntry { id: string; at: string; purpose: string; status: string; text: string; kind: 'output' | 'tool' | 'assignment'; consensus?: Json; citation_discard_audit?: Json; truncated?: boolean; tool_name?: string; caller_role?: string; tool_call_id?: string; handoff_id?: string }
export interface AgentAssignment { id: string; purpose: string; status: string; label: string; created_at?: string; updated_at?: string }
export interface AgentActivityProgress { kind: 'writing' | 'reading' | null; label: string; percent: number | null; completed?: number | null; total?: number | null; unit?: string | null; reported: boolean; status_label?: string; completed_label?: string; state?: 'calculating' | 'unavailable' }
export interface AgentActivityTool { name: string; label?: string; status: 'running' | 'completed' | 'failed'; document_title?: string | null; document_id?: string | null; section_label?: string | null; section_id?: string | null; offset?: number | null; limit?: number | null; range_kind?: 'pending_request' | 'actual_read' | null; started_at?: string | null; completed_at?: string | null; error?: string | null; validation_code?: string | null; validation_detail?: string | null }
export interface AgentRevisionStatus {
  total: number; handled: number; failed_units: number; latest_notes: string[];
  self_check: { state: 'not_reported' | 'needs_revision' | 'reported'; complete: number | null;
    incomplete: number | null; blocked: number | null; verified: false;
    review_pending?: { issue_id: string; index: number; owner: string; requirement: string; verified: false }[];
    pending: { issue_id: string; condition_index: number; status: string; requirement: string; reason: string }[] };
}
export interface AgentCurrentActivity { revision_status?: AgentRevisionStatus; handoff_id?: string; phase: string; label: string; state: string; detail?: string; updated_at?: string | null; version?: number | null; round?: number | null; progress?: AgentActivityProgress | null; tool?: AgentActivityTool | null; revision_memory?: { source_revision: number; selected_fields: number; selected_notes: number; historical_read_chars: number; current_fields?: number; restored_source_chars?: number; source: string }; reading?: { percent: number | null; completed: number | null; total: number | null; unit: 'characters'; reported?: boolean; state?: 'calculating' | 'unavailable'; documents_completed?: number; documents_total?: number; source?: string; scope?: string; optional_documents?: number; optional_chars?: number; catalog_documents?: number; catalog_chars?: number; catalog_groups?: {kind: string; documents: number; characters: number}[] } | null }
export interface ResearchMapAgent { role: string; provider: string; model: string; status: string; current_task?: string; last_task?: string; current_action?: string; current_assignment?: AgentAssignment | null; current_activity?: AgentCurrentActivity | null; current_output?: AgentHistoryEntry | null; current_assignments_count?: number; history: AgentHistoryEntry[]; history_total?: number; history_loaded?: boolean }
export interface ResearchMapLink { id: string; from_role: string; to_role: string; purpose: string; status: string; at: string; active?: boolean }
export interface ExperimentExecution {
  kind: 'experiment'; status: 'running' | 'preparing' | 'awaiting_confirmation'; action: string; source: string;
  trials: {id: string; phase?: string; variant?: string; seed?: number | string; execution_mode?: string; started_at?: string; recorded_status?: string; source_id?: string}[];
}
export type CurrentExecution = ExperimentExecution | import('./CommandCodex').CodexExecution;
export interface ResearchMap { optimization_loop?: Json; current_execution?: CurrentExecution | null; current_stage: string; current_stage_label?: string; current_cycle: number; progress: number; next_action: string; consensus_activity?: Json | null; mainline: {id: string; label: string; status: 'passed' | 'rejected' | 'pending' | 'active' | 'skipped'; reason?: string | null}[]; branches: {id: string; parent_id?: string; label: string; status: 'passed' | 'rejected' | 'pending'; cycle: number; reason: string; source_id: string; kind: string}[]; agents: ResearchMapAgent[]; links?: ResearchMapLink[] }
export interface LiteratureDetail extends Json { coverage?: LiteratureCoverageRecord | null; papers?: Json[]; analysis?: Json; reading?: Json }
export interface Detail { architecture_adjustments?: Json | null; parameter_grid?: Json; optimization_loop?: Json; architecture_tuning?: Json | null; dataset_configuration?: Json | null; implementation_progress?: Json | null; client_refresh?: number; projection?: 'summary' | 'view' | 'stage'; revision?: string; loaded_sections?: string[]; counts?: Record<string, number>; stage_summaries?: Record<string, string>; architecture_review?: Json | null; innovation?: Json | null; context_reference_check?: Json | null; local_codex?: Json | null; literature?: LiteratureDetail | null; run: Run; steps: Json[]; events: Json[]; artifacts: Json[]; experiments: Json[]; consensus: Json; current_debate?: Json | null; moderation_reviews?: Json[]; context: Json; billing: Json; handoffs?: Json[]; references?: Json[]; evaluation?: TrialEvaluation; experiment_summary?: Json | null; orchestration?: Orchestration; research_cycles?: ResearchCycle[]; research_map?: ResearchMap }
export interface Settings { providers: Provider[]; roles: Record<string, string>; defaults: Json & ExecutionConfig; cache_note?: string }
export interface RunReport { markdown: string; kind: 'final' | 'stage' | 'none'; status: string; run_id: string; run_updated_at: string; filename: string | null; title: string }

export class ApiResponseError extends Error {
  constructor(message: string, readonly status: number) { super(message); this.name = 'ApiResponseError'; }
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  });
  let body: any;
  try { body = await response.json(); } catch { throw new Error(`服务器响应异常（HTTP ${response.status}）`); }
  if (!response.ok) {
    const detail = body.detail ?? body.message ?? body.error;
    const message = typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `请求失败（HTTP ${response.status}）`;
    throw new ApiResponseError(message, response.status);
  }
  return body as T;
}
export const post = <T,>(path: string, body?: unknown) => api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });
export const number = (value: unknown, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
export const money = (value: unknown) => `$${number(value).toFixed(number(value) > 0 && number(value) < 0.01 ? 5 : 2)}`;
export const compact = (value: unknown) => new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(number(value));
export const date = (value: unknown, time = false) => {
  if (!value || Number.isNaN(new Date(String(value)).getTime())) return '—';
  return new Date(String(value)).toLocaleString('zh-CN', time ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' } : { month: 'long', day: 'numeric' });
};
export function textOf(value: unknown): string {
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}
export function safeUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined; } catch { return undefined; }
}
