"""Bounded display of current-version author declarations, never approval."""
import json
import sqlite3


def _checkpoints(db, run_id, hid, meta):
    """Display saved author checkpoints, never infer completion from read counts."""
    from .implementation_self_check import _seal, KEY, COLLECTION
    def get(collection):
        row = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection,hid)).fetchone()
        value = json.loads(row[0]) if row else {}
        return value if value.get('run_id') == run_id else {}
    inputs, progress, record = get('handoff_inputs'), get('handoff_progress'), _seal(get(COLLECTION))
    expected = {(i['issue_id'],n):req for i in inputs.get('context',{}).get('acceptance_issues',[])
                for n,req in enumerate(i['close_when'])}
    from .implementation_condition_roles import review_pending
    review = review_pending(inputs.get('context', {}))
    review_keys = {(r['issue_id'], r['index']) for r in review}
    expected = {k:v for k,v in expected.items() if k not in review_keys}
    summary = progress.get('normal_request',{}).get('prepared',{}).get('context',{}).get(KEY,{})
    entries = {}
    if summary.get('document_id') == meta.get('draft_document_id') and summary.get('spec_hash') == meta.get('draft_spec_hash'):
        entries = {(c['issue_id'],c['index']):c for c in summary.get('valid',[])}
    for batch in record.get('batches',[]):
        for entry in batch['entries']:
            if (entry['document']['document_id']==meta.get('draft_document_id')
                    and entry['document']['sha256']==meta.get('draft_sha256')):
                c=entry['check'];entries[(c['issue_id'],c['index'])]=c
    entries = {k:c for k,c in entries.items() if k in expected}
    if not entries: return None
    counts = {s:sum(c['status']==s for c in entries.values()) for s in ('complete','incomplete','blocked')}
    missing=len(expected)-len(entries)
    pending=[{'issue_id':key[0],'condition_index':key[1],'status':entries.get(key,{}).get('status','unrecorded'),
              'requirement':req,'reason':entries.get(key,{}).get('explanation','尚未登记作者自检')}
             for key,req in expected.items() if entries.get(key,{}).get('status')!='complete']
    return {**counts,'unrecorded':missing,'state':'checkpointing' if missing or counts['incomplete'] else 'checkpointed',
            'pending':pending[:64], 'review_pending':review, 'verified':False}


def self_check(db, run_id, hid, meta):
    result = {'state': 'not_reported', 'complete': None, 'incomplete': None,
              'blocked': None, 'pending': [], 'review_pending': [], 'verified': False}
    try:
        result = _checkpoints(db,run_id,hid,meta) or result
        row = db.execute("""WITH scope AS (
            SELECT i.data input,p.data progress FROM records i
            LEFT JOIN records p ON p.collection='handoff_progress' AND p.key=i.key
                AND json_extract(p.data,'$.run_id')=? AND json_extract(p.data,'$.id')=i.key
            WHERE i.collection='handoff_inputs' AND i.key=? AND json_extract(i.data,'$.run_id')=?),
        candidates AS (
            SELECT json_extract(progress,'$.normal_request.response.json') candidate,
                json_extract(progress,'$.normal_request.turn') turn,1 priority,0 position
            FROM scope WHERE json_extract(progress,'$.normal_request.status')='response_saved'
            UNION ALL
            SELECT json_extract(progress,'$.finalization.response.json'),
                coalesce(json_extract(progress,'$.finalization.turn'),json_extract(progress,'$.turn')),2,0
            FROM scope WHERE json_extract(progress,'$.mode')='final_only'
                AND json_extract(progress,'$.finalization.status') IN ('response_saved','completed')
            UNION ALL
            SELECT json_extract(h.value,'$.candidate'),json_extract(h.value,'$.source_turn'),0,CAST(h.key AS INTEGER)
            FROM scope,json_each(progress,'$.alignment_candidate_history') h),
        latest AS (SELECT * FROM candidates WHERE json_type(candidate,'$.acceptance_completion')='array'
            ORDER BY turn DESC,priority DESC,position DESC LIMIT 1)
        SELECT json_object(
            'matches',(SELECT json_extract(candidate,'$.document_id')=?
                AND json_extract(candidate,'$.document_sha256')=?
                AND json_extract(candidate,'$.spec_hash')=? FROM latest),
            'required_count',(SELECT sum(json_array_length(j.value,'$.close_when'))
                FROM json_each(input,'$.context.acceptance_issues') j),
            'requirements',(SELECT json_group_array(json_object(
                'issue_id',json_extract(j.value,'$.issue_id'),'index',CAST(c.key AS INTEGER),
                'requirement',substr(c.value,1,400))) FROM json_each(input,'$.context.acceptance_issues') j,
                json_each(j.value,'$.close_when') c WHERE CAST(j.key AS INTEGER)<128 AND CAST(c.key AS INTEGER)<128),
            'claim_count',(SELECT count(*) FROM latest,json_each(candidate,'$.acceptance_completion') a,
                json_each(a.value,'$.conditions') c),
            'claims',(SELECT json_group_array(json_object('issue_id',json_extract(a.value,'$.issue_id'),
                'index',json_extract(c.value,'$.index'),'status',json_extract(c.value,'$.status'),
                'index_type',json_type(c.value,'$.index'),
                'explanation',substr(json_extract(c.value,'$.explanation'),1,600)))
                FROM latest,json_each(candidate,'$.acceptance_completion') a,json_each(a.value,'$.conditions') c
                WHERE CAST(a.key AS INTEGER)<128 AND CAST(c.key AS INTEGER)<128)) FROM scope""",
            (run_id, hid, run_id, meta.get('draft_document_id'), meta.get('draft_sha256'),
             meta.get('draft_spec_hash'))).fetchone()
        value = json.loads(row[0]) if row else {}
        requirements, claims = value.get('requirements', []), value.get('claims', [])
        expected = {(r['issue_id'], r['index']): r['requirement'] for r in requirements}
        keys = [(c['issue_id'], c['index']) for c in claims]
        if (value.get('matches') != 1 or not expected or len(expected) != value.get('required_count')
                or len(claims) != value.get('claim_count') or len(keys) != len(set(keys))
                or set(keys) != set(expected) or any(c['index_type'] != 'integer'
                    or c['status'] not in {'complete', 'incomplete', 'blocked'} for c in claims)):
            return result
        from .implementation_condition_roles import review_pending
        frozen = db.execute("SELECT data FROM records WHERE collection='handoff_inputs' AND key=?", (hid,)).fetchone()
        context = json.loads(frozen[0]).get('context', {}) if frozen else {}
        review = review_pending(context)
        review_keys = {(r['issue_id'], r['index']) for r in review}
        author_claims = [c for c in claims if (c['issue_id'], c['index']) not in review_keys]
        counts = {status: sum(c['status'] == status for c in author_claims)
                  for status in ('complete', 'incomplete', 'blocked')}
        pending = [{'issue_id': c['issue_id'], 'condition_index': c['index'], 'status': c['status'],
                    'requirement': expected[(c['issue_id'], c['index'])], 'reason': c['explanation']}
                   for c in author_claims if c['status'] != 'complete']
        return {**result, **counts, 'state': 'needs_revision' if pending else 'reported',
                'pending': pending[:64], 'review_pending': review}
    except (sqlite3.DatabaseError, TypeError, ValueError, KeyError):
        return result  # Invalid or old-version declarations are not progress.


def latest_notes(db, run_id, hid):
    """Show explicit author notes without classifying free prose as a verdict."""
    row = db.execute("""WITH latest AS (
        SELECT t.value receipt FROM records p,json_each(p.data,'$.tool_results') t
        WHERE p.collection='handoff_progress' AND p.key=? AND json_extract(p.data,'$.run_id')=?
            AND json_extract(t.value,'$.call.name')='implementation.edit_document'
        ORDER BY CAST(t.key AS INTEGER) DESC LIMIT 1)
        SELECT substr(json_extract(c.value,'$.reason'),1,600) reason
        FROM latest,json_each(receipt,'$.call.arguments.revision_plan') plan,
            json_each(plan.value,'$.conditions') c LIMIT 5""", (hid, run_id)).fetchall()
    return list(dict.fromkeys(r['reason'] for r in row if isinstance(r['reason'], str) and r['reason']))
