export interface MemoryPageResult<T> {
  items: T[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
  next_offset: number | null;
}

export interface MemoryTask {
  id: string;
  role: string;
  purpose: string;
  summary: string;
  status: string;
  created_at?: string;
  updated_at?: string;
}

export interface MemoryOverview {
  roles: { role: string; current_task: MemoryTask | null; history_count: number; task_count: number; request_count: number }[];
  memory_counts: { run: number; shared: number; total: number };
}

export interface MemoryRequest {
  id: string;
  role: string;
  purpose: string;
  handoff_id: string | null;
  status: string;
  capture_stage: string;
  dispatch_status: string;
  created_at: string;
  provider: string | null;
  model: string | null;
  context_available: boolean;
  legacy: boolean;
  operation_id?: string | null;
  context_sha256?: string | null;
  context_tokens_estimate?: number | null;
  source_ids?: unknown;
}

export interface InputReference {
  id: unknown;
  type?: unknown;
  sha256?: unknown;
  source?: unknown;
  inclusion: unknown;
  preview?: unknown;
  provenance?: unknown;
  score?: unknown;
  tool_name?: unknown;
}

export interface MemoryRequestDetail {
  request: MemoryRequest;
  history: InputReference[];
  retrievals: { tool_name: string; query: unknown; results: InputReference[]; inclusion: string }[];
  references: InputReference[];
  missing: string[];
  truncated?: boolean;
  counts?: { history_total: number; history_displayed: number; tool_results_total: number; retrieval_results_displayed: number; references_displayed: number };
}

export interface StoredMemoryEntry {
  id: string;
  run_id: string | null;
  role: string | null;
  text_preview: string;
  text_chars: number;
  scope: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface MemoryHistoryEntry {
  id: string;
  role: string;
  created_at: string;
  provider: string | null;
  model: string | null;
  operation_id: string | null;
  request_id: string | null;
  purpose: string;
  text_preview: string;
  text_chars: number;
  truncated: boolean;
}

export interface MemoryTextPage {
  text: string;
  total_chars: number;
  offset?: number;
  limit?: number;
  next_offset: number | null;
  has_more: boolean;
  sha256: string | null;
  available?: boolean;
  missing?: string[];
  source?: string;
  capture_stage?: string;
  dispatch_status?: string;
  redacted?: boolean;
}
