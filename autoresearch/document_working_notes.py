"""Source-anchored author notes for indexed, bounded document continuations.

These are a role's working observations, never primary evidence or approval.
Original pages stay in the full read ledger; notes use the existing MD index.
An opt-in flag preserves every already dispatched assignment's schema.
"""
from __future__ import annotations

import copy
import hashlib
import json

from . import document_index
from .document_read_window import enabled as window_enabled

VERSION = 1
HOT_LIMIT = 6000
NOTE_DIRECTORIES = 'document_working_note_documents'
CLOSEOUT = '_indexed_review_closeout'


class WorkingNoteAnchorError(ValueError):
    """Correctable model metadata; never an altered file or read receipt."""
    def __init__(self, message, path, directory, pages):
        super().__init__(message)
        self.details = {'path':path, 'reason':message,
            'expected_document': {k:directory[k] for k in ('document_id','sha256','total_chars')} if directory else None,
            'available_read_ranges':list({(p['document_id'],p['sha256'],p['section_id'],p['char_start'],p['char_end']):{
                k:p[k] for k in ('document_id','sha256','section_id','char_start','char_end')}
                for p in pages if directory and p['document_id']==directory['document_id']}.values()),
            'action':'Use the original document sha256, section and inspected Unicode range. '
                'Do not substitute receipt_sha256 or a summary anchor. No submitted notes were accepted.'}


def enabled(context):
    return context.get('document_working_notes_protocol') == VERSION and window_enabled(context)


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def obj(fields):
    return {'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}


ANCHOR = obj({'document_id':{'type':'string','minLength':1},
    'expected_sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'},
    'section_id':{'type':'string','minLength':1},
    'char_start':{'type':'integer','minimum':0},'char_end':{'type':'integer','minimum':1}})
NOTES = {'type':'array','minItems':1,'maxItems':6,'items':obj({
    'kind':{'enum':['observation','open_issue']},
    'text':{'type':'string','minLength':1,'maxLength':2000,'pattern':r'\S'},
    'anchors':{'type':'array','minItems':1,'maxItems':6,'items':ANCHOR}})}


def _checked(engine, run_id, role, key, progress):
    record = progress.get('document_working_notes')
    if not record:
        return None
    payload = record.get('payload',{})
    if (record.get('sha256') != sha(payload) or payload.get('version') != VERSION
            or payload.get('run_id') != run_id or payload.get('role') != role
            or payload.get('handoff_id') != key):
        raise ValueError('Document working notes identity or fingerprint changed')
    directory = record['document']
    _, text = document_index.load(engine, run_id, directory['document_id'], directory['sha256'])
    if text != render(payload):
        raise ValueError('Document working notes differ from their indexed original')
    originals = {}
    for entry in payload['entries']:
        if entry['id'] != sha({field:entry[field] for field in ('kind','text','anchors')}):
            raise ValueError('Document working note entry changed')
        for anchor in entry['anchors']:
            did = anchor['document_id']
            if did not in originals:
                manifest,original = document_index.load(engine,run_id,did,anchor['expected_sha256'])
                if manifest.get('document_kind') == 'document_working_notes':
                    raise ValueError('Working notes must retain original input anchors')
                originals[did] = original
            source = originals[did][anchor['char_start']:anchor['char_end']]
            if hashlib.sha256(source.encode('utf-8')).hexdigest() != anchor['source_text_sha256']:
                raise ValueError('Working note source anchor changed')
    return record


def render(payload):
    return ('# 本次文档阅读工作笔记\n\n作者观察与待核实问题，不是原始论文、独立评议或执行批准。'
        '每项保留已读取原文的版本和精确范围；科学判断仍须核对原文。\n\n```json\n'
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n```\n')


def _review(context):
    from .implementation_indexed_review import CONTRACT
    return CONTRACT in context


def _closeout(context, run_id, role, key, progress):
    """Only the host's same-handoff closeout may expand the recent-note view."""
    from .implementation_indexed_review import CONTRACT
    marker = context.get(CLOSEOUT)
    if marker is None:
        return None
    if (not isinstance(marker, dict) or marker != progress.get('indexed_review_closeout')
            or type(marker.get('version')) is not int or marker['version'] != VERSION
            or marker.get('phase') not in {'consolidating', 'final'}
            or marker.get('run_id') != run_id or marker.get('role') != role
            or marker.get('handoff_id') != key or marker.get('scientific_approval') is not False
            or not _review(context)
            or marker.get('contract_sha256') != context[CONTRACT].get('sha256')):
        raise ValueError('Indexed review closeout notes require the trusted same-handoff progress and frozen contract')
    return marker


def read_directories(context):
    """A read window may include separately authenticated ancillary notes.

    This view is never the frozen review scope or its required reading list.
    Callers which authorize tools must use checked_tool_directories first.
    """
    # Old final-review packets may list a definition both directly and through
    # its grouped scope. Collapse identical directory entries only in this
    # read view; never rewrite the frozen assignment or forgive conflicting
    # identities. Ancillary notes still cannot shadow any primary entry.
    primary = {}
    for doc in context['indexed_documents']:
        did = doc['document_id']
        if did in primary and primary[did] != doc:
            raise ValueError('Frozen review directory contains conflicting document metadata')
        primary[did] = doc
    documents = list(primary.values())
    extra = context.get(NOTE_DIRECTORIES, [])
    if (not isinstance(extra, list) or any(not isinstance(doc, dict)
            or doc.get('kind') != 'document_working_notes'
            or doc.get('primary_source_reading') is not False
            or doc.get('scientific_claims_verified') is not False
            or 'text' in doc or any('text' in section for section in doc.get('sections', []))
            for doc in extra)):
        raise ValueError('Ancillary note directory is not non-primary metadata')
    documents.extend(extra)
    if len({doc['document_id'] for doc in documents}) != len(documents):
        raise ValueError('Ancillary note directory duplicates the frozen review scope')
    return documents


def _review_base(h, run_id, role, key, context):
    from .implementation_indexed_review import CONTRACT, assert_context
    frozen = h.engine.store.get('handoff_inputs', key, {})
    assignment = h.engine.store.get('handoffs', key, {})
    base = frozen.get('context', {})
    contract = base.get(CONTRACT, {})
    if (frozen.get('run_id') != run_id or assignment.get('run_id') != run_id
            or assignment.get('to_role') != role or contract.get('role') != role
            or context.get(CONTRACT) != contract):
        raise ValueError('Ancillary notes require this role and frozen review handoff')
    bound = contract.get('binding', {})
    assert_context({'id': bound.get('workflow_id'), 'proposal_hash': bound.get('proposal_hash')},
        {'index': bound.get('revision'), 'spec_hash': bound.get('spec_hash'),
         'document': {'document_id': bound.get('document_id'), 'sha256': bound.get('document_sha256')}},
        base, role=role)
    return copy.deepcopy(base['indexed_documents'])


def _checked_note_directory(h, run_id, role, key, progress, directory, base):
    if not isinstance(directory, dict) or directory.get('kind') != 'document_working_notes':
        raise ValueError('Only this handoff\'s immutable working notes may supplement a review')
    canonical = document_index.directory(h.engine, run_id, directory.get('document_id'), directory.get('sha256'))
    if directory != canonical:
        raise ValueError('Ancillary working note directory changed')
    _, text = document_index.load(h.engine, run_id, canonical['document_id'], canonical['sha256'])
    try:
        payload = json.loads(text.split('```json\n', 1)[1].rsplit('\n```\n', 1)[0])
    except (ValueError, IndexError) as exc:
        raise ValueError('Indexed working notes have no valid immutable payload') from exc
    record = _checked(h.engine, run_id, role, key, {'document_working_notes': {
        'payload': payload, 'sha256': sha(payload), 'document': canonical}})
    if (payload.get('scientific_claims_verified') is not False or payload.get('scientific_approval') is not False
            or any(entry.get('primary_source_reading') is not False
                   or entry.get('scientific_claims_verified') is not False for entry in payload['entries'])):
        raise ValueError('Ancillary notes cannot confer primary reading or scientific approval')
    originals = {doc['document_id']: doc for doc in base}
    for entry in record['payload']['entries']:
        for anchor in entry['anchors']:
            doc = originals.get(anchor['document_id'])
            start, end = anchor['char_start'], anchor['char_end']
            section = next((s for s in (doc or {}).get('sections', []) if s['id'] == anchor['section_id']), None)
            if (not doc or anchor['expected_sha256'] != doc['sha256'] or not section
                    or type(start) is not int or type(end) is not int
                    or not section['start'] <= start < end <= section['end']
                    or not any(row.get('call', {}).get('name') == 'documents.read'
                        and row['call'].get('arguments', {}).get('document_id') == doc['document_id']
                        and row['call']['arguments'].get('expected_sha256') == doc['sha256']
                        and row.get('result', {}).get('document_id') == doc['document_id']
                        and row['result'].get('sha256') == doc['sha256']
                        and row['result'].get('section_id') == section['id']
                        and isinstance(row['result'].get('text'), str)
                        and row['result'].get('char_start', -1) <= start < end <= row['result'].get('char_end', -1)
                        and hashlib.sha256(row['result']['text'][start-row['result']['char_start']:
                            end-row['result']['char_start']].encode('utf-8')).hexdigest() == anchor['source_text_sha256']
                        for row in progress.get('tool_results', []))):
                raise ValueError('Ancillary note anchor is outside this handoff\'s frozen inspected evidence')
    return canonical


def project_review_context(h, run_id, role, key, progress, context):
    """Purely separate legitimate old note additions from a frozen review index.

    Never rewrite a paid request with this helper. Recovery must independently
    prove no native request/reservation/submission before projecting its copy.
    """
    if not _review(context):
        return context
    base = _review_base(h, run_id, role, key, context)
    supplied = context.get('indexed_documents')
    if not isinstance(supplied, list) or supplied[:len(base)] != base:
        raise ValueError('Frozen review directory changed; notes cannot repair its identity')
    extras = [*supplied[len(base):], *context.get(NOTE_DIRECTORIES, [])]
    record = _checked(h.engine, run_id, role, key, progress)
    if (extras or record) and h.engine.store.get('handoffs', key, {}).get('document_working_notes_protocol') != VERSION:
        raise ValueError('Ancillary notes were not enabled for this frozen handoff')
    if extras and not record:
        raise ValueError('Ancillary note directory lacks this handoff\'s captured notes')
    shown = context.get('document_working_notes')
    if shown is not None and (not record or not isinstance(shown, dict)
            or shown.get('role') != role or shown.get('handoff_id') != key
            or shown.get('document') != record['document'] or shown.get('sha256') != record['sha256']
            or shown.get('scientific_claims_verified') is not False or shown.get('scientific_approval') is not False
            or any(entry not in record['payload']['entries'] for entry in shown.get('selected', []))):
        raise ValueError('Ancillary working note projection changed its owner or captured observations')
    if record:
        extras.append(record['document'])
    notes = {}
    for directory in extras:
        checked = _checked_note_directory(h, run_id, role, key, progress, directory, base)
        notes[checked['document_id']] = checked
    result = {**copy.deepcopy(context), 'indexed_documents': base}
    if notes:
        result[NOTE_DIRECTORIES] = list(notes.values())
    elif NOTE_DIRECTORIES in result:
        result[NOTE_DIRECTORIES] = []
    read_directories(result)
    return result


def checked_tool_directories(h, run_id, role, key, progress, context, *, document_id=None, expected_sha256=None):
    """Authorize original review documents and only owned immutable note versions."""
    if _review(context) and document_id is not None:
        base = _review_base(h, run_id, role, key, context)
        if any(doc['document_id'] == document_id for doc in base):
            return base  # Ordinary source reads need no global note-version scan.
        manifest = h.engine.store.get('memory_documents', document_id, {})
        if manifest.get('document_kind') != 'document_working_notes':
            return base
        directory = document_index.directory(h.engine, run_id, document_id, expected_sha256)
        scoped = project_review_context(h, run_id, role, key, progress,
            {**context, NOTE_DIRECTORIES: [directory]})
        return read_directories(scoped)
    scoped = scoped_context(h, run_id, role, key, progress, context)
    if _review(scoped):
        scoped = project_review_context(h, run_id, role, key, progress, scoped)
    return read_directories(scoped)


def scoped_context(h, run_id, role, key, progress, context):
    """Reopen only this assignment's verified immutable note versions.

    Captured policy keeps existing dispatched assignments unchanged. Both tool
    execution and prompt projection use this same directory, never global RAG.
    """
    assignment = h.engine.store.get('handoffs', key) or {}
    if (assignment.get('document_working_notes_protocol') != VERSION
            or not window_enabled(context) or not progress.get('document_working_notes')):
        return context
    _checked(h.engine, run_id, role, key, progress)
    prefix = f'cycle-{h.engine.cycle(run_id)}/document-notes/{sha(key)[:16]}/'
    review = _review(context)
    if review:
        context = project_review_context(h, run_id, role, key, progress, context)
    directories = {row['document_id']:copy.deepcopy(row) for row in
                   (context.get(NOTE_DIRECTORIES, []) if review else context['indexed_documents'])}
    current = _checked(h.engine, run_id, role, key, progress)
    directories[current['document']['document_id']] = copy.deepcopy(current['document'])
    artifacts = {row['id']: row for row in h.engine.store.list('artifacts', run_id)}
    for manifest in h.engine.store.list('memory_documents', run_id):
        aliases = manifest.get('artifact_ids') or [manifest.get('artifact_id')]
        identities = [artifacts.get(aid, {}).get('logical_name')
                      or artifacts.get(aid, {}).get('name', '') for aid in aliases]
        if (manifest.get('document_kind') != 'document_working_notes'
                or not any(name.startswith(prefix)
                           for name in [manifest.get('title', ''), *identities])):
            continue
        directory = document_index.directory(h.engine, run_id, manifest['id'], manifest['text_sha256'])
        _, text = document_index.load(h.engine, run_id, directory['document_id'], directory['sha256'])
        try:
            payload = json.loads(text.split('```json\n', 1)[1].rsplit('\n```\n', 1)[0])
        except (ValueError, IndexError) as exc:
            raise ValueError('Indexed working notes have no valid immutable payload') from exc
        checked = _checked(h.engine, run_id, role, key, {'document_working_notes':{
            'payload':payload, 'sha256':sha(payload), 'document':directory}})
        directories[checked['document']['document_id']] = checked['document']
    result = {**context, (NOTE_DIRECTORIES if review else 'indexed_documents'):list(directories.values())}
    return project_review_context(h, run_id, role, key, progress, result) if review else result


def capture(h, run_id, role, key, prepared, result, progress, response_id=None):
    context = prepared.get('context',{}) if prepared else {}
    notes = result.get('working_notes') if isinstance(result,dict) else None
    if notes is None:
        if (enabled(context) and context.get('document_working_notes_required')
                and isinstance(result,dict) and 'tool_calls' in result
                and any(row.get('call',{}).get('name') == 'documents.read'
                    and isinstance(row.get('result',{}).get('text'),str)
                    for row in progress.get('tool_results',[]))):
            raise ValueError('Inspected document continuations require source-anchored working notes')
        return
    if not enabled(context) or 'tool_calls' not in result:
        raise ValueError('Working notes require an opted-in document continuation')
    from .prose_schema import validate
    validate(notes,NOTES)
    engine = h.engine
    previous = _checked(engine,run_id,role,key,progress)
    entries = copy.deepcopy(previous['payload']['entries']) if previous else []
    directories = {row['document_id']:row for row in context['indexed_documents']}
    raw_pages = [row['result'] for row in progress.get('tool_results',[])
        if row.get('call',{}).get('name') == 'documents.read'
        and isinstance(row.get('result',{}).get('text'),str)]
    originals = {}
    for ni,note in enumerate(notes):
        anchors = []
        for ai,anchor in enumerate(note['anchors']):
            did,start,end = anchor['document_id'],anchor['char_start'],anchor['char_end']
            doc = directories.get(did)
            path = f'/working_notes/{ni}/anchors/{ai}'
            if not doc or anchor['expected_sha256'] != doc['sha256'] or not 0 <= start < end <= doc['total_chars']:
                raise WorkingNoteAnchorError('Working note anchor is outside its frozen document',path,doc,raw_pages)
            section = next((row for row in doc['sections'] if row['id'] == anchor['section_id']),None)
            if not section or start < section['start'] or end > section['end']:
                raise WorkingNoteAnchorError('Working note anchor crosses the indexed section',path,doc,raw_pages)
            pages = [page for page in raw_pages if page.get('document_id') == did
                and page.get('sha256') == doc['sha256'] and page.get('section_id') == section['id']
                and page['char_start'] <= start < end <= page['char_end']]
            if not pages:
                raise WorkingNoteAnchorError('Working note cites a range that was not actually read',path,doc,raw_pages)
            if did not in originals:
                manifest,original = document_index.load(engine,run_id,did,doc['sha256'])
                if manifest.get('document_kind') == 'document_working_notes':
                    raise WorkingNoteAnchorError('Working notes must anchor original input, not another summary',path,doc,raw_pages)
                originals[did] = original
            text = originals[did][start:end]
            if not any(page['text'][start-page['char_start']:end-page['char_start']] == text for page in pages):
                raise ValueError('Working note read receipt no longer matches original bytes')
            anchors.append({**copy.deepcopy(anchor),'source_text_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest()})
        entry = {**copy.deepcopy(note),'anchors':anchors}
        nid = sha(entry)
        if all(row['id'] != nid for row in entries):
            entries.append({'id':nid,**entry,'origin_response_id':response_id,
                'scientific_claims_verified':False,'primary_source_reading':False})
    payload = {'version':VERSION,'run_id':run_id,'role':role,'handoff_id':key,'entries':entries,
        'provenance':'schema_validated_role_observations_with_exact_read_anchors',
        'scientific_claims_verified':False,'scientific_approval':False}
    from .implementation_documents import _save
    digest = sha(payload)
    document = _save(engine,h,run_id,
        f'cycle-{engine.cycle(run_id)}/document-notes/{sha(key)[:16]}/{digest}.md',
        render(payload),'document_working_notes')
    progress['document_working_notes'] = {'payload':payload,'sha256':digest,'document':document}


def prepare(h,run_id,role,key,progress,prepared):
    closeout = _closeout(prepared['context'], run_id, role, key, progress)
    retained = (prepared['context'].get('document_read_window_protocol') == 3
                and prepared['context'].get('implementation_acceptance_protocol') == 1)
    all_notes = closeout or retained
    if not enabled(prepared['context']):
        if closeout:
            _review_base(h, run_id, role, key, prepared['context'])
        return prepared
    if _review(prepared['context']):
        prepared['context'] = project_review_context(h, run_id, role, key, progress, prepared['context'])
    for branch in prepared['schema'].get('anyOf',[]):
        if 'tool_calls' in branch.get('properties',{}):
            branch['properties']['working_notes'] = copy.deepcopy(NOTES)
            if (prepared['context'].get('document_working_notes_required')
                    and any(row.get('call',{}).get('name') == 'documents.read'
                        and isinstance(row.get('result',{}).get('text'),str)
                        for row in progress.get('tool_results',[]))):
                if 'working_notes' not in branch['required']:
                    branch['required'].append('working_notes')
    record = _checked(h.engine,run_id,role,key,progress)
    if record:
        if all_notes:
            # The immutable latest payload accumulates every captured entry.
            # Closeout must see older open issues too; admission, rather than
            # HOT_LIMIT, decides whether this complete view fits the request.
            selected = record['payload']['entries']
        else:
            selected = []
            for entry in reversed(record['payload']['entries']):
                candidate = [entry,*selected]
                if len(json.dumps(candidate,ensure_ascii=False,sort_keys=True)) <= HOT_LIMIT:
                    selected = candidate
        prepared['context']['document_working_notes'] = {
            'role':role,'handoff_id':key,'selected':copy.deepcopy(selected),
            'total_notes':len(record['payload']['entries']),
            'omitted_notes':len(record['payload']['entries'])-len(selected),
            'document':record['document'],'sha256':record['sha256'],
            'scientific_claims_verified':False,'scientific_approval':False}
        if all_notes:
            prepared['context']['document_working_notes'].update(
                selection_policy=('all_verified_same_handoff_entries_for_closeout' if closeout
                                  else 'all_verified_same_handoff_entries_for_retained_review'),
                open_issues_count=sum(entry['kind'] == 'open_issue' for entry in selected))
        directory_key = NOTE_DIRECTORIES if _review(prepared['context']) else 'indexed_documents'
        if all(row['document_id'] != record['document']['document_id']
                for row in prepared['context'].get(directory_key, [])):
            prepared['context'][directory_key] = [*prepared['context'].get(directory_key, []),
                copy.deepcopy(record['document'])]
    prepared['task'] += ('\n阅读连续性：在继续请求工具时可附 working_notes 数组（每次1至6条），'
        '每条为{kind:observation或open_issue,text,anchors:[{document_id,expected_sha256,section_id,char_start,char_end}]}。'
        '只引用已经实际读过的精确范围；笔记正文不设字符上限。保存必要公式、数值、条件、反证和待核实问题，'
        '用于下一请求继续思考；不要只反复请求同一页。笔记只属自己的观察，不替代原文或独立批准。'
        + ('document_working_notes.selected 完整保留本角色本交接的全部已校验笔记与未决问题；'
           '收尾判断须逐项核对，未核实的问题不能因正文离开活跃窗口而消失。'
           if all_notes else 'document_working_notes.selected 保留自己的近期笔记；更早笔记按其 document 索引补读。')
        +
        'document_working_note_documents 是本交接的附属笔记目录，不属于冻结审查文档或必读证据。'
        '不能删除原始证据或把未核实判断写成科学结果。最终仍输出原阶段schema，不添加working_notes。')
    prepared['task'] += (' 若出现 controller.document_working_notes_validation 拒绝回执，按其 available_read_ranges '
        '修正原文锚点；expected_sha256 使用文档 sha256，不能使用 receipt_sha256。被拒绝的整批笔记未保存。')
    return prepared


def prepare_future_policy(engine,run_id):
    """Future handoffs capture the policy; all existing assignments keep theirs."""
    from .engine import ResearchBlocked
    from .storage import now
    run = engine.store.get('runs',run_id)
    key = run_id + ':document-working-notes-policy'
    previous = engine.store.get('stage_contexts',key)
    if previous:
        return previous
    if not run or run['status'] not in {'blocked','failed','paused'}:
        raise ResearchBlocked('须在任务已停止且原请求确定结束后准备阅读工作笔记策略。')
    transient = {'running','queued','starting','turn_start_dispatched','thread_start_dispatched','thread_resume_dispatched'}
    if any((row.get('checkpoint') or {}).get('status') in transient
            for row in engine.store.list('codex_reasoning',run_id)):
        raise ResearchBlocked('原文档请求仍在执行，不能改变其阅读策略。')
    record = {'id':key,'run_id':run_id,'version':VERSION,'source':'controller_bug_fix',
        'status':'prepared','created_at':now(),'scope':'Only newly created indexed assignments. '
            'Keep previous inputs, schemas, decisions, receipts, scientific gates and charges.',
        'document_working_notes_protocol':VERSION}
    engine.store.put('stage_contexts',key,record)
    engine.update_run(run_id,config={**run['config'],'document_working_notes_protocol':VERSION})
    engine.store.event(run_id,'documents.working_notes_prepared',
        '新文档交接保留自己的带原文锚点工作笔记；既有付费交接保持原策略。',{'id':key})
    return record


