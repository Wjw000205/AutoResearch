"""Exact-base immutable draft patches, never scientific approval.

Only the approved scientific sources constrain science. Unapproved prior SPECs
provide an optional exact patch base, without locking their author-added clauses.
"""
from __future__ import annotations

import copy
import re

EDIT_EXPECTED_SHA256_CONTRACT = (
    'implementation.edit_document requires this exact document_id, document_sha256 as '
    'expected_sha256, and spec_hash as base_spec_hash, including the first edit of a '
    'previous reviewed version. After saving, use the current draft descriptor. '
    'Submit only targeted edits allowed by the current tool schema; untouched content is retained exactly.')


class SaveConflict(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def _identity(row):
    doc = row.get('document') or {}
    return row.get('index'), row.get('spec_hash'), doc.get('document_id'), doc.get('sha256')


def baseline(engine, run_id, state, binding, context, latest):
    """Use the current immutable draft, otherwise the assignment's exact predecessor."""
    from .engine import digest
    from . import document_index
    if latest:
        return latest['row']
    from .implementation_acceptance_author import CONTINUATION, continuation_base
    if CONTINUATION in context:
        return continuation_base(engine, run_id, state, context)
    revision = binding['revision']
    if revision == 0:
        return None
    candidates = [r for r in state.get('specifications', []) if r.get('index') == revision - 1]
    frozen = [r for r in context.get('previous_specifications', []) if r.get('index') == revision - 1]
    if len(candidates) != 1 or len(frozen) != 1 or _identity(candidates[0]) != _identity(frozen[0]):
        raise SaveConflict('authoring_base_conflict', '前版实现文档与本次冻结编写输入不一致；未保存，请恢复正确的版本绑定。')
    row = candidates[0]
    if digest(row['spec']) != row.get('spec_hash'):
        raise SaveConflict('authoring_base_conflict', '前版实现规范内容哈希不符；未保存，请核对原始版本。')
    doc = row['document']
    document_index.load(engine, run_id, doc['document_id'], doc['sha256'])
    return row


def descriptor(row, *, edit=False):
    if row is None:
        return None
    from .implementation_document_locator import item_index, catalog
    value = {'spec_hash': row['spec_hash'], 'document_id': row['document']['document_id'],
            'document_sha256': row['document']['sha256'], 'revision': row['index'],
            'item_index': item_index(row['spec']),
            'document_index': catalog(row['spec']),
            'acceptance_ids': [r['id'] for r in row['spec']['acceptance_checks']],
            'patch_contract': 'JSON Pointer add/replace/remove; arrays use zero-based indices. '
                'Set base_spec_hash to this descriptor\'s spec_hash. Before this request saves its first '
                'draft, this is the previous reviewed version; after saving, it is this request\'s current '
                'draft. Untouched fields are copied exactly, not regenerated.',
            'expected_sha256_contract': 'Omit expected_sha256 for this request\'s first save, even when '
                'a previous version exists. The previous version\'s document_sha256 is not a draft '
                'write precondition. Only when modifying a saved implementation_document_draft, use '
                'that current draft\'s document.sha256 as expected_sha256 and submission.spec_hash '
                'as base_spec_hash; do not reuse hashes from the previous reviewed version.',
            'scientific_approval': False}
    if edit:
        value['expected_sha256_contract'] = EDIT_EXPECTED_SHA256_CONTRACT
        value['locate_tool'] = 'implementation.locate_document'
    return value


def _tokens(path):
    if not isinstance(path, str) or not path.startswith('/') or re.search(r'~(?![01])', path):
        raise SaveConflict('invalid_document_patch', 'patch.path 必须是非根 JSON Pointer，使用 ~0/~1 转义。')
    return [x.replace('~1', '/').replace('~0', '~') for x in path[1:].split('/')]


def _index(token, length, *, append=False):
    if append and token == '-':
        return length
    if not re.fullmatch(r'0|[1-9][0-9]*', token):
        raise SaveConflict('invalid_document_patch', '数组 patch 路径必须使用非负整数索引。')
    # Bound the digit count before int(): even a malformed huge index must
    # return a normal patch diagnostic, not Python's integer conversion error.
    if len(token) > len(str(length)):
        raise SaveConflict('invalid_document_patch', '数组 patch 路径超出当前基线范围。')
    index = int(token)
    if index >= length + int(append):
        raise SaveConflict('invalid_document_patch', '数组 patch 路径超出当前基线范围。')
    return index


def _patch(spec, operations):
    if not isinstance(operations, list) or not 1 <= len(operations) <= 64:
        raise SaveConflict('invalid_document_patch', 'patch 必须含 1 至 64 项定向修改。')
    result = copy.deepcopy(spec)
    for position, operation in enumerate(operations):
        if (not isinstance(operation, dict) or not isinstance(operation.get('op'), str)
                or operation['op'] not in {'add', 'replace', 'remove'}):
            raise SaveConflict('invalid_document_patch', 'patch 仅允许 add/replace/remove。', operation_index=position)
        op = operation['op']
        if set(operation) != ({'op', 'path'} if op == 'remove' else {'op', 'path', 'value'}):
            raise SaveConflict('invalid_document_patch', 'patch 操作字段不完整或含未知字段。', operation_index=position)
        parent = result
        try:
            tokens = _tokens(operation['path'])
            for token in tokens[:-1]:
                parent = parent[_index(token, len(parent))] if isinstance(parent, list) else parent[token]
            token = tokens[-1]
            if isinstance(parent, list):
                index = _index(token, len(parent), append=op == 'add')
                if op == 'remove':
                    parent.pop(index)
                elif op == 'add':
                    parent.insert(index, copy.deepcopy(operation['value']))
                else:
                    parent[index] = copy.deepcopy(operation['value'])
            elif isinstance(parent, dict):
                if op != 'add' and token not in parent:
                    raise KeyError(token)
                if op == 'remove':
                    del parent[token]
                else:
                    parent[token] = copy.deepcopy(operation['value'])
            else:
                raise TypeError('not a container')
        except SaveConflict as exc:
            raise SaveConflict(exc.code, str(exc), **exc.details,
                               operation_index=position, path=operation['path']) from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise SaveConflict('invalid_document_patch', 'patch 路径不存在或不是可修改容器。',
                               operation_index=position, path=operation['path']) from exc
    return result


def resolve(args, base):
    supplied = args.get('base_spec_hash')
    if supplied is not None and (not isinstance(supplied, str) or not re.fullmatch('[0-9a-f]{64}', supplied)):
        raise SaveConflict('authoring_base_conflict', 'base_spec_hash 必须是冻结基线的完整 SHA256。')
    if supplied is not None and (base is None or supplied != base['spec_hash']):
        raise SaveConflict('authoring_base_conflict', 'base_spec_hash 与当前保存基线不一致；未应用修改。',
                           base=descriptor(base))
    if 'patch' in args:
        if 'specification' in args or base is None or supplied is None:
            raise SaveConflict('invalid_document_patch', '定向 patch 需要已有基线及 base_spec_hash，不能同时传 specification。',
                               base=descriptor(base))
        return _patch(base['spec'], args['patch'])
    return copy.deepcopy(args.get('specification'))


def resolve_edit(args, base):
    """Apply only a bounded patch to the exact current immutable base."""
    required = {'document_id', 'expected_sha256', 'base_spec_hash'}
    if not isinstance(args, dict) or set(args) not in (required | {'patch'}, required | {'revisions'}):
        raise SaveConflict('invalid_document_edit',
            'implementation.edit_document 接受三个基线标识，以及 patch 或 revisions 之一。')
    check_edit_base(args, base)
    if 'revisions' in args:
        from .implementation_document_locator import revision_patch
        operations = revision_patch(base['spec'], args['revisions'])
    else:
        operations = args['patch']
    return resolve({'base_spec_hash': args['base_spec_hash'], 'patch': operations}, base)


def check_edit_base(args, base):
    """Shared immutable identity check for locating and editing the same value."""
    if base is None:
        raise SaveConflict('document_edit_base_missing',
            '尚无实现文档可编辑；首次创建请使用 implementation.write_document。')
    document = base['document']
    if (not isinstance(args['document_id'], str) or not args['document_id']
            or not isinstance(args['expected_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', args['expected_sha256'])
            or args['document_id'] != document['document_id']
            or args['expected_sha256'] != document['sha256']
            or args['base_spec_hash'] != base['spec_hash']):
        raise SaveConflict('authoring_base_conflict',
            '编辑目标 ID、正文 SHA256 或 spec_hash 与当前不可变基线不一致；未应用修改。',
            base=descriptor(base, edit=True))

