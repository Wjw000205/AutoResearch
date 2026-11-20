"""Authenticated source-quote views and bounded same-role citation correction.

Raw provider answers are immutable. Formatting reconciliation only resolves a
unique contiguous Python fragment with identical non-indent text and AST. It
does not certify the accompanying interpretation or any execution behavior.
"""
import ast
import copy
import io
import textwrap
import tokenize

from . import bounded_evidence_review as packets
from .experiment_workflow import sha
from .parameter_resolution import owner
from .training_adjustment import obj

METHOD = 'unique_contiguous_indent_and_python_ast_v1'
INSTRUCTION = '''这是你原源码观察的定向引用补正，不是重新审查整个候选。
只核对给定这一段已冻结源码。选择能支持你的观察的连续行号，宿主会直接提取原文，不要重新抄写或拼接quote。
若原finding不受原文支持，修正本条finding/kind，并在change_reason明确解释撤回或修正；跨文件或运行证据不足应保留evidence_incomplete。
其他观察不变，不新增批准、不声称测试已运行。只返回给定schema，不调用工具、不改代码、不运行训练。
start_line/end_line是numbered_source中的相对行号，必须start<=end，选择非空原文。'''


def structure(req):
    from .prose_schema import validate
    answer = req['answer']; packet = req['context'][packets.KEY]
    validate(answer, req['schema'])
    echo = answer.get('packet_sha256', packet['packet_sha256'] if packet.get('receipt_bound_read') == 1 else None)
    if (packet.get('phase') != 'read' or echo != packet['packet_sha256']
            or packet.get('receipt_bound_read') == 1 and req['purpose'] != 'rapid_review:host-repair:' + echo):
        raise ValueError('Source observation is detached from its reading packet')
    if sha({k:v for k,v in packet.items() if k != 'packet_sha256'}) != packet['packet_sha256']:
        raise ValueError('Frozen source packet changed')
    sections = {s['id']:s for s in packet['payload']['sections']}
    rows = answer['observations']
    if len(rows) != len(sections) or {o['section_id'] for o in rows} != set(sections):
        raise ValueError('Observation coverage or section identity is invalid')
    return sections


def same_ast(left, right):
    trees = []
    for text in (left, right):
        text = textwrap.dedent(text)
        try:
            # Dedenting a multiline literal can change its value. Never treat
            # such differences as harmless formatting, even if ASTs look equal.
            if any(t.type == tokenize.STRING and t.start[0] != t.end[0]
                   for t in tokenize.generate_tokens(io.StringIO(text).readline)):
                return False
            tree = ast.parse(text)
            if not tree.body:
                return False
            trees.append(ast.dump(tree, include_attributes=False))
        except (SyntaxError, ValueError, tokenize.TokenError, IndentationError, RecursionError):
            return False
    return trees[0] == trees[1]


def indentation_match(section, quote):
    if not section['path'].endswith('.py') or not quote.strip():
        return None
    needle = quote.splitlines()
    if len(needle) < 2:
        return None
    lines = section['text'].splitlines(keepends=True)
    normalized = [line.lstrip(' \t') for line in needle]
    matches = []
    for start in range(len(lines)-len(needle)+1):
        candidate = lines[start:start+len(needle)]
        if [line.rstrip('\r\n').lstrip(' \t') for line in candidate] == normalized:
            matches.append((start, ''.join(candidate)))
    if len(matches) != 1 or not same_ast(quote, matches[0][1]):
        return None
    start, exact = matches[0]
    offset = sum(map(len, lines[:start]))
    return {'method': METHOD, 'original_quote': quote, 'exact_quote': exact,
            'section_id': section['id'], 'source_path': section['path'], 'source_sha256': section['sha256'],
            'start_line_in_section': start+1, 'end_line_in_section': start+len(needle),
            'source_offset': section['offset']+offset, 'source_end': section['offset']+offset+len(exact),
            'finding_changed': False, 'scope': 'Citation formatting only; no new review, approval or behavior evidence'}


def correction_request(req, index, section, role):
    from .host_review_compaction import core
    original_packet = req['context'][packets.KEY]
    lines = section['text'].splitlines(keepends=True)
    body = {'version': 1, 'phase': 'read', 'compact_review_protocol': 1,
            'host_repair_id': original_packet['host_repair_id'], 'role': role,
            'candidate_sha256': original_packet['candidate_sha256'],
            'payload': {'citation_correction': 1, 'parent_packet_sha256': original_packet['packet_sha256'],
                'original_answer_sha256': sha(req['answer']), 'observation_index': index,
                'original_observation': copy.deepcopy(req['answer']['observations'][index]),
                'source': {k:section[k] for k in ('id','path','sha256','offset','end','total_chars')},
                'numbered_source': '\n'.join(f'{i}: {line.rstrip(chr(13)+chr(10))}' for i,line in enumerate(lines,1)),
                'review_scope': core(original_packet['payload'].get('review_scope', {}))}}
    body['packet_sha256'] = sha(body)
    schema = obj({'packet_sha256': {'const': body['packet_sha256']},
        'start_line': {'type':'integer','minimum':1,'maximum':len(lines)},
        'end_line': {'type':'integer','minimum':1,'maximum':len(lines)},
        'finding': packets.TEXT, 'kind': {'enum':['observation','code_defect','evidence_incomplete']},
        'change_reason': packets.TEXT})
    return {'purpose':'rapid_review:host-repair:quote-correction:'+body['packet_sha256'],
            'instruction':INSTRUCTION, 'context':{'training_context_protocol':1, packets.KEY:body}, 'schema':schema}


def resolve(req):
    """Produce a derived view; never mutate the raw answer or claim authorship."""
    from .prose_schema import validate
    sections = structure(req)
    view = copy.deepcopy(req); audits = []; unresolved = []
    view['answer'].setdefault('packet_sha256', req['context'][packets.KEY]['packet_sha256'])
    for index, observation in enumerate(req['answer']['observations']):
        section = sections[observation['section_id']]
        quote = observation['quote']
        if quote.strip() and quote in section['text']:
            continue
        change = indentation_match(section, quote)
        if change is not None:
            view['answer']['observations'][index]['quote'] = change['exact_quote']
            audits.append({'observation_index':index, **change})
            continue
        correction = req.get('quote_corrections', {}).get(str(index))
        if not correction or 'answer' not in correction:
            unresolved.append((index, section)); continue
        role = req['context'][packets.KEY]['role']
        expected = correction_request(req, index, section, role)
        if any(correction[k] != v for k,v in expected.items()):
            raise ValueError('Quote correction lost its parent/source binding')
        answer = correction['answer']; validate(answer, expected['schema'])
        lines = section['text'].splitlines(keepends=True)
        first, last = answer['start_line'], answer['end_line']
        exact = ''.join(lines[first-1:last])
        if first > last or not exact.strip():
            raise ValueError('Quote correction selected an invalid or blank range')
        view['answer']['observations'][index] = {
            'section_id':observation['section_id'], 'quote':exact, 'finding':answer['finding'], 'kind':answer['kind']}
        offset = sum(map(len, lines[:first-1]))
        audits.append({'observation_index':index, 'method':'same_role_numbered_source_selection_v1',
            'original_quote':quote, 'exact_quote':exact, 'section_id':section['id'],
            'source_path':section['path'], 'source_sha256':section['sha256'],
            'source_offset':section['offset']+offset, 'source_end':section['offset']+offset+len(exact),
            'finding_changed':answer['finding']!=observation['finding'] or answer['kind']!=observation['kind'],
            'change_reason':answer['change_reason'], 'correction_answer_sha256':sha(answer)})
    if not unresolved:
        packets.validate_answer(view)
    audit = {'version':1, 'packet_sha256':view['answer']['packet_sha256'],
             'original_answer_sha256':sha(req['answer']), 'entries':audits, 'approval_granted':False}
    return view, audit, unresolved


def validated(engine, run_id, role, req):
    if req['context'][packets.KEY].get('role') != role:
        raise ValueError('Source citation belongs to another role')
    hid = owner(engine, run_id, role, req)
    view, audit, unresolved = resolve(req)
    if unresolved:
        raise ValueError('Source citation still requires same-role correction')
    correction_ids = []
    for index, child in req.get('quote_corrections', {}).items():
        if 'answer' in child:
            correction_ids.append(owner(engine, run_id, role, child))
    if req.get('quote_reconciliation') is not None and req['quote_reconciliation'] != audit:
        raise ValueError('Saved citation reconciliation changed')
    return view, hid, correction_ids


def annotations(req):
    view, audit, unresolved = resolve(req)
    if unresolved:
        raise ValueError('Unresolved quote cannot support final judgment')
    rows = packets.annotated(view)
    by_index = {r['observation_index']:r for r in audit['entries']}
    for index, row in enumerate(rows):
        if index in by_index:
            item = by_index[index]
            row['citation_provenance'] = {k:item[k] for k in ('method','source_offset','source_end','finding_changed')}
            row['citation_provenance']['original_quote_sha256'] = sha(item['original_quote'])
            if item['finding_changed']:
                old = req['answer']['observations'][index]
                row['citation_provenance'].update(superseded_finding=old['finding'], superseded_kind=old['kind'],
                                                change_reason=item['change_reason'])
    return rows


async def ensure(engine, harness, record, role, req, save):
    from .engine import ResearchBlocked
    # Authenticate before any reconciliation, including the zero-call path.
    hid = owner(engine, record['run_id'], role, req)
    if req['context'][packets.KEY].get('role') != role:
        raise ValueError('Quote reconciliation cannot cross roles')
    try:
        _, _, unresolved = resolve(req)
    except ValueError as exc:
        raise ResearchBlocked('源码引用核验未通过；保留原答复和已有补正，不重复派发：'+str(exc)) from exc
    for index, section in unresolved:
        corrections = req.setdefault('quote_corrections', {})
        child = corrections.setdefault(str(index), correction_request(req, index, section, role))
        save()
        if 'answer' not in child:
            child['answer'] = await harness.ask(record['run_id'],role,child['purpose'],child['instruction'],child['context'],child['schema'])
            save()
        owner(engine, record['run_id'], role, child)
    try:
        _, audit, unresolved = resolve(req)
        if unresolved:
            raise ValueError('No supported citation returned')
    except ValueError as exc:
        raise ResearchBlocked('原角色的定向引用补正未通过；保留原答复和补正回执，不重复派发：'+str(exc)) from exc
    if audit['entries'] and req.get('quote_reconciliation') is None:
        req['quote_reconciliation'] = audit; save()
        engine.store.event(record['run_id'],'review.quote_reconciled',
            '已建立可追溯的精确原文引用视图；原模型答复保留，不授予新批准。',
            {'handoff_id':hid,'role':role,'methods':[r['method'] for r in audit['entries']]})
    validated(engine, record['run_id'], role, req)
    return hid
