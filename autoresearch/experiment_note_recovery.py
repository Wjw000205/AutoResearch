"""One same-agent correction of malformed experiment-note receipt references.

Never guess or replace a hash. The original tool batch has not executed. Its
author gets the validation error and a bounded directory of actual saved reads;
every corrected call still passes the unchanged tool and provenance gates.
"""
from __future__ import annotations
from .prose_schema import validator as response_validator

import copy
import json
import re

from jsonschema import Draft202012Validator

from .experiment_workflow import sha
from .storage import now, uid

KEY = 'experiment_note_correction'
MARKER = 'experiment_note_correction_of'
HASH = re.compile(r'^[0-9a-f]{64}$')


def malformed_note_refs(value, schema):
    if not isinstance(value, dict) or set(value) != {'tool_calls'} or not isinstance(value['tool_calls'], list):
        return False
    candidate, found = copy.deepcopy(value), False
    for call in candidate['tool_calls']:
        if not isinstance(call, dict) or call.get('name') != 'experiments.record_notes':
            continue
        arguments = call.get('arguments', {})
        if not isinstance(arguments, dict) or not isinstance(arguments.get('notes'), list):
            return False
        for note in arguments['notes']:
            if not isinstance(note, dict) or not isinstance(note.get('receipt_sha256s'), list):
                return False
            for i, ref in enumerate(note['receipt_sha256s']):
                if not isinstance(ref, str) or not 1 <= len(ref) <= 128:
                    return False
                if not HASH.fullmatch(ref):
                    # Classification only: this candidate is never returned or
                    # persisted as a response, tool call, note or read receipt.
                    note['receipt_sha256s'][i] = sha({'invalid_ref': ref})
                    found = True
    return found and response_validator(schema).is_valid(candidate)


def read_index(progress):
    rows = []
    for row in progress.get('tool_results', []):
        call, result = row.get('call', {}), row.get('result', {})
        if (call.get('name') not in {'experiments.read','experiments.compare','experiments.analyze','experiments.inspect_module'}
                or not isinstance(result, dict) or not result.get('text') or result.get('error')):
            continue
        ref = result.get('receipt_sha256')
        if ref != sha({'call':call,'result':{k:v for k,v in result.items() if k != 'receipt_sha256'}}):
            continue
        rows.append({'receipt_sha256':ref,'tool':call['name'],
            **{k:v for k,v in call.get('arguments',{}).items() if k in {'path','section','trial_id','offset','limit','snapshot','view'}}})
    # No source text, historic transcripts or complete logs enter the request.
    return rows[-32:]


def source(h, run_id, key, original):
    from .schema_recovery import derive_source
    from .codex_reasoning import key_for, profile, TRANSPORT_SCHEMA
    from .engine import digest
    from .intelligence import parse_provider_json, ProviderError
    from .storage import redact
    try:
        proof = derive_source(h.engine.store,h.intelligence,run_id,key,original,experiment_notes=True)
        if proof is None:
            return None
        prepared = original['prepared']
        provider = h.intelligence.provider_for(prepared['role'])
        if provider.get('protocol') != 'local_codex':
            return None
        native_key, operation = key_for(h.intelligence,run_id,**prepared)
        native = h.engine.store.get('codex_reasoning',native_key,{})
        checkpoint = native.get('checkpoint',{})
        if (original.get('prepared_sha256') != digest(prepared)
                or native.get('schema') != prepared['schema'] or native.get('operation_id') != operation
                or native.get('selection') != profile(h.engine.store,run_id,provider,prepared['context'])
                or checkpoint.get('error_code') or checkpoint.get('operation_id') != native_key
                or checkpoint.get('run_id') != run_id):
            return None
        messages = checkpoint.get('public_messages',[])
        final = [m for m in messages if m.get('phase') == 'final_answer']
        if not final:
            final = [m for m in messages if m.get('phase') is None]
        if not final or any(not isinstance(m.get('text'),str) for m in final):
            return None
        text = str(redact('\n'.join(m['text'] for m in final)))
        if native.get('transport_schema'):
            text = parse_provider_json(text,TRANSPORT_SCHEMA,{'provider_status':'completed',
                'output_truncated':False,'output_limit_reached':False})[0]['result_json']
        if text != proof['original_text']:
            return None
        return {**proof,'native_sha256':digest(native)}
    except (ProviderError,KeyError,TypeError,ValueError,AttributeError):
        return None


async def schedule(h, run_id, key, progress):
    from .engine import ResearchBlocked, digest
    original = progress.get('normal_request', {})
    prepared = original.get('prepared', {})
    if (progress.get('mode') == 'final_only' or progress.get('pending_tool_calls')
            or original.get('status') != 'blocked' or original.get('response') is not None
            or original.get('turn') != progress.get('turn') or original.get('provider_id') != 'local_codex'
            or prepared.get('context',{}).get('experiment_workflow_protocol') != 1
            or not re.search(r'arguments\.notes\[\d+\]\.receipt_sha256s\[\d+\]: must satisfy pattern',original.get('error',''))):
        return False
    if progress.get(KEY):
        raise ResearchBlocked('实验笔记引用已交回原 agent 补正一次，仍未通过校验；原请求和审查进度保留。')
    h._moderation_gate(run_id)
    proof = await h.engine._read_consensus_check(run_id, source,h,run_id,key,original)
    if proof is None or original.get('prepared_sha256') != digest(prepared):
        raise ResearchBlocked('实验笔记补正缺少完整同源答复和已结算回执；未重发或改写原请求。')
    replacement, operation = copy.deepcopy(prepared), uid()
    replacement['context'].update(experiment_note_repair={
        'source_request_id':proof['request_id'],'diagnostic':proof['diagnostic'],
        'rejected_output':json.loads(proof['original_text']), 'available_reads':read_index(progress),
        'original_tool_batch_executed':False}, _output_token_limit=proof['effective_output_limit'])
    replacement['context']['_harness_control']={**replacement['context'].get('_harness_control',{}),
        'operation_id':operation, MARKER:proof['request_id']}
    replacement['task'] += ('\n实验笔记调用未执行：receipt_sha256s 格式错误。根据 experiment_note_repair 中的'
        '真实读取目录选择确切引用，原始答复仅供纠错，不是证据。不得拼接、猜测或截断哈希；'
        '目录不足时定向重读。保留已有审查判断和未决问题，修正笔记调用后继续剩余读取与评议。'
        '不要重复已成功执行的工具。本次仅允许一次引用补正，工具契约和最终审批条件不变。')
    audit={'source':proof,'original_request':copy.deepcopy(original),'operation_id':operation,
        'tool_results_sha256':sha(progress.get('tool_results',[])),'prepared_sha256':digest(replacement),'created_at':now()}
    expected=copy.deepcopy(progress)
    h.engine.check_active(run_id)
    def claim(current):
        if current != expected:
            raise ResearchBlocked('实验笔记补正进度已变化，未创建重复请求。')
        from .tool_request_recovery import prepared_successor
        current[KEY]=audit
        current['normal_request']=prepared_successor(h.engine.store,key,{
            'status':'ready','turn':progress['turn'],'operation_id':operation,'prepared':replacement,
            'provider_id':original['provider_id'],'model':original['model'],
            MARKER:proof['request_id'],'created_at':now()})
        return current
    progress.update(h.engine.store.mutate('handoff_progress',key,claim))
    h.engine.store.event(run_id,'handoff.experiment_note_correction',
        '实验笔记引用格式错误，已派回原 agent 补正；保存原回执，继续当前评审。',
        {'handoff_id':key,'source_request_id':proof['request_id'],'operation_id':operation})
    return True


async def claim(h, run_id, key, progress, preview):
    from .engine import ResearchBlocked, digest
    audit, normal = progress[KEY], progress['normal_request']
    proof = await h.engine._read_consensus_check(run_id,source,h,run_id,key,audit['original_request'])
    if (proof != audit['source'] or normal.get('status') != 'ready'
            or normal.get(MARKER) != proof['request_id'] or normal['operation_id'] != audit['operation_id']
            or digest(normal['prepared']) != audit['prepared_sha256']
            or sha(progress.get('tool_results',[])) != audit['tool_results_sha256']
            or not preview or preview['requests'][0]['max_output_tokens'] != proof['effective_output_limit']):
        raise ResearchBlocked('实验笔记补正的来源、工具进度或输出额度已变化，未发送请求。')
    expected=copy.deepcopy(progress)
    h.engine.check_active(run_id)
    def mark(current):
        if current != expected:
            raise ResearchBlocked('实验笔记补正已被领取，未重复派发。')
        current['normal_request']={**normal,'status':'dispatching','dispatched_at':now()}
        return current
    progress.update(h.engine.store.mutate('handoff_progress',key,mark))
