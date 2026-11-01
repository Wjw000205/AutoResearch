"""Versioned parameter contracts for the host-executed research tools.

These schemas describe requests, not authorization or successful execution.
The harness retains its role, frozen-assignment, provenance and execution gates.
No provider text is evaluated, and validation diagnostics omit argument values.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import ipaddress
from datetime import datetime, timezone
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import best_match


VERSION = 1
_CONDITION_MARK = 'autoresearch.tool_contracts.v1:'


def _object(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {},
            'required': list(required), 'additionalProperties': False}


def _text(maximum=None, *, minimum=1, nonblank=False):
    result = {'type': 'string', 'minLength': minimum}
    if maximum is not None:
        result['maxLength'] = maximum
    if nonblank:
        result['pattern'] = r'\S'
    return result


def _integer(minimum=0, maximum=None):
    result = {'type': 'integer', 'minimum': minimum}
    if maximum is not None:
        result['maximum'] = maximum
    return result


def _array(items, minimum=0, maximum=None, *, unique=False):
    result = {'type': 'array', 'items': items, 'minItems': minimum}
    if maximum is not None:
        result['maxItems'] = maximum
    if unique:
        result['uniqueItems'] = True
    return result


def _nullable(schema):
    return {'anyOf': [schema, {'type': 'null'}]}


_ID = _text()
_HASH = {'type': 'string', 'pattern': '^[0-9a-fA-F]{64}$'}
_LOWER_HASH = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
_BOOL = {'type': 'boolean'}
_NUMBER = {'type': 'number', 'minimum': 0}
_POINTER = {'type': 'string', 'pattern': '^/'}
_CROP = _array({'type': 'number', 'minimum': 0, 'maximum': 1}, 4, 4)


def _paged(properties, required, maximum=16000, *, continuation_hash=None):
    result = _object({**properties, 'offset': _integer(), 'limit': _integer(1, maximum)}, required)
    if continuation_hash:
        result['allOf'] = [{
            'if': {'properties': {'offset': {'type': 'integer', 'minimum': 1}}, 'required': ['offset']},
            'then': {'required': [continuation_hash],
                     'properties': {continuation_hash: _HASH}}}]
    return result


def _resource_plan():
    # assess_resources merges plan.resources over the flat plan. Keep every
    # supported estimator alias, while expressing complete admission fields on
    # the merged object rather than requiring them in one physical location.
    props = {name: copy.deepcopy(_NUMBER) for name in (
        'ram_gb', 'memory_gb', 'disk_gb', 'estimated_vram_gb', 'vram_gb',
        'parameter_count', 'model_parameters', 'model_parameters_billions',
        'context_length', 'batch_size')}
    props.update({
        'gpu_required': _BOOL, 'requires_gpu': _BOOL, 'is_llm': _BOOL,
        'required_packages': _array(_text(minimum=0)),
        'framework': _text(minimum=0), 'model_name': _text(minimum=0), 'model': _text(minimum=0),
        'precision_bits': {'type': 'number', 'enum': [4, 8, 16, 32, 64]},
        'training_regime': {'anyOf': [_text(minimum=0), _BOOL]},
        'training': {'anyOf': [_text(minimum=0), _BOOL]},
        'cpu_cores': _integer(1), 'timeout_seconds': {'type': 'number', 'minimum': 1, 'maximum': 86400},
        'timeout': {'type': 'number', 'minimum': 1, 'maximum': 86400}, 'cpu_seconds': _integer(1),
    })
    result = _object({**props, 'resources': _object(copy.deepcopy(props))})
    required = ('ram_gb', 'disk_gb', 'gpu_required', 'required_packages',
                'cpu_cores', 'timeout_seconds', 'cpu_seconds')
    result['allOf'] = [{'anyOf': [
        {'required': [field]},
        {'required': ['resources'], 'properties': {'resources': {'required': [field]}}}
    ]} for field in required]
    return result


def _paper():
    # verify accepts identifier-only inputs and both raw papers and the compact
    # metadata records produced by the application's scholarly retrievers.
    nullable_text = _nullable(_text(minimum=0))
    strings = _array(_text(minimum=0))
    venue = _object({name: nullable_text for name in (
        'kind', 'publication_type', 'source_id', 'name', 'journal', 'conference')}
        | {'names': strings, 'issn': strings})
    verification = _object({name: nullable_text for name in (
        'status', 'method', 'evidence_url', 'canonical_title', 'note', 'reason', 'metadata_sha256')}
        | {name: _BOOL for name in ('title_match', 'identifier_match', 'author_match', 'year_match')}
        | {'canonical_year': _nullable({'type': 'integer'}), 'errors': strings,
           'venue_metadata': _nullable(venue)})
    fulltext = _object({name: nullable_text for name in ('url', 'source', 'access', 'format', 'evidence')})
    ccf = _object({name: nullable_text for name in (
        'catalog_version', 'source_url', 'rank', 'venue_name', 'abbr', 'kind', 'reason', 'domain')}
        | {'catalog_page': {'type': ['integer', 'string', 'null']}, 'eligible': _BOOL,
           'domain_catalog_page': {'type': ['integer', 'string', 'null']},
           'scientific_quality_verified': _BOOL})
    props = {name: nullable_text for name in (
        'id', 'paper_id', 'title', 'doi', 'arxiv_id', 'url', 'fulltext_url', 'pmid', 'pmcid',
        'semantic_scholar_id', 'journal', 'source', 'abstract', 'abstract_excerpt',
        'publication_date', 'oa_pdf_url', 'fulltext_access', 'checked_at', 'provenance',
        'retrieval_origin', 'rag_entry_id', 'rag_version_id', 'rag_analysis_at',
        'canonical_paper_id', 'scope', 'rag_read_hint', 'rag_subject_query', 'dblp_key', 'openalex_id')}
    ranking_values = _object({key: {'type': 'number'} for key in (
        'relevance', 'query_coverage', 'recency', 'influence', 'source_quality', 'fulltext')})
    quotation = _object({'locator': _text(), 'quote': _text()}, ['locator', 'quote'])
    measurement = _object({key: _text(minimum=0) for key in (
        'dataset', 'metric', 'value', 'setting', 'locator', 'quote')})
    aspects = _object({name: _object({
        'fields': _object({key: nullable_text for key in ('method', 'research_question', 'summary')}
            | {'baselines': strings, 'results': _array(measurement)}),
        'excerpts': _array(quotation),
        **{key: nullable_text for key in ('note_id', 'text_sha256', 'origin_run_id',
                                        'analysis_created_at', 'basis', 'provenance')},
        'scientific_claims_verified': _BOOL, 'novelty_verified': _BOOL,
    }) for name in ('architecture', 'contributions', 'baselines')})
    props.update({'authors': _nullable(strings), 'sources': strings,
        'year': {'type': ['integer', 'string', 'null']},
        'citation_count': _nullable(_integer()), 'relevance_score': {'type': 'number'},
        'verified': _BOOL, 'cache_hit': _BOOL, 'is_oa': _nullable(_BOOL),
        'verification': verification, 'venue': _nullable(venue), 'ccf': ccf,
        'fulltext_locations': _array(fulltext),
        'identity_aliases': strings,
        'matched_queries': _array({'type': ['string', 'integer']}),
        'publication_types': strings,
        'metadata_conflicts': _object({'publication_years': _array({'type': 'integer'})}),
        'retrieval_relations': _array(_object({'seed_paper_id': _text(), 'relation': _text()},
                                             ['seed_paper_id', 'relation'])),
        'ranking': _object({'mode': _text(), 'components': ranking_values, 'weights': ranking_values,
                            'relevance_method': _text(), 'scientific_quality_verified': _BOOL}),
        'relevance': _object({'query_terms': strings, 'matched_terms': strings,
                              'accepted': _BOOL, 'note': _text(minimum=0),
                              'fraction': {'type': 'number'}}),
        'rag_score': {'type': 'number'}, 'rag_matched_aspects': strings, 'rag_evidence': aspects,
        'rag_manual_supplement_count': _integer(), 'rag_analysis_version_count': _integer(),
        'rag_source_version_count': _integer(), 'rag_identifiers': strings})
    result = _object(props)
    result['anyOf'] = [{'required': [name], 'properties': {name: _text()}}
                       for name in ('id', 'title', 'doi', 'arxiv_id', 'url', 'pmid', 'semantic_scholar_id')]
    return result


def _notes():
    # Citation handles are expanded locally by resolve_note_citations before
    # the unchanged quotation/read-receipt proof gates run.
    prose = {'type': 'string', 'minLength': 1, 'maxLength': 2400,
             'pattern': r'[\u3400-\u9fff]'}
    row_text = _text(1200, nonblank=True)
    row_prose = {'type': 'string', 'minLength': 1, 'maxLength': 1200,
                 'pattern': r'[\u3400-\u9fff]'}
    original = {'locator': row_text, 'quote': _text(3200, nonblank=True)}
    measures = {'dataset': row_text, 'metric': row_text, 'value': row_text, 'setting': row_prose}
    excerpts = {'oneOf': [_object(original, original),
                          _object({'citation_id': _text()}, ['citation_id'])]}
    results = {'oneOf': [_object({**measures, **original}, [*measures, *original]),
                         _object({**measures, 'citation_id': _text()}, [*measures, 'citation_id'])]}
    props = {'paper_id': _text(500, nonblank=True), 'source_id': _text(500, nonblank=True),
             'text_sha256': _LOWER_HASH,
             **{name: copy.deepcopy(prose) for name in ('summary', 'research_question', 'method', 'relevance')},
             **{name: _array(row_text, maximum=32) for name in ('datasets', 'baselines')},
             **{name: _array(row_prose, maximum=32) for name in ('limitations', 'missing_information')},
             'results': _array(results, maximum=12), 'excerpts': _array(excerpts, 1, 12)}
    note = _object(props, props)
    note['allOf'] = [{'if': {'properties': {'results': {'maxItems': 0}}, 'required': ['results']},
                      'then': {'properties': {'missing_information': {'minItems': 1}}}}]
    return _object({'notes': _array(note, 1, 6)}, ['notes'])


def _catalog():
    from .experiment_protocols import argument_schema as protocol_schema
    search = _object({'query': _text(minimum=0), 'sort': {'enum': ['relevance', 'recent']},
        'year_from': _nullable(_integer(1800)), 'year_to': _nullable(_integer(1800))}, ['query'])
    source_page = _paged({'source_id': _ID, 'expected_sha256': _nullable(_HASH)},
                         ['source_id'], continuation_hash='expected_sha256')
    page_binding = {'source_id': _ID, 'expected_sha256': _HASH}
    document_binding = {'document_id': _ID, 'expected_sha256': _HASH}
    media = {**page_binding, 'page': _integer(1), 'crop': _CROP}
    paper = _paper()
    schemas = {
        'hardware.inspect': _object(),
        'resources.assess': _object({'plan': _resource_plan()}, ['plan']),
        'memory.search': _object({'query': _text(minimum=0), 'limit': _nullable(_integer(1))}, ['query']),
        'experiments.history': _object({'scope': {'enum': ['all', 'current']}, 'cycle': _nullable(_integer(1))}),
        'experiments.status': _object({'offset': _integer(0), 'limit': _integer(1, 25), 'expected_sha256': _HASH}),
        'experiments.cleanup': _object({'action': {'enum': ['preview', 'delete']},
            'trial_id': _text(120), 'preview_id': _ID, 'expected_sha256': _HASH,
            'file_ids': _array(_HASH, 1, 20, unique=True)}, ['action', 'trial_id']),
        'experiments.read': _object({'trial_id': _ID, 'expected_sha256': _HASH,
            'section': {'enum': ['manifest', 'params', 'metrics', 'identity', 'diagnostic', 'resources']},
            'offset': _integer(0), 'limit': _integer(1, 6000)}, ['trial_id']),
        'experiments.compare': _object({'evidence_id': _ID, 'expected_sha256': _HASH,
            'path': _text(200, minimum=0), 'offset': _integer(0), 'limit': _integer(1, 6000)},
            ['evidence_id', 'expected_sha256']),
        'experiments.record_notes': _object({'notes': _array(_object({
            'key': _text(80), 'statement': _text(600),
            'receipt_sha256s': _array(_HASH, 1, 8, unique=True)}, ['key', 'statement', 'receipt_sha256s']), 1, 4)}, ['notes']),
        'experiments.analyze': _object({'evidence_id': _ID, 'expected_sha256': _HASH,
            'kind': {'enum': ['comparison', 'ablation', 'training', 'integrity']},
            'unit_id': _ID, 'reference_unit_id': _ID, 'trial_id': _ID,
            'offset': _integer(0), 'limit': _integer(1, 6000)}, ['evidence_id', 'expected_sha256', 'kind']),
        'experiments.inspect_module': _object({'evidence_id': _ID, 'expected_sha256': _HASH,
            'snapshot': {'enum': ['current', 'candidate']},
            'path': _text(2048), 'view': {'enum': ['symbols', 'source']}, 'query': _text(200, minimum=0),
            'offset': _integer(0), 'limit': _integer(1, 6000)}, ['evidence_id', 'expected_sha256']),
        'hyperparameters.read': {**_object({'revision_id': _ID, 'expected_sha256': _HASH,
            'base_unit_id': _ID, 'validation_seed': _integer(0), 'offset': _integer(0), 'limit': _integer(1, 6000)}),
            'oneOf': [{'required': ['revision_id', 'expected_sha256'], 'not': {'required': ['base_unit_id']}},
                      {'required': ['base_unit_id', 'validation_seed'], 'not': {'required': ['revision_id']}}]},
        'hyperparameters.revise': {**_object({'revision_id': _ID, 'expected_sha256': _HASH,
            'action': {'enum': ['patch', 'new_version']}, 'yaml': _text(131072),
            'operations': _array({**_object({'op': {'enum': ['set', 'remove']}, 'path': _text(500), 'value': {}}, ['op', 'path']),
                'allOf': [{'if': {'properties': {'op': {'const': 'set'}}}, 'then': {'required': ['value']}, 'else': {'not': {'required': ['value']}}}]}, 1, 64)},
            ['revision_id', 'expected_sha256', 'action']),
            'allOf': [{'if': {'properties': {'action': {'const': 'patch'}}}, 'then': {'required': ['operations'], 'not': {'required': ['yaml']}},
                       'else': {'required': ['yaml'], 'not': {'required': ['operations']}}}]},
        'artifacts.read': _paged({'id': _ID, 'expected_sha256': _HASH}, ['id']),
        'artifacts.list': _object({'directory': _text(2048, minimum=0), 'offset': _integer(0), 'limit': _integer(1, 50)}),
        'code.locate': _object({'path': _text(2048), 'query': _text(200, minimum=0),
            'offset': _integer(0), 'limit': _integer(1, 30)}),
        'documents.recall': _object({**document_binding, 'section_id': _ID, 'query': _text(200, minimum=0),
            'offset': _integer(0), 'limit': _integer(1, 4)}, document_binding),
        'code.read': _paged({'id': _ID, 'expected_sha256': _HASH}, ['id', 'expected_sha256'], maximum=6000),
        'code.record_notes': _object({'notes': _array(_object({'key': _text(160), 'statement': _text(1200),
            'receipt_sha256s': _array(_LOWER_HASH, 1, 20, unique=True)}, ['key', 'statement', 'receipt_sha256s']), 1, 6)}, ['notes']),
        'code.validate': _object({'code': _text(minimum=0)}, ['code']),
        'python.run': _object({'trial_name': _text(120, nonblank=True),
            'plan_sha256': _LOWER_HASH, 'code_sha256': _LOWER_HASH},
            ['trial_name', 'plan_sha256', 'code_sha256']),
        'literature.search': search, 'web.search': copy.deepcopy(search),
        'literature.verify': {'oneOf': [_object({'paper': paper}, ['paper']), paper]},
        'literature.survey': _object({'queries': _array(_text(400, nonblank=True), 1, 3),
                                    'search_profile': {'enum': ['novelty', None]}}, ['queries']),
        'literature.read_papers': _object({'paper_ids': _array(_text(500), 1, 6, unique=True),
                                         'require_fulltext': _BOOL}, ['paper_ids']),
        'literature.record_notes': _notes(),
        'literature.record_protocol': protocol_schema(),
        'literature.protocols': _object({'protocol_id': _ID, 'note_id': _ID, 'expected_sha256': _LOWER_HASH,
            'offset': _integer(0), 'limit': _integer(1, 20)}),
        'literature.link_source': _object({'paper_id': _ID, 'source_id': _ID}, ['paper_id', 'source_id']),
        'literature.paper_map': _object(page_binding, page_binding),
        'literature.dossier': _paged({**page_binding, 'dossier_sha256': _nullable(_HASH)}, page_binding,
                                    continuation_hash='dossier_sha256'),
        'literature.deep_analyze': _object({**page_binding, 'question': _text(4000)},
                                          [*page_binding, 'question']),
        'literature.expand_citations': _object({'paper_id': _ID, 'backwards': _BOOL,
                                               'forwards': _BOOL, 'limit': _integer(1, 20)}, ['paper_id']),
        'literature.resolve_fulltext': _object({'paper_id': _ID}, ['paper_id']),
        'literature.rag_search': _object({'query': _text(minimum=0)}, ['query']),
        'literature.rag_read': _paged({'version_id': _ID, 'expected_sha256': _nullable(_HASH),
            'section': {'type': 'string', 'pattern': r'^(analysis|source|curation_source:.+)$'}},
            ['version_id'], continuation_hash='expected_sha256'),
        'sources.list': _object(),
        'datasets.discover': _object({'paper_id': _ID, 'offset': _integer(0), 'limit': _integer(1, 20)}),
        'datasets.inspect': _object({'asset_id': _ID, 'expected_sha256': _LOWER_HASH,
                                    'offset': _integer(0), 'limit': _integer(1, 20)}),
        'datasets.download': _object({'note_id': _ID, 'dataset': _text(1200, nonblank=True),
            'url': _text(2048), 'filename': _text(240),
            'catalog_key': _text(80),
            'source_chain': _array(_object({'source_id': _ID, 'expected_sha256': _LOWER_HASH},
                                          ['source_id', 'expected_sha256']), maximum=4),
            'expected_content_sha256': _LOWER_HASH, 'max_bytes': _integer(1, 512 * 1024**2),
            'extract_members': _array(_text(240), maximum=64, unique=True),
            'max_extracted_bytes': _integer(1, 1024**3)}, ['note_id', 'dataset']),
        'sources.links': _paged({**page_binding, 'expected_links_sha256': _nullable(_HASH)},
                               page_binding, 20, continuation_hash='expected_links_sha256'),
        'sources.read': source_page,
        'sources.fetch': _object({'url': _text(), 'limit': _integer(1, 16000)}, ['url']),
        'sources.open_reference': _object({'reference_id': _ID, 'limit': _integer(1, 16000)}, ['reference_id']),
        'evidence.search': _object({'query': _text(1000),
            'source_ids': _array(_ID, 1, 8, unique=True), 'section': _nullable(_text(minimum=0)),
            'limit': _integer(2)}, ['query', 'source_ids']),
        'evidence.expand': _object({'spans': _array(
            _paged({**page_binding, 'section_id': _text(minimum=0)}, page_binding), 1, 4)}, ['spans']),
        'evidence.read': _paged({'artifact_id': _ID, 'expected_sha256': _HASH,
            'path': {'type': 'string', 'pattern': '^/', 'maxLength': 256}},
            ['artifact_id', 'expected_sha256', 'path'], 6000),
        'evidence.catalog': _object({'artifact_id': _ID, 'expected_sha256': _HASH,
            'query': _text(minimum=0), 'purpose': _text(minimum=0)}, ['artifact_id', 'expected_sha256']),
        'evidence.request': _object({'question': _text(4000, nonblank=True),
            'urls': _array(_text(2048), maximum=3)}, ['question']),
        'context.expand': _paged({'receipt_id': _ID, 'expected_sha256': _HASH,
                                  'path': {**_POINTER, 'maxLength': 256}},
                                ['receipt_id', 'expected_sha256'], 6000),
        'documents.list': {'oneOf': [_object(), _object(document_binding, document_binding)]},
        'documents.search': _object({**document_binding, 'query': _text(500, nonblank=True),
            'limit': _integer(1, 8)}, [*document_binding, 'query']),
        'documents.read': _paged({**document_binding, 'section_id': _ID},
                                [*document_binding, 'section_id'], 6000),
        'pdf.info': _object({'source_id': _ID}, ['source_id']),
        'pdf.search': _object({'source_id': _ID, 'query': _text(400, nonblank=True),
                              'limit': _integer(1, 10)}, ['source_id', 'query']),
        'pdf.read': _paged({'source_id': _ID, 'page': _integer(1), 'expected_sha256': _nullable(_HASH)},
                          ['source_id', 'page']),
        'pdf.manifest': _object({'source_id': _ID, 'expected_sha256': _nullable(_HASH)}, ['source_id']),
        'pdf.render': _object(media, ['source_id', 'expected_sha256', 'page']),
        'pdf.ocr': _object(media, ['source_id', 'expected_sha256', 'page']),
        'pdf.analyze_figure': _object({**media, 'question': _text(2000)},
                                    ['source_id', 'expected_sha256', 'page', 'question']),
    }
    schemas['papers.download'] = copy.deepcopy(schemas['literature.read_papers'])
    # CodexPDFReader.render treats an explicit null crop like an omitted crop.
    schemas['pdf.render']['properties']['crop'] = _nullable(_CROP)
    return schemas


def _compact_schema(schema, name):
    """Share repeated schema nodes, keeping tool metadata within context limits.

    A local resource ID makes fragment refs resolve within this argument schema
    even when the schema is embedded in a tool_calls condition. No remote schema
    or reference is fetched. Approved scientific schemas are not rewritten.
    """
    if '$id' in schema:
        return schema  # Preserve the reference scope of an existing resource.
    counts, nodes = {}, {}
    schema_maps = {'properties', 'patternProperties', 'dependentSchemas', '$defs'}
    schema_lists = {'allOf', 'anyOf', 'oneOf', 'prefixItems'}
    schema_values = {'items', 'contains', 'additionalProperties', 'unevaluatedProperties',
                     'propertyNames', 'not', 'if', 'then', 'else'}

    def encoded(node):
        return json.dumps(node, sort_keys=True, ensure_ascii=False, separators=(',', ':'))

    def children(node):
        for key, value in node.items():
            if key in schema_maps and isinstance(value, dict):
                yield from value.values()
            elif key in schema_lists and isinstance(value, list):
                yield from value
            elif key in schema_values:
                yield value

    def count(node):
        if not isinstance(node, dict):
            return
        if '$id' in node:
            return
        key = encoded(node)
        counts[key] = counts.get(key, 0) + 1
        nodes[key] = node
        for child in children(node):
            count(child)

    count(schema)
    root_key = encoded(schema)
    repeated = [key for key, count_value in counts.items()
                if count_value > 1 and len(key) >= 60 and key != root_key]
    if not repeated:
        return schema
    references = {key: 'shared_' + str(index + 1)
                  for index, key in enumerate(sorted(repeated, key=lambda key: (-len(key), key)))}

    def project(node, *, defining=None):
        if not isinstance(node, dict):
            return copy.deepcopy(node)
        if '$id' in node:
            return copy.deepcopy(node)
        key = encoded(node)
        if key in references and key != defining:
            return {'$ref': '#/$defs/' + references[key]}
        output = copy.deepcopy(node)
        for field, value in node.items():
            if field in schema_maps and isinstance(value, dict):
                output[field] = {prop: project(child) for prop, child in value.items()}
            elif field in schema_lists and isinstance(value, list):
                output[field] = [project(child) for child in value]
            elif field in schema_values:
                output[field] = project(value)
        return output

    result = project(schema)
    result['$id'] = 'urn:autoresearch:tool-contracts:v1:' + name
    result['$defs'] = {references[key]: project(nodes[key], defining=key) for key in references}
    return result if len(encoded(result)) < len(root_key) else schema


_SCHEMAS = {name: _compact_schema(schema, name) for name, schema in _catalog().items()}
TOOL_NAMES = frozenset({*_SCHEMAS, 'implementation.write_document',
                        'implementation.edit_document', 'implementation.locate_document',
                        'implementation.record_self_check', 'architecture.draw_diagrams'})


class ToolArgumentsError(ValueError):
    """An argument rejection without provider text or submitted values."""
    def __init__(self, name, *, path='$', code='invalid_arguments'):
        self.name = self.tool = name
        self.path, self.code = path, code
        label = name if name in TOOL_NAMES else 'unknown tool'
        super().__init__(f'{label} arguments rejected at {path} ({code})')


def argument_schema(name, context=None):
    from .prose_schema import relax
    return relax(_argument_schema(name,context))


def _argument_schema(name, context=None):
    """Return an isolated schema; never mutate an approved input contract."""
    if name not in TOOL_NAMES:
        raise ToolArgumentsError(name, code='unknown_tool')
    context = context if isinstance(context, dict) else {}
    if name == 'python.run' and context.get('training_context_protocol') == 1:
        from .python_training import arguments
        result = copy.deepcopy(_SCHEMAS[name])
        for key, value in arguments(context).items():
            result['properties'][key] = {**result['properties'][key], 'const': value}
        return result
    if name == 'implementation.write_document':
        from . import implementation_authoring
        from .codex_workflow import SPEC
        from .implementation_module_details import validation_schema
        authoring = context.get('implementation_document_authoring') or {}
        specification = authoring.get('specification_schema', SPEC) if isinstance(authoring, dict) else SPEC
        if not isinstance(specification, dict):
            raise ToolArgumentsError(name, code='invalid_specification_schema')
        return copy.deepcopy(implementation_authoring.write_arguments_schema(
            validation_schema(specification, context)))
    if name == 'implementation.edit_document':
        from .implementation_authoring import edit_arguments_schema
        return copy.deepcopy(edit_arguments_schema(context))
    if name == 'implementation.locate_document':
        from .implementation_document_locator import schema
        return copy.deepcopy(schema())
    if name == 'implementation.record_self_check':
        from .implementation_self_check import schema
        return copy.deepcopy(schema())
    if name == 'architecture.draw_diagrams':
        from .architecture_diagrams import schema
        return copy.deepcopy(schema())
    result = copy.deepcopy(_SCHEMAS[name])
    if name in {'literature.search', 'web.search'}:
        year = datetime.now(timezone.utc).year + 1
        for field in ('year_from', 'year_to'):
            result['properties'][field]['anyOf'][0]['maximum'] = year
    if name == 'evidence.search' and type(context.get('evidence_max_results')) is int:
        result['properties']['limit']['maximum'] = context['evidence_max_results']
    return result


def _conditions(names, context):
    result = []
    for name in names:
        arguments = argument_schema(name, context)
        identity = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode('utf-8')).hexdigest()
        result.append({'$comment': _CONDITION_MARK + name + ':' + identity,
            'if': {'properties': {'name': {'const': name}}, 'required': ['name']},
            'then': {'properties': {'arguments': arguments}}})
    return result


def call_schema(names, context=None):
    """Keep the existing name enum path and add each tool's typed arguments."""
    names = list(dict.fromkeys(names))
    result = _object({'name': {'enum': names}, 'arguments': {'type': 'object'}}, ['name', 'arguments'])
    result['allOf'] = _conditions(names, context)
    return _compact_call_item(result)


def _compact_call_item(schema):
    identity = hashlib.sha256(json.dumps(schema, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()[:24]
    return _compact_schema(schema, 'call.' + identity)


def constrain_schema(schema, context=None):
    """Type tool_calls branches without changing names, batches or final data.

    Exact call enums already bind both names and full arguments, so they remain
    byte-for-byte identical. Existing specialized conditions are preserved.
    Reapplying this helper refreshes only conditions owned by this module.
    """
    result = copy.deepcopy(schema)

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            properties = node.get('properties')
            calls = properties.get('tool_calls') if isinstance(properties, dict) else None
            items = calls.get('items') if isinstance(calls, dict) else None
            if isinstance(items, dict) and 'enum' not in items:
                item_properties = items.get('properties')
                name_schema = item_properties.get('name') if isinstance(item_properties, dict) else None
                names = name_schema.get('enum') if isinstance(name_schema, dict) else None
                if isinstance(names, list) and all(isinstance(name, str) for name in names):
                    old = items.get('allOf', [])
                    kept = [rule for rule in old if not (isinstance(rule, dict)
                            and str(rule.get('$comment', '')).startswith(_CONDITION_MARK))]
                    retained = {rule['$comment']: rule for rule in old if isinstance(rule, dict)
                                and str(rule.get('$comment', '')).startswith(_CONDITION_MARK)}
                    fresh = _conditions(names, context)
                    # Keep already compacted conditions for identical contracts.
                    # Added tools get a new condition; changed contextual schemas
                    # replace only their old condition. This is idempotent even
                    # when a previous pass has moved shared schemas into $defs.
                    items['allOf'] = [*kept, *(retained.get(rule['$comment'], rule) for rule in fresh)]
                    calls['items'] = _compact_call_item(items)
            for key, value in list(node.items()):
                # enum/const/default payloads are data, not schema nodes.
                if key not in {'enum', 'const', 'default', 'examples'}:
                    visit(value)

    visit(result)
    return result


_STRICT_TYPES = Draft202012Validator.TYPE_CHECKER.redefine_many({
    'integer': lambda checker, value: type(value) is int,
    'number': lambda checker, value: type(value) in (int, float) and (
        type(value) is int or math.isfinite(value)),
})
_StrictValidator = validators.extend(Draft202012Validator, type_checker=_STRICT_TYPES)
StrictToolValidator = _StrictValidator


def _error_path(error):
    parts = ['$']
    for part in error.absolute_path:
        if type(part) is int:
            parts.append(f'[{part}]')
        else:
            parts.append('.' + (part if isinstance(part, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', part)
                                else '<field>'))
    return ''.join(parts)


def validate_arguments(name, args, context=None):
    """Validate before dispatch; object ownership/receipt gates remain handlers."""
    schema = argument_schema(name, context)
    Draft202012Validator.check_schema(schema)
    error = best_match(_StrictValidator(schema).iter_errors(args))
    if error is not None:
        raise ToolArgumentsError(name, path=_error_path(error), code=str(error.validator))
    validate_value_semantics(name, args)


def validate_value_semantics(name, args):
    """Stable v1 cross-field checks after the exact frozen schema is validated.

    This helper does not consult the registry or a newer argument schema. The
    authorizer can therefore reuse a frozen request without changing its scope.
    """
    if name == 'experiments.cleanup':
        bound = {'preview_id', 'expected_sha256', 'file_ids'}
        if (args['action'] == 'delete' and not bound.issubset(args)
                or args['action'] == 'preview' and bound.intersection(args)):
            raise ToolArgumentsError(name, path='$', code='cleanup_preview_binding_required')
    # Exact call enums compare JSON numbers by value (1 equals 1.0), and may not
    # contain a type node for the strict validator. The readers themselves need
    # native integer offsets/pages, so enforce that stable v1 requirement here.
    paged = {'experiments.analyze', 'experiments.inspect_module', 'experiments.status', 'experiments.read', 'experiments.compare', 'datasets.discover', 'datasets.inspect', 'artifacts.read', 'artifacts.list', 'sources.read', 'sources.links', 'documents.read',
             'literature.dossier', 'literature.rag_read', 'evidence.read', 'context.expand', 'pdf.read'}
    limited = paged | {'documents.search', 'pdf.search', 'evidence.search',
                       'literature.expand_citations', 'sources.fetch', 'sources.open_reference'}
    integer_fields = set()
    if name in paged:
        integer_fields.add('offset')
    if name in limited or name == 'memory.search':
        integer_fields.add('limit')
    if name in {'pdf.read', 'pdf.render', 'pdf.ocr', 'pdf.analyze_figure'}:
        integer_fields.add('page')
    if name == 'experiments.history':
        integer_fields.add('cycle')
    if name == 'datasets.download':
        integer_fields.update(('max_bytes', 'max_extracted_bytes'))
    if name in {'literature.search', 'web.search'}:
        integer_fields.update(('year_from', 'year_to'))
    nullable = {'cycle', 'year_from', 'year_to'} | ({'limit'} if name == 'memory.search' else set())
    for field in integer_fields:
        if field in args and not (field in nullable and args[field] is None) and type(args[field]) is not int:
            raise ToolArgumentsError(name, path='$.' + field, code='native_integer_required')
    if name == 'evidence.expand':
        for index, span in enumerate(args.get('spans', [])):
            for field in ('offset', 'limit'):
                if field in span and type(span[field]) is not int:
                    raise ToolArgumentsError(name, path=f'$.spans[{index}].{field}', code='native_integer_required')
    if name in {'sources.fetch', 'datasets.download', 'evidence.request'}:
        urls = ([(args['url'], '$.url')] if 'url' in args else []) if name != 'evidence.request' else [
            (url, f'$.urls[{index}]') for index, url in enumerate(args.get('urls', []))]
        for url, path in urls:
            _validate_url(name, url, path)
    if name == 'datasets.download':
        if ('url' in args) != ('filename' in args) or ('url' in args and 'catalog_key' in args):
            raise ToolArgumentsError(name, path='$.url', code='dataset_source_mode_conflict')
    crop = args.get('crop')
    if crop is not None and not (crop[0] < crop[2] and crop[1] < crop[3]):
        raise ToolArgumentsError(name, path='$.crop', code='invalid_crop_order')
    if name in {'literature.search', 'web.search'}:
        start, end = args.get('year_from'), args.get('year_to')
        if start is not None and end is not None and start > end:
            raise ToolArgumentsError(name, path='$.year_from', code='invalid_year_order')


def _validate_url(name, url, path):
    """Static public-URL checks; the source service still checks and pins DNS."""
    value = url.strip()
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (any(ord(char) < 32 for char in value)
                or parsed.scheme not in {'http', 'https'} or not host
                or parsed.username is not None or parsed.password is not None):
            raise ValueError
        if name in {'sources.fetch', 'datasets.download'} and port not in (None, 80, 443):
            raise ValueError
        if host.lower() == 'localhost' or host.lower().endswith(('.localhost', '.local')):
            raise ValueError
        try:
            address = ipaddress.ip_address(host.split('%', 1)[0])
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
    except ValueError:
        raise ToolArgumentsError(name, path=path, code='invalid_public_url') from None
