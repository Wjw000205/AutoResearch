"""Bounded current-assignment activity for cards; never an execution gate.

Only the selected handoff's headers, fixed directory metadata and authenticated
range receipts enter this projection. Model prompts, document/tool bodies and
role histories are not loaded into Python. Reading is never writing progress.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import psutil


TOOL_LABELS = {
    'documents.read': '读取文档原文', 'documents.list': '读取文档目录',
    'documents.search': '定位文档内容', 'implementation.write_document': '校验并保存实现文档',
    'implementation.edit_document': '修改并保存实现文档',
    'implementation.locate_document': '定位实现文档修改位置与原文',
    'sources.read': '读取来源', 'sources.list': '查看来源目录',
    'literature.record_notes': '保存阅读笔记', 'pdf.read': '读取论文正文',
    'web.search': '检索资料', 'evidence.read': '读取研究证据',
}
_IMPLEMENTATION = {'implementation_spec', 'implementation_document_review', 'spec_review', 'code_review',
                   'code_repair_map', 'code_repair_audit', 'code_repair_amend'}


def generation_condition():
    """Exact workflow ownership for author/reviewer assignments after a reset."""
    return """(substr(json_extract(records.data,'$.purpose'),1,
        instr(json_extract(records.data,'$.purpose')||':',':')-1)
        NOT IN ('implementation_spec','implementation_document_review') OR EXISTS(
        SELECT 1 FROM records i JOIN records w ON w.collection='codex_workflows'
            AND w.key=coalesce(json_extract(i.data,'$.context.implementation_document_authoring.workflow_id'),
                json_extract(i.data,'$.context.implementation_indexed_review_contract.binding.workflow_id'))
        WHERE i.collection='handoff_inputs' AND i.key=records.key
            AND json_extract(i.data,'$.run_id')=json_extract(records.data,'$.run_id')
            AND json_extract(w.data,'$.run_id')=json_extract(records.data,'$.run_id')
            AND json_extract(w.data,'$.implementation_generation')=?))"""


def _meta(db, run_id, hid):
    row = db.execute("""SELECT json_object(
        'updated_at',max(i.updated_at,coalesce(p.updated_at,i.updated_at),coalesce(d.updated_at,i.updated_at)),
        'revision',coalesce(json_extract(i.data,'$.context.implementation_document_authoring.revision'),
            json_extract(i.data,'$.context.implementation_indexed_review_contract.binding.revision')),
        'workflow_id',coalesce(json_extract(i.data,'$.context.implementation_document_authoring.workflow_id'),
            json_extract(i.data,'$.context.implementation_indexed_review_contract.binding.workflow_id')),
        'generation',json_extract(w.data,'$.implementation_generation'),
        'workflow_run_id',json_extract(w.data,'$.run_id'),
        'request_status',CASE WHEN json_extract(p.data,'$.mode')='final_only'
            THEN json_extract(p.data,'$.finalization.status') ELSE json_extract(p.data,'$.normal_request.status') END,
        'request_final_only',json_extract(p.data,'$.mode')='final_only',
        'review_closeout_phase',CASE WHEN json_extract(p.data,'$.mode')='final_only' THEN
            CASE WHEN json_type(p.data,'$.finalization.prepared.context._indexed_review_closeout.version')='integer'
                    AND json_extract(p.data,'$.finalization.prepared.context._indexed_review_closeout.version')=1
                THEN substr(json_extract(p.data,'$.finalization.prepared.context._indexed_review_closeout.phase'),1,32) END
            ELSE CASE WHEN json_type(p.data,'$.normal_request.prepared.context._indexed_review_closeout.version')='integer'
                    AND json_extract(p.data,'$.normal_request.prepared.context._indexed_review_closeout.version')=1
                THEN substr(json_extract(p.data,'$.normal_request.prepared.context._indexed_review_closeout.phase'),1,32) END END,
        'request_at',CASE WHEN json_extract(p.data,'$.mode')='final_only'
            THEN json_extract(p.data,'$.finalization.dispatched_at') ELSE json_extract(p.data,'$.normal_request.dispatched_at') END,
        'request_error',substr(CASE WHEN json_extract(p.data,'$.mode')='final_only'
            THEN json_extract(p.data,'$.finalization.error') ELSE json_extract(p.data,'$.normal_request.error') END,1,500),
        'tool_turn',json_extract(p.data,'$.turn'),
        'save_only',json_extract(i.data,'$.context.implementation_document_completion_protocol')=1,
        'pending_tool',json_extract(p.data,'$.pending_tool_calls['||coalesce(json_extract(p.data,'$.tool_cursor'),0)||'].name'),
        'draft_document_id',json_extract(d.data,'$.row.document.document_id'),
        'draft_sha256',json_extract(d.data,'$.row.document.sha256'),
        'draft_spec_hash',json_extract(d.data,'$.row.spec_hash'),
        'revision_contract',json_type(i.data,'$.context.implementation_author_revision_contract.version')='integer'
            AND json_extract(i.data,'$.context.implementation_author_revision_contract.version')=1,
        'revision_required_items',CASE WHEN json_type(i.data,'$.context.implementation_author_revision_contract.items')='array'
            THEN json_array_length(i.data,'$.context.implementation_author_revision_contract.items') END,
        'draft_at',d.updated_at,
        'saved_draft',EXISTS(SELECT 1 FROM json_each(p.data,'$.tool_results') t
            WHERE json_extract(t.value,'$.call.name') IN ('implementation.write_document','implementation.edit_document')
                AND json_extract(t.value,'$.predecessor_handoff_id') IS NULL
                AND json_extract(t.value,'$.result.saved')=1
                AND json_extract(t.value,'$.result.submission.document_id')=json_extract(d.data,'$.row.document.document_id')
                AND json_extract(t.value,'$.result.submission.document_sha256')=json_extract(d.data,'$.row.document.sha256')))
        FROM records i LEFT JOIN records p ON p.collection='handoff_progress' AND p.key=i.key
            AND json_extract(p.data,'$.run_id')=? AND json_extract(p.data,'$.id')=i.key
        LEFT JOIN records w ON w.collection='codex_workflows'
            AND w.key=coalesce(json_extract(i.data,'$.context.implementation_document_authoring.workflow_id'),
                json_extract(i.data,'$.context.implementation_indexed_review_contract.binding.workflow_id'))
        LEFT JOIN records d ON d.collection='implementation_document_drafts' AND d.key=i.key
            AND json_extract(d.data,'$.run_id')=?
            AND json_extract(d.data,'$.workflow_id')=w.key
            AND json_extract(d.data,'$.revision')=json_extract(i.data,'$.context.implementation_document_authoring.revision')
        WHERE i.collection='handoff_inputs' AND i.key=? AND json_extract(i.data,'$.run_id')=?""",
        (run_id, run_id, hid, run_id)).fetchone()
    value = json.loads(row[0]) if row else {}
    if not value.get('draft_document_id'):
        # A continuation can check the certified inherited draft before saving
        # its own copy. Display that identity without claiming a new save/read.
        inherited = db.execute("""SELECT json_object(
            'draft_document_id',json_extract(s.data,'$.row.document.document_id'),
            'draft_sha256',json_extract(s.data,'$.row.document.sha256'),
            'draft_spec_hash',json_extract(s.data,'$.row.spec_hash'))
            FROM records i JOIN records a ON a.collection='stage_contexts'
                AND a.key=json_extract(i.data,'$.context.implementation_acceptance_author_continuation.audit_id')
            JOIN records s ON s.collection='implementation_document_drafts'
                AND s.key=json_extract(i.data,'$.context.implementation_acceptance_author_continuation.source_handoff_id')
            WHERE i.collection='handoff_inputs' AND i.key=? AND json_extract(i.data,'$.run_id')=?
                AND json_extract(a.data,'$.run_id')=? AND json_extract(a.data,'$.successor_id')=i.key
                AND json_extract(a.data,'$.sha256')=json_extract(i.data,'$.context.implementation_acceptance_author_continuation.sha256')
                AND json_extract(s.data,'$.run_id')=?
                AND json_extract(s.data,'$.workflow_id')=json_extract(i.data,'$.context.implementation_document_authoring.workflow_id')
                AND json_extract(s.data,'$.revision')=json_extract(i.data,'$.context.implementation_document_authoring.revision')
                AND json_extract(s.data,'$.row.spec_hash')=json_extract(i.data,'$.context.implementation_acceptance_author_continuation.spec_hash')
                AND json_extract(s.data,'$.row.document.document_id')=json_extract(i.data,'$.context.implementation_acceptance_author_continuation.document.document_id')
                AND json_extract(s.data,'$.row.document.sha256')=json_extract(i.data,'$.context.implementation_acceptance_author_continuation.document.sha256')
                AND json_extract(s.data,'$.row.document.document_id')=json_extract(a.data,'$.payload.source.implementation_document_drafts.row.document.document_id')
                AND json_extract(s.data,'$.row.document.sha256')=json_extract(a.data,'$.payload.source.implementation_document_drafts.row.document.sha256')
                AND json_extract(s.data,'$.row.spec_hash')=json_extract(a.data,'$.payload.source.implementation_document_drafts.row.spec_hash')
            """, (hid,run_id,run_id,run_id)).fetchone()
        if inherited:
            value.update(json.loads(inherited[0]))
    return value


def _batch_revision(db, run_id, hid):
    """Only bounded counts from real persisted saves/rejections; no document body."""
    row = db.execute("""WITH scope AS (
        SELECT i.data input, coalesce((SELECT coalesce(json_extract(t.value,'$.result.revision_progress'),
            json_extract(t.value,'$.result.validation.revision_progress'))
            FROM json_each(p.data,'$.tool_results') t
            WHERE json_extract(t.value,'$.call.name')='implementation.edit_document'
                AND (json_type(t.value,'$.result.revision_progress')='object'
                    OR json_type(t.value,'$.result.validation.revision_progress')='object')
            ORDER BY CAST(t.key AS INTEGER) DESC LIMIT 1),
            json_extract(d.data,'$.revision_progress'),
            json_extract(i.data,'$.context.acceptance_author_saved_progress')) progress
        FROM records i LEFT JOIN records p ON p.collection='handoff_progress' AND p.key=i.key
        LEFT JOIN records d ON d.collection='implementation_document_drafts' AND d.key=i.key
        WHERE i.collection='handoff_inputs' AND i.key=? AND json_extract(i.data,'$.run_id')=?
            AND json_extract(i.data,'$.context.implementation_acceptance_author_protocol')=1)
        SELECT json_object('total',coalesce((SELECT sum(json_array_length(j.value,'$.close_when'))
            FROM json_each(input,'$.context.acceptance_issues') j),0),
            'handled',coalesce(json_array_length(progress,'$.handled_conditions'),0),
            'failed_units',coalesce(json_array_length(progress,'$.failed_units'),0)) FROM scope""", (hid, run_id)).fetchone()
    return json.loads(row[0]) if row else None


def _author_revision(db, run_id, hid, meta):
    """Bounded latest self-check metadata, never a claim that revision passed.

    A saved draft can contain only one changed field. Extract identities and
    declared outcomes in SQL; neither author prose nor full requests enter this
    display, and only a candidate bound to the current draft can supply counts.
    """
    if not meta.get('revision_contract'):
        return None
    try:
        row = db.execute("""WITH scope AS (
        SELECT i.data AS input,p.data AS progress
        FROM records i LEFT JOIN records p ON p.collection='handoff_progress' AND p.key=i.key
            AND json_extract(p.data,'$.run_id')=? AND json_extract(p.data,'$.id')=i.key
        WHERE i.collection='handoff_inputs' AND i.key=? AND json_extract(i.data,'$.run_id')=?),
        candidates AS (
            SELECT json_extract(progress,'$.finalization.response.json') AS candidate,
                coalesce(json_extract(progress,'$.finalization.turn'),json_extract(progress,'$.turn')) AS turn,
                3 AS priority,0 AS position,'finalization' AS source
            FROM scope WHERE json_extract(progress,'$.mode')='final_only'
                AND json_extract(progress,'$.finalization.status')='response_saved'
                AND json_type(progress,'$.finalization.response.json.document_id')='text'
            UNION ALL
            SELECT json_extract(progress,'$.normal_request.response.json'),
                json_extract(progress,'$.normal_request.turn'),2,0,'normal_request'
            FROM scope WHERE json_extract(progress,'$.normal_request.status')='response_saved'
                AND json_type(progress,'$.normal_request.response.json.document_id')='text'
            UNION ALL
            SELECT json_extract(h.value,'$.candidate'),json_extract(h.value,'$.source_turn'),
                1,CAST(h.key AS INTEGER),'alignment_candidate_history'
            FROM scope,json_each(scope.progress,'$.alignment_candidate_history') h
            WHERE json_type(h.value,'$.candidate.document_id')='text'),
        latest AS (SELECT * FROM candidates ORDER BY turn DESC,priority DESC,position DESC LIMIT 1)
        SELECT json_object(
            'required_count',json_array_length(input,'$.context.implementation_author_revision_contract.items'),
            'requirements',(SELECT json_group_array(json_object(
                'fix_id',substr(json_extract(item.value,'$.fix_id'),1,128),
                'invalid',coalesce(json_type(item.value,'$.fix_id'),'')!='text'
                    OR length(json_extract(item.value,'$.fix_id'))>128
                    OR coalesce(json_type(item.value,'$.fields'),'')!='array'
                    OR json_array_length(item.value,'$.fields')>64
                    OR EXISTS(SELECT 1 FROM json_each(item.value,'$.fields') field
                        WHERE field.type!='text' OR length(field.value)>120),
                'fields',(SELECT json_group_array(substr(field.value,1,120)) FROM json_each(item.value,'$.fields') field
                    WHERE CAST(field.key AS INTEGER)<64)))
                FROM json_each(input,'$.context.implementation_author_revision_contract.items') item
                WHERE CAST(item.key AS INTEGER)<96),
            'candidate_source',(SELECT source FROM latest),
            'candidate_matches_draft',(SELECT json_extract(candidate,'$.document_id')=?
                AND json_extract(candidate,'$.document_sha256')=? AND json_extract(candidate,'$.spec_hash')=? FROM latest),
            'claims_count',(SELECT json_array_length(candidate,'$.revision_completion') FROM latest),
            'claims',(SELECT json_group_array(json_object(
                'fix_id',substr(json_extract(claim.value,'$.fix_id'),1,128),
                'outcome',substr(json_extract(claim.value,'$.outcome'),1,32),
                'invalid',coalesce(json_type(claim.value,'$.fix_id'),'')!='text'
                    OR length(json_extract(claim.value,'$.fix_id'))>128
                    OR coalesce(json_type(claim.value,'$.outcome'),'')!='text'
                    OR coalesce(json_type(claim.value,'$.field_checks'),'')!='array'
                    OR json_array_length(claim.value,'$.field_checks')>64
                    OR EXISTS(SELECT 1 FROM json_each(claim.value,'$.field_checks') check_row
                        WHERE coalesce(json_type(check_row.value,'$.field'),'')!='text'
                            OR length(json_extract(check_row.value,'$.field'))>120
                            OR coalesce(json_type(check_row.value,'$.outcome'),'')!='text'),
                'field_checks',(SELECT json_group_array(json_object(
                    'field',substr(json_extract(check_row.value,'$.field'),1,120),
                    'outcome',substr(json_extract(check_row.value,'$.outcome'),1,32)))
                    FROM json_each(claim.value,'$.field_checks') check_row WHERE CAST(check_row.key AS INTEGER)<64)))
                FROM latest,json_each(latest.candidate,'$.revision_completion') claim
                WHERE CAST(claim.key AS INTEGER)<96)) FROM scope""",
            (run_id, hid, run_id, meta.get('draft_document_id'), meta.get('draft_sha256'), meta.get('draft_spec_hash'))).fetchone()
    except sqlite3.DatabaseError:
        # Malformed candidate structures must not prevent the card from loading.
        row = None
    value = json.loads(row[0]) if row else {}
    required, requirements, claims = (value.get('required_count', meta.get('revision_required_items')),
        value.get('requirements', []), value.get('claims', []))
    targets = sorted({field for item in requirements for field in item.get('fields', []) if isinstance(field, str)})[:64]
    result = {'state': 'unknown', 'required_items': required, 'target_fields': targets,
        'draft_saved': bool(meta.get('saved_draft')), 'candidate_source': value.get('candidate_source'),
        'candidate_matches_draft': value.get('candidate_matches_draft') == 1,
        'declared_revised': None, 'declared_rebutted': None, 'declared_incomplete': None,
        'source': 'latest_actual_author_candidate_self_check', 'verified': False,
        'scientific_approval': False, 'execution_authorization': False}
    if (type(required) is not int or not 0 < required <= 96 or len(requirements) != required
            or not result['candidate_matches_draft'] or value.get('claims_count') != required or len(claims) != required
            or any(item.get('invalid') for item in requirements + claims)):
        return result
    expected = {item['fix_id']: item.get('fields', []) for item in requirements if isinstance(item.get('fix_id'), str)}
    ids = [claim.get('fix_id') for claim in claims]
    if len(expected) != required or len(set(ids)) != required or set(ids) != set(expected):
        return result
    counts = {'revised': 0, 'rebutted': 0, 'incomplete': 0}
    for claim in claims:
        checks, fields = claim.get('field_checks', []), expected[claim['fix_id']]
        actual_fields = [check.get('field') for check in checks]
        outcome = claim.get('outcome')
        if (outcome not in counts or not fields or len(checks) != len(fields)
                or len(set(actual_fields)) != len(fields) or set(actual_fields) != set(fields)
                or any(check.get('outcome') not in {'changed', 'already_satisfied', 'incomplete'} for check in checks)):
            return result
        # Contradictory self-checks cannot be promoted to completion.
        if outcome != 'incomplete' and any(check.get('outcome') == 'incomplete' for check in checks):
            return result
        counts[outcome] += 1
    return {**result, 'state': 'incomplete' if counts['incomplete'] else 'waiting_validation',
        'declared_revised': counts['revised'], 'declared_rebutted': counts['rebutted'],
        'declared_incomplete': counts['incomplete']}


def _directories(db, run_id, hid):
    # Keep navigation for the current prepared request, but distinguish its
    # fixed task sources from optional dependencies, drafts and note versions.
    # A phase's sources array also grows when an author expands dependencies;
    # it is not a mandatory reading list or a stable percentage denominator.
    # Freeze the selected context once, then carry only navigation operands
    # through each document row. Otherwise SQLite repeatedly parses the full
    # prepared request (including tool history) for every directory entry.
    rows = db.execute("""WITH original_scope AS MATERIALIZED (
        SELECT coalesce(
            CASE WHEN json_extract(p.data,'$.mode')='final_only'
                THEN json_extract(p.data,'$.finalization.prepared.context') END,
            json_extract(p.data,'$.normal_request.prepared.context'),
            json_extract(i.data,'$.context')) AS context
        FROM records i LEFT JOIN records p ON p.collection='handoff_progress' AND p.key=i.key
            AND json_extract(p.data,'$.run_id')=? AND json_extract(p.data,'$.id')=i.key
        WHERE i.collection='handoff_inputs' AND i.key=? AND json_extract(i.data,'$.run_id')=?),
        scope AS MATERIALIZED (SELECT json_object(
            'indexed_documents',CASE WHEN json_type(context,'$.indexed_documents') IS NULL
                THEN json('[]') ELSE json_extract(context,'$.indexed_documents') END,
            'implementation_on_demand_documents',CASE WHEN json_type(context,'$.implementation_on_demand_documents') IS NULL
                THEN json('[]') ELSE json_extract(context,'$.implementation_on_demand_documents') END,
            'implementation_indexed_review_contract',json_object(
                'required_documents',json_extract(context,'$.implementation_indexed_review_contract.required_documents')),
            'implementation_phase_context_protocol',json_extract(context,'$.implementation_phase_context_protocol'),
            'implementation_document_authoring',json_extract(context,'$.implementation_document_authoring'),
            'architecture_document',json_extract(context,'$.architecture_document'),
            'approved_proposal_document',json_extract(context,'$.approved_proposal_document'),
            'frozen_experiment_plan_document',json_extract(context,'$.frozen_experiment_plan_document'),
            'approved_definition_document',json_extract(context,'$.approved_definition_document'),
            'protocol_document',json_extract(context,'$.protocol_document'),
            'implementation_capability_document',json_extract(context,'$.implementation_capability_document'),
            'previous_specifications',json_extract(context,'$.previous_specifications')) AS context FROM original_scope),
        documents AS (
            SELECT doc.value,scope.context FROM scope,json_each(scope.context,'$.indexed_documents') doc
            UNION ALL
            SELECT doc.value,scope.context FROM scope,json_each(scope.context,'$.implementation_on_demand_documents') doc)
        SELECT json_object('document_id',json_extract(doc.value,'$.document_id'),
        'sha256',json_extract(doc.value,'$.sha256'),'title',substr(json_extract(doc.value,'$.title'),1,200),
        'kind',json_extract(doc.value,'$.kind'),'total_chars',json_extract(doc.value,'$.total_chars'),
        'progress_required',CASE
            WHEN json_extract(doc.value,'$.kind')='document_working_notes' THEN 0
            WHEN json_type(doc.context,'$.implementation_indexed_review_contract.required_documents')='array'
                THEN EXISTS(SELECT 1 FROM json_each(doc.context,
                    '$.implementation_indexed_review_contract.required_documents') required
                    WHERE json_extract(required.value,'$.document_id')=json_extract(doc.value,'$.document_id')
                        AND json_extract(required.value,'$.sha256')=json_extract(doc.value,'$.sha256'))
            WHEN json_type(doc.context,'$.implementation_phase_context_protocol')='integer'
                    AND json_extract(doc.context,'$.implementation_phase_context_protocol')=1
                    AND json_type(doc.context,'$.implementation_document_authoring')='object'
                THEN (EXISTS(SELECT 1 FROM json_each(json_array(
                    json_extract(doc.context,'$.architecture_document'),
                    CASE WHEN json_extract(doc.context,'$.implementation_document_authoring.revision')=0
                        THEN json_extract(doc.context,'$.approved_proposal_document') END,
                    CASE WHEN json_extract(doc.context,'$.implementation_document_authoring.revision')=0
                        THEN json_extract(doc.context,'$.frozen_experiment_plan_document') END,
                    CASE WHEN json_extract(doc.context,'$.implementation_document_authoring.revision')=0
                        THEN json_extract(doc.context,'$.approved_definition_document') END,
                    CASE WHEN json_extract(doc.context,'$.implementation_document_authoring.revision')=0
                        THEN json_extract(doc.context,'$.protocol_document') END,
                    CASE WHEN json_extract(doc.context,'$.implementation_document_authoring.revision')=0
                        THEN json_extract(doc.context,'$.implementation_capability_document') END)) required
                    WHERE json_extract(required.value,'$.document_id')=json_extract(doc.value,'$.document_id')
                        AND json_extract(required.value,'$.sha256')=json_extract(doc.value,'$.sha256'))
                    OR EXISTS(SELECT 1 FROM json_each(doc.context,'$.previous_specifications') previous
                        WHERE json_extract(previous.value,'$.index')=
                            json_extract(doc.context,'$.implementation_document_authoring.revision')-1
                            AND json_extract(previous.value,'$.document.document_id')=json_extract(doc.value,'$.document_id')
                            AND json_extract(previous.value,'$.document.sha256')=json_extract(doc.value,'$.sha256')))
            ELSE 1 END,
        'sections',(SELECT json_group_array(json_object('id',json_extract(s.value,'$.id'),
            'title',substr(json_extract(s.value,'$.title'),1,160),
            'start',json_extract(s.value,'$.start'),'end',json_extract(s.value,'$.end')))
            FROM json_each(doc.value,'$.sections') s))
        FROM documents doc""",
        (run_id, hid, run_id))
    return [json.loads(row[0]) for row in rows]


def _reading(db, run_id, hid, directories, *, store=None, role=None):
    if not directories:
        return None
    docs = {(doc['document_id'], doc['sha256']): doc for doc in directories
            if isinstance(doc.get('document_id'), str) and isinstance(doc.get('sha256'), str)
            and type(doc.get('total_chars')) is int and doc['total_chars'] > 0
            and doc.get('progress_required', 1)}
    if not docs:
        return None
    rows = db.execute("""SELECT json_object(
        'document_id',json_extract(t.value,'$.result.document_id'),'sha256',json_extract(t.value,'$.result.sha256'),
        'section_id',json_extract(t.value,'$.result.section_id'),
        'start',json_extract(t.value,'$.result.char_start'),'end',json_extract(t.value,'$.result.char_end'),
        'offset',coalesce(json_extract(t.value,'$.call.arguments.offset'),0),
        'limit',coalesce(json_extract(t.value,'$.call.arguments.limit'),4000),
        'text_chars',length(json_extract(t.value,'$.result.text')))
        FROM records p,json_each(p.data,'$.tool_results') t
        WHERE p.collection='handoff_progress' AND p.key=? AND json_extract(p.data,'$.run_id')=?
            AND json_extract(p.data,'$.id')=p.key
            AND json_extract(t.value,'$.call.name')='documents.read'
            AND json_extract(t.value,'$.predecessor_handoff_id') IS NULL
            AND json_extract(t.value,'$.result.error') IS NULL
            AND json_extract(t.value,'$.result.provenance')='exact_document_section'
            AND json_type(t.value,'$.result.text')='text'
            AND json_extract(t.value,'$.call.arguments.document_id')=json_extract(t.value,'$.result.document_id')
            AND json_extract(t.value,'$.call.arguments.expected_sha256')=json_extract(t.value,'$.result.sha256')
            AND json_extract(t.value,'$.call.arguments.section_id')=json_extract(t.value,'$.result.section_id')""",
        (hid, run_id))
    # Legacy receipts retain their existing, explicitly labelled projection.
    # A tracked assignment must additionally prove delivery to this role's
    # acknowledged requests; a read result alone only proves retrieval.
    from .evidence_delivery import tracking_required, delivery_spans
    tracked = bool(store and tracking_required(store, run_id, hid))
    delivered = {}
    spans = {}
    for row in rows:
        page = json.loads(row[0])
        key = (page['document_id'], page['sha256'])
        doc = docs.get(key)
        if not doc:
            continue
        section = next((s for s in doc.get('sections', []) if s['id'] == page['section_id']), None)
        start, end, offset, limit = (page[name] for name in ('start', 'end', 'offset', 'limit'))
        if (not section or any(type(value) is not int for value in (start, end, offset, limit, section['start'], section['end']))
                or not 1 <= limit <= 6000 or not section['start'] <= start < end <= section['end']
                or start != section['start'] + offset or end > start + limit or end - start != page['text_chars']):
            continue
        if tracked:
            # A document without a valid retrieval can contribute no progress.
            # Keep it in the denominator, but do not authenticate the entire
            # submitted inventory just to intersect it with an empty range.
            if key not in delivered:
                delivered[key] = delivery_spans(store, run_id, role, key[0], key[1],
                    identity_key='document_id', handoff_id=hid)
            for receipt in delivered[key]:
                if receipt.get('section_id') == page['section_id']:
                    a, b = max(start, receipt['char_start']), min(end, receipt['char_end'])
                    if a < b:
                        spans.setdefault(key, []).append((a, b))
        else:
            spans.setdefault(key, []).append((start, end))
    completed, documents_completed = 0, 0
    for key, doc in docs.items():
        merged = []
        for start, end in sorted(spans.get(key, [])):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        read = min(doc['total_chars'], sum(end - start for start, end in merged))
        completed += read
        documents_completed += read == doc['total_chars']
    total = sum(doc['total_chars'] for doc in docs.values())
    optional = {(doc['document_id'], doc['sha256']): doc for doc in directories
        if isinstance(doc.get('document_id'), str) and isinstance(doc.get('sha256'), str)
        and type(doc.get('total_chars')) is int and doc['total_chars'] > 0
        and doc.get('kind') != 'document_working_notes'
        and (doc['document_id'], doc['sha256']) not in docs}
    percent = 100 if completed == total else min(99.9, round(completed * 100 / total, 1))
    return {'percent': percent, 'completed': completed, 'total': total,
            'unit': 'characters', 'documents_completed': documents_completed,
            'documents_total': len(docs), 'reported': True,
            'source': 'same_handoff_delivered_receipts' if tracked else 'same_handoff_read_receipts',
            'scope': 'current_task_sources', 'optional_documents': len(optional),
            'optional_chars': sum(doc['total_chars'] for doc in optional.values())}


def _tool(db, run_id, hid, role, directories, request_at=None):
    # Limit an indexed handoff slice before projecting any public arguments.
    rows = db.execute("""SELECT id,kind,message,created_at,
        json_object('call_id',json_extract(data,'$.tool_call_id'),
            'document_id',coalesce(json_extract(data,'$.arguments.document_id'),json_extract(data,'$.result.document_id')),
            'section_id',coalesce(json_extract(data,'$.arguments.section_id'),json_extract(data,'$.result.section_id')),
            'expected_sha256',json_extract(data,'$.arguments.expected_sha256'),
            'result_sha256',json_extract(data,'$.result.sha256'),
            'result_document_id',json_extract(data,'$.result.document_id'),
            'result_section_id',json_extract(data,'$.result.section_id'),
            'section_title',substr(json_extract(data,'$.result.title'),1,160),
            'offset',json_extract(data,'$.arguments.offset'),'limit',json_extract(data,'$.arguments.limit'),
            'char_start',json_extract(data,'$.result.char_start'),'char_end',json_extract(data,'$.result.char_end'),
            'validation_code',CASE WHEN message IN ('implementation.write_document','implementation.edit_document')
                AND json_extract(data,'$.result.saved')=0 THEN substr(json_extract(data,'$.result.validation.code'),1,80) END,
            'validation_detail',CASE WHEN message IN ('implementation.write_document','implementation.edit_document')
                AND json_extract(data,'$.result.saved')=0 THEN substr((SELECT group_concat(detail,'；') FROM (
                    SELECT json_extract(err.value,'$.field_path')||'：'||substr(json_extract(err.value,'$.reason'),1,180) AS detail
                    FROM json_each(data,'$.result.validation.errors') err LIMIT 3)),1,600) END,
            'error',substr(coalesce(json_extract(data,'$.error'),json_extract(data,'$.result.error')),1,400)) AS meta
        FROM events WHERE run_id=? AND json_extract(data,'$.handoff_id')=?
            AND kind IN ('tool.started','tool.completed','tool.failed') AND json_extract(data,'$.role')=?
        ORDER BY id DESC LIMIT 64""", (run_id, hid, role)).fetchall()
    if not rows:
        return None
    decoded = [{**dict(row), 'meta': json.loads(row['meta'])} for row in rows]
    finished = {(row['message'], row['meta']['call_id']) for row in decoded
                if row['kind'] != 'tool.started' and row['meta']['call_id']}
    # Harness tools run sequentially. A newer real start supersedes an orphaned
    # start left by a process interruption, without inventing its completion.
    newest_start = next((row for row in decoded if row['kind'] == 'tool.started' and row['meta']['call_id']), None)
    active = (newest_start if newest_start
              and (newest_start['message'], newest_start['meta']['call_id']) not in finished
              and (not request_at or newest_start['created_at'] >= request_at) else None)
    latest = active or decoded[0]
    if latest['kind'] == 'tool.started' and not latest['meta']['call_id']:
        return None  # Historical uncorrelated events cannot prove a live call.
    if not active and latest['kind'] == 'tool.started':
        return None  # A subsequent model dispatch proves this start is stale.
    start = latest if latest['kind'] == 'tool.started' else next((row for row in decoded
        if latest['meta']['call_id'] and
        row['kind'] == 'tool.started' and row['meta']['call_id'] == latest['meta']['call_id']
        and row['message'] == latest['message']), None)
    args = (start or latest)['meta']
    result = latest['meta']
    expected_sha, actual_sha = args['expected_sha256'], result['result_sha256']
    sha = actual_sha or expected_sha
    identity_matches = (isinstance(sha, str) and bool(sha)
        and (not expected_sha or not actual_sha or expected_sha == actual_sha)
        and (not result['result_document_id'] or result['result_document_id'] == args['document_id'])
        and (not result['result_section_id'] or result['result_section_id'] == args['section_id']))
    doc = next((doc for doc in directories if identity_matches
        and doc['document_id'] == args['document_id'] and doc['sha256'] == sha), {})
    section = next((s for s in doc.get('sections', []) if s['id'] == args['section_id']), {})
    document_title = doc.get('title')
    if not document_title and identity_matches:
        # Display-only navigation for explicitly requested documents outside the
        # prepared reading scope, including a newly saved author's own draft.
        # Project its bounded title from the exact owned version, never its body
        # or sections; this must not add any denominator or reading credit.
        manifest = db.execute("""SELECT substr(json_extract(data,'$.title'),1,200)
            FROM records WHERE collection='memory_documents' AND key=?
                AND json_extract(data,'$.run_id')=?
                AND json_extract(data,'$.text_sha256')=?
                AND json_extract(data,'$.document_index_protocol')=1""",
            (args['document_id'], run_id, sha)).fetchone()
        document_title = manifest[0] if manifest else None
    section_label = section.get('title')
    if (not section_label and identity_matches and latest['kind'] == 'tool.completed'
            and latest['message'] == 'documents.read' and not result['error']):
        section_label = result['section_title']
    limit = args['limit']
    page_start, page_end = latest['meta']['char_start'], latest['meta']['char_end']
    actual_read = (latest['kind'] == 'tool.completed' and latest['message'] == 'documents.read'
                   and not latest['meta']['error'] and type(page_start) is int and type(page_end) is int and page_end > page_start)
    if actual_read:
        limit = page_end - page_start
    return {'name': latest['message'], 'label': TOOL_LABELS.get(latest['message'], latest['message']),
            'status': 'running' if active else 'failed' if latest['kind'] == 'tool.failed' or latest['meta']['error'] else 'completed',
            'document_id': args['document_id'], 'document_title': document_title,
            'section_id': args['section_id'], 'section_label': section_label,
            'offset': args['offset'], 'limit': limit,
            'range_kind': 'actual_read' if actual_read else 'pending_request',
            'started_at': (start or {}).get('created_at'),
            'completed_at': latest['created_at'] if latest['kind'] != 'tool.started' else None,
            'updated_at': latest['created_at'], 'error': latest['meta']['error'],
            'validation_code': latest['meta']['validation_code'],
            'validation_detail': latest['meta']['validation_detail']}


def _worker_alive(pid, started):
    try:
        return type(pid) is int and abs(psutil.Process(pid).create_time() - started) < .1
    except (psutil.Error, TypeError):
        return False


def _recovery(db, run, current, status, role, meta):
    """Display host recovery only; retry targets grant no general task ownership.

    A saved answer whose tools already advanced the cursor is deliberately absent
    from operator_resumes.targets. The current workflow and fresh graph entry,
    rather than that uncertain-request retry list, bind this display to its task.
    """
    if (run.get('status') != 'running' or run.get('stage') != 'implementation'
            or status not in {'interrupted', 'blocked'}
            or (current.get('purpose') or '').split(':')[0] not in
                {'implementation_spec', 'implementation_document_review'}):
        return None
    generation = run.get('state', {}).get('resume_generation')
    if not isinstance(generation, str) or not generation:
        return None
    row = db.execute("""SELECT json_object(
        'handoff_status',json_extract(h.data,'$.status'),
        'handoff_at',json_extract(h.data,'$.updated_at'),
        'handoff_role',json_extract(h.data,'$.to_role'),
        'handoff_stage',json_extract(h.data,'$.stage'),
        'handoff_cycle',json_extract(h.data,'$.cycle'),
        'run_cycle',coalesce(json_extract(r.data,'$.state.research_cycle'),1),
        'generation_id',json_extract(g.data,'$.id'),
        'generation_at',json_extract(g.data,'$.created_at'),
        'generation_kind',json_extract(g.data,'$.kind'),
        'held',json_extract(l.data,'$.held'),
        'owner_pid',json_extract(r.data,'$.owner_pid'),
        'owner_started',json_extract(r.data,'$.owner_started'),
        'owner_instance',json_extract(r.data,'$.owner_instance'),
        'lease_pid',json_extract(l.data,'$.owner_pid'),
        'lease_started',json_extract(l.data,'$.owner_started'),
        'lease_instance',json_extract(l.data,'$.owner_instance'),
        'graph_node',json_extract(s.data,'$.node'),
        'graph_status',json_extract(s.data,'$.status'),
        'graph_at',json_extract(s.data,'$.updated_at'),
        'workflow_id',w.key,'workflow_revision',json_extract(w.data,'$.current_revision'),
        'workflow_status',json_extract(w.data,'$.status'))
        FROM records r
        JOIN records g ON g.collection='operator_resumes' AND g.key=?
            AND json_extract(g.data,'$.run_id')=r.key
        JOIN records l ON l.collection='runtime' AND l.key='worker'
            AND json_extract(l.data,'$.run_id')=r.key
        JOIN records s ON s.collection='graph_status' AND s.key=r.key
        JOIN records h ON h.collection='handoffs' AND h.key=?
            AND json_extract(h.data,'$.run_id')=r.key
        JOIN records w ON w.collection='codex_workflows' AND w.key=(
            SELECT key FROM records WHERE collection='codex_workflows'
                AND json_extract(data,'$.run_id')=r.key
                AND json_extract(data,'$.cycle')=coalesce(json_extract(r.data,'$.state.research_cycle'),1)
                AND json_type(data,'$.implementation_indexed_review_protocol')='integer'
                AND json_extract(data,'$.implementation_indexed_review_protocol')=1
                AND json_extract(data,'$.implementation_generation') IS json_extract(r.data,'$.state.implementation_generation')
            ORDER BY updated_at DESC,key DESC LIMIT 1)
        WHERE r.collection='runs' AND r.key=?
            AND json_extract(r.data,'$.status')='running'
            AND json_extract(r.data,'$.stage')='implementation'
            AND json_extract(r.data,'$.state.resume_generation')=?""",
        (generation, current['id'], run['id'], generation)).fetchone()
    if not row:
        return None
    value = json.loads(row[0])
    workflow_phases = ({'specification', 'blocked'} if
        (current.get('purpose') or '').split(':')[0] == 'implementation_spec' else {'spec_review', 'blocked'})
    if (value['handoff_status'] != status or value['handoff_role'] != role
            or value['handoff_stage'] not in {None, 'implementation'}
            or value['handoff_cycle'] not in {None, value['run_cycle']}
            or value['workflow_id'] != meta.get('workflow_id')
            or value['workflow_revision'] != meta.get('revision')
            or value['workflow_status'] not in workflow_phases
            or meta.get('workflow_run_id') != run['id']
            or value['generation_kind'] != 'explicit_start_command'
            or value['generation_id'] != generation
            or value['graph_node'] != 'implementation'
            or value['graph_status'] not in {None, 'running'}
            or value['held'] != 1 or not value['owner_instance']
            or (value['owner_pid'], value['owner_started'], value['owner_instance']) !=
               (value['lease_pid'], value['lease_started'], value['lease_instance'])):
        return None
    try:
        resumed = datetime.fromisoformat(value['generation_at'])
        if (datetime.fromisoformat(value['graph_at']) < resumed
                or datetime.fromisoformat(value['handoff_at']) > resumed):
            return None
    except (TypeError, ValueError):
        return None
    if not _worker_alive(value['owner_pid'], value['owner_started']):
        return None
    return {'phase': 'host_validation', 'original_status': status,
            'original_error': (current.get('error') or '')[:500],
            'operator_resume_generation': generation, 'started_at': value['graph_at'],
            'provenance': 'current_worker_resume_and_graph_metadata',
            'model_work_claimed': False, 'scientific_approval': False,
            'execution_authorization': False}


def _revision_memory(db, run_id, hid):
    row = db.execute("""SELECT coalesce(
        CASE WHEN json_extract(data,'$.mode')='final_only'
          THEN json_extract(data,'$.finalization.prepared.context.implementation_revision_memory') END,
        json_extract(data,'$.normal_request.prepared.context.implementation_revision_memory'))
        FROM records WHERE collection='handoff_progress' AND key=? AND json_extract(data,'$.run_id')=?""",
        (hid, run_id)).fetchone()
    if not row or not row[0]:
        return None
    from .implementation_review_routing import digest
    value = json.loads(row[0])
    if (value.get('sha256') != digest({k: v for k, v in value.items() if k != 'sha256'})
            or value.get('role') != 'engineer' or value.get('reading_credit') is not False):
        return None
    return {'source_revision': value['previous_output']['revision'] + 1,
            'selected_fields': len(value['previous_output']['fields']),
            'selected_notes': len(value['working_notes']['selected']),
            'historical_read_chars': sum(d['covered_chars'] for d in value['historical_reading']),
            **({'current_fields': len(value['current_output']['fields'])
                                  + len(value['current_output']['inherited_fields']),
                'restored_source_chars': sum(len(part['text']) for part in value['restored_reading'])}
               if value.get('version') in (2, 3) else {}),
            'source': 'prepared_revision_memory'}


def _deferred_reading(store, run_id, hid, role, workflow_id, directories):
    from functools import partial
    from . import assignment_reading_cache as cache
    token = cache.revision(store, hid, workflow_id, directories)
    path = cache.database_identity(store.path)
    identity = (path, run_id, hid, role)

    calculate = partial(cache.calculate_reading, path, run_id, hid, role, workflow_id, token)
    return cache.CACHE.get(identity, token, calculate)


def view(store, run, current, status, role, *, defer_reading=False):
    """Project the actual assignment, keeping paused/blocked phase visible."""
    if not current:
        return None
    purpose = (current.get('purpose') or '').split(':')[0]
    hid = current['id']
    # One request-local SQLite snapshot keeps directory and delivery metadata
    # coherent across a concurrent tool/checkpoint commit. Its bounded cache is
    # discarded on exit; a later view revalidates modified manifests/receipts.
    with store.read_snapshot() as snapshot, snapshot._read_db() as db:
        if defer_reading:
            from .activity_projection_cache import project
            meta = project(snapshot, run['id'], hid, 'metadata')
        else:
            meta = _meta(db, run['id'], hid)
        generation = run.get('state', {}).get('implementation_generation')
        if (purpose in {'implementation_spec', 'implementation_document_review'} and generation is not None
                and (meta.get('generation') != generation or meta.get('workflow_run_id') != run['id'])):
            return None
        author_revision = _author_revision(db, run['id'], hid, meta) if purpose == 'implementation_spec' else None
        batch_revision = _batch_revision(db, run['id'], hid) if purpose == 'implementation_spec' else None
        if batch_revision is not None:
            from .implementation_revision_activity import self_check, latest_notes
            batch_revision['self_check'] = self_check(db, run['id'], hid, meta)
            batch_revision['latest_notes'] = latest_notes(db, run['id'], hid)
        directories = []
        if purpose in _IMPLEMENTATION and not meta.get('save_only'):
            directories = (project(snapshot, run['id'], hid, 'directories') if defer_reading
                else _directories(db, run['id'], hid))
        from .evidence_delivery import tracking_required
        if (defer_reading and directories and purpose in _IMPLEMENTATION
                and tracking_required(snapshot, run['id'], hid)):
            reading = _deferred_reading(snapshot, run['id'], hid, role, meta.get('workflow_id'), directories)
        else:
            reading = _reading(db, run['id'], hid, directories, store=snapshot, role=role) if purpose in _IMPLEMENTATION else None
        tool = _tool(db, run['id'], hid, role, directories, meta.get('request_at'))
        recovery = _recovery(db, run, current, status, role, meta)
        memory = _revision_memory(db, run['id'], hid) if purpose == 'implementation_spec' else None
    if purpose == 'code_review' and reading:
        # Code review authenticates the cited ranges for each issue, not every
        # document in its navigation catalog. Preserve receipt counts, but never
        # present catalog coverage as required reading or task completion.
        catalog = {(doc['document_id'], doc['sha256']): doc for doc in directories
            if isinstance(doc.get('document_id'), str) and isinstance(doc.get('sha256'), str)
            and type(doc.get('total_chars')) is int and doc['total_chars'] > 0
            and doc.get('kind') != 'document_working_notes'}
        groups = {}
        for doc in catalog.values():
            kind = doc.get('kind') or 'other'
            group = groups.setdefault(kind, {'kind': kind, 'documents': 0, 'characters': 0})
            group['documents'] += 1
            group['characters'] += doc['total_chars']
        reading = {**reading, 'scope': 'reference_catalog', 'percent': None,
            'catalog_documents': len(catalog),
            'catalog_chars': sum(doc['total_chars'] for doc in catalog.values()),
            'catalog_groups': sorted(groups.values(), key=lambda group: (-group['characters'], group['kind']))}
    if purpose not in _IMPLEMENTATION and purpose != 'implementation' and not tool:
        return None
    revision = meta.get('revision')
    version = revision + 1 if type(revision) is int and revision >= 0 else None
    saved = bool(meta.get('saved_draft'))
    author = purpose == 'implementation_spec'
    phase = ('document_revision' if author_revision is not None or batch_revision is not None else 'document_checking' if saved else 'document_saving' if meta.get('save_only') or ':save:' in current.get('purpose', '')
             else 'document_revision' if version and version > 1 else 'document_authoring') if author else (
        'document_review' if purpose in {'implementation_document_review', 'spec_review'} else
        'code_repair' if purpose in {'code_repair_map', 'code_repair_audit', 'code_repair_amend'} else
        'code_review' if purpose == 'code_review' else 'code_generation' if purpose == 'implementation' else 'general')
    noun = {'document_authoring': '实现文档编写', 'document_revision': '实现文档修订',
            'document_checking': '已保存文档核对与提交', 'document_review': '实现文档全文审核',
            'document_saving': '实现文档整理与保存',
            'code_generation': '代码实现', 'code_review': '实际代码审核', 'code_repair': '代码修改依据核对'}.get(phase, '当前任务')
    running_labels = {'document_authoring': '正在编写实现文档', 'document_revision': '正在修订实现文档',
        'document_checking': '正在核对已保存文档并提交', 'document_review': '正在阅读全文审核',
        'document_saving': '正在整理并保存实现文档',
        'code_generation': '正在实现代码', 'code_review': '正在审核实际代码', 'code_repair': '正在核对修改依据'}
    # The current prepared request owns this display, including an old request
    # still in flight. Reading receipts and a newer progress marker cannot prove
    # delivery or silently turn a tool-capable request into final submission.
    review_final = phase == 'document_review' and meta.get('request_final_only')
    review_consolidating = (phase == 'document_review' and not review_final
                           and meta.get('review_closeout_phase') == 'consolidating')
    if review_final:
        running_labels['document_review'] = '正在提交最终审核意见'
    elif review_consolidating:
        running_labels['document_review'] = '正在汇总审核记录与必要核对'
    label = (running_labels.get(phase, '正在执行当前任务') if status == 'running' else
        noun + {'paused': '已暂停', 'blocked': '等待处理', 'failed': '失败', 'cancelled': '已取消',
                'interrupted': '已中断', 'completed': '已完成', 'queued': '待开始'}.get(status, '等待执行'))
    progress = None
    if author:
        if batch_revision is not None:
            total, check = batch_revision['total'], batch_revision['self_check']
            total = max(0, total - len(check.get('review_pending', [])))
            reported = check['state'] != 'not_reported'
            checkpointed = check['state'] in {'checkpointing','checkpointed'}
            progress = {'kind': 'writing', 'label': '作者自检记录' if checkpointed else '作者自检报告',
                'percent': None, 'completed': check['complete'], 'total': total, 'unit': 'revision_items',
                'reported': reported, 'source': 'current_version_author_self_check',
                'status_label': ('逐条自检中' if check['state']=='checkpointing' else
                    '已登记，待最终报告' if check['state']=='checkpointed' else
                    '仍有待补齐项' if check['state'] == 'needs_revision' else
                    '已报告，待提交校验' if reported else '尚未提交自检'),
                'completed_label': '作者报告已完成'}
        elif author_revision is not None:
            progress = {'kind': 'writing', 'label': '草稿保存 / 修订待核对', 'percent': None,
                        'completed': None, 'total': author_revision['required_items'], 'unit': 'revision_items',
                        'reported': False, 'source': 'latest_actual_author_candidate_self_check'}
        else:
            progress = {'kind': 'writing', 'label': '编写进度', 'percent': 100 if saved else None,
                        'completed': 1 if saved else None, 'total': 1 if saved else None, 'unit': 'documents',
                        'reported': saved, 'source': 'saved_document_receipt' if saved else None}
    elif phase == 'document_review' and reading:
        progress = {**reading, 'kind': 'reading', 'label': '审核阅读进度'}
    detail = ('正在保留成功修改并处理剩余意见。' if batch_revision is not None else
        '正在处理当前修订任务。' if author_revision is not None else
        '已保存本版完整文档；正在核对与提交。' if saved else '本版文档尚未保存，编写百分比未上报。' if author else
        '核对当前版本的文档与验收要求。' if phase == 'document_review' else '正在等待当前任务的执行记录。')
    if current.get('activity') == 'native_readback':
        detail = '正在读取原请求状态与已保存回复。'
    elif tool and tool['status'] == 'running':
        detail = '正在调用 ' + tool['name'] + '：' + tool['label'] + '。'
    elif meta.get('pending_tool'):
        detail = '模型已返回工具请求，准备调用 ' + meta['pending_tool'] + '。'
    elif meta.get('request_status') == 'dispatching':
        detail = '等待模型响应：' + ('整理当前实现文档并准备保存。' if phase == 'document_saving' else
            '根据当前来源编写实现文档。' if author and not saved else
            '提交当前文档的最终审核意见。' if review_final else
            '汇总审核记录并完成必要核对。' if review_consolidating else
            '补读核对并整理当前文档的审核记录。' if phase == 'document_review' else '处理当前任务。')
    elif meta.get('request_status') == 'response_saved':
        detail = '模型回复已保存，正在整理本次输出。'
    elif tool and tool['status'] == 'failed':
        detail = '工具 ' + tool['name'] + '执行失败：' + (tool['error'] or '等待处理失败回执。')
    if author_revision is not None:
        if not saved:
            revision_detail = '本版草稿尚未保存；修订完成度待核对。'
        elif author_revision['state'] == 'incomplete':
            revision_detail = f"草稿已保存；作者自检仍有 {author_revision['declared_incomplete']} 项未完成，修订尚未结束。"
        elif author_revision['state'] == 'waiting_validation':
            revision_detail = f"草稿已保存；作者已上报 {author_revision['required_items']} 项修订自检，结果等待核对。"
        else:
            revision_detail = '草稿已保存；尚无绑定当前稿的完整修订自检，修订进度待核对。'
        detail = revision_detail + ' ' + detail
    if batch_revision is not None:
        detail = ('修改保存与自检分别统计；保存记录不代表整条意见已完成。全部补齐并自检后才提交下一轮审核。 ' + detail)
    if status in {'paused', 'blocked', 'failed', 'cancelled', 'interrupted'}:
        detail = (current.get('error') or run.get('error') or meta.get('request_error') or
                  ('恢复后继续当前' + noun + '。'))[:500]
    elif status == 'completed':
        detail = '当前交接的输出已保存，等待下一次任务分派。'
    original_status = status
    if recovery:
        status = 'recovering'
        label = noun + '恢复校验中'
        detail = '宿主正在恢复实现阶段并校验已保存来源与执行约束；当前交接尚未重新派发模型请求。'
    updated = max(value for value in (current.get('updated_at'), meta.get('updated_at'),
                                      (tool or {}).get('updated_at'), (recovery or {}).get('started_at')) if value) if any(
        (current.get('updated_at'), meta.get('updated_at'), (tool or {}).get('updated_at'), (recovery or {}).get('started_at'))) else None
    return {'handoff_id': hid, 'phase': phase, 'label': label, 'state': status, 'detail': detail,
            'version': version, 'round': version if phase == 'document_review' else None,
            'updated_at': updated, 'progress': progress, 'reading': reading,
            **({'revision_memory': memory} if memory else {}),
            'tool': tool, 'request_status': meta.get('request_status'), 'tool_turn': meta.get('tool_turn'),
            **({'revision_completion': author_revision} if author_revision is not None else {}),
            **({'revision_status': batch_revision} if batch_revision is not None else {}),
            **({'original_status': original_status, 'recovery': recovery} if recovery else {})}
