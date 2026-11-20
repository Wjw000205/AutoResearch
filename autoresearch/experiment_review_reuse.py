"""Reuse unchanged archive sections in a revised experiment proposal review."""
from . import independent_full_review as full

KEY = 'previous_experiment_reading'
SECTIONS = ('evidence', 'sources', 'supplemental_evidence')


def original(store, run_id, decision_id, reference):
    parent = store.get('experiment_decisions', decision_id) or {}
    if (parent.get('id') != decision_id or parent.get('run_id') != run_id
            or parent.get('status') != 'disputed' or parent.get('evidence') != reference):
        raise ValueError('Experiment review reading must refer to its disputed source decision')
    return parent


def source_vote(engine, run_id, parent, vote, *, preparing=False):
    hid = vote[full.KEY]['handoff_id']
    if preparing and any(not engine.store.get(c, hid) for c in ('handoff_inputs', 'handoffs', 'decisions')):
        return None
    frozen = full.authenticate(engine.store, run_id, vote)['context']
    from . import review_material_packet as packets
    if (KEY in frozen and frozen[KEY].get('version') != 4 or packets.enabled(frozen) or not full.required_root_chars(frozen)
            or frozen[full.KEY]['domain'] != 'experiment_vote:' + parent['proposal_sha256']
            or frozen['decision_proposal'] != parent['proposal']
            or frozen['validation_evidence'] != parent['evidence']
            or vote.get('proposal_sha256') != parent['proposal_sha256']):
        raise ValueError('Previous experiment reading is not bound to the original full proposal review')
    try:
        full.validate_delivery(engine.store, run_id, vote, engine=engine)
    except ValueError:
        if preparing:
            return None  # Without a prior delivery proof the new review reads the full archive.
        raise
    return frozen[full.MATERIALS]


def prepare(engine, run_id, record, context):
    parent = original(engine.store, run_id, record['consensus_parent'], record['evidence'])
    prior = parent.get(KEY)
    if prior:
        parent = original(engine.store, run_id, prior['decision_id'], record['evidence'])
    votes = parent['votes']
    if not full.members(votes) or full.reviewers(votes) != full.REVIEWERS:
        return None
    current = full.resolve_materials(engine.store, run_id, context)
    sections = {k: full.sha(current[k]) for k in SECTIONS if k in current}
    for vote in votes:
        archive = source_vote(engine, run_id, parent, vote, preparing=True)
        if archive is None:
            return None
        sections = {k: digest for k, digest in sections.items() if k in archive and archive[k] == current[k]}
    if sections:
        return {'version': 1, 'revision_id': record['id'], 'decision_id': parent['id'], 'sections': sections}


def reduced(context):
    proof = context[KEY]
    archive = context[full.MATERIALS]
    if proof.get('version') == 2:
        sections = proof['evidence_sections']
        if (not sections and not proof.get('sources_sha256') or
                any(k not in archive['evidence'] or full.sha(archive['evidence'][k]) != v for k, v in sections.items()) or
                proof.get('sources_sha256') and full.sha(archive['sources']) != proof['sources_sha256']):
            raise ValueError('Earlier analysis reading differs from the current archive')
        result = {**archive, 'evidence': {k: v for k, v in archive['evidence'].items() if k not in sections}}
        if proof.get('sources_sha256'):
            result.pop('sources')
        return result
    sections = proof['sections']
    allowed = ('evidence', 'sources') if proof.get('version') in (3, 4) else SECTIONS
    if (proof.get('version') not in (1, 3, 4) or not sections or set(sections) - set(allowed)
            or any(k not in archive or full.sha(archive[k]) != value for k, value in sections.items())):
        raise ValueError('Previous experiment reading differs from the current frozen archive')
    return {k: v for k, v in archive.items() if k not in sections}


def validate(engine, run_id, role, context):
    proof = context[KEY]
    reduced(context)
    if proof.get('version') in (3, 4):
        from .experiment_evidence_handoff import KEY as HANDOFF
        from .experiment_workflow import sha
        parent = engine.store.get('experiment_decisions', proof['decision_id']) or {}
        if (parent.get('id') != proof['decision_id'] or parent.get('run_id') != run_id
                or parent.get('evidence') != context['validation_evidence']
                or not full.members(parent.get('analyses', [])) or full.reviewers(parent['analyses']) != full.REVIEWERS):
            raise ValueError('Earlier analysis reading is outside the current review')
        domain = context[full.KEY]['domain']
        if proof['version'] == 3:
            review = parent.get(HANDOFF, {}).get('independent_review') or {}
            assignment = review.get('assignment') or {}
            if (domain != assignment.get('purpose') or assignment['context'].get(KEY) != proof
                    or domain != 'experiment_diagnose:' + parent['evidence']['sha256'] + ':artifact-review-1:' + sha(review.get('assessment'))):
                raise ValueError('Earlier analysis reading is outside the current fact review')
        elif (parent.get(KEY) != proof or parent.get('proposal') != context.get('decision_proposal')
              or sha(parent['proposal']) != context.get('proposal_sha256')
              or domain != 'experiment_vote:' + parent['proposal_sha256']
              or parent['proposal_sha256'] != context['proposal_sha256']):
            raise ValueError('Earlier analysis reading is outside the current proposal vote')
        vote = next(v for v in parent['analyses'] if v['role'] == role)
        prior = _analysis_archive(engine, run_id, parent, vote, allow_reassessment=True)
        if prior is None or any(k not in prior or full.sha(prior[k]) != v for k, v in proof['sections'].items()):
            raise ValueError('Reviewer did not receive the earlier analysis sections')
        return
    if proof.get('version') == 2:
        if context[full.KEY]['domain'] != 'experiment_diagnose:' + context['validation_evidence']['sha256']:
            raise ValueError('Prior analysis reading is outside the current analysis')
        parent = _analysis_parent(engine.store, run_id, context[full.MATERIALS]['evidence'])
        if not parent or parent['id'] != proof['decision_id']:
            raise ValueError('Prior analysis reading lost its reviewed predecessor')
        vote = next(v for v in parent['analyses'] if v['role'] == role)
        prior = _analysis_archive(engine, run_id, parent, vote)
        if (prior is None or proof != _analysis_difference(parent, prior, context[full.MATERIALS])):
            raise ValueError('Reviewer did not receive the referenced archive sections')
        return
    revision = engine.store.get('experiment_decisions', proof['revision_id']) or {}
    if (revision.get('run_id') != run_id or revision.get('evidence') != context['validation_evidence']
            or revision.get('proposal') != context.get('decision_proposal')
            or revision.get('proposal_sha256') != context.get('proposal_sha256')
            or context[full.KEY]['domain'] != 'experiment_vote:' + revision['proposal_sha256']):
        raise ValueError('Previous reading is outside this revised proposal vote')
    immediate = original(engine.store, run_id, revision['consensus_parent'], revision['evidence'])
    if proof['decision_id'] != ((immediate.get(KEY) or {}).get('decision_id') or immediate['id']):
        raise ValueError('Previous reading refers to a different consensus predecessor')
    parent = original(engine.store, run_id, proof['decision_id'], context['validation_evidence'])
    if not full.members(parent['votes']) or full.reviewers(parent['votes']) != full.REVIEWERS:
        raise ValueError('Previous experiment reading lacks the original independent reviewers')
    vote = next(v for v in parent['votes'] if v['role'] == role)
    archive = source_vote(engine, run_id, parent, vote)
    if any(k not in archive or full.sha(archive[k]) != value for k, value in proof['sections'].items()):
        raise ValueError('This reviewer did not receive the unchanged experiment material')


def unexecuted_review(revision):
    return bool(revision and revision.get('check') is None and
                (revision.get('status'), revision.get('review_outcome')) in {
                    ('rejected', 'authenticated_veto'), ('admission_blocked', 'resource_admission_blocked')})


def _analysis_parent(store, run_id, evidence):
    revision = evidence.get('rapid_revision') or {}
    if not unexecuted_review(revision):
        return None
    parent = store.get('experiment_decisions', revision['decision_id'])
    if not parent:
        return None
    if (parent.get('id') != revision['decision_id'] or parent.get('run_id') != run_id or
            parent.get('cycle') != evidence['binding']['cycle'] or parent.get('status') != 'approved'):
        raise ValueError('Rejected candidate is not linked to its approved decision')
    votes = parent.get('analyses', [])
    return parent if full.members(votes) and full.reviewers(votes) == full.REVIEWERS else None


def _analysis_archive(engine, run_id, parent, vote, *, preparing=False, allow_reassessment=False):
    from . import review_material_packet as packets
    hid = vote[full.KEY]['handoff_id']
    if preparing and any(not engine.store.get(c, hid) for c in ('handoff_inputs', 'handoffs', 'decisions')):
        return None
    frozen = full.authenticate(engine.store, run_id, vote)['context']
    if ((KEY in frozen and not (allow_reassessment and frozen[KEY].get('version') == 2))
            or packets.enabled(frozen) or vote.get('evidence_sha256') != parent['evidence']['sha256']
            or frozen[full.KEY]['domain'] != 'experiment_diagnose:' + parent['evidence']['sha256']
            or frozen.get('validation_evidence') != parent['evidence'] or not full.required_root_chars(frozen)):
        return None
    try:
        full.validate_delivery(engine.store, run_id, vote, engine=engine)
    except ValueError:
        if preparing:
            return None
        raise
    return frozen[full.MATERIALS]


def _analysis_difference(parent, prior, current):
    before, after = prior['evidence'], current['evidence']
    if full.sha(before['binding']) != full.sha(after['binding']):
        return None
    sections = {k: full.sha(v) for k, v in after.items() if k in before and full.sha(v) == full.sha(before[k])}
    sources = full.sha(current['sources'])
    if full.sha(prior['sources']) != sources:
        sources = None
    if sections or sources:
        return {'version': 2, 'decision_id': parent['id'], 'prior_evidence': parent['evidence'],
                'evidence_sections': sections, 'sources_sha256': sources,
                'removed_sections': sorted(before.keys() - after.keys())}


def prepare_analysis(engine, run_id, context):
    current = full.resolve_materials(engine.store, run_id, context)
    parent = _analysis_parent(engine.store, run_id, current['evidence'])
    if not parent:
        return None
    proofs = []
    for vote in parent['analyses']:
        prior = _analysis_archive(engine, run_id, parent, vote, preparing=True)
        if prior is None:
            return None
        proofs.append(_analysis_difference(parent, prior, current))
    return proofs[0] if all(p == proofs[0] for p in proofs) else None


def prepare_after_analysis(engine, run_id, record, context, version):
    votes = record.get('analyses', [])
    if not full.members(votes) or full.reviewers(votes) != full.REVIEWERS:
        return None
    current = full.resolve_materials(engine.store, run_id, context)
    sections = {k: full.sha(current[k]) for k in ('evidence', 'sources')}
    for vote in votes:
        prior = _analysis_archive(engine, run_id, record, vote, preparing=True, allow_reassessment=True)
        if prior is None:
            return None
        sections = {k: digest for k, digest in sections.items() if k in prior and full.sha(prior[k]) == digest}
    if sections:
        return {'version': version, 'decision_id': record['id'], 'sections': sections}


def prior_diagnostic_review(engine, run_id, evidence, ref):
    """Supply actual upstream findings when only the implementation was rejected."""
    from . import experiment_evidence_handoff as handoff
    from .experiment_artifact_review import require_binding
    from .experiment_decisions import _approved
    from .experiment_workflow import sha
    from .training_investigation import scoped_repair
    recent = evidence.get('rapid_revision') or {}
    if not unexecuted_review(recent):
        return None
    account = engine.store.get('experiment_evidence', ref['id']) or {}
    if (account.get('run_id') != run_id or sha(evidence) != ref['sha256']
            or sha(account.get('summary')) != ref['sha256']):
        raise ValueError('Current diagnostic evidence lost its frozen reference')
    parent = engine.store.get('experiment_decisions', recent['decision_id']) or {}
    proposal = parent.get('proposal') or {}
    saved = parent.get(handoff.KEY) or {}
    review = saved.get('independent_review') or {}
    if not scoped_repair(proposal) or not review.get('results'):
        return None
    previous = engine.store.get('rapid_experiment_revisions', recent['id']) or {}
    base = engine.store.get('experiment_evidence', parent['evidence']['id']) or {}
    if (previous.get('id') != recent['id'] or previous.get('run_id') != run_id
            or previous.get('cycle') != evidence['binding']['cycle'] or previous.get('status') != recent['status']
            or previous.get('check') is not None or previous.get('review_outcome') != recent['review_outcome']
            or sha(previous.get('votes')) != sha(recent['votes']) or previous.get('decision_id') != parent.get('id')
            or previous.get('decision_sha256') != sha(proposal) or previous.get('evidence') != parent['evidence']
            or previous.get('candidate', {}).get('code_sources') != recent.get('code_sources')
            or parent.get('run_id') != run_id or not _approved(parent) or not require_binding(parent)
            or base.get('run_id') != run_id or sha(base.get('summary')) != parent['evidence']['sha256']):
        raise ValueError('Prior diagnostic findings lost their original work order or review')
    if (sha(base['summary']['binding']) != sha(evidence['binding']) or
            sha(base['summary']['records']) != sha(evidence['records'])):
        return None
    archive = handoff.bound_evidence(engine, run_id, parent, proposal)
    if not archive:
        raise ValueError('Prior diagnostic artifact archive is unavailable')
    findings = []
    for vote in review['results']:
        if full.KEY not in vote:
            return None
        full.authenticate(engine.store, run_id, vote)
        if vote['artifact_evidence_sha256'] != sha(archive):
            raise ValueError('Prior diagnostic reviewer refers to a different artifact archive')
        findings.append({k: vote[k] for k in ('role', 'conclusion', 'findings', 'remaining_gaps', 'next_step')}
                        | {'handoff_id': vote[full.KEY]['handoff_id']})
    packet = {'decision_id': parent['id'], 'proposal_sha256': parent['proposal_sha256'],
            'evidence': parent['evidence'], 'artifact_evidence_sha256': sha(archive),
            'artifact_trial_ids': saved['request'].get('artifact_trial_ids'),
            'fact_reviews': findings, 'implementation_votes': [
                {k: vote[k] for k in ('role', 'approve', 'issues')} for vote in recent['votes']],
            'scope': '这些是上版已完成的产物核对和代码审查。沿用仍适用的事实，保留已记录的未知，核对退回项和本轮可执行的下一步。原批准仅适用于原方案，未执行的检查没有测量结果。'}
    job = previous.get('codex_work_item')
    if job:
        source = recent['code_sources']
        archived = engine.store.get('experiment_sources', source['id']) or {}
        if archived.get('run_id') != run_id or sha(archived.get('files')) != source['sha256']:
            raise ValueError('Prior candidate source lost its archive binding')
        original = archived['files'].get('work_order.json')
        if original and 'text' in original:
            if original['sha256'] != job['input_sha256']:
                raise ValueError('Prior work order differs from the reviewed candidate')
            packet['original_work_order'] = {'name': 'experiments.inspect_module', 'arguments': {
                'evidence_id': ref['id'], 'expected_sha256': ref['sha256'], 'snapshot': 'candidate',
                'path': 'work_order.json', 'view': 'source'}}
    return packet


def prior_artifact_observations(engine, run_id, record, evidence):
    """Attach earlier source-bound observations to a new fact request for the same attempts."""
    from .experiment_consensus import vote_request
    from . import experiment_evidence_handoff as handoff
    from .artifact_fact_review import reference
    from .experiment_workflow import sha
    saved = record[handoff.KEY]
    current = saved['request'].get('artifact_trial_ids')
    if not current:
        return None
    prior = prior_diagnostic_review(engine, run_id, evidence, record['evidence'])
    if not prior or sorted(prior.get('artifact_trial_ids') or []) != sorted(current):
        return None
    parent = engine.store.get('experiment_decisions', prior['decision_id'])
    current_ref = reference(saved) if saved.get('independent_review') else saved['evidence']
    supplied = handoff.archived_evidence(engine, run_id, record, current_ref['sha256'])
    earlier = handoff.archived_evidence(engine, run_id, parent, prior['artifact_evidence_sha256'])
    expected = {r['id']: (sha(r), sha(r['params'])) for r in evidence['records'] if r['id'] in current}
    if len(expected) != len(current) or set(expected) != set(current):
        raise ValueError('Selected diagnostic attempts are missing from current evidence')
    for archive in (supplied, earlier):
        if 'artifact_trial_ids' in archive and sorted(archive['artifact_trial_ids']) != sorted(current):
            raise ValueError('Diagnostic archive selection differs from the request')
        summaries = [t['summary'] for t in archive['trials'] if t['summary']['trial_id'] in expected]
        identities = {s['trial_id']: (s.get('record_projection_sha256'), s.get('params_sha256')) for s in summaries}
        if len(summaries) != len(expected) or identities != expected:
            raise ValueError('Diagnostic observations refer to different archived attempts')
    snapshots = []
    for vote in parent['votes']:
        request = vote_request(engine, run_id, parent, vote)['context']
        if (not vote['approve'] or vote['proposal_sha256'] != prior['proposal_sha256']
                or request['decision_proposal'] != parent['proposal'] or request['validation_evidence'] != prior['evidence']):
            raise ValueError('Prior observations belong to another diagnostic decision')
        fields = {}
        for key in ('saved_weight_audit', 'saved_validation_configuration'):
            item = request['experiment_decision'].get(key)
            if item:
                if sha(item['result']) != item['sha256']:
                    raise ValueError('Prior diagnostic observation digest changed')
                result = item['result']
                ids = (set(result['weight_files']) | set(result['fit_histories']) if key == 'saved_weight_audit'
                       else {r['trial_id'] for r in result['trials']})
                if not ids.issubset(current):
                    continue
                fields[key] = {k: item[k] for k in ('sha256', 'result')}
        snapshots.append(fields)
    if any(sha(item) != sha(snapshots[0]) for item in snapshots):
        raise ValueError('Prior reviewers received different diagnostic observations')
    return {**prior, 'as_observed_in_prior_review': snapshots[0]}
