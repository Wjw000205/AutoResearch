"""Lossless sharing of identical structured input records, never paper merging.

Every record remains visible in the same model input. Source identities, versions,
unknown fields and decisive evidence are reconstructed exactly, without retrieval.
"""
from __future__ import annotations

from collections import Counter
import copy
import json


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def pack(active):
    value = {key: copy.deepcopy(item) for key, item in active.items() if key != 'context_compilation'}
    counts, originals = Counter(), {}
    def count(item):
        if isinstance(item, dict):
            if '$record' in item or 'shared_context_records' in item:
                raise ValueError('Reserved record reference already present')
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            return
        text = encoded(item)
        # Text interning leaves small but repeated source/condition/locator
        # records. They still cost more than a reference; use encoded size so
        # non-ASCII evidence receives the same lossless sharing opportunity.
        if len(text.encode('utf-8')) >= 64:
            counts[text] += 1
            originals.setdefault(text, item)
        for child in children:
            count(child)
    try:
        count(value)
    except ValueError:
        return active  # Unknown/untrusted reference shapes cannot be reinterpreted.
    references = {text: str(i) for i, (text, n) in enumerate(counts.items()) if n > 1}
    if not references:
        return active
    def share(item, *, root=False):
        if isinstance(item, (dict, list)):
            text = encoded(item)
            if not root and text in references:
                return {'$record': references[text]}
        if isinstance(item, dict):
            return {key: share(child) for key, child in item.items()}
        if isinstance(item, list):
            return [share(child) for child in item]
        return item
    result = share(value)
    # Entries only refer to strictly smaller descendants; there are no cycles.
    result['shared_context_records'] = {rid: share(originals[text], root=True) for text, rid in references.items()}
    result['shared_context_records_policy'] = ('LOSSLESS RECORD TABLE: each object with exactly one $record field '
        'stands for the complete dictionary or array at shared_context_records[ID]. Resolve nested record and text '
        'references before interpreting any source, value, condition, objection or gate. Every field is visible '
        'in this input. Identical serialization alone grants no paper identity, reading credit or approval.')
    if 'context_compilation' in active:
        result['context_compilation'] = copy.deepcopy(active['context_compilation'])
    return _inline_single_use(result)


def _inline_single_use(packed):
    """Remove sharing overhead that becomes redundant after parent interning.

    A repeated parent can make its child/text table entries single-use or cost
    more than their literal values. Inline those exact values; no source,
    number, condition or objection is omitted.
    Only tables produced at the root by the compiler are traversed here.
    """
    names = {'$record': 'shared_context_records', '$text': 'shared_context_text',
             '$catalog_record': 'shared_context_catalog_records'}
    tables = {ref: copy.deepcopy(packed[name]) for ref, name in names.items() if name in packed}
    body = {key: copy.deepcopy(value) for key, value in packed.items() if key not in names.values()}
    while True:
        counts = Counter()
        def count(value):
            if isinstance(value, dict):
                if len(value) == 1:
                    ref = next(iter(value))
                    if ref in tables and isinstance(value[ref], str) and value[ref] in tables[ref]:
                        counts[ref, value[ref]] += 1
                        return
                for child in value.values():
                    count(child)
            elif isinstance(value, list):
                for child in value:
                    count(child)
        count(body)
        for table in tables.values():
            for value in table.values():
                count(value)
        def saves_space(ref, key, value):
            size = len(encoded(value).encode('utf-8'))
            reference_size = len(encoded({ref: key}).encode('utf-8'))
            # One table value/key plus a reference at each remaining use.
            overhead = len(encoded(key).encode('utf-8')) + 2
            return counts[ref, key] * (size - reference_size) > size + overhead
        singles = {(ref, key) for ref, table in tables.items() for key, value in table.items()
                   if not saves_space(ref, key, value)}
        if not singles:
            break
        def inline(value):
            if isinstance(value, dict):
                if len(value) == 1:
                    ref = next(iter(value))
                    if isinstance(value[ref], str) and (ref, value[ref]) in singles:
                        return inline(tables[ref][value[ref]])
                return {key: inline(child) for key, child in value.items()}
            if isinstance(value, list):
                return [inline(child) for child in value]
            return value
        body = inline(body)
        tables = {ref: {key: inline(value) for key, value in table.items() if (ref, key) not in singles}
                  for ref, table in tables.items()}
    result = {**body, **{names[ref]: table for ref, table in tables.items()}}
    return result if len(encoded(result).encode('utf-8')) < len(encoded(packed).encode('utf-8')) else packed
