"""One independent review and one integrating author per graph handoff.

Exact contexts, votes, amendments and paid responses use the existing journal.
Historical reviews are reconstructed without altering their original inputs.
"""
import asyncio

from .communication_graph import communicate
from . import discussion_actions, idea_provenance
from .storage import now


async def run(engine, h, run_id, common, execution_constraints, proposal, committed_at_resume):
    from .engine import (VOTE, VOTE_PROGRESS, PROPOSAL, ROLES, Harness, ResearchBlocked, digest,
                         assess_progress, previous_review, effective_progress, assess_resources, review_schema)
    self = engine
    run = self.store.get('runs', run_id)
    until_stalled = run['config'].get('discussion_mode', 'bounded') == 'until_stalled'

    async def review_turn(state):
        payload = dict(state['payload'])
        round_number, proposal = payload['round_number'], payload['proposal']
        prior_hashes, history = set(payload['prior_hashes']), list(payload['history'])
        proposal_evidence_snapshot = payload.get('proposal_evidence_snapshot')
        evidence = None
        self.check_active(run_id)
        proposal_hash = digest(proposal)
        if not until_stalled and proposal_hash in prior_hashes:
            raise ResearchBlocked('Debate repeated an already rejected proposal without changes')
        prior_hashes.add(proposal_hash)
        current_hardware = await self.execution_hardware(run_id)
        assessment = assess_resources(proposal['resource_plan'], current_hardware, require_complete=common.get('resource_plan_protocol') == 1)
        context_key = f'{run_id}:{self.cycle(run_id)}:{round_number}:{proposal_hash}'
        if common.get('resource_plan_protocol') == 1:
            context_key += ':resource-policy-' + digest(current_hardware.get('execution_preferences', {}))[:16]
        if common.get('innovation_selection'):
            context_key += ':innovation-' + digest(common['innovation_selection'])[:16]
        if common.get('dataset_transition'):
            # A data change needs the old outcome and identities, never its
            # prediction arrays or raw runner output in each review request.
            from .experiment_workflow import validation_records
            previous_validation = [{k: row.get(k) for k in ('id', 'research_cycle', 'status', 'metrics', 'code_sha256')}
                for row in validation_records(self.store, run_id)
                if (row.get('research_cycle') or 1) == self.cycle(run_id) - 1]
        else:
            previous_validation = self.validation_history(run_id, include_superseded=True, cycle=self.cycle(run_id) - 1) if self.cycle(run_id) > 1 else []
        if previous_validation:
            # This additional, immutable evidence creates a new audited
            # assignment when resuming older empty-history tool loops.
            context_key += ':history-' + digest(previous_validation)[:16]
        frozen_input = self.store.get('debate_inputs', context_key)
        if frozen_input:
            context = frozen_input['context']
        else:
            checked_evidence = await self._read_consensus_check(run_id, self._proposal_evidence,
                run_id, proposal, protocol=common.get('idea_provenance_protocol'),
                notes_protocol=common.get('literature_notes_protocol'))
            context = {**common, 'execution_constraints': execution_constraints, 'hardware': current_hardware, 'proposal': proposal, 'proposal_hash': proposal_hash,
                       'resource_assessment': assessment, 'evidence_assessment': checked_evidence, 'proposal_schema': idea_provenance.proposal_schema(PROPOSAL, common)}
            from .execution_contract import host_budget_snapshot
            context['execution_constraints'] = {**execution_constraints,
                'host_budget_review': host_budget_snapshot(self.store, run_id, proposal,
                    proposal_hash, self.cycle(run_id))}
            from . import innovation_policy
            if run['config'].get('innovation_policy_version') == innovation_policy.VERSION:
                selected = self.store.get('innovation_sessions', f'{run_id}:{self.cycle(run_id)}') or {}
                novelty = selected.get('novelty_gate') or {}
                context['novelty_screening'] = {'policy_version': innovation_policy.VERSION,
                    'gate': {k: novelty.get(k) for k in ('approved', 'proposal_hash', 'approval_basis',
                        'approval_provenance', 'scope', 'policy_limitations')},
                    'instruction': innovation_policy.INSTRUCTION}
            context.update(source_tool_protocol=1, review_protocol_version=1, context_reference_protocol=1, evidence=(proposal_evidence_snapshot
                           if proposal_evidence_snapshot is not None else self.evidence_snapshot(run_id)))
            context['previous_discussion'] = previous_review(history[-1] if history else None)
            if context.get('idea_provenance_protocol') == 1:
                context['evidence'] = {**context['evidence'], 'novelty_search_scope': idea_provenance.search_scope(self.store, run_id)}
            if until_stalled:
                context['discussion_policy_version'] = 1
            work = discussion_actions.index(self, run_id, round_number - 1)
            if work:
                context['discussion_work'] = work
            if previous_validation:
                context['previous_validation_evidence'] = previous_validation
            self.store.put('debate_inputs', context_key, {'id': context_key, 'run_id': run_id, 'context': context, 'created_at': now()})
        instruction = 'Independently review this exact proposal. director: methodology and venue evidence; engineer: implementability and hardware; scout: novelty, citations and strongest counterexample. Approve only when ready for the specified bounded experiment, all blocking concerns resolved. Return exactly the supplied proposal_hash. Do not agree merely to reach consensus. This is prospective permission to RUN the proposed experiments, not validation of their eventual results. No measurements for this new cycle are expected yet. Use explicitly labelled prior-cycle evidence to assess motivation and feasibility; do not demand new-cycle measurements before allowing the cycle to begin. Read supplied previous_validation_evidence and completed context.tool_results before requesting more tools. Once the requested evidence has been returned, give your own approve=true or approve=false judgment with concrete reasons and required_changes; repeating the same history request supplies no new evidence.'
        if context.get('dataset_transition'):
            from .dataset_transition import GUIDANCE
            instruction += '\n' + GUIDANCE
        if context.get('architecture_convergence_protocol') == 1:
            from .design_review_policy import SCALE
            instruction += SCALE
        if context.get('novelty_screening'):
            instruction += context['novelty_screening']['instruction']
            instruction += ('\nnovelty_screening记录的是已检索范围内排重放行，不是原创性或能力的实验证明。'
                '未读材料、覆盖局限与未来能力验证不能单独否定新颖性；新的高度相似完整机制须给具体原文依据。'
                '原评审的设计问题仍需独立核查，资源、方法、可证伪性和共识标准不变。')
        if context.get('innovation_quality_protocol') == 1:
            from .innovation_quality import REVIEW as quality_review_instruction
            instruction += quality_review_instruction
        vote_schema = VOTE
        if context.get('idea_provenance_protocol') == 1:
            instruction += idea_provenance.REVIEW_INSTRUCTION
        if context.get('discussion_policy_version') == 1:
            vote_schema = VOTE_PROGRESS
            instruction += ('\nAlso assess material progress against context.previous_discussion. '
                'Use the exact prior objection IDs, actually changed proposal field names, and newly retrieved evidence IDs. '
                'Paraphrasing, a new title, dropping an objection without resolution, or an irrelevant source is not progress. '
                'Report made_progress=false when no grounded change resolves a prior issue. On the initial round there is no prior progress to claim. '
                'Progress is separate from approval: acknowledge a partial improvement while continuing to reject unresolved concerns. '
                'Give a concise public rationale. The controller counts consecutive rounds without support from at least two roles; no fixed total round cap applies. '
                'Do not demand the experiment code or new measurements before the design and implementation stages have been authorized; '
                'instead evaluate a sufficiently precise, falsifiable implementation specification and honestly bounded resource estimate.')
        if context.get('review_protocol_version') == 1:
            vote_schema = review_schema(vote_schema,
                allow_context_references=context.get('context_reference_protocol') == 1)
            instruction += ('\nConstructive review is required from EVERY dissenting role: for each required_changes item, '
                'provide at least one fixes entry with its zero-based objection_index, exact target_field, '
                'a concrete proposed_change you authored, a verification_plan, and supporting task evidence_ids when needed. '
                'Engineer: propose implementable algorithms, resource estimates or evaluation corrections; '
                'scout: propose evidence-backed method/baseline/citation changes; director: propose methodological corrections. '
                'Do not merely ask the director to figure out a fix. Empty evidence_ids is permitted for self-contained design suggestions. '
                'Approve only with empty required_changes and fixes. On later rounds examine the attributed previous amendments '
                'and verify whether your own proposed changes were implemented or justifiably superseded. '
                'Suggested fixes are not scientific evidence or automatic approval; retain disagreement where alternatives conflict.')
            if context.get('context_reference_protocol') == 1:
                instruction += ('\nIn fixes, evidence_ids accepts only exact verified paper IDs or retrieved source IDs from this task. '
                    'For reasoning based on the supplied proposal or review use context_references instead '
                    '(for example proposal.method or original_review.required_changes[0], only when those fields exist). '
                    'These are context pointers, not scientific evidence. Self-contained design remedies may use evidence_ids=[].')
        vote_schema = idea_provenance.review_schema(vote_schema, context)
        if context.get('discussion_work'):
            instruction += discussion_actions.REVIEW
        if run['mode'] == 'live' and hasattr(h.intelligence, 'preflight_batch'):
            pending_roles = [role for role in ROLES if not self.store.get('decisions', Harness.decision_key(run_id, role, f'vote:{round_number}', instruction, context, vote_schema))]
            for role in pending_roles:
                await h.complete_pending_tools(run_id, role, f'vote:{round_number}', instruction, context, vote_schema)
            pending_roles = [role for role in pending_roles if not self.store.get('decisions', Harness.decision_key(run_id, role, f'vote:{round_number}', instruction, context, vote_schema))]
            requests = [h.next_request(run_id, role, f'vote:{round_number}', instruction, context, vote_schema) for role in pending_roles]
            if requests:
                if hasattr(h.intelligence, 'prepare_context_batch'):
                    await h.intelligence.prepare_context_batch(run_id, requests)
                h.intelligence.preflight_batch(run_id, requests)
        vote_tasks = [asyncio.create_task(h.ask(run_id, role, f'vote:{round_number}', instruction, context, vote_schema)) for role in ROLES]
        votes = await self._collect_debate_votes(vote_tasks)
        votes = [{'role': role, **vote} for role, vote in zip(ROLES, votes)]
        replayed = self._historical_rejected_review(run_id, committed_at_resume,
            round_number, proposal, votes, frozen_input)
        if replayed is None:
            evidence = await self._read_consensus_check(run_id, self._proposal_evidence,
                run_id, proposal, protocol=context.get('idea_provenance_protocol'),
                notes_protocol=context.get('literature_notes_protocol'))
            unanimous = assessment['approved'] and evidence['approved'] and all(v['approve'] and not v['required_changes'] and v['proposal_hash'] == proposal_hash for v in votes)
            record = {'cycle': self.cycle(run_id), 'round': round_number, 'proposal': proposal, 'proposal_hash': proposal_hash, 'votes': votes, 'resource_assessment': assessment, 'evidence_assessment': evidence, 'unanimous': unanimous}
            if common.get('innovation_selection'):
                record['innovation_session_id'] = common['innovation_selection']['session_id']
            if context.get('idea_provenance_protocol') == 1:
                record['idea_provenance_protocol'] = 1
                problem = await self._read_consensus_check(run_id, self._problem_assessment,
                    run_id, votes, context.get('literature_notes_protocol'))
                record['problem_assessment'] = problem
                unanimous = record['unanimous'] = unanimous and problem['approved']
            if context.get('literature_notes_protocol') == 1:
                record['literature_notes_protocol'] = 1
            if until_stalled:
                record['discussion_evidence'] = context.get('evidence', {})
                record['discussion_progress'] = assess_progress(history, record,
                    window=run['config'].get('stagnation_rounds', 3))
            replayed = self._saved_debate_review(run_id, record)
        if replayed is not None:
            # New notes can change the live assessment while a paid
            # revision is still pending. Its frozen work/recovery owns
            # the original review. Reassess the successor proposal with
            # new independent votes instead of reallocating old work.
            record = replayed
            unanimous = record['unanimous']
        saved_review = self.save_proposal_review(run_id, record)
        if isinstance(saved_review, dict) and saved_review.get('amendments') and not record.get('amendments'):
            # Replaying a completed vote may branch before the old
            # revision runs again. Keep its attributed saved remedies
            # in the reconstructed history and successor review.
            record['amendments'] = json.loads(json.dumps(saved_review['amendments']))
        if replayed is not None and until_stalled:
            correction = self._reassess_stopped_progress(run_id, record, history, committed_at_resume)
            if correction:
                record['progress_reassessment'] = correction
        history.append(record)
        # Reconstructing earlier paid rounds must not make the live UI
        # jump backwards while the current checkpoint is resuming.
        if (committed_at_resume.get('cycle', 1) != self.cycle(run_id)
                or round_number >= committed_at_resume.get('round', 0)):
            self.store.put('consensus', run_id, {**record, 'id': run_id, 'run_id': run_id, 'history': history})
        if replayed is None:
            self.store.event(run_id, 'consensus.round', f'Round {round_number}: ' + ('unanimous' if unanimous else 'revision required'), record)
        payload.update(prior_hashes=sorted(prior_hashes), history=history)
        if unanimous:
            return {'phase': 'done', 'result': {**record, 'history': history}, 'payload': payload}
        from .execution_contract import control_review_available
        if until_stalled and control_review_available(self, run_id, record):
            # Platform facts were missing from this old frozen review.
            # Retain its exact vote/counter and review the same proposal
            # once in the next round, without rebuying earlier work.
            return {'phase': 'review', 'payload': {**payload, 'round_number': round_number + 1}}
        if until_stalled and effective_progress(record)['stalled']:
            self.store.event(run_id, 'discussion.stalled', 'Discussion has no supported material progress', effective_progress(record))
            if not discussion_actions.recovery_allowed(self, run_id, record):
                raise ResearchBlocked('Discussion stalled without material progress: ' + effective_progress(record)['reason'])
        if not until_stalled and round_number >= run['config']['max_rounds']:
            raise ResearchBlocked('Maximum discussion rounds reached without unanimous agreement; experiment execution is blocked. Revise the objective or add evidence before retrying.')
        return {'phase': 'revise', 'payload': {**payload, 'context': context,
            'record': record, 'votes': votes, 'proposal_hash': proposal_hash,
            'review_evidence': evidence or record.get('evidence_assessment', {})}}

    async def revise_turn(state):
        payload = dict(state['payload'])
        round_number, context, record = payload['round_number'], payload['context'], payload['record']
        votes, proposal_hash, evidence = payload['votes'], payload['proposal_hash'], payload['review_evidence']
        history = list(payload['history'])
        revision_instruction = 'Revise the proposal to resolve the explicit objections and resource failures. Do not erase dissent or just reword the same rejected idea. Retain the user research direction.'
        if context.get('architecture_convergence_protocol') == 1:
            from .design_review_policy import AUTHOR
            revision_instruction += AUTHOR
        review_context = context
        if context.get('idea_provenance_protocol') == 1:
            review_key = f'{run_id}:post-review-evidence:{self.cycle(run_id)}:{round_number}:{proposal_hash}'
            frozen_review = self.store.get('stage_contexts', review_key)
            if not frozen_review:
                frozen_review = {'id': review_key, 'run_id': run_id, 'created_at': now(),
                                 'context': {**context, 'evidence_assessment': evidence}}
                self.store.put('stage_contexts', review_key, frozen_review)
            review_context = frozen_review['context']
        revision_context = self._revision_evidence_context(run_id, h, round_number, review_context, votes, revision_instruction)
        revision_instruction, revision_context = await self._constructive_revision(
            run_id, h, round_number, revision_instruction, revision_context)
        if revision_context.get('amendments'):
            record['amendments'] = revision_context['amendments']
            self.save_proposal_review(run_id, record)
            if (committed_at_resume.get('cycle', 1) != self.cycle(run_id)
                    or round_number >= committed_at_resume.get('round', 0)):
                self.store.put('consensus', run_id, {**record, 'id': run_id, 'run_id': run_id, 'history': history})
        proposal = await h.ask(run_id, 'director', f'revise:{round_number}', revision_instruction, revision_context, idea_provenance.proposal_schema(PROPOSAL, revision_context))
        proposal_evidence_snapshot = self._completed_revision_evidence(
            run_id, h, round_number, revision_context, revision_instruction, proposal)
        history[-1] = record
        return {'phase': 'review', 'payload': {**payload, 'proposal': proposal,
            'history': history, 'round_number': round_number + 1,
            'proposal_evidence_snapshot': proposal_evidence_snapshot}}

    return await communicate(review_turn, revise_turn,
        {'round_number': 1, 'proposal': proposal, 'prior_hashes': [], 'history': [],
         'proposal_evidence_snapshot': None},
        transition_limit=1_000_000_000 if until_stalled else 2 * run['config']['max_rounds'] + 2)
