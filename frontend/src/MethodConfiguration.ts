import type { Json } from './api';

function stable(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, child]) => JSON.stringify(key) + ':' + stable(child)).join(',') + '}';
  return JSON.stringify(value) ?? 'null';
}

const metadata = new Set(['seed', 'phase', 'algorithm', 'method', 'loss', 'dataset', 'dataset_assets', 'data',
  'benchmark_task_id', 'benchmark_source', 'comparison_task', 'comparison_inputs', 'variant',
  'implementation_contract', 'loss_contract', 'rho_contract', 'evaluation_contract_version', 'runtime']);

function parameters(params: Json, prefix = ''): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(params)) {
    if ((!prefix && metadata.has(key)) || key === 'data') continue;
    const path = prefix + '/' + key.replace(/~/g, '~0').replace(/\//g, '~1');
    if (value && typeof value === 'object' && !Array.isArray(value)) Object.assign(result, parameters(value, path));
    else result[path] = value;
  }
  return result;
}

export type ConfigurationRow = {
  group: Json; params?: Json; values: Record<string, unknown>; valid: boolean; scope: string; winner: boolean;
};
export type MethodSection = { id: string; rows: ConfigurationRow[]; columns: string[] };
const preferred = ['lr', 'learning_rate', 'weight_decay', 'depth', 'embedding_dim', 'trend_widths', 'dropout'];
const priority = (path: string) => {
  const index = preferred.indexOf(path.split('/').at(-1) ?? '');
  return index < 0 ? preferred.length : index;
};
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

export function methodSections(groups: Json[], experiments: Json[], metric: string, direction?: string): MethodSection[] {
  const byId = new Map(experiments.map(trial => [trial.id, trial]));
  const sections = new Map<string, ConfigurationRow[]>();
  for (const group of groups) {
    const ids: string[] = group.trial_ids ?? [];
    const trials = ids.map(id => byId.get(id));
    const matched = trials.length > 0 && trials.every((t): t is Json => Boolean(t && !t.superseded && t.params));
    const configs = matched ? trials.map(t => Object.fromEntries(Object.entries(t!.params)
      .filter(([key]) => !['seed', 'phase', 'comparison_inputs'].includes(key)))) : [];
    const params = configs.length && configs.every(p => stable(p) === stable(configs[0])) ? configs[0] : undefined;
    const seeds: unknown[] = group.seeds ?? [];
    const valid = Boolean(params && group.phase === 'validation' && group.completed === group.total && group.completed > 0
      && !(group.issues ?? []).length && seeds.length === trials.length
      && seeds.every(s => Number.isInteger(s)) && new Set(seeds).size === seeds.length
      && group.dataset_key && group.code_sha256 && group.plan_sha256 && group.code_revision != null
      && finite(group.metrics?.[metric]?.mean) && ['minimize', 'maximize'].includes(direction ?? '')
      && trials.every(t => t!.status === 'completed' && t!.params_complete !== false && t!.metrics_complete !== false
        && !(t!.evaluation_errors ?? []).length && finite(t!.metrics?.[metric])
        && t!.code_sha256 === group.code_sha256 && t!.experiment_plan_sha256 === group.plan_sha256
        && t!.code_revision === group.code_revision && t!.research_cycle === group.cycle
        && t!.params.phase === group.phase && t!.params.variant === group.variant && seeds.includes(t!.params.seed))
      && stable(trials.map(t => t!.params.seed).sort((a, b) => a - b))
        === stable([...seeds].sort((a, b) => Number(a) - Number(b))));
    const scope = stable({
      seeds: [...seeds].sort((a, b) => Number(a) - Number(b)),
      bundles: [...new Set(trials.map(t => t?.bundle_sha256))].sort(),
    });
    const id = stable([group.name, group.variant, group.loss, group.dataset_key, group.phase, group.cycle,
      group.code_revision, group.code_sha256, group.plan_sha256, group.provenance]);
    const rows = sections.get(id) ?? [];
    rows.push({group, params, values: params ? parameters(params) : {}, valid, scope, winner: false});
    sections.set(id, rows);
  }
  return [...sections].map(([id, rows]) => {
    const keys = new Set(rows.flatMap(row => Object.keys(row.values)));
    let columns = [...keys].filter(path => new Set(rows.map(r => stable(r.values[path]))).size > 1);
    if (!columns.length) columns = [...keys].filter(path => priority(path) < preferred.length);
    columns.sort((a, b) => priority(a) - priority(b) || a.localeCompare(b));
    for (const row of rows) {
      const peers = rows.filter(p => p.valid && p.scope === row.scope);
      if (!row.valid || peers.length < 2) continue;
      const best = (direction === 'maximize' ? Math.max : Math.min)(...peers.map(p => p.group.metrics[metric].mean));
      row.winner = row.group.metrics[metric].mean === best;
    }
    // Keep different seed aggregates apart. Within each scope show best points first.
    rows.sort((a, b) => (a.group.seeds?.length ?? 0) - (b.group.seeds?.length ?? 0)
      || a.scope.localeCompare(b.scope) || Number(b.valid) - Number(a.valid)
      || (a.valid && b.valid ? (direction === 'maximize' ? -1 : 1)
        * (a.group.metrics[metric].mean - b.group.metrics[metric].mean) : 0));
    return {id, rows, columns};
  });
}
