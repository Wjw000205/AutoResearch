"""Uncapped rapid-patch code, including audited reuse of legacy replies."""
import copy
import json

from .storage import now

POLICY = 'rapid-code-without-character-limits-v1'
COLLECTION = 'rapid_output_length_recoveries'


def validation_audit(store, run_id, key, prepared, result, progress):
    """Bind context validation to the separately saved length-policy receipt."""
    from .engine import digest, ResearchBlocked
    audit = store.get(COLLECTION, key)
    if not audit:
        return None
    original = progress.get(audit.get('slot')) or {}
    frozen = store.get('handoff_inputs', key) or {}
    if (audit.get('policy') != POLICY or audit.get('run_id') != run_id
            or audit.get('id') != key or digest(result) != audit.get('result_sha256')
            or prepared != original.get('prepared')
            or digest(original) != audit.get('original_request_sha256')
            or digest(frozen.get('schema')) != audit.get('original_schema_sha256')
            or digest(schema_without_code_limits(frozen['schema'])) != audit.get('effective_schema_sha256')
            or digest(progress.get('tool_results', [])) != audit.get('tool_results_sha256')):
        raise ResearchBlocked('Rapid patch length-policy validation receipt changed')
    return {'policy': POLICY, 'audit_id': key,
            'original_schema_sha256': audit['original_schema_sha256'],
            'effective_schema_sha256': digest(schema_without_code_limits(prepared['schema'])),
            'source_request_id': audit['source']['request_id']}


def schema_without_code_limits(schema):
    """Remove only code-string limits; never walk const values or tool schemas."""
    result = copy.deepcopy(schema)
    for branch in result.get('anyOf', []) + result.get('oneOf', []):
        if 'checks' in branch.get('properties', {}):
            branch.update(schema_without_code_limits(branch))
    fields = result.get('properties', {})
    if 'checks' in fields:
        fields['checks'].pop('maxLength', None)
        array = fields.get('edits', {})
        edits = array.get('items', {})
        items = ([edits] if isinstance(edits, dict) else
                 edits if isinstance(edits, list) else [])
        for item in items + array.get('prefixItems', []):
            for name in ('old', 'new'):
                item.get('properties', {}).get(name, {}).pop('maxLength', None)
    return result


def assignment_schema(engine, run_id, purpose, instruction, context, schema):
    """Reuse an exact frozen assignment when only retired code limits differ."""
    from .engine import Harness, digest, ResearchBlocked
    matches = []
    for row in engine.store.list('handoffs', run_id):
        if row.get('purpose') != purpose or row.get('to_role') != 'engineer':
            continue
        frozen = engine.store.get('handoff_inputs', row['id']) or {}
        if frozen.get('instruction') != instruction or frozen.get('context') != context:
            continue
        if schema_without_code_limits(frozen.get('schema', {})) != schema:
            raise ResearchBlocked('Rapid patch contract changed beyond retired code length limits')
        payload = {k: frozen[k] for k in ('instruction', 'context', 'schema')}
        if (frozen.get('run_id') != run_id or frozen.get('id') != row['id']
                or row.get('input_hash') != digest(payload)
                or Harness.decision_key(run_id, 'engineer', purpose, **payload) != row['id']):
            raise ResearchBlocked('Rapid patch frozen assignment identity changed')
        matches.append(frozen['schema'])
    if len(matches) > 1:
        raise ResearchBlocked('Multiple rapid patch assignments match the retired length contract')
    return matches[0] if matches else schema


def retained_response(harness, run_id, key, schema):
    """Authenticate the latest settled reply; do not shorten or replay anything."""
    from .engine import digest
    from .intelligence import parse_json
    from .schema_recovery import derive_source
    engine = harness.engine
    progress = engine.store.get('handoff_progress', key) or {}
    if progress.get('pending_tool_calls'):
        return None
    slot = 'finalization' if progress.get('mode') == 'final_only' else 'normal_request'
    original = progress.get(slot) or {}
    source = derive_source(engine.store, harness.intelligence, run_id, key, original,
                           rapid_code_limits=True)
    if source is None:
        return None
    # A schema correction may bind every other field with const. Check those
    # original constraints too, so an overlong correction cannot alter anchors.
    value = parse_json(source['original_text'],
        schema_without_code_limits(original['prepared']['schema']))
    parse_json(json.dumps(value, ensure_ascii=False, allow_nan=False), schema)
    audit = {
        'id': key, 'run_id': run_id, 'policy': POLICY, 'slot': slot,
        'source': source, 'original_request_sha256': digest(original),
        'original_schema_sha256': digest(engine.store.get('handoff_inputs', key)['schema']),
        'effective_schema_sha256': digest(schema), 'result_sha256': digest(value),
        'tool_results_sha256': digest(progress.get('tool_results', [])),
        'execution_authorized': False, 'scientific_approval': False,
    }
    return value, audit


async def ask(engine, harness, run_id, purpose, instruction, context, schema):
    """New work uses no code cap; old completed replies retain their own IDs."""
    from .engine import Harness, ResearchBlocked, digest
    from .intelligence import ProviderError
    effective = schema_without_code_limits(schema)
    frozen = assignment_schema(engine, run_id, purpose, instruction, context, effective)
    key = Harness.decision_key(run_id, 'engineer', purpose, instruction, context, frozen)
    cached = engine.store.get('decisions', key)
    if cached and engine.store.get(COLLECTION, key):
        # Completion can be interrupted after the decision but before its
        # working-context commit. Finish that local step without replay.
        engine.check_active(run_id)
        progress = engine.store.get('handoff_progress', key)
        audit = engine.store.get(COLLECTION, key)
        prepared = progress[audit['slot']]['prepared']
        validation_audit(engine.store, run_id, key, prepared, cached['result'], progress)
        harness._commit_working_turn(run_id, 'engineer', key, progress, prepared,
                                    cached['result'], audit['source']['request_id'])
    if effective == frozen or cached:
        return await harness.ask(run_id, 'engineer', purpose, instruction, context, frozen)

    async def recover():
        harness._moderation_gate(run_id)
        recovered = await engine._read_consensus_check(
            run_id, retained_response, harness, run_id, key, effective)
        if recovered is None:
            return None
        result, audit = recovered
        # Recheck after the off-thread read, before committing the local result.
        progress = engine.store.get('handoff_progress', key) or {}
        if (digest(progress.get(audit['slot'])) != audit['original_request_sha256']
                or digest(progress.get('tool_results', [])) != audit['tool_results_sha256']):
            raise ResearchBlocked('Rapid patch source changed during length-policy recovery')
        saved = engine.store.get(COLLECTION, key)
        if saved and {k: v for k, v in saved.items() if k != 'created_at'} != audit:
            raise ResearchBlocked('Rapid patch length-policy recovery evidence changed')
        if not saved:
            engine.store.put(COLLECTION, key, {**audit, 'created_at': now()})
        source = audit['source']
        harness._record_receipt(key, {
            'provider': source['provider_identity']['id'],
            'model': source['provider_identity']['model'],
            'request_id': source['request_id'], 'transcript_id': source['transcript_id'],
            'provenance': 'retained_provider_output', 'cache_hit': True})
        value = await harness._commit_with_reference_repair(run_id, 'engineer', purpose, key, result)
        engine.store.event(run_id, 'rapid.code_length_limit_removed',
            '已按无代码字符上限策略复用完整原答复；原回执保留，仍需独立评审和实际 CPU 检查。',
            {'handoff_id': key, 'policy': POLICY, 'request_id': source['request_id'],
             'result_sha256': digest(value), 'execution_authorized': False})
        return value

    result = await recover()
    if result is not None:
        return result
    try:
        return await harness.ask(run_id, 'engineer', purpose, instruction, context, frozen)
    except (ProviderError, ResearchBlocked):
        result = await recover()
        if result is None:
            raise
        return result
