"""Bounded public task operands for cards, without loading paid prompts."""
import json

from .storage import redact


def view(store, run_id, role, handoff_id):
    # Read only the actual role's issue operands. Large model prompts, tool
    # histories, source documents and other roles' conclusions stay in SQLite.
    with store._read_db() as db:
        row = db.execute("""SELECT
            (SELECT json_group_array(json(value)) FROM json_each(i.data,'$.context.code_repair_scope.issues')
                WHERE json_extract(value,'$.role')=:role),
            json_extract(p.data,'$.code_review_inheritance.parent.active_issue_ids'),
            json_extract(i.data,'$.context.definition_repair.issues'),
            json_type(i.data,'$.context.host_constraint_reconciliation')='object',
            json_type(p.data,'$.code_review_inheritance.parent')='object',
            json_extract(i.data,'$.context.implementation_evidence_task')
            FROM records i LEFT JOIN records p ON p.collection='handoff_progress' AND p.key=i.key
            WHERE i.collection='handoff_inputs' AND i.key=:hid AND json_extract(i.data,'$.run_id')=:rid
            AND EXISTS(SELECT 1 FROM records h WHERE h.collection='handoffs' AND h.key=i.key
                AND json_extract(h.data,'$.run_id')=:rid AND json_extract(h.data,'$.to_role')=:role)""",
            {'hid': handoff_id, 'rid': run_id, 'role': role}).fetchone()
    if not row:
        return None
    work = json.loads(row[5]) if row[5] else None
    code = json.loads(row[0]) if row[0] else []
    active = set(json.loads(row[1])) if row[1] is not None else None
    definitions = json.loads(row[2]) if row[2] else []
    owned = [i for i in code if i.get('role') == role]
    selected = work['issues'] if work else [i for i in owned if active is None or i.get('issue_id') in active] if code else definitions
    if not code and not definitions and not work:
        return None
    clipped = False
    def text(value, maximum):
        nonlocal clipped
        value = str(value or '')
        clipped |= len(value) > maximum
        return redact(value[:maximum])
    items = []
    for issue in selected[:20]:
        fixes = issue.get('fixes', [])
        items.append({'id': issue.get('issue_id'), 'requirement_id': issue.get('requirement_id'),
            'problem': text(issue.get('issue'), 1800),
            'fixes': [{'location': text(f.get('location'), 1000), 'change': text(f.get('change'), 1800),
                       'verification': text(f.get('verification'), 1800)} for f in fixes[:4]]})
        clipped |= len(fixes) > 4
    return {'source': 'current_frozen_evidence_task' if work else 'current_frozen_assignment', 'handoff_id': handoff_id,
        'title': work['title'] if work else '工程定义补充' if definitions and not code else '定向补查' if active is not None else '本轮待核对问题',
        'issue_count': len(selected), 'items': items,
        'inherits_previous': bool(row[4]) or bool(work), 'inherited_issue_count': len(owned) - len(selected) if code and not work else 0,
        'host_constraints_handled': bool(row[3]), 'truncated': clipped or len(selected) > 20}
