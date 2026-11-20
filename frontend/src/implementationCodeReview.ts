import type { Json, ResearchMapAgent, ResearchMapLink } from './api';
import { sidebarDetailMatches } from './implementationProjection';

const finalStates = ['passed', 'approved', 'changes_required', 'changes_requested'];
const findings = (row: Json) => ['issues', 'fixes', 'required_changes'].some(key => Array.isArray(row[key]) && row[key].length > 0);

export function codeReviewStatus(row: Json) {
  const status = row.status ?? row.handoff_status ?? 'pending';
  return ['passed', 'approved'].includes(status) && findings(row) ? 'changes_required' : status;
}

/** The host's explicit code-review version owns membership, never vote count. */
export function codeReviewCounts(delivery?: Json | null) {
  if (!delivery) return null;
  const version = delivery.code_revision_review_version ?? delivery.whole_code_review_version;
  if (version != null && version !== 1) return null;
  const roles = version === 1 ? ['director', 'scout'] : ['director', 'engineer', 'scout'];
  const reviews: Json[] = Array.isArray(delivery.reviews) ? delivery.reviews : [];
  if (reviews.some(row => !row || typeof row !== 'object' || !roles.includes(row.role))
    || new Set(reviews.map(row => row.role)).size !== reviews.length) return null;
  const rows = roles.map(role => {
    const row = reviews.find(item => item.role === role);
    if (!row) return { role, status: 'pending', binding_valid: false };
    // Earlier servers can report a saved parent result while its replacement
    // is still running. Count only terminal opinions from the current handoff.
    return delivery.code_revision_review_version === 1 && row.handoff_status === 'running' && finalStates.includes(row.status)
      ? { ...row, status: 'running' } : row;
  });
  return { roles, rows, expected: roles.length,
    saved: rows.filter(row => row.binding_valid === true && finalStates.includes(row.status)).length,
    approved: rows.filter(row => row.binding_valid === true && ['passed', 'approved'].includes(row.status) && !findings(row)).length,
    changes: rows.filter(row => row.binding_valid === true && (['changes_required', 'changes_requested'].includes(row.status) || findings(row))).length };
}

export function currentCodeReview(progress: Json, response: Json | null | undefined, runId: string) {
  if (!['code_review', 'completed'].includes(progress.phase) || !sidebarDetailMatches(progress, response, runId)) return null;
  if (typeof progress.workflow?.document_sha256 !== 'string' || !progress.workflow.document_sha256) return null;
  const revision = progress.workflow?.revision;
  const delivery = response?.details?.code_delivery;
  if (!Number.isInteger(revision) || revision < 0 || response?.workflow?.revision !== revision
    || delivery?.index !== revision || !delivery.code_sha256 || !delivery.delivery_hash) return null;
  const counts = codeReviewCounts(delivery);
  return counts ? { ...counts, revision, delivery } : null;
}

function selectedHandoff(row: Json, links: ResearchMapLink[], purpose?: string) {
  const match = links.find(link => link.id === row.handoff_id && link.to_role === row.role);
  // Older projections can pair a parent's saved vote with its running
  // correction's status. Only the same role's unique active child qualifies.
  if (purpose && row.binding_valid && row.handoff_status === 'running' && match?.status === 'completed') {
    const root = match.purpose.split(':full-')[0];
    const children = links.filter(link => link.to_role === row.role && link.status === 'running'
      && root.startsWith(purpose + ':changes-v1:') && link.purpose.startsWith(root + ':full-'));
    if (children.length === 1) return children[0];
  }
  return match;
}

/** The command map and review detail can arrive in different polling cycles. */
export function revisionReviewCards(agents: ResearchMapAgent[], links: ResearchMapLink[],
  progress: Json, response: Json | null, runId: string) {
  const current = currentCodeReview(progress, response, runId);
  const revised = current?.delivery.code_revision_review_version === 1 ? current : null;
  const changedLinks = links.filter(link => link.purpose.startsWith('code_review:') && link.purpose.includes(':changes-v1:'));
  const cards = agents.map(agent => {
    if (agent.role !== 'director' && agent.role !== 'scout') return agent;
    const row = revised?.rows.find(item => item.role === agent.role);
    const assignment = agent.current_assignment;
    if (row?.handoff_id) {
      const link = selectedHandoff(row, changedLinks, revised?.delivery.review_purpose);
      const id = link?.id ?? row.handoff_id;
      const recorded = row.handoff_status ?? link?.status ?? row.status;
      const status = recorded === 'running' && progress.run_status !== 'running' ? progress.run_status : recorded;
      const vote = recorded === 'completed' && id === row.handoff_id && row.binding_valid === true ? codeReviewStatus(row) : '';
      const stateLabel = ['changes_required','changes_requested'].includes(vote) ? '已提交修改意见'
        : ['passed','approved'].includes(vote) ? '已提交通过意见' : status === 'running' ? '正在复审' : undefined;
      if (assignment && assignment.id === id && assignment.status === status && agent.status === status)
        return { ...agent, review_state_label: stateLabel };
      const label = '审查上一轮意见与本次代码修改';
      return { ...agent, status, review_state_label: stateLabel, current_task: label, last_task: label, current_action: '',
        current_assignment: { id, purpose: link?.purpose ?? row.handoff_purpose ?? revised?.delivery.review_purpose ?? 'code_review',
          label, status, updated_at: link?.at },
        current_activity: agent.current_activity?.handoff_id === id ? agent.current_activity : null,
        current_output: agent.current_output?.id === id + ':output' ? agent.current_output : null,
        current_assignments_count: status === 'running' ? 1 : 0 };
    }
    // While synchronizing, an old whole-review card for this same delivery
    // is historical. A genuine interruption of the selected generation is visible.
    const base = assignment?.purpose.split(':').slice(0, 3).join(':');
    if (revised || (!current && assignment?.purpose.startsWith('code_review:') &&
        !assignment.purpose.includes(':changes-v1:') && changedLinks.some(item => item.purpose.startsWith(base + ':')))) {
      return { ...agent, status: 'pending', current_assignment: null, current_activity: null, current_output: null,
        current_task: '同步本轮修改复审', last_task: '', current_action: '', current_assignments_count: 0 };
    }
    return agent;
  });
  return cards;
}

export function currentReviewLinks(links: ResearchMapLink[], progress: Json, response: Json | null, runId: string) {
  const review = currentCodeReview(progress, response, runId);
  if (review && review.delivery.code_revision_review_version !== 1) return links;
  const active = new Set(review?.rows.map(row => selectedHandoff(row, links, review.delivery.review_purpose)?.id ?? row.handoff_id).filter(Boolean));
  const deliveries = new Set(links.filter(link => link.purpose.includes(':changes-v1:'))
    .map(link => link.purpose.split(':').slice(0, 3).join(':')));
  return links.filter(link => !link.purpose.startsWith('code_review:') ||
    (review ? active.has(link.id) : link.purpose.includes(':changes-v1:') || !deliveries.has(link.purpose.split(':').slice(0, 3).join(':'))));
}
