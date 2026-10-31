"""Read projections for the run workspace; evidence is fetched only on demand.

These projections are display hints. Execution and recovery retain their complete
original validators. SQL selects metadata before JSON decoding, so opening a
workspace never materializes saved model responses, notes or context histories.
"""
from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from .orchestration import STAGES
from .observability import LABELS, PURPOSES
from .storage import redact


SECTIONS = frozenset({'command', 'branches', 'orchestration', 'timeline', 'handoffs', 'innovation',
                     'consensus', 'evidence', 'implementation', 'experiments', 'artifacts', 'context'})


def _field(path):
    return "json_extract(data,'$." + path + "')"


def _rows(store, collection, fields, *, run_id=None, key=None, limit=None, where='', args=(),
          order_by='updated_at DESC,key DESC'):
    """Internal, fixed SQL expressions; request values are always parameters."""
    fields = {name: _field(name) for name in fields} if not isinstance(fields, dict) else fields
    expressions = ','.join("'" + name + "'," + expression for name, expression in fields.items())
    query = f'SELECT json_object({expressions}) AS projection FROM records WHERE collection=?'
    params = [collection]
    if run_id is not None:
        query += " AND json_extract(data,'$.run_id')=?"
        params.append(run_id)
    if key is not None:
        query += ' AND key=?'
        params.append(key)
    if where:
        query += ' AND ' + where
        params.extend(args)
    query += ' ORDER BY ' + order_by
    if limit is not None:
        query += ' LIMIT ?'
        params.append(limit)
    with store._read_db() as db:
        return [json.loads(row['projection']) for row in db.execute(query, params)]


def _one(store, collection, fields, **kwargs):
    return next(iter(_rows(store, collection, fields, limit=1, **kwargs)), None)


def _array(value):
    return json.loads(value) if isinstance(value, str) else value or []


def revision(store, run_id):
    """An exact database observation token, not a scientific version or hash."""
    # data_version is connection-local. Metadata timestamps plus total_changes
    # would miss another connection, so observe persisted per-run record heads.
    with store._read_db() as db:
        collections = ('steps', 'research_cycles', 'handoffs', 'handoff_inputs', 'decisions', 'graph_status', 'consensus', 'proposal_reviews',
            'architecture_reviews', 'architecture_revisions', 'architecture_definitions', 'architecture_progress',
            'innovation_sessions', 'experiments', 'artifacts', 'budgets', 'usage', 'moderation_reviews',
            'literature_inputs', 'contexts', 'context_snapshots', 'codex_workflows', 'protocol_reviews',
            'implementation_document_drafts', 'architecture_diagrams', 'handoff_progress',
            'dataset_experiment_setups', 'dataset_codex_jobs', 'dataset_agent_plans')
        rows = [(collection, *db.execute("""SELECT count(*),max(updated_at) FROM records
            WHERE collection=? AND json_extract(data,'$.run_id')=?""", (collection, run_id)).fetchone())
            for collection in collections]
        rows.append(('runs', *db.execute("SELECT count(*),max(updated_at) FROM records WHERE collection='runs' AND key=?", (run_id,)).fetchone()))
        rows.append(('settings', *db.execute("SELECT count(*),max(updated_at) FROM records WHERE collection='settings' AND key='main'").fetchone()))
        event = db.execute('SELECT coalesce(max(id),0) FROM events WHERE run_id=?', (run_id,)).fetchone()[0]
    return hashlib.sha256(json.dumps([rows, event], ensure_ascii=False).encode()).hexdigest()[:24]


def _steps(store, run_id):
    fields = {name: _field(name) for name in ('id', 'run_id', 'stage', 'status', 'cycle', 'started_at', 'completed_at', 'error')}
    fields.update(activity="json_extract(data,'$.output.activity')",
                  semantic_completion="json_extract(data,'$.output.semantic_completion')",
                  tuning_pending="json_extract(data,'$.output.tuning_pending')",
                  has_output="json_type(data,'$.output') IS NOT NULL AND json_type(data,'$.output')!='null'",
                  summary="substr(coalesce(json_extract(data,'$.output.summary'),''),1,320)")
    values = _rows(store, 'steps', fields, run_id=run_id)
    for value in values:
        value.update(has_output=bool(value['has_output']), output_loaded=False)
    return sorted(values, key=lambda value: STAGES.index(value['stage']) if value['stage'] in STAGES else len(STAGES))


def _convergence(store, run, scope):
    cycle = run.get('state', {}).get('research_cycle', 1)
    fields = {name: _field(name) for name in ('status', 'number', 'consecutive_no_progress', 'repeated_state',
                                              'supported_progress', 'reason', 'progress_roles')}
    rows = _rows(store, 'architecture_progress', fields, run_id=run['id'],
                 where="json_extract(data,'$.cycle')=? AND json_extract(data,'$.scope')=?", args=(cycle, scope))
    last = max(rows, key=lambda row: row.get('number') or 0) if rows else {}
    window = run['config'].get('architecture_stagnation_rounds', 3)
    count = last.get('consecutive_no_progress') or 0
    return {'mode': 'until_stalled', 'round_limit': None, 'completed_rounds': len(rows),
            'consecutive_no_progress': count, 'stagnation_window': window,
            'stalled': bool(last and last.get('status') != 'approved' and
                            (last.get('repeated_state') and not last.get('supported_progress') or count >= window)),
            'reason': last.get('reason'), 'progress_roles': last.get('progress_roles') or [],
            'repeated_state': bool(last.get('repeated_state')), 'scientific_approval': False,
            'details_loaded': False}


def _definition(store, run, saved):
    fields = {name: _field(name) for name in ('id', 'status', 'reason', 'review_protocol', 'authoring_protocol',
                                             'review_unit_count', 'document')}
    for name, path in [('issue_count', 'issues'), ('received_votes', 'votes'), ('saved_issue_count', 'issue_drafts'),
                       ('saved_review_units', 'unit_reviews'), ('scope_issue_count', 'candidate.scope_issues')]:
        fields[name] = f"coalesce(json_array_length(data,'$.{path}'),0)"
    fields.update(active_issue_number=_field('active_issue_handoff.number'),
                  active_review_unit_number=_field('active_review_handoff.unit_number'),
                  has_candidate="json_type(data,'$.candidate')='object'",
                  invalid_handoff_id=_field('invalid_review.handoff_id'),
                  votes="(SELECT json_group_array(json_object('role',json_extract(value,'$.role'),"
                        "'approve',json_extract(value,'$.approve'),'handoff_id',json_extract(value,'$.handoff_id'),"
                        "'required_changes_count',coalesce(json_array_length(value,'$.required_changes'),0))) FROM json_each(data,'$.votes'))",
                  unresolved_issue_count="CASE WHEN json_type(data,'$.candidate')='object' THEN "
                        "(SELECT count(*) FROM json_each(data,'$.candidate.definitions') WHERE json_extract(value,'$.status')='unresolved') "
                        "ELSE (SELECT count(*) FROM json_each(data,'$.issue_drafts') WHERE json_extract(value,'$.result.definitions[0].status')='unresolved') END")
    fields['scope_issue_count'] = ("CASE WHEN json_type(data,'$.candidate')='object' THEN "
        "coalesce(json_array_length(data,'$.candidate.scope_issues'),0) ELSE "
        "(SELECT count(DISTINCT item.value) FROM json_each(data,'$.issue_drafts') draft, "
        "json_each(draft.value,'$.result.scope_issues') item) END")
    value = _one(store, 'architecture_definitions', fields, key=saved.get('definition_handoff_id', ''))
    if not value:
        return None
    value['votes'] = [{**v, 'approve': bool(v['approve']) if v['approve'] is not None else None} for v in _array(value['votes'])]
    value.update(expected_votes=3, expected_review_units=(value.get('review_unit_count') or 0) * 3,
                 details_loaded=False, scientific_approval=False)
    progress = _convergence(store, run, 'definitions')
    delivery = None
    if (value.get('review_protocol') == 3 and value.get('invalid_handoff_id')
            and not any(v.get('handoff_id') == value['invalid_handoff_id'] for v in value['votes'])):
        # Only this rare recovery case needs the exact scope operands; never load
        # the original prompt, candidate, body or evidence histories.
        record = _one(store, 'architecture_definitions', ['issues', 'invalid_review', 'review_protocol'], key=value['id'])
        record['votes'] = value['votes']
        class ScopeStore:
            def get(self, collection, key, default=None):
                row = _one(store, collection, {'definition_issues': _field('context.definition_issues'),
                    'previous_architecture_review': _field('context.previous_architecture_review'),
                    'definition_final_scope_protocol': _field('context.definition_final_scope_protocol')}, key=key)
                return {'context': row} if row else default
        from .architecture_final_delivery import diagnostic
        delivery = diagnostic(SimpleNamespace(store=ScopeStore()), run['id'], record)
    eligible = (run['mode'] == 'live' and run['stage'] == 'design' and run['status'] in {'blocked', 'failed', 'paused'}
                and saved['status'] == 'needs_consensus' and value['status'] in {'blocked', 'rejected'}
                and bool(value['document']) and bool(value.pop('has_candidate')))
    value.pop('has_candidate', None)
    value.pop('invalid_handoff_id', None)
    used = saved.get('definition_version_count') or 0
    value['completion'] = {'can_prepare': eligible and not progress['stalled'] and not delivery,
        'limit_reached': False, 'versions_used': used, 'version_limit': None, 'next_version': used + 1,
        'convergence': progress, 'delivery_recovery': delivery, 'preparation_starts_research': False}
    return value


def architecture_summary(store, run):
    from .engine import digest
    cycle = run.get('state', {}).get('research_cycle', 1)
    reviews = _rows(store, 'architecture_reviews', ['id', 'status', 'cycle', 'created_at', 'document', 'review_delivery_validation'],
                   run_id=run['id'], where="json_extract(data,'$.cycle')=?", args=(cycle,))
    current = max(reviews, key=lambda row: row.get('created_at') or '') if reviews else None
    fields = {name: _field(name) for name in ('id', 'version', 'status', 'cycle', 'reason', 'original_design_hash',
        'authority_document', 'definition_handoff_id', 'delivery_validation')}
    fields.update(revision_count="coalesce(json_array_length(data,'$.revisions'),0)",
                  definition_version_count="coalesce(json_array_length(data,'$.definition_handoffs'),0)",
                  current_design_hash="coalesce(json_extract(data,'$.revisions[#-1].design_hash'),json_extract(data,'$.original_design_hash'))",
                  pending_parent_design_hash=_field('pending.context.parent_design_hash'),
                  pending_handoff_id=_field('pending.handoff_id'),
                  integration_protocol=_field('pending.context.architecture_integration_protocol'))
    consensus = _one(store, 'consensus', ['proposal'], key=run['id']) or {}
    key = f"{run['id']}:architecture-revisions:{cycle}:{digest(consensus.get('proposal'))}"
    saved = _one(store, 'architecture_revisions', fields, key=key) if run['config'].get('architecture_revision_protocol') == 1 else None
    if saved:
        state = saved.get('delivery_validation')
        if state:
            state['can_prepare'] = state['status'] == 'delivery_validation_failed'
        elif (run['status'] in {'blocked', 'paused', 'failed'} and saved.get('integration_protocol') == 1
              and 'schema validation' in (run.get('error') or '')
              and _one(store, 'decisions', ['id'], key=saved.get('pending_handoff_id', ''))):
            saved['delivery_validation'] = {'status': 'delivery_validation_failed', 'can_prepare': True,
                'original_handoff_id': saved['pending_handoff_id'], 'error': run['error'],
                'reason': '原交付已保存，合成架构未通过校验；可准备一次保留原约束的定向补正。',
                'scientific_approval': False, 'attempt_limit': 1}
        saved['definition_review'] = _definition(store, run, saved)
        saved['convergence'] = _convergence(store, run, 'architecture')
        saved['details_loaded'] = False
        for field in ('pending_handoff_id', 'integration_protocol', 'definition_handoff_id', 'definition_version_count'):
            saved.pop(field, None)
    return {'required': run['config'].get('architecture_document_protocol') == 1, 'current': current,
            'revision': saved, 'details_loaded': False}


def _handoffs(store, run_id, *, limit=30, activity=None):
    from .implementation_activity import predicate, receipt_metadata
    where, args = predicate(receipt_metadata(store, run_id) if activity is None else activity, 'handoffs')
    return _rows(store, 'handoffs', ['id', 'run_id', 'from_role', 'to_role', 'purpose', 'status',
        'error', 'created_at', 'updated_at'], run_id=run_id, limit=limit, where=where, args=args)


def _agent_configuration(store):
    """Match settings' partial/default merge using only public display fields."""
    from .config import DEFAULTS
    roles = dict(DEFAULTS['roles'])
    providers = {p['id']: {key: p.get(key) for key in ('id', 'name', 'model')} for p in DEFAULTS['providers']}
    saved = _one(store, 'settings', {'roles': _field('roles'),
        'providers': "(SELECT json_group_array(json_object('id',json_extract(p.value,'$.id'),"
            "'name',json_extract(p.value,'$.name'),'has_name',json_type(p.value,'$.name') IS NOT NULL,"
            "'model',json_extract(p.value,'$.model'),'has_model',json_type(p.value,'$.model') IS NOT NULL)) "
            "FROM json_each(data,'$.providers') p)"}, key='main') or {}
    roles.update(saved.get('roles') or {})
    for item in _array(saved.get('providers')):
        provider = providers.setdefault(item['id'], {'id': item['id']})
        provider.update({field: item[field] for field in ('name', 'model') if item['has_' + field]})
    return {role: providers.get(provider_id, {}) for role, provider_id in roles.items()}


def _review_feedback_fields():
    """Select a bounded preview without materializing a full review response."""
    limit, characters = 5, 600
    fields, clipped = {}, []
    for name in ('issues', 'required_changes'):
        path = '$.result.' + name
        fields[name + '_count'] = f"CASE WHEN json_type(data,'{path}')='array' THEN json_array_length(data,'{path}') END"
        # Keep array positions: a malformed historical entry must not renumber
        # the issue_index values carried by otherwise usable public fixes.
        fields[name] = f"""(SELECT json_group_array(CASE WHEN item.type='text'
            THEN substr(item.value,1,{characters}) ELSE NULL END)
            FROM json_each(data,'{path}') item
            WHERE json_type(data,'{path}')='array' AND CAST(item.key AS INTEGER)<{limit})"""
        clipped.append(f"coalesce(json_array_length(data,'{path}'),0)>{limit}")
        clipped.append(f"""EXISTS (SELECT 1 FROM json_each(data,'{path}') item
            WHERE item.type='text' AND CAST(item.key AS INTEGER)<{limit}
            AND length(item.value)>{characters})""")
    text_fields = ('location', 'change', 'verification', 'target', 'reason',
                   'proposed_change', 'verification_plan', 'target_field')
    index = "coalesce(json_extract(fix.value,'$.issue_index'),json_extract(fix.value,'$.objection_index'))"
    # Attach the matching original issue even when its index is outside the
    # small issues preview. Do not bind an index to another task or list.
    issue_path = f"'$.result.' || CASE WHEN json_type(data,'$.result.issues')='array' THEN 'issues' ELSE 'required_changes' END || '[' || {index} || ']'"
    issue = f"CASE WHEN typeof({index})='integer' AND {index}>=0 THEN CASE WHEN json_type(data,{issue_path})='text' THEN json_extract(data,{issue_path}) END END"
    pairs = [f"'{name}',CASE WHEN json_type(fix.value,'$.{name}')='integer' THEN json_extract(fix.value,'$.{name}') END"
             for name in ('issue_index', 'objection_index')]
    pairs += [f"'{name}',CASE WHEN json_type(fix.value,'$.{name}')='text' THEN substr(json_extract(fix.value,'$.{name}'),1,{characters}) END"
              for name in text_fields]
    pairs.append(f"'issue',substr(({issue}),1,{characters})")
    fields['fixes'] = f"""(SELECT json_group_array(json_object({','.join(pairs)}))
        FROM json_each(data,'$.result.fixes') fix
        WHERE json_type(data,'$.result.fixes')='array' AND fix.type='object'
        AND CAST(fix.key AS INTEGER)<{limit})"""
    fields['fixes_count'] = "CASE WHEN json_type(data,'$.result.fixes')='array' THEN json_array_length(data,'$.result.fixes') END"
    clipped.append(f"coalesce(json_array_length(data,'$.result.fixes'),0)>{limit}")
    lengths = [f"length(json_extract(fix.value,'$.{name}'))>{characters}" for name in text_fields]
    lengths.append(f"length(({issue}))>{characters}")
    clipped.append(f"""EXISTS (SELECT 1 FROM json_each(data,'$.result.fixes') fix
        WHERE fix.type='object' AND CAST(fix.key AS INTEGER)<{limit}
        AND ({' OR '.join(lengths)}))""")
    routing_path = '$.result.review_routing.issues'
    fields['review_routing_version'] = "CASE WHEN json_type(data,'$.result.review_routing.version')='integer' THEN json_extract(data,'$.result.review_routing.version') END"
    fields['review_routing_issues_count'] = f"CASE WHEN json_type(data,'{routing_path}')='array' THEN json_array_length(data,'{routing_path}') END"
    fields['review_routing_issues'] = f"""(SELECT json_group_array(json_object(
        'issue_index',CASE WHEN json_type(item.value,'$.issue_index')='integer' THEN json_extract(item.value,'$.issue_index') END,
        'kind',substr(json_extract(item.value,'$.kind'),1,80),
        'objection_id',substr(json_extract(item.value,'$.objection_id'),1,{characters}),
        'recovery_condition',substr(json_extract(item.value,'$.recovery_condition'),1,{characters})))
        FROM json_each(data,'{routing_path}') item
        WHERE item.type='object' AND CAST(item.key AS INTEGER)<{limit})"""
    # All kinds matter: an omitted later document defect must not make a mixed
    # review look like it needs only further reading.
    fields['review_routing_kinds'] = f"""(SELECT json_group_array(DISTINCT json_extract(item.value,'$.kind'))
        FROM json_each(data,'{routing_path}') item WHERE item.type='object'
        AND json_extract(item.value,'$.kind') IN ('document_revision','external_dependency','review_incomplete'))"""
    clipped.append(f"coalesce(json_array_length(data,'{routing_path}'),0)>{limit}")
    clipped.append(f"""EXISTS (SELECT 1 FROM json_each(data,'{routing_path}') item
        WHERE item.type='object' AND CAST(item.key AS INTEGER)<{limit}
        AND (length(json_extract(item.value,'$.recovery_condition'))>{characters}
             OR coalesce(json_array_length(item.value,'$.required_reads'),0)>0))""")
    fields['feedback_truncated'] = '(' + ' OR '.join(clipped) + ')'
    return fields


def _assignment_scope(store, run):
    """Share current assignment metadata only inside one SQLite read snapshot."""
    memoize = getattr(store, 'memoized_read', None)
    if callable(memoize):
        state = run.get('state', {})
        identity = ('run_assignment_scope', run['id'], run.get('stage'),
                    state.get('research_cycle'), state.get('implementation_generation'))
        return memoize(identity, lambda: _build_assignment_scope(store, run))
    return _build_assignment_scope(store, run)


def _build_assignment_scope(store, run):
    """Current-stage metadata, with legacy fields left unknown rather than invented."""
    stage = run.get('stage')
    if not stage:
        return '', (), None
    cycle = run.get('state', {}).get('research_cycle')
    clauses = ["(json_extract(data,'$.stage') IS NULL OR json_extract(data,'$.stage')=?)"]
    args = [stage]
    if cycle is not None:
        clauses.append("(json_extract(data,'$.cycle') IS NULL OR json_extract(data,'$.cycle')=?)")
        args.append(cycle)
        clauses.append("""NOT EXISTS(SELECT 1 FROM records scope_input
            WHERE scope_input.collection='handoff_inputs' AND scope_input.key=records.key
                AND json_extract(scope_input.data,'$.run_id')=json_extract(records.data,'$.run_id')
                AND coalesce(json_extract(scope_input.data,'$.context.research_cycle'),
                    json_extract(scope_input.data,'$.context.cycle'),
                    json_extract(scope_input.data,'$.context.implementation_indexed_review_contract.binding.cycle'))!=?)""")
        args.append(cycle)
    workflow = None
    if stage in {'experiments', 'tuning'}:
        from .dataset_codex_view import execution_view
        if execution_view(store, run):
            # Native file delivery replaced this reasoning assignment; retain its
            # interruption in history without presenting it as current work.
            clauses.append("coalesce(json_extract(data,'$.purpose'),'') NOT LIKE 'dataset_configure:%'")
    if stage == 'implementation':
        from .implementation_progress import _latest, _current_spec
        workflow = _latest(store, 'codex_workflows', run['id'], cycle or 1, {
            'id': 'key', 'status': _field('status'), 'revision': _field('current_revision'),
            'document_id': _current_spec("json_extract(s.value,'$.document.document_id')")},
            generation=run.get('state', {}).get('implementation_generation'))
        if workflow:
            clauses.append("""(substr(json_extract(records.data,'$.purpose'),1,
                instr(json_extract(records.data,'$.purpose')||':',':')-1)
                NOT IN ('implementation_spec','implementation_document_review') OR EXISTS(
                SELECT 1 FROM records scope_input WHERE scope_input.collection='handoff_inputs'
                    AND scope_input.key=records.key
                    AND json_extract(scope_input.data,'$.run_id')=json_extract(records.data,'$.run_id')
                    AND coalesce(json_extract(scope_input.data,'$.context.implementation_document_authoring.workflow_id'),
                        json_extract(scope_input.data,'$.context.implementation_indexed_review_contract.binding.workflow_id'))=?
                    AND (? IS NULL OR coalesce(json_extract(scope_input.data,'$.context.implementation_document_authoring.revision'),
                        json_extract(scope_input.data,'$.context.implementation_indexed_review_contract.binding.revision'))=?)))""")
            args.extend((workflow['id'], workflow['revision'], workflow['revision']))
    return ' AND '.join(clauses), tuple(args), workflow


def agent_assignment_summary(store, run, role, *, activity=None):
    """Bind the card to one actual assignment and its own committed output.

    SQL projects bounded public fields; neither complete prompts nor response
    bodies/history are loaded just to populate a role's current-task card.
    """
    from .implementation_activity import predicate, receipt_metadata
    from .assignment_activity import generation_condition, view as assignment_activity
    activity = receipt_metadata(store, run['id']) if activity is None else activity
    visible, visible_args = predicate(activity, 'handoffs')
    # Preserve rejected dispatches in history, but do not let an audited,
    # never-dispatched replacement hide the actual author and its final report.
    visible += """ AND NOT (json_extract(records.data,'$.status')='cancelled' AND EXISTS(
        SELECT 1 FROM records rejected WHERE rejected.collection='stage_contexts'
            AND rejected.key=records.key||':undispatched-cancel'
            AND json_extract(rejected.data,'$.run_id')=json_extract(records.data,'$.run_id')
            AND json_extract(rejected.data,'$.payload.handoff.id')=records.key
            AND json_extract(rejected.data,'$.payload.handoff.status')='queued')
        AND NOT EXISTS(SELECT 1 FROM records dispatched
            WHERE dispatched.collection IN ('handoff_progress','decisions') AND dispatched.key=records.key))"""
    generation = run.get('state', {}).get('implementation_generation')
    if generation is not None:
        visible += ' AND ' + generation_condition()
        visible_args = (*visible_args, generation)
    scope, scope_args, workflow = _assignment_scope(store, run)
    scoped_visible = visible + (' AND ' + scope if scope else '')
    scoped_args = (*visible_args, *scope_args)
    fields = ['id', 'purpose', 'status', 'error', 'activity', 'created_at', 'updated_at']
    current = _one(store, 'handoffs', fields, run_id=run['id'],
        where="json_extract(data,'$.to_role')=? AND " + scoped_visible, args=(role, *scoped_args),
        order_by="(json_extract(data,'$.status')='running') DESC,"
                 "coalesce(json_extract(data,'$.created_at'),updated_at) DESC,key DESC") or {}
    historical = None
    if not current:
        historical = _one(store, 'handoffs', fields, run_id=run['id'],
            where="json_extract(data,'$.to_role')=? AND " + visible, args=(role, *visible_args),
            order_by="coalesce(json_extract(data,'$.created_at'),updated_at) DESC,key DESC")
        # A previous delivery remains visible; a stale unresolved assignment is
        # history, not a blocker or pending task in the new stage/workflow.
        if historical and historical.get('status') == 'completed':
            current = historical
    waiting_document = (not current and role == 'director' and workflow
                        and workflow.get('status') == 'specification' and not workflow.get('document_id'))
    status = current.get('status') or 'idle'
    if status == 'running' and run['status'] != 'running':
        status = run['status'] if run['status'] in {'blocked', 'paused', 'failed', 'cancelled'} else 'pending'
    with store._read_db() as db:
        active_count = db.execute("""SELECT count(*) FROM records WHERE collection='handoffs'
            AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.to_role')=?
            AND json_extract(data,'$.status')='running' AND """ + scoped_visible,
            (run['id'], role, *scoped_args)).fetchone()[0]
    last = current or historical or {}
    task = PURPOSES.get((last.get('purpose') or '').split(':')[0], last.get('purpose')) or '等待分派'
    assignment, output = None, None
    if current:
        context = _one(store, 'handoff_inputs', {
            'document_title': "substr(coalesce(json_extract(data,'$.context.implementation_document.title'),"
                "json_extract(data,'$.context.architecture_document.title')),1,500)",
            'authoring_revision': "CASE WHEN json_type(data,'$.context.implementation_document_authoring.revision')='integer' "
                "THEN json_extract(data,'$.context.implementation_document_authoring.revision') END",
            'review_revision': "CASE WHEN json_type(data,'$.context.implementation_indexed_review_contract.binding.revision')='integer' "
                "THEN json_extract(data,'$.context.implementation_indexed_review_contract.binding.revision') END",
            'unit_number': _field('context.review_unit.number'),
            'unit_total': _field('context.review_unit.total'),
            'followup': _field('context.implementation_review_followup.sequence')},
            key=current['id'], run_id=run['id']) or {}
        # An author's architecture is an input, not the implementation version
        # being produced. The frozen author revision is zero-based; its save
        # successor retains that revision even when the purpose adds :save:1.
        if (current.get('purpose') or '').split(':')[0] == 'implementation_spec':
            author_revision = context.get('authoring_revision')
            if not isinstance(author_revision, int) or author_revision < 0:
                identity = re.fullmatch(r'implementation_spec:[^:]+:(\d+)(?::save:\d+)?', current['purpose'])
                author_revision = int(identity.group(1)) if identity else None
            version_number = author_revision + 1 if author_revision is not None else None
        elif ((current.get('purpose') or '').split(':')[0] == 'implementation_document_review'
                and isinstance(context.get('review_revision'), int) and context['review_revision'] >= 0):
            # Readable filenames count retained outputs across workflows. The
            # frozen review binding identifies this workflow's document revision.
            version_number = context['review_revision'] + 1
        else:
            version = re.search(r'(?:specification|architecture)-v(\d+)\.md$', context.get('document_title') or '')
            version_number = int(version.group(1)) if version else None
        labels = [task]
        if version_number is not None:
            labels.append(f'第 {version_number} 版')
        if isinstance(context.get('unit_number'), int) and context['unit_number'] > 0:
            total = context.get('unit_total')
            labels.append(f"第 {context['unit_number']}{' / ' + str(total) if isinstance(total, int) and total > 0 else ''} 组")
        if isinstance(context.get('followup'), int) and context['followup'] > 0:
            labels.append(f"补充评议 {context['followup']}")
        assignment = {name: current.get(name) for name in ('id', 'purpose', 'created_at', 'updated_at')}
        assignment.update(status=status, label=' · '.join(labels))
        if status == 'completed':
            fields = {name: _field(name) for name in ('id', 'created_at')}
            for name in ('rationale', 'summary', 'explanation', 'hypothesis', 'architecture',
                         'research_question', 'markdown', 'title'):
                fields[name] = f"CASE WHEN json_type(data,'$.result.{name}')='text' THEN substr(json_extract(data,'$.result.{name}'),1,1200) END"
            fields.update(approve=_field('result.approve'),
                document_id="coalesce(json_extract(data,'$.result.document_id'),json_extract(data,'$.result.document.document_id'))",
                document_title="substr(json_extract(data,'$.result.document.title'),1,500)",
                plain="CASE WHEN json_type(data,'$.result')='text' THEN substr(json_extract(data,'$.result'),1,1200) END")
            fields.update(_review_feedback_fields())
            decision = _one(store, 'decisions', fields, key=current['id'], run_id=run['id'],
                            where="json_extract(data,'$.role')=?", args=(role,))
            if decision:
                summary = next((decision[name] for name in ('rationale', 'summary', 'explanation', 'hypothesis',
                    'architecture', 'research_question', 'markdown', 'title', 'plain') if decision.get(name)), None)
                if not summary:
                    if decision.get('document_id'):
                        summary = '已保存文档：' + (decision.get('document_title') or '实现规范文档')
                    elif isinstance(decision.get('fixes_count'), int):
                        summary = f"已提交 {decision['fixes_count']} 条修改意见。"
                    else:
                        summary = '已保存该任务的结构化公开输出，可查看完整记录。'
                consensus = {}
                if decision.get('approve') in (0, 1):
                    consensus['approve'] = bool(decision['approve'])
                if decision.get('rationale'):
                    consensus['rationale'] = decision['rationale']
                for name in ('issues', 'required_changes', 'fixes'):
                    if isinstance(decision.get(name + '_count'), int):
                        consensus[name] = _array(decision[name])
                        consensus[name + '_count'] = decision[name + '_count']
                if 'fixes' in consensus:
                    consensus['fixes'] = [{key: value for key, value in fix.items() if value is not None}
                                          for fix in consensus['fixes']]
                if isinstance(decision.get('review_routing_issues_count'), int):
                    consensus['review_routing'] = {'version': decision['review_routing_version'],
                        'issues': _array(decision['review_routing_issues'])}
                    consensus['review_routing_issues_count'] = decision['review_routing_issues_count']
                    consensus['review_routing_kinds'] = _array(decision['review_routing_kinds'])
                if any(name in consensus for name in ('issues', 'required_changes', 'fixes', 'review_routing')):
                    consensus['feedback_truncated'] = bool(decision['feedback_truncated'])
                output = {'id': current['id'] + ':output', 'at': decision['created_at'],
                    'purpose': current['purpose'], 'status': 'completed', 'kind': 'output',
                    'text': summary, 'truncated': True, 'consensus': consensus}
    current_activity = assignment_activity(store, run, current, status, role, defer_reading=True)
    original_status = status
    if current_activity and current_activity.get('state') == 'recovering':
        status = 'recovering'
        if assignment:
            assignment.update(status=status, original_status=original_status)
    return redact({'status': status, 'last_task': task,
        **({'original_status': original_status} if status == 'recovering' else {}),
        'current_task': '等待当前实现文档提交' if waiting_document else
            '等待下一次任务分派' if status == 'completed' else assignment['label'] if assignment else '等待分派',
        'current_assignment': assignment, 'current_assignments_count': active_count if run['status'] == 'running' else 0,
        'current_output': output, 'current_activity': current_activity,
        'current_action': '等待当前实现文档提交，随后独立审查；当前尚未派发审查任务。' if waiting_document else
            '已交付，等待下一次分派' if status == 'completed' else
            '正在恢复原请求，检查已保存回复' if status == 'running' and current.get('activity') == 'native_readback' else
            current_activity['detail'] if current_activity else
            current.get('error') or (run.get('error') if current.get('status') == 'running' else None) or
            '等待执行记录'})


def command_summary(store, run, steps, architecture=None, *, activity=None):
    from .implementation_activity import receipt_metadata
    from .dataset_codex_view import execution_view
    activity = receipt_metadata(store, run['id']) if activity is None else activity
    handoffs = _handoffs(store, run['id'], activity=activity)
    configured = _agent_configuration(store)
    agents = []
    for role in ('director', 'engineer', 'scout'):
        provider = configured.get(role, {})
        agents.append({'role': role, 'name': {'director': '研究总监', 'engineer': '实验工程师', 'scout': '文献研究员'}[role],
            'provider': provider.get('id'), 'provider_name': provider.get('name'), 'model': provider.get('model'),
            'configuration_source': 'current_settings', **agent_assignment_summary(store, run, role, activity=activity),
            'history': [], 'history_loaded': False, 'history_url': f"/api/runs/{run['id']}/agents/{role}/history"})
    by_stage = {step['stage']: step for step in steps}
    cycle = run.get('state', {}).get('research_cycle', 1)
    labels = {**LABELS, **({'experiments': '实验数据配置与验证'}
        if run.get('state', {}).get('dataset_configuration_protocol') == 1 else
        {'design': '数据与协议适配', 'implementation': '代码适配'} if run.get('state', {}).get('dataset_adaptation') else {})}
    mainline = []
    for stage in STAGES:
        if stage == 'revision' or stage == 'related_work' and run['config'].get('literature_protocol') != 2:
            continue
        step = by_stage.get(stage, {})
        current = stage == run['stage']
        status = 'skipped' if step.get('status') == 'skipped' else 'passed' if step.get('status') == 'completed' else 'pending'
        if current and run['status'] == 'running':
            status = 'active'
        elif current and stage == 'design' and architecture and architecture.get('required'):
            review = architecture.get('current') or {}
            revised = architecture.get('revision') or {}
            if revised.get('status') not in {None, 'approved'} or review.get('status') != 'approved':
                status = 'rejected' if review.get('status') == 'rejected' else 'pending'
        hold = run.get('validation_hold')
        if hold and stage in {'validation_review', 'final_tests'}:
            status = 'pending'
        mainline.append({'id': stage, 'label': labels[stage], 'status': status, 'cycle': cycle,
                         'is_current': current, 'activity_status': run['status'] if current else None,
                         'reason': hold['reason'] if hold and stage == 'validation_review' else run.get('error') if current else None})
    from .optimization_loop import view as optimization_view
    return {'projection': 'summary', 'details_loaded': False, 'optimization_loop':optimization_view(store,run), 'current_stage': run['stage'],
        'current_stage_label': '验证结果与停止规则待核对' if run.get('validation_hold') else labels.get(run['stage'], run['stage']), 'current_cycle': cycle,
        'run_status': run['status'], 'run_error': run.get('error'), 'progress': run.get('progress', 0),
        'current_execution': execution_view(store, run),
        'progress_scope': '当前流程阶段进度，非研究达标概率', 'next_action': (run.get('validation_hold') or {}).get('reason') or run.get('error') or
            ('恢复后继续：' if run['status'] == 'paused' else '当前阶段：') + labels.get(run['stage'], run['stage']),
        'agents': agents, 'mainline': mainline, 'branches': [], 'branches_loaded': False,
        'links': [{**h, 'at': h['updated_at'], 'active': h['status'] == 'running' and run['status'] == 'running'} for h in handoffs]}


def legacy_moderation_receipts(store, run_id):
    # Feed the existing moderation projection only error receipts. Do not load
    # the entire billing ledger to discover a resume-blocking legacy review.
    with store._read_db() as db:
        fields = ('id', 'run_id', 'role', 'provider', 'model', 'operation_id', 'controller_operation_id',
                  'status', 'http_status', 'error_details', 'response_request_ids', 'request_audit_id',
                  'created_at', 'completed_at')
        def projection(source):
            return 'json_object(' + ','.join(
                f"'{field}',json_extract({source},'$.{field}')" for field in fields) + ')'
        # Keep every canonical ID to suppress conflicting usage mirrors, but
        # materialize bodies only for errors and only their public fields.
        # Successful receipts can contain large archived request diagnostics.
        rows = [json.loads(row[0]) for row in db.execute(f"""WITH ledger AS MATERIALIZED (
            SELECT json_extract(item.value,'$.id') AS id,
                CASE WHEN json_extract(item.value,'$.status')='http_error'
                    THEN {projection('item.value')} END AS projection
            FROM records,json_each(records.data,'$.records') item
            WHERE collection='budgets' AND json_extract(records.data,'$.run_id')=?),
            combined AS (SELECT projection FROM ledger WHERE projection IS NOT NULL
                UNION ALL SELECT {projection('records.data')} FROM records
                WHERE collection='usage' AND json_extract(data,'$.run_id')=?
                AND json_extract(data,'$.status')='http_error'
                AND (json_extract(data,'$.id') IS NULL OR json_extract(data,'$.id') NOT IN
                    (SELECT id FROM ledger WHERE id IS NOT NULL)))
            SELECT projection FROM combined""", (run_id, run_id))]
    values = {row['id']: row for row in rows if row.get('id')}
    return {'records': list(values.values())}


def native_reasoning_recovery(store, run):
    """A display hint from the same strict predicate used by the native runner.

    Never infer non-dispatch from missing projected IDs. Inspect only the latest
    modified receipt; a mismatch suppresses the hint without searching history.
    """
    if run.get('mode') != 'live' or run.get('status') not in {'blocked', 'paused', 'failed'}:
        return None
    formats = (
        r'本机 Codex 请求尚无完整结果：([a-z][a-z0-9_]{0,80})；原回执已保存，恢复时读取原会话，不回退到 API。',
        r'本机 Codex 初始化尚未完成：([a-z][a-z0-9_]{0,80})；尚未提交模型回合，恢复时继续原请求的首次初始化。',
    )
    match = next((matched for pattern in formats if (matched := re.fullmatch(pattern, run.get('error') or ''))), None)
    if not match:
        return None
    with store._read_db() as db:
        row = db.execute("""SELECT key,
            json_extract(data,'$.checkpoint.error_code') AS error_code,
            json_extract(data,'$.checkpoint.thread_id') AS thread_id,
            json_extract(data,'$.checkpoint.turn_id') AS turn_id,
            json_extract(data,'$.selection.stage') AS stage,
            json_type(data,'$.checkpoint.auth_mode') AS auth_marker,
            json_type(data,'$.checkpoint.turn_dispatched_at') AS dispatch_marker,
            json_type(data,'$.result') AS result_type
            FROM records WHERE collection='codex_reasoning' AND json_extract(data,'$.run_id')=?
            ORDER BY updated_at DESC,key DESC LIMIT 1""", (run['id'],)).fetchone()
        if (row is None or row['stage'] != run.get('stage') or row['error_code'] != match[1]
                or row['result_type'] not in {None, 'null'}):
            return None
        operation_id = row['key']
        if match[1] == 'codex_turn_failed':
            return _native_service_failure_hint(store, run, operation_id)
        if (row['thread_id'] or row['turn_id'] or row['auth_marker'] is not None
                or row['dispatch_marker'] is not None):
            return None
    saved = _one(store, 'codex_reasoning', ['checkpoint'], key=operation_id)
    from .local_codex import LocalCodexClient
    checkpoint = (saved or {}).get('checkpoint') or {}
    if (checkpoint.get('run_id') != run['id'] or checkpoint.get('operation_id') != operation_id
            or not LocalCodexClient._unsubmitted_initialization(checkpoint, True)):
        return None
    return {'derived': True, 'run_id': run['id'], 'run_updated_at': run.get('updated_at'),
        'operation_id': operation_id, 'kind': 'unsubmitted_initialization',
        'error_code': match[1], 'resume_same_request': True}


def _native_service_failure_hint(store, run, operation_id):
    from .native_failure import retryable_failure
    fields = {name: _field('checkpoint.' + name) for name in (
        'run_id', 'operation_id', 'status', 'error_code', 'thread_id', 'turn_id', 'auth_mode',
        'reasoning_only', 'model_provider', 'server_errors', 'non_retryable_server_error',
        'public_output_truncated', 'native_tool_violation')}
    fields.update(public_count="json_array_length(data,'$.checkpoint.public_messages')",
                  activity_count="coalesce(json_array_length(data,'$.checkpoint.activities'),0)")
    cp = _one(store, 'codex_reasoning', fields, key=operation_id) or {}
    if (cp.get('run_id') != run['id'] or cp.get('operation_id') != operation_id
            or not cp.get('thread_id') or not cp.get('turn_id') or cp.get('auth_mode') != 'chatgpt'
            or cp.get('reasoning_only') != 1 or cp.get('model_provider') != 'openai'
            or cp.get('public_count') != 0 or cp.get('activity_count') != 0):
        return None
    cp.update(public_messages=[], activities=[])
    if not retryable_failure(cp):
        return None
    return {'derived': True, 'run_id': run['id'], 'run_updated_at': run.get('updated_at'),
        'operation_id': operation_id, 'kind': 'transient_service_failure',
        'error_code': cp['error_code'], 'category': cp['server_errors'][-1]['category'],
        'readback_required': True}


def summary(engine, run_id):
    from .billing_projection import summary as billing_summary
    from .implementation_progress import view as implementation_progress
    with engine.store.read_snapshot() as store:
        run = _one(store, 'runs', {**{name: _field(name) for name in
            ('id', 'title', 'objective', 'mode', 'status', 'stage', 'created_at', 'updated_at', 'config', 'error', 'progress')},
            'state': "json_object('research_cycle',coalesce(json_extract(data,'$.state.research_cycle'),1),"
                     "'experiment_attempt_policy',json_extract(data,'$.state.experiment_attempt_policy'),"
                     "'dataset_adaptation',json_extract(data,'$.state.dataset_adaptation'),"
                     "'dataset_configuration_protocol',json_extract(data,'$.state.dataset_configuration_protocol'),"
                     "'implementation_generation',json_extract(data,'$.state.implementation_generation'),"
                     "'resume_generation',json_extract(data,'$.state.resume_generation'))"}, key=run_id)
        if run is None:
            raise KeyError(run_id)
        run['state'] = json.loads(run['state']) if isinstance(run['state'], str) else run['state']
        from .research_iteration import view as iteration_view
        run['research_iteration'] = iteration_view(store,run_id)
        from .validation_hold import current as validation_hold
        run['validation_hold'] = validation_hold(store, run)
        recovery = native_reasoning_recovery(store, run)
        if recovery:
            run['native_reasoning_recovery'] = recovery
        steps = _steps(store, run_id)
        from .implementation_activity import receipt_metadata, count as activity_count
        activity = receipt_metadata(store, run_id)
        counts = {name: store.count(name, run_id) for name in ('experiments', 'artifacts', 'handoffs', 'research_cycles')}
        for name in ('artifacts', 'handoffs'):
            counts[name] = activity_count(store, name, run_id, metadata=activity)
        with store._read_db() as db:
            counts['events'] = db.execute('SELECT count(*) FROM events WHERE run_id=?', (run_id,)).fetchone()[0]
        counts['steps'] = len(steps)
        cycle = run['state']['research_cycle']
        graph = _one(store, 'graph_status', ['node', 'status', 'activity', 'waiting_for', 'error'], key=run_id)
        consensus = _one(store, 'consensus', ['round', 'cycle', 'unanimous', 'proposal_hash'], key=run_id)
        if consensus:
            consensus['unanimous'] = bool(consensus['unanimous']) if consensus['unanimous'] is not None else None
            consensus['details_loaded'] = False
        architecture = architecture_summary(store, run)
        value = {'projection': 'summary', 'revision': revision(store, run_id), 'loaded_sections': ['command'],
            'run': run, 'steps': steps, 'counts': counts, 'architecture_review': architecture,
            'innovation': _one(store, 'innovation_sessions', ['id', 'status', 'cycle'], key=f'{run_id}:{cycle}'),
            'local_codex': {'backend': run['config'].get('implementation_backend'),
                'configuration_locked': bool(activity_count(store, 'codex_workflows', run_id, metadata=activity) or
                    _one(store, 'stage_contexts', ['id'], key=run_id + ':implementation')), 'details_loaded': False},
            'consensus': consensus, 'graph_status': graph, 'handoffs': _handoffs(store, run_id, activity=activity),
            'research_map': command_summary(store, run, steps, architecture, activity=activity), 'billing': billing_summary(store, run_id),
            'implementation_progress': implementation_progress(store, run)}
        progress = value['implementation_progress']
        if run['stage'] == 'implementation' and progress:
            value['research_map']['next_action'] = run.get('error') or progress['next_action']
            value['research_map']['current_stage_label'] = progress['label']
    return value


def references(store, run):
    values = []
    for key in run['config'].get('reference_ids', []):
        value = _one(store, 'references', {**{name: _field(name) for name in ('id', 'title', 'source')},
                                          'text_length': "length(coalesce(json_extract(data,'$.text'),''))"}, key=key)
        if value:
            values.append(value)
    return values


def paper_metadata(store, run_id):
    """Identifiers and links only; this list does not assert reading or validity."""
    fields = ('id', 'title', 'doi', 'url', 'source', 'year', 'authors')
    expr = ','.join("'" + field + "',json_extract(p.value,'$." + field + "')" for field in fields)
    with store._read_db() as db:
        rows = db.execute(f"""SELECT json_object({expr}) AS paper FROM records,
            json_each(records.data,'$.output.papers') p
            WHERE collection='steps' AND json_extract(records.data,'$.run_id')=?
            AND json_extract(records.data,'$.stage') IN ('literature','related_work')""", (run_id,)).fetchall()
    values = {}
    for row in rows:
        value = json.loads(row['paper'])
        values[value.get('id') or value.get('doi') or value.get('title')] = value
    return {'papers': list(values.values()), 'details_loaded': False, 'projection': 'metadata'}


def branches(store, run_id):
    """Historical branch labels without model histories, context or literature."""
    run = _one(store, 'runs', ['stage', 'state'], key=run_id)
    cycle = (run.get('state') or {}).get('research_cycle', 1)
    fields = {name: _field(name) for name in ('review_id', 'cycle', 'round', 'proposal_hash', 'votes', 'unanimous')}
    fields.update(title=_field('proposal.title'), resource_reasons=_field('resource_assessment.reasons'),
                  missing=_field('evidence_assessment.unverified_reference_ids'))
    rounds = _rows(store, 'proposal_reviews', fields, run_id=run_id)
    # Historical reviews are nested inside old committed consensus/cycle steps.
    with store._read_db() as db:
        def nested(collection, path, extra=''):
            expressions = ','.join("'" + name + "'," +
                ("coalesce(json_extract(h.value,'$.cycle'),json_extract(records.data,'$.cycle'),1)" if name == 'cycle'
                 else expression.replace('data,', 'h.value,')) for name, expression in fields.items())
            return [json.loads(row[0]) for row in db.execute(f"""SELECT json_object({expressions})
                FROM records,json_each(records.data,?) h WHERE collection=?
                AND json_extract(records.data,'$.run_id')=? {extra}""", (path, collection, run_id))]
        rounds += nested('cycle_steps', '$.output.history', "AND json_extract(records.data,'$.stage')='debate'")
        rounds += nested('consensus', '$.history')
    latest = _one(store, 'consensus', fields, key=run_id)
    if latest:
        rounds.append(latest)
    result, seen = [], set()
    for record in rounds:
        number = record.get('cycle') or 1
        votes = record.get('votes') or []
        key = (number, record.get('round') or 1, record.get('proposal_hash'), json.dumps(votes, sort_keys=True), record.get('unanimous'))
        if key in seen:
            continue
        seen.add(key)
        passed = record.get('unanimous') in (True, 1)
        reasons = [v.get('rationale', '') for v in votes if not v.get('approve') or v.get('required_changes')]
        reasons += record.get('resource_reasons') or []
        if record.get('missing'):
            reasons.append('未核验引用：' + ', '.join(record['missing']))
        result.append({'id': record.get('review_id') or 'proposal:' + str(len(result)), 'parent_id': 'debate',
            'cycle': number, 'kind': 'proposal', 'label': record.get('title') or '研究提案',
            'status': 'passed' if passed else 'rejected', 'reason': '三位 Agent 同意本版本立项；效果仍待实验验证' if passed
                else '；'.join(map(str, reasons)) or '尚未满足同版本三票及资源门禁', 'source_id': record.get('proposal_hash')})
    for row in _rows(store, 'research_cycles', ['id', 'cycle', 'decision', 'rationale', 'revision_hash', 'review_type'], run_id=run_id):
        if row.get('review_type') == 'implementation_specification':
            result.append({'id': row['id'], 'parent_id': 'implementation', 'cycle': row['cycle'],
                'kind': 'implementation', 'label': '实现规范审查待处理', 'status': 'pending',
                'reason': row['rationale'], 'source_id': row['id']})
            continue
        result.append({'id': row['id'], 'parent_id': 'validation_review', 'cycle': row['cycle'], 'kind': 'validation',
            'label': f"第 {row['cycle']} 轮验证", 'status': 'passed' if row['decision'] == 'finalize' else 'rejected',
            'reason': row['rationale'], 'source_id': row['revision_hash']})
    if run['stage'] == 'revision':
        result.append({'id': f'active-revision:{cycle}', 'parent_id': 'validation_review', 'cycle': cycle,
            'kind': 'revision', 'label': '正在修订方案，下一步重新三方立项', 'status': 'pending',
            'reason': '根据本轮真实验证结果和异议调整，修订后还需重新投票', 'source_id': f'{run_id}:{cycle}'})
    selection = _one(store, 'selections', ['selected_trial_id'], key=run_id) or {}
    validation = _one(store, 'steps', {'decision': _field('output.decision')}, key=run_id + ':validation_review') or {}
    trials = _rows(store, 'experiments', {**{name: _field(name) for name in ('id', 'status', 'error', 'research_cycle')},
        'variant': _field('params.variant')}, run_id=run_id,
        where="json_extract(data,'$.params.phase')='validation' AND NOT coalesce(json_extract(data,'$.superseded'),0)")
    for trial in trials:
        chosen = trial['id'] == selection.get('selected_trial_id')
        state = 'passed' if chosen else 'rejected' if trial['status'] == 'failed' else 'pending'
        reason = ('验证集选中的参数；验证未达标，按预定规则停止，未执行独立测试' if validation.get('decision') == 'stop'
                  else '验证集选中的参数，等待独立最终测试') if chosen else ('执行失败：' + str(trial.get('error') or '')
                  if state == 'rejected' else '已测量，保留为基线/消融/候选证据；不等于方案被否定'
                  if trial['status'] == 'completed' else '实验待完成')
        result.append({'id': 'trial:' + trial['id'], 'parent_id': 'tuning', 'cycle': trial.get('research_cycle') or 1,
            'kind': 'experiment', 'label': f"{trial.get('variant') or '实验'} · {trial['id']}", 'status': state,
            'reason': reason, 'source_id': trial['id']})
    return result


def section(engine, run_id, name, *, intelligence, local_codex):
    if name not in SECTIONS:
        raise KeyError(name)
    if name == 'experiments':
        from .experiment_views import view
        return view(engine, run_id)
    store = engine.store
    value = {'projection': 'view', 'section': name, 'revision': revision(store, run_id), 'loaded_sections': [name]}
    if name == 'command':
        data = summary(engine, run_id)
        value['research_map'] = data['research_map']
    elif name == 'branches':
        value['research_map'] = {'branches': branches(store, run_id), 'branches_loaded': True}
    elif name == 'orchestration':
        value['orchestration'] = engine.orchestration(run_id)
        value['research_cycles'] = sorted(store.list('research_cycles', run_id), key=lambda row: row['cycle'])
    elif name == 'timeline':
        value['events'] = store.recent_events(run_id)
    elif name == 'handoffs':
        from .observability import handoff_views
        value['handoffs'] = handoff_views(store, run_id)
    elif name == 'innovation':
        from .innovation import view
        value['innovation'] = view(engine, run_id)
        value['literature'] = paper_metadata(store, run_id)
        value['references'] = references(store, store.get('runs', run_id))
    elif name == 'consensus':
        from .observability import current_debate
        from .review_context_status import context_reference_status
        value.update(consensus=store.get('consensus', run_id), current_debate=current_debate(engine, run_id),
                     context_reference_check=context_reference_status(store, run_id))
        value['literature'] = paper_metadata(store, run_id)
        value['references'] = references(store, store.get('runs', run_id))
    elif name == 'evidence':
        value.update(literature=engine.literature_view(run_id), references=references(store, store.get('runs', run_id)))
    elif name == 'implementation':
        value['local_codex'] = local_codex()
        from .implementation_progress import view as implementation_progress
        value['implementation_progress'] = implementation_progress(store, store.get('runs', run_id))
    elif name == 'artifacts':
        from .implementation_activity import records as activity_records
        value['artifacts'] = activity_records(store, 'artifacts', run_id)
    elif name == 'context':
        value['context'] = intelligence().context_stats(run_id)
    return value


def stage(engine, run_id, name, *, intelligence, local_codex):
    if name not in STAGES:
        raise KeyError(name)
    if name=='architecture_tuning':
        from .optimization_loop import view,current
        run=engine.store.get('runs',run_id)
        value={'projection':'stage','stage':name,'optimization_loop':view(engine.store,run),
               'architecture_tuning':current(engine.store,run_id,engine.cycle(run_id))}
        from .architecture_adjustment_views import view as adjustments
        value['architecture_adjustments']=adjustments(engine.store,run)
        step=engine.store.get('steps',run_id+':'+name)
        value['steps']=[{**step,'output_loaded':True}] if step else []
        return value
    if name in {'experiments', 'tuning', 'final_tests'}:
        from .experiment_views import view, _bounded
        value = view(engine, run_id)
        value.update(projection='stage', stage=name)
        fields = {k: _field(k) for k in ('id', 'run_id', 'stage', 'status', 'started_at', 'completed_at', 'error')}
        fields['output'] = _bounded('output', 131072)
        fields['output_omitted'] = "coalesce(length(json_extract(data,'$.output'))>131072,0)"
        rows = _rows(engine.store, 'steps', fields, key=run_id + ':' + name)
        value['steps'] = [dict(row, output_loaded=not row['output_omitted']) for row in rows]
        from .dataset_experiments import view as dataset_configuration
        value['dataset_configuration'] = dataset_configuration(engine, run_id)
        return value
    value = {'projection': 'stage', 'stage': name, 'revision': revision(engine.store, run_id)}
    if name == 'design':
        from .architecture_documents import view
        value['architecture_review'] = view(engine, run_id)
    selected = {'literature': 'evidence', 'related_work': 'evidence', 'innovation': 'innovation',
                'debate': 'consensus', 'implementation': 'implementation', 'experiments': 'experiments',
                'tuning': 'experiments', 'final_tests': 'experiments'}.get(name)
    if selected:
        part = section(engine, run_id, selected, intelligence=intelligence, local_codex=local_codex)
        value.update({key: item for key, item in part.items() if key not in {'projection', 'section', 'revision'}})
    if name in {'design', 'implementation', 'innovation'}:
        value['steps'] = [row for row in _steps(engine.store, run_id) if row['stage'] == name]
    else:
        step = engine.store.get('steps', run_id + ':' + name)
        value['steps'] = [{**step, 'output_loaded': True}] if step else []
    return value
