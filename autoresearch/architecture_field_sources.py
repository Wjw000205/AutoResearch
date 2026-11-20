"""Authenticated derived field locations for immutable architecture layouts.

This view does not render or rewrite approved bytes, nor infer field semantics.
Extra fields in the legacy JSON block share its exact indexed read unit.
"""
import hashlib
import json

from .implementation_review_routing import digest

VERSION = 1


def compile_sources(design, document, text):
    from .document_index import sections
    if (hashlib.sha256(text.encode('utf-8')).hexdigest() != document['sha256']
            or sections(text) != document['sections']):
        raise ValueError('Architecture field source document/index identity changed')
    if f'设计版本：`{digest(design)}`' not in text:
        raise ValueError('Architecture field source design identity changed')
    mapped, missing = {}, []
    for field, value in design.items():
        direct = [s for s in document['sections'] if s['title'] == field]
        extra = []
        for section in document['sections']:
            if section['title'] != '其他设计字段':
                continue
            body = text[section['start']:section['end']]
            if '```json\n' not in body:
                continue
            raw = body.split('```json\n', 1)[1].rsplit('```', 1)[0].strip()
            try:
                values = json.loads(raw)
            except ValueError:
                continue
            if isinstance(values, dict) and field in values and digest(values[field]) == digest(value):
                extra.append(section)
        candidates = direct + extra
        if len(candidates) != 1:
            missing.append({'field': field, 'reason': 'missing_or_ambiguous_exact_source',
                            'candidate_section_ids': [s['id'] for s in candidates]})
            continue
        section = candidates[0]
        covered = [section]
        encoding = 'legacy_extra_json_v1' if extra else 'field_section_v1'
        if direct:
            rendered = value if isinstance(value, str) else '```json\n' + architecture_json(value) + '\n```'
            expected = '### ' + field + '\n\n' + rendered + '\n'
            if text.startswith(expected, section['start']):
                stop = section['start'] + len(expected)
                covered = [s for s in document['sections']
                           if s['start'] < stop and s['end'] > section['start']]
            elif field != 'benchmark_tasks' or '输入输出组合（每行一个任务）' not in text[section['start']:section['end']]:
                missing.append({'field': field, 'reason': 'field_value_not_in_exact_source',
                                'candidate_section_ids': [section['id']]})
                continue
            else:
                encoding = 'shared_benchmark_template_v1'
        mapped[field] = {'field': field, 'section_id': section['id'], 'section_title': section['title'],
            'source_encoding': encoding,
            'section_ids': [s['id'] for s in covered], 'section_titles': [s['title'] for s in covered],
            'ranges': [{**s, 'source_sha256': hashlib.sha256(text[s['start']:s['end']].encode()).hexdigest()}
                       for s in covered],
            'start': section['start'], 'end': section['end'],
            'value_sha256': digest(value),
            'source_sha256': hashlib.sha256(text[section['start']:section['end']].encode()).hexdigest()}
    body = {'version': VERSION, 'document_id': document['document_id'],
            'document_sha256': document['sha256'], 'design_sha256': digest(design),
            'fields': mapped, 'unresolved': missing, 'semantic_equivalence_verified': False}
    return {**body, 'sha256': digest(body)}


def architecture_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
