import type { Json } from './api';

const timestamp = (value: unknown) => typeof value === 'string' && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null;

function completionTime(trial?: Json): number | null {
  if (!trial || trial.status !== 'completed' || trial.superseded) return null;
  const completed = timestamp(trial.completed_at);
  if (completed !== null) return completed;
  const started = timestamp(trial.started_at);
  // Prefer execution timing to a later metadata update on an old experiment.
  if (started !== null && typeof trial.duration_seconds === 'number'
      && Number.isFinite(trial.duration_seconds) && trial.duration_seconds >= 0) {
    const end = started + trial.duration_seconds * 1000;
    if (Number.isFinite(end)) return end;
  }
  return timestamp(trial.source_updated_at);
}

export function comparisonCompletion(comparison: Json, trials: Json[]): number | null {
  const ids: string[] = (comparison.pairs ?? []).flatMap((pair: Json) => [pair.baseline_trial_id, pair.proposed_trial_id]);
  if (!ids.length) return null;
  const times = ids.map(id => completionTime(trials.find(trial => trial.id === id)));
  return times.every((time): time is number => time !== null) ? Math.max(...times) : null;
}

export function latestComparison(comparisons: Json[]): Json | null {
  if (comparisons.length === 1) return comparisons[0];
  if (!comparisons.length || comparisons.some(row => typeof row.completed_timestamp !== 'number'
      || !Number.isFinite(row.completed_timestamp))) return null;
  const latest = Math.max(...comparisons.map(row => row.completed_timestamp));
  const matches = comparisons.filter(row => row.completed_timestamp === latest);
  // A tie or missing timing cannot choose a configuration on the user's behalf.
  return matches.length === 1 ? matches[0] : null;
}
