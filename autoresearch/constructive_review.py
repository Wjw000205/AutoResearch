"""Public, actionable review structure without rewriting independent votes.

Integration: opt only new frozen assignments into ``review_schema``. Before a
new revision, request one amendment from each legacy dissenting role in parallel.
Freeze the original proposal hash, complete original vote hash, objection list,
role, evidence metadata and this schema under a deterministic amendment purpose.
Keep original votes immutable and persist amendments as separate handoffs. The
director integrates these attributed fixes and must resolve disagreement; an
amendment neither approves a proposal nor changes its historical vote. Fix text
is a suggested modification, not new retrieved evidence or measured progress.
"""
from __future__ import annotations

from copy import deepcopy
import re


PROPOSAL_FIELDS = ('title', 'hypothesis', 'novelty', 'method', 'baseline', 'metrics', 'risks',
                   'resource_plan', 'data_plan', 'publication_gap', 'references')
MAX_OBJECTIONS = 8
MAX_FIXES = 24
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1200, 'pattern': r'\S'}
FIX_SCHEMA = {
    'type': 'object', 'properties': {
        'objection_index': {'type': 'integer', 'minimum': 0, 'maximum': MAX_OBJECTIONS - 1},
        'target_field': {'enum': list(PROPOSAL_FIELDS)},
        'proposed_change': TEXT,
        'verification_plan': TEXT,
        'evidence_ids': {'type': 'array', 'maxItems': 30, 'uniqueItems': True,
                         'items': {'type': 'string', 'minLength': 1, 'maxLength': 500, 'pattern': r'\S'}}},
    'required': ['objection_index', 'target_field', 'proposed_change', 'verification_plan', 'evidence_ids'],
    'additionalProperties': False,
}
FIXES_SCHEMA = {'type': 'array', 'maxItems': MAX_FIXES, 'items': FIX_SCHEMA}
CONTEXT_REFERENCES_SCHEMA = {'type': 'array', 'maxItems': 30, 'uniqueItems': True,
    'items': {'type': 'string', 'minLength': 1, 'maxLength': 500, 'pattern': r'\S'}}
PUBLIC_REVIEW_FIELDS = frozenset({'role', 'approve', 'proposal_hash', 'rationale', 'required_changes',
                                 'progress', 'fixes'})
_CONTEXT_ROOTS = frozenset({'resource_assessment', 'evidence_assessment', 'execution_constraints',
    'idea_literature_index', 'related_work_index', 'literature_date_policy'})
_PATH = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:(?:\.[A-Za-z][A-Za-z0-9_]*)|(?:\[(?:0|[1-9][0-9]*)\]))*')
_OBJECTION_ID = re.compile(r'(director|engineer|scout):[0-9a-f]{16}')
_PREVIOUS_OBJECTION_PATH = re.compile(
    r'(?:context\.)?previous_discussion\.objections\['
    r'(?:"(?P<id>(?:director|engineer|scout):[0-9a-f]{16})"|(?P<index>0|[1-9][0-9]*))'
    r'\](?:\.(?P<field>id|role|text))?')

# Referencing a public field does not make it a valid modification target. Keep
# PROPOSAL_FIELDS and the request schemas unchanged for frozen assignments.
# None marks a leaf; a one-element list describes an indexed public array.
_PUBLIC_PROPOSAL_EXTENSIONS = {
    'idea_rationale': {
        **dict.fromkeys(('origin', 'motivation', 'gap', 'mechanism', 'differentiation')),
        'problem_validation': {
            **dict.fromkeys(('target_scenario', 'affected_users', 'observable_failure', 'why_it_matters',
                             'existing_solutions', 'why_insufficient', 'necessity_check', 'abandonment_rule')),
            'evidence_ids': [None]},
        'evidence': [dict.fromkeys(('paper_id', 'claim', 'relation', 'evidence_level', 'locator', 'limitation'))]},
    'paper_story': {
        **dict.fromkeys(('problem', 'gap', 'hypothesis', 'method')),
        'claims': [{**dict.fromkeys(('claim', 'validation_plan', 'status')), 'evidence_ids': [None]}],
        'outline': [None], 'title_candidates': [None]},
    'novelty_review': {
        **dict.fromkeys(('search_cutoff', 'conclusion')),
        'search_queries': [None], 'limitations': [None],
        'closest_work': [dict.fromkeys(('paper_id', 'overlap', 'differences', 'risk', 'why_distinct', 'required_check'))]},
}
_PUBLIC_HARDWARE = {
    **dict.fromkeys(('cpu_name', 'cpu_cores', 'cpu_physical_cores', 'ram_total_gb', 'ram_available_gb',
                     'disk_free_gb', 'platform', 'python', 'docker_available', 'checked_at', 'provenance',
                     'gpu_detection')),
    'gpus': [dict.fromkeys(('index', 'name', 'total_gb', 'free_gb', 'driver_version'))],
    'software': {
        **dict.fromkeys(('python_executable', 'python_prefix', 'python_version', 'is_virtualenv',
                         'installed_distributions', 'inventory_verified', 'inventory_error',
                         'excluded_from_isolated_runtime', 'checked_at', 'provenance', 'cache_seconds')),
        'packages': {name: dict.fromkeys(('installed', 'version', 'verification')) for name in
                     ('numpy', 'scipy', 'scikit-learn', 'pandas', 'matplotlib', 'torch', 'torchvision',
                      'torchaudio', 'tensorflow', 'jax', 'jaxlib', 'transformers', 'datasets', 'accelerate',
                      'xgboost', 'lightgbm', 'timm', 'optuna', 'pypdf')},
        'gpu_runtime': {
            **dict.fromkeys(('cuda_toolkit_version', 'nvcc_available', 'note')),
            'torch': dict.fromkeys(('installed', 'probe_status', 'cuda_available', 'cuda_build', 'version',
                                   'error', 'error_tail', 'exit_code', 'probe_timeout_seconds'))}},
}

# These are public retrieval receipts, not source text or scientific evidence.
_PUBLIC_EVIDENCE = {'novelty_search_scope': {
    'exhaustive': None, 'policy': None,
    'queries': [{**dict.fromkeys(('receipt_id', 'plan_id', 'as_of_date', 'query', 'base_query',
        'facet', 'sort', 'year_from', 'year_to', 'scout_requested', 'successful')),
        'paper_ids': [None], 'errors': [dict.fromkeys(('source', 'error', 'status_code', 'checked_at'))]}]}}


def _public_path(parts, tree):
    """Check public field names separately from their presence in a snapshot."""
    for part in parts:
        if isinstance(part, str) and isinstance(tree, dict) and part in tree:
            tree = tree[part]
        elif type(part) is int and isinstance(tree, list):
            tree = tree[0]
        else:
            return False
    return True


class ConstructiveReviewError(ValueError):
    """The public review does not cover its own objections with structured fixes."""


def _changes(value):
    if not isinstance(value, list) or len(value) > MAX_OBJECTIONS:
        raise ConstructiveReviewError('required_changes must contain at most eight objections')
    if any(not isinstance(text, str) or not text.strip() for text in value):
        raise ConstructiveReviewError('Every required change must be nonempty text')
    return value


def _fixes(value, objection_count):
    if not isinstance(value, list) or len(value) > MAX_FIXES:
        raise ConstructiveReviewError('fixes must be a bounded array')
    covered = set()
    for fix in value:
        required = set(FIX_SCHEMA['required'])
        if (not isinstance(fix, dict) or not required <= set(fix)
                or set(fix) - required - {'context_references'}):
            raise ConstructiveReviewError('Every fix requires exactly index, target field, proposed change, verification plan and evidence IDs')
        index = fix['objection_index']
        if type(index) is not int or not 0 <= index < objection_count:
            raise ConstructiveReviewError('Fix objection_index must identify an existing required change')
        if fix['target_field'] not in PROPOSAL_FIELDS:
            raise ConstructiveReviewError('Fix target_field must be a proposal field')
        for field in ('proposed_change', 'verification_plan'):
            text = fix[field]
            if not isinstance(text, str) or not text.strip():
                raise ConstructiveReviewError(f'Fix {field} must be nonempty text')
        evidence = fix['evidence_ids']
        if (not isinstance(evidence, list) or len(evidence) > 30
                or any(not isinstance(identifier, str) or not identifier.strip()
                       for identifier in evidence) or len(set(evidence)) != len(evidence)):
            raise ConstructiveReviewError('Fix evidence_ids must contain unique bounded source identifiers')
        if 'context_references' in fix:
            _reference_strings(fix['context_references'], 'context_references')
        covered.add(index)
    if covered != set(range(objection_count)):
        raise ConstructiveReviewError('Every required change must have at least one corresponding fix')


def _fixes_schema(allow_context_references):
    schema = deepcopy(FIXES_SCHEMA)
    if allow_context_references:
        schema['items']['properties']['context_references'] = deepcopy(CONTEXT_REFERENCES_SCHEMA)
    return schema


def review_schema(base_schema, *, allow_context_references=False):
    """Return an independent schema copy; never change the legacy vote schema."""
    if (not isinstance(base_schema, dict) or base_schema.get('type') != 'object'
            or not {'approve', 'required_changes'} <= set(base_schema.get('properties', {}))):
        raise ConstructiveReviewError('Review schema must contain approve and required_changes')
    schema = deepcopy(base_schema)
    schema['properties']['fixes'] = _fixes_schema(allow_context_references)
    schema['required'] = list(dict.fromkeys([*schema.get('required', []), 'fixes']))
    schema.setdefault('allOf', []).append({
        'if': {'properties': {'approve': {'const': True}}, 'required': ['approve']},
        'then': {'properties': {'required_changes': {'maxItems': 0}, 'fixes': {'maxItems': 0}}},
        'else': {'properties': {'required_changes': {'minItems': 1}, 'fixes': {'minItems': 1}}},
    })
    return schema


def validate_review(review):
    """Validate structure/coverage, not scientific quality or evidence ownership.

    The caller still validates the full output schema, exact proposal hash and
    run-owned evidence IDs. Empty evidence_ids is legitimate for a proposed
    design fix that makes no external evidentiary claim. No text is rewritten.
    """
    if not isinstance(review, dict) or type(review.get('approve')) is not bool:
        raise ConstructiveReviewError('Review approve must be an explicit boolean')
    changes = _changes(review.get('required_changes'))
    fixes = review.get('fixes')
    if review['approve']:
        if changes or fixes != []:
            raise ConstructiveReviewError('Approval requires empty required_changes and fixes')
    else:
        if not changes:
            raise ConstructiveReviewError('A dissenting review must state at least one required change')
        _fixes(fixes, len(changes))
    return review


def amendment_schema(required_changes, *, proposal_hash=None, review_hash=None, allow_context_references=False):
    """A legacy dissenter supplements its saved vote, bound to both identities."""
    changes = _changes(required_changes)
    if not changes:
        raise ConstructiveReviewError('Only a dissenting review with objections needs an amendment')
    fixes = _fixes_schema(allow_context_references)
    fixes.update(minItems=len(changes))
    fixes['items']['properties']['objection_index']['maximum'] = len(changes) - 1
    hashes = {}
    for field, value in [('proposal_hash', proposal_hash), ('review_hash', review_hash)]:
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConstructiveReviewError(f'{field} must be a nonempty exact hash')
        hashes[field] = {'type': 'string', 'minLength': 1, 'maxLength': 200,
                         **({'const': value} if value is not None else {})}
    return {'type': 'object', 'properties': {**hashes, 'fixes': fixes},
            'required': ['proposal_hash', 'review_hash', 'fixes'], 'additionalProperties': False}


def validate_amendment(amendment, original_review, *, proposal_hash, review_hash):
    """Check one supplemental response while leaving its original vote intact."""
    if (not isinstance(amendment, dict) or set(amendment) != {'proposal_hash', 'review_hash', 'fixes'}
            or not isinstance(proposal_hash, str) or not proposal_hash.strip()
            or not isinstance(review_hash, str) or not review_hash.strip()
            or amendment.get('proposal_hash') != proposal_hash or amendment.get('review_hash') != review_hash):
        raise ConstructiveReviewError('Amendment must match the exact frozen proposal and review hashes')
    if (not isinstance(original_review, dict) or original_review.get('approve') is not False
            or original_review.get('proposal_hash') != proposal_hash):
        raise ConstructiveReviewError('Amendment requires the original dissenting vote for this proposal')
    validate_review({**original_review, 'fixes': amendment['fixes']})
    return amendment


def _reference_strings(value, field):
    if (not isinstance(value, list) or len(value) > 30
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(set(value)) != len(value)):
        raise ConstructiveReviewError(f'Fix {field} must contain unique bounded reference strings')
    return value


def _previous_objection_pointer(match, context):
    """Resolve only public objections in this assignment's frozen prior round.

    Bracketed IDs select an exact, unique ID in the list produced by
    discussion.previous_review; they are not arbitrary dictionary expressions.
    A historical objection remains review context, never scientific evidence.
    """
    previous = context.get('previous_discussion')
    objections = previous.get('objections') if isinstance(previous, dict) else None
    if not isinstance(objections, list):
        return None
    if match['id'] is not None:
        matches = [index for index, row in enumerate(objections)
                   if isinstance(row, dict) and row.get('id') == match['id']]
        if len(matches) != 1:
            return None
        index = matches[0]
    else:
        index = int(match['index'])
        if index >= len(objections):
            return None
    row = objections[index]
    if not isinstance(row, dict) or set(row) != {'id', 'role', 'text'}:
        return None
    identifier = row['id']
    if not isinstance(identifier, str) or not _OBJECTION_ID.fullmatch(identifier):
        return None
    if row['role'] != identifier.split(':', 1)[0] or not isinstance(row['text'], str) or not row['text'].strip():
        return None
    if sum(isinstance(item, dict) and item.get('id') == identifier for item in objections) != 1:
        return None
    pointer = f'/previous_discussion/objections/{index}'
    return pointer + '/' + match['field'] if match['field'] else pointer


def _context_pointer(path, context):
    """Resolve a small JSON-only path grammar; never evaluate model text."""
    if not isinstance(path, str) or not isinstance(context, dict):
        return None
    if path in {'previous_discussion.objections', 'context.previous_discussion.objections'}:
        previous = context.get('previous_discussion')
        rows = previous.get('objections') if isinstance(previous, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        for index in range(len(rows)):
            match = _PREVIOUS_OBJECTION_PATH.fullmatch(f'previous_discussion.objections[{index}]')
            if _previous_objection_pointer(match, context) is None:
                return None
        return '/previous_discussion/objections'
    objection = _PREVIOUS_OBJECTION_PATH.fullmatch(path)
    if objection:
        return _previous_objection_pointer(objection, context)
    if not _PATH.fullmatch(path):
        return None
    parts = [word if word else int(index) for word, index in
             re.findall(r'([A-Za-z][A-Za-z0-9_]*)|\[([0-9]+)\]', path)]
    if parts[0] == 'context':
        parts = parts[1:]
    if not parts:
        return None
    root = parts[0]
    if root == 'proposal':
        if len(parts) < 2:
            return None
        if parts[1] not in PROPOSAL_FIELDS and not _public_path(parts[1:], _PUBLIC_PROPOSAL_EXTENSIONS):
            return None
    elif root == 'hardware':
        if len(parts) < 2 or not _public_path(parts[1:], _PUBLIC_HARDWARE):
            return None
    elif root == 'evidence':
        if len(parts) < 2 or not _public_path(parts[1:], _PUBLIC_EVIDENCE):
            return None
    elif root == 'original_review':
        if len(parts) < 2 or parts[1] not in PUBLIC_REVIEW_FIELDS:
            return None
        if parts[1] == 'required_changes' and len(parts) > 2 and (len(parts) != 3 or type(parts[2]) is not int):
            return None
    elif root == 'votes':
        if (len(parts) not in {3, 4} or parts[1] not in {'director', 'engineer', 'scout'}
                or not (parts[2] == 'rationale' and len(parts) == 3
                        or parts[2] == 'required_changes' and len(parts) == 4 and type(parts[3]) is int)):
            return None
        votes = context.get('votes')
        if not isinstance(votes, list):
            return None
        matches = [index for index, vote in enumerate(votes)
                   if isinstance(vote, dict) and vote.get('role') == parts[1]]
        if len(matches) != 1:
            return None
        parts[1] = matches[0]
    elif root not in _CONTEXT_ROOTS:
        return None
    value = context
    for part in parts:
        if isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        elif type(part) is int and isinstance(value, list) and 0 <= part < len(value):
            value = value[part]
        else:
            return None
    if value is None or isinstance(value, str) and not value.strip() or isinstance(value, (dict, list)) and not value:
        return None
    if not isinstance(value, (str, dict, list, bool, int, float)):
        return None
    return '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1') for part in parts)


def _reference_fixes(result):
    if not isinstance(result, dict) or not isinstance(result.get('fixes'), list) or len(result['fixes']) > MAX_FIXES:
        raise ConstructiveReviewError('Reference validation requires a bounded fixes array')
    if any(not isinstance(fix, dict) for fix in result['fixes']):
        raise ConstructiveReviewError('Reference validation requires structured fixes')
    return result['fixes']


def validate_references(result, context):
    """Validate every explicit context reference; evidence ownership is separate."""
    for fix in _reference_fixes(result):
        for path in _reference_strings(fix.get('context_references', []), 'context_references'):
            if _context_pointer(path, context) is None:
                raise ConstructiveReviewError('Context reference is not an existing nonempty allowed public path')


def normalize_references(result, context):
    """Separate exact public context paths from source IDs without changing raw input.

    Unknown/malformed paths, DOI strings and foreign source IDs remain in
    evidence_ids for the caller's run-owned citation check and discard audit. The audit
    contains original paths and resolved JSON pointers, never copied values.
    Schema expansion is opt-in so saved model request identities stay unchanged.
    """
    normalized = deepcopy(result)
    validate_references(normalized, context)
    audit = []
    for index, fix in enumerate(_reference_fixes(normalized)):
        remaining = []
        references = list(fix.get('context_references', []))
        for identifier in _reference_strings(fix.get('evidence_ids'), 'evidence_ids'):
            pointer = _context_pointer(identifier, context)
            if pointer is None:
                remaining.append(identifier)
            else:
                if identifier not in references:
                    references.append(identifier)
                audit.append({'fix_index': index, 'original_path': identifier, 'resolved_pointer': pointer})
        fix['evidence_ids'] = remaining
        if references or 'context_references' in fix:
            fix['context_references'] = references
    validate_references(normalized, context)
    return normalized, audit
