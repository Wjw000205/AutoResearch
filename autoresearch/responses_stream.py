"""Bounded Responses SSE transport; only explicit semantic terminals count.

Output/reasoning deltas are never accumulated, logged or returned. The terminal
response is authoritative; an EOF or [DONE] marker cannot establish completion.
"""
from __future__ import annotations

import asyncio
import codecs
import json
import re
import time

import httpx

MAX_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 20000
TERMINALS = {'response.completed': 'completed', 'response.incomplete': 'incomplete', 'response.failed': 'failed'}


def _invalid():
    return httpx.RemoteProtocolError('Responses 流缺少一致的完整终态；费用状态不确定')


def _json(text):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate key')
            value[key] = item
        return value
    try:
        result = json.loads(text, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid() from exc
    if not isinstance(result, dict):
        raise _invalid()
    return result


def _response(value, expected=None):
    if (not isinstance(value, dict) or value.get('status') not in {'completed', 'incomplete', 'failed'}
            or expected is not None and value.get('status') != expected
            or not isinstance(value.get('id'), str) or re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value['id']) is None
            or value.get('object') not in (None, 'response')
            or not isinstance(value.get('output'), list)
            or value.get('usage') is not None and not isinstance(value['usage'], dict)):
        raise _invalid()
    status = value['status']
    if (status == 'completed' and (value.get('error') is not None or value.get('incomplete_details') is not None)
            or status == 'incomplete' and (not isinstance(value.get('incomplete_details'), dict)
                                         or not isinstance(value['incomplete_details'].get('reason'), str))
            or status == 'failed' and not isinstance(value.get('error'), dict)):
        raise _invalid()
    # Keep only public text, identity, usage and structured terminal diagnostics.
    # In particular never return reasoning items, encrypted reasoning or deltas.
    clean = {key: value[key] for key in ('id', 'object', 'status', 'usage', 'error', 'incomplete_details',
                                        'request_id', 'requestId') if key in value}
    messages = []
    for item in value['output']:
        if not isinstance(item, dict):
            raise _invalid()
        if item.get('type') != 'message':
            continue
        if item.get('role') not in (None, 'assistant') or not isinstance(item.get('content'), list):
            raise _invalid()
        content = []
        for block in item['content']:
            if not isinstance(block, dict):
                raise _invalid()
            if block.get('type') == 'output_text':
                if not isinstance(block.get('text'), str):
                    raise _invalid()
                content.append({'type': 'output_text', 'text': block['text']})
        messages.append({'type': 'message', 'role': 'assistant', 'content': content})
    clean['output'] = messages
    return clean


def _event(data, named_type, response_id):
    if data.strip() == '[DONE]':
        raise _invalid()
    event = _json(data)
    kind = event.get('type')
    if not isinstance(kind, str) or named_type and named_type != kind:
        raise _invalid()
    nested = event.get('response')
    current_id = nested.get('id') if isinstance(nested, dict) else event.get('response_id')
    if current_id is not None:
        if (not isinstance(current_id, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,200}', current_id) is None
                or response_id is not None and response_id != current_id):
            raise _invalid()
        response_id = current_id
    if kind in TERMINALS:
        return _response(nested, TERMINALS[kind]), response_id
    if kind == 'error':
        error = event.get('error', event)
        if not isinstance(error, dict) or not any(isinstance(error.get(field), str) and error[field]
                                                 for field in ('code', 'message')):
            raise _invalid()
        safe = {key: error[key] for key in ('code', 'message', 'param', 'request_id', 'requestId') if key in error}
        if error.get('type') not in (None, 'error'):
            safe['type'] = error['type']
        return {'status': 'failed', 'error': safe, 'id': response_id, 'output': []}, response_id
    # Event contents (including hidden reasoning deltas) are intentionally dropped.
    return None, response_id


async def _sse(response, statistics, started):
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    buffer, data, named_type, response_id = '', [], None, None
    total = events = frame_bytes = 0
    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            statistics['bytes_received'] = total
            if total > MAX_BYTES:
                raise _invalid()
            buffer += decoder.decode(chunk)
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                line = line.removesuffix('\r')
                frame_bytes += len(line.encode('utf-8')) + 1
                if frame_bytes > MAX_EVENT_BYTES:
                    raise _invalid()
                if not line:
                    if data:
                        events += 1
                        statistics['events_received'] = events
                        if statistics.get('first_event_latency_ms') is None:
                            statistics['first_event_latency_ms'] = round((time.monotonic() - started) * 1000)
                        if events > MAX_EVENTS:
                            raise _invalid()
                        terminal, response_id = _event('\n'.join(data), named_type, response_id)
                        if response_id is not None:
                            statistics['response_id'] = response_id
                        if terminal is not None:
                            statistics.update(terminal_status=terminal['status'], terminal_received=True)
                            return terminal
                    data, named_type, frame_bytes = [], None, 0
                elif not line.startswith(':'):
                    field, separator, value = line.partition(':')
                    value = value[1:] if value.startswith(' ') else value
                    if field == 'data' and separator:
                        data.append(value)
                    elif field == 'event' and separator:
                        if named_type is not None:
                            raise _invalid()
                        named_type = value
            if len(buffer.encode('utf-8')) + frame_bytes > MAX_EVENT_BYTES:
                raise _invalid()
        decoder.decode(b'', final=True)
    except (UnicodeError, ValueError) as exc:
        raise _invalid() from exc
    # No complete terminal SSE frame was received, even if the last delta looks JSON-like.
    raise _invalid()


async def _body(response):
    chunks, size = [], 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_BYTES:
            raise _invalid()
        chunks.append(chunk)
    return b''.join(chunks)


async def post_responses(client, url, headers, body, *, total_timeout):
    """Return (closed HTTP response, safe payload); no request retries here."""
    started, response = time.monotonic(), None
    statistics = {'transport': 'responses_stream', 'first_event_latency_ms': None,
                  'terminal_received': False, 'events_received': 0, 'bytes_received': 0}
    try:
        async with asyncio.timeout(total_timeout):
            async with client.stream('POST', url, headers=headers, json=body) as response:
                statistics['headers_latency_ms'] = round((time.monotonic() - started) * 1000)
                response.extensions['autoresearch_stream'] = statistics
                if response.is_success and response.headers.get('content-type', '').split(';')[0].strip().lower() == 'text/event-stream':
                    return response, await _sse(response, statistics, started)
                raw = await _body(response)
                if response.is_success:
                    terminal = _response(_json(raw))
                    statistics.update(terminal_status=terminal['status'], terminal_received=True,
                                      response_id=terminal['id'], bytes_received=len(raw),
                                      transport='responses_stream_json_fallback')
                    return response, terminal
                # A known non-2xx HTTP response retains the existing bounded HTTP
                # retry policy, whether or not its diagnostic body is valid JSON.
                try:
                    payload = _json(raw)
                except httpx.RemoteProtocolError:
                    payload = {}
                return response, payload
    except (asyncio.CancelledError, TimeoutError, httpx.HTTPError) as exc:
        failure = httpx.ReadTimeout('Responses 流超过总时间限制；费用状态不确定') if isinstance(exc, TimeoutError) else exc
        failure.stream_metadata = dict(statistics)
        if response is not None:
            failure.stream_headers_http_status = response.status_code
        if failure is exc:
            raise
        raise failure from exc
