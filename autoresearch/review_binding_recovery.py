"""Recover a single mistyped digest echoed from an authenticated review assignment."""
import copy
import json
import re

from . import implementation_alignment as alignment
from . import review_document_ids
from . import code_repair_response_correction as correction
from . import review_evidence_supplement as supplement
from .host_section_recovery import one_digit_copy
from .native_retained_response import captured_delivery
from .prose_schema import validator

KIND = 'review_digest_from_frozen_assignment'
FIELDS = tuple(alignment.BINDING_SCHEMA['properties'])
LEGACY_ERRORS = {f'Provider JSON failed schema validation at $.alignment_binding.{field}: must satisfy pattern'
                 for field in FIELDS}


def applies(purpose, role, context):
    return (str(purpose).split(':', 1)[0] in {'code_review', 'implementation_document_review'}
            and role in {'scout','director','engineer'} and context.get(alignment.FIELD) == 1)


def local(store, saved, frozen, prepared, text):
    from .json_format_recovery import _unique_object
    ctx = frozen['context']
    request = prepared['context']
    if not applies(request.get('_purpose'), saved['role'], ctx):
        return None
    contract = ctx.get('implementation_alignment_contract', {})
    if (contract.get('version') != 1 or request.get('implementation_alignment_contract') != contract
            or any(not isinstance(contract.get(k), str) or not re.fullmatch('[0-9a-f]{64}', contract[k])
                   for k in FIELDS)
            or any(contract[k] != ctx.get(k) for k in ('design_hash','experiment_plan_hash'))
            or contract['architecture_document_sha256'] != (ctx.get('architecture_document') or {}).get('sha256')):
        return None
    value = json.loads(text, object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or type(value.get('approve')) is not bool:
        return None
    binding = value.get('alignment_binding')
    if not isinstance(binding, dict) or set(binding) != set(FIELDS):
        return None
    for key in ('proposal_hash','spec_hash','code_sha256','delivery_hash','document_sha256'):
        if key in ctx and (key in value or key != 'document_sha256') and value.get(key) != ctx[key]:
            return None
    changed = [key for key in FIELDS if binding[key] != contract[key]]
    if len(changed) != 1:
        return None
    field, = changed
    raw, expected = binding[field], contract[field]
    if not isinstance(raw, str) or not one_digit_copy(raw, expected):
        return None
    result = {**value, 'alignment_binding':{**binding, field:expected}}
    from .review_response_policy import acceptance_schema
    if not all(validator(schema).is_valid(result) for schema in (acceptance_schema(frozen['schema']), prepared['schema'])):
        return None
    wire = captured_delivery(store, saved, frozen)
    if not wire or not re.search(r'(?<![0-9a-f])' + expected + r'(?![0-9a-f])', wire['user']):
        return None
    return result, {'kind':KIND, 'field':'alignment_binding.' + field, 'original':raw, 'verified':expected}


def blocked(run):
    return (run.get('status') == 'blocked' and run.get('stage') == 'implementation'
            and (run.get('error') in LEGACY_ERRORS | review_document_ids.ERRORS |
                 {correction.SAME_REQUIREMENT_ERROR,correction.SUPPLEMENT_SCOPE_ERROR}
                 or supplement.legacy_error(run.get('error'))))


def recoverable_batch(engine, run):
    """Only resume a stopped batch whose outstanding roles already delivered recoverable answers."""
    from .codex_reasoning import key_for
    from .codex_workflow import _digest, _frozen_spec_assignment
    from .implementation_indexed_review import purpose as spec_purpose
    from .config import get_settings
    from .engine import ResearchBlocked, digest
    from .intelligence import Intelligence, ProviderError
    from .native_retained_response import recover
    from .task_reasoning import request_provider
    if not blocked(run):
        return False
    rid = run['id']
    with engine.store.read_snapshot() as store:
        keys = store.record_keys('codex_workflows', rid,
            predicate="json_extract(data,'$.status') IN ('code_review','spec_review')")
        if len(keys) != 1:
            return False
        state = store.get('codex_workflows', keys[0])
        intelligence = Intelligence(store, get_settings(store))
        try:
            if state['status'] == 'code_review':
                attempt = state['attempts'][-1]
                purpose = attempt.get('review_purpose') or f'code_review:{_digest(state["id"])}:{attempt["index"]}'
                clarifications = [attempt.get(field) or {} for field in ('repair_clarification','verification_followup')]
                in_progress = [c for c in clarifications if c.get('status') == 'reviewing']
                if len(in_progress) > 1:
                    return False
                if in_progress:
                    purpose = in_progress[0]['purpose']
            else:
                purpose = spec_purpose(state, state['specifications'][-1])
            reader = copy.copy(engine)
            reader.store = store
            eligible = False
            for role in ('director', 'engineer', 'scout'):
                frozen = _frozen_spec_assignment(reader, rid, purpose, role)
                if not frozen:
                    return False
                key = frozen['id']
                if store.get('decisions', key):
                    continue
                handoff = store.get('handoffs', key)
                progress = store.get('handoff_progress', key)
                if not isinstance(progress, dict) or not isinstance(handoff, dict):
                    return False
                if supplement.legacy_error(run.get('error')):
                    if handoff.get('status') == 'interrupted' and progress.get('mode') != 'final_only' and (
                            progress.get('normal_request') or {}).get('status') == 'response_saved':
                        continue
                    receipt = supplement.rejected_receipt(store,intelligence,rid,key,progress)
                    if not receipt:
                        return False
                    eligible = True
                    continue
                if (run.get('error') == correction.SUPPLEMENT_SCOPE_ERROR
                        and handoff.get('status') == 'interrupted' and progress.get('mode') != 'final_only'
                        and (progress.get('normal_request') or {}).get('status') == 'response_saved'):
                    continue
                final = progress.get('mode') == 'final_only'
                slot = progress.get('finalization' if final else 'normal_request', {})
                if (handoff.get('status') not in ('blocked','interrupted') or slot.get('provider_id') != 'local_codex'
                        or progress.get('pending_tool_calls')
                        or not final and slot.get('turn') != progress.get('turn')):
                    return False
                prepared = slot['prepared']
                if (prepared.get('role') != role or prepared['context'].get('_purpose') != purpose
                        or slot.get('prepared_sha256') not in (None, digest(prepared))):
                    return False
                provider = request_provider(intelligence, prepared)
                if provider['id'] != 'local_codex' or provider['model'] != slot['model']:
                    return False
                if handoff.get('error') == correction.SAME_REQUIREMENT_ERROR or run.get('error') == correction.SUPPLEMENT_SCOPE_ERROR:
                    from . import code_review_catalog as catalog
                    from .implementation_alignment_evidence import _workflow_design
                    ctx = frozen['context']
                    response = slot.get('response') or {}
                    original = slot.get('original_catalog_response', response)
                    if (slot.get('status') != 'response_saved' or not review_document_ids.saved_candidate(
                            store,intelligence,rid,frozen,prepared,original)):
                        return False
                    value = original['json']
                    if 'original_catalog_response' in slot:
                        wire_ctx = {**ctx, catalog.PROTOCOL:prepared['context'][catalog.PROTOCOL]}
                        design = _workflow_design(reader,rid,key,ctx) if ctx.get('definition_adoption_id') else None
                        value = catalog.expand(value,wire_ctx,frozen['schema'],prepared,design=design)
                    if value != response.get('json'):
                        return False
                    if run.get('error') == correction.SUPPLEMENT_SCOPE_ERROR:
                        anchor = (progress.get('alignment_candidate_history') or [{}])[-1]
                        if ({p['kind'] for p in anchor.get(correction.FIELD,[])} != {'missing_evidence_indices'}
                                or supplement.FIELD in prepared['context']):
                            return False
                        correction.verify_archive(reader,rid,role,purpose,key,ctx,anchor)
                        try:
                            correction.validate_correction(value,anchor)
                        except ResearchBlocked as exc:
                            if str(exc) != correction.SUPPLEMENT_SCOPE_ERROR:
                                return False
                        else:
                            return False
                    elif not correction.problems(value,ctx,role):
                        return False
                    eligible = True
                    continue
                if handoff.get('error') in review_document_ids.ERRORS:
                    from .implementation_alignment_evidence import validate_candidate
                    response = slot.get('response') or {}
                    if (slot.get('status') != 'response_saved' or response.get('model') != slot['model']
                            or not review_document_ids.saved_candidate(store,intelligence,rid,frozen,prepared,response)):
                        return False
                    try:
                        validate_candidate(reader,rid,role,key,response['json'],frozen['context'],delivery=True)
                    except ResearchBlocked as exc:
                        if str(exc) != handoff['error'] or not getattr(exc,'alignment_reference_problems',[]):
                            return False
                    else:
                        return False
                    eligible = True
                    continue
                if (handoff.get('error') not in LEGACY_ERRORS or slot.get('status') != 'blocked'
                        or slot.get('response') is not None):
                    return False
                native_key, operation = key_for(intelligence, rid, **prepared)
                saved = store.get('codex_reasoning', native_key, {})
                if (saved.get('id') != native_key or saved.get('operation_id') != operation
                        or saved.get('role') != role or saved.get('schema') != prepared['schema']
                        or saved.get('selection', {}).get('model') != slot['model']):
                    return False
                response = recover(intelligence, saved, strict_source=True, schema_repair=lambda text, meta:
                    local(store, saved, frozen, prepared, text))
                if (response or {}).get('json_format_repair', {}).get('kind') != KIND:
                    return False
                eligible = True
            return eligible
        except (KeyError, TypeError, ValueError, IndexError, ResearchBlocked, ProviderError):
            return False
