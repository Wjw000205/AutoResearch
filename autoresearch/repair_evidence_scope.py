"""Frozen evidence capabilities, independent of permission to edit source files.

The catalog grants read access to exact artifact/version pairs only. It never
changes an issue's allowed_paths or supplies code/execution approval.
"""
from __future__ import annotations


def code_sources(packet):
    return packet.get('audit_sources', {}).get('code_files', packet.get('code_files', {}))


def matches_code_receipt(packet, page):
    source = code_sources(packet).get(page.get('path'))
    return bool(source and source.get('artifact_id') == page.get('artifact_id')
                and source.get('sha256') == page.get('sha256'))


def provided_code_paths(source):
    # A filename in a directory is not evidence that its body was supplied.
    paths = {path for path, row in source.get('code_files', {}).items()
             if any(part.get('text') for part in row.get('excerpts', []))}
    paths.update(row['identity'] for row in source.get('requested_evidence', [])
                 if row.get('kind') == 'code' and row.get('text'))
    return paths
