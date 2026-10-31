"""Immutable experiment settings extracted from revalidated, actually read notes.

This records attributed claims, not scientific approval or semantic entailment.
No benchmark settings are supplied by the host. Unreported/conflicting fields
cannot become executable task dimensions.
"""
from __future__ import annotations

import copy
import json

from .literature_notes import _digest, _value_in_quote, notes_view
from .storage import now


FIELDS = ('note_id', 'note_sha256', 'dataset', 'setting', 'parameters')
GUIDANCE = '''
论文实验设置须结构化保存：先 sources.read/pdf.read 实际查看原文并用 literature.record_notes
保存摘录，再 literature.record_protocol 绑定 note_id、note_sha256、dataset、setting 和 parameters。
每个参数填写 name、status(reported/not_reported/conflicting)、values、evidence。
values 是原文报告的可选标量值列表，保留数值/字符串类型；evidence 引用该笔记的
{kind:excerpts或results,index:零基索引}，原文、定位、哈希和阅读回执由宿主复制并校验。
reported 值必须逐一见于所引摘录；not_reported/conflicting 的 values 必须为空，不能补默认值。
dataset 本身也作为 reported 参数保存，值与 dataset 字段一致。同一记录只描述同一实验设置，
不能把不同实验表格的参数值拼为组合。用 literature.protocols 查目录/按哈希读取。
各数据集与任务规格在设计的 benchmark_tasks 中逐项列出，引用对应 protocol_id/protocol_sha256，
每项独立提供完整三分支参数、搜索网格、validation_seeds、final_test_seeds。
各任务的 task_dimensions 与分支 comparison_task 声明协议中的共同任务条件及实际 JSON 参数路径；
dataset 与所有共同维度只能选择该协议已报告值，并与原文来源对应。
根级三分支参数/网格在矩阵模式不参与执行，执行参数以各 benchmark_tasks 为准。
未知或冲突的必要设置先定向补读；保存提取记录不代表该参数语义或科学协议已获审核通过。
'''


def argument_schema():
    def obj(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
    text = {'type': 'string', 'minLength': 1, 'maxLength': 1200}
    scalar = {'type': ['string', 'number', 'boolean']}
    parameter = obj({'name': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,79}$'},
        'status': {'enum': ['reported', 'not_reported', 'conflicting']},
        'values': {'type': 'array', 'items': scalar, 'maxItems': 32, 'uniqueItems': True},
        'evidence': {'type': 'array', 'maxItems': 12, 'uniqueItems': True, 'items': obj({
            'kind': {'enum': ['excerpts', 'results']}, 'index': {'type': 'integer', 'minimum': 0, 'maximum': 11}})}})
    return obj({'note_id': text, 'note_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'dataset': text, 'setting': text,
        'parameters': {'type': 'array', 'items': parameter, 'minItems': 1, 'maxItems': 64}})


def _notes(engine, run_id):
    return {row['id']: row for row in notes_view(engine.store, run_id,
        document_loader=engine.sources.document)}


def _compile(args, notes):
    from jsonschema import ValidationError
    from .prose_schema import validate
    from .fair_comparison import strict_equal
    try:
        validate(args, argument_schema())
    except ValidationError as exc:
        raise ValueError('Invalid structured experiment protocol fields') from exc
    json.dumps(args, ensure_ascii=False, allow_nan=False)
    note = notes.get(args['note_id'])
    if not note or note['note_sha256'] != args['note_sha256']:
        raise ValueError('Protocol requires an exact, still-valid task reading note and SHA256')
    # Imported analyses alone never grant current-task source-reading credit.
    if note.get('generation') != 'ai_extraction':
        raise ValueError('Record a task-owned note from actually read source passages before extracting a protocol')
    if args['dataset'] not in note['datasets']:
        raise ValueError('Protocol dataset must occur in the bound reading note datasets')
    parameters = []
    names = set()
    for parameter in args['parameters']:
        name, status, values = (parameter[k] for k in ('name', 'status', 'values'))
        if name in names:
            raise ValueError('Protocol parameter names must be unique')
        names.add(name)
        sources = []
        for ref in parameter['evidence']:
            if type(ref['index']) is not int or ref['index'] >= len(note[ref['kind']]):
                raise ValueError('Protocol evidence index is absent from the frozen note')
            citation = next((c for c in note['citations'] if c['kind'] == ref['kind'] and c['index'] == ref['index']), None)
            if citation is None:
                raise ValueError('Protocol parameter lacks a validated citation')
            sources.append({**copy.deepcopy(citation),
                **{key: note.get(key) for key in ('paper_id', 'source_id', 'text_sha256', 'content_sha256')},
                'note_id': note['id'], 'note_sha256': note['note_sha256'],
                'delivery_receipt_ids': copy.deepcopy(note.get('evidence_delivery_receipt_ids', [])),
                'source_read_receipts': copy.deepcopy(note.get('source_read_receipts', []))})
        if status == 'reported':
            if not values or not sources:
                raise ValueError('Reported parameters require values and actual read evidence')
            for value in values:
                literal = json.dumps(value, ensure_ascii=False, allow_nan=False) if not isinstance(value, str) else value
                if not literal.strip() or not any(_value_in_quote(literal, source['quote']) for source in sources):
                    raise ValueError(f'Parameter {name} value does not occur unchanged in cited original text')
            if name in {'input_length', 'output_length', 'channels'} and any(type(v) is not int or v <= 0 for v in values):
                raise ValueError(f'{name} must contain reported positive integer values')
        elif values or status == 'conflicting' and len(sources) < 2:
            raise ValueError('Unknown/conflicting parameters have no executable values; conflicts require at least two citations')
        parameters.append({**copy.deepcopy(parameter), 'sources': sources})
    dataset = next((p for p in parameters if p['name'] == 'dataset'), None)
    if not dataset or dataset['status'] != 'reported' or not strict_equal(dataset['values'], [args['dataset']]):
        raise ValueError('Protocol requires a reported dataset parameter matching the exact dataset')
    return {'version': 1, **copy.deepcopy(args), 'parameters': parameters,
        **{key: note[key] for key in ('paper_id', 'source_id', 'text_sha256')},
        'provenance': 'ai_extraction_from_verified_read_passages', 'scientific_claims_verified': False}


def record(engine, run_id, **args):
    engine.check_active(run_id)
    notes = _notes(engine, run_id)
    payload = _compile(args, notes)
    from .literature_notes import NOTE_FIELDS, validate_notes
    note = notes[args['note_id']]
    prepared = validate_notes(engine, run_id, [{k: note[k] for k in NOTE_FIELDS}],
        allowed_read_event_ids=note['source_read_event_ids'],
        handoff_id=note.get('evidence_handoff_id'),
        allowed_delivery_receipt_ids=note.get('evidence_delivery_receipt_ids'))[0]
    sha = _digest([run_id, payload])
    key = f'{run_id}:experiment-protocol:{sha}'
    value = {**payload, 'id': key, 'run_id': run_id, 'protocol_sha256': sha, 'created_at': now()}
    engine.check_active(run_id)
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        engine.store._guard_run(db, run_id)
        run = db.execute("SELECT data FROM records WHERE collection='runs' AND key=?", (run_id,)).fetchone()
        if not run or json.loads(run[0]).get('status') != 'running':
            raise ValueError('Research is no longer running')
        source = db.execute("SELECT data FROM records WHERE collection='literature_notes' AND key=?", (args['note_id'],)).fetchone()
        if not source or _digest(json.loads(source[0])) != _digest({k: v for k, v in notes[args['note_id']].items() if k != 'storage_key'}):
            raise ValueError('Reading note changed while preparing experiment protocol')
        for collection, anchor_key, expected in prepared['anchors']:
            anchor = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, anchor_key)).fetchone()
            if not anchor or _digest(json.loads(anchor[0])) != expected:
                raise ValueError('Protocol source acquisition/delivery changed before commit')
        for receipt in prepared['read_receipts']:
            event = db.execute('SELECT * FROM events WHERE run_id=? AND id=?', (run_id, receipt['id'])).fetchone()
            if not event or _digest({**dict(event), 'data': json.loads(event['data'])}) != receipt['event_sha256']:
                raise ValueError('Protocol source reading receipt changed before commit')
        from .literature_notes import _TransactionView
        from .literature_reading import _run_papers
        from .paper_identity import task_paper
        view = _TransactionView(db)
        if _digest(task_paper(view, run_id, note['paper_id'], _run_papers(view, run_id))) != prepared['paper_sha256']:
            raise ValueError('Protocol paper identity changed before commit')
        previous = db.execute("SELECT data FROM records WHERE collection='experiment_protocols' AND key=?", (key,)).fetchone()
        if previous:
            saved = json.loads(previous[0])
            if {k: v for k, v in saved.items() if k != 'created_at'} != {k: v for k, v in value.items() if k != 'created_at'}:
                raise ValueError('Immutable experiment protocol identity collision')
            return saved
        engine.store._guard_write(db, 'experiment_protocols', key, value)
        db.execute('INSERT INTO records VALUES(?,?,?,?)', ('experiment_protocols', key, json.dumps(value, ensure_ascii=False, allow_nan=False), now()))
    return value


def checked(engine, run_id, protocol_id, expected_sha256, *, notes=None):
    row = engine.store.get('experiment_protocols', protocol_id)
    if not row or row.get('id') != protocol_id or row.get('run_id') != run_id or row.get('protocol_sha256') != expected_sha256:
        raise ValueError('Experiment protocol is missing, belongs to another task or has a different SHA256')
    args = {key: copy.deepcopy(row[key]) for key in FIELDS}
    args['parameters'] = [{k: v for k, v in p.items() if k != 'sources'} for p in args['parameters']]
    payload = _compile(args, _notes(engine, run_id) if notes is None else notes)
    if _digest([run_id, payload]) != expected_sha256 or any(row.get(k) != v for k, v in payload.items()):
        raise ValueError('Experiment protocol or its source proof changed')
    return copy.deepcopy(row)


def inspect(engine, run_id, *, protocol_id=None, note_id=None, expected_sha256=None, offset=0, limit=10):
    notes = _notes(engine, run_id)
    if protocol_id and note_id:
        raise ValueError('Read either a protocol or its source note, not both')
    if note_id:
        note = notes.get(note_id)
        if not note or not expected_sha256 or note['note_sha256'] != expected_sha256:
            raise ValueError('Source note is missing or differs from expected_sha256')
        return {'note_id': note_id, **{k: copy.deepcopy(note[k]) for k in (
            'note_sha256', 'paper_id', 'source_id', 'text_sha256', 'datasets', 'excerpts', 'results', 'citations')},
            'scope': 'Previously inspected note evidence; this lookup grants no new source-reading credit.'}
    if protocol_id:
        if not expected_sha256:
            raise ValueError('Reading an experiment protocol requires its exact expected_sha256')
        return checked(engine, run_id, protocol_id, expected_sha256, notes=notes)
    records = engine.store.list('experiment_protocols', run_id)
    selected, invalid = [], []
    for row in records[offset:offset + limit]:
        try:
            valid = checked(engine, run_id, row['id'], row['protocol_sha256'], notes=notes)
            selected.append({k: valid[k] for k in ('id', 'protocol_sha256', 'dataset', 'setting', 'paper_id', 'source_id', 'text_sha256', 'parameters')})
        except (ValueError, KeyError) as exc:
            invalid.append({'id': row['id'], 'error': str(exc)})
    eligible = [n for n in notes.values() if n.get('generation') == 'ai_extraction']
    return {'protocols': selected, 'invalid': invalid, 'total': len(records),
        'reading_notes': [{'note_id': n['id'], **{k: n[k] for k in ('note_sha256', 'paper_id', 'datasets')}} for n in eligible[offset:offset + limit]],
        'notes_total': len(eligible), 'notes_next_offset': offset + limit if offset + limit < len(eligible) else None,
        'next_offset': offset + limit if offset + limit < len(records) else None,
        'scientific_claims_verified': False}


def resolve_design(engine, run_id, design):
    if 'benchmark_tasks' not in design:
        return None
    notes = _notes(engine, run_id)
    return {task['protocol_id']: checked(engine, run_id, task['protocol_id'], task['protocol_sha256'], notes=notes)
            for task in design['benchmark_tasks']}
