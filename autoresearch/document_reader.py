"""Standalone bounded reader for an immutable Codex input bundle (stdlib only)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def navigate(manifest_path, action, *, document_id=None, section_id=None, offset=0, limit=4000, query=None):
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('version') != 1:
        raise ValueError('Unsupported document index version')
    if action == 'list':
        return {'run_id': manifest['run_id'], 'spec_hash': manifest['spec_hash'],
                'documents': manifest['documents'], 'max_read_chars': 6000}
    doc = next((v for v in manifest['documents'] if v['document_id'] == document_id), None)
    if not doc:
        raise ValueError('Document is outside the fixed input index')
    file = path.parent / doc['file']
    if (Path(doc['file']).name != doc['file'] or file.is_symlink()
            or getattr(file, 'is_junction', lambda: False)() or file.stat().st_size > 4 * 1024**2):
        raise ValueError('Invalid indexed document file')
    text = file.read_text(encoding='utf-8')
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != doc['sha256']:
        raise ValueError('Indexed document SHA256 changed')
    identity = {'document_id': doc['document_id'], 'sha256': doc['sha256']}
    if action == 'read':
        section = next((v for v in doc['sections'] if v['id'] == section_id), None)
        if (not section or type(limit) is not int or not 1 <= limit <= 6000
                or type(offset) is not int or not 0 <= offset <= section['chars']):
            raise ValueError('Read requires a bounded indexed section and offset')
        # Even a one-section file is paged; never print the entire document.
        length = min(limit, section['chars'] - offset, max(0, len(text) - 1))
        start, end = section['start'] + offset, section['start'] + offset + length
        return {**identity, 'section_id': section_id, 'title': section['title'],
                'text': text[start:end], 'char_start': start, 'char_end': end,
                'next_offset': offset + length if offset + length < section['chars'] else None,
                'untrusted': True, 'scientific_claims_verified': False}
    if action != 'search' or not isinstance(query, str) or not query.strip():
        raise ValueError('Search requires a bounded nonempty query')
    terms = [v.casefold() for v in re.findall(r'\w+', query)]
    hits = []
    for section in doc['sections']:
        body = text[section['start']:section['end']]
        matches = [body.casefold().find(term) for term in terms]
        found = [v for v in matches if v >= 0]
        if found:
            start = max(0, min(found) - 80)
            hits.append({'section_id': section['id'], 'title': section['title'],
                'excerpt': body[start:start + min(240, max(0, len(text) - 1))],
                'offset': start, 'score': len(found)})
    return {**identity, 'hits': sorted(hits, key=lambda v: -v['score'])[:5], 'reading_credit': False}


def main():
    parser = argparse.ArgumentParser(description='Read exact indexed document sections; never dump full Markdown.')
    parser.add_argument('--manifest', required=True)
    parser.add_argument('action', choices=('list', 'read', 'search'))
    parser.add_argument('--document-id')
    parser.add_argument('--section-id')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int, default=4000)
    parser.add_argument('--query')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    result = navigate(args.manifest, args.action, document_id=args.document_id, section_id=args.section_id,
                      offset=args.offset, limit=args.limit, query=args.query)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
