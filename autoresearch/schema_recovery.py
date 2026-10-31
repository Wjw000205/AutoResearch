"""One author correction for a settled, complete but schema-invalid final reply.

No unknown-charge retry, tool replay or field fabrication. Author corrections
preserve the schema. Retired rapid-code limits use a separate audited local
revalidation policy; the source authentication is shared.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

from jsonschema import Draft202012Validator

from .json_format_recovery import completed_output


def narrative_length_paths(purpose, schema, value):
    """Only concise prose may be shortened; decisions and evidence stay fixed."""
    if str(purpose).split(':', 1)[0] not in {'experiment_diagnose', 'experiment_decide', 'experiment_vote'}:
        return set()
    if not isinstance(value, dict) or 'tool_calls' in value:
        return set()
    branches = schema.get('anyOf', schema.get('oneOf', []))
    if branches:
        finals = [b for b in branches if b.get('properties') and 'tool_calls' not in b['properties']]
        outer = list(Draft202012Validator(schema).iter_errors(value))
        if len(finals) != 1 or any(e.validator not in {'anyOf', 'oneOf'} or e.path for e in outer):
            return set()
        return narrative_length_paths(purpose, finals[0], value)
    roots = {'observations', 'hypotheses', 'missing_evidence', 'rationale', 'issues',
             'hypothesis', 'verification', 'stop_condition', 'objective',
             'strategy', 'ordering_rationale', 'stopping_rule'}
    def prose(path):
        return bool(path) and (path[0] in roots or len(path) == 3 and (
            path[0] == 'resolutions' and path[2] in {'instruction', 'acceptance_check'} or
            path[0] == 'factors' and path[2] in {'rationale', 'runtime_support'}))
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if not errors or any(e.validator != 'maxLength' or not prose(tuple(e.path)) for e in errors):
        return set()
    return {tuple(e.path) for e in errors}


def constrain_prose(schema, value, paths, prefix=()):
    if prefix in paths:
        return copy.deepcopy(schema)
    if not any(p[:len(prefix)] == prefix for p in paths):
        return {'const': copy.deepcopy(value)}
    result = copy.deepcopy(schema)
    if isinstance(value, dict):
        result['properties'] = {k: constrain_prose(schema['properties'][k], v, paths, prefix+(k,)) for k,v in value.items()}
    elif isinstance(value, list):
        result.update(prefixItems=[constrain_prose(schema['items'], v, paths, prefix+(i,)) for i,v in enumerate(value)],
                      items=False, minItems=len(value), maxItems=len(value))
    return result


def rapid_check_length_errors(purpose, schema, value):
    return rapid_code_length_paths(purpose, schema, value) == {('checks',)}


def rapid_code_length_paths(purpose, schema, value):
    """Only overlong check/replacement code may change, never patch bindings."""
    if str(purpose).split(':', 1)[0] != 'rapid_implement' or not isinstance(value, dict) or 'tool_calls' in value:
        return set()
    errors = list(Draft202012Validator(schema).iter_errors(value))
    paths = set()
    for error in errors:
        path = tuple(error.path)
        if error.validator != 'maxLength' or not (path == ('checks',) or (
                len(path) == 3 and path[0] == 'edits' and type(path[1]) is int and path[2] == 'new')):
            return set()
        paths.add(path)
    return paths


def derive_source(store, intelligence, run_id, key, original, *, syntax=False, experiment_notes=False,
                  rapid_code_limits=False):
    from .engine import Harness, digest
    from .intelligence import Intelligence, ProviderError, parse_json, parse_provider_json
    if not isinstance(intelligence, Intelligence):
        return None
    inputs = store.get('handoff_inputs', key, {})
    handoff = store.get('handoffs', key, {})
    run = store.get('runs', run_id, {})
    prepared = original.get('prepared', {})
    role = prepared.get('role')
    if (run.get('mode') != 'live' or run.get('status') not in {'running', 'paused', 'blocked'}
            or store.get('run_deletions', run_id) or store.get('decisions', key)
            or original.get('status') not in {'blocked', 'dispatching'}
            or any(row.get('run_id') != run_id or row.get('id') != key for row in (inputs, handoff))
            or role != handoff.get('to_role') or handoff.get('stage') != run.get('stage')
            or key != Harness.decision_key(run_id, role, handoff.get('purpose'), inputs.get('instruction'),
                                            inputs.get('context'), inputs.get('schema'))
            or not all(field in prepared for field in ('task', 'context', 'schema'))):
        return None
    provider = intelligence.provider_for(role)
    if (provider['id'], provider['model']) != (original.get('provider_id'), original.get('model')):
        return None
    operation = intelligence._operation_identity(role, prepared['task'], prepared['context'], prepared['schema'])
    ledger = store.get('budgets', run_id, {})
    rows = [row for row in ledger.get('records', []) if row.get('operation_id') == operation]
    if len(rows) != 1 or any(row.get('operation_id') == operation for row in ledger.get('pending', {}).values()):
        return None
    row = rows[0]
    metadata, cost, cap = row.get('response_metadata'), row.get('cost_usd'), row.get('request_output_token_limit')
    if provider.get('protocol') == 'local_codex':
        from .codex_reasoning import key_for
        native_key, _ = key_for(intelligence, run_id, role, prepared['task'], prepared['context'], prepared['schema'])
        native = store.get('codex_reasoning', native_key, {})
        checkpoint = native.get('checkpoint', {})
        verified_completion = (
            native.get('reservation', {}).get('id') == row['id']
            and checkpoint.get('status') == 'completed' and checkpoint.get('reasoning_only') is True
            and checkpoint.get('auth_mode') == 'chatgpt' and not checkpoint.get('native_tool_violation')
            and not checkpoint.get('public_output_truncated')
            and all(checkpoint.get(k) and checkpoint[k] == row.get(k) for k in ('thread_id', 'turn_id'))
            and row.get('provenance') == 'local_codex_chatgpt'
            and row.get('billing_scope') == 'chatgpt_subscription' and cost is None)
    else:
        verified_completion = (type(row.get('http_status')) is int and 200 <= row['http_status'] < 300
            and type(cost) in (int, float) and math.isfinite(cost) and cost >= 0)
    if (any(row.get(field) != expected for field, expected in {
            'run_id': run_id, 'role': role, 'provider': provider['id'], 'model': provider['model']}.items())
            or row.get('status') != 'completed' or not verified_completion or not completed_output(metadata)
            or row.get('moderation_required') or not isinstance(row.get('usage'), dict)
            or type(cap) is not int or cap < 1 or store.get('usage', row['id']) != row):
        return None
    with store._read_db() as db:
        outputs = [json.loads(item[0]) for item in db.execute(
            "SELECT data FROM records WHERE collection='transcripts' AND json_extract(data,'$.run_id')=? "
            "AND json_extract(data,'$.kind')='output' AND json_extract(data,'$.request_id')=? LIMIT 2",
            (run_id, row['id']))]
    if len(outputs) != 1:
        return None
    output = outputs[0]
    text = output.get('text')
    if (any(output.get(field) != row.get(field) for field in
            ('run_id', 'role', 'provider', 'model', 'operation_id', 'response_metadata'))
            or not isinstance(text, str) or not text
            or (not rapid_code_limits and len(text) > (128_000 if syntax else 24_000))):
        return None
    if provider.get('protocol') == 'local_codex':
        from .codex_reasoning import TRANSPORT_SCHEMA, profile
        from .storage import redact
        if (native.get('schema') != prepared['schema'] or native.get('operation_id') != operation
                or native.get('selection') != profile(store, run_id, provider, prepared['context'])
                or checkpoint.get('error_code') or checkpoint.get('operation_id') != native_key
                or checkpoint.get('run_id') != run_id):
            return None
        messages = checkpoint.get('public_messages', [])
        final = [message for message in messages if message.get('phase') == 'final_answer']
        if not final:
            final = [message for message in messages if message.get('phase') is None]
        if not final or any(not isinstance(message.get('text'), str) for message in final):
            return None
        native_text = str(redact('\n'.join(message['text'] for message in final)))
        if native.get('transport_schema'):
            try:
                native_text = parse_provider_json(native_text, TRANSPORT_SCHEMA, metadata)[0]['result_json']
            except ProviderError:
                return None
        # A changed transcript cannot masquerade as a new schema defect and
        # bypass the syntax-recovery source checks or rewrite the native answer.
        if native_text != text:
            return None
    hash_omission = None
    try:
        parse_provider_json(text, prepared['schema'], metadata)
        return None  # Locally recoverable or already valid: no paid correction.
    except ProviderError as exc:
        diagnostic = str(exc)
    try:
        value = parse_json(text)
    except ProviderError:
        if not syntax:
            return None
        # Duplicate keys and nonstandard numeric values are ambiguous content,
        # not syntax repair. The strict parser must report an actual syntax error.
        if not diagnostic.startswith('Provider returned no valid JSON object:'):
            return None
        try:
            json.loads(text)
            return None
        except json.JSONDecodeError:
            pass
    else:
        if rapid_code_limits:
            from .rapid_output_length import schema_without_code_limits
            if (syntax or experiment_notes or not handoff.get('purpose', '').startswith('rapid_implement:')
                    or not isinstance(value, dict) or 'tool_calls' in value
                    or not Draft202012Validator(schema_without_code_limits(inputs['schema'])).is_valid(value)
                    or not Draft202012Validator(schema_without_code_limits(prepared['schema'])).is_valid(value)):
                return None
        elif experiment_notes:
            from .experiment_note_recovery import malformed_note_refs
            if syntax or not malformed_note_refs(value, prepared['schema']):
                return None
        elif syntax or not isinstance(value, dict) or 'tool_calls' in value:
            return None
        # A malformed hash in a same-author clarification may be an omission,
        # but only with authenticated original source reads and frozen files.
        # Different valid hashes and quantitative gates remain substantive.
        if not experiment_notes and not rapid_code_limits:
            from .rapid_review_hash_recovery import omission
            hash_omission = omission(store, run_id, key, value) if not syntax else None
            errors = list(Draft202012Validator(inputs['schema']).iter_errors(value))
            prose_paths = narrative_length_paths(handoff.get('purpose'), inputs['schema'], value)
            if prose_paths and not narrative_length_paths(handoff.get('purpose'), prepared['schema'], value):
                return None  # A later prose edit cannot change previously frozen fields.
            if not errors or (any(error.validator not in {'required', 'type', 'additionalProperties'} for error in errors)
                    and not rapid_code_length_paths(handoff.get('purpose'), inputs['schema'], value)
                    and not hash_omission
                    and not narrative_length_paths(handoff.get('purpose'), inputs['schema'], value)):
                return None
    return {'request_id': row['id'], 'transcript_id': output['id'], 'diagnostic': diagnostic,
            **({'rapid_review_hash_omission': hash_omission} if hash_omission else {}),
            'original_text': text, 'original_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'receipt_sha256': digest([row, output]), 'effective_output_limit': cap,
            'provider_identity': {field: provider.get(field) for field in ('id', 'model', 'protocol', 'base_url')}}


def correction_request(original, schema, operation, source):
    prepared = copy.deepcopy(original['prepared'])
    prepared['schema'] = copy.deepcopy(schema)
    prepared['task'] += (
        '\nSCHEMA CORRECTION: Your completed final answer failed local validation. '
        'Correct your own answer using output_schema_repair and the saved evidence/tool receipts. '
        'Return one final JSON object conforming to the original final-result schema below. '
        'Preserve supported content and all scientific judgments; supply missing fields only from real evidence. '
        'Do not invent a reading plan, citations, results or approval just to pass validation. '
        'Do not call tools or repeat completed work. This is the only schema correction for this assignment.')
    value = json.loads(source['original_text'])
    prose = narrative_length_paths(prepared['context'].get('_purpose'), schema, value)
    if prose:
        prepared['schema'] = constrain_prose(schema, value, prose)
        prepared['task'] = prepared['task'].replace('This is the only schema correction for this assignment.',
            'This step only corrects authenticated prose length; it does not repeat the original request.')
        prepared['task'] += ('\n仅精简超长说明字段，保留原判断、全部实质异议、证据限定与未知结论。'
            '其余字段、数组项目数、批准/拒绝、动作、SHA、参数与读取回执已固定，不能改变。'
            '这是表达格式补正，不是重新诊断或科研批准；直接复用原工具证据。'
            '目标控制在各字段上限的约一半，留足字符余量。可用简洁术语指向已给的契约，不必重复所有文件路径及协议常量。')
        def at(tree, path):
            for part in path: tree = tree[part]
            return tree
        prepared['task'] += '\n需精简字段及当前实际字符数：' + json.dumps(
            [{'path': list(path), 'characters': len(at(value, path))} for path in sorted(prose)], ensure_ascii=False)
    if source.get('rapid_review_hash_omission'):
        from .rapid_review_hash_recovery import constrain
        constrain(prepared, value, source)
    paths = rapid_code_length_paths(prepared['context'].get('_purpose'), schema, value)
    if paths:
        # Freeze all valid content. Each overlong replacement keeps its exact
        # original anchor, file SHA and approved change; no new edit is allowed.
        for field, content in value.items():
            if field == 'edits' and any(path[0] == 'edits' for path in paths):
                edits_schema = prepared['schema']['properties']['edits']
                items = []
                for index, edit in enumerate(content):
                    if ('edits', index, 'new') not in paths:
                        items.append({'const': edit})
                        continue
                    item = copy.deepcopy(edits_schema['items'])
                    for name, text in edit.items():
                        if name != 'new':
                            item['properties'][name] = {'const': text}
                    items.append(item)
                edits_schema.update(prefixItems=items, items=False,
                                    minItems=len(content), maxItems=len(content))
            elif (field,) not in paths:
                prepared['schema']['properties'][field] = {'const': content}
        if ('checks',) in paths:
            prepared['task'] += (
                '\n仅精简超长 checks 的格式或重复脚手架，保留全部正向和负向检查及其不变量，不能删断言来满足长度。'
                '宿主会核对每条 assert 实际成功执行：预期异常未发生时应使用 raise AssertionError，'
                '不要用永远不能成功执行的 assert False 作为计数断言。保留该失败分支。')
        edit_indices = sorted(path[1] for path in paths if path[0] == 'edits')
        if edit_indices:
            prepared['task'] += (
                f'\n仅精简 edits 中索引 {edit_indices} 的超长 new 字符串，使其满足原 maxLength；'
                '优先精简空白、注释和重复脚手架，保持已批准修改的行为、诊断覆盖、断言与异常分支。'
                '不得截断源码、删减检查、增加补丁或改变原文件、SHA、old、change_index；'
                '其他补丁与已合规字段均固定为原答复。')
        prepared['task'] += (
            '\n仅上述超长字段可补正，其余内容已固定。无需再次读取源码或执行工具；'
            '补正后仍需独立复核及真实 CPU 执行，不代表修改获批或实验通过。')
    context = prepared['context']
    context['output_schema_repair'] = {key: source[key] for key in
        ('request_id', 'transcript_id', 'diagnostic', 'original_text', 'original_sha256')}
    context['_output_token_limit'] = source['effective_output_limit']
    context['_harness_control'] = {**context.get('_harness_control', {}),
                                   'mode': 'final_only', 'operation_id': operation,
                                   'schema_correction_of': source['request_id']}
    return prepared
