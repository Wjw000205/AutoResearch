import type { Json } from './api';

// Legacy recovery checkpoints execute an architecture candidate inside tuning.
// Display the bound pending candidate without changing the execution checkpoint.
export function optimizationDisplayStage(stage: string, loop?: Json) {
  const pending = ['implementing', 'reviewing', 'review_blocked', 'checking', 'check_repairing', 'check_failed', 'rejected', 'invalid'];
  return stage === 'tuning' && ['architecture_assessment', 'architecture_pending'].includes(loop?.status)
    && pending.includes(loop?.progress?.current_candidate_status) ? 'architecture_tuning' : stage;
}
