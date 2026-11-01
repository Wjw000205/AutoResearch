"""Read-only recheck of the context paths in a saved blocked review."""
from .constructive_review import ConstructiveReviewError, validate_references
from .storage import redact

CONTEXT_ERROR = 'Constructive review is incomplete: Context reference is not an existing nonempty allowed public path'


def context_reference_status(store, run_id):
    run = store.get('runs', run_id) or {}
    if run.get('error') != CONTEXT_ERROR:
        return None
    checks = []
    for handoff in store.list('handoffs', run_id):
        if handoff.get('status') != 'blocked' or handoff.get('error') != CONTEXT_ERROR:
            continue
        key = handoff['id']
        inputs = store.get('handoff_inputs', key) or {}
        progress = store.get('handoff_progress', key) or {}
        field = 'finalization' if progress.get('mode') == 'final_only' else 'normal_request'
        response = (progress.get(field) or {}).get('response') or {}
        valid = False
        if isinstance(response.get('json'), dict) and isinstance(inputs.get('context'), dict):
            try:
                validate_references(response['json'], inputs['context'])
                valid = True
            except ConstructiveReviewError:
                pass
        checks.append({'handoff_id': key, 'role': handoff.get('to_role'), 'valid': valid})
    return redact({'status': 'ready' if checks and all(row['valid'] for row in checks) else 'blocked',
                   'checks': checks, 'scope': '仅复核已保存回复的公开上下文引用；不授权模型请求、实验或跳过其他门禁。'})
