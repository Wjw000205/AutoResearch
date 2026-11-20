import type { Json } from './api';

export const parameterValue = (value: unknown) => Array.isArray(value) ? JSON.stringify(value) : String(value);
export const parameterKey = (value: unknown) => JSON.stringify(value);

function stable(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, child]) => JSON.stringify(key) + ':' + stable(child)).join(',') + '}';
  return JSON.stringify(value) ?? 'null';
}

export type MatrixRow = { id: string; grid: Json; sources: Json[]; control?: Json; candidates: Json[]; best?: Json };
export const scoreKey = (grid: Json) => grid.selection_score === 'baseline_score' || grid.variant === 'baseline' ? 'baseline_score' : 'proposed_score';
export const measured = (point: Json, grid: Json) => ['completed', 'reused'].includes(point.status)
  && typeof point[scoreKey(grid)] === 'number' && Number.isFinite(point[scoreKey(grid)]);

export function matrixRows(grids: Json[]): MatrixRow[] {
  const groups = new Map<string, Json[]>();
  for (const grid of grids) {
    // Reordered grids can represent the same experiments. Match the entire
    // comparison identity and actual receipts, never just a parameter value.
    const points = (grid.points ?? []).map((p: Json) => ({
      value: p.value, control: Boolean(p.control), status: p.status, jobs: [...(p.jobs ?? [])].sort(),
      baseline_score: p.baseline_score, proposed_score: p.proposed_score, improvement: p.improvement_vs_control,
    })).sort((a: Json, b: Json) => stable(a.value).localeCompare(stable(b.value)));
    const identity = stable({
      binding: grid.binding, base: grid.base_unit_id, task: grid.task_id, seed: grid.validation_seed,
      variant: grid.variant ?? 'shared', factor: grid.factor_path, metric: grid.metric, direction: grid.direction,
      control: grid.control_value, lower: grid.lower_bound, upper: grid.upper_bound, selection: scoreKey(grid),
      values: [...(grid.grid_values ?? [])].map(stable).sort(), points,
      legacy: grid.binding && grid.base_unit_id ? undefined : grid.id,
    });
    const sources = groups.get(identity) ?? [];
    sources.push(grid);
    groups.set(identity, sources);
  }
  return [...groups.values()].map(sources => {
    const grid = sources[0];
    const points: Json[] = grid.points ?? [];
    const control = points.find(p => p.control);
    const candidates = points.filter(p => !p.control).sort((a, b) =>
      typeof a.value === 'number' && typeof b.value === 'number' ? a.value - b.value : 0);
    const comparable = [control, ...candidates].filter((p): p is Json => Boolean(p && measured(p, grid)));
    const best = comparable.reduce<Json | undefined>((chosen, p) => {
      if (!chosen) return p;
      const improves = grid.direction === 'maximize' ? p[scoreKey(grid)] > chosen[scoreKey(grid)]
        : p[scoreKey(grid)] < chosen[scoreKey(grid)];
      return improves ? p : chosen;
    }, undefined);
    return {id: sources.map(g => g.id).sort().join('|'), grid, sources, control, candidates, best};
  });
}

export function matrixGroups(rows: MatrixRow[]) {
  const groups = new Map<string, MatrixRow[]>();
  for (const row of rows) {
    const grid = row.grid;
    const key = stable({factor: grid.factor_path, task: grid.task_id, binding: grid.binding,
      seed: grid.validation_seed, metric: grid.metric, direction: grid.direction});
    const group = groups.get(key) ?? [];
    group.push(row);
    groups.set(key, group);
  }
  return [...groups.entries()].map(([id, rows]) => ({id, rows}));
}

const names: Record<string, string> = {
  lr: '学习率', learning_rate: '学习率', weight_decay: '权重衰减', depth: '网络深度',
  embedding_dim: '嵌入维度', trend_widths: '趋势层宽度', dropout: 'Dropout',
  batch_size: '批大小', grad_clip: '梯度裁剪', clip_grad_norm: '梯度裁剪',
};
export const parameterName = (path: string) => names[path.split('/').at(-1) ?? ''] ?? path;
