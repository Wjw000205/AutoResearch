"""Shared guidance for new model requests and preflight for tool hash bindings.

Never repair a model's hash by padding, truncating or fuzzy matching. Frozen
requests retain their original instructions; callers attach this before sealing.
"""
from __future__ import annotations

import re

GUIDANCE = """HASH SELF-CHECK (every request, before returning): Copy each supplied SHA256
verbatim from the exact host evidence/receipt for that identifier and version.
Check every hash field in your final JSON and every tool call: SHA256 is exactly
64 hexadecimal characters, with no omissions, whitespace or invented padding.
Compare all 64 characters with its authoritative binding, not just its length or
prefix. Repeated references to the same source/version and hash kind must agree
across the entire batch. Keep text_sha256, original content_sha256, document,
artifact, archive, manifest and receipt hashes distinct. Use the hash kind required
by the tool. Do not calculate a source hash from a quotation or from memory.
If the binding is missing or ambiguous, use an allowed lookup or report the gap;
never guess, silently replace a version, or claim that a check/tool succeeded.
Perform this self-check without adding fields outside the requested schema."""


def instruction(text):
    return text if GUIDANCE in text else text + '\n\n' + GUIDANCE


class HashBindingError(ValueError):
    pass


def validate_batch(engine, run_id, calls):
    """Check the whole batch before any execution; handlers still check versions.

    Source-bound expected_sha256 always means the retained text snapshot. Other
    domains stay separate and retain their tool's own authoritative file checks.
    No values or frozen schemas are changed by this preflight.
    """
    seen, documents = {}, {}

    def visit(value, path, domain):
        if isinstance(value, list):
            for i, item in enumerate(value):
                visit(item, f'{path}[{i}]', domain)
        elif isinstance(value, dict):
            for field, item in value.items():
                if (field == 'sha256' or field.endswith('_sha256')) and item is not None:
                    if not isinstance(item, str) or re.fullmatch(r'[0-9a-fA-F]{64}', item) is None:
                        raise HashBindingError(f'{path}.{field}: SHA256 must contain exactly 64 hexadecimal characters')
                visit(item, f'{path}.{field}', domain)
            expected = value.get('expected_sha256')
            if expected is None:
                return
            # Shared source text is one domain across PDF/source/reading tools.
            source = value.get('source_id')
            identity = (('source_text', source) if source else
                (domain, tuple((k, value[k]) for k in ('document_id', 'artifact_id',
                    'receipt_id', 'version_id', 'revision_id', 'id') if isinstance(value.get(k), str))))
            if identity[1]:
                if identity in seen and seen[identity] != expected:
                    raise HashBindingError(f'{path}.expected_sha256: conflicting hashes for the same identity and hash domain')
                seen[identity] = expected
            if source:
                if source not in documents:
                    documents[source] = engine.sources.document(run_id, source)
                if expected != documents[source]['text_sha256']:
                    raise HashBindingError(f'{path}.expected_sha256: does not match the task-owned source text_sha256; no call executed')

    for index, call in enumerate(calls):
        visit(call.get('arguments', {}), f'$.tool_calls[{index}].arguments', call.get('name'))
