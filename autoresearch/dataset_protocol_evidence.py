"""Route a dataset work-order evidence gap to Scout and bind its actual literature receipts."""
import copy
import re

from . import experiment_protocols as protocols
from .experiment_workflow import sha
from .storage import now
from .training_adjustment import obj
from . import paper_protocol_fields as paper_fields

COLLECTION = 'dataset_protocol_supplements'
FIELDS = ('dataset', 'channels', 'sampling_frequency', 'split', 'input_length', 'output_length',
          'target_columns', 'metrics', 'metric_scale')
PARAMETER = protocols.argument_schema()['properties']['parameters']['items']
EXTRACTION = obj({'note': {'type': 'string'}, 'parameters': {'type': 'array', 'minItems': 1, 'items': PARAMETER}})
RESULT = obj({'extractions': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': EXTRACTION},
              'limitations': {'type': 'array', 'items': {'type': 'string'}}})
TASK_EXTRACTION = copy.deepcopy(EXTRACTION)
TASK_EXTRACTION['properties'].update(setting={'type':'string','minLength':1,'maxLength':1200},
    reported_baselines=protocols.argument_schema()['properties']['reported_baselines'])
INSTRUCTION = '''你是本工单定向补证的 Scout。dataset_planning.protocol_task 含已核验的笔记、原文页位置与必需字段。
仅提取这篇论文中该数据集的主要多变量预测设置。不要审批或改写工程工单。直接使用提供的 bound_note 摘录索引；
只有其未覆盖的字段要按 source_windows 提示用 sources.read 读取准确原文。若需新摘录，调用 literature.record_notes
保存本任务同论文同来源的补充笔记（同时读入并摘录含数据集名的表行）。不要重复查询空的协议目录。
返回 extractions，note 填 bound 指原笔记，latest 指本交接保存的补充笔记，也可填本交接保存的 note_id。
每份提取都须含 dataset 的 reported 值及摘录索引；同一字段不要重复。所有 required_fields 都须交代。
包括时间顺序划分、输入窗口候选及预测长度集合、通道数、采样频率和评价指标。values 是原文逐字可见的标量。
target_columns 指明确的预测列名；通道数不证明单独目标列。metric_scale 指评价指标的计算尺度，不能用模型归一化步骤代替。
每条 reported 都给出对应笔记的真实 excerpts/results 零基索引。未明确报告的字段用 not_reported、空 values；
来源冲突用 conflicting、空 values 及两处真实引文。limitations 说明缺项及对应的项目侧决策。
宿主按你提交的提取逐项核对原文，绑定准确哈希并执行 literature.record_protocol。请勿手抄哈希或自行触发训练。'''


def needs_evidence(issues):
    text = ' '.join(issues).lower()
    return (any(s in text for s in ('结构化协议', '论文协议', 'literature.protocols', 'protocol_id',
                                    'paper protocol', '笔记', 'source note')) and
            any(s in text for s in ('缺', '未', '冲突', '哈希', '缺失', 'missing', 'hash', 'unverified')))


def retained_order(store, plan):
    """Only an actual completed Engineer answer from this exact planning lineage is reusable."""
    if not plan:
        return None
    rid = plan['run_id']
    previous = plan
    if 'result' not in previous:
        ref = plan.get('previous') or {}
        archive = store.get('experiment_evidence', ref.get('id', '')) or {}
        summary = archive.get('summary', {})
        if (not ref or archive.get('run_id') != rid or ref.get('sha256') != sha(summary)
                or summary.get('binding', {}).get('plan_id') != plan['id']):
            return None
        previous = summary.get('plan', {})
    owner = previous.get('author_handoff_id', '')
    result = previous.get('result')
    assignment = store.get('handoffs', owner) or {}
    decision = store.get('decisions', owner) or {}
    if (previous.get('id') != plan['id'] or previous.get('scope') != plan['scope']
            or previous.get('cycle') != plan['cycle'] or previous.get('result_sha256') != sha(result)
            or assignment.get('to_role') != 'engineer' or assignment.get('run_id') != rid
            or decision.get('result') != result or decision.get('run_id') != rid or not result):
        raise ValueError('Cannot bind supplemental evidence to the saved Engineer work order')
    return {'plan_id': plan['id'], 'author_handoff_id': owner, 'result_sha256': sha(result), 'result': result}


def windows(text, dataset):
    markers = [rf'(?im)^{re.escape(dataset)}\s+\d+', r'(?i)multivariate forecasting results averaged',
               r'(?i)look.back window size', r'(?i)chronological order',
               r'(?i)mean squared error\s*\(MSE\)', rf'(?i){re.escape(dataset)}\s*\(',
               r'(?i)standardiz(?:ed|ation)', r'(?i)target column']
    found = []
    for pattern in markers:
        match = re.search(pattern, text)
        if match and not any(s['offset'] <= match.start() < s['offset'] + s['limit'] for s in found):
            start = max(0, match.start() - 230)
            found.append({'offset': start, 'limit': min(1150, len(text) - start)})
    return found


def source_packet(engine, rid, binding):
    target, = binding['targets']
    proof, = binding['verified_sources']
    note = next((n for n in engine.datasets._notes(rid) if n['id'] == target['note_id']), None)
    if (not note or sha(note) != proof['source_note_sha256'] or target['dataset'] not in note['datasets']
            or note.get('generation') != 'ai_extraction'):
        raise ValueError('Dataset evidence no longer matches its bound reading note')
    document = engine.sources.document(rid, note['source_id'])
    if document['text_sha256'] != note['text_sha256']:
        raise ValueError('Retained paper text changed before targeted reading')
    return {'dataset': target['dataset'], 'asset': target,
            'source_note_record_sha256': proof['source_note_sha256'],
            'bound_note': {k: note[k] for k in ('id','note_sha256','paper_id','source_id','text_sha256','excerpts','results')},
            'hash_usage': 'source_note_record_sha256 hashes the archived row. Use bound_note.note_sha256 for literature.protocols and record_protocol.',
            'required_fields': list(FIELDS), 'source_windows': windows(document['text'], target['dataset'])}


def _required(packet, answer):
    if 'task_profiles' not in packet:
        return set(FIELDS)
    kind = answer.get('task_kind')
    if kind not in packet['task_profiles']:
        raise ValueError('Identify the task from its paper input, output and evaluation')
    return set(packet['required_fields']) | set(packet['task_profiles'][kind])


def compile_extractions(answer, packet, notes, allowed):
    """Validate all references before writing even a partial extraction."""
    original = packet['bound_note']
    seen = {}
    requests = []
    required = _required(packet, answer)
    for item in answer['extractions']:
        alias = item['note']
        key = original['id'] if alias == 'bound' else allowed[-1] if alias == 'latest' and allowed else alias
        if key not in [original['id'], *allowed]:
            raise ValueError('Extraction refers to a note outside this supplemental assignment')
        note = notes.get(key) or {}
        if any(note.get(k) != original[k] for k in ('paper_id', 'source_id', 'text_sha256')):
            raise ValueError('Supplemental note belongs to another paper or source version')
        args = {'note_id': key, 'note_sha256': note['note_sha256'], 'dataset': packet['dataset'],
                'setting': item.get('setting',packet['dataset'] + ' · ' + answer['task_kind']) if 'task_profiles' in packet
                    else packet['dataset'] + ' · 主要多变量预测实验', 'parameters': item['parameters']}
        if 'reported_baselines' in item:
            args['reported_baselines'] = item['reported_baselines']
        compiled = protocols._compile(args, notes)
        for p in compiled['parameters']:
            name = p['name']
            foreign = ('task_profiles' in packet and name not in required and
                       any(name in group for group in packet['task_profiles'].values()))
            if foreign or ('task_profiles' not in packet and name not in required) or (name in seen and name != 'dataset'):
                raise ValueError('Unexpected or repeated protocol field: ' + name)
            seen[name] = {'status': p['status'], 'values': p['values']}
        requests.append(args)
    if missing := required - set(seen):
        raise ValueError('Missing protocol fields: ' + ', '.join(sorted(missing)))
    return requests


async def supplement(engine, harness, rid, saved, binding, predecessor, *, issues=(), prior=None):
    from .engine import ResearchBlocked
    from .dataset_planning import _context
    import asyncio
    packet = await asyncio.to_thread(source_packet, engine, rid, binding)
    if prior:
        packet.update(verified_fields=prior['fields'], prior_protocols=prior['protocols'], missing_evidence=list(issues))
    identity = sha([saved['id'], sha(binding), predecessor['plan_id'], packet])
    key = rid + ':dataset-protocol-supplement:' + identity
    record = engine.store.get(COLLECTION, key)
    if not record:
        # Already dispatched legacy assignments above retain their exact inputs.
        packet = {**packet, 'required_fields': list(paper_fields.COMMON),
                  'task_profiles': {k:list(v) for k,v in paper_fields.BY_TASK.items()},
                  'task_requirements': engine.store.get('runs', rid).get('objective', '')}
        doc = engine.sources.document(rid, packet['bound_note']['source_id'])
        names = [re.escape(packet['dataset']), r'(?i:table\s+\d+)', r'(?i:dataset|preprocess|evaluation|experiment setup|resolution|segmentation|classification|forecast)']
        packet['source_windows'] = []
        for pattern in names:
            for match in list(re.finditer(pattern, doc['text'], re.I))[:6]:
                start = max(0, match.start()-150)
                if not any(x['offset'] <= match.start() < x['offset'] + x['limit'] for x in packet['source_windows']):
                    packet['source_windows'].append({'offset':start, 'limit':min(1400, len(doc['text'])-start)})
        identity = sha([saved['id'], sha(binding), predecessor['plan_id'], packet])
        key = rid + ':dataset-protocol-supplement:' + identity
        record = engine.store.get(COLLECTION, key)
    if not record:
        ctx = _context(engine, rid, key, {'protocol_task': packet}, 'protocol_evidence')
        ctx['dataset_planning'].update(protocol_task=packet, read_paths=['/protocol_task'])
        task = {'purpose': 'dataset_select:protocol-evidence:' + identity,
                'instruction': '定向补证，只提取当前论文和数据集的实验协议。先填写 task_kind，再覆盖 required_fields 和对应 task_profiles 的字段。'
                    '根据 bound_note 摘录、源文件和新增阅读笔记返回 extractions，note 可填 bound 或 latest。reported 绑定逐字值与摘录索引。'
                    + paper_fields.GUIDANCE,
                'context': ctx, 'schema': obj({**RESULT['properties'],
                    'extractions':{**RESULT['properties']['extractions'],'items':TASK_EXTRACTION},
                    'task_kind': {'enum':list(paper_fields.BY_TASK)}})}
        record = {'id': key, 'run_id': rid, 'setup_id': saved['id'], 'binding_sha256': sha(binding),
                  'task': task, 'created_at': now(), 'status': 'collecting'}
        engine.check_active(rid)
        record = engine.store.mutate(COLLECTION, key, lambda old: old or record)
    task = record['task']
    if record['binding_sha256'] != sha(binding) or task['context']['dataset_planning']['protocol_task'] != packet:
        raise ValueError('Frozen supplemental evidence assignment changed')
    refs = record.get('protocols')
    if not refs:
        engine.check_active(rid)
        if not record.get('dispatched_at'):
            record['dispatched_at'] = now()
            engine.store.put(COLLECTION, key, record)
            engine.store.event(rid,'dataset.protocol_evidence_requested','协议缺项已定向派发给 Scout，保留原工程工单。',
                {'id':key,'setup_id':saved['id'],'role':'scout','fields':packet['required_fields']})
        from .parameter_resolution import owner
        allowed = []
        for attempt in range(2):
            current = record['task'] if attempt == 0 else record['correction']
            answer = await harness.ask(rid, 'scout', **current)
            hid = owner(engine, rid, 'scout', {**current, 'answer': answer})
            allowed.extend(note['id'] for row in engine.store.get('handoff_progress', hid, {}).get('tool_results', [])
                           if row.get('call', {}).get('name') == 'literature.record_notes'
                           for note in row.get('result', {}).get('notes', []) if note.get('id'))
            notes = await asyncio.to_thread(protocols._notes, engine, rid)
            try:
                requests = compile_extractions(answer, packet, notes, allowed)
                break
            except ValueError as exc:
                if attempt:
                    record.update(status='needs_source_evidence', error=str(exc))
                    engine.store.put(COLLECTION, key, record)
                    raise ResearchBlocked('两份 Scout 回执均已保留，仍缺少可核验的提取：' + str(exc)) from exc
                correction = copy.deepcopy(task)
                correction['purpose'] += ':correct:' + sha(answer)
                correction['context']['dataset_planning']['correction'] = {
                    'source_handoff_id': hid, 'answer': answer, 'error': str(exc),
                    'available_notes': [{k:notes[n][k] for k in ('id','note_sha256','excerpts','results')}
                                        for n in dict.fromkeys(allowed) if n in notes]}
                correction['instruction'] += '\n仅纠正 correction 明确指出的证据/字段错误，保留原有有效读取与结论。'
                if record.get('correction', correction) != correction:
                    raise ValueError('Saved supplemental correction changed')
                record['correction'] = correction
                engine.store.put(COLLECTION, key, record)
        # The completed Scout decision is provenance. The controller owns the
        # subsequent exact write; an inactive LLM handoff grants no tool authority.
        intent = {'scout_handoff_id': hid, 'answer_sha256': sha(answer), 'requests_sha256': sha(requests)}
        if record.get('registration', intent) != intent:
            raise ValueError('Saved protocol registration intent changed')
        record.update(registration=intent, status='registering')
        engine.check_active(rid)
        engine.store.put(COLLECTION, key, record)
        refs = []
        for args in requests:
            result = await harness.execute_controller_tool(rid, 'scout', 'literature.record_protocol', args,
                purpose='register_checked_dataset_protocol:' + sha([identity, intent]))
            refs.append({'id': result['id'], 'sha256': result['protocol_sha256']})
        record.update(protocols=refs, status='recorded', scout_handoff_id=hid, limitations=answer['limitations'],
                      protocol_requests_sha256=sha(requests))
        if 'task_profiles' in packet:
            record['task_kind'] = answer['task_kind']
        engine.check_active(rid)
        engine.store.put(COLLECTION, key, record)
    notes = await asyncio.to_thread(protocols._notes, engine, rid)
    rows = [protocols.checked(engine,rid,ref['id'],ref['sha256'],notes=notes) for ref in refs]
    requests = [{**{k:row[k] for k in protocols.FIELDS if k != 'parameters'},
                 **({'reported_baselines':row['reported_baselines']} if 'reported_baselines' in row else {}),
                 'parameters': [{k:v for k,v in p.items() if k != 'sources'} for p in row['parameters']]}
                for row in rows]
    if (sha(requests) != record['protocol_requests_sha256']
            or any(row['paper_id'] != packet['bound_note']['paper_id'] or row['text_sha256'] != packet['bound_note']['text_sha256']
                   for row in rows)):
        raise ValueError('Supplemental protocol no longer matches the verified Scout extraction')
    fields = {p['name']: {'status': p['status'], 'values': p['values'], 'protocol_id': row['id']}
              for row in rows for p in row['parameters']}
    if not _required(packet, record).issubset(fields) or any(row['dataset'] != packet['dataset'] for row in rows):
        raise ValueError('Recorded supplemental protocol lost its dataset or fields')
    return {'supplement_id': key, 'protocols': refs, 'fields': fields, 'limitations': record['limitations'],
            'scout_handoff_id': record['scout_handoff_id'], 'note_id': packet['bound_note']['id'],
            'note_sha256': packet['bound_note']['note_sha256'], 'source_note_record_sha256': packet['source_note_record_sha256'],
            'scope': '论文报告值仅限对应实验；未报告项需由项目单独声明和评审。配置工单可审，训练仍受接口、资源及验证门禁。'}
