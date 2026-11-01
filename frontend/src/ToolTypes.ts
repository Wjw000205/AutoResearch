export interface ToolAgent {
  role: string;
  provider: string;
  model: string;
  tools: string[];
}

export interface RegisteredTool {
  name: string;
  description: string;
  arguments_contract: string;
  roles: string[];
  category: string;
  availability: { role: string; condition: string; protocol: string | null }[];
  execution_scope: string;
}

export interface ToolCatalog {
  schema_version: number;
  source: string;
  run_id: string | null;
  agents: ToolAgent[];
  tools: RegisteredTool[];
  total: number;
  scope?: string;
  notice?: string;
}

export interface ToolCall {
  id: string;
  tool_name: string;
  role: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  handoff_id: string | null;
  arguments: unknown;
  result: unknown;
  error: string | null;
  error_type?: string | null;
  interrupted?: boolean;
  arguments_summary: string;
  result_summary: string;
  truncated: boolean;
  provenance: { mode: string; source: string; result: string | null };
  evidence: { started_event_id: number | null; finished_event_id: number | null; correlated: boolean };
}

export interface ToolCallDetail extends ToolCall {
  schema_version: number;
  run_id: string;
}

export interface ToolCallPage {
  schema_version: number;
  run_id: string;
  items: ToolCall[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  next_offset: number | null;
  filters: { role: string | null; tool: string | null; status: string | null };
}
