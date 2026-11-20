"""Bounded, assignment-bound dependency reads. Reading never transfers ownership.

The original request and its whitelist remain immutable. A supplemental tool
receipt records its own exact request, source version and delivered bytes.
Consumers may derive a normal read view, but never rewrite the original receipt.
"""
from __future__ import annotations

import copy

from .implementation_review_routing import digest

TOOL = 'materials.request_read'
PROTOCOL = 'supplemental_read_protocol'
VERSION = 1
MARKER = 'supplemental_read'
SOURCE_KINDS = {'architecture_design', 'approved_proposal', 'frozen_experiment_plan',
                'host_implementation_contract', 'experiment_materials', 'experiment_source_protocol'}
GUIDANCE = ('\n按需补读：需要核对本任务目录中尚未授权的原文或代码依赖时，使用 materials.request_read。'
    '出现 UNKNOWN 时，应先定位缺口并自动调用工具补读可取得的决定性原文，再独立复核；'
    '不得把尚未读取或原分派范围不足本身当作终止理由。'
    '声明原验收项和具体用途，指定精确版本与单页范围；宿主核验目录及任务身份后返回真实原文。'
    '补读不新增任务、不转移文件归口、不改变科学标准，也不表示批准或执行许可。'
    '先使用已有同版本原文，缺多少补多少；目录之外的资源必须如实报告缺失。')


def enabled(context):
    value = context.get(PROTOCOL)
    if value is not None and (type(value) is not int or value != VERSION):
        raise ValueError('Unsupported supplemental read protocol')
    return value == VERSION


def schema():
    text = {'type': 'string', 'minLength': 1, 'maxLength': 1500, 'pattern': r'\S'}
    binding = {'expected_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
               'offset': {'type': 'integer', 'minimum': 0},
               'limit': {'type': 'integer', 'minimum': 1, 'maximum': 6000},
               'reason': text, 'requirement_id': text}
    def branch(kind, fields):
        props = {**binding, 'kind': {'const': kind}, **{f: text for f in fields}}
        return {'type': 'object', 'additionalProperties': False, 'properties': props,
                'required': list(props)}
    return {'oneOf': [branch('document', ['document_id', 'section_id']), branch('code', ['id'])]}


def opt_in(context):
    value = copy.deepcopy(context)
    value[PROTOCOL] = VERSION
    return value


def available(context):
    """Unscoped catalogs already authorize these reads through normal readers.

    This compatibility adapter adds no source outside their original catalog.
    Scoped document/code assignments require their explicit versioned opt-in.
    """
    if enabled(context):
        return True
    return (not context.get('implementation_document_review_scope') and not context.get('code_review_assignment')
            and bool(context.get('indexed_documents') or context.get('code_repair_tool_review') == 1
                     or context.get('definition_code_sources')))


def bounded_output(context, call, output):
    if isinstance(output, dict) and output.get('error'):
        return output
    args = call.get('arguments', {})
    _arguments(args)
    marker = output.get(MARKER, {}) if isinstance(output, dict) else {}
    if (not available(context) or marker.get('version') != VERSION or marker.get('reading_only') is not True
            or marker.get('request_sha256') != digest(args)
            or not isinstance(output.get('text'), str) or len(output['text']) != args['limit']):
        raise ValueError('Supplemental read delivery lost its bounded request receipt')
    return output


def _bound(engine, run_id, role, hid):
    frozen = engine.store.get('handoff_inputs', hid, {})
    hand = engine.store.get('handoffs', hid, {})
    payload = {k: frozen.get(k) for k in ('instruction', 'context', 'schema')}
    from .engine import Harness
    if (frozen.get('id') != hid or frozen.get('run_id') != run_id
            or frozen.get('role', role) != role or frozen.get('purpose', hand.get('purpose')) != hand.get('purpose')
            or hand.get('id') != hid or hand.get('run_id') != run_id or hand.get('to_role') != role
            or hand.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, role, hand.get('purpose'), **payload) != hid
            or not available(frozen.get('context', {}))):
        raise ValueError('Supplemental read requires the exact frozen role assignment')
    return frozen['context'], hand['input_hash']


def _arguments(args):
    from .tool_contracts import StrictToolValidator
    if not StrictToolValidator(schema()).is_valid(args):
        raise ValueError('Supplemental read requires an exact bounded source and purpose')
    return {k: v for k, v in args.items() if k not in {'kind', 'reason', 'requirement_id'}}


def _page(engine, run_id, role, hid, context, args):
    call_args = _arguments(args)
    scope = context.get('implementation_acceptance', {})
    partition = context.get('code_review_assignment', {})
    duties = scope.get('assigned_criteria', []) if scope else list(partition.get('checks', {}))
    if duties and args['requirement_id'] not in duties:
        raise ValueError('Supplemental read changed the assigned requirement')
    if args['kind'] == 'document':
        matches = [d for d in context.get('indexed_documents', [])
                   if (d['document_id'], d['sha256']) == (args['document_id'], args['expected_sha256'])]
        if not matches or any(d != matches[0] for d in matches):
            raise ValueError('Supplemental document is outside the frozen catalog')
        doc = matches[0]
        current = scope.get('document') or context.get('implementation_document', {})
        if scope and doc.get('kind') not in SOURCE_KINDS and (
                doc['document_id'], doc['sha256']) != (current.get('document_id'), current.get('sha256')):
            raise ValueError('Supplemental source is not an approved input or the current document')
        section = next((s for s in doc['sections'] if s['id'] == args['section_id']), None)
        if section is None or not 0 <= args['offset'] < args['offset'] + args['limit'] <= section['chars']:
            raise ValueError('Supplemental document range exceeds its frozen section')
        from .document_index import load, read
        manifest, _ = load(engine, run_id, doc['document_id'], doc['sha256'])
        if manifest['sections'] != doc['sections']:
            raise ValueError('Supplemental document index changed')
        return {'name': 'documents.read', 'arguments': call_args}, read(engine, run_id, call_args)
    from . import code_navigation, code_review_partition as partitions
    if partition:
        batch = partitions.authenticate(engine, run_id, role, hid, context)
        files = [s for a in batch['assignments'].values() for s in a['context']['delivery_artifacts']]
        catalog = context.get('supplemental_code_sources', [])
        if any(s not in files for s in catalog):
            raise ValueError('Supplemental code catalog differs from the frozen delivery')
    else:
        code_navigation.frozen(engine, run_id, role, hid)
        catalog = list(code_navigation.sources(engine, run_id, context).values())
    matches = [s for s in catalog if (s['artifact_id'], s['sha256']) == (args['id'], args['expected_sha256'])]
    if not matches or any(s != matches[0] for s in matches):
        raise ValueError('Supplemental code is outside the frozen delivery')
    source = matches[0]
    text = code_navigation.body(engine, run_id, context, source)
    start, end = args['offset'], args['offset'] + args['limit']
    if not 0 <= start < end <= len(text):
        raise ValueError('Supplemental code range exceeds its frozen file')
    page = {'artifact_id': source['artifact_id'], 'sha256': source['sha256'], 'path': source['path'],
            'text': text[start:end], 'total_chars': len(text), 'next_offset': end if end < len(text) else None,
            'truncated': end < len(text), 'untrusted': True, 'dependency_only': True,
            **code_navigation.coordinates(text, start, end)}
    return {'name': 'code.read', 'arguments': call_args}, page


def execute(engine, run_id, role, hid, args):
    context, identity = _bound(engine, run_id, role, hid)
    call, page = _page(engine, run_id, role, hid, context, args)
    return {**page, MARKER: {'version': VERSION, 'run_id': run_id, 'role': role,
        'handoff_id': hid, 'input_sha256': identity, 'request_sha256': digest(args),
        'call': call, 'reading_only': True}}


def normal_read(engine, run_id, hid, receipt):
    """Authenticate a derived view; the original invocation remains untouched."""
    if receipt.get('call', {}).get('name') != TOOL:
        return receipt
    hand = engine.store.get('handoffs', hid, {})
    expected = execute(engine, run_id, hand.get('to_role'), hid, receipt['call']['arguments'])
    if receipt.get('result') != expected or receipt.get('predecessor_handoff_id'):
        raise ValueError('Supplemental read receipt identity, version or bytes changed')
    return {'call': expected[MARKER]['call'], 'result': {k: v for k, v in expected.items() if k != MARKER}}


def permits_evidence(engine, run_id, hid, context, ref, *, report_evidence=False):
    if not enabled(context):
        return False
    spans = []
    for receipt in engine.store.get('handoff_progress', hid, {}).get('tool_results', []):
        if receipt.get('call', {}).get('name') != TOOL or receipt.get('result', {}).get('error'):
            continue
        view = normal_read(engine, run_id, hid, receipt)
        args = view['call']['arguments']
        if view['call']['name'] == 'documents.read' and all(args.get(a) == ref.get(b) for a, b in
                [('document_id', 'document_id'), ('expected_sha256', 'document_sha256'), ('section_id', 'section_id')]):
            spans.append((args['offset'], args['offset'] + args['limit'], hid))
    if spans and report_evidence:
        from .implementation_document_review_scope import (KEY, wire_enabled, read_ranges,
                                                         authenticate_read_authority, _permitted_range)
        if wire_enabled(context) and context[KEY]['wire_version'] == 3:
            authenticate_read_authority(context)
            # A report may join adjacent frozen and independently authenticated
            # supplemental windows. Tool grants and dependency proofs retain
            # their existing contracts. Retrieval/delivery is checked separately.
            args = {'document_id': ref['document_id'], 'expected_sha256': ref['document_sha256'],
                    'section_id': ref['section_id'], 'offset': ref['offset'], 'limit': ref['limit']}
            identity = (ref['document_id'], ref['document_sha256'], ref['section_id'])
            if not _permitted_range([(*identity, 0, ref['offset'] + ref['limit'])], args):
                return False
            spans.extend((start, end, hid) for did, sha, sid, start, end in read_ranges(context)
                         if (did, sha, sid) == identity)
    from .document_read_window import _uncovered
    return bool(spans) and not _uncovered(ref['offset'], ref['offset'] + ref['limit'], sorted(spans))
